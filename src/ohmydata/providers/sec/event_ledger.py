"""Atomic, replay-validated event and cursor generations for SEC discovery."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ...core.snapshot import SnapshotStore
from . import _event_ledger_io as io
from .errors import ResourceLimitError, SchemaMismatchError, SnapshotIntegrityError
from .event_discovery import (
    SecDiscoveryBatch,
    SecDiscoveryCursor,
    SecFilingDiscoveryEvent,
    _DiscoveryReplayCache,
)


class SecEventLedgerConflictError(SnapshotIntegrityError):
    """An append disagrees with the committed parent, policy, or event facts."""


@dataclass(frozen=True)
class SecEventLedgerReceipt:
    generation: int
    receipt_id: str
    parent_receipt_id: str | None
    batch_identity: str
    policy_identity: str
    cursor: SecDiscoveryCursor | None
    event_count: int
    new_event_ids: tuple[str, ...]


def _manifest(
    batch: SecDiscoveryBatch,
    head: SecEventLedgerReceipt | None,
    events: dict[str, SecFilingDiscoveryEvent],
) -> tuple[dict[str, object], SecEventLedgerReceipt]:
    if batch.prior_cursor != (None if head is None else head.cursor):
        raise SecEventLedgerConflictError("discovery prior cursor disagrees with ledger")
    if head is not None and batch.policy.policy_identity != head.policy_identity:
        raise SecEventLedgerConflictError("discovery policy disagrees with ledger")
    new_ids: list[str] = []
    for event in batch.events:
        previous = events.get(event.key)
        if previous is not None and previous.metadata_digest != event.metadata_digest:
            raise SecEventLedgerConflictError("discovery event metadata conflicts with ledger")
        if previous is None:
            new_ids.append(event.key)
    generation = 1 if head is None else head.generation + 1
    parent = None if head is None else head.receipt_id
    payload: dict[str, object] = {
        "schema": "sec-event-discovery-ledger-v1",
        "generation": generation,
        "parent_receipt_id": parent,
        "policy_identity": batch.policy.policy_identity,
        "batch_identity": batch.batch_identity,
        "batch": batch.canonical_payload(),
        "new_event_ids": sorted(new_ids),
        "cursor": None
        if batch.candidate_cursor is None
        else batch.candidate_cursor.canonical_payload(),
        "event_count": len(events) + len(new_ids),
    }
    receipt_id = hashlib.sha256(io.canonical_bytes(payload)).hexdigest()
    payload["receipt_id"] = receipt_id
    receipt = SecEventLedgerReceipt(
        generation,
        receipt_id,
        parent,
        batch.batch_identity,
        batch.policy.policy_identity,
        batch.candidate_cursor,
        len(events) + len(new_ids),
        tuple(sorted(new_ids)),
    )
    return payload, receipt


class SecEventDiscoveryLedger:
    """Persist complete discovery batches with an explicit optimistic parent.

    Each operation replays retained source observations. This ledger records
    discovery only; acceptance timestamps do not establish first publication.
    Writers cooperate through a POSIX lock. Caller storage remains caller-owned.
    """

    def __init__(self, root: str | Path, *, store: SnapshotStore) -> None:
        if not isinstance(store, SnapshotStore):
            raise TypeError("store must be a SnapshotStore")
        self.root = Path(root)
        self.store = store

    def _scan(
        self,
        fd: int,
        cache: _DiscoveryReplayCache,
    ) -> tuple[
        SecEventLedgerReceipt | None,
        dict[str, SecFilingDiscoveryEvent],
        dict[str, SecEventLedgerReceipt],
        int,
    ]:
        events: dict[str, SecFilingDiscoveryEvent] = {}
        receipts: dict[str, SecEventLedgerReceipt] = {}
        head = None
        total = 0
        for name in io.generation_names(fd):
            raw = io.read_manifest(fd, name, io.MAX_TOTAL_MANIFEST_BYTES - total)
            total += len(raw)
            stored = io.decode_manifest(raw)
            batch_payload = stored.get("batch")
            if type(batch_payload) is not dict:
                raise SnapshotIntegrityError("event generation missing batch")
            try:
                batch = SecDiscoveryBatch.from_canonical_payload(
                    cast(dict[str, object], batch_payload), self.store, _replay_cache=cache
                )
            except SchemaMismatchError as exc:
                raise SnapshotIntegrityError("event source reconstruction failed") from exc
            if batch.batch_identity in receipts:
                raise SnapshotIntegrityError("duplicate committed discovery batch")
            expected, receipt = _manifest(batch, head, events)
            if io.canonical_bytes(expected) != raw:
                raise SnapshotIntegrityError("event generation reconstruction mismatch")
            for event in batch.events:
                events.setdefault(event.key, event)
            receipts[batch.batch_identity] = receipt
            head = receipt
        return head, events, receipts, total

    def load(self) -> tuple[SecEventLedgerReceipt | None, tuple[SecFilingDiscoveryEvent, ...]]:
        """Return the validated head and key-sorted unique discovered events."""
        cache = _DiscoveryReplayCache(self.store)
        with io.root_directory(self.root, create=False) as fd:
            if fd is None:
                return None, ()
            head, events, _, _ = self._scan(fd, cache)
            return head, tuple(events[key] for key in sorted(events))

    def append(
        self,
        batch: SecDiscoveryBatch,
        *,
        expected_receipt_id: str | None,
    ) -> SecEventLedgerReceipt:
        """Commit events and cursor together, or return an exact retry's receipt."""
        try:
            import fcntl
        except ImportError as exc:
            raise NotImplementedError(
                "SEC event ledger writes require POSIX filesystem support"
            ) from exc
        if type(batch) is not SecDiscoveryBatch:
            raise TypeError("batch must be a SecDiscoveryBatch")
        if expected_receipt_id is not None and (
            type(expected_receipt_id) is not str
            or len(expected_receipt_id) != 64
            or any(char not in "0123456789abcdef" for char in expected_receipt_id)
        ):
            raise ValueError("invalid expected receipt identity")
        # Bound the supplied representation before source replay or filesystem writes.
        proposed = batch.canonical_payload()
        io.canonical_bytes(proposed)
        cache = _DiscoveryReplayCache(self.store)
        with io.root_directory(self.root, create=True) as root_fd:
            fd = cast(int, root_fd)
            try:
                lock = os.open(
                    io.LOCK_NAME,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=fd,
                )
            except FileExistsError:
                lock = os.open(io.LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW, dir_fd=fd)
            try:
                info = os.fstat(lock)
                if not stat.S_ISREG(info.st_mode) or info.st_size != 0 or info.st_nlink != 1:
                    raise SnapshotIntegrityError("unsafe event ledger writer lock")
                fcntl.flock(lock, fcntl.LOCK_EX)
                head, events, receipts, total = self._scan(fd, cache)
                rebuilt = SecDiscoveryBatch.from_canonical_payload(
                    proposed, self.store, _replay_cache=cache
                )
                if rebuilt.batch_identity != batch.batch_identity:
                    raise SnapshotIntegrityError("discovery batch identity mismatch")
                if retry := receipts.get(rebuilt.batch_identity):
                    return retry
                if expected_receipt_id != (None if head is None else head.receipt_id):
                    raise SecEventLedgerConflictError("stale event ledger parent")
                if head is not None and head.generation >= io.MAX_GENERATIONS:
                    raise ResourceLimitError("event generation limit exceeded")
                payload, receipt = _manifest(rebuilt, head, events)
                raw = io.canonical_bytes(payload)
                if total + len(raw) > io.MAX_TOTAL_MANIFEST_BYTES:
                    raise ResourceLimitError("event aggregate manifest budget exceeded")
                io.publish_manifest(fd, receipt.generation, raw)
                return receipt
            finally:
                os.close(lock)


__all__ = ["SecEventDiscoveryLedger", "SecEventLedgerConflictError", "SecEventLedgerReceipt"]

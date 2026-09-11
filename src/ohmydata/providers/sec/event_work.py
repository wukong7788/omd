"""Durable caller-reported work state tied to retained SEC discovery evidence."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import cast

from ...core.snapshot import SnapshotStore
from . import _event_ledger_io as io
from ._event_work_codec import decode_command, decode_spec
from ._event_work_models import (
    SecEventWorkCommand,
    SecEventWorkErrorClass,
    SecEventWorkPhase,
    SecEventWorkReceipt,
    SecEventWorkSpec,
    SecEventWorkState,
)
from ._event_work_transitions import advance
from .errors import ResourceLimitError, SchemaMismatchError, SnapshotIntegrityError
from .event_dependencies import SecDependencyEdge
from .event_discovery import SecDiscoveryBatch, _DiscoveryReplayCache
from .event_ledger import SecEventLedgerConflictError


def _manifest(
    batch: SecDiscoveryBatch,
    spec: SecEventWorkSpec,
    command: SecEventWorkCommand,
    previous: SecEventWorkState | None,
    edges: tuple[SecDependencyEdge, ...],
    head: SecEventWorkReceipt | None,
) -> tuple[
    dict[str, object], SecEventWorkReceipt, SecEventWorkState, tuple[SecDependencyEdge, ...]
]:
    state, registered = advance(batch, spec, command, previous, edges)
    generation = 1 if head is None else head.generation + 1
    parent = None if head is None else head.receipt_id
    payload: dict[str, object] = {
        "schema": "sec-event-work-ledger-v1",
        "generation": generation,
        "parent_receipt_id": parent,
        "work_identity": spec.work_identity,
        "discovery_batch": batch.canonical_payload(),
        "spec": spec.canonical_payload(),
        "command": command.canonical_payload(),
        "state": state.canonical_payload(),
        "registered_edges": [edge.canonical_payload() for edge in registered],
    }
    receipt_id = hashlib.sha256(io.canonical_bytes(payload)).hexdigest()
    payload["receipt_id"] = receipt_id
    return (
        payload,
        SecEventWorkReceipt(
            generation,
            receipt_id,
            parent,
            spec.work_identity,
            command.command_id,
            command.command_identity,
            state.state_identity,
        ),
        state,
        registered,
    )


class SecEventWorkLedger:
    """Record one work item's reports; no report executes or publishes data."""

    def __init__(self, root: str | Path, *, store: SnapshotStore) -> None:
        if not isinstance(store, SnapshotStore):
            raise TypeError("store must be a SnapshotStore")
        self.root, self.store = Path(root), store

    def _scan(self, fd: int, cache: _DiscoveryReplayCache):
        head: SecEventWorkReceipt | None = None
        state: SecEventWorkState | None = None
        batch: SecDiscoveryBatch | None = None
        edges: tuple[SecDependencyEdge, ...] = ()
        receipts: dict[str, SecEventWorkReceipt] = {}
        total = 0
        for name in io.generation_names(fd):
            raw = io.read_manifest(fd, name, io.MAX_TOTAL_MANIFEST_BYTES - total)
            total += len(raw)
            stored = io.decode_manifest(raw)
            try:
                if batch is None:
                    value = stored.get("discovery_batch")
                    if type(value) is not dict:
                        raise SchemaMismatchError("work generation missing discovery batch")
                    batch = SecDiscoveryBatch.from_canonical_payload(
                        cast(dict[str, object], value),
                        self.store,
                        _replay_cache=cache,
                    )
                spec = decode_spec(stored.get("spec"))
                command = decode_command(stored.get("command"))
                if command.command_id in receipts:
                    raise SnapshotIntegrityError("duplicate committed work command")
                expected, receipt, state, edges = _manifest(
                    batch, spec, command, state, edges, head
                )
                if io.canonical_bytes(expected) != raw:
                    raise SnapshotIntegrityError("work generation reconstruction mismatch")
            except SchemaMismatchError as exc:
                raise SnapshotIntegrityError(
                    "work generation evidence reconstruction failed"
                ) from exc
            head = receipt
            receipts[command.command_id] = receipt
        return head, state, edges, receipts, total

    def load(
        self,
    ) -> tuple[SecEventWorkReceipt | None, SecEventWorkState | None, tuple[SecDependencyEdge, ...]]:
        cache = _DiscoveryReplayCache(self.store)
        with io.root_directory(self.root, create=False) as fd:
            if fd is None:
                return None, None, ()
            head, state, edges, _, _ = self._scan(fd, cache)
            return head, state, edges

    def append(
        self,
        discovery_batch: SecDiscoveryBatch,
        spec: SecEventWorkSpec,
        command: SecEventWorkCommand,
        *,
        expected_receipt_id: str | None,
    ) -> SecEventWorkReceipt:
        try:
            import fcntl
        except ImportError as exc:
            raise NotImplementedError("SEC work ledger requires POSIX filesystem support") from exc
        if (
            type(discovery_batch) is not SecDiscoveryBatch
            or type(spec) is not SecEventWorkSpec
            or type(command) is not SecEventWorkCommand
        ):
            raise TypeError("invalid work ledger inputs")
        if expected_receipt_id is not None and (
            type(expected_receipt_id) is not str
            or len(expected_receipt_id) != 64
            or any(char not in "0123456789abcdef" for char in expected_receipt_id)
        ):
            raise ValueError("invalid expected work receipt identity")
        proposed = {
            "discovery_batch": discovery_batch.canonical_payload(),
            "spec": spec.canonical_payload(),
            "command": command.canonical_payload(),
        }
        io.canonical_bytes(proposed)
        # Reconstruct ordinary dataclasses before relying on their values.
        spec = decode_spec(proposed["spec"])
        command = decode_command(proposed["command"])
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
                    raise SnapshotIntegrityError("unsafe work ledger writer lock")
                fcntl.flock(lock, fcntl.LOCK_EX)
                head, state, edges, receipts, total = self._scan(fd, cache)
                batch = SecDiscoveryBatch.from_canonical_payload(
                    proposed["discovery_batch"],
                    self.store,
                    _replay_cache=cache,
                )
                if batch.batch_identity != discovery_batch.batch_identity:
                    raise SnapshotIntegrityError("work discovery identity mismatch")
                if state is not None and (
                    state.spec != spec or state.discovery_batch_identity != batch.batch_identity
                ):
                    raise SecEventLedgerConflictError(
                        "another work or discovery batch occupies ledger"
                    )
                if retry := receipts.get(command.command_id):
                    if retry.command_identity != command.command_identity:
                        raise SecEventLedgerConflictError(
                            "work command id reused with different content"
                        )
                    return retry
                if expected_receipt_id != (None if head is None else head.receipt_id):
                    raise SecEventLedgerConflictError("stale work ledger parent")
                if head is not None and head.generation >= io.MAX_GENERATIONS:
                    raise ResourceLimitError("work generation limit exceeded")
                payload, receipt, _, _ = _manifest(batch, spec, command, state, edges, head)
                raw = io.canonical_bytes(payload)
                if total + len(raw) > io.MAX_TOTAL_MANIFEST_BYTES:
                    raise ResourceLimitError("work aggregate manifest budget exceeded")
                io.publish_manifest(fd, receipt.generation, raw)
                return receipt
            finally:
                os.close(lock)


__all__ = [
    "SecEventWorkCommand",
    "SecEventWorkErrorClass",
    "SecEventWorkLedger",
    "SecEventWorkPhase",
    "SecEventWorkReceipt",
    "SecEventWorkSpec",
    "SecEventWorkState",
]

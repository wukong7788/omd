"""Offline, retained-snapshot discovery of SEC filing acceptance events."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any, Never, cast

from ...core.snapshot import SnapshotStore
from ._event_discovery_models import (
    SecDiscoveryBatch,
    SecDiscoveryCursor,
    SecDiscoveryMode,
    SecDiscoveryPolicy,
    SecDiscoverySource,
    SecFilingDiscoveryEvent,
    SecFilingEventKind,
    SecRootCoverageStatus,
    SecRootDiscoveryResult,
    SecRootDiscoveryWindow,
    _canonical,
    _parse_stamp,
    _stamp,
    canonical_batch_bytes,
)
from .edgar import _MAX_HISTORICAL_FILES, historical_basenames, historical_submission_url
from .errors import CoverageError, ResourceLimitError, SchemaMismatchError

_SCHEMA = 1
_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A"})
_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_ITEM = re.compile(r"^[0-9]+\.[0-9]+$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_MAX_SOURCE_BYTES = 8 * 1024**2
_MAX_TOTAL_BYTES = 64 * 1024**2
_MAX_ROWS = 100_000
_MAX_EVENTS = 10_000
_MAX_STRING = 1024
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 500_000


class _FrozenDict(dict[str, Any]):
    def _readonly(self, *_: Any, **__: Any) -> Never:
        raise TypeError("immutable parsed JSON")

    __setitem__ = __delitem__ = clear = pop = setdefault = update = _readonly

    def popitem(self) -> tuple[str, Any]:
        raise TypeError("immutable parsed JSON")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _strict_json(payload: bytes) -> dict[str, Any]:
    _json_preflight(payload)

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise SchemaMismatchError("invalid submissions JSON") from exc
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise ResourceLimitError("submissions JSON structure limit exceeded")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    if not isinstance(value, dict):
        raise SchemaMismatchError("submissions payload must be an object")
    return value


def _json_preflight(payload: bytes) -> None:
    """Bound structural complexity before handing bytes to Python's JSON decoder."""
    depth = 0
    nodes = 0
    quoted = False
    escaped = False
    for byte in payload:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
            continue
        if byte == 34:
            quoted = True
        elif byte in (123, 91):
            depth += 1
            nodes += 1
        elif byte in (125, 93):
            depth -= 1
            if depth < 0:
                raise SchemaMismatchError("invalid submissions JSON")
        elif byte == 44:
            nodes += 1
        if depth > _MAX_JSON_DEPTH or nodes > _MAX_JSON_NODES:
            raise ResourceLimitError("submissions JSON structure limit exceeded")
    if quoted or depth != 0:
        raise SchemaMismatchError("invalid submissions JSON")


class _DiscoveryReplayCache:
    """Bounded operation-local cache keyed by the complete source descriptor."""

    def __init__(self, store: SnapshotStore, max_total_bytes: int = _MAX_TOTAL_BYTES) -> None:
        if (
            type(max_total_bytes) is not int
            or max_total_bytes <= 0
            or max_total_bytes > _MAX_TOTAL_BYTES
        ):
            raise ValueError("invalid discovery replay cache limit")
        self.store = store
        self.max_total_bytes = max_total_bytes
        self._replays: dict[bytes, tuple[Mapping[str, Any], Mapping[str, Any], int]] = {}
        self._observed_bytes = 0

    def replay(
        self, source: SecDiscoverySource
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], int]:
        key = _canonical(source.canonical_payload())
        replay = self._replays.get(key)
        if replay is not None:
            return replay
        remaining = self.max_total_bytes - self._observed_bytes
        if remaining < 0:
            raise ResourceLimitError("aggregate submissions bytes exceeded")
        replay = self.store.replay_observation(
            source.observation, max_payload_bytes=min(_MAX_SOURCE_BYTES, remaining)
        )
        size = len(replay.payload)
        if self._observed_bytes + size > self.max_total_bytes:
            raise ResourceLimitError("aggregate submissions bytes exceeded")
        self._observed_bytes += size
        parsed = _strict_json(replay.payload)
        value = (
            cast(Mapping[str, Any], _freeze_json(parsed)),
            _FrozenDict(replay.manifest),
            size,
        )
        self._replays[key] = value
        return value


def _bounded_sources(values: Iterable[SecDiscoverySource]) -> tuple[SecDiscoverySource, ...]:
    iterator = iter(values)
    result: list[SecDiscoverySource] = []
    for _ in range(_MAX_HISTORICAL_FILES):
        try:
            result.append(next(iterator))
        except StopIteration:
            return tuple(result)
    try:
        next(iterator)
    except StopIteration:
        return tuple(result)
    raise ResourceLimitError(f"historical source limit exceeded (maximum {_MAX_HISTORICAL_FILES})")


def _validate_source(
    store: SnapshotStore,
    source: SecDiscoverySource,
    cik: str,
    *,
    root: bool,
    basename: str | None = None,
    _replay_cache: _DiscoveryReplayCache | None = None,
) -> tuple[dict[str, Any], int]:
    padded = cik.zfill(10)
    expected_url = (
        f"https://data.sec.gov/submissions/CIK{padded}.json"
        if root
        else historical_submission_url(padded, cast(str, basename))
    )
    if not isinstance(source, SecDiscoverySource) or source.url != expected_url:
        raise SchemaMismatchError("invalid submissions source URL")
    ref = source.observation
    expected_endpoint = "edgar_submissions" if root else "edgar_submissions_history"
    if ref.provider != "sec" or ref.endpoint != expected_endpoint:
        raise SchemaMismatchError("submissions snapshot provider or endpoint mismatch")
    if _replay_cache is None:
        replay = store.replay_observation(ref, max_payload_bytes=_MAX_SOURCE_BYTES)
        payload: Mapping[str, Any] = _strict_json(replay.payload)
        manifest: Mapping[str, Any] = replay.manifest
        size = len(replay.payload)
    else:
        payload, manifest, size = _replay_cache.replay(source)
    request = manifest["canonical_request"]
    if (
        not isinstance(request, dict)
        or request.get("provider") != "sec"
        or request.get("endpoint") != expected_endpoint
        or not isinstance(request.get("parameters"), dict)
        or request.get("fields") != []
        or request["parameters"].get("cik") != padded
    ):
        raise SchemaMismatchError("submissions snapshot request CIK mismatch")
    if root:
        params = request["parameters"]
        accessions = params.get("required_accessions")
        if set(params) == {"cik"}:
            pass
        elif (
            set(params) != {"cik", "required_accessions"}
            or not isinstance(accessions, list)
            or not accessions
            or accessions != sorted(set(accessions))
            or any(
                not isinstance(item, str) or _ACCESSION.fullmatch(item) is None
                for item in accessions
            )
        ):
            raise SchemaMismatchError("submissions root snapshot request mismatch")
    if not root and request["parameters"] != {"cik": padded, "basename": basename}:
        raise SchemaMismatchError("submissions history snapshot request mismatch")
    return cast(dict[str, Any], payload), size


def _submission_rows(
    payload: Mapping[str, Any], padded_cik: str, *, child: bool
) -> list[dict[str, object]]:
    body: Mapping[str, Any]
    if child and "filings" not in payload:
        body = payload
    else:
        filings = payload.get("filings")
        if not isinstance(filings, Mapping) or not isinstance(filings.get("recent"), Mapping):
            raise SchemaMismatchError("recent filings must be an object")
        body = cast(Mapping[str, Any], filings["recent"])
    if str(payload.get("cik", padded_cik)).zfill(10) != padded_cik:
        raise SchemaMismatchError("CIK mismatch")
    columns = (
        "accessionNumber",
        "form",
        "filingDate",
        "reportDate",
        "primaryDocument",
        "acceptanceDateTime",
    )
    if any(not isinstance(body.get(name), (list, tuple)) for name in columns):
        raise SchemaMismatchError("submission columns missing")
    count = len(cast(tuple[object, ...], body["accessionNumber"]))
    if any(len(cast(tuple[object, ...], body[name])) != count for name in columns):
        raise SchemaMismatchError("submission column length mismatch")
    items = body.get("items")
    if items is not None and (not isinstance(items, (list, tuple)) or len(items) != count):
        raise SchemaMismatchError("submission item column mismatch")
    if count > _MAX_ROWS:
        raise ResourceLimitError("filing row limit exceeded")
    return [
        {name: cast(tuple[object, ...], body[name])[index] for name in columns}
        | ({"items": cast(tuple[object, ...], items)[index]} if items is not None else {})
        for index in range(count)
    ]


def discover_sec_filing_events(
    store: SnapshotStore,
    root_source: SecDiscoverySource,
    historical_sources: Iterable[SecDiscoverySource],
    *,
    policy: SecDiscoveryPolicy,
    prior_cursor: SecDiscoveryCursor | None,
    _replay_cache: _DiscoveryReplayCache | None = None,
    _root_only: bool = False,
) -> SecDiscoveryBatch:
    """Replay a complete retained SEC submissions closure and emit selected events."""
    if (
        not isinstance(store, SnapshotStore)
        or type(policy) is not SecDiscoveryPolicy
        or (prior_cursor is not None and type(prior_cursor) is not SecDiscoveryCursor)
    ):
        raise TypeError("invalid discovery input")
    if (
        policy.mode is SecDiscoveryMode.INCREMENTAL
        and prior_cursor is not None
        and (
            policy.acceptance_lower > prior_cursor.acceptance_at - policy.overlap
            or policy.acceptance_upper < prior_cursor.acceptance_at
        )
    ):
        raise ValueError("incremental window excludes declared overlap")
    _replay_cache = _replay_cache or _DiscoveryReplayCache(store)
    children = _bounded_sources(historical_sources)
    if _root_only and children:
        raise ValueError("root-only discovery cannot include historical sources")
    padded = policy.cik.zfill(10)
    root, root_bytes = _validate_source(
        store, root_source, policy.cik, root=True, _replay_cache=_replay_cache
    )
    filings = root.get("filings")
    if not isinstance(filings, Mapping) or not isinstance(filings.get("files"), (list, tuple)):
        raise SchemaMismatchError("historical submissions references missing")
    names = historical_basenames(root, padded)
    if not _root_only:
        if len(names) != len(children):
            raise CoverageError("historical submissions closure mismatch")
        expected = tuple(historical_submission_url(padded, name) for name in names)
        if tuple(sorted(source.url for source in children)) != tuple(sorted(expected)) or len(
            {source.url for source in children}
        ) != len(children):
            raise CoverageError("historical submissions closure mismatch")
    children = tuple(sorted(children, key=lambda source: source.url))
    names_by_url = {historical_submission_url(padded, name): name for name in names}
    payloads = [(root_source, root, False, root_bytes)]
    for source in children:
        child, size = _validate_source(
            store,
            source,
            policy.cik,
            root=False,
            basename=names_by_url[source.url],
            _replay_cache=_replay_cache,
        )
        payloads.append((source, child, True, size))
    rows: dict[str, tuple[dict[str, object], list[str]]] = {}
    observed_bytes = sum(size for _, _, _, size in payloads)
    if observed_bytes > _MAX_TOTAL_BYTES:
        raise ResourceLimitError("aggregate submissions bytes exceeded")
    row_count = 0
    for source, payload, child, _ in payloads:
        for row in _submission_rows(payload, padded, child=child):
            row_count += 1
            if row_count > _MAX_ROWS:
                raise ResourceLimitError("aggregate filing row limit exceeded")
            accession = row["accessionNumber"]
            if not isinstance(accession, str) or not _ACCESSION.fullmatch(accession):
                raise SchemaMismatchError("invalid accession")
            prior = rows.get(accession)
            if prior is not None and prior[0] != row:
                raise SchemaMismatchError("conflicting submission metadata")
            if prior is None:
                rows[accession] = (row, [source.observation.observation_identity])
            elif source.observation.observation_identity not in prior[1]:
                prior[1].append(source.observation.observation_identity)
    events: list[SecFilingDiscoveryEvent] = []
    candidates: list[SecDiscoveryCursor] = []
    for accession, (row, identities) in rows.items():
        form, filing_date, report_date, document = (
            row["form"],
            row["filingDate"],
            row["reportDate"],
            row["primaryDocument"],
        )
        if (
            not all(
                isinstance(value, str) and len(value.encode()) <= _MAX_STRING
                for value in (form, filing_date, document)
            )
            or report_date is not None
            and (not isinstance(report_date, str) or len(report_date.encode()) > _MAX_STRING)
        ):
            raise SchemaMismatchError("invalid submission metadata")
        acceptance_native = row["acceptanceDateTime"]
        accepted = _parse_stamp(acceptance_native, "acceptance timestamp")
        if form not in policy.forms or not (
            policy.acceptance_lower <= accepted <= policy.acceptance_upper
        ):
            continue
        candidates.append(SecDiscoveryCursor(accepted, accession))
        items = row.get("items")
        if items is not None and (not isinstance(items, str) or len(items.encode()) > _MAX_STRING):
            raise SchemaMismatchError("invalid submission items")
        kinds = [SecFilingEventKind.SEC_FILING_ACCEPTED]
        if form in {"8-K", "8-K/A"} and items:
            tokens = items.split(",")
            if not tokens or any(_ITEM.fullmatch(token) is None for token in tokens):
                raise SchemaMismatchError("invalid submission items")
            if "2.02" in tokens:
                kinds.append(SecFilingEventKind.SEC_8K_ITEM_2_02)
        metadata = {
            "accession": accession,
            "form": form,
            "filing_date": filing_date,
            "report_date": report_date,
            "acceptance_native": acceptance_native,
            "acceptance_at": _stamp(accepted),
            "primary_document": document,
            "items": items,
        }
        digest = hashlib.sha256(_canonical(metadata)).hexdigest()
        for kind in kinds:
            if len(events) >= _MAX_EVENTS:
                raise ResourceLimitError("discovery event limit exceeded")
            key = hashlib.sha256(
                _canonical(
                    {
                        "schema_version": _SCHEMA,
                        "provider": "sec",
                        "cik": policy.cik,
                        "accession": accession,
                        "kind": kind.value,
                    }
                )
            ).hexdigest()
            events.append(
                SecFilingDiscoveryEvent(
                    key,
                    policy.cik,
                    accession,
                    kind,
                    cast(str, form),
                    cast(str, filing_date),
                    report_date,
                    cast(str, acceptance_native),
                    accepted,
                    cast(str, document),
                    items,
                    digest,
                    tuple(sorted(identities)),
                )
            )
    events.sort(key=lambda item: item.key)
    candidate = max(
        ([prior_cursor] if prior_cursor is not None else []) + candidates,
        key=lambda item: (item.acceptance_at, item.accession),
        default=None,
    )
    provisional = SecDiscoveryBatch(
        policy, prior_cursor, (root_source,) + children, tuple(events), candidate, ""
    )
    identity = hashlib.sha256(canonical_batch_bytes(provisional)).hexdigest()
    return SecDiscoveryBatch(
        policy, prior_cursor, provisional.sources, provisional.events, candidate, identity
    )


def discover_sec_incremental_events_from_root(
    store: SnapshotStore,
    root_source: SecDiscoverySource,
    *,
    policy: SecDiscoveryPolicy,
    prior_cursor: SecDiscoveryCursor | None,
    _replay_cache: _DiscoveryReplayCache | None = None,
) -> SecRootDiscoveryResult:
    """Discover from one retained root when its recent rows prove the full window.

    SEC root `files` date ranges describe filing dates, not acceptance timestamps,
    and are not used to establish the covered lower boundary. Windows extending
    before the oldest acceptance in the recent rows require full reconciliation.
    """
    if policy.mode is not SecDiscoveryMode.INCREMENTAL:
        raise ValueError("root-only discovery requires incremental mode")
    cache = _replay_cache or _DiscoveryReplayCache(store)
    root, _ = _validate_source(store, root_source, policy.cik, root=True, _replay_cache=cache)
    body_rows = _submission_rows(root, policy.cik.zfill(10), child=False)
    accepted: list[datetime] = []
    for row in body_rows:
        accepted.append(_parse_stamp(row.get("acceptanceDateTime"), "acceptance timestamp"))
    fetched_at = root_source.observation.snapshot_fetched_at.astimezone(UTC)
    if not accepted:
        return SecRootDiscoveryResult(
            SecRootCoverageStatus.NEEDS_RECONCILE,
            root_source,
            None,
            fetched_at,
            None,
            "root has no recent acceptance rows to prove coverage",
        )
    ordered = all(left >= right for left, right in pairwise(accepted))
    oldest = min(accepted)
    newest = max(accepted)
    reason: str | None = None
    if not ordered:
        reason = "root acceptance rows are not in descending timestamp order"
    elif policy.acceptance_upper > fetched_at:
        reason = "requested window extends beyond root observation time"
    elif policy.acceptance_lower < oldest:
        reason = "requested window begins before the root recent-row coverage boundary"
    if reason is not None:
        return SecRootDiscoveryResult(
            SecRootCoverageStatus.NEEDS_RECONCILE,
            root_source,
            oldest,
            min(fetched_at, newest),
            None,
            reason,
        )
    batch = discover_sec_filing_events(
        store,
        root_source,
        (),
        policy=policy,
        prior_cursor=prior_cursor,
        _replay_cache=cache,
        _root_only=True,
    )
    window = SecRootDiscoveryWindow(
        policy,
        prior_cursor,
        root_source,
        batch.events,
        batch.candidate_cursor,
        oldest,
        fetched_at,
    )
    refs = historical_basenames(root, policy.cik.zfill(10))
    if refs:
        return SecRootDiscoveryResult(
            SecRootCoverageStatus.NEEDS_RECONCILE,
            root_source,
            oldest,
            fetched_at,
            window,
            "root events cover recent rows only; historical references remain unscanned",
        )
    return SecRootDiscoveryResult(
        SecRootCoverageStatus.COMPLETE,
        root_source,
        oldest,
        fetched_at,
        window,
    )


__all__ = [
    "SecDiscoveryBatch",
    "SecDiscoveryCursor",
    "SecDiscoveryMode",
    "SecDiscoveryPolicy",
    "SecDiscoverySource",
    "SecFilingDiscoveryEvent",
    "SecFilingEventKind",
    "SecRootCoverageStatus",
    "SecRootDiscoveryResult",
    "canonical_batch_bytes",
    "discover_sec_filing_events",
    "discover_sec_incremental_events_from_root",
]

"""Immutable restart bundles for locally observed SEC financial productions."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from ...core import RequestSpec, SnapshotMode, SnapshotObservationRef, SnapshotRef, SnapshotStore
from ._observed_financial_bundle_codec import (
    _COMMIT,
    _MAX_BUNDLE_BYTES,
    _MAX_COMMITS,
    _MAX_DEPENDENCY_BYTES,
    _MAX_PAYLOAD,
    _MAX_PRODUCTIONS,
    _MAX_QUALITY,
    _MAX_ROWS,
    _PRODUCTION,
    _QUALITY,
    _RECEIPT,
    _REQUEST,
    _SCHEMA,
    _canonical,
    _commit,
    _decode,
    _identity,
    _limit,
    _production,
    _quality,
    _receipt,
    _stamp,
    _time,
    _utc,
)
from ._observed_financial_bundle_codec import (
    validate_existing_production as _validate_existing,
)
from ._observed_financial_bundle_graph import validate_lifecycle as _lifecycle
from .observed_financial_replay import (
    SecObservedFinancialConsumerCommit,
    SecObservedFinancialQualityRecord,
)
from .observed_xbrl_financials import (
    SecObservedFinancialProduction,
    _restore_sec_observed_financial_production,
)
from .pit import SecQualityStatus
from .sgml_financials import SecSgmlFinancialsRequest

ObservationResolver = Callable[[str], tuple[SnapshotStore, SnapshotObservationRef]]
T = TypeVar("T")


def _take(values: Iterable[T], maximum: int, name: str) -> tuple[T, ...]:
    result: list[T] = []
    for value in values:
        if len(result) >= maximum:
            raise ValueError(f"SEC observed financial bundle {name} limit exceeded")
        result.append(value)
    return tuple(result)


@dataclass(frozen=True)
class _Dependencies:
    resolver: ObservationResolver
    maximum: int
    used: int = 0
    cache: dict[str, tuple[SnapshotStore, SnapshotObservationRef, bytes]] | None = None

    def resolve(self, claim: object) -> tuple[SnapshotStore, SnapshotObservationRef, bytes]:
        if not isinstance(claim, dict) or frozenset(claim) != _RECEIPT:
            raise ValueError("invalid SEC observed financial receipt")
        if any(type(claim[key]) is not str for key in _RECEIPT):
            raise ValueError("invalid SEC observed financial receipt")
        for key in (
            "observation_identity",
            "snapshot_identity",
            "fact_version",
            "request_identity",
            "response_sha256",
        ):
            _identity(claim[key], key)
        if claim["mode"] not in {mode.value for mode in SnapshotMode}:
            raise ValueError("invalid SEC observed financial receipt")
        _time(claim["snapshot_fetched_at"], "snapshot_fetched_at")
        identity = _identity(claim["observation_identity"], "observation_identity")
        cache = self.cache if self.cache is not None else {}
        prior = cache.get(identity)
        if prior is not None:
            if _receipt(prior[1]) != claim:
                raise ValueError("conflicting SEC observed financial receipt claim")
            return prior
        resolved = self.resolver(identity)
        if (
            not isinstance(resolved, tuple)
            or len(resolved) != 2
            or type(resolved[0]) is not SnapshotStore
            or type(resolved[1]) is not SnapshotObservationRef
        ):
            raise TypeError(
                "resolve_observation must return (SnapshotStore, SnapshotObservationRef)"
            )
        store, observation = resolved
        if observation.observation_identity != identity or _receipt(observation) != claim:
            raise ValueError("resolved SEC observed financial receipt mismatch")
        remaining = self.maximum - self.used
        if remaining <= 0:
            raise ValueError("SEC observed financial dependency byte limit exceeded")
        replay = store.replay_observation(
            observation, max_payload_bytes=min(_MAX_PAYLOAD, remaining)
        )
        payload = replay.payload
        if len(payload) > _MAX_PAYLOAD or len(payload) > remaining:
            raise ValueError("SEC observed financial dependency byte limit exceeded")
        cache[identity] = (store, observation, payload)
        object.__setattr__(self, "used", self.used + len(payload))
        object.__setattr__(self, "cache", cache)
        return cache[identity]


def _restore(
    entry: object, dependencies: _Dependencies, rows_left: int
) -> SecObservedFinancialProduction:
    if not isinstance(entry, dict) or frozenset(entry) != _PRODUCTION:
        raise ValueError("invalid SEC observed financial production receipt")
    request_data = entry["request"]
    if (
        not isinstance(request_data, dict)
        or frozenset(request_data) != _REQUEST
        or type(request_data["statement_types"]) is not list
        or any(type(x) is not str for x in request_data["statement_types"])
        or type(request_data["include_dimensions"]) is not bool
        or any(
            type(request_data[k]) is not str for k in ("symbol", "cik", "accession_number", "form")
        )
    ):
        raise ValueError("invalid SEC observed financial request receipt")
    request = SecSgmlFinancialsRequest(
        request_data["symbol"],
        request_data["cik"],
        request_data["accession_number"],
        request_data["form"],
        tuple(request_data["statement_types"]),
        request_data["include_dimensions"],
    )
    source_store, source, _ = dependencies.resolve(entry["source_receipt"])
    package_store, package, _ = dependencies.resolve(entry["package_receipt"])
    output_store, output, _ = dependencies.resolve(entry["output_receipt"])
    restored = _restore_sec_observed_financial_production(
        source_store=source_store,
        source_observation=source,
        package_store=package_store,
        package_observation=package,
        output_store=output_store,
        output_observation=output,
        request=request,
        produced_at=_time(entry["produced_at"], "produced_at"),
        max_rows=min(10_000, rows_left),
    )
    if restored.production_identity != _identity(
        entry["production_identity"], "production_identity"
    ):
        raise ValueError("SEC observed financial production identity mismatch")
    return restored


@dataclass(frozen=True)
class SecObservedFinancialBundle:
    batch_identity: str
    productions: tuple[SecObservedFinancialProduction, ...]
    quality_records: tuple[SecObservedFinancialQualityRecord, ...]
    consumer_commits: tuple[SecObservedFinancialConsumerCommit, ...]
    captured_at: datetime


def _prepare(
    *,
    batch_identity: str,
    productions: Iterable[SecObservedFinancialProduction],
    quality_records: Iterable[SecObservedFinancialQualityRecord],
    consumer_commits: Iterable[SecObservedFinancialConsumerCommit],
    captured_at: datetime,
    resolver: ObservationResolver,
    max_productions: int,
    max_quality_records: int,
    max_commits: int,
    max_rows: int,
    max_dependency_bytes: int,
) -> SecObservedFinancialBundle:
    if not isinstance(batch_identity, str) or not batch_identity:
        raise ValueError("invalid SEC observed financial bundle batch identity")
    captured = _utc(captured_at, "captured_at")
    production_items = _take(productions, max_productions, "production")
    quality_items = _take(quality_records, max_quality_records, "quality record")
    commit_items = _take(consumer_commits, max_commits, "commit")
    if any(type(x) is not SecObservedFinancialQualityRecord for x in quality_items) or any(
        type(x) is not SecObservedFinancialConsumerCommit for x in commit_items
    ):
        raise TypeError("quality records and commits must be SEC observed financial values")
    rows = sum(len(item.vintage.rows) for item in production_items)
    if rows > max_rows:
        raise ValueError("SEC observed financial bundle row limit exceeded")
    for item in production_items:
        _validate_existing(item)
    dependencies = _Dependencies(resolver, max_dependency_bytes)
    restored_map: dict[str, SecObservedFinancialProduction] = {}
    for item in production_items:
        restored = _restore(_production(item), dependencies, max_rows)
        prior = restored_map.get(restored.production_identity)
        if prior is not None and _production(prior) != _production(restored):
            raise ValueError("conflicting SEC observed financial production receipt")
        restored_map[restored.production_identity] = restored
    qualities: dict[str, SecObservedFinancialQualityRecord] = {}
    for item in quality_items:
        expected = SecObservedFinancialQualityRecord(
            item.production_identity,
            item.quality_policy_version,
            item.status,
            item.recorded_at,
            item.supersedes_quality_record_id,
        )
        if expected.quality_record_id != item.quality_record_id:
            raise ValueError("SEC observed financial quality identity mismatch")
        prior = qualities.get(item.quality_record_id)
        if prior is not None and _quality(prior) != _quality(item):
            raise ValueError("conflicting SEC observed financial quality receipt")
        qualities[item.quality_record_id] = item
    commits: dict[str, SecObservedFinancialConsumerCommit] = {}
    for item in commit_items:
        expected = SecObservedFinancialConsumerCommit(
            item.production_identity,
            item.quality_record_id,
            item.consumer_dataset_identity,
            item.committed_at,
        )
        if expected.commit_id != item.commit_id:
            raise ValueError("SEC observed financial commit identity mismatch")
        prior = commits.get(item.commit_id)
        if prior is not None and _commit(prior) != _commit(item):
            raise ValueError("conflicting SEC observed financial commit receipt")
        commits[item.commit_id] = item
    ordered_productions = tuple(sorted(restored_map.values(), key=lambda x: x.production_identity))
    ordered_qualities = tuple(sorted(qualities.values(), key=lambda x: x.quality_record_id))
    ordered_commits = tuple(sorted(commits.values(), key=lambda x: x.commit_id))
    _lifecycle(ordered_productions, ordered_qualities, ordered_commits)
    if any(
        captured < value
        for value in [
            *(item.produced_at for item in ordered_productions),
            *(item.evidence.source_observation.snapshot_fetched_at for item in ordered_productions),
            *(
                item.evidence.package_observation.snapshot_fetched_at
                for item in ordered_productions
            ),
            *(item.output_observation.snapshot_fetched_at for item in ordered_productions),
            *(item.recorded_at for item in ordered_qualities),
            *(item.committed_at for item in ordered_commits),
        ]
    ):
        raise ValueError("captured_at precedes bundle evidence")
    return SecObservedFinancialBundle(
        batch_identity, ordered_productions, ordered_qualities, ordered_commits, captured
    )


def write_sec_observed_financial_bundle(
    *,
    store: SnapshotStore,
    batch_identity: str,
    productions: Iterable[SecObservedFinancialProduction],
    quality_records: Iterable[SecObservedFinancialQualityRecord],
    consumer_commits: Iterable[SecObservedFinancialConsumerCommit],
    captured_at: datetime,
    resolve_observation: ObservationResolver,
    max_productions: int = _MAX_PRODUCTIONS,
    max_quality_records: int = _MAX_QUALITY,
    max_commits: int = _MAX_COMMITS,
    max_rows: int = _MAX_ROWS,
    max_bundle_bytes: int = _MAX_BUNDLE_BYTES,
    max_dependency_bytes: int = _MAX_DEPENDENCY_BYTES,
) -> SnapshotRef:
    """Validate a complete restart closure before atomically writing its sole bundle snapshot."""
    limits = (
        _limit(max_productions, "max_productions", _MAX_PRODUCTIONS),
        _limit(max_quality_records, "max_quality_records", _MAX_QUALITY),
        _limit(max_commits, "max_commits", _MAX_COMMITS),
        _limit(max_rows, "max_rows", _MAX_ROWS),
        _limit(max_bundle_bytes, "max_bundle_bytes", _MAX_BUNDLE_BYTES),
        _limit(max_dependency_bytes, "max_dependency_bytes", _MAX_DEPENDENCY_BYTES),
    )
    # Reject an oversized caller envelope before replaying or parsing dependencies.
    captured = _utc(captured_at, "captured_at")
    production_items = _take(productions, limits[0], "production")
    quality_items = _take(quality_records, limits[1], "quality record")
    commit_items = _take(consumer_commits, limits[2], "commit")
    if any(type(item) is not SecObservedFinancialProduction for item in production_items):
        raise TypeError("productions must contain SecObservedFinancialProduction values")
    if any(type(item) is not SecObservedFinancialQualityRecord for item in quality_items) or any(
        type(item) is not SecObservedFinancialConsumerCommit for item in commit_items
    ):
        raise TypeError("quality records and commits must be SEC observed financial values")
    _canonical(
        {
            "schema": _SCHEMA,
            "batch_identity": batch_identity,
            "captured_at": _stamp(captured),
            "productions": [_production(item) for item in production_items],
            "quality_records": [_quality(item) for item in quality_items],
            "consumer_commits": [_commit(item) for item in commit_items],
        },
        limits[4],
    )
    bundle = _prepare(
        batch_identity=batch_identity,
        productions=production_items,
        quality_records=quality_items,
        consumer_commits=commit_items,
        captured_at=captured,
        resolver=resolve_observation,
        max_productions=limits[0],
        max_quality_records=limits[1],
        max_commits=limits[2],
        max_rows=limits[3],
        max_dependency_bytes=limits[5],
    )
    payload = _canonical(
        {
            "schema": _SCHEMA,
            "batch_identity": bundle.batch_identity,
            "captured_at": _stamp(bundle.captured_at),
            "productions": [_production(x) for x in bundle.productions],
            "quality_records": [_quality(x) for x in bundle.quality_records],
            "consumer_commits": [_commit(x) for x in bundle.consumer_commits],
        },
        limits[4],
    )
    return store.write(
        RequestSpec("sec", "observed-financial-bundle", {"batch_identity": bundle.batch_identity}),
        payload,
        bundle.captured_at,
        _SCHEMA,
        SnapshotMode.FROZEN,
    )


def load_sec_observed_financial_bundle(
    *,
    store: SnapshotStore,
    bundle_ref: SnapshotRef,
    resolve_observation: ObservationResolver,
    max_productions: int = _MAX_PRODUCTIONS,
    max_quality_records: int = _MAX_QUALITY,
    max_commits: int = _MAX_COMMITS,
    max_rows: int = _MAX_ROWS,
    max_bundle_bytes: int = _MAX_BUNDLE_BYTES,
    max_dependency_bytes: int = _MAX_DEPENDENCY_BYTES,
) -> SecObservedFinancialBundle:
    """Load a bundle without writing; every retained dependency is replayed and rebuilt."""
    limits = (
        _limit(max_productions, "max_productions", _MAX_PRODUCTIONS),
        _limit(max_quality_records, "max_quality_records", _MAX_QUALITY),
        _limit(max_commits, "max_commits", _MAX_COMMITS),
        _limit(max_rows, "max_rows", _MAX_ROWS),
        _limit(max_bundle_bytes, "max_bundle_bytes", _MAX_BUNDLE_BYTES),
        _limit(max_dependency_bytes, "max_dependency_bytes", _MAX_DEPENDENCY_BYTES),
    )
    if (
        bundle_ref.provider != "sec"
        or bundle_ref.endpoint != "observed-financial-bundle"
        or bundle_ref.mode is not SnapshotMode.FROZEN
        or bundle_ref.serialization_identifier != _SCHEMA
    ):
        raise ValueError("not a SEC observed financial bundle snapshot")
    replay = store.replay(bundle_ref, max_payload_bytes=limits[4])
    data = _decode(replay.payload, limits[4])
    expected = RequestSpec(
        "sec", "observed-financial-bundle", {"batch_identity": data["batch_identity"]}
    )
    if (
        bundle_ref.request_identity != expected.request_identity
        or replay.manifest["canonical_request"] != expected.canonical_payload
    ):
        raise ValueError("bundle batch identity does not match snapshot request")
    if _time(replay.manifest["retrieved_at"], "captured_at") != _time(
        data["captured_at"], "captured_at"
    ):
        raise ValueError("bundle capture does not match manifest")
    batch = data["batch_identity"]
    productions_raw, qualities_raw, commits_raw = (
        data["productions"],
        data["quality_records"],
        data["consumer_commits"],
    )
    if (
        not isinstance(batch, str)
        or not isinstance(productions_raw, list)
        or not isinstance(qualities_raw, list)
        or not isinstance(commits_raw, list)
    ):
        raise TypeError("invalid SEC observed financial bundle fields")
    if (
        len(productions_raw) > limits[0]
        or len(qualities_raw) > limits[1]
        or len(commits_raw) > limits[2]
    ):
        raise ValueError("SEC observed financial bundle record limit exceeded")

    def quality(value: object) -> SecObservedFinancialQualityRecord:
        if (
            not isinstance(value, dict)
            or frozenset(value) != _QUALITY
            or not isinstance(value["quality_policy_version"], str)
            or not isinstance(value["status"], str)
            or value["supersedes_quality_record_id"] is not None
            and not isinstance(value["supersedes_quality_record_id"], str)
        ):
            raise ValueError("invalid SEC observed financial quality receipt")
        item = SecObservedFinancialQualityRecord(
            _identity(value["production_identity"], "production_identity"),
            value["quality_policy_version"],
            SecQualityStatus(value["status"]),
            _time(value["recorded_at"], "recorded_at"),
            value["supersedes_quality_record_id"],
        )
        if item.quality_record_id != _identity(value["quality_record_id"], "quality_record_id"):
            raise ValueError("SEC observed financial quality identity mismatch")
        return item

    def commit(value: object) -> SecObservedFinancialConsumerCommit:
        if (
            not isinstance(value, dict)
            or frozenset(value) != _COMMIT
            or not isinstance(value["consumer_dataset_identity"], str)
        ):
            raise ValueError("invalid SEC observed financial commit receipt")
        item = SecObservedFinancialConsumerCommit(
            _identity(value["production_identity"], "production_identity"),
            _identity(value["quality_record_id"], "quality_record_id"),
            _identity(value["consumer_dataset_identity"], "consumer_dataset_identity"),
            _time(value["committed_at"], "committed_at"),
        )
        if item.commit_id != _identity(value["commit_id"], "commit_id"):
            raise ValueError("SEC observed financial commit identity mismatch")
        return item

    dependencies = _Dependencies(resolve_observation, limits[5])
    rows = 0
    restored: list[SecObservedFinancialProduction] = []
    for value in productions_raw:
        if rows >= limits[3]:
            raise ValueError("SEC observed financial bundle row limit exceeded")
        production = _restore(value, dependencies, limits[3] - rows)
        rows += len(production.vintage.rows)
        if rows > limits[3]:
            raise ValueError("SEC observed financial bundle row limit exceeded")
        restored.append(production)
    qualities, commits = (
        tuple(quality(x) for x in qualities_raw),
        tuple(commit(x) for x in commits_raw),
    )
    bundle = SecObservedFinancialBundle(
        batch,
        tuple(sorted(restored, key=lambda x: x.production_identity)),
        tuple(sorted(qualities, key=lambda x: x.quality_record_id)),
        tuple(sorted(commits, key=lambda x: x.commit_id)),
        _time(data["captured_at"], "captured_at"),
    )
    _lifecycle(bundle.productions, bundle.quality_records, bundle.consumer_commits)
    if any(
        bundle.captured_at < value
        for value in [
            *(item.produced_at for item in bundle.productions),
            *(item.evidence.source_observation.snapshot_fetched_at for item in bundle.productions),
            *(item.evidence.package_observation.snapshot_fetched_at for item in bundle.productions),
            *(item.output_observation.snapshot_fetched_at for item in bundle.productions),
            *(item.recorded_at for item in bundle.quality_records),
            *(item.committed_at for item in bundle.consumer_commits),
        ]
    ):
        raise ValueError("captured_at precedes bundle evidence")
    return bundle


__all__ = [
    "SecObservedFinancialBundle",
    "load_sec_observed_financial_bundle",
    "write_sec_observed_financial_bundle",
]

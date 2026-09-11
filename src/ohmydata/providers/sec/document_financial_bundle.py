"""Immutable lifecycle bundles with complete document source/parser restoration."""

from __future__ import annotations

from collections.abc import Iterable
from copy import copy
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from ...core import RequestSpec, SnapshotMode, SnapshotRef, SnapshotStore
from ._document_bundle_codec import Dependencies, commit, quality
from ._observed_financial_bundle_codec import (
    _canonical,
    _commit,
    _identity,
    _quality,
    _receipt,
    _stamp,
    _time,
    _utc,
)
from ._observed_financial_bundle_graph import validate_lifecycle
from .document_financial_replay import _admit
from .document_financials import (
    SecDocumentFinancialProduction,
    restore_sec_document_financial_production,
)
from .document_source import ObservationResolver
from .event_discovery import _strict_json
from .observed_financial_replay import (
    SecObservedFinancialConsumerCommit,
    SecObservedFinancialQualityRecord,
    _bounded,
    _deduplicate_commits,
    _deduplicate_quality,
    _limit,
)

_SCHEMA = "sec-document-financial-bundle-v1"
_ENDPOINT = "document-financial-bundle"
_MAX_BYTES = 8 * 1024 * 1024
_MAX_DEPENDENCIES = 32 * 1024 * 1024
_TOP = {
    "schema",
    "batch_identity",
    "captured_at",
    "productions",
    "quality_records",
    "consumer_commits",
}


@dataclass(frozen=True)
class SecDocumentFinancialBundle:
    batch_identity: str
    productions: tuple[SecDocumentFinancialProduction, ...]
    quality_records: tuple[SecObservedFinancialQualityRecord, ...]
    consumer_commits: tuple[SecObservedFinancialConsumerCommit, ...]
    captured_at: datetime


def _spec(batch: str) -> RequestSpec:
    return RequestSpec("sec", _ENDPOINT, {"batch_identity": batch})


def _entry(item: SecDocumentFinancialProduction) -> dict[str, object]:
    return {
        "production_identity": item.production_identity,
        "output_receipt": _receipt(item.output_observation),
    }


def _encode(bundle: SecDocumentFinancialBundle, maximum: int) -> bytes:
    return _canonical(
        {
            "schema": _SCHEMA,
            "batch_identity": bundle.batch_identity,
            "captured_at": _stamp(bundle.captured_at),
            "productions": [_entry(item) for item in bundle.productions],
            "quality_records": [_quality(item) for item in bundle.quality_records],
            "consumer_commits": [_commit(item) for item in bundle.consumer_commits],
        },
        maximum,
    )


def _bundle(
    batch: str,
    productions: tuple[SecDocumentFinancialProduction, ...],
    qualities: tuple[SecObservedFinancialQualityRecord, ...],
    commits: tuple[SecObservedFinancialConsumerCommit, ...],
    captured: datetime,
) -> SecDocumentFinancialBundle:
    if type(batch) is not str or not batch or len(batch) > 1024:
        raise ValueError("document bundle batch identity must have 1..1024 characters")
    captured = _utc(captured, "captured_at")
    qualities = _deduplicate_quality(qualities)
    commits = _deduplicate_commits(commits)
    validate_lifecycle(productions, qualities, commits)
    if any(
        value > captured
        for value in (
            *(item.produced_at for item in productions),
            *(item.recorded_at for item in qualities),
            *(item.committed_at for item in commits),
        )
    ):
        raise ValueError("document bundle capture precedes evidence")
    return SecDocumentFinancialBundle(
        batch,
        tuple(sorted(productions, key=lambda item: item.production_identity)),
        tuple(sorted(qualities, key=lambda item: item.quality_record_id)),
        tuple(sorted(commits, key=lambda item: item.commit_id)),
        captured,
    )


def _restore(entry: object, dependencies: Dependencies) -> SecDocumentFinancialProduction:
    if not isinstance(entry, dict) or set(entry) != {"production_identity", "output_receipt"}:
        raise ValueError("invalid document bundle production receipt")
    identity = _identity(entry["production_identity"], "production_identity")
    output_store, output_observation = dependencies.claim(entry["output_receipt"])
    result = restore_sec_document_financial_production(
        output_store=output_store,
        output_observation=output_observation,
        resolve_observation=dependencies.resolve,
    )
    if result.production_identity != identity:
        raise ValueError("document bundle production identity mismatch")
    return result


def write_sec_document_financial_bundle(
    *,
    store: SnapshotStore,
    batch_identity: str,
    productions: Iterable[SecDocumentFinancialProduction],
    quality_records: Iterable[SecObservedFinancialQualityRecord],
    consumer_commits: Iterable[SecObservedFinancialConsumerCommit],
    captured_at: datetime,
    resolve_observation: ObservationResolver,
    max_bundle_bytes: int = _MAX_BYTES,
    max_dependency_bytes: int = _MAX_DEPENDENCIES,
) -> SnapshotRef:
    """Validate all retained dependencies before a single immutable FROZEN write."""
    maximum = _limit(max_bundle_bytes, "max_bundle_bytes", _MAX_BYTES)
    dependency_maximum = _limit(max_dependency_bytes, "max_dependency_bytes", _MAX_DEPENDENCIES)
    raw = _bounded(productions, "production", 10)
    qualities = _bounded(quality_records, "quality record", 10_000)
    commits = _bounded(consumer_commits, "commit", 10_000)
    if any(type(item) is not SecDocumentFinancialProduction for item in raw):
        raise TypeError("productions must contain SecDocumentFinancialProduction")
    items = cast(tuple[SecDocumentFinancialProduction, ...], raw)
    _admit((batch_identity, captured_at, items, qualities, commits), _MAX_DEPENDENCIES)
    if sum(len(item.vintage.rows) for item in items) > 100_000:
        raise ValueError("document bundle row limit exceeded")
    unique = {}
    for item in items:
        checked = copy(item)
        checked.__post_init__()
        if checked.production_identity != item.production_identity:
            raise ValueError("document bundle production identity mismatch")
        prior = unique.get(item.production_identity)
        if prior is not None and prior != item:
            raise ValueError("conflicting document bundle production")
        unique[item.production_identity] = item
    admitted = _bundle(
        batch_identity,
        tuple(unique.values()),
        _deduplicate_quality(qualities),
        _deduplicate_commits(commits),
        captured_at,
    )
    _encode(admitted, maximum)
    dependencies = Dependencies(resolve_observation, dependency_maximum)
    restored = tuple(_restore(_entry(item), dependencies) for item in admitted.productions)
    bundle = _bundle(
        batch_identity, restored, admitted.quality_records, admitted.consumer_commits, captured_at
    )
    return store.write(
        _spec(batch_identity),
        _encode(bundle, maximum),
        bundle.captured_at,
        _SCHEMA,
        SnapshotMode.FROZEN,
    )


def load_sec_document_financial_bundle(
    *,
    store: SnapshotStore,
    bundle_ref: SnapshotRef,
    resolve_observation: ObservationResolver,
    max_bundle_bytes: int = _MAX_BYTES,
    max_dependency_bytes: int = _MAX_DEPENDENCIES,
) -> SecDocumentFinancialBundle:
    """Strictly replay the entire source closure and parser without writing."""
    maximum = _limit(max_bundle_bytes, "max_bundle_bytes", _MAX_BYTES)
    dependency_maximum = _limit(max_dependency_bytes, "max_dependency_bytes", _MAX_DEPENDENCIES)
    if (
        bundle_ref.provider,
        bundle_ref.endpoint,
        bundle_ref.mode,
        bundle_ref.serialization_identifier,
    ) != ("sec", _ENDPOINT, SnapshotMode.FROZEN, _SCHEMA):
        raise ValueError("not a document financial bundle")
    replay = store.replay(bundle_ref, max_payload_bytes=maximum)
    data = _strict_json(replay.payload)
    if set(data) != _TOP or data["schema"] != _SCHEMA:
        raise ValueError("invalid document financial bundle envelope")
    batch = data["batch_identity"]
    captured = _time(data["captured_at"], "captured_at")
    if type(batch) is not str or not batch or len(batch) > 1024:
        raise ValueError("invalid document bundle batch identity")
    expected = _spec(batch)
    if (
        bundle_ref.request_identity != expected.request_identity
        or replay.manifest["canonical_request"] != expected.canonical_payload
        or _time(replay.manifest["retrieved_at"], "captured_at") != captured
    ):
        raise ValueError("document bundle request or capture mismatch")
    for name, limit in (
        ("productions", 10),
        ("quality_records", 10_000),
        ("consumer_commits", 10_000),
    ):
        if type(data[name]) is not list or len(data[name]) > limit:
            raise ValueError("document bundle record limit exceeded")
    qualities = tuple(quality(item) for item in data["quality_records"])
    commits = tuple(commit(item) for item in data["consumer_commits"])
    dependencies = Dependencies(resolve_observation, dependency_maximum)
    restored = []
    seen = set()
    rows = 0
    for entry in data["productions"]:
        if not isinstance(entry, dict):
            raise ValueError("invalid document bundle production")  # noqa: TRY004 -- persisted schema
        identity = _identity(entry.get("production_identity"), "production_identity")
        if identity in seen:
            raise ValueError("duplicate document bundle production")
        seen.add(identity)
        result = _restore(entry, dependencies)
        rows += len(result.vintage.rows)
        if rows > 100_000:
            raise ValueError("document bundle row limit exceeded")
        restored.append(result)
    bundle = _bundle(batch, tuple(restored), qualities, commits, captured)
    if _encode(bundle, maximum) != replay.payload:
        raise ValueError("noncanonical document financial bundle")
    return bundle

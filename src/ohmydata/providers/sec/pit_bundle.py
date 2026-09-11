"""Immutable, replay-validated receipts for SEC PIT production inputs."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from typing import Any

from ...core import SnapshotMode, SnapshotObservationRef, SnapshotRef, SnapshotStore
from ...core.specs import RequestSpec
from ._pit_projection import _decode_projection, _projection_datetime
from .pit import (
    _SERIALIZATION,
    SecConsumerCommit,
    SecNormalizedFinancialFactVersion,
    SecQualityRecord,
    SecQualityStatus,
    _utc,
    _version_from_replayed_projection,
)

_SCHEMA = "sec-pit-bundle-v1"
_SERIALIZATION_BUNDLE = "sec-pit-bundle-v1"
_TOP = frozenset({"schema", "batch_identity", "versions", "quality_records", "consumer_commits"})
_VERSION_FIELDS = frozenset(
    {
        "observation_id",
        "accession_number",
        "source_artifact_identity",
        "source_available_at",
        "vintage_identity",
        "row_ordinal",
        "schema_version",
        "adapter_version",
        "normalization_version",
        "configuration_identity",
        "recorded_at",
        "content_identity",
        "normalized_version_id",
    }
)
_DEFAULT_MAX_RECORDS = 10_000
_DEFAULT_MAX_BYTES = 8 * 1024 * 1024


def _limits(max_records: int, max_bytes: int) -> None:
    if any(type(value) is not int or value <= 0 for value in (max_records, max_bytes)):
        raise ValueError("invalid SEC PIT bundle limits")
    if max_records > _DEFAULT_MAX_RECORDS or max_bytes > _DEFAULT_MAX_BYTES:
        raise ValueError("SEC PIT bundle limits may only be stricter")


def _bounded(values: Iterable[Any], maximum: int) -> tuple[Any, ...]:
    items = tuple(islice(values, maximum + 1))
    if len(items) > maximum:
        raise ValueError("SEC PIT bundle record limit exceeded")
    return items


def _decode_json(payload: bytes) -> dict[str, Any]:
    def reject_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in items:
            if key in output:
                raise ValueError("duplicate JSON key in SEC PIT bundle")
            output[key] = value
        return output

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid SEC PIT bundle JSON") from exc
    if not isinstance(value, dict) or frozenset(value) != _TOP or value["schema"] != _SCHEMA:
        raise ValueError("invalid SEC PIT bundle schema")
    if not isinstance(value["batch_identity"], str) or not value["batch_identity"]:
        raise ValueError("invalid SEC PIT bundle batch identity")
    if any(
        not isinstance(value[key], list)
        for key in ("versions", "quality_records", "consumer_commits")
    ):
        raise ValueError("invalid SEC PIT bundle receipts")
    return value


def _stamp(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _time(value: object, name: str) -> datetime:
    return _projection_datetime(value, name)


def _receipt(version: SecNormalizedFinancialFactVersion) -> dict[str, Any]:
    return {
        "observation_id": version.observation.observation_identity,
        "accession_number": version.accession_number,
        "source_artifact_identity": version.source_artifact_identity,
        "source_available_at": _stamp(version.source_available_at),
        "vintage_identity": version.vintage_identity,
        "row_ordinal": version.row_ordinal,
        "schema_version": version.schema_version,
        "adapter_version": version.adapter_version,
        "normalization_version": version.normalization_version,
        "configuration_identity": version.configuration_identity,
        "recorded_at": _stamp(version.recorded_at),
        "content_identity": version.content_identity,
        "normalized_version_id": version.normalized_version_id,
    }


def _quality(record: SecQualityRecord) -> dict[str, Any]:
    return {
        "normalized_version_id": record.normalized_version_id,
        "quality_policy_version": record.quality_policy_version,
        "status": record.status.value,
        "recorded_at": _stamp(record.recorded_at),
        "supersedes_quality_record_id": record.supersedes_quality_record_id,
        "quality_record_id": record.quality_record_id,
    }


def _commit(commit: SecConsumerCommit) -> dict[str, Any]:
    return {
        "normalized_version_id": commit.normalized_version_id,
        "quality_record_id": commit.quality_record_id,
        "consumer_dataset_identity": commit.consumer_dataset_identity,
        "committed_at": _stamp(commit.committed_at),
        "commit_id": commit.commit_id,
    }


def _canonical_bundle(
    batch_identity: str,
    versions: Iterable[SecNormalizedFinancialFactVersion],
    qualities: Iterable[SecQualityRecord],
    commits: Iterable[SecConsumerCommit],
    *,
    max_bytes: int | None = None,
) -> bytes:
    entries = list(
        {_receipt(value)["normalized_version_id"]: _receipt(value) for value in versions}.values()
    )
    q_entries = list(
        {_quality(value)["quality_record_id"]: _quality(value) for value in qualities}.values()
    )
    c_entries = list({_commit(value)["commit_id"]: _commit(value) for value in commits}.values())
    data = {
        "schema": _SCHEMA,
        "batch_identity": batch_identity,
        "versions": sorted(entries, key=lambda x: x["normalized_version_id"]),
        "quality_records": sorted(q_entries, key=lambda x: x["quality_record_id"]),
        "consumer_commits": sorted(c_entries, key=lambda x: x["commit_id"]),
    }
    if max_bytes is not None:

        def check_strings(value: Any) -> None:
            if isinstance(value, str) and (
                len(value) > max_bytes or len(value.encode()) > max_bytes
            ):
                raise ValueError("SEC PIT bundle byte limit exceeded")
            if isinstance(value, Mapping):
                for child in value.values():
                    check_strings(child)
            elif isinstance(value, list):
                for child in value:
                    check_strings(child)

        check_strings(data)
    chunks: list[bytes] = []
    size = 0
    for chunk in json.JSONEncoder(
        sort_keys=True, separators=(",", ":"), allow_nan=False
    ).iterencode(data):
        encoded = chunk.encode()
        size += len(encoded)
        if max_bytes is not None and size > max_bytes:
            raise ValueError("SEC PIT bundle byte limit exceeded")
        chunks.append(encoded)
    return b"".join(chunks)


def _validate_graph(
    versions: Mapping[str, SecNormalizedFinancialFactVersion],
    qualities: Iterable[SecQualityRecord],
    commits: Iterable[SecConsumerCommit],
) -> tuple[tuple[SecQualityRecord, ...], tuple[SecConsumerCommit, ...]]:
    records = tuple(qualities)
    ids = {item.quality_record_id: item for item in records}
    for record in records:
        production = versions.get(record.normalized_version_id)
        if production is None or record.recorded_at < production.recorded_at:
            raise ValueError("quality record is not causally bound to production")
        if record.supersedes_quality_record_id is not None:
            parent = ids.get(record.supersedes_quality_record_id)
            if (
                parent is None
                or parent.normalized_version_id != record.normalized_version_id
                or parent.quality_policy_version != record.quality_policy_version
                or parent.recorded_at >= record.recorded_at
            ):
                raise ValueError("invalid quality supersedes graph")
        elif record.status is SecQualityStatus.REVOKED:
            raise ValueError("revoked quality record must supersede a prior record")
    by_time: set[tuple[str, str, datetime]] = set()
    for record in records:
        key = (record.normalized_version_id, record.quality_policy_version, record.recorded_at)
        if key in by_time:
            raise ValueError("conflicting same-time quality records")
        by_time.add(key)
    loaded_commits = tuple(commits)
    for commit in loaded_commits:
        production = versions.get(commit.normalized_version_id)
        quality = ids.get(commit.quality_record_id)
        if (
            production is None
            or quality is None
            or quality.normalized_version_id != commit.normalized_version_id
        ):
            raise ValueError("consumer commit has missing dependency")
        if (
            commit.committed_at < production.recorded_at
            or commit.committed_at < production.observation.snapshot_fetched_at
            or commit.committed_at < quality.recorded_at
        ):
            raise ValueError("consumer commit precedes evidence")
        current = [
            item
            for item in records
            if item.normalized_version_id == commit.normalized_version_id
            and item.quality_policy_version == quality.quality_policy_version
            and item.recorded_at <= commit.committed_at
        ]
        if (
            not current
            or max(current, key=lambda item: item.recorded_at).quality_record_id
            != quality.quality_record_id
            or quality.status is not SecQualityStatus.PASS
        ):
            raise ValueError("consumer commit does not bind current PASS quality")
    return records, loaded_commits


@dataclass(frozen=True)
class SecPitBundle:
    batch_identity: str
    versions: tuple[SecNormalizedFinancialFactVersion, ...]
    quality_records: tuple[SecQualityRecord, ...]
    consumer_commits: tuple[SecConsumerCommit, ...]
    captured_at: datetime


def write_sec_pit_bundle(
    *,
    store: SnapshotStore,
    batch_identity: str,
    versions: Iterable[SecNormalizedFinancialFactVersion],
    quality_records: Iterable[SecQualityRecord],
    consumer_commits: Iterable[SecConsumerCommit] = (),
    captured_at: datetime,
    max_records: int = 10_000,
    max_bytes: int = 8 * 1024 * 1024,
) -> SnapshotRef:
    """Atomically persist a complete immutable SEC PIT receipt closure."""
    if not isinstance(batch_identity, str) or not batch_identity:
        raise ValueError("invalid SEC PIT bundle limit or identity")
    _limits(max_records, max_bytes)
    captured = _utc(captured_at, "captured_at")
    version_items = _bounded(versions, max_records)
    quality_items = _bounded(quality_records, max_records - len(version_items))
    commit_items = _bounded(consumer_commits, max_records - len(version_items) - len(quality_items))
    if any(type(item) is not SecNormalizedFinancialFactVersion for item in version_items):
        raise TypeError("versions must contain SecNormalizedFinancialFactVersion values")
    if any(type(item) is not SecQualityRecord for item in quality_items) or any(
        type(item) is not SecConsumerCommit for item in commit_items
    ):
        raise TypeError("quality records and commits must be SEC receipt values")
    for item in (*version_items, *quality_items, *commit_items):
        item.__post_init__()
    version_items = tuple({item.normalized_version_id: item for item in version_items}.values())
    quality_items = tuple({item.quality_record_id: item for item in quality_items}.values())
    commit_items = tuple({item.commit_id: item for item in commit_items}.values())
    version_map = {item.normalized_version_id: item for item in version_items}
    records, commits = _validate_graph(version_map, quality_items, commit_items)
    all_times = (
        [item.observation.snapshot_fetched_at for item in version_items]
        + [item.recorded_at for item in version_items]
        + [item.recorded_at for item in records]
        + [item.committed_at for item in commits]
    )
    if any(captured < value for value in all_times):
        raise ValueError("captured_at precedes bundle evidence")
    payload = _canonical_bundle(
        batch_identity, version_items, records, commits, max_bytes=max_bytes
    )
    if len(payload) > max_bytes:
        raise ValueError("SEC PIT bundle byte limit exceeded")
    return store.write(
        RequestSpec("sec", "pit-bundle", {"batch_identity": batch_identity}),
        payload,
        captured,
        _SERIALIZATION_BUNDLE,
        SnapshotMode.FROZEN,
    )


def load_sec_pit_bundle(
    *,
    store: SnapshotStore,
    bundle_ref: SnapshotRef,
    source_store: SnapshotStore,
    resolve_observation: Callable[[str], SnapshotObservationRef],
    max_records: int = 10_000,
    max_bytes: int = 8 * 1024 * 1024,
) -> SecPitBundle:
    """Load only after rebuilding every version from its replayed source observation."""
    _limits(max_records, max_bytes)
    replay = store.replay(bundle_ref, max_payload_bytes=max_bytes)
    if (
        bundle_ref.provider != "sec"
        or bundle_ref.endpoint != "pit-bundle"
        or bundle_ref.mode is not SnapshotMode.FROZEN
        or bundle_ref.serialization_identifier != _SERIALIZATION_BUNDLE
    ):
        raise ValueError("not a SEC PIT bundle snapshot")
    if len(replay.payload) > max_bytes:
        raise ValueError("SEC PIT bundle byte limit exceeded")
    data = _decode_json(replay.payload)
    expected = RequestSpec("sec", "pit-bundle", {"batch_identity": data["batch_identity"]})
    if bundle_ref.request_identity != expected.request_identity:
        raise ValueError("bundle batch identity does not match snapshot request")
    receipts = data["versions"]
    if len(receipts) + len(data["quality_records"]) + len(data["consumer_commits"]) > max_records:
        raise ValueError("SEC PIT bundle record limit exceeded")
    observations: dict[str, tuple[SnapshotObservationRef, dict[str, Any]]] = {}
    rebuilt: list[SecNormalizedFinancialFactVersion] = []
    for item in receipts:
        if not isinstance(item, dict) or set(item) != _VERSION_FIELDS:
            raise ValueError("invalid normalized version receipt")
        observation_id = item["observation_id"]
        if not isinstance(observation_id, str):
            raise TypeError("invalid observation receipt")
        if observation_id not in observations:
            observation = resolve_observation(observation_id)
            if (
                not isinstance(observation, SnapshotObservationRef)
                or observation.observation_identity != observation_id
            ):
                raise ValueError("observation resolver identity mismatch")
            if (
                observation.provider != "sec"
                or observation.serialization_identifier != _SERIALIZATION
            ):
                raise ValueError("invalid SEC source observation")
            observations[observation_id] = (
                observation,
                _decode_projection(
                    source_store.replay_observation(
                        observation, max_payload_bytes=max_bytes
                    ).payload
                ),
            )
        observation, projection = observations[observation_id]
        source_at = _time(item["source_available_at"], "source_available_at")
        if (
            projection["source_artifact_identity"] != item["source_artifact_identity"]
            or _projection_datetime(projection["source_available_at"], "source_available_at")
            != source_at
        ):
            raise ValueError("source receipt does not match replayed projection")
        version = _version_from_replayed_projection(
            observation=observation,
            projection=projection,
            source_available_at=source_at,
            accession_number=item["accession_number"],
            vintage_identity=item["vintage_identity"],
            row_ordinal=item["row_ordinal"],
            expected_row=None,
            schema_version=item["schema_version"],
            adapter_version=item["adapter_version"],
            normalization_version=item["normalization_version"],
            configuration_identity=item["configuration_identity"],
            recorded_at=_time(item["recorded_at"], "recorded_at"),
        )
        if (
            version.content_identity != item["content_identity"]
            or version.normalized_version_id != item["normalized_version_id"]
        ):
            raise ValueError("normalized version receipt identity mismatch")
        rebuilt.append(version)
    if len({item.normalized_version_id for item in rebuilt}) != len(rebuilt):
        raise ValueError("duplicate normalized version receipt")
    qualities = tuple(_load_quality(item) for item in data["quality_records"])
    commits = tuple(_load_commit(item) for item in data["consumer_commits"])
    _validate_graph({item.normalized_version_id: item for item in rebuilt}, qualities, commits)
    captured = _time(replay.manifest["retrieved_at"], "captured_at")
    if any(
        captured < value
        for value in [
            *(item.observation.snapshot_fetched_at for item in rebuilt),
            *(item.recorded_at for item in rebuilt),
            *(item.recorded_at for item in qualities),
            *(item.committed_at for item in commits),
        ]
    ):
        raise ValueError("bundle capture precedes evidence")
    return SecPitBundle(data["batch_identity"], tuple(rebuilt), qualities, commits, captured)


def _load_quality(value: object) -> SecQualityRecord:
    if not isinstance(value, dict) or set(value) != {
        "normalized_version_id",
        "quality_policy_version",
        "status",
        "recorded_at",
        "supersedes_quality_record_id",
        "quality_record_id",
    }:
        raise ValueError("invalid quality receipt")
    try:
        item = SecQualityRecord(
            value["normalized_version_id"],
            value["quality_policy_version"],
            SecQualityStatus(value["status"]),
            _time(value["recorded_at"], "quality timestamp"),
            value["supersedes_quality_record_id"],
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid quality receipt") from exc
    if item.quality_record_id != value["quality_record_id"]:
        raise ValueError("quality receipt identity mismatch")
    return item


def _load_commit(value: object) -> SecConsumerCommit:
    if not isinstance(value, dict) or set(value) != {
        "normalized_version_id",
        "quality_record_id",
        "consumer_dataset_identity",
        "committed_at",
        "commit_id",
    }:
        raise ValueError("invalid consumer commit receipt")
    try:
        item = SecConsumerCommit(
            value["normalized_version_id"],
            value["quality_record_id"],
            value["consumer_dataset_identity"],
            _time(value["committed_at"], "commit timestamp"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid consumer commit receipt") from exc
    if item.commit_id != value["commit_id"]:
        raise ValueError("consumer commit receipt identity mismatch")
    return item


__all__ = ["SecPitBundle", "load_sec_pit_bundle", "write_sec_pit_bundle"]

"""Strict v2 receipt codec and closure checks for SEC PIT bundle findings."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

from ...core import SnapshotObservationRef, SnapshotStore
from .quality_findings import (
    SecMissingDataReason,
    SecQualityEvidenceRef,
    SecQualityFinding,
    SecQualityFindingStatus,
    SecQualityIssueClass,
)

_FINDING_FIELDS = frozenset(
    {
        "normalized_version_id",
        "affected_fields",
        "finding_key",
        "rule_id",
        "rule_version",
        "adapter_version",
        "issue_class",
        "missing_reason",
        "reason",
        "evidence",
        "status",
        "detected_at",
        "recorded_at",
        "adjudicated_at",
        "supersedes_finding_id",
        "finding_id",
    }
)
_EVIDENCE_FIELDS = frozenset({"observation_identity", "fact_version"})


def receipt(finding: SecQualityFinding, stamp: Callable[[datetime], str]) -> dict[str, Any]:
    return {
        "normalized_version_id": finding.normalized_version_id,
        "affected_fields": list(finding.affected_fields),
        "finding_key": finding.finding_key,
        "rule_id": finding.rule_id,
        "rule_version": finding.rule_version,
        "adapter_version": finding.adapter_version,
        "issue_class": finding.issue_class.value,
        "missing_reason": None if finding.missing_reason is None else finding.missing_reason.value,
        "reason": finding.reason,
        "evidence": [
            {"observation_identity": item.observation_identity, "fact_version": item.fact_version}
            for item in finding.evidence
        ],
        "status": finding.status.value,
        "detected_at": stamp(finding.detected_at),
        "recorded_at": stamp(finding.recorded_at),
        "adjudicated_at": None if finding.adjudicated_at is None else stamp(finding.adjudicated_at),
        "supersedes_finding_id": finding.supersedes_finding_id,
        "finding_id": finding.finding_id,
    }


def load(value: object, time: Callable[[object, str], datetime]) -> SecQualityFinding:
    if not isinstance(value, dict) or frozenset(value) != _FINDING_FIELDS:
        raise ValueError("invalid quality finding receipt")
    evidence = value.get("evidence")
    if not isinstance(evidence, list):
        raise TypeError("invalid quality finding evidence")
    fields = value.get("affected_fields")
    if not isinstance(fields, list):
        raise TypeError("invalid quality finding fields")
    try:
        decoded_evidence = []
        for entry in evidence:
            if not isinstance(entry, dict) or frozenset(entry) != _EVIDENCE_FIELDS:
                raise TypeError("invalid quality finding evidence")
            decoded_evidence.append(
                SecQualityEvidenceRef(entry["observation_identity"], entry["fact_version"])
            )
        item = SecQualityFinding(
            value["normalized_version_id"],
            tuple(fields),
            value["finding_key"],
            value["rule_id"],
            value["rule_version"],
            value["adapter_version"],
            SecQualityIssueClass(value["issue_class"]),
            None
            if value["missing_reason"] is None
            else SecMissingDataReason(value["missing_reason"]),
            value["reason"],
            tuple(decoded_evidence),
            SecQualityFindingStatus(value["status"]),
            time(value["detected_at"], "finding detected_at"),
            time(value["recorded_at"], "finding recorded_at"),
            None
            if value["adjudicated_at"] is None
            else time(value["adjudicated_at"], "finding adjudicated_at"),
            value["supersedes_finding_id"],
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid quality finding receipt") from exc
    if item.finding_id != value["finding_id"]:
        raise ValueError("quality finding receipt identity mismatch")
    return item


def validate_graph(
    findings: Iterable[SecQualityFinding],
    versions: Mapping[str, Any],
) -> tuple[SecQualityFinding, ...]:
    """Validate complete histories once, retaining every exact duplicate only once."""
    by_id: dict[str, SecQualityFinding] = {}
    for finding in findings:
        previous = by_id.get(finding.finding_id)
        if previous is not None and previous != finding:
            raise ValueError("quality finding identity collision")
        by_id[finding.finding_id] = finding
    grouped: dict[tuple[str, str, str, str], list[SecQualityFinding]] = defaultdict(list)
    for finding in by_id.values():
        version = versions.get(finding.normalized_version_id)
        if version is None:
            raise ValueError("quality finding is not bound to included production")
        if finding.adapter_version != version.adapter_version:
            raise ValueError("quality finding adapter does not match production")
        if finding.recorded_at < version.recorded_at:
            raise ValueError("quality finding precedes production")
        grouped[
            (
                finding.normalized_version_id,
                finding.rule_version,
                finding.rule_id,
                finding.finding_key,
            )
        ].append(finding)
    for chain in grouped.values():
        roots = [item for item in chain if item.supersedes_finding_id is None]
        if len(roots) != 1:
            raise ValueError("quality finding chain requires one root")
        children: dict[str, list[SecQualityFinding]] = defaultdict(list)
        chain_ids = {item.finding_id: item for item in chain}
        for item in chain:
            if item.supersedes_finding_id is None:
                continue
            parent = chain_ids.get(item.supersedes_finding_id)
            if parent is None:
                raise ValueError("quality finding supersedes target is missing")
            if item.scope != parent.scope:
                raise ValueError("quality finding revision changed its scope")
            if parent.recorded_at >= item.recorded_at:
                raise ValueError("quality finding revisions must have increasing recorded_at")
            children[parent.finding_id].append(item)
        if any(len(items) > 1 for items in children.values()):
            raise ValueError("quality finding revision fork")
    return tuple(sorted(by_id.values(), key=lambda item: item.finding_id))


def bounded_findings(values: Iterable[Any], remaining: int) -> tuple[SecQualityFinding, ...]:
    items: list[SecQualityFinding] = []
    used = 0
    for item in values:
        if type(item) is not SecQualityFinding:
            raise TypeError("quality_findings must contain SecQualityFinding values")
        expected_id = item.finding_id
        item.__post_init__()
        if item.finding_id != expected_id:
            raise ValueError("quality finding identity mismatch")
        cost = 1 + len(item.evidence)
        if used + cost > remaining:
            raise ValueError("SEC PIT bundle record limit exceeded")
        items.append(item)
        used += cost
    return tuple(items)


def replay_source(
    observation_id: str,
    *,
    source_store: SnapshotStore,
    resolve_observation: Callable[[str], SnapshotObservationRef],
    max_bytes: int,
    cache: dict[str, tuple[SnapshotObservationRef, bytes]],
) -> tuple[SnapshotObservationRef, dict[str, Any]]:
    from ._pit_projection import _decode_projection
    from .pit import _SERIALIZATION

    cached = cache.get(observation_id)
    if cached is None:
        observation = resolve_observation(observation_id)
        if (
            not isinstance(observation, SnapshotObservationRef)
            or observation.observation_identity != observation_id
        ):
            raise ValueError("observation resolver identity mismatch")
        replay = source_store.replay_observation(observation, max_payload_bytes=max_bytes)
        cached = (observation, replay.payload)
        cache[observation_id] = cached
    observation, payload = cached
    if observation.provider != "sec" or observation.serialization_identifier != _SERIALIZATION:
        raise ValueError("invalid SEC source observation")
    return observation, _decode_projection(payload)


def verify_versions(
    versions: Iterable[Any],
    *,
    source_store: SnapshotStore,
    resolve_observation: Callable[[str], SnapshotObservationRef],
    max_bytes: int,
    cache: dict[str, tuple[SnapshotObservationRef, bytes]],
) -> None:
    from .pit import _version_from_replayed_projection

    for version in versions:
        observation, projection = replay_source(
            version.observation.observation_identity,
            source_store=source_store,
            resolve_observation=resolve_observation,
            max_bytes=max_bytes,
            cache=cache,
        )
        replayed = _version_from_replayed_projection(
            observation=observation,
            projection=projection,
            source_available_at=version.source_available_at,
            accession_number=version.accession_number,
            vintage_identity=version.vintage_identity,
            row_ordinal=version.row_ordinal,
            expected_row=None,
            schema_version=version.schema_version,
            adapter_version=version.adapter_version,
            normalization_version=version.normalization_version,
            configuration_identity=version.configuration_identity,
            recorded_at=version.recorded_at,
        )
        if replayed.normalized_version_id != version.normalized_version_id:
            raise ValueError("normalized version does not match replayed source")


def load_quality(value: object, time: Callable[[object, str], datetime]) -> Any:
    from .pit import SecQualityRecord, SecQualityStatus

    keys = {
        "normalized_version_id",
        "quality_policy_version",
        "status",
        "recorded_at",
        "supersedes_quality_record_id",
        "quality_record_id",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid quality receipt")
    try:
        item = SecQualityRecord(
            value["normalized_version_id"],
            value["quality_policy_version"],
            SecQualityStatus(value["status"]),
            time(value["recorded_at"], "quality timestamp"),
            value["supersedes_quality_record_id"],
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid quality receipt") from exc
    if item.quality_record_id != value["quality_record_id"]:
        raise ValueError("quality receipt identity mismatch")
    return item


def load_commit(value: object, time: Callable[[object, str], datetime]) -> Any:
    from .pit import SecConsumerCommit

    keys = {
        "normalized_version_id",
        "quality_record_id",
        "consumer_dataset_identity",
        "committed_at",
        "commit_id",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid consumer commit receipt")
    try:
        item = SecConsumerCommit(
            value["normalized_version_id"],
            value["quality_record_id"],
            value["consumer_dataset_identity"],
            time(value["committed_at"], "commit timestamp"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid consumer commit receipt") from exc
    if item.commit_id != value["commit_id"]:
        raise ValueError("consumer commit receipt identity mismatch")
    return item


def verify_evidence(
    findings: Iterable[SecQualityFinding],
    *,
    source_store: SnapshotStore,
    resolve_observation: Callable[[str], SnapshotObservationRef],
    max_bytes: int,
    cache: dict[str, tuple[SnapshotObservationRef, bytes]] | None = None,
) -> dict[str, tuple[SnapshotObservationRef, bytes]]:
    """Replay each evidence observation once and bind its public identity pair."""
    observations = {} if cache is None else cache
    for finding in findings:
        for evidence in finding.evidence:
            cached = observations.get(evidence.observation_identity)
            if cached is None:
                observation = resolve_observation(evidence.observation_identity)
                if (
                    not isinstance(observation, SnapshotObservationRef)
                    or observation.observation_identity != evidence.observation_identity
                ):
                    raise ValueError("evidence observation resolver identity mismatch")
                replay = source_store.replay_observation(observation, max_payload_bytes=max_bytes)
                observations[evidence.observation_identity] = (observation, replay.payload)
            else:
                observation = cached[0]
            if observation.fact_version != evidence.fact_version:
                raise ValueError("evidence fact version does not match observation")
            if observation.snapshot_fetched_at > finding.recorded_at:
                raise ValueError("quality finding precedes evidence observation")
    return observations

"""Private PIT selection helpers for SEC typed financial versions."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .pit import (
    SecConsumerCommit,
    SecNormalizedFinancialFactVersion,
    SecPitMode,
    SecPitPolicy,
    SecPitResult,
    SecQualityRecord,
    SecQualityStatus,
    _utc,
)

_SGML_ACCEPTANCE_PROXY_ADAPTER_VERSIONS = frozenset(
    {"sec-sgml-financial-adapter-v1", "sec-sgml-financial-adapter-v2"}
)


def _quality_as_of(
    records: Iterable[SecQualityRecord],
    version: SecNormalizedFinancialFactVersion,
    policy: SecPitPolicy,
) -> SecQualityRecord | None:
    candidates = [
        record
        for record in records
        if record.normalized_version_id == version.normalized_version_id
        and record.quality_policy_version == policy.quality_policy_version
        and record.recorded_at >= version.recorded_at
        and record.recorded_at <= policy.quality_cutoff
    ]
    if not candidates:
        return None
    newest = max(record.recorded_at for record in candidates)
    winners = {
        record.quality_record_id: record for record in candidates if record.recorded_at == newest
    }
    if len(winners) != 1:
        raise ValueError("conflicting quality records at the same time")
    return next(iter(winners.values()))


def _validate_quality_chain(records: tuple[SecQualityRecord, ...]) -> None:
    by_id: dict[str, SecQualityRecord] = {}
    for record in records:
        prior = by_id.get(record.quality_record_id)
        if prior is not None and prior != record:
            raise ValueError("quality record identity collision")
        by_id[record.quality_record_id] = record
    for record in by_id.values():
        target_id = record.supersedes_quality_record_id
        if record.status is SecQualityStatus.REVOKED and target_id is None:
            raise ValueError("revoked quality record must supersede a prior record")
        if target_id is None:
            continue
        target = by_id.get(target_id)
        if target is None:
            raise ValueError("quality supersedes target is missing")
        if (
            target.normalized_version_id != record.normalized_version_id
            or target.quality_policy_version != record.quality_policy_version
            or target.recorded_at > record.recorded_at
        ):
            raise ValueError("invalid quality supersedes chain")


def select_sec_financial_versions(
    versions: Iterable[SecNormalizedFinancialFactVersion],
    *,
    mode: SecPitMode,
    knowledge_cutoff: datetime,
    policy: SecPitPolicy,
    quality_records: Iterable[SecQualityRecord],
    consumer_commits: Iterable[SecConsumerCommit] = (),
) -> tuple[SecPitResult, ...]:
    """Return all eligible versions for an explicit mode and version policy."""
    if type(mode) is not SecPitMode:
        raise TypeError("mode must be a SecPitMode")
    if type(policy) is not SecPitPolicy:
        raise TypeError("policy must be a SecPitPolicy")
    policy.__post_init__()
    cutoff = _utc(knowledge_cutoff, "knowledge_cutoff")
    if mode is SecPitMode.SYSTEM_REPLAY and policy.quality_cutoff != cutoff:
        raise ValueError("system replay quality_cutoff must equal knowledge_cutoff")
    supplied_records = tuple(quality_records)
    supplied_commits = tuple(consumer_commits)
    if any(type(record) is not SecQualityRecord for record in supplied_records):
        raise TypeError("quality_records must contain SecQualityRecord values")
    if any(type(commit) is not SecConsumerCommit for commit in supplied_commits):
        raise TypeError("consumer_commits must contain SecConsumerCommit values")
    for record in supplied_records:
        record.__post_init__()
    for commit in supplied_commits:
        commit.__post_init__()
    records = tuple(
        record for record in supplied_records if record.recorded_at <= policy.quality_cutoff
    )
    commits = supplied_commits
    _validate_quality_chain(records)
    results: list[SecPitResult] = []
    seen: dict[str, SecNormalizedFinancialFactVersion] = {}
    for version in versions:
        if type(version) is not SecNormalizedFinancialFactVersion:
            raise TypeError("versions must contain SecNormalizedFinancialFactVersion values")
        version.__post_init__()
        if (
            mode is SecPitMode.MARKET_KNOWN
            and version.adapter_version in _SGML_ACCEPTANCE_PROXY_ADAPTER_VERSIONS
        ):
            raise ValueError("MARKET_KNOWN rejects automatic SGML acceptance-proxy versions")
        previous = seen.get(version.normalized_version_id)
        if previous is not None and previous != version:
            raise ValueError("normalized version identity collision")
        if previous is not None:
            continue
        seen[version.normalized_version_id] = version
        if (
            version.schema_version != policy.schema_version
            or version.adapter_version != policy.adapter_version
            or version.normalization_version != policy.normalization_version
            or version.configuration_identity != policy.configuration_identity
            or version.source_available_at > cutoff
            or (mode is SecPitMode.MARKET_KNOWN and version.recorded_at > policy.quality_cutoff)
        ):
            continue
        quality = _quality_as_of(records, version, policy)
        if quality is None or quality.status is not SecQualityStatus.PASS:
            continue
        if mode is SecPitMode.MARKET_KNOWN:
            results.append(SecPitResult(version, quality, None))
            continue
        if (
            version.observation.snapshot_fetched_at > cutoff
            or version.recorded_at > cutoff
            or quality.recorded_at > cutoff
        ):
            continue
        matching = [
            commit
            for commit in commits
            if commit.normalized_version_id == version.normalized_version_id
            and commit.quality_record_id == quality.quality_record_id
            and commit.committed_at <= cutoff
            and commit.committed_at >= version.recorded_at
            and commit.committed_at >= quality.recorded_at
            and commit.committed_at >= version.observation.snapshot_fetched_at
        ]
        if not matching:
            continue
        newest_commit_time = max(commit.committed_at for commit in matching)
        commit_winners = {
            commit.commit_id: commit
            for commit in matching
            if commit.committed_at == newest_commit_time
        }
        if len(commit_winners) != 1:
            raise ValueError("conflicting consumer commits at the same time")
        results.append(SecPitResult(version, quality, next(iter(commit_winners.values()))))
    return tuple(
        sorted(
            results,
            key=lambda item: (
                item.version.source_available_at,
                item.version.accession_number,
                item.version.row_ordinal,
                item.version.normalized_version_id,
            ),
        )
    )

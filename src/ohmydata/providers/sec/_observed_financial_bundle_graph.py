"""Complete lifecycle graph validation for observed-financial bundles."""

from __future__ import annotations

from datetime import datetime

from .observed_financial_replay import (
    SecObservedFinancialConsumerCommit,
    SecObservedFinancialQualityRecord,
    _ReplayProduction,
)
from .pit import SecQualityStatus


def validate_lifecycle(
    productions: tuple[_ReplayProduction, ...],
    qualities: tuple[SecObservedFinancialQualityRecord, ...],
    commits: tuple[SecObservedFinancialConsumerCommit, ...],
) -> None:
    """Require every supplied policy and dataset history to be causally complete."""
    by_production = {item.production_identity: item for item in productions}
    ids = {item.quality_record_id: item for item in qualities}
    if len(ids) != len(qualities):
        raise ValueError("duplicate SEC observed financial quality record")
    grouped: dict[tuple[str, str], list[SecObservedFinancialQualityRecord]] = {}
    for item in qualities:
        production = by_production.get(item.production_identity)
        if production is None or item.recorded_at < production.produced_at:
            raise ValueError("quality record is not causally bound to production")
        grouped.setdefault((item.production_identity, item.quality_policy_version), []).append(item)
    for records in grouped.values():
        ordered = sorted(records, key=lambda x: (x.recorded_at, x.quality_record_id))
        if len({x.recorded_at for x in ordered}) != len(ordered):
            raise ValueError("conflicting same-time quality records")
        for index, item in enumerate(ordered):
            if index == 0:
                if (
                    item.supersedes_quality_record_id is not None
                    or item.status is SecQualityStatus.REVOKED
                ):
                    raise ValueError("invalid first quality record")
            elif item.supersedes_quality_record_id != ordered[index - 1].quality_record_id:
                raise ValueError("quality history is not a chronological chain")
    seen: set[tuple[str, str, str, datetime]] = set()
    for item in commits:
        production, quality = (
            by_production.get(item.production_identity),
            ids.get(item.quality_record_id),
        )
        key = (
            item.production_identity,
            item.quality_record_id,
            item.consumer_dataset_identity,
            item.committed_at,
        )
        if key in seen:
            raise ValueError("conflicting same-time consumer commits")
        seen.add(key)
        if (
            production is None
            or quality is None
            or quality.production_identity != item.production_identity
            or quality.status is not SecQualityStatus.PASS
            or item.committed_at < quality.recorded_at
            or quality.recorded_at < production.produced_at
        ):
            raise ValueError("consumer commit has invalid dependency")

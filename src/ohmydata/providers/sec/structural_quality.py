"""Offline structural assertions for replay-bound SEC normalized rows.

The checks in this module annotate supplied typed rows.  They neither inspect
raw SEC material nor establish correctness, availability, or quality-policy
outcomes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from ._structural_admission import admit as _admit
from .financials import SecStatementRow
from .pit import SecNormalizedFinancialFactVersion
from .quality_findings import (
    SecMissingDataReason,
    SecQualityEvidenceRef,
    SecQualityFinding,
    SecQualityFindingStatus,
    SecQualityIssueClass,
)

RULE_VERSION = "sec-structural-v1"
_FIELD_MISSING = "sec.structural.field_missing"
_PERIOD = "sec.structural.period"
_DUPLICATE_VALUE = "sec.structural.duplicate_value"
_RULE_IDS = (_FIELD_MISSING, _PERIOD, _DUPLICATE_VALUE)
_MAX_RECORDS = 10_000


def _utc(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _present(value: object) -> bool:
    return value is not None and (type(value) is not str or bool(value.strip()))


def _finding(
    version: SecNormalizedFinancialFactVersion,
    *,
    field: str,
    finding_key: str,
    rule_id: str,
    missing_reason: SecMissingDataReason | None,
    reason: str,
    detected_at: datetime,
    recorded_at: datetime,
) -> SecQualityFinding:
    return SecQualityFinding(
        normalized_version_id=version.normalized_version_id,
        affected_fields=(field,),
        finding_key=finding_key,
        rule_id=rule_id,
        rule_version=RULE_VERSION,
        adapter_version=version.adapter_version,
        issue_class=SecQualityIssueClass.UNKNOWN,
        missing_reason=missing_reason,
        reason=reason,
        evidence=(
            SecQualityEvidenceRef(
                version.observation.observation_identity, version.observation.fact_version
            ),
        ),
        status=SecQualityFindingStatus.OPEN,
        detected_at=detected_at,
        recorded_at=recorded_at,
    )


def _identity_complete(row: SecStatementRow) -> bool:
    if not all(
        _present(getattr(row, name)) for name in ("concept", "context_ref", "unit", "unit_ref")
    ):
        return False
    if row.period_type == "instant":
        return row.period_end is not None
    return (
        row.period_type == "duration"
        and row.period_start is not None
        and row.period_end is not None
    )


def _duplicate_key(version: SecNormalizedFinancialFactVersion) -> tuple[object, ...]:
    row = version.row
    return (
        version.observation.observation_identity,
        version.observation.fact_version,
        version.accession_number,
        version.vintage_identity,
        version.source_artifact_identity,
        version.schema_version,
        version.adapter_version,
        version.normalization_version,
        version.configuration_identity,
        version.recorded_at,
        row.statement_type,
        row.concept,
        row.context_ref,
        row.unit,
        row.unit_ref,
        row.period_type,
        row.period_start,
        row.period_end,
        row.period_key,
        row.period_source,
        row.dimension,
        row.is_point_in_time,
        row.decimals,
        row.decimals_native,
    )


@dataclass(frozen=True)
class SecStructuralQualityReport:
    """Frozen coverage receipt and generated structural findings."""

    rule_version: str
    rule_ids: tuple[str, ...]
    checked_normalized_version_ids: tuple[str, ...]
    input_record_count: int
    findings: tuple[SecQualityFinding, ...]

    def __post_init__(self) -> None:
        if self.rule_version != RULE_VERSION or self.rule_ids != _RULE_IDS:
            raise ValueError("invalid SEC structural quality rule receipt")
        if type(self.input_record_count) is not int or isinstance(self.input_record_count, bool):
            raise TypeError("input_record_count must be an integer")
        if self.input_record_count < len(self.checked_normalized_version_ids):
            raise ValueError("input_record_count cannot be smaller than checked versions")
        if (
            tuple(sorted(self.checked_normalized_version_ids))
            != self.checked_normalized_version_ids
        ):
            raise ValueError("checked_normalized_version_ids must be sorted")
        if len(set(self.checked_normalized_version_ids)) != len(
            self.checked_normalized_version_ids
        ):
            raise ValueError("checked_normalized_version_ids must be unique")
        for value in self.checked_normalized_version_ids:
            if type(value) is not str or len(value) != 64:
                raise ValueError("invalid checked normalized version identity")
        if type(self.findings) is not tuple or any(
            type(item) is not SecQualityFinding for item in self.findings
        ):
            raise TypeError("findings must contain SecQualityFinding values")
        checked = set(self.checked_normalized_version_ids)
        ordered = tuple(
            sorted(
                self.findings,
                key=lambda item: (
                    item.normalized_version_id,
                    item.rule_id,
                    item.finding_key,
                    item.finding_id,
                ),
            )
        )
        if ordered != self.findings:
            raise ValueError("findings must be canonically ordered")
        if any(
            item.rule_version != RULE_VERSION
            or item.rule_id not in _RULE_IDS
            or item.normalized_version_id not in checked
            for item in self.findings
        ):
            raise ValueError("findings are not valid SEC structural quality output")


def evaluate_sec_structural_quality(
    versions: Iterable[SecNormalizedFinancialFactVersion],
    *,
    detected_at: datetime,
    recorded_at: datetime,
    max_records: int = _MAX_RECORDS,
    max_findings: int = _MAX_RECORDS,
) -> SecStructuralQualityReport:
    """Evaluate bounded structural assertions on supplied normalized SEC rows."""
    for value, name in ((max_records, "max_records"), (max_findings, "max_findings")):
        if type(value) is not int or isinstance(value, bool) or not 0 < value <= _MAX_RECORDS:
            raise ValueError(f"{name} must be a positive integer at most {_MAX_RECORDS}")
    detected = _utc(detected_at, "detected_at")
    recorded = _utc(recorded_at, "recorded_at")
    if detected > recorded:
        raise ValueError("detected_at must not follow recorded_at")
    supplied: list[SecNormalizedFinancialFactVersion] = []
    iterator = iter(versions)
    for _ in range(max_records + 1):
        try:
            value = next(iterator)
        except StopIteration:
            break
        if len(supplied) == max_records:
            raise ValueError("structural quality input exceeds max_records")
        supplied.append(_admit(value))
    unique: dict[str, SecNormalizedFinancialFactVersion] = {}
    for version in supplied:
        if detected < version.recorded_at or recorded < detected:
            raise ValueError("structural quality timestamps must follow version production")
        prior = unique.get(version.normalized_version_id)
        if prior is not None and prior != version:
            raise ValueError("normalized version identity collision")
        unique[version.normalized_version_id] = version
    ordered_versions = tuple(unique[key] for key in sorted(unique))
    findings: list[SecQualityFinding] = []

    def append(item: SecQualityFinding) -> None:
        if len(findings) == max_findings:
            raise ValueError("structural quality output exceeds max_findings")
        findings.append(item)

    for version in ordered_versions:
        row = version.row
        for field in ("concept", "context_ref", "unit", "unit_ref"):
            if not _present(getattr(row, field)):
                append(
                    _finding(
                        version,
                        field=field,
                        finding_key=field,
                        rule_id=_FIELD_MISSING,
                        missing_reason=SecMissingDataReason.FIELD_MISSING,
                        reason="Structural evaluator found an absent required row field.",
                        detected_at=detected,
                        recorded_at=recorded,
                    )
                )
        if row.value is None:
            append(
                _finding(
                    version,
                    field="value",
                    finding_key="value",
                    rule_id=_FIELD_MISSING,
                    missing_reason=SecMissingDataReason.FIELD_MISSING,
                    reason="Structural evaluator found an absent required row field.",
                    detected_at=detected,
                    recorded_at=recorded,
                )
            )
        if not _present(row.period_type):
            append(
                _finding(
                    version,
                    field="period_type",
                    finding_key="period_type",
                    rule_id=_PERIOD,
                    missing_reason=SecMissingDataReason.FIELD_MISSING,
                    reason="Structural evaluator found an absent required row field.",
                    detected_at=detected,
                    recorded_at=recorded,
                )
            )
        elif row.period_type not in {"instant", "duration"}:
            append(
                _finding(
                    version,
                    field="period_type",
                    finding_key="period_type",
                    rule_id=_PERIOD,
                    missing_reason=None,
                    reason="Structural evaluator found an unsupported period type.",
                    detected_at=detected,
                    recorded_at=recorded,
                )
            )
        else:
            required = (
                ("period_end",) if row.period_type == "instant" else ("period_start", "period_end")
            )
            for field in required:
                if getattr(row, field) is None:
                    append(
                        _finding(
                            version,
                            field=field,
                            finding_key=field,
                            rule_id=_PERIOD,
                            missing_reason=SecMissingDataReason.FIELD_MISSING,
                            reason="Structural evaluator found an absent required row field.",
                            detected_at=detected,
                            recorded_at=recorded,
                        )
                    )
    groups: dict[tuple[object, ...], list[SecNormalizedFinancialFactVersion]] = defaultdict(list)
    for version in ordered_versions:
        if _identity_complete(version.row) and version.row.value is not None:
            groups[_duplicate_key(version)].append(version)
    for values in groups.values():
        if len({item.row.value for item in values}) > 1:
            for version in values:
                append(
                    _finding(
                        version,
                        field="value",
                        finding_key="duplicate_value",
                        rule_id=_DUPLICATE_VALUE,
                        missing_reason=None,
                        reason="Structural evaluator found conflicting values for the same structural identity.",
                        detected_at=detected,
                        recorded_at=recorded,
                    )
                )
    final_findings = tuple(
        sorted(
            findings,
            key=lambda item: (
                item.normalized_version_id,
                item.rule_id,
                item.finding_key,
                item.finding_id,
            ),
        )
    )
    return SecStructuralQualityReport(
        RULE_VERSION, _RULE_IDS, tuple(sorted(unique)), len(supplied), final_findings
    )


__all__ = ["RULE_VERSION", "SecStructuralQualityReport", "evaluate_sec_structural_quality"]

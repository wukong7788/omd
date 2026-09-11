"""Caller-authored SEC normalized-row quality findings and bounded history lookup.

This module records assertions about a normalized row.  It deliberately does
not inspect SEC payloads, correct values, or decide whether an assertion is
true.  Evidence references identify material retained by the caller.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from .financials import SecStatementRow

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT = 1_000
_MAX_NAME = 128
_MAX_FIELDS = 64
_MAX_EVIDENCE = 64
_DEFAULT_MAX_RECORDS = 10_000
_ROW_FIELDS = frozenset(SecStatementRow.__dataclass_fields__)


def _sha(value: str, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 identity")
    return value


def _text(value: str, name: str, *, maximum: int = _MAX_NAME) -> str:
    if type(value) is not str or len(value) > maximum or not value.strip():
        raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters")
    return value


def _utc(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class SecQualityIssueClass(str, Enum):
    PARSER_ERROR = "PARSER_ERROR"
    SOURCE_DISCLOSURE_SUSPECT = "SOURCE_DISCLOSURE_SUSPECT"
    SOURCE_REVISION = "SOURCE_REVISION"
    CROSS_SOURCE_CONFLICT = "CROSS_SOURCE_CONFLICT"
    UNKNOWN = "UNKNOWN"


class SecMissingDataReason(str, Enum):
    FIELD_MISSING = "FIELD_MISSING"
    SOURCE_NOT_DISCLOSED = "SOURCE_NOT_DISCLOSED"
    PARSE_FAILED = "PARSE_FAILED"
    CONFLICT_QUARANTINED = "CONFLICT_QUARANTINED"
    PIT_UNPROVEN = "PIT_UNPROVEN"


class SecQualityFindingStatus(str, Enum):
    OPEN = "OPEN"
    CONFIRMED = "CONFIRMED"
    DISMISSED = "DISMISSED"
    RETRACTED = "RETRACTED"


@dataclass(frozen=True)
class SecQualityEvidenceRef:
    """A caller-supplied identity reference, never source payload or location."""

    observation_identity: str
    fact_version: str

    def __post_init__(self) -> None:
        _sha(self.observation_identity, "observation_identity")
        _sha(self.fact_version, "fact_version")

    @property
    def identity(self) -> str:
        return _hash(
            {"observation_identity": self.observation_identity, "fact_version": self.fact_version}
        )


def _fields(value: tuple[str, ...]) -> tuple[str, ...]:
    if type(value) is not tuple or not value or len(value) > _MAX_FIELDS:
        raise ValueError(f"affected_fields must contain 1 to {_MAX_FIELDS} field names")
    result = tuple(sorted(_text(field, "affected_fields") for field in value))
    if len(set(result)) != len(result):
        raise ValueError("affected_fields must be unique")
    if any(field not in _ROW_FIELDS for field in result):
        raise ValueError("affected_fields must name SecStatementRow fields")
    return result


def _evidence(value: tuple[SecQualityEvidenceRef, ...]) -> tuple[SecQualityEvidenceRef, ...]:
    if type(value) is not tuple or not value or len(value) > _MAX_EVIDENCE:
        raise ValueError(f"evidence must contain 1 to {_MAX_EVIDENCE} references")
    if any(type(item) is not SecQualityEvidenceRef for item in value):
        raise TypeError("evidence must contain SecQualityEvidenceRef values")
    result = tuple(sorted(value, key=lambda item: item.identity))
    if len({item.identity for item in result}) != len(result):
        raise ValueError("evidence references must be unique")
    return result


@dataclass(frozen=True)
class SecQualityFinding:
    """One immutable, caller-authored finding or adjudication revision."""

    normalized_version_id: str
    affected_fields: tuple[str, ...]
    finding_key: str
    rule_id: str
    rule_version: str
    adapter_version: str
    issue_class: SecQualityIssueClass
    missing_reason: SecMissingDataReason | None
    reason: str
    evidence: tuple[SecQualityEvidenceRef, ...]
    status: SecQualityFindingStatus
    detected_at: datetime
    recorded_at: datetime
    adjudicated_at: datetime | None = None
    supersedes_finding_id: str | None = None
    finding_id: str = field(init=False)

    def __post_init__(self) -> None:
        _sha(self.normalized_version_id, "normalized_version_id")
        fields = _fields(self.affected_fields)
        for value, name in (
            (self.finding_key, "finding_key"),
            (self.rule_id, "rule_id"),
            (self.rule_version, "rule_version"),
            (self.adapter_version, "adapter_version"),
        ):
            _text(value, name)
        if type(self.issue_class) is not SecQualityIssueClass:
            raise TypeError("issue_class must be a SecQualityIssueClass")
        if (
            self.missing_reason is not None
            and type(self.missing_reason) is not SecMissingDataReason
        ):
            raise TypeError("missing_reason must be a SecMissingDataReason or None")
        _text(self.reason, "reason", maximum=_MAX_TEXT)
        evidence = _evidence(self.evidence)
        if type(self.status) is not SecQualityFindingStatus:
            raise TypeError("status must be a SecQualityFindingStatus")
        detected = _utc(self.detected_at, "detected_at")
        recorded = _utc(self.recorded_at, "recorded_at")
        if detected > recorded:
            raise ValueError("detected_at must not follow recorded_at")
        if self.status is SecQualityFindingStatus.OPEN:
            if self.adjudicated_at is not None:
                raise ValueError("OPEN finding must not have adjudicated_at")
            adjudicated = None
        else:
            if self.adjudicated_at is None:
                raise ValueError("non-OPEN finding requires adjudicated_at")
            adjudicated = _utc(self.adjudicated_at, "adjudicated_at")
            if not detected <= adjudicated <= recorded:
                raise ValueError("detected_at <= adjudicated_at <= recorded_at is required")
        if self.status is SecQualityFindingStatus.RETRACTED and self.supersedes_finding_id is None:
            raise ValueError("RETRACTED finding must supersede a preceding finding")
        if self.supersedes_finding_id is not None:
            _sha(self.supersedes_finding_id, "supersedes_finding_id")
        object.__setattr__(self, "affected_fields", fields)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "detected_at", detected)
        object.__setattr__(self, "recorded_at", recorded)
        object.__setattr__(self, "adjudicated_at", adjudicated)
        object.__setattr__(
            self,
            "finding_id",
            _hash(
                {
                    "normalized_version_id": self.normalized_version_id,
                    "affected_fields": fields,
                    "finding_key": self.finding_key,
                    "rule_id": self.rule_id,
                    "rule_version": self.rule_version,
                    "adapter_version": self.adapter_version,
                    "issue_class": self.issue_class.value,
                    "missing_reason": None
                    if self.missing_reason is None
                    else self.missing_reason.value,
                    "reason": self.reason,
                    "evidence": [item.identity for item in evidence],
                    "status": self.status.value,
                    "detected_at": detected.isoformat().replace("+00:00", "Z"),
                    "recorded_at": recorded.isoformat().replace("+00:00", "Z"),
                    "adjudicated_at": None
                    if adjudicated is None
                    else adjudicated.isoformat().replace("+00:00", "Z"),
                    "supersedes_finding_id": self.supersedes_finding_id,
                }
            ),
        )

    @property
    def scope(self) -> tuple[str, tuple[str, ...], str, str, str, str]:
        return (
            self.normalized_version_id,
            self.affected_fields,
            self.finding_key,
            self.rule_id,
            self.rule_version,
            self.adapter_version,
        )


def select_sec_quality_findings(
    findings: Iterable[SecQualityFinding],
    *,
    normalized_version_id: str,
    rule_version: str,
    cutoff: datetime,
    max_records: int = _DEFAULT_MAX_RECORDS,
) -> tuple[SecQualityFinding, ...]:
    """Return the latest visible caller-authored finding per rule/key as of ``cutoff``.

    This is a bounded quality-history lookup.  It is not a PIT selector and
    does not make a finding a financial-field policy or source correctness fact.
    """
    _sha(normalized_version_id, "normalized_version_id")
    _text(rule_version, "rule_version")
    cutoff = _utc(cutoff, "cutoff")
    if (
        type(max_records) is not int
        or isinstance(max_records, bool)
        or not 0 < max_records <= _DEFAULT_MAX_RECORDS
    ):
        raise ValueError(f"max_records must be a positive integer at most {_DEFAULT_MAX_RECORDS}")
    supplied: list[SecQualityFinding] = []
    iterator = iter(findings)
    for _ in range(max_records + 1):
        try:
            finding = next(iterator)
        except StopIteration:
            break
        if len(supplied) == max_records:
            raise ValueError("quality finding input exceeds max_records")
        if type(finding) is not SecQualityFinding:
            raise TypeError("findings must contain SecQualityFinding values")
        finding.__post_init__()
        supplied.append(finding)
    visible = [
        item
        for item in supplied
        if item.normalized_version_id == normalized_version_id
        and item.rule_version == rule_version
        and item.recorded_at <= cutoff
        and (item.adjudicated_at is None or item.adjudicated_at <= cutoff)
    ]
    by_id: dict[str, SecQualityFinding] = {}
    for item in visible:
        previous = by_id.get(item.finding_id)
        if previous is not None and previous != item:
            raise ValueError("quality finding identity collision")
        by_id[item.finding_id] = item
    children: dict[str, list[SecQualityFinding]] = {}
    roots: dict[tuple[str, str], SecQualityFinding] = {}
    for item in by_id.values():
        predecessor_id = item.supersedes_finding_id
        if predecessor_id is None:
            key = (item.rule_id, item.finding_key)
            if key in roots:
                raise ValueError("same rule/finding key has multiple independent roots")
            roots[key] = item
            continue
        predecessor = by_id.get(predecessor_id)
        if predecessor is None:
            raise ValueError("quality finding supersedes target is missing")
        if predecessor.scope != item.scope:
            raise ValueError("quality finding revision changed its scope")
        if predecessor.recorded_at >= item.recorded_at:
            raise ValueError("quality finding revisions must have increasing recorded_at")
        children.setdefault(predecessor_id, []).append(item)
    if any(len(items) > 1 for items in children.values()):
        raise ValueError("quality finding revision fork")
    latest = [item for item in by_id.values() if item.finding_id not in children]
    return tuple(sorted(latest, key=lambda item: (item.rule_id, item.finding_key, item.finding_id)))


__all__ = [
    "SecMissingDataReason",
    "SecQualityEvidenceRef",
    "SecQualityFinding",
    "SecQualityFindingStatus",
    "SecQualityIssueClass",
    "select_sec_quality_findings",
]

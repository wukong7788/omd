"""Bounded, in-memory known-by selection for sealed observed SEC financials."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from copy import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, TypeVar

from .observed_xbrl_financials import SecObservedFinancialProduction
from .pit import SecQualityStatus

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_PRODUCTIONS = 100
_MAX_QUALITY_RECORDS = 10_000
_MAX_COMMITS = 10_000
_MAX_ROWS = 100_000


def _utc(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _sha(value: str, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 identity")
    return value


def _version(value: str, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _stamp(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _identity(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded(values: Iterable[object], name: str, maximum: int) -> tuple[object, ...]:
    items: list[object] = []
    for value in values:
        if len(items) >= maximum:
            raise ValueError(f"SEC observed replay {name} limit exceeded")
        items.append(value)
    return tuple(items)


def _limit(value: int, name: str, maximum: int) -> int:
    if type(value) is not int or value <= 0 or value > maximum:
        raise ValueError(f"{name} must be a positive integer no greater than {maximum}")
    return value


@dataclass(frozen=True)
class SecObservedFinancialReplayPolicy:
    """Every known-by selector input is explicit; it has no latest defaults."""

    output_schema_version: str
    parser_version: str
    configuration_version: str
    configuration_identity: str
    quality_policy_version: str
    consumer_dataset_identity: str
    knowledge_cutoff: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.output_schema_version, "output_schema_version"),
            (self.parser_version, "parser_version"),
            (self.configuration_version, "configuration_version"),
            (self.quality_policy_version, "quality_policy_version"),
        ):
            _version(value, name)
        _sha(self.configuration_identity, "configuration_identity")
        _sha(self.consumer_dataset_identity, "consumer_dataset_identity")
        object.__setattr__(
            self, "knowledge_cutoff", _utc(self.knowledge_cutoff, "knowledge_cutoff")
        )

    @property
    def quality_cutoff(self) -> datetime:
        """Quality is intentionally assessed at the same system cutoff."""
        return self.knowledge_cutoff

    @property
    def commit_cutoff(self) -> datetime:
        """Consumer commits are intentionally assessed at the same system cutoff."""
        return self.knowledge_cutoff


@dataclass(frozen=True)
class SecObservedFinancialQualityRecord:
    """Caller-attested quality history for one sealed production."""

    production_identity: str
    quality_policy_version: str
    status: SecQualityStatus
    recorded_at: datetime
    supersedes_quality_record_id: str | None = None
    quality_record_id: str = field(init=False)

    def __post_init__(self) -> None:
        _sha(self.production_identity, "production_identity")
        _version(self.quality_policy_version, "quality_policy_version")
        if type(self.status) is not SecQualityStatus:
            raise TypeError("status must be a SecQualityStatus")
        recorded = _utc(self.recorded_at, "recorded_at")
        if self.supersedes_quality_record_id is not None:
            _sha(self.supersedes_quality_record_id, "supersedes_quality_record_id")
        object.__setattr__(self, "recorded_at", recorded)
        object.__setattr__(
            self,
            "quality_record_id",
            _identity(
                {
                    "domain": "sec-observed-financial-quality-v1",
                    "production_identity": self.production_identity,
                    "quality_policy_version": self.quality_policy_version,
                    "status": self.status.value,
                    "recorded_at": _stamp(recorded),
                    "supersedes_quality_record_id": self.supersedes_quality_record_id,
                }
            ),
        )


@dataclass(frozen=True)
class SecObservedFinancialConsumerCommit:
    """Caller-attested consumer use of one exact observed quality record."""

    production_identity: str
    quality_record_id: str
    consumer_dataset_identity: str
    committed_at: datetime
    commit_id: str = field(init=False)

    def __post_init__(self) -> None:
        _sha(self.production_identity, "production_identity")
        _sha(self.quality_record_id, "quality_record_id")
        _sha(self.consumer_dataset_identity, "consumer_dataset_identity")
        committed = _utc(self.committed_at, "committed_at")
        object.__setattr__(self, "committed_at", committed)
        object.__setattr__(
            self,
            "commit_id",
            _identity(
                {
                    "domain": "sec-observed-financial-consumer-commit-v1",
                    "production_identity": self.production_identity,
                    "quality_record_id": self.quality_record_id,
                    "consumer_dataset_identity": self.consumer_dataset_identity,
                    "committed_at": _stamp(committed),
                }
            ),
        )


@dataclass(frozen=True)
class SecObservedFinancialReplayResult:
    """One complete package eligible at a caller's known-by cutoff."""

    production: SecObservedFinancialProduction
    quality: SecObservedFinancialQualityRecord
    commit: SecObservedFinancialConsumerCommit


def _deduplicate_productions(
    items: tuple[object, ...],
) -> tuple[SecObservedFinancialProduction, ...]:
    result: dict[str, SecObservedFinancialProduction] = {}
    for item in items:
        if type(item) is not SecObservedFinancialProduction:
            raise TypeError("production_identity has an invalid type")
        # Validate disposable shallow copies. Calling __post_init__ on caller
        # objects can repair cached derived identities before rejecting them.
        evidence = copy(item.evidence)
        vintage = copy(item.vintage)
        evidence.__post_init__()
        vintage.__post_init__()
        if vintage.vintage_identity != item.vintage.vintage_identity:
            raise ValueError("conflicting SEC observed replay vintage_identity")
        production = copy(item)
        object.__setattr__(production, "evidence", evidence)
        object.__setattr__(production, "vintage", vintage)
        production.__post_init__()
        if production.production_identity != item.production_identity:
            raise ValueError("conflicting SEC observed replay production_identity")
        identity = item.production_identity
        prior = result.get(identity)
        if prior is not None and prior != item:
            raise ValueError("conflicting SEC observed replay production_identity")
        result[identity] = item
    return tuple(result.values())


def _deduplicate_quality(
    items: tuple[object, ...],
) -> tuple[SecObservedFinancialQualityRecord, ...]:
    result: dict[str, SecObservedFinancialQualityRecord] = {}
    for item in items:
        if type(item) is not SecObservedFinancialQualityRecord:
            raise TypeError("quality_record_id has an invalid type")
        expected = SecObservedFinancialQualityRecord(
            item.production_identity,
            item.quality_policy_version,
            item.status,
            item.recorded_at,
            item.supersedes_quality_record_id,
        )
        if expected.quality_record_id != item.quality_record_id:
            raise ValueError("conflicting SEC observed replay quality_record_id")
        prior = result.get(item.quality_record_id)
        if prior is not None and prior != item:
            raise ValueError("conflicting SEC observed replay quality_record_id")
        result[item.quality_record_id] = item
    return tuple(result.values())


def _deduplicate_commits(
    items: tuple[object, ...],
) -> tuple[SecObservedFinancialConsumerCommit, ...]:
    result: dict[str, SecObservedFinancialConsumerCommit] = {}
    for item in items:
        if type(item) is not SecObservedFinancialConsumerCommit:
            raise TypeError("commit_id has an invalid type")
        expected = SecObservedFinancialConsumerCommit(
            item.production_identity,
            item.quality_record_id,
            item.consumer_dataset_identity,
            item.committed_at,
        )
        if expected.commit_id != item.commit_id:
            raise ValueError("conflicting SEC observed replay commit_id")
        prior = result.get(item.commit_id)
        if prior is not None and prior != item:
            raise ValueError("conflicting SEC observed replay commit_id")
        result[item.commit_id] = item
    return tuple(result.values())


class _ReplayProduction(Protocol):
    @property
    def production_identity(self) -> str: ...
    @property
    def produced_at(self) -> datetime: ...
    @property
    def output_schema_version(self) -> str: ...
    @property
    def parser_version(self) -> str: ...
    @property
    def configuration_version(self) -> str: ...
    @property
    def configuration_identity(self) -> str: ...


_P = TypeVar("_P", bound=_ReplayProduction)


def _validate_history(
    production: _ReplayProduction,
    records: tuple[SecObservedFinancialQualityRecord, ...],
) -> SecObservedFinancialQualityRecord | None:
    if not records:
        return None
    ordered = sorted(records, key=lambda item: (item.recorded_at, item.quality_record_id))
    if any(item.recorded_at < production.produced_at for item in ordered):
        raise ValueError("SEC observed quality predates production")
    if len({item.recorded_at for item in ordered}) != len(ordered):
        raise ValueError("SEC observed quality history has distinct same-time records")
    prior: SecObservedFinancialQualityRecord | None = None
    for item in ordered:
        if prior is None:
            if (
                item.supersedes_quality_record_id is not None
                or item.status is SecQualityStatus.REVOKED
            ):
                raise ValueError("SEC observed quality history has invalid first record")
        elif item.supersedes_quality_record_id != prior.quality_record_id:
            raise ValueError("SEC observed quality history is not a chronological chain")
        prior = item
    return prior


def select_sec_observed_financial_productions(
    productions: Iterable[SecObservedFinancialProduction],
    quality_records: Iterable[SecObservedFinancialQualityRecord],
    consumer_commits: Iterable[SecObservedFinancialConsumerCommit],
    policy: SecObservedFinancialReplayPolicy,
    *,
    max_productions: int = _MAX_PRODUCTIONS,
    max_quality_records: int = _MAX_QUALITY_RECORDS,
    max_commits: int = _MAX_COMMITS,
    max_rows: int = _MAX_ROWS,
) -> tuple[SecObservedFinancialReplayResult, ...]:
    """Select sealed packages only from caller-supplied in-memory evidence."""
    if type(policy) is not SecObservedFinancialReplayPolicy:
        raise TypeError("policy must be SecObservedFinancialReplayPolicy")
    policy.__post_init__()
    production_limit = _limit(max_productions, "max_productions", _MAX_PRODUCTIONS)
    quality_limit = _limit(max_quality_records, "max_quality_records", _MAX_QUALITY_RECORDS)
    commit_limit = _limit(max_commits, "max_commits", _MAX_COMMITS)
    row_limit = _limit(max_rows, "max_rows", _MAX_ROWS)
    raw_productions = _bounded(productions, "production", production_limit)
    row_count = 0
    for item in raw_productions:
        if type(item) is not SecObservedFinancialProduction:
            raise TypeError("production_identity has an invalid type")
        row_count += len(item.vintage.rows)
        if row_count > row_limit:
            raise ValueError("SEC observed replay aggregate row limit exceeded")
    productions_ = _deduplicate_productions(raw_productions)
    qualities = _deduplicate_quality(_bounded(quality_records, "quality record", quality_limit))
    commits = _deduplicate_commits(_bounded(consumer_commits, "commit", commit_limit))
    return tuple(
        SecObservedFinancialReplayResult(*item)
        for item in _select_validated(
            productions_,
            qualities,
            commits,
            policy,
            known_by=lambda item: item.evidence.known_by_at,
            accession=lambda item: item.vintage.accession_number,
        )
    )


def _select_validated(
    productions_: tuple[_P, ...],
    qualities: tuple[SecObservedFinancialQualityRecord, ...],
    commits: tuple[SecObservedFinancialConsumerCommit, ...],
    policy: SecObservedFinancialReplayPolicy,
    *,
    known_by: Callable[[_P], datetime],
    accession: Callable[[_P], str],
) -> tuple[tuple[_P, SecObservedFinancialQualityRecord, SecObservedFinancialConsumerCommit], ...]:
    """Shared temporal algorithm; public adapters own admission and seal validation."""
    by_production = {item.production_identity: item for item in productions_}
    selected = [
        item
        for item in productions_
        if item.output_schema_version == policy.output_schema_version
        and item.parser_version == policy.parser_version
        and item.configuration_version == policy.configuration_version
        and item.configuration_identity == policy.configuration_identity
        and known_by(item) <= item.produced_at <= policy.knowledge_cutoff
    ]
    visible_quality = [
        item
        for item in qualities
        if item.production_identity in by_production
        and item.quality_policy_version == policy.quality_policy_version
        and item.recorded_at <= policy.quality_cutoff
    ]
    qualities_by_production: dict[str, list[SecObservedFinancialQualityRecord]] = {}
    for item in visible_quality:
        qualities_by_production.setdefault(item.production_identity, []).append(item)
    latest_quality = {
        identity: _validate_history(by_production[identity], tuple(records))
        for identity, records in qualities_by_production.items()
    }
    visible_commits = [
        item
        for item in commits
        if item.production_identity in by_production
        and item.consumer_dataset_identity == policy.consumer_dataset_identity
        and item.committed_at <= policy.commit_cutoff
    ]
    commits_by_quality: dict[tuple[str, str], list[SecObservedFinancialConsumerCommit]] = {}
    quality_ids = {item.quality_record_id: item for item in visible_quality}
    all_quality_ids = {item.quality_record_id: item for item in qualities}
    for item in visible_commits:
        quality = quality_ids.get(item.quality_record_id)
        if quality is None:
            referenced = all_quality_ids.get(item.quality_record_id)
            if (
                referenced is not None
                and referenced.production_identity == item.production_identity
                and referenced.quality_policy_version != policy.quality_policy_version
            ):
                continue
        if (
            quality is None
            or quality.production_identity != item.production_identity
            or quality.status is not SecQualityStatus.PASS
        ):
            raise ValueError(
                "SEC observed commit does not bind a visible selected-policy quality record"
            )
        production = by_production[item.production_identity]
        if item.committed_at < quality.recorded_at or quality.recorded_at < production.produced_at:
            raise ValueError("SEC observed commit timing is invalid")
        commits_by_quality.setdefault(
            (item.production_identity, item.quality_record_id), []
        ).append(item)
    for records in commits_by_quality.values():
        if len({item.committed_at for item in records}) != len(records):
            raise ValueError("SEC observed commits have distinct same-time records")
    results: list[
        tuple[_P, SecObservedFinancialQualityRecord, SecObservedFinancialConsumerCommit]
    ] = []
    for production in selected:
        quality = latest_quality.get(production.production_identity)
        if quality is None or quality.status is not SecQualityStatus.PASS:
            continue
        matches = commits_by_quality.get(
            (production.production_identity, quality.quality_record_id), ()
        )
        if matches:
            results.append(
                (
                    production,
                    quality,
                    max(matches, key=lambda item: (item.committed_at, item.commit_id)),
                )
            )
    return tuple(
        sorted(
            results,
            key=lambda item: (
                known_by(item[0]),
                accession(item[0]),
                item[0].production_identity,
            ),
        )
    )


__all__ = [
    "SecObservedFinancialConsumerCommit",
    "SecObservedFinancialQualityRecord",
    "SecObservedFinancialReplayPolicy",
    "SecObservedFinancialReplayResult",
    "select_sec_observed_financial_productions",
]

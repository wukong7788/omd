"""Offline four-independent-quarter SEC revenue and net-income TTM recipes.

The recipe validates structural compatibility of caller declarations with
selected normalized rows.  CIK, quarter independence, accounting scope and
comparability cohort are caller assertions; this module does not validate
issuer ownership, source authenticity, or financial correctness.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from itertools import islice, pairwise
from typing import Any

from .pit import (
    SecConsumerCommit,
    SecNormalizedFinancialFactVersion,
    SecPitMode,
    SecPitPolicy,
    SecQualityRecord,
    _utc,
    select_sec_financial_versions,
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT_BYTES = 1_024
_MAX_CONFIG_BYTES = 65_536
_MAX_INPUT_RECORDS = 10_000
_MAX_DECIMAL_DIGITS = 10_000


class SecQuarterTtmRecipe(str, Enum):
    """The only supported four-quarter sum formulas and their initial versions."""

    REVENUE_V1 = "sec-four-quarter-revenue-sum-v1"
    NET_INCOME_V1 = "sec-four-quarter-net-income-sum-v1"

    @property
    def metric_name(self) -> str:
        return "revenue" if self is SecQuarterTtmRecipe.REVENUE_V1 else "net_income"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ValueError(f"{name} must be non-empty and at most {_MAX_TEXT_BYTES} UTF-8 bytes")
    return value


def _sha(value: object, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 identity")
    return value


def _canonical(value: Any) -> Any:
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is Decimal:
        return {"decimal": str(value)}
    if type(value) is datetime:
        return {"datetime": _utc(value, "datetime").isoformat().replace("+00:00", "Z")}
    if type(value) is date:
        return {"date": value.isoformat()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple | list):
        return [_canonical(item) for item in value]
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def _payload_bytes(value: Any) -> bytes:
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class SecQuarterDeclaration:
    """A caller assertion binding a normalized version to one independent quarter."""

    normalized_version_id: str
    fiscal_year: int
    fiscal_quarter: int
    independent_quarter: bool
    period_start: date
    period_end: date
    native_concept: str
    accounting_scope: str
    attribution_scope: str
    comparability_revision_cohort: str
    declaration_reference: str

    def __post_init__(self) -> None:
        _sha(self.normalized_version_id, "normalized_version_id")
        if type(self.fiscal_year) is not int or type(self.fiscal_quarter) is not int:
            raise TypeError("fiscal year and quarter must be integers")
        if not 1 <= self.fiscal_year <= 9_999:
            raise ValueError("fiscal_year must be in 1..9999")
        if not 1 <= self.fiscal_quarter <= 4:
            raise ValueError("fiscal_quarter must be in 1..4")
        if self.independent_quarter is not True:
            raise ValueError("quarter declaration must explicitly assert an independent quarter")
        if type(self.period_start) is not date or type(self.period_end) is not date:
            raise TypeError("quarter period dates must be dates")
        if self.period_start > self.period_end:
            raise ValueError("quarter period dates are reversed")
        for value, name in (
            (self.native_concept, "native_concept"),
            (self.accounting_scope, "accounting_scope"),
            (self.attribution_scope, "attribution_scope"),
            (self.comparability_revision_cohort, "comparability_revision_cohort"),
            (self.declaration_reference, "declaration_reference"),
        ):
            _text(value, name)


@dataclass(frozen=True)
class SecQuarterTtmConfig:
    """Explicit query and caller-declaration configuration for one TTM calculation."""

    recipe: SecQuarterTtmRecipe
    canonical_cik: str
    metric_name: str
    issuer_declaration_reference: str
    metric_declaration_reference: str
    quarters: tuple[SecQuarterDeclaration, ...]
    mode: SecPitMode
    knowledge_cutoff: datetime
    policy: SecPitPolicy
    configuration_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.recipe) is not SecQuarterTtmRecipe:
            raise TypeError("recipe must be a SecQuarterTtmRecipe")
        _text(self.canonical_cik, "canonical_cik")
        if not self.canonical_cik.isdecimal() or (
            str(int(self.canonical_cik)) != self.canonical_cik
        ):
            raise ValueError("canonical_cik must be canonical decimal CIK")
        _text(self.metric_name, "metric_name")
        if self.metric_name != self.recipe.metric_name:
            raise ValueError("metric_name does not match recipe")
        _text(self.issuer_declaration_reference, "issuer_declaration_reference")
        _text(self.metric_declaration_reference, "metric_declaration_reference")
        if type(self.quarters) is not tuple or len(self.quarters) != 4:
            raise ValueError("quarters must contain exactly four declarations")
        if any(type(item) is not SecQuarterDeclaration for item in self.quarters):
            raise TypeError("quarters must contain SecQuarterDeclaration values")
        ids = tuple(item.normalized_version_id for item in self.quarters)
        if len(set(ids)) != 4:
            raise ValueError("quarter declarations must name four distinct normalized versions")
        if type(self.mode) is not SecPitMode or type(self.policy) is not SecPitPolicy:
            raise TypeError("mode and policy must be SEC PIT values")
        for value, name in (
            (self.policy.schema_version, "policy.schema_version"),
            (self.policy.adapter_version, "policy.adapter_version"),
            (self.policy.normalization_version, "policy.normalization_version"),
            (self.policy.configuration_identity, "policy.configuration_identity"),
            (self.policy.quality_policy_version, "policy.quality_policy_version"),
        ):
            _text(value, name)
        cutoff = _utc(self.knowledge_cutoff, "knowledge_cutoff")
        object.__setattr__(self, "knowledge_cutoff", cutoff)
        payload = self._identity_payload()
        encoded = _payload_bytes(payload)
        if len(encoded) > _MAX_CONFIG_BYTES:
            raise ValueError("quarter TTM configuration exceeds byte limit")
        object.__setattr__(self, "configuration_identity", hashlib.sha256(encoded).hexdigest())

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": "sec-quarter-ttm-config-v1",
            "recipe": self.recipe,
            "canonical_cik": self.canonical_cik,
            "metric_name": self.metric_name,
            "issuer_declaration_reference": self.issuer_declaration_reference,
            "metric_declaration_reference": self.metric_declaration_reference,
            "quarters": tuple(vars(item) for item in self.quarters),
            "mode": self.mode,
            "knowledge_cutoff": self.knowledge_cutoff,
            "policy": vars(self.policy),
        }


@dataclass(frozen=True)
class SecQuarterTtmInputEvidence:
    """Selected source lineage for one structurally compatible declared quarter."""

    declaration: SecQuarterDeclaration
    version: SecNormalizedFinancialFactVersion
    quality_record: SecQualityRecord
    consumer_commit: SecConsumerCommit | None

    def __post_init__(self) -> None:
        if type(self.declaration) is not SecQuarterDeclaration:
            raise TypeError("declaration must be a SecQuarterDeclaration")
        if type(self.version) is not SecNormalizedFinancialFactVersion:
            raise TypeError("version must be a SecNormalizedFinancialFactVersion")
        if type(self.quality_record) is not SecQualityRecord:
            raise TypeError("quality_record must be a SecQualityRecord")
        if self.consumer_commit is not None and type(self.consumer_commit) is not SecConsumerCommit:
            raise TypeError("consumer_commit must be a SecConsumerCommit or None")


@dataclass(frozen=True)
class SecQuarterTtmResult:
    """Immutable aggregate and source evidence; declarations are caller assertions."""

    config: SecQuarterTtmConfig
    aggregate_period_start: date
    aggregate_period_end: date
    value: Decimal
    unit: str
    inputs: tuple[SecQuarterTtmInputEvidence, ...]
    input_availability_bound: datetime
    result_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.config) is not SecQuarterTtmConfig:
            raise TypeError("config must be a SecQuarterTtmConfig")
        if type(self.value) is not Decimal or not self.value.is_finite():
            raise ValueError("value must be a finite Decimal")
        _text(self.unit, "unit")
        if type(self.inputs) is not tuple or len(self.inputs) != 4:
            raise ValueError("inputs must contain four evidence records")
        if any(type(item) is not SecQuarterTtmInputEvidence for item in self.inputs):
            raise TypeError("inputs must contain SecQuarterTtmInputEvidence values")
        bound = _utc(self.input_availability_bound, "input_availability_bound")
        object.__setattr__(self, "input_availability_bound", bound)
        payload = {
            "schema": "sec-quarter-ttm-result-v1",
            "configuration_identity": self.config.configuration_identity,
            "recipe": self.config.recipe,
            "mode": self.config.mode,
            "knowledge_cutoff": self.config.knowledge_cutoff,
            "policy": vars(self.config.policy),
            "aggregate_period_start": self.aggregate_period_start,
            "aggregate_period_end": self.aggregate_period_end,
            "value": self.value,
            "unit": self.unit,
            "input_availability_bound": bound,
            "inputs": tuple(
                {
                    "declaration": vars(item.declaration),
                    "normalized_version_id": item.version.normalized_version_id,
                    "content_identity": item.version.content_identity,
                    "observation_identity": item.version.observation.observation_identity,
                    "fact_version": item.version.observation.fact_version,
                    "quality_record_id": item.quality_record.quality_record_id,
                    "consumer_commit_id": (
                        item.consumer_commit.commit_id if item.consumer_commit is not None else None
                    ),
                }
                for item in self.inputs
            ),
        }
        object.__setattr__(
            self, "result_identity", hashlib.sha256(_payload_bytes(payload)).hexdigest()
        )


def _bounded_inputs(values: Iterable[Any], maximum: int) -> tuple[Any, ...]:
    items = tuple(islice(values, maximum + 1))
    if len(items) > maximum:
        raise ValueError("quarter TTM input record limit exceeded")
    return items


def _validate_limit(max_input_records: int) -> None:
    if type(max_input_records) is not int or not 0 < max_input_records <= _MAX_INPUT_RECORDS:
        raise ValueError("max_input_records must be a positive integer at most 10000")


def _quarter_sequence(quarters: tuple[SecQuarterDeclaration, ...]) -> None:
    for earlier, later in pairwise(quarters):
        expected = (earlier.fiscal_year, earlier.fiscal_quarter + 1)
        if earlier.fiscal_quarter == 4:
            expected = (earlier.fiscal_year + 1, 1)
        if (later.fiscal_year, later.fiscal_quarter) != expected:
            raise ValueError("quarter declarations must be consecutive")
        if later.period_start <= earlier.period_end:
            raise ValueError("quarter declarations overlap")
        if later.period_start != earlier.period_end + timedelta(days=1):
            raise ValueError("quarter declarations have a gap")
    common = (
        quarters[0].accounting_scope,
        quarters[0].attribution_scope,
        quarters[0].comparability_revision_cohort,
    )
    if any(
        (item.accounting_scope, item.attribution_scope, item.comparability_revision_cohort)
        != common
        for item in quarters[1:]
    ):
        raise ValueError("quarter declarations have incompatible scope or cohort")


def _decimal_sum(values: tuple[Decimal, ...]) -> Decimal:
    coefficients: list[tuple[int, int, int]] = []
    for value in values:
        if not value.is_finite():
            raise ValueError("quarter values must be finite")
        digits = value.as_tuple().digits
        exponent = value.as_tuple().exponent
        adjusted = value.adjusted()
        if type(exponent) is not int:
            raise ValueError("quarter Decimal must have a finite integer exponent")
        if (
            len(digits) > _MAX_DECIMAL_DIGITS
            or abs(exponent) > _MAX_DECIMAL_DIGITS
            or abs(adjusted) > _MAX_DECIMAL_DIGITS
        ):
            raise ValueError("quarter Decimal exceeds digit bounds")
        coefficient = 0
        for digit in digits:
            coefficient = coefficient * 10 + digit
        coefficients.append(
            (-coefficient if value.is_signed() else coefficient, exponent, adjusted)
        )
    e = min(item[1] for item in coefficients)
    a = max(item[2] for item in coefficients)
    if max(1, a - e + 1) + 1 > _MAX_DECIMAL_DIGITS:
        raise ValueError("quarter Decimal sum span exceeds digit bounds")
    total = sum(coefficient * 10 ** (exponent - e) for coefficient, exponent, _ in coefficients)
    if total == 0:
        return Decimal((0, (0,), e))
    sign = int(total < 0)
    total_digits = Decimal(abs(total)).as_tuple().digits
    return Decimal((sign, total_digits, e))


def compute_sec_four_quarter_ttm(
    *,
    config: SecQuarterTtmConfig,
    versions: Iterable[SecNormalizedFinancialFactVersion],
    quality_records: Iterable[SecQualityRecord],
    consumer_commits: Iterable[SecConsumerCommit] = (),
    max_input_records: int = _MAX_INPUT_RECORDS,
) -> SecQuarterTtmResult:
    """Select and exactly sum four caller-declared independent SEC quarters offline."""
    if type(config) is not SecQuarterTtmConfig:
        raise TypeError("config must be a SecQuarterTtmConfig")
    _validate_limit(max_input_records)
    version_items = _bounded_inputs(versions, max_input_records)
    quality_items = _bounded_inputs(quality_records, max_input_records - len(version_items))
    commit_items = _bounded_inputs(
        consumer_commits, max_input_records - len(version_items) - len(quality_items)
    )
    _quarter_sequence(config.quarters)
    selected = select_sec_financial_versions(
        version_items,
        mode=config.mode,
        knowledge_cutoff=config.knowledge_cutoff,
        policy=config.policy,
        quality_records=quality_items,
        consumer_commits=commit_items,
    )
    by_id = {item.version.normalized_version_id: item for item in selected}
    declared_ids = {item.normalized_version_id for item in config.quarters}
    if not declared_ids <= by_id.keys():
        raise ValueError("requested quarter version is unavailable")
    evidence: list[SecQuarterTtmInputEvidence] = []
    rows = []
    for declaration in config.quarters:
        item = by_id[declaration.normalized_version_id]
        row = item.version.row
        if (
            row.value is None
            or not row.value.is_finite()
            or row.period_type != "duration"
            or row.period_start != declaration.period_start
            or row.period_end != declaration.period_end
            or row.concept != declaration.native_concept
        ):
            raise ValueError("selected row does not match independent-quarter declaration")
        if row.unit is None or not row.unit:
            raise ValueError("selected rows require a complete non-null unit")
        rows.append(row)
        evidence.append(
            SecQuarterTtmInputEvidence(
                declaration,
                item.version,
                item.quality_record,
                item.consumer_commit,
            )
        )
    if len({row.unit for row in rows}) != 1 or len({row.dimension for row in rows}) != 1:
        raise ValueError("selected rows have incompatible unit or dimensions")
    values: list[Decimal] = []
    for row in rows:
        if type(row.value) is not Decimal:
            raise ValueError("selected rows require Decimal values")
        values.append(row.value)
    total = _decimal_sum(tuple(values))
    times = [item.version.source_available_at for item in evidence]
    if config.mode is SecPitMode.SYSTEM_REPLAY:
        for item in evidence:
            times.extend(
                (
                    item.version.observation.snapshot_fetched_at,
                    item.version.recorded_at,
                    item.quality_record.recorded_at,
                )
            )
            if item.consumer_commit is None:
                raise ValueError("SYSTEM_REPLAY selected input lacks consumer commit")
        times.extend(item.consumer_commit.committed_at for item in evidence if item.consumer_commit)
    unit = rows[0].unit
    if unit is None:
        raise ValueError("selected rows require a complete non-null unit")
    return SecQuarterTtmResult(
        config=config,
        aggregate_period_start=config.quarters[0].period_start,
        aggregate_period_end=config.quarters[-1].period_end,
        value=total,
        unit=unit,
        inputs=tuple(evidence),
        input_availability_bound=max(times),
    )


__all__ = [
    "SecQuarterDeclaration",
    "SecQuarterTtmConfig",
    "SecQuarterTtmInputEvidence",
    "SecQuarterTtmRecipe",
    "SecQuarterTtmResult",
    "compute_sec_four_quarter_ttm",
]

"""Explicit rules and immutable evidence for observed accounting diagnostics."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from ._metric_common import identity, text
from .financials import SecStatementRow


class SecAccountingApplicability(str, Enum):
    SAME_CONTEXT = "SAME_CONTEXT"
    CASH_ROLLFORWARD = "CASH_ROLLFORWARD"


class SecAccountingTolerance(str, Enum):
    EXACT = "EXACT"
    ASSUME_NEAREST_REPORTED_DECIMALS = "ASSUME_NEAREST_REPORTED_DECIMALS"


class SecAccountingStatus(str, Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    MISSING = "MISSING"
    INCOMPARABLE = "INCOMPARABLE"


@dataclass(frozen=True)
class SecAccountingTerm:
    statement_type: str
    concept: str
    context_ref: str
    coefficient: int

    def __post_init__(self) -> None:
        for name in ("statement_type", "concept", "context_ref"):
            if not text(getattr(self, name), name).strip():
                raise ValueError("accounting selectors must not be blank")
        if type(self.coefficient) is not int or self.coefficient not in (-1, 1):
            raise ValueError("accounting coefficients must be +1 or -1")

    @property
    def selector(self) -> tuple[str, str, str]:
        return self.statement_type, self.concept, self.context_ref


@dataclass(frozen=True)
class SecAccountingRule:
    rule_id: str
    applicability: SecAccountingApplicability
    terms: tuple[SecAccountingTerm, ...]
    expected_unit: str
    tolerance_policy: SecAccountingTolerance
    scope_reference: str
    rule_identity: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("rule_id", "expected_unit", "scope_reference"):
            if not text(getattr(self, name), name).strip():
                raise ValueError("accounting declarations must not be blank")
        if (
            type(self.applicability) is not SecAccountingApplicability
            or type(self.tolerance_policy) is not SecAccountingTolerance
        ):
            raise TypeError("accounting applicability/tolerance requires enum values")
        if type(self.terms) is not tuple or not 2 <= len(self.terms) <= 8:
            raise ValueError("accounting rules require 2..8 terms")
        for term in self.terms:
            if type(term) is not SecAccountingTerm:
                raise TypeError("invalid accounting term")
            term.__post_init__()
        if len({term.selector for term in self.terms}) != len(self.terms):
            raise ValueError("accounting term selectors must be unique")
        if self.applicability is SecAccountingApplicability.CASH_ROLLFORWARD and (
            tuple(t.coefficient for t in self.terms) != (1, -1, -1)
            or self.terms[0].concept != self.terms[1].concept
        ):
            raise ValueError("cash rollforward requires end/begin same concept and +1/-1/-1")
        object.__setattr__(self, "rule_identity", identity("sec-accounting-rule-v1", self))


@dataclass(frozen=True)
class SecAccountingTermEvidence:
    term: SecAccountingTerm
    row_ordinals: tuple[int, ...]
    rows: tuple[SecStatementRow, ...]


@dataclass(frozen=True)
class SecAccountingCheckResult:
    rule: SecAccountingRule
    status: SecAccountingStatus
    residual: Decimal | None
    tolerance: Decimal | None
    evidence: tuple[SecAccountingTermEvidence, ...]
    missing_reasons: tuple[tuple[int, str], ...]
    incomparable_reasons: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class SecObservedAccountingReport:
    production_identity: str
    output_observation_identity: str
    output_fact_version: str
    parser_version: str
    configuration_identity: str
    input_row_count: int
    detected_at: datetime
    recorded_at: datetime
    checks: tuple[SecAccountingCheckResult, ...]
    report_identity: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "report_identity", identity("sec-observed-accounting-report-v1", self)
        )

    @property
    def counts(self) -> tuple[tuple[SecAccountingStatus, int], ...]:
        return tuple(
            (status, sum(c.status is status for c in self.checks)) for status in SecAccountingStatus
        )

"""Distinct SEC terminal declarations and caller-attested external metric inputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from ._event_discovery_models import _utc
from ._metric_arithmetic import validate_decimal
from ._metric_common import (
    ACTUAL_METRICS,
    DURATION_KINDS,
    EXTERNAL_METRICS,
    currency,
    dates,
    identity,
    sha,
    text,
)


@dataclass(frozen=True)
class SecMetricTerminalDeclaration:
    local_id: str
    source_kind: str
    source_identity: str
    metric: str
    period_kind: str
    fiscal_year: int | None
    fiscal_end_quarter: int | None
    period_start: date
    period_end: date
    native_concept: str | None
    currency: str
    accounting_scope: str
    attribution_scope: str
    dimension: str | None
    comparability_cohort: str
    security_basis: str
    declaration_reference: str

    def __post_init__(self) -> None:
        for name in (
            "local_id",
            "accounting_scope",
            "attribution_scope",
            "comparability_cohort",
            "security_basis",
            "declaration_reference",
        ):
            text(getattr(self, name), name)
        sha(self.source_identity, "source_identity")
        currency(self.currency)
        dates(self.period_start, self.period_end)
        if self.dimension is not None:
            text(self.dimension, "dimension")
        if self.source_kind == "NORMALIZED_FACT":
            if self.metric not in ACTUAL_METRICS or self.period_kind not in DURATION_KINDS:
                raise ValueError(
                    "SEC metric terminals must declare actual financial duration facts"
                )
            text(self.native_concept, "native_concept")
            if type(self.fiscal_year) is not int or not 1 <= self.fiscal_year <= 9999:
                raise ValueError("invalid fiscal year")
            if type(self.fiscal_end_quarter) is not int or not 1 <= self.fiscal_end_quarter <= 4:
                raise ValueError("invalid fiscal end quarter")
            if self.period_kind == "FY" and self.fiscal_end_quarter != 4:
                raise ValueError("FY must end in fiscal quarter4")
        elif self.source_kind == "EXTERNAL_ATTESTATION":
            if (
                self.metric not in EXTERNAL_METRICS
                or self.native_concept is not None
                or self.dimension is not None
            ):
                raise ValueError("invalid external terminal declaration")
            expected = "FORECAST" if self.metric == "FORECAST_EPS" else "INSTANT"
            if (
                self.period_kind != expected
                or self.fiscal_year is not None
                or self.fiscal_end_quarter is not None
            ):
                raise ValueError("invalid external period declaration")
            if expected == "INSTANT" and self.period_start != self.period_end:
                raise ValueError("instant declaration requires equal dates")
        else:
            raise ValueError("invalid terminal source kind")


@dataclass(frozen=True)
class SecMetricExternalInput:
    metric: str
    value: Decimal
    unit: str
    currency: str
    valuation_at: datetime
    period_start: date
    period_end: date
    forecast_horizon: str | None
    accounting_scope: str
    attribution_scope: str
    security_basis: str
    company_total_equity: bool
    source_reference: str
    source_available_at: datetime
    observation_identity: str
    observed_at: datetime
    recorded_at: datetime
    quality_reference: str
    quality_recorded_at: datetime
    commit_identity: str | None
    committed_at: datetime | None
    external_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.metric not in EXTERNAL_METRICS:
            raise ValueError("external metric must be cap, price or forecast EPS")
        validate_decimal(self.value)
        currency(self.currency)
        dates(self.period_start, self.period_end)
        for name in (
            "unit",
            "accounting_scope",
            "attribution_scope",
            "security_basis",
            "source_reference",
            "quality_reference",
        ):
            text(getattr(self, name), name)
        sha(self.observation_identity, "observation_identity")
        for name in (
            "valuation_at",
            "source_available_at",
            "observed_at",
            "recorded_at",
            "quality_recorded_at",
        ):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if type(self.company_total_equity) is not bool or self.company_total_equity != (
            self.metric == "COMPANY_MARKET_CAP"
        ):
            raise ValueError("company market cap requires explicit total-equity attestation")
        if self.metric == "FORECAST_EPS":
            text(self.forecast_horizon, "forecast_horizon")
        elif (
            self.forecast_horizon is not None
            or self.period_start != self.period_end
            or self.period_end != self.valuation_at.date()
        ):
            raise ValueError("market cap and price require matching valuation instant dates")
        if (
            self.recorded_at < max(self.observed_at, self.source_available_at)
            or self.quality_recorded_at < self.source_available_at
        ):
            raise ValueError("external evidence has invalid causal times")
        if (self.commit_identity is None) != (self.committed_at is None):
            raise ValueError("external commit identity and time must be provided together")
        if self.commit_identity is not None:
            sha(self.commit_identity, "commit_identity")
            if self.committed_at is None:
                raise ValueError("missing external commit time")
            committed = _utc(self.committed_at, "committed_at")
            object.__setattr__(self, "committed_at", committed)
            if committed < max(self.recorded_at, self.quality_recorded_at):
                raise ValueError("external commit precedes production or quality evidence")
        object.__setattr__(
            self, "external_id", identity("sec-metric-external-attestation-v1", self)
        )

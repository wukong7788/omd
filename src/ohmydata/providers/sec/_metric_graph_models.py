"""Frozen graph definitions, declared basis metadata, and traced metric results."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from ._event_discovery_models import _utc
from ._metric_common import (
    DIVISION_RECIPES,
    VALUATION_RECIPES,
    SecMetricCapexSign,
    SecMetricDomainPolicy,
    SecMetricRecipe,
    SecMetricRounding,
    encoded,
    identity,
    text,
)
from ._metric_inputs import SecMetricExternalInput, SecMetricTerminalDeclaration
from .pit import SecPitMode, SecPitPolicy, SecPitResult


@dataclass(frozen=True)
class SecMetricStep:
    step_id: str
    recipe: SecMetricRecipe
    input_refs: tuple[str, ...]
    domain_policy: SecMetricDomainPolicy | None
    capex_sign: SecMetricCapexSign | None
    division_precision: int | None
    rounding: SecMetricRounding | None

    def __post_init__(self) -> None:
        text(self.step_id, "step_id")
        if type(self.recipe) is not SecMetricRecipe:
            raise TypeError("invalid named metric recipe")
        count = (
            4
            if self.recipe is SecMetricRecipe.FOUR_QUARTER_TTM_V1
            else 3
            if self.recipe is SecMetricRecipe.FY_YTD_TTM_V1
            else 2
        )
        if type(self.input_refs) is not tuple or len(self.input_refs) != count:
            raise ValueError("named metric recipe has wrong input arity")
        for ref in self.input_refs:
            text(ref, "input_ref")
        if len(set(self.input_refs)) != count:
            raise ValueError("duplicate metric input references")
        if self.recipe in DIVISION_RECIPES:
            if (
                type(self.domain_policy) is not SecMetricDomainPolicy
                or type(self.rounding) is not SecMetricRounding
            ):
                raise TypeError("division requires explicit domain and rounding policies")
            if type(self.division_precision) is not int or not 1 <= self.division_precision <= 100:
                raise ValueError("division precision must be integer1..100")
            if self.recipe is not SecMetricRecipe.YOY_V1 and self.domain_policy in {
                SecMetricDomainPolicy.RAISE_ZERO_ABS,
                SecMetricDomainPolicy.MISSING_ZERO_ABS,
            }:
                raise ValueError("absolute denominator policies apply only to YoY")
        elif (
            self.domain_policy is not None
            or self.division_precision is not None
            or self.rounding is not None
        ):
            raise ValueError("nondivision recipe cannot have division policies")
        if self.recipe is SecMetricRecipe.CFO_CAPEX_V1:
            if type(self.capex_sign) is not SecMetricCapexSign:
                raise TypeError("CFO-CapEx requires explicit CapEx sign policy")
        elif self.capex_sign is not None:
            raise ValueError("unexpected CapEx sign policy")


@dataclass(frozen=True)
class SecMetricGraphConfig:
    canonical_cik: str
    declarations: tuple[SecMetricTerminalDeclaration, ...]
    steps: tuple[SecMetricStep, ...]
    mode: SecPitMode
    knowledge_cutoff: datetime
    policy: SecPitPolicy
    valuation_at: datetime | None
    scope_reference: str
    configuration_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.canonical_cik) is not str
            or re.fullmatch(r"[1-9][0-9]{0,9}", self.canonical_cik) is None
        ):
            raise ValueError("graph CIK must be canonical ASCII digits")
        text(self.scope_reference, "scope_reference")
        if (
            type(self.declarations) is not tuple
            or not 1 <= len(self.declarations) <= 64
            or any(type(item) is not SecMetricTerminalDeclaration for item in self.declarations)
        ):
            raise ValueError("graph requires1..64 typed terminal declarations")
        if (
            type(self.steps) is not tuple
            or not 1 <= len(self.steps) <= 32
            or any(type(item) is not SecMetricStep for item in self.steps)
        ):
            raise ValueError("graph requires1..32 named formula steps")
        if type(self.mode) is not SecPitMode or type(self.policy) is not SecPitPolicy:
            raise TypeError("metric graph requires explicit SEC PIT mode and policy")
        object.__setattr__(
            self, "knowledge_cutoff", _utc(self.knowledge_cutoff, "knowledge_cutoff")
        )
        for name, value in vars(self.policy).items():
            if type(value) is str:
                text(value, f"policy.{name}")
        if self.valuation_at is not None:
            object.__setattr__(self, "valuation_at", _utc(self.valuation_at, "valuation_at"))
            if self.knowledge_cutoff > self.valuation_at:
                raise ValueError("knowledge cutoff exceeds valuation time")
            if any(
                item.source_kind == "NORMALIZED_FACT" and item.period_end > self.valuation_at.date()
                for item in self.declarations
            ):
                raise ValueError("actual financial period exceeds valuation date")
        if (
            any(step.recipe in VALUATION_RECIPES for step in self.steps)
            and self.valuation_at is None
        ):
            raise ValueError("valuation formula requires valuation_at")
        known = {item.local_id for item in self.declarations}
        if len(known) != len(self.declarations) or len(
            {item.source_identity for item in self.declarations}
        ) != len(self.declarations):
            raise ValueError("duplicate metric terminal ID or source identity")
        ancestors: dict[str, tuple[str, ...]] = {}
        ratios: set[str] = set()
        for step in self.steps:
            if step.step_id in known or any(ref not in known for ref in step.input_refs):
                raise ValueError("graph contains duplicate, forward or unknown references")
            if any(ref in ratios for ref in step.input_refs):
                raise ValueError("rounded ratio outputs cannot feed subsequent formulas")
            known.add(step.step_id)
            ancestors[step.step_id] = step.input_refs
            if step.recipe in DIVISION_RECIPES:
                ratios.add(step.step_id)
        used: set[str] = set()
        pending = [self.steps[-1].step_id]
        while pending:
            current = pending.pop()
            if current in used:
                continue
            used.add(current)
            pending.extend(ancestors.get(current, ()))
        if used != known:
            raise ValueError("graph contains unused terminal or step nodes")
        if len(encoded(self)) > 65_536:
            raise ValueError("metric graph configuration exceeds65536 bytes")
        object.__setattr__(
            self, "configuration_identity", identity("sec-metric-graph-config-v1", self)
        )


@dataclass(frozen=True)
class SecMetricMetadata:
    metric: str
    period_kind: str
    fiscal_year: int | None
    fiscal_end_quarter: int | None
    period_start: date
    period_end: date
    accounting_scope: str
    attribution_scope: str
    dimension: str | None
    comparability_cohort: str
    security_basis: str
    forecast_horizon: str | None = None
    valuation_at: datetime | None = None
    company_total_equity: bool = False


@dataclass(frozen=True)
class SecMetricValue:
    local_id: str
    configuration_identity: str
    value: Decimal | None
    unit: str
    currency: str
    metadata: SecMetricMetadata
    input_refs: tuple[str, ...]
    input_evidence_ids: tuple[str, ...]
    input_availability_bound: datetime
    missing_reason: str | None
    missing_origins: tuple[str, ...]
    division_error_bound: Decimal | None
    value_identity: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "value_identity", identity("sec-metric-value-v1", self))


@dataclass(frozen=True)
class SecMetricTerminalEvidence:
    declaration: SecMetricTerminalDeclaration
    result: SecMetricValue
    sec_evidence: SecPitResult | None
    external_attestation: SecMetricExternalInput | None


@dataclass(frozen=True)
class SecMetricGraphResult:
    config: SecMetricGraphConfig
    terminals: tuple[SecMetricTerminalEvidence, ...]
    steps: tuple[SecMetricValue, ...]
    result_identity: str

    @property
    def final(self) -> SecMetricValue:
        return self.steps[-1]

"""Fixed positional monetary formulas over already selected, declared inputs."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import cast

from ._metric_arithmetic import exact_sum, negate, rounded_ratio
from ._metric_common import SecMetricCapexSign, SecMetricDomainPolicy, SecMetricRecipe
from ._metric_graph_models import (
    SecMetricGraphConfig,
    SecMetricMetadata,
    SecMetricStep,
    SecMetricValue,
)
from .quarter_ttm import SecQuarterDeclaration, _quarter_sequence

_R = SecMetricRecipe


def _basis(values: tuple[SecMetricValue, ...], *, period: bool = False) -> None:
    first = values[0]
    for value in values[1:]:
        if (value.unit, value.currency) != (first.unit, first.currency):
            raise ValueError("metric units or currencies disagree")
        names = (
            "accounting_scope",
            "attribution_scope",
            "dimension",
            "comparability_cohort",
            "security_basis",
        )
        if period:
            names += (
                "period_kind",
                "fiscal_year",
                "fiscal_end_quarter",
                "period_start",
                "period_end",
            )
        if any(getattr(value.metadata, name) != getattr(first.metadata, name) for name in names):
            raise ValueError("metric accounting, period or security basis disagrees")


def _ttm(
    step: SecMetricStep, values: tuple[SecMetricValue, ...]
) -> tuple[Decimal, SecMetricMetadata]:
    _basis(values)
    metric = values[0].metadata.metric
    if metric not in {"REVENUE", "NET_INCOME"} or any(
        value.metadata.metric != metric for value in values
    ):
        raise ValueError("TTM requires homogeneous revenue or net income")
    if step.recipe is _R.FOUR_QUARTER_TTM_V1:
        quarters = []
        for value in values:
            meta = value.metadata
            if meta.period_kind != "INDEPENDENT_QUARTER":
                raise ValueError("four-quarter TTM requires independent quarter declarations")
            quarters.append(
                SecQuarterDeclaration(
                    value.value_identity,
                    cast(int, meta.fiscal_year),
                    cast(int, meta.fiscal_end_quarter),
                    True,
                    meta.period_start,
                    meta.period_end,
                    metric,
                    meta.accounting_scope,
                    meta.attribution_scope,
                    meta.comparability_cohort,
                    "metric-graph-quarter-declaration",
                )
            )
        _quarter_sequence(tuple(quarters))
        return exact_sum(tuple(cast(Decimal, value.value) for value in values)), replace(
            values[-1].metadata, period_kind="TTM", period_start=values[0].metadata.period_start
        )
    fy, current, prior = (value.metadata for value in values)
    if (
        fy.period_kind != "FY"
        or fy.fiscal_end_quarter != 4
        or current.period_kind != "YTD"
        or prior.period_kind != "YTD"
        or current.fiscal_end_quarter != prior.fiscal_end_quarter
        or current.fiscal_end_quarter not in (1, 2, 3)
        or current.fiscal_year != cast(int, fy.fiscal_year) + 1
        or prior.fiscal_year != fy.fiscal_year
        or prior.period_start != fy.period_start
        or prior.period_end >= fy.period_end
        or current.period_start != fy.period_end + timedelta(days=1)
    ):
        raise ValueError("invalid FY plus YTD bridge declarations")
    result = exact_sum(
        (
            cast(Decimal, values[0].value),
            cast(Decimal, values[1].value),
            negate(cast(Decimal, values[2].value)),
        )
    )
    return result, replace(
        current, period_kind="TTM", period_start=prior.period_end + timedelta(days=1)
    )


def _division_roles(
    config: SecMetricGraphConfig, step: SecMetricStep, values: tuple[SecMetricValue, ...]
) -> SecMetricMetadata:
    left, right = values
    a, b = left.metadata, right.metadata
    if step.recipe is _R.YOY_V1:
        _basis(values)
        if (
            a.metric != b.metric
            or a.metric
            not in {
                "REVENUE",
                "NET_INCOME",
                "OPERATING_INCOME",
                "GROSS_PROFIT",
                "CFO",
                "CAPEX",
                "FREE_CASH_FLOW",
            }
            or a.period_kind != b.period_kind
            or a.fiscal_end_quarter != b.fiscal_end_quarter
            or a.fiscal_year is None
            or b.fiscal_year is None
            or a.fiscal_year != b.fiscal_year + 1
            or b.period_end >= a.period_start
        ):
            raise ValueError("YoY requires nonoverlapping consecutive comparable fiscal periods")
        return replace(a, metric="YOY")
    if step.recipe is _R.MARGIN_V1:
        _basis(values, period=True)
        if (
            a.metric not in {"NET_INCOME", "OPERATING_INCOME", "GROSS_PROFIT", "FREE_CASH_FLOW"}
            or b.metric != "REVENUE"
        ):
            raise ValueError("margin requires a declared profit and revenue pair")
        return replace(a, metric="MARGIN")
    if config.valuation_at is None or a.valuation_at != config.valuation_at:
        raise ValueError("valuation input time disagrees with graph")
    if (
        left.currency != right.currency
        or a.security_basis != b.security_basis
        or a.attribution_scope != b.attribution_scope
    ):
        raise ValueError("valuation currency, equity or security basis disagrees")
    if cast(Decimal, left.value) < 0:
        raise ValueError("valuation price or market cap cannot be negative")
    if step.recipe in {_R.PE_V1, _R.PS_V1}:
        expected = "NET_INCOME" if step.recipe is _R.PE_V1 else "REVENUE"
        if (
            a.metric != "COMPANY_MARKET_CAP"
            or not a.company_total_equity
            or b.metric != expected
            or b.period_kind != "TTM"
            or not right.input_refs
        ):
            raise ValueError("valuation requires company-total equity and derived TTM denominator")
        if left.unit != left.currency or right.unit != right.currency:
            raise ValueError("PE/PS inputs require declared monetary currency units")
        return replace(
            b, metric="PE" if step.recipe is _R.PE_V1 else "PS", valuation_at=config.valuation_at
        )
    if (
        a.metric != "SECURITY_PRICE"
        or b.metric != "FORECAST_EPS"
        or b.period_kind != "FORECAST"
        or b.valuation_at != config.valuation_at
        or a.accounting_scope != b.accounting_scope
        or left.unit != right.unit
        or left.unit != left.currency + "/security"
        or b.period_end <= config.valuation_at.date()
        or not b.forecast_horizon
    ):
        raise ValueError("FPE requires matching explicit security units and future forecast basis")
    return replace(b, metric="FPE", valuation_at=config.valuation_at)


def evaluate_step(
    config: SecMetricGraphConfig, step: SecMetricStep, values: tuple[SecMetricValue, ...]
) -> SecMetricValue:
    if any(value.value is None for value in values):
        raise ValueError("domain-missing ratio cannot be reused as an input")
    numbers = tuple(cast(Decimal, value.value) for value in values)
    reason = None
    error_bound = None
    result: Decimal | None
    unit = values[0].unit
    if step.recipe in {_R.FOUR_QUARTER_TTM_V1, _R.FY_YTD_TTM_V1}:
        result, meta = _ttm(step, values)
    elif step.recipe is _R.CFO_CAPEX_V1:
        _basis(values, period=True)
        if values[0].metadata.metric != "CFO" or values[1].metadata.metric != "CAPEX":
            raise ValueError("CFO-CapEx requires declared CFO and CapEx roles")
        positive = step.capex_sign is SecMetricCapexSign.POSITIVE_OUTFLOW
        if positive and numbers[1] < 0 or not positive and numbers[1] > 0:
            raise ValueError("CapEx value disagrees with explicit outflow sign")
        result = exact_sum((numbers[0], negate(numbers[1]) if positive else numbers[1]))
        meta = replace(values[0].metadata, metric="FREE_CASH_FLOW")
    else:
        meta = _division_roles(config, step, values)
        denominator = numbers[1]
        absolute = step.domain_policy in {
            SecMetricDomainPolicy.RAISE_ZERO_ABS,
            SecMetricDomainPolicy.MISSING_ZERO_ABS,
        }
        invalid = denominator == 0 if absolute else denominator <= 0
        if invalid:
            reason = "ZERO_DENOMINATOR" if absolute else "NONPOSITIVE_DENOMINATOR"
            if step.domain_policy in {
                SecMetricDomainPolicy.RAISE_NONPOSITIVE,
                SecMetricDomainPolicy.RAISE_ZERO_ABS,
            }:
                raise ValueError(reason)
            result = None
        else:
            numerator = (
                exact_sum((numbers[0], negate(numbers[1])))
                if step.recipe is _R.YOY_V1
                else numbers[0]
            )
            denominator = denominator.copy_abs() if absolute else denominator
            if step.rounding is None or step.division_precision is None:
                raise ValueError("division policy missing")
            result, error_bound = rounded_ratio(
                numerator,
                denominator,
                precision=step.division_precision,
                rounding=step.rounding.value,
            )
        unit = "pure"
    return SecMetricValue(
        step.step_id,
        config.configuration_identity,
        result,
        unit,
        values[0].currency,
        meta,
        step.input_refs,
        tuple(sorted({item for value in values for item in value.input_evidence_ids})),
        max(value.input_availability_bound for value in values),
        reason,
        (step.step_id,) if reason else (),
        error_bound,
    )

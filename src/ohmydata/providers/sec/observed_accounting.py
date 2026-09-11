"""Bounded, offline accounting diagnostics for one observed SEC production."""

import re
from collections import defaultdict
from collections.abc import Iterable
from copy import copy
from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path

from ...core.errors import ResourceLimitError
from ._accounting_models import (
    SecAccountingApplicability,
    SecAccountingCheckResult,
    SecAccountingRule,
    SecAccountingStatus,
    SecAccountingTerm,
    SecAccountingTermEvidence,
    SecAccountingTolerance,
    SecObservedAccountingReport,
)
from ._event_discovery_models import _utc
from ._metric_arithmetic import validate_decimal
from ._metric_common import encoded
from ._observed_financial_bundle_codec import validate_existing_production
from ._structural_admission import _check_row
from .observed_xbrl_financials import SecObservedFinancialProduction
from .quarter_ttm import _decimal_sum

__all__ = [
    "SecAccountingApplicability",
    "SecAccountingCheckResult",
    "SecAccountingRule",
    "SecAccountingStatus",
    "SecAccountingTerm",
    "SecAccountingTermEvidence",
    "SecAccountingTolerance",
    "SecObservedAccountingReport",
    "evaluate_sec_observed_accounting",
]


def _bounded_production(production: SecObservedFinancialProduction, max_rows: int) -> None:
    if type(production) is not SecObservedFinancialProduction:
        raise TypeError("accounting input must be an observed production")
    rows = production.vintage.rows
    if type(rows) is not tuple or not 1 <= len(rows) <= max_rows:
        raise ResourceLimitError("accounting row budget exceeded")
    nodes, total = 0, 0

    def visit(value, depth=0):
        nonlocal nodes, total
        nodes += 1
        if nodes > 250000 or depth > 16:
            raise ResourceLimitError("accounting input structure budget exceeded")
        if isinstance(value, Enum) or value is None or type(value) in (bool, date, datetime):
            return
        if isinstance(value, Path):
            value = str(value)
        if type(value) is str:
            if len(value) > 4096 or len(value.encode()) > 4096:
                raise ResourceLimitError("accounting input text budget exceeded")
            total += len(value.encode())
        elif type(value) is Decimal:
            validate_decimal(value)
            total += len(value.as_tuple().digits) + 32
        elif type(value) is int:
            if value.bit_length() > 64:
                raise ResourceLimitError("accounting integer budget exceeded")
        elif type(value) is tuple:
            if len(value) > 10000:
                raise ResourceLimitError("accounting tuple budget exceeded")
            for item in value:
                visit(item, depth + 1)
        elif is_dataclass(value) and not isinstance(value, type):
            for f in fields(value):
                if f.name != "_capability":
                    visit(getattr(value, f.name), depth + 1)
        else:
            raise TypeError("unsupported accounting production field")
        if total > 8 * 1024**2:
            raise ResourceLimitError("accounting input byte budget exceeded")

    visit(production)
    for row in rows:
        _check_row(row)
    validate_existing_production(production)


def _precision_allowance(native: str | None) -> Decimal | None:
    if native == "INF":
        return Decimal(0)
    if type(native) is not str or re.fullmatch(r"-?(?:0|[1-9][0-9]{0,3})", native) is None:
        return None
    exponent = -int(native) - 1
    if abs(exponent) > 9999:
        return None
    return Decimal((0, (5,), exponent))


def _basis(row):
    return (
        row.value,
        row.unit,
        row.unit_ref,
        row.period_type,
        row.period_start,
        row.period_end,
        row.dimension,
        row.decimals_native,
        row.decimals,
        row.period_key,
        row.period_source,
        row.is_point_in_time,
        row.currency,
    )


def _period_valid(row) -> bool:
    if type(row.period_end) is not date:
        return False
    if row.period_type == "instant":
        return row.period_start is None
    return (
        row.period_type == "duration"
        and type(row.period_start) is date
        and row.period_start <= row.period_end
    )


def evaluate_sec_observed_accounting(
    production: SecObservedFinancialProduction,
    rules: Iterable[SecAccountingRule],
    *,
    detected_at: datetime,
    recorded_at: datetime,
    max_rules: int = 64,
    max_rows: int = 10000,
) -> SecObservedAccountingReport:
    """Report selected equalities; a MATCH never grants financial quality PASS."""
    for limit, maximum in ((max_rules, 64), (max_rows, 10000)):
        if type(limit) is not int or not 1 <= limit <= maximum:
            raise ValueError("invalid accounting resource cap")
    detected, recorded = _utc(detected_at, "detected_at"), _utc(recorded_at, "recorded_at")
    if recorded < detected:
        raise ValueError("accounting recording precedes detection")
    _bounded_production(production, max_rows)
    if detected < max(production.produced_at, production.output_observation.snapshot_fetched_at):
        raise ValueError("accounting detection precedes production")
    admitted = {}
    for count, rule in enumerate(rules, 1):
        if count > max_rules:
            raise ResourceLimitError("accounting rule budget exceeded")
        if type(rule) is not SecAccountingRule:
            raise TypeError("invalid accounting rule")
        if type(rule.terms) is not tuple or not 2 <= len(rule.terms) <= 8:
            raise ResourceLimitError("accounting term budget exceeded")
        rebuilt = replace(rule, terms=tuple(replace(t) for t in rule.terms))
        if rebuilt != rule:
            raise ValueError("accounting rule identity mismatch")
        prior = admitted.get(rule.rule_id)
        if prior is not None and prior != rebuilt:
            raise ValueError("conflicting accounting rule IDs")
        admitted[rule.rule_id] = rebuilt
    if not admitted:
        raise ValueError("accounting requires at least one rule")
    indexed = defaultdict(list)
    sizes = []
    for ordinal, row in enumerate(production.vintage.rows):
        indexed[(row.statement_type, row.concept, row.context_ref)].append((ordinal, row))
        sizes.append(len(encoded(row)))
    checks, evidence_bytes, evidence_ordinals = [], 0, 0
    for rule in sorted(admitted.values(), key=lambda r: r.rule_id):
        evidence, missing, incomparable, selected = [], [], [], []
        for ti, term in enumerate(rule.terms):
            matches = indexed.get(term.selector, ())
            evidence_ordinals += len(matches)
            evidence_bytes += sum(sizes[i] for i, _ in matches)
            if evidence_ordinals > 100000 or evidence_bytes > 4 * 1024**2:
                raise ResourceLimitError("accounting report evidence budget exceeded")
            evidence.append(
                SecAccountingTermEvidence(
                    term, tuple(i for i, _ in matches), tuple(copy(row) for _, row in matches)
                )
            )
            if not matches:
                missing.append((ti, "ROW_ABSENT"))
                continue
            if len({_basis(row) for _, row in matches}) != 1:
                incomparable.append((ti, "CONFLICTING_DUPLICATES"))
                continue
            row = matches[0][1]
            selected.append(row)
            if row.value is None:
                missing.append((ti, "VALUE_MISSING"))
            if row.unit != rule.expected_unit or not row.unit_ref or not row.unit_ref.strip():
                incomparable.append((ti, "UNIT_MISMATCH_OR_MISSING"))
            if not _period_valid(row):
                incomparable.append((ti, "PERIOD_INCOMPLETE"))
        if len(selected) == len(rule.terms):
            if len({row.dimension for row in selected}) != 1:
                incomparable.append((-1, "DIMENSION_MISMATCH"))
            if rule.applicability is SecAccountingApplicability.SAME_CONTEXT:
                if (
                    len(
                        {
                            (r.context_ref, r.period_type, r.period_start, r.period_end)
                            for r in selected
                        }
                    )
                    != 1
                ):
                    incomparable.append((-1, "CONTEXT_OR_PERIOD_MISMATCH"))
            elif all(_period_valid(row) for row in selected):
                end, begin, change = selected
                if (
                    (end.period_type, begin.period_type, change.period_type)
                    != ("instant", "instant", "duration")
                    or end.period_end != change.period_end
                    or begin.period_end == date.max
                    or begin.period_end + timedelta(days=1) != change.period_start
                ):
                    incomparable.append((-1, "CASH_BOUNDARY_MISMATCH"))
        tolerance = Decimal(0)
        if rule.tolerance_policy is SecAccountingTolerance.ASSUME_NEAREST_REPORTED_DECIMALS:
            allowances = []
            for ti, item in enumerate(evidence):
                if item.rows:
                    amount = _precision_allowance(item.rows[0].decimals_native)
                    if amount is None:
                        incomparable.append((ti, "PRECISION_MISSING_OR_INVALID"))
                    else:
                        allowances.append(amount)
            if not missing and not incomparable:
                tolerance = _decimal_sum(tuple(allowances))
        residual = None
        if not missing and not incomparable:
            values = tuple(
                r.value.copy_negate() if t.coefficient == -1 else r.value
                for t, r in zip(rule.terms, selected, strict=True)
            )
            residual = _decimal_sum(values)
        status = (
            SecAccountingStatus.MISSING
            if missing
            else SecAccountingStatus.INCOMPARABLE
            if incomparable
            else SecAccountingStatus.MATCH
            if residual is not None and residual.copy_abs() <= tolerance
            else SecAccountingStatus.MISMATCH
        )
        checks.append(
            SecAccountingCheckResult(
                rule,
                status,
                residual,
                None if missing or incomparable else tolerance,
                tuple(evidence),
                tuple(missing),
                tuple(incomparable),
            )
        )
    # Evidence and rule counts bound this allocation; enforce the exact final wire size.
    report = SecObservedAccountingReport(
        production_identity=production.production_identity,
        output_observation_identity=production.output_observation.observation_identity,
        output_fact_version=production.output_observation.fact_version,
        parser_version=production.parser_version,
        configuration_identity=production.configuration_identity,
        input_row_count=len(production.vintage.rows),
        detected_at=detected,
        recorded_at=recorded,
        checks=tuple(checks),
    )
    if len(encoded(report)) > 8 * 1024**2:
        raise ResourceLimitError("accounting report encoding budget exceeded")
    return report

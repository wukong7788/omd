"""Bounded accounting diagnostics for one sealed SEC document production."""

from collections.abc import Iterable
from copy import copy
from datetime import datetime

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
from ._structural_admission import _check_row
from .document_financial_replay import _MAX_BYTES, _admit
from .document_financials import SecDocumentFinancialProduction
from .observed_accounting import _evaluate_validated_accounting

__all__ = [
    "SecAccountingApplicability",
    "SecAccountingCheckResult",
    "SecAccountingRule",
    "SecAccountingStatus",
    "SecAccountingTerm",
    "SecAccountingTermEvidence",
    "SecAccountingTolerance",
    "SecObservedAccountingReport",
    "evaluate_sec_document_accounting",
]


def _bounded_document_production(production: SecDocumentFinancialProduction, max_rows: int) -> None:
    if type(production) is not SecDocumentFinancialProduction:
        raise TypeError("accounting input must be a document financial production")
    rows = production.vintage.rows
    if type(rows) is not tuple or not 1 <= len(rows) <= max_rows:
        raise ResourceLimitError("accounting row budget exceeded")
    _admit((production,), _MAX_BYTES)
    for row in rows:
        _check_row(row)
    checked = copy(production)
    checked.__post_init__()
    if checked.production_identity != production.production_identity:
        raise ValueError("accounting production identity mismatch")


def evaluate_sec_document_accounting(
    production: SecDocumentFinancialProduction,
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
    _bounded_document_production(production, max_rows)
    if detected < max(production.produced_at, production.output_observation.snapshot_fetched_at):
        raise ValueError("accounting detection precedes production")
    return _evaluate_validated_accounting(
        production, rules, detected=detected, recorded=recorded, max_rules=max_rules
    )

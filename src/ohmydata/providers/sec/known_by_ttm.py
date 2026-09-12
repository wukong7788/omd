"""Public offline FY+YTD diagnostic for sealed observed and document financial productions."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import islice
from typing import cast

from ohmydata.core.errors import ResourceLimitError

from ._event_discovery_models import _utc
from ._metric_arithmetic import validate_decimal
from ._metric_common import currency, dates, encoded, identity, sha, text
from .document_accounting import _bounded_document_production
from .document_financials import SecDocumentFinancialProduction
from .financials import SecStatementRow
from .observed_accounting import _bounded_production
from .observed_xbrl_financials import SecObservedFinancialProduction
from .quarter_ttm import _decimal_sum

_CIK_RE = re.compile(r"[1-9][0-9]{0,9}")
_ALLOWED_METRICS = frozenset({"REVENUE", "NET_INCOME"})
_PERIOD_ROLES = ("PRIOR_FY", "CURRENT_YTD", "PRIOR_YTD")


@dataclass(frozen=True)
class SecKnownByTtmInput:
    """Ordered input declaration for prior FY, current YTD, or prior YTD."""

    production_identity: str
    statement_type: str
    concept: str
    context_ref: str
    fiscal_year: int
    fiscal_end_quarter: int
    period_start: date
    period_end: date
    declaration_reference: str

    def __post_init__(self) -> None:
        sha(self.production_identity, "production_identity")
        text(self.statement_type, "statement_type")
        text(self.concept, "concept")
        text(self.context_ref, "context_ref")
        text(self.declaration_reference, "declaration_reference")
        if (
            type(self.fiscal_year) is not int
            or type(self.fiscal_year) is bool
            or not 1 <= self.fiscal_year <= 9999
        ):
            raise ValueError("fiscal_year must be integer 1..9999")
        if (
            type(self.fiscal_end_quarter) is not int
            or type(self.fiscal_end_quarter) is bool
            or not 1 <= self.fiscal_end_quarter <= 4
        ):
            raise ValueError("fiscal_end_quarter must be integer 1..4")
        if type(self.period_start) is not date or type(self.period_start) is datetime:
            raise ValueError("period_start must be date, not datetime")
        if type(self.period_end) is not date or type(self.period_end) is datetime:
            raise ValueError("period_end must be date, not datetime")
        dates(self.period_start, self.period_end)


@dataclass(frozen=True)
class SecKnownByTtmEvidence:
    """Selected row and provenance evidence for one TTM input term."""

    declaration: SecKnownByTtmInput
    period_role: str
    production_identity: str
    output_observation_identity: str
    content_hash: str
    known_by_at: datetime
    produced_at: datetime
    snapshot_fetched_at: datetime
    row: SecStatementRow

    def __post_init__(self) -> None:
        if type(self.declaration) is not SecKnownByTtmInput:
            raise TypeError("declaration must be SecKnownByTtmInput")
        if self.period_role not in _PERIOD_ROLES:
            raise ValueError("invalid period_role")
        sha(self.production_identity, "production_identity")
        sha(self.output_observation_identity, "output_observation_identity")
        sha(self.content_hash, "content_hash")
        object.__setattr__(self, "known_by_at", _utc(self.known_by_at, "known_by_at"))
        object.__setattr__(self, "produced_at", _utc(self.produced_at, "produced_at"))
        object.__setattr__(
            self, "snapshot_fetched_at", _utc(self.snapshot_fetched_at, "snapshot_fetched_at")
        )
        if type(self.row) is not SecStatementRow:
            raise TypeError("row must be SecStatementRow")


@dataclass(frozen=True)
class SecKnownByTtmResult:
    """Diagnostic TTM result derived from sealed observed/document productions."""

    canonical_cik: str
    metric: str
    accounting_scope: str
    attribution_scope: str
    comparability_cohort: str
    security_basis: str
    scope_reference: str
    declarations: tuple[SecKnownByTtmInput, ...]
    evidences: tuple[SecKnownByTtmEvidence, ...]
    period_start: date
    period_end: date
    value: Decimal
    unit: str
    currency: str
    input_known_by_bound: datetime
    knowledge_cutoff: datetime
    diagnosed_at: datetime
    result_identity: str


def diagnose_sec_fy_ytd_ttm(
    *,
    productions: Iterable[SecObservedFinancialProduction | SecDocumentFinancialProduction],
    declarations: tuple[SecKnownByTtmInput, ...],
    canonical_cik: str,
    metric: str,
    accounting_scope: str,
    attribution_scope: str,
    comparability_cohort: str,
    security_basis: str,
    scope_reference: str,
    knowledge_cutoff: datetime,
    diagnosed_at: datetime,
    max_rows: int = 10000,
) -> SecKnownByTtmResult:
    """Diagnose offline FY+YTD TTM from sealed observed/document productions."""
    if type(metric) is not str or metric not in _ALLOWED_METRICS:
        raise ValueError("metric must be REVENUE or NET_INCOME")
    if type(canonical_cik) is not str or _CIK_RE.fullmatch(canonical_cik) is None:
        raise ValueError("canonical_cik must be nonzero unpadded ASCII digits, at most 10")
    if type(declarations) is not tuple or len(declarations) != 3:
        raise ValueError("declarations must be a tuple of exactly 3 SecKnownByTtmInput objects")

    checked_declarations: list[SecKnownByTtmInput] = []
    for dec in declarations:
        if type(dec) is not SecKnownByTtmInput:
            raise TypeError("declarations must contain SecKnownByTtmInput objects")
        rebuilt = replace(dec)
        rebuilt.__post_init__()
        checked_declarations.append(rebuilt)
    declarations = tuple(checked_declarations)

    text(accounting_scope, "accounting_scope")
    text(attribution_scope, "attribution_scope")
    text(comparability_cohort, "comparability_cohort")
    text(security_basis, "security_basis")
    text(scope_reference, "scope_reference")

    cutoff = _utc(knowledge_cutoff, "knowledge_cutoff")
    diag_at = _utc(diagnosed_at, "diagnosed_at")
    if diag_at < cutoff:
        raise ValueError("diagnosed_at cannot precede knowledge_cutoff")

    if type(max_rows) is not int or type(max_rows) is bool or not 1 <= max_rows <= 10000:
        raise ValueError("max_rows must be integer 1..10000")

    # Bound canonical configuration before enumerating productions or hashing payload.
    config_payload = {
        "canonical_cik": canonical_cik,
        "metric": metric,
        "accounting_scope": accounting_scope,
        "attribution_scope": attribution_scope,
        "comparability_cohort": comparability_cohort,
        "security_basis": security_basis,
        "scope_reference": scope_reference,
        "declarations": declarations,
        "knowledge_cutoff": cutoff,
        "diagnosed_at": diag_at,
    }
    if len(encoded(config_payload)) > 65_536:
        raise ValueError("canonical configuration exceeds 65536 bytes")

    # Read and bound supplied productions (<=3 unique objects + 1 sentinel = 4).
    prod_items = tuple(islice(productions, 4))
    if len(prod_items) > 3:
        raise ResourceLimitError("production count budget exceeded")
    if not prod_items:
        raise ValueError("productions cannot be empty")

    seen_ids: set[int] = set()
    prod_by_identity: dict[
        str, SecObservedFinancialProduction | SecDocumentFinancialProduction
    ] = {}
    for p in prod_items:
        if id(p) in seen_ids:
            raise ValueError("duplicate production object")
        seen_ids.add(id(p))
        if (
            type(p) is not SecObservedFinancialProduction
            and type(p) is not SecDocumentFinancialProduction
        ):
            raise TypeError(
                "production must be SecObservedFinancialProduction or SecDocumentFinancialProduction"
            )
        if p.production_identity in prod_by_identity:
            raise ValueError("duplicate production identity")
        prod_by_identity[p.production_identity] = p
        if type(p.vintage.rows) is not tuple:
            raise TypeError("production rows must be a tuple")

    declared_identities = {dec.production_identity for dec in declarations}
    if set(prod_by_identity.keys()) != declared_identities:
        raise ValueError("productions do not exactly match declared production identities")

    # Validate aggregate row count and sealed structure.
    aggregate_rows = sum(len(p.vintage.rows) for p in prod_items)
    if aggregate_rows > max_rows:
        raise ResourceLimitError("aggregate row budget exceeded")

    bounds: list[datetime] = []
    for p in prod_items:
        if type(p) is SecObservedFinancialProduction:
            _bounded_production(p, max_rows)
        else:
            _bounded_document_production(cast(SecDocumentFinancialProduction, p), max_rows)

        if p.request.cik.lstrip("0") != canonical_cik:
            raise ValueError("production CIK does not match canonical_cik")

        p_bound = max(
            p.vintage.known_by_at, p.produced_at, p.output_observation.snapshot_fetched_at
        )
        if p_bound > cutoff:
            raise ValueError("production availability bound exceeds knowledge_cutoff")
        bounds.append(p_bound)

    input_known_by_bound = max(bounds)

    # Fiscal bridge rules.
    prior_fy, current_ytd, prior_ytd = declarations
    if (
        prior_fy.fiscal_end_quarter != 4
        or current_ytd.fiscal_end_quarter != prior_ytd.fiscal_end_quarter
        or current_ytd.fiscal_end_quarter not in (1, 2, 3)
        or current_ytd.fiscal_year != prior_fy.fiscal_year + 1
        or prior_ytd.fiscal_year != prior_fy.fiscal_year
        or prior_ytd.period_start != prior_fy.period_start
        or prior_ytd.period_end >= prior_fy.period_end
        or current_ytd.period_start != prior_fy.period_end + timedelta(days=1)
    ):
        raise ValueError("invalid FY plus YTD bridge declarations")

    # Match each declaration to exactly one row.
    evidences: list[SecKnownByTtmEvidence] = []
    selected_rows: list[SecStatementRow] = []
    for dec, role in zip(declarations, _PERIOD_ROLES, strict=True):
        p = prod_by_identity[dec.production_identity]
        matches = [
            row
            for row in p.vintage.rows
            if row.statement_type == dec.statement_type
            and row.concept == dec.concept
            and row.context_ref == dec.context_ref
            and row.period_start == dec.period_start
            and row.period_end == dec.period_end
        ]
        if len(matches) != 1:
            raise ValueError(
                f"declaration {dec.declaration_reference} matched {len(matches)} rows, expected exactly 1"
            )
        row = matches[0]
        if row.dimension is not None:
            raise ValueError("dimensions are rejected in consolidated slice")
        if row.period_type != "duration" or row.period_start is None or row.period_end is None:
            raise ValueError("matched row must be duration period with non-null start and end")
        if row.value is None:
            raise ValueError("matched row value must not be None")
        validate_decimal(row.value)
        if row.unit is None or row.currency is None:
            raise ValueError("matched row unit and currency must not be None")
        currency(row.currency)
        if not row.unit.startswith("iso4217:"):
            raise ValueError("matched row unit must be iso4217 currency")
        if row.unit != f"iso4217:{row.currency}":
            raise ValueError("matched row unit must match currency")

        selected_rows.append(row)
        evidences.append(
            SecKnownByTtmEvidence(
                declaration=dec,
                period_role=role,
                production_identity=p.production_identity,
                output_observation_identity=p.output_observation.observation_identity,
                content_hash=p.output_observation.response_sha256,
                known_by_at=p.vintage.known_by_at,
                produced_at=p.produced_at,
                snapshot_fetched_at=p.output_observation.snapshot_fetched_at,
                row=row,
            )
        )

    # Validate unit and currency agreement.
    unit, cur = selected_rows[0].unit, selected_rows[0].currency
    assert unit is not None and cur is not None
    for r in selected_rows[1:]:
        if (r.unit, r.currency) != (unit, cur):
            raise ValueError("all 3 selected rows must have identical unit and currency")

    # Compute TTM: prior_fy + current_ytd - prior_ytd.
    fy_val = validate_decimal(selected_rows[0].value)
    cur_val = validate_decimal(selected_rows[1].value)
    pri_val = validate_decimal(selected_rows[2].value)
    ttm_val = _decimal_sum((fy_val, cur_val, pri_val.copy_negate()))

    result_period_start = declarations[2].period_end + timedelta(days=1)
    result_period_end = declarations[1].period_end

    diagnostic_payload = {
        "canonical_cik": canonical_cik,
        "metric": metric,
        "accounting_scope": accounting_scope,
        "attribution_scope": attribution_scope,
        "comparability_cohort": comparability_cohort,
        "security_basis": security_basis,
        "scope_reference": scope_reference,
        "declarations": declarations,
        "evidences": tuple(evidences),
        "period_start": result_period_start,
        "period_end": result_period_end,
        "value": ttm_val,
        "unit": unit,
        "currency": cur,
        "input_known_by_bound": input_known_by_bound,
        "knowledge_cutoff": cutoff,
        "diagnosed_at": diag_at,
    }
    if len(encoded(diagnostic_payload)) > 65_536:
        raise ValueError("canonical diagnostic payload exceeds 65536 bytes")

    result_identity = identity("sec-known-by-ttm-diagnostic-v1", diagnostic_payload)

    return SecKnownByTtmResult(
        canonical_cik=canonical_cik,
        metric=metric,
        accounting_scope=accounting_scope,
        attribution_scope=attribution_scope,
        comparability_cohort=comparability_cohort,
        security_basis=security_basis,
        scope_reference=scope_reference,
        declarations=declarations,
        evidences=tuple(evidences),
        period_start=result_period_start,
        period_end=result_period_end,
        value=ttm_val,
        unit=unit,
        currency=cur,
        input_known_by_bound=input_known_by_bound,
        knowledge_cutoff=cutoff,
        diagnosed_at=diag_at,
        result_identity=result_identity,
    )


__all__ = [
    "SecKnownByTtmEvidence",
    "SecKnownByTtmInput",
    "SecKnownByTtmResult",
    "diagnose_sec_fy_ytd_ttm",
]

"""SEC-only quarterly fact projection from declared filing evidence.

Caller-supplied vintages and fiscal-period labels require retained source identities.
This projection does not authenticate arbitrary inputs or claim first publication.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from itertools import islice

from ._metric_arithmetic import exact_sum, validate_decimal
from ._quarterly_eligibility import (
    SecCompanyEligibilityStatus,
    SecQuarterlyEligibility,
    classify_sec_company_eligibility_from_root,
    fetch_sec_company_eligibility,
)
from ._quarterly_periods import (
    _ACCESSION,
    _IDENTITY,
    SecQuarterlyFilingPeriodEvidence,
    SecQuarterlyNativeDurationEvidence,
    SecQuarterlyPeriodEvidence,
    SecQuarterlyPeriodResolution,
    SecQuarterPeriodKind,
    SecQuarterPeriodResolutionStatus,
    SecQuarterPeriodSourceKind,
    derive_sec_quarterly_period_evidence,
)
from .financials import SecCompanyFinancialVintage, SecStatementRow

_MAX_VINTAGES = 1_000
_MAX_CONCEPTS = 256
_MAX_ROWS = 100_000


class SecQuarterFieldStatus(str, Enum):
    PRESENT = "PRESENT"
    MISSING = "MISSING"
    AMBIGUOUS = "AMBIGUOUS"


class SecQuarterlyProjectionStatus(str, Enum):
    NO_SEC_COVERAGE = "NO_SEC_COVERAGE"
    SEC_COMPANY = "SEC_COMPANY"
    SEC_COMPANY_MISSING_FACT = "SEC_COMPANY_MISSING_FACT"
    SEC_COMPANY_PARTIAL = "SEC_COMPANY_PARTIAL"


@dataclass(frozen=True)
class SecQuarterlyVintageInput:
    vintage: SecCompanyFinancialVintage
    source_observation_id: str
    period: SecQuarterlyPeriodEvidence

    def __post_init__(self) -> None:
        if type(self.vintage) is not SecCompanyFinancialVintage:
            raise TypeError("vintage must be SecCompanyFinancialVintage")
        if type(self.source_observation_id) is not str or not _IDENTITY.fullmatch(
            self.source_observation_id
        ):
            raise ValueError("source_observation_id must be a lowercase SHA-256 identity")
        if self.period.accession_number != self.vintage.accession_number:
            raise ValueError("period evidence accession does not match vintage")
        if self.vintage.accepted_at is None:
            raise ValueError("accepted_at is required for filing evidence")
        if (
            self.vintage.period_end is not None
            and self.period.period_end != self.vintage.period_end
        ):
            raise ValueError("period evidence end does not match vintage period_end")
        if (
            self.vintage.fiscal_year is not None
            and self.period.fiscal_year != self.vintage.fiscal_year
        ):
            raise ValueError("period evidence conflicts with vintage fiscal_year")
        declared_period = (self.vintage.fiscal_period or "").upper()
        if declared_period in {"Q1", "Q2", "Q3", "Q4", "FY"}:
            expected = (
                "Q4"
                if self.period.period_kind is SecQuarterPeriodKind.FY
                else f"Q{self.period.fiscal_quarter}"
            )
            if declared_period not in {expected, "FY" if expected == "Q4" else expected}:
                raise ValueError("period evidence conflicts with vintage fiscal_period")
        form = self.vintage.form.upper()
        expected_form = "10-K" if self.period.fiscal_quarter == 4 else "10-Q"
        if form not in {expected_form, expected_form + "/A"}:
            raise ValueError("period kind does not match SEC filing form")

    @property
    def evidence_identity(self) -> str:
        period = self.period
        payload = {
            "schema": "sec-quarterly-vintage-evidence-v1",
            "vintage_identity": self.vintage.vintage_identity,
            "source_observation_id": self.source_observation_id,
            "accession_number": period.accession_number,
            "fiscal_year": period.fiscal_year,
            "fiscal_quarter": period.fiscal_quarter,
            "period_kind": period.period_kind.value,
            "period_start": period.period_start.isoformat() if period.period_start else None,
            "period_end": period.period_end.isoformat(),
            "source_kind": period.source_kind.value,
            "source_label": period.source_label,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class SecQuarterlyFactObservation:
    fiscal_year: int
    fiscal_quarter: int
    concept: str
    statement_type: str
    value: Decimal
    unit: str
    accession_numbers: tuple[str, ...]
    accepted_at: tuple[datetime, ...]
    source_observation_ids: tuple[str, ...]
    fact_references: tuple[SecQuarterlyFactReference, ...]
    operation: str

    @property
    def latest_accepted_at(self) -> datetime:
        """Latest source filing acceptance time; not a first-publication guarantee."""
        return max(self.accepted_at)


@dataclass(frozen=True)
class SecQuarterlyFactReference:
    accession_number: str
    source_observation_id: str
    vintage_identity: str
    evidence_identity: str
    row_ordinal: int
    context_ref: str | None
    concept: str
    standard_concept: str
    period_type: str | None
    period_start: date | None
    period_end: date | None
    unit: str | None
    unit_ref: str | None
    dimension: str | None
    value: Decimal
    fact_identity: str


def _fact_reference(
    item: SecQuarterlyVintageInput, row: SecStatementRow
) -> SecQuarterlyFactReference:
    ordinal = next(index for index, candidate in enumerate(item.vintage.rows) if candidate is row)
    payload = {
        "schema": "sec-quarterly-native-fact-ref-v1",
        "accession_number": item.vintage.accession_number,
        "source_observation_id": item.source_observation_id,
        "vintage_identity": item.vintage.vintage_identity,
        "evidence_identity": item.evidence_identity,
        "row_ordinal": ordinal,
        "context_ref": row.context_ref,
        "concept": row.concept,
        "standard_concept": row.standard_concept,
        "period_type": row.period_type,
        "period_start": row.period_start.isoformat() if row.period_start else None,
        "period_end": row.period_end.isoformat() if row.period_end else None,
        "unit": row.unit,
        "unit_ref": row.unit_ref,
        "dimension": row.dimension,
        "value": str(row.value),
    }
    identity = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return SecQuarterlyFactReference(
        item.vintage.accession_number,
        item.source_observation_id,
        item.vintage.vintage_identity,
        item.evidence_identity,
        ordinal,
        row.context_ref,
        row.concept,
        row.standard_concept,
        row.period_type,
        row.period_start,
        row.period_end,
        row.unit,
        row.unit_ref,
        row.dimension,
        validate_decimal(row.value),
        identity,
    )


@dataclass(frozen=True)
class SecQuarterlyField:
    fiscal_year: int
    fiscal_quarter: int
    concept: str
    status: SecQuarterFieldStatus
    observations: tuple[SecQuarterlyFactObservation, ...]


@dataclass(frozen=True)
class SecQuarterlyProjection:
    status: SecQuarterlyProjectionStatus
    periods: tuple[tuple[int, int], ...]
    fields: tuple[SecQuarterlyField, ...]

    @property
    def latest_four(self) -> tuple[tuple[int, int], ...]:
        return self.periods[-4:]

    @property
    def prior_year_comparable(self) -> tuple[tuple[int, int], ...]:
        requested = set(self.periods)
        return tuple(
            (year - 1, quarter)
            for year, quarter in self.latest_four
            if (year - 1, quarter) in requested
        )


def _row_for(
    item: SecQuarterlyVintageInput,
    concept: str,
    *,
    kind: SecQuarterPeriodKind,
) -> tuple[SecStatementRow, ...]:
    result = []
    for row in item.vintage.rows:
        if row.period_source != "xbrl-context" or not row.context_ref:
            continue
        if row.concept != concept and row.standard_concept != concept:
            continue
        if row.value is None or row.unit is None or row.dimension is not None:
            continue
        if kind is SecQuarterPeriodKind.INSTANT:
            if (
                row.statement_type != "balance_sheet"
                or row.period_type != "instant"
                or row.period_end != item.period.period_end
            ):
                continue
        else:
            if (
                row.period_type != "duration"
                or row.period_start != item.period.period_start
                or row.period_end != item.period.period_end
                or row.period_source != "xbrl-context"
            ):
                continue
        result.append(row)
    return tuple(result)


def _observation(
    fiscal_year: int,
    fiscal_quarter: int,
    concept: str,
    rows: tuple[tuple[SecQuarterlyVintageInput, SecStatementRow], ...],
    value: Decimal,
    unit: str,
    operation: str,
) -> SecQuarterlyFactObservation:
    return SecQuarterlyFactObservation(
        fiscal_year,
        fiscal_quarter,
        concept,
        rows[0][1].statement_type,
        validate_decimal(value),
        unit,
        tuple(item.vintage.accession_number for item, _ in rows),
        tuple(item.vintage.accepted_at for item, _ in rows if item.vintage.accepted_at is not None),
        tuple(item.source_observation_id for item, _ in rows),
        tuple(_fact_reference(item, row) for item, row in rows),
        operation,
    )


def project_sec_quarterly_facts(
    vintages: Iterable[SecQuarterlyVintageInput],
    *,
    periods: tuple[tuple[int, int], ...],
    concepts: tuple[str, ...],
    additive_usd_flow_concepts: tuple[str, ...] = (),
    compatible_amendment_pairs: tuple[tuple[str, str], ...] = (),
    acceptance_upper: datetime | None = None,
) -> SecQuarterlyProjection:
    """Project only declared SEC filing facts; missing values remain typed missing.

    All amendment vintages are retained. If more than one source alternative exists,
    the field is AMBIGUOUS and the caller must select explicitly. Q4 flow arithmetic
    is limited to caller-designated additive concepts with matching USD XBRL facts.
    """
    if type(periods) is not tuple or not periods or len(periods) > 8:
        raise ValueError("periods must contain one to eight comparable quarters")
    if any(
        type(year) is not int or not 1 <= year <= 9999 or type(q) is not int or q not in range(1, 5)
        for year, q in periods
    ):
        raise ValueError("invalid fiscal quarter request")
    if tuple(sorted(set(periods))) != periods:
        raise ValueError("periods must be unique and ascending")
    if type(concepts) is not tuple or not concepts or len(concepts) > _MAX_CONCEPTS:
        raise ValueError("concepts must contain 1..256 entries")
    if len(set(concepts)) != len(concepts) or any(
        type(c) is not str or not c.strip() for c in concepts
    ):
        raise ValueError("invalid or duplicate concepts")
    if type(additive_usd_flow_concepts) is not tuple or not set(additive_usd_flow_concepts) <= set(
        concepts
    ):
        raise ValueError("additive concepts must also be requested concepts")
    if type(compatible_amendment_pairs) is not tuple or any(
        type(pair) is not tuple
        or len(pair) != 2
        or any(
            type(accession) is not str or not _ACCESSION.fullmatch(accession) for accession in pair
        )
        for pair in compatible_amendment_pairs
    ):
        raise ValueError("compatible_amendment_pairs must contain accession pairs")
    inputs = tuple(islice(iter(vintages), _MAX_VINTAGES + 1))
    if len(inputs) > _MAX_VINTAGES or any(
        type(item) is not SecQuarterlyVintageInput for item in inputs
    ):
        raise ValueError("invalid quarterly vintage inputs")
    if sum(len(item.vintage.rows) for item in inputs) > _MAX_ROWS:
        raise ValueError("quarterly vintage row limit exceeded")
    cik_identities: set[str] = set()
    for item in inputs:
        raw_cik = item.vintage.cik
        if (
            type(raw_cik) is not str
            or not raw_cik.isascii()
            or not raw_cik.isdecimal()
            or not 1 <= int(raw_cik) <= 9_999_999_999
        ):
            raise ValueError("quarterly vintage CIK is invalid")
        cik_identities.add(str(int(raw_cik)))
    if len(cik_identities) > 1:
        raise ValueError("quarterly vintages must belong to one SEC CIK")
    if acceptance_upper is not None and (
        type(acceptance_upper) is not datetime or acceptance_upper.tzinfo is None
    ):
        raise ValueError("acceptance_upper must be timezone-aware")
    if acceptance_upper is not None:
        inputs = tuple(
            item
            for item in inputs
            if item.vintage.accepted_at is not None and item.vintage.accepted_at <= acceptance_upper
        )
    fields: list[SecQuarterlyField] = []
    for year, quarter in periods:
        for concept in concepts:
            observations: list[SecQuarterlyFactObservation] = []
            if quarter < 4:
                for item in inputs:
                    if (item.period.fiscal_year, item.period.fiscal_quarter) != (year, quarter):
                        continue
                    if item.period.period_kind is SecQuarterPeriodKind.INSTANT:
                        kind = SecQuarterPeriodKind.INSTANT
                    elif item.period.period_kind is SecQuarterPeriodKind.INDEPENDENT_3M:
                        kind = SecQuarterPeriodKind.INDEPENDENT_3M
                    else:
                        continue
                    for row in _row_for(item, concept, kind=kind):
                        value = validate_decimal(row.value)
                        observations.append(
                            _observation(
                                year,
                                quarter,
                                concept,
                                ((item, row),),
                                value,
                                row.unit or "",
                                "SOURCE",
                            )
                        )
            else:
                for fy in inputs:
                    if (
                        fy.period.fiscal_year != year
                        or fy.period.fiscal_quarter != 4
                        or fy.period.period_kind
                        not in {SecQuarterPeriodKind.FY, SecQuarterPeriodKind.INSTANT}
                    ):
                        continue
                    for row in _row_for(fy, concept, kind=SecQuarterPeriodKind.INSTANT):
                        value = validate_decimal(row.value)
                        observations.append(
                            _observation(
                                year,
                                quarter,
                                concept,
                                ((fy, row),),
                                value,
                                row.unit or "",
                                "SOURCE",
                            )
                        )
                    if (
                        fy.period.period_kind is not SecQuarterPeriodKind.FY
                        or concept not in additive_usd_flow_concepts
                    ):
                        continue
                    for ytd in inputs:
                        if (
                            ytd.period.fiscal_year != year
                            or ytd.period.fiscal_quarter != 3
                            or ytd.period.period_kind is not SecQuarterPeriodKind.YTD_9M
                        ):
                            continue
                        if (
                            fy.period.period_start is None
                            or fy.period.period_start != ytd.period.period_start
                        ):
                            continue
                        if ytd.period.period_end >= fy.period.period_end:
                            continue
                        if not 60 <= (fy.period.period_end - ytd.period.period_end).days <= 110:
                            continue
                        fy_rows = _row_for(fy, concept, kind=SecQuarterPeriodKind.FY)
                        ytd_rows = _row_for(ytd, concept, kind=SecQuarterPeriodKind.YTD_9M)
                        for annual in fy_rows:
                            for interim in ytd_rows:
                                amended = fy.vintage.form.endswith(
                                    "/A"
                                ) or ytd.vintage.form.endswith("/A")
                                if (
                                    amended
                                    and (
                                        fy.vintage.accession_number,
                                        ytd.vintage.accession_number,
                                    )
                                    not in compatible_amendment_pairs
                                ):
                                    continue
                                if (
                                    annual.statement_type != interim.statement_type
                                    or annual.concept != interim.concept
                                    or annual.unit != interim.unit
                                    or annual.unit not in {"USD", "iso4217:USD"}
                                    or annual.dimension != interim.dimension
                                    or annual.period_start != interim.period_start
                                    or annual.period_end != fy.period.period_end
                                    or interim.period_end != ytd.period.period_end
                                ):
                                    continue
                                annual_value = validate_decimal(annual.value)
                                ytd_value = validate_decimal(interim.value)
                                value = exact_sum((annual_value, ytd_value.copy_negate()))
                                observations.append(
                                    _observation(
                                        year,
                                        quarter,
                                        concept,
                                        ((fy, annual), (ytd, interim)),
                                        value,
                                        annual.unit or "",
                                        "FY_MINUS_Q3_YTD",
                                    )
                                )
            status = (
                SecQuarterFieldStatus.MISSING
                if not observations
                else SecQuarterFieldStatus.PRESENT
                if len(observations) == 1
                else SecQuarterFieldStatus.AMBIGUOUS
            )
            fields.append(SecQuarterlyField(year, quarter, concept, status, tuple(observations)))
    has_missing = any(field.status is SecQuarterFieldStatus.MISSING for field in fields)
    has_present = any(field.status is not SecQuarterFieldStatus.MISSING for field in fields)
    status = (
        SecQuarterlyProjectionStatus.NO_SEC_COVERAGE
        if not inputs
        else SecQuarterlyProjectionStatus.SEC_COMPANY_MISSING_FACT
        if not has_present
        else SecQuarterlyProjectionStatus.SEC_COMPANY_PARTIAL
        if has_missing
        else SecQuarterlyProjectionStatus.SEC_COMPANY
    )
    return SecQuarterlyProjection(status, periods, tuple(fields))


__all__ = [
    "SecCompanyEligibilityStatus",
    "SecQuarterFieldStatus",
    "SecQuarterPeriodKind",
    "SecQuarterPeriodResolutionStatus",
    "SecQuarterPeriodSourceKind",
    "SecQuarterlyEligibility",
    "SecQuarterlyFactObservation",
    "SecQuarterlyField",
    "SecQuarterlyFilingPeriodEvidence",
    "SecQuarterlyNativeDurationEvidence",
    "SecQuarterlyPeriodEvidence",
    "SecQuarterlyPeriodResolution",
    "SecQuarterlyProjection",
    "SecQuarterlyProjectionStatus",
    "SecQuarterlyVintageInput",
    "classify_sec_company_eligibility_from_root",
    "derive_sec_quarterly_period_evidence",
    "fetch_sec_company_eligibility",
    "project_sec_quarterly_facts",
]

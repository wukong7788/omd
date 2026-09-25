"""Typed SEC companyfacts source facts and bounded JSON parser."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any

from ._metric_arithmetic import validate_decimal
from .errors import ResourceLimitError, SchemaMismatchError
from .quarterly import SecQuarterlyEligibility

SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_CANONICAL_CONCEPTS_VERSION = "sec-us-gaap-quarterly-v1"
SEC_CANONICAL_CONCEPTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "REVENUE": (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ),
        "GROSS_PROFIT": ("GrossProfit",),
        "OPERATING_INCOME": ("OperatingIncomeLoss",),
        "GAAP_DILUTED_EPS": ("EarningsPerShareDiluted",),
    }
)

_SERIALIZATION = "sec-companyfacts-json-v1"
_MAX_BODY_BYTES = 64 * 1024**2
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 1_500_000
_MAX_TAGS = 50_000
_MAX_FACT_ROWS = 500_000
_MAX_UNIT_ROWS = 200_000
_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_CIK = re.compile(r"^[0-9]{10}$")
_FRAME = re.compile(r"^CY([0-9]{4})(?:Q([1-4]))?(?:I)?$")
_QUARTER_FRAME = re.compile(r"^CY[0-9]{4}Q[1-4]$")
_ALLOWED_FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A"}
_FLOW_METRICS = {"REVENUE", "GROSS_PROFIT", "OPERATING_INCOME"}
_UNITS = {
    "REVENUE": {"USD", "iso4217:USD"},
    "GROSS_PROFIT": {"USD", "iso4217:USD"},
    "OPERATING_INCOME": {"USD", "iso4217:USD"},
    "GAAP_DILUTED_EPS": {"USD/shares", "usd/shares"},
}


class SecCanonicalMetric(str, Enum):
    REVENUE = "REVENUE"
    GROSS_PROFIT = "GROSS_PROFIT"
    OPERATING_INCOME = "OPERATING_INCOME"
    GAAP_DILUTED_EPS = "GAAP_DILUTED_EPS"


class SecCanonicalFieldStatus(str, Enum):
    PRESENT = "PRESENT"
    MISSING = "MISSING"
    AMBIGUOUS = "AMBIGUOUS"
    COVERAGE_INCOMPLETE = "COVERAGE_INCOMPLETE"


@dataclass(frozen=True)
class SecCompanyFactsFiling:
    accession_number: str
    form: str
    report_date: date
    accepted_at: datetime
    primary_document: str | None = None
    filing_url: str | None = None
    filing_date: date | None = None


@dataclass(frozen=True)
class SecCanonicalFactEvidence:
    native_tag: str
    unit: str
    value: Decimal
    period_start: date
    period_end: date
    fiscal_year_focus: int
    fiscal_period_focus: str
    frame: str | None
    accession_number: str
    form: str
    accepted_at: datetime
    companyfacts_observation_id: str
    filing_url: str | None = None
    source_evidence: tuple[SecCanonicalFactEvidence, ...] = ()


@dataclass(frozen=True)
class SecCanonicalQuarterField:
    metric: SecCanonicalMetric
    status: SecCanonicalFieldStatus
    value: Decimal | None
    unit: str | None
    evidence: tuple[SecCanonicalFactEvidence, ...]


@dataclass(frozen=True)
class SecCanonicalQuarterSlot:
    fiscal_year: int
    fiscal_quarter: int
    fields: tuple[SecCanonicalQuarterField, ...]

    def field(self, metric: SecCanonicalMetric) -> SecCanonicalQuarterField:
        for item in self.fields:
            if item.metric is metric:
                return item
        raise KeyError(metric)


@dataclass(frozen=True)
class SecCanonicalQuarterlyResult:
    ticker: str
    cik: str | None
    eligibility: SecQuarterlyEligibility
    slots: tuple[SecCanonicalQuarterSlot, ...]
    companyfacts_observation_id: str | None
    submissions_observation_ids: tuple[str, ...]
    coverage_complete: bool = False
    periods_resolved: bool = False
    uncovered_accessions: tuple[str, ...] = ()
    unresolved_period_accessions: tuple[str, ...] = ()
    concept_mapping_version: str = SEC_CANONICAL_CONCEPTS_VERSION


@dataclass(frozen=True)
class SecCompanyFactsFact:
    tag: str
    unit: str
    value: Decimal
    start: date
    end: date
    fy: int
    fp: str
    form: str
    filed: date
    accn: str
    frame: str | None


def _strict_companyfacts_json(body: bytes) -> dict[str, Any]:
    if type(body) is not bytes or len(body) > _MAX_BODY_BYTES:
        raise ResourceLimitError("SEC companyfacts response exceeds byte limit")
    depth = nodes = 0
    quoted = escaped = False
    for byte in body:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
            continue
        if byte == 34:
            quoted = True
        elif byte in (123, 91):
            depth += 1
            nodes += 1
        elif byte in (125, 93):
            depth -= 1
            if depth < 0:
                raise SchemaMismatchError("malformed SEC companyfacts JSON")
        elif byte == 44:
            nodes += 1
        if depth > _MAX_JSON_DEPTH or nodes > _MAX_JSON_NODES:
            raise ResourceLimitError("SEC companyfacts JSON structure limit exceeded")
    if quoted or depth != 0:
        raise SchemaMismatchError("malformed SEC companyfacts JSON")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SchemaMismatchError("duplicate key in SEC companyfacts JSON")
            result[key] = value
        return result

    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        if isinstance(exc, SchemaMismatchError):
            raise
        raise SchemaMismatchError("invalid SEC companyfacts JSON") from exc
    if type(value) is not dict:
        raise SchemaMismatchError("SEC companyfacts payload must be an object")
    return value


def _date(value: object, name: str) -> date:
    if type(value) is not str:
        raise SchemaMismatchError(f"SEC companyfacts {name} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise SchemaMismatchError(f"SEC companyfacts {name} is invalid") from exc
    if parsed.isoformat() != value:
        raise SchemaMismatchError(f"SEC companyfacts {name} is invalid")
    return parsed


def _accepted(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise SchemaMismatchError("SEC submissions acceptance timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SchemaMismatchError("SEC submissions acceptance timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SchemaMismatchError("SEC submissions acceptance timestamp is invalid")
    return parsed.astimezone(UTC)


def parse_sec_companyfacts_payload(
    payload: bytes | dict[str, Any], cik: str
) -> tuple[SecCompanyFactsFact, ...]:
    """Parse a bounded companyfacts payload into exact, typed us-gaap source facts."""
    if type(cik) is not str or not _CIK.fullmatch(cik):
        raise ValueError("CIK must be ten digits")
    if type(payload) is bytes:
        payload = _strict_companyfacts_json(payload)
    if type(payload) is not dict:
        raise SchemaMismatchError("SEC companyfacts payload must be an object")
    raw_cik = payload.get("cik")
    if type(raw_cik) is int and 0 < raw_cik <= 9_999_999_999:
        payload_cik = f"{raw_cik:010d}"
    elif (
        type(raw_cik) is str
        and raw_cik.isascii()
        and raw_cik.isdecimal()
        and 1 <= len(raw_cik) <= 10
    ):
        payload_cik = raw_cik.zfill(10)
    else:
        payload_cik = None
    if payload_cik != cik:
        raise SchemaMismatchError("SEC companyfacts CIK mismatch")
    facts = payload.get("facts")
    if type(facts) is not dict or type(facts.get("us-gaap")) is not dict:
        raise SchemaMismatchError("SEC companyfacts us-gaap facts missing")
    gaap = facts["us-gaap"]
    if len(gaap) > _MAX_TAGS:
        raise ResourceLimitError("SEC companyfacts taxonomy tag limit exceeded")
    aliases = {tag for items in SEC_CANONICAL_CONCEPTS.values() for tag in items}
    result: list[SecCompanyFactsFact] = []
    row_count = 0
    for tag in aliases:
        concept = gaap.get(tag)
        if concept is None:
            continue
        if type(concept) is not dict or type(concept.get("units")) is not dict:
            raise SchemaMismatchError("SEC companyfacts concept schema mismatch")
        units = concept["units"]
        if len(units) > 64:
            raise ResourceLimitError("SEC companyfacts unit count exceeded")
        unit_rows = sum(len(rows) for rows in units.values() if type(rows) is list)
        row_count += unit_rows
        if row_count > _MAX_FACT_ROWS:
            raise ResourceLimitError("SEC companyfacts fact row limit exceeded")
        for unit, rows in units.items():
            if type(unit) is not str or not unit or type(rows) is not list:
                raise SchemaMismatchError("SEC companyfacts unit schema mismatch")
            if len(rows) > _MAX_UNIT_ROWS:
                raise ResourceLimitError("SEC companyfacts unit row limit exceeded")
            for row in rows:
                if type(row) is not dict:
                    raise SchemaMismatchError("SEC companyfacts fact row is invalid")
                val = row.get("val")
                if type(val) is int:
                    decimal = Decimal(val)
                elif type(val) is Decimal and val.is_finite():
                    decimal = val
                else:
                    raise SchemaMismatchError("SEC companyfacts fact value is invalid")
                try:
                    validate_decimal(decimal)
                except ValueError as exc:
                    raise ResourceLimitError(
                        "SEC companyfacts decimal value exceeds limits"
                    ) from exc
                fy, fp, form, filed, accn = (
                    row.get("fy"),
                    row.get("fp"),
                    row.get("form"),
                    row.get("filed"),
                    row.get("accn"),
                )
                if type(form) is not str:
                    raise SchemaMismatchError("SEC companyfacts form is invalid")
                if form not in _ALLOWED_FORMS:
                    # Other SEC forms do not establish this 10-K/Q contract.
                    continue
                if (
                    type(fy) is not int
                    or not 1 <= fy <= 9999
                    or type(fp) is not str
                    or fp not in {"Q1", "Q2", "Q3", "Q4", "FY"}
                    or type(accn) is not str
                    or not _ACCESSION.fullmatch(accn)
                ):
                    raise SchemaMismatchError(
                        "SEC companyfacts fiscal or accession metadata is invalid"
                    )
                filed_date = _date(filed, "filed")
                if "end" not in row:
                    raise SchemaMismatchError("SEC companyfacts duration metadata is missing")
                if "start" not in row:
                    # Companyfacts can carry instant-context rows under a flow
                    # tag. They cannot establish a quarterly duration value.
                    _date(row.get("end"), "end")
                    continue
                start, end = _date(row.get("start"), "start"), _date(row.get("end"), "end")
                if start > end:
                    raise SchemaMismatchError("SEC companyfacts duration is reversed")
                frame = row.get("frame")
                if frame is not None and (type(frame) is not str or not _FRAME.fullmatch(frame)):
                    raise SchemaMismatchError("SEC companyfacts frame is invalid")
                result.append(
                    SecCompanyFactsFact(
                        tag, unit, decimal, start, end, fy, fp, form, filed_date, accn, frame
                    )
                )
    return tuple(result)


__all__ = [
    "SEC_CANONICAL_CONCEPTS",
    "SEC_CANONICAL_CONCEPTS_VERSION",
    "SEC_COMPANYFACTS_URL",
    "SecCanonicalFactEvidence",
    "SecCanonicalFieldStatus",
    "SecCanonicalMetric",
    "SecCanonicalQuarterField",
    "SecCanonicalQuarterSlot",
    "SecCanonicalQuarterlyResult",
    "SecCompanyFactsFact",
    "SecCompanyFactsFiling",
    "parse_sec_companyfacts_payload",
]

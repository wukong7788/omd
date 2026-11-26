"""Typed SEC companyfacts source facts and bounded JSON parser."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from types import MappingProxyType
from typing import Any

from ._bulk_zip_models import (
    _INSTANT_TAGS_BY_NS,
    _PARSED_TAGS_BY_NS,
    _SUPPORTED_NAMESPACES,
    SEC_CANONICAL_IFRS_CONCEPTS,
    SEC_CANONICAL_IFRS_REVENUE_PRIORITY,
    SEC_FUNDAMENTAL_CONCEPTS,
    SEC_MULTI_TAXONOMY_CONCEPTS_VERSION,
    SecFundamentalMetric,
    _currency_from_unit,
    _is_valid_canonical_unit,
)
from ._companyfacts_json import strict_companyfacts_json
from ._metric_arithmetic import validate_decimal
from .errors import ResourceLimitError, SchemaMismatchError
from .quarterly import SecQuarterlyEligibility

SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_CANONICAL_CONCEPTS_VERSION = "sec-us-gaap-quarterly-v2"
# In v2, the unqualified US-GAAP Revenues concept is the total-revenue tier.
# Contract-specific revenue concepts remain eligible fallbacks but do not
# override a same-period, same-context total-revenue fact.
SEC_CANONICAL_REVENUE_PRIORITY = ("Revenues",)
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
_QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A"})
_SAFE_INT_BOUND = 10**100
_ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"})
_ALLOWED_FORMS = _QUARTERLY_FORMS | _ANNUAL_FORMS
_VALID_FISCAL_PERIODS = frozenset({"Q1", "Q2", "Q3", "Q4", "FY"})
_FLOW_METRICS = {"REVENUE", "GROSS_PROFIT", "OPERATING_INCOME"}


def _is_annual_form(form: str) -> bool:
    return form in _ANNUAL_FORMS


def _is_quarterly_form(form: str) -> bool:
    return form in _QUARTERLY_FORMS


def normalize_sec_cik(cik: str | int) -> str:
    """Normalize an integer or digit-string SEC CIK to 10 zero-padded digits."""
    if type(cik) is int:
        if not 1 <= cik <= 9_999_999_999:
            raise ValueError("CIK must be between 1 and 9999999999")
        return f"{cik:010d}"
    if type(cik) is str:
        raw = cik.strip()
        if raw.isascii() and raw.isdecimal() and 1 <= len(raw) <= 10 and int(raw) >= 1:
            return raw.zfill(10)
    raise ValueError("CIK must be a positive integer or 1..10 digit string")


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
    source_evidence: tuple[SecCanonicalFactEvidence | SecFilingXbrlFactEvidence, ...] = ()
    namespace: str = "us-gaap"


@dataclass(frozen=True)
class SecFilingXbrlFactEvidence:
    """Canonical fact parsed from the exact retained XBRL filing instance."""

    native_tag: str
    unit: str
    value: Decimal
    period_start: date
    period_end: date
    fiscal_year_focus: int
    fiscal_period_focus: str
    accession_number: str
    form: str
    accepted_at: datetime
    index_observation_id: str
    instance_observation_id: str
    filing_url: str | None = None

    @property
    def source_evidence(self) -> tuple[SecCanonicalFactEvidence, ...]:
        return ()


@dataclass(frozen=True)
class Sec8KReleaseEvidence:
    """Direct Form 8-K Item 2.02 earnings release exhibit financial fact evidence."""

    native_label: str
    unit: str
    value: Decimal
    period_end: date
    fiscal_year_focus: int
    fiscal_period_focus: str
    accession_number: str
    form: str
    accepted_at: datetime
    filing_url: str
    exhibit_url: str
    index_observation_id: str
    exhibit_observation_id: str

    @property
    def native_tag(self) -> str:
        return self.native_label

    @property
    def source_evidence(self) -> tuple[SecCanonicalFactEvidence, ...]:
        return ()


SecCanonicalEvidence = SecCanonicalFactEvidence | SecFilingXbrlFactEvidence | Sec8KReleaseEvidence


@dataclass(frozen=True)
class SecCanonicalQuarterField:
    metric: SecCanonicalMetric
    status: SecCanonicalFieldStatus
    value: Decimal | None
    unit: str | None
    evidence: tuple[
        SecCanonicalFactEvidence | SecFilingXbrlFactEvidence | Sec8KReleaseEvidence, ...
    ]


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
    filing_xbrl_observation_ids: tuple[str, ...] = ()
    taxonomy: str | None = None
    reporting_currency: str | None = None
    dei_shares_outstanding: tuple[SecCompanyFactsFact, ...] = ()


@dataclass(frozen=True)
class SecCompanyFactsFact:
    tag: str
    unit: str
    value: Decimal
    start: date | None
    end: date
    fy: int | None
    fp: str | None
    form: str
    filed: date
    accn: str
    frame: str | None
    namespace: str = "us-gaap"


def _strict_companyfacts_json(body: bytes) -> dict[str, Any]:
    return strict_companyfacts_json(
        body,
        max_body_bytes=_MAX_BODY_BYTES,
        max_depth=_MAX_JSON_DEPTH,
        max_nodes=_MAX_JSON_NODES,
    )


@lru_cache(maxsize=8192)
def _parse_iso_date_cached(value: str) -> date | None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _date(value: object, name: str) -> date:
    if type(value) is not str:
        raise SchemaMismatchError(f"SEC companyfacts {name} is invalid")
    parsed = _parse_iso_date_cached(value)
    if parsed is None:
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
    """Parse a bounded companyfacts payload into typed us-gaap, ifrs-full, and dei facts."""
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
    if type(facts) is not dict:
        raise SchemaMismatchError("SEC companyfacts facts object missing")
    for ns in _SUPPORTED_NAMESPACES:
        if ns in facts and type(facts[ns]) is not dict:
            raise SchemaMismatchError(f"SEC companyfacts {ns} taxonomy schema mismatch")
    has_us_gaap = isinstance(facts.get("us-gaap"), dict)
    has_ifrs = isinstance(facts.get("ifrs-full"), dict)
    if not has_us_gaap and not has_ifrs:
        raise SchemaMismatchError("SEC companyfacts us-gaap or ifrs-full facts missing")
    total_tags = sum(
        len(facts[ns]) for ns in _SUPPORTED_NAMESPACES if isinstance(facts.get(ns), dict)
    )
    if total_tags > _MAX_TAGS:
        raise ResourceLimitError("SEC companyfacts taxonomy tag limit exceeded")
    result: list[SecCompanyFactsFact] = []
    valid_accns: set[str] = set()
    row_count = 0
    for namespace in _SUPPORTED_NAMESPACES:
        taxonomy_map = facts.get(namespace)
        if not isinstance(taxonomy_map, dict):
            continue
        instant_tags = _INSTANT_TAGS_BY_NS[namespace]
        for tag in _PARSED_TAGS_BY_NS[namespace]:
            concept = taxonomy_map.get(tag)
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
                        if not -_SAFE_INT_BOUND < val < _SAFE_INT_BOUND:
                            try:
                                validate_decimal(decimal)
                            except ValueError as exc:
                                raise ResourceLimitError(
                                    "SEC companyfacts decimal value exceeds limits"
                                ) from exc
                    elif type(val) is Decimal and val.is_finite():
                        decimal = val
                        try:
                            validate_decimal(decimal)
                        except ValueError as exc:
                            raise ResourceLimitError(
                                "SEC companyfacts decimal value exceeds limits"
                            ) from exc
                    else:
                        raise SchemaMismatchError("SEC companyfacts fact value is invalid")
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
                        continue
                    if type(accn) is not str:
                        raise SchemaMismatchError(
                            "SEC companyfacts fiscal or accession metadata is invalid"
                        )
                    if accn not in valid_accns:
                        if not _ACCESSION.fullmatch(accn):
                            raise SchemaMismatchError(
                                "SEC companyfacts fiscal or accession metadata is invalid"
                            )
                        valid_accns.add(accn)
                    if (
                        (namespace != "dei" and ("fy" not in row or "fp" not in row))
                        or (fy is not None and (type(fy) is not int or fy < 0))
                        or (fp is not None and type(fp) is not str)
                    ):
                        raise SchemaMismatchError(
                            "SEC companyfacts fiscal or accession metadata is invalid"
                        )
                    # Real SEC EDGAR rows occasionally carry omitted/placeholder/non-standard
                    # fiscal focus (fy in {None, 0, >9999} or fp not in Q1..Q4/FY). Keep DEI
                    # cover facts with normalized None focus and skip unfocused non-DEI rows
                    # without discarding the rest of the issuer's history.
                    fy_norm: int | None = fy if type(fy) is int and 1 <= fy <= 9999 else None
                    fp_norm: str | None = (
                        fp if type(fp) is str and fp in _VALID_FISCAL_PERIODS else None
                    )
                    filed_date = _date(filed, "filed")
                    if "end" not in row:
                        raise SchemaMismatchError("SEC companyfacts duration metadata is missing")
                    end = _date(row.get("end"), "end")
                    if "start" not in row:
                        if tag not in instant_tags:
                            continue
                        start: date | None = None
                    else:
                        start = _date(row.get("start"), "start")
                        if start > end:
                            raise SchemaMismatchError("SEC companyfacts duration is reversed")
                    frame = row.get("frame")
                    if frame is not None and (
                        type(frame) is not str or not _FRAME.fullmatch(frame)
                    ):
                        raise SchemaMismatchError("SEC companyfacts frame is invalid")
                    if namespace != "dei" and (fy_norm is None or fp_norm is None):
                        continue
                    result.append(
                        SecCompanyFactsFact(
                            tag,
                            unit,
                            decimal,
                            start,
                            end,
                            fy_norm,
                            fp_norm,
                            form,
                            filed_date,
                            accn,
                            frame,
                            namespace,
                        )
                    )
    return tuple(result)


__all__ = [
    "SEC_CANONICAL_CONCEPTS",
    "SEC_CANONICAL_CONCEPTS_VERSION",
    "SEC_CANONICAL_IFRS_CONCEPTS",
    "SEC_CANONICAL_IFRS_REVENUE_PRIORITY",
    "SEC_COMPANYFACTS_URL",
    "SEC_FUNDAMENTAL_CONCEPTS",
    "SEC_MULTI_TAXONOMY_CONCEPTS_VERSION",
    "Sec8KReleaseEvidence",
    "SecCanonicalEvidence",
    "SecCanonicalFactEvidence",
    "SecCanonicalFieldStatus",
    "SecCanonicalMetric",
    "SecCanonicalQuarterField",
    "SecCanonicalQuarterSlot",
    "SecCanonicalQuarterlyResult",
    "SecCompanyFactsFact",
    "SecCompanyFactsFiling",
    "SecFilingXbrlFactEvidence",
    "SecFundamentalMetric",
    "_currency_from_unit",
    "_is_valid_canonical_unit",
    "normalize_sec_cik",
    "parse_sec_companyfacts_payload",
]

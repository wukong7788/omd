"""Bounded multi-taxonomy XBRL/iXBRL parser for foreign private issuer Form 6-K."""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from ._fpi_6k_models import SecFpi6KMetricRow, SecFpi6KShareBasis, normalize_fpi_cik
from .errors import ResourceLimitError, SchemaMismatchError

_MAX_XBRL_BYTES = 8 * 1024**2
_MAX_ELEMENTS = 250_000
_DECIMAL = re.compile(r"^-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")

_US_GAAP_NS = re.compile(r"^https?://fasb\.org/us-gaap/[0-9]{4}(?:-[0-9]{2}-[0-9]{2})?$")
_IFRS_FULL_NS = re.compile(
    r"^https?://xbrl\.ifrs\.org/taxonomy/[0-9]{4}(?:-[0-9]{2}-[0-9]{2})?/ifrs-full$"
)
_DEI_NS = re.compile(r"^https?://xbrl\.sec\.gov/dei/[0-9]{4}(?:-[0-9]{2}-[0-9]{2})?$")
_XBRLI_NS = "http://www.xbrl.org/2003/instance"
_XBRLDI_NS = "http://xbrl.org/2006/xbrldi"
_IX_NS = "http://www.xbrl.org/2013/inlineXBRL"
_XHTML_NS = "http://www.w3.org/1999/xhtml"
_ISO4217_NS = "http://www.xbrl.org/2003/iso4217"
_SEC_CIK_SCHEME = "http://www.sec.gov/CIK"

_CONCEPT_MAP: dict[tuple[str, str], tuple[str, str, str]] = {
    ("us-gaap", "Revenues"): ("revenue", "duration", "monetary"),
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"): (
        "revenue",
        "duration",
        "monetary",
    ),
    ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"): (
        "revenue",
        "duration",
        "monetary",
    ),
    ("us-gaap", "SalesRevenueNet"): ("revenue", "duration", "monetary"),
    ("us-gaap", "GrossProfit"): ("gross_profit", "duration", "monetary"),
    ("us-gaap", "OperatingIncomeLoss"): ("operating_income", "duration", "monetary"),
    ("us-gaap", "NetIncomeLoss"): ("net_income", "duration", "monetary"),
    ("us-gaap", "ProfitLoss"): ("net_income", "duration", "monetary"),
    ("us-gaap", "EarningsPerShareDiluted"): ("diluted_eps", "duration", "per_share"),
    ("us-gaap", "EarningsPerShareBasic"): ("basic_eps", "duration", "per_share"),
    ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"): ("ocf", "duration", "monetary"),
    ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"): ("capex", "duration", "monetary"),
    ("us-gaap", "Assets"): ("total_assets", "instant", "monetary"),
    ("us-gaap", "LongTermDebtAndCapitalLeaseObligations"): ("total_debt", "instant", "monetary"),
    ("us-gaap", "LongTermDebt"): ("total_debt", "instant", "monetary"),
    ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"): (
        "diluted_shares",
        "duration",
        "shares",
    ),
    ("ifrs-full", "Revenue"): ("revenue", "duration", "monetary"),
    ("ifrs-full", "RevenueFromContractsWithCustomers"): ("revenue", "duration", "monetary"),
    ("ifrs-full", "GrossProfit"): ("gross_profit", "duration", "monetary"),
    ("ifrs-full", "ProfitLossFromOperatingActivities"): (
        "operating_income",
        "duration",
        "monetary",
    ),
    ("ifrs-full", "ProfitLoss"): ("net_income", "duration", "monetary"),
    ("ifrs-full", "ProfitLossAttributableToOwnersOfParent"): (
        "net_income",
        "duration",
        "monetary",
    ),
    ("ifrs-full", "DilutedEarningsLossPerShare"): ("diluted_eps", "duration", "per_share"),
    ("ifrs-full", "BasicEarningsLossPerShare"): ("basic_eps", "duration", "per_share"),
    ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"): ("ocf", "duration", "monetary"),
    ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"): (
        "capex",
        "duration",
        "monetary",
    ),
    ("ifrs-full", "Assets"): ("total_assets", "instant", "monetary"),
    ("ifrs-full", "Borrowings"): ("total_debt", "instant", "monetary"),
    ("ifrs-full", "AdjustedWeightedAverageShares"): ("diluted_shares", "duration", "shares"),
    ("dei", "EntityCommonStockSharesOutstanding"): (
        "dei_common_shares_outstanding",
        "instant",
        "shares",
    ),
}


@dataclass(frozen=True)
class SecFpi6KXbrlParseResult:
    """Parsed canonical facts and DEI period focus from a 6-K XBRL/iXBRL attachment."""

    period_end: date | None
    fiscal_year: int | None
    fiscal_quarter: int | None
    reporting_currency: str | None
    metrics: tuple[SecFpi6KMetricRow, ...]


def _expanded_name(name: str) -> tuple[str, str]:
    if name.startswith("{") and "}" in name:
        namespace, local = name[1:].split("}", 1)
        return namespace, local
    return "", name


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1].split(":")[-1]


def _is_el(element: ET.Element, namespace: str, local: str) -> bool:
    return _expanded_name(element.tag) == (namespace, local)


def _taxonomy_name(namespace: str) -> str | None:
    if _US_GAAP_NS.fullmatch(namespace) is not None:
        return "us-gaap"
    if _IFRS_FULL_NS.fullmatch(namespace) is not None:
        return "ifrs-full"
    if _DEI_NS.fullmatch(namespace) is not None:
        return "dei"
    return None


def _resolve_qname(value: str, namespaces: dict[str, str]) -> tuple[str, str]:
    if not value or len(value) > 256:
        raise SchemaMismatchError("SEC 6-K XBRL QName is missing or invalid")
    if ":" in value:
        prefix, local = value.split(":", 1)
        namespace = namespaces.get(prefix)
    else:
        local = value
        namespace = namespaces.get("")
    if not namespace or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9._-]*", local):
        raise SchemaMismatchError("SEC 6-K XBRL QName is unbound or invalid")
    return namespace, local


def _iso_date(value: str, name: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise SchemaMismatchError(f"SEC 6-K XBRL {name} is invalid") from exc
    if parsed.isoformat() != value:
        raise SchemaMismatchError(f"SEC 6-K XBRL {name} is invalid")
    return parsed


def _simple_text(element: ET.Element, name: str) -> str:
    value = "".join(element.itertext()).strip()
    if not value or len(value) > 128:
        raise SchemaMismatchError(f"SEC 6-K XBRL {name} is missing or invalid")
    return value


def _parse_decimal(raw: str) -> Decimal:
    value = raw.strip().replace(",", "")
    if not _DECIMAL.fullmatch(value):
        raise SchemaMismatchError("SEC 6-K XBRL numeric fact is malformed")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise SchemaMismatchError("SEC 6-K XBRL numeric fact is malformed") from exc
    if not result.is_finite() or len(result.as_tuple().digits) > 64:
        raise SchemaMismatchError("SEC 6-K XBRL numeric fact exceeds bounds")
    return result


def _parse_unit_measure(
    element: ET.Element,
    namespaces: dict[str, str],
) -> tuple[str, str | None, str]:
    measures = [c for c in element.iter() if _is_el(c, _XBRLI_NS, "measure")]
    numerators = [c for c in element.iter() if _is_el(c, _XBRLI_NS, "unitNumerator")]
    denominators = [c for c in element.iter() if _is_el(c, _XBRLI_NS, "unitDenominator")]

    if numerators or denominators:
        num_measures = (
            [c for c in numerators[0].iter() if _is_el(c, _XBRLI_NS, "measure")]
            if len(numerators) == 1
            else []
        )
        den_measures = (
            [c for c in denominators[0].iter() if _is_el(c, _XBRLI_NS, "measure")]
            if len(denominators) == 1
            else []
        )
        if len(num_measures) != 1 or len(den_measures) != 1:
            raise SchemaMismatchError("SEC 6-K XBRL divided unit is malformed")
        num_ns, num_local = _resolve_qname(_simple_text(num_measures[0], "unit"), namespaces)
        den_ns, den_local = _resolve_qname(_simple_text(den_measures[0], "unit"), namespaces)
        if (
            num_ns == _ISO4217_NS
            and _ISO_CURRENCY.fullmatch(num_local) is not None
            and (den_ns, den_local) == (_XBRLI_NS, "shares")
        ):
            return f"{num_local}/shares", num_local, "per_share"
        return "unsupported", None, "unsupported"

    if len(measures) != 1:
        raise SchemaMismatchError("SEC 6-K XBRL unit is malformed")
    m_ns, m_local = _resolve_qname(_simple_text(measures[0], "unit"), namespaces)
    if m_ns == _ISO4217_NS and _ISO_CURRENCY.fullmatch(m_local) is not None:
        return m_local, m_local, "monetary"
    if (m_ns, m_local) == (_XBRLI_NS, "shares"):
        return "shares", None, "shares"
    return "unsupported", None, "unsupported"


def _parse_contexts(
    elements: list[ET.Element],
    cik: str,
) -> dict[str, tuple[str, date | None, date, bool]]:
    contexts: dict[str, tuple[str, date | None, date, bool]] = {}
    for element in elements:
        if not _is_el(element, _XBRLI_NS, "context"):
            continue
        context_id = element.get("id")
        if not context_id or context_id in contexts:
            raise SchemaMismatchError("SEC 6-K XBRL context identity is missing or duplicate")
        identifiers = [c for c in element.iter() if _is_el(c, _XBRLI_NS, "identifier")]
        if len(identifiers) != 1 or identifiers[0].get("scheme") != _SEC_CIK_SCHEME:
            raise SchemaMismatchError("SEC 6-K XBRL context entity identifier is invalid")
        entity_raw = _simple_text(identifiers[0], "entity CIK")
        try:
            entity_cik = normalize_fpi_cik(entity_raw)
        except (TypeError, ValueError) as exc:
            raise SchemaMismatchError("SEC 6-K XBRL context entity CIK is invalid") from exc
        if entity_cik != cik:
            raise SchemaMismatchError("SEC 6-K XBRL context entity mismatch")
        dimensional = any(
            _is_el(c, _XBRLI_NS, "segment")
            or _is_el(c, _XBRLI_NS, "scenario")
            or _is_el(c, _XBRLDI_NS, "explicitMember")
            or _is_el(c, _XBRLDI_NS, "typedMember")
            for c in element.iter()
        )
        starts = [c for c in element.iter() if _is_el(c, _XBRLI_NS, "startDate")]
        ends = [c for c in element.iter() if _is_el(c, _XBRLI_NS, "endDate")]
        instants = [c for c in element.iter() if _is_el(c, _XBRLI_NS, "instant")]
        if len(ends) + len(instants) != 1 or len(starts) > 1 or (starts and instants):
            raise SchemaMismatchError("SEC 6-K XBRL context period is malformed")
        end = _iso_date(
            _simple_text(ends[0] if ends else instants[0], "context end"), "context end"
        )
        start = (
            _iso_date(_simple_text(starts[0], "context start"), "context start") if starts else None
        )
        if start is not None and start > end:
            raise SchemaMismatchError("SEC 6-K XBRL context duration is reversed")
        contexts[context_id] = ("duration" if start else "instant", start, end, dimensional)
    return contexts


def _extract_dei_focus(
    elements: list[ET.Element],
    namespaces: dict[str, str],
) -> tuple[date | None, int | None, int | None]:
    dates: set[date] = set()
    years: set[int] = set()
    quarters: set[int | None] = set()
    for element in elements:
        ns, local = _expanded_name(element.tag)
        if ns == _IX_NS and local == "nonNumeric":
            fact_ns, tag = _resolve_qname(element.get("name", ""), namespaces)
            if _DEI_NS.fullmatch(fact_ns) is None:
                continue
        elif _DEI_NS.fullmatch(ns) is not None:
            tag = local
        else:
            continue
        if tag == "DocumentPeriodEndDate":
            dates.add(_iso_date(_simple_text(element, "period end"), "period end"))
        elif tag == "DocumentFiscalYearFocus":
            raw_yr = _simple_text(element, "fiscal year focus")
            try:
                yr = int(raw_yr)
            except ValueError as exc:
                raise SchemaMismatchError("SEC 6-K XBRL fiscal year focus is invalid") from exc
            if not 1 <= yr <= 9999:
                raise SchemaMismatchError("SEC 6-K XBRL fiscal year focus is out of range")
            years.add(yr)
        elif tag == "DocumentFiscalPeriodFocus":
            raw_fp = _simple_text(element, "fiscal period focus")
            if raw_fp in {"Q1", "Q2", "Q3", "Q4"}:
                quarters.add(int(raw_fp[1]))
            elif raw_fp == "FY":
                quarters.add(None)
    if len(dates) > 1 or len(years) > 1 or len(quarters) > 1:
        raise SchemaMismatchError("SEC 6-K XBRL DEI focus is conflicting")
    return (
        next(iter(dates)) if dates else None,
        next(iter(years)) if years else None,
        next(iter(quarters)) if quarters else None,
    )


def parse_sec_fpi_6k_xbrl(
    body: bytes,
    *,
    cik: str,
    expected_period_end: date | None = None,
) -> SecFpi6KXbrlParseResult:
    """Parse ``us-gaap``, ``ifrs-full``, and ``dei`` facts from a 6-K XBRL/iXBRL file.

    Preserves native ISO-4217 currencies (``EUR``, ``TWD``, ``USD``, ``CNY``, etc.)
    without currency conversion and rejects dimensional or non-quarterly flow facts.
    """
    normalized_cik = normalize_fpi_cik(cik)
    if type(body) is not bytes or not 0 < len(body) <= _MAX_XBRL_BYTES:
        raise ResourceLimitError("SEC 6-K XBRL source exceeds byte limit")
    upper = body.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise SchemaMismatchError("SEC 6-K XBRL XML declarations are unsupported")

    namespaces: dict[str, str] = {}
    elements: list[ET.Element] = []
    root: ET.Element | None = None
    try:
        for event, payload in ET.iterparse(io.BytesIO(body), events=("start-ns", "start")):
            if event == "start-ns":
                prefix, namespace = payload
                if prefix in namespaces and namespaces[prefix] != namespace:
                    raise SchemaMismatchError("SEC 6-K XBRL namespace prefix is redefined")
                namespaces[prefix] = namespace
            else:
                if root is None:
                    root = payload
                elements.append(payload)
                if len(elements) > _MAX_ELEMENTS:
                    raise ResourceLimitError("SEC 6-K XBRL element limit exceeded")
    except ET.ParseError as exc:
        raise SchemaMismatchError("malformed SEC 6-K XBRL document") from exc

    if root is None:
        raise SchemaMismatchError("SEC 6-K XBRL root element is missing")
    root_ns, root_local = _expanded_name(root.tag)
    if (root_ns, root_local) != (_XBRLI_NS, "xbrl") and not (
        root_local == "html" and root_ns in {"", _XHTML_NS}
    ):
        raise SchemaMismatchError("SEC 6-K XBRL root element is invalid")

    dei_period_end, dei_fy, dei_fq = _extract_dei_focus(elements, namespaces)
    if (
        expected_period_end is not None
        and dei_period_end is not None
        and dei_period_end != expected_period_end
    ):
        raise SchemaMismatchError("SEC 6-K XBRL DEI period end conflicts with submissions")
    target_period_end = dei_period_end or expected_period_end

    contexts = _parse_contexts(elements, normalized_cik)
    units: dict[str, tuple[str, str | None, str]] = {}
    for element in elements:
        if not _is_el(element, _XBRLI_NS, "unit"):
            continue
        unit_id = element.get("id")
        if not unit_id or unit_id in units:
            raise SchemaMismatchError("SEC 6-K XBRL unit identity is missing or duplicate")
        units[unit_id] = _parse_unit_measure(element, namespaces)

    rows: list[SecFpi6KMetricRow] = []
    seen_rows: set[tuple[object, ...]] = set()
    for element in elements:
        ns, local = _expanded_name(element.tag)
        is_ix = ns == _IX_NS and local in {"nonFraction", "fraction"}
        fact_ns, tag = _resolve_qname(element.get("name", ""), namespaces) if is_ix else (ns, local)
        taxonomy = _taxonomy_name(fact_ns)
        if taxonomy is None:
            continue
        spec = _CONCEPT_MAP.get((taxonomy, tag))
        if spec is None:
            continue
        metric_name, expected_period_type, expected_unit_kind = spec

        context = contexts.get(element.get("contextRef", ""))
        unit_info = units.get(element.get("unitRef", ""))
        if context is None or unit_info is None:
            raise SchemaMismatchError("SEC 6-K XBRL fact references unknown context or unit")
        period_type, start, end, dimensional = context
        unit_str, currency, unit_kind = unit_info
        if dimensional or period_type != expected_period_type or unit_kind != expected_unit_kind:
            continue
        if target_period_end is not None:
            if metric_name == "dei_common_shares_outstanding":
                if end < target_period_end:
                    continue
            elif end != target_period_end:
                continue
        if period_type == "duration" and (start is None or not 60 <= (end - start).days + 1 <= 120):
            continue

        nil = next((v for k, v in element.attrib.items() if _local(k) == "nil"), "false")
        if nil.lower() == "true":
            continue
        value = _parse_decimal("".join(element.itertext()).strip())
        if is_ix:
            scale = element.get("scale")
            sign = element.get("sign")
            if scale is not None:
                try:
                    power = int(scale)
                except ValueError as exc:
                    raise SchemaMismatchError("SEC 6-K XBRL scale is invalid") from exc
                if not -18 <= power <= 18:
                    raise SchemaMismatchError("SEC 6-K XBRL scale is outside bounds")
                value = value * (Decimal(10) ** power)
            if sign == "-":
                value = value.copy_negate()
            elif sign not in {None, "+"}:
                raise SchemaMismatchError("SEC 6-K XBRL sign is invalid")

        row = SecFpi6KMetricRow(
            metric=metric_name,
            native_tag=f"{taxonomy}:{tag}",
            value=value,
            unit=unit_str,
            reporting_currency=currency,
            share_basis=SecFpi6KShareBasis.UNKNOWN,
            taxonomy=taxonomy,
            period_start=start,
            period_end=end,
            period_type=period_type,
        )
        dedupe_key = (
            row.metric,
            row.native_tag,
            row.value,
            row.unit,
            row.period_start,
            row.period_end,
            row.period_type,
        )
        if dedupe_key not in seen_rows:
            seen_rows.add(dedupe_key)
            rows.append(row)

    inferred_period_end = target_period_end
    if inferred_period_end is None and rows:
        non_dei_ends = [
            r.period_end
            for r in rows
            if r.metric != "dei_common_shares_outstanding" and r.period_end is not None
        ]
        candidate_ends = non_dei_ends or [r.period_end for r in rows if r.period_end is not None]
        if candidate_ends:
            inferred_period_end = max(candidate_ends)
            rows = [
                r
                for r in rows
                if r.period_end is not None
                and (
                    r.period_end >= inferred_period_end
                    if r.metric == "dei_common_shares_outstanding"
                    else r.period_end == inferred_period_end
                )
            ]

    currencies = {r.reporting_currency for r in rows if r.reporting_currency is not None}
    reporting_currency = next(iter(currencies)) if len(currencies) == 1 else None
    return SecFpi6KXbrlParseResult(
        period_end=inferred_period_end,
        fiscal_year=dei_fy,
        fiscal_quarter=dei_fq,
        reporting_currency=reporting_currency,
        metrics=tuple(rows),
    )


__all__ = [
    "_MAX_XBRL_BYTES",
    "SecFpi6KXbrlParseResult",
    "parse_sec_fpi_6k_xbrl",
]

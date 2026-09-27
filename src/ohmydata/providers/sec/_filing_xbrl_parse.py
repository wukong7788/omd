"""Bounded parsing of canonical facts from one SEC XBRL instance."""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ._companyfacts_models import SEC_CANONICAL_CONCEPTS
from .errors import ResourceLimitError, SchemaMismatchError

_MAX_BYTES = 8 * 1024**2
_MAX_ELEMENTS = 250_000
_CIK = re.compile(r"^[0-9]{10}$")
_DECIMAL = re.compile(r"^-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_US_GAAP_NAMESPACE = re.compile(r"^http://fasb\.org/us-gaap/[0-9]{4}$")
_DEI_NAMESPACE = re.compile(r"^http://xbrl\.sec\.gov/dei/[0-9]{4}$")
_XBRLI_NAMESPACE = "http://www.xbrl.org/2003/instance"
_XBRLDI_NAMESPACE = "http://xbrl.org/2006/xbrldi"
_IX_NAMESPACE = "http://www.xbrl.org/2013/inlineXBRL"
_ISO4217_NAMESPACE = "http://www.xbrl.org/2003/iso4217"
_SEC_CIK_SCHEME = "http://www.sec.gov/CIK"
_ALLOWED_TAGS = frozenset(tag for tags in SEC_CANONICAL_CONCEPTS.values() for tag in tags)


@dataclass(frozen=True)
class SecFilingXbrlFact:
    """One undimensioned canonical fact from an exact filing instance."""

    tag: str
    unit: str
    value: Decimal
    start: date
    end: date
    fiscal_year_focus: int
    fiscal_period_focus: str
    accession_number: str
    form: str
    filed: date
    accepted_at: datetime

    @property
    def fy(self) -> int:
        return self.fiscal_year_focus

    @property
    def fp(self) -> str:
        return self.fiscal_period_focus

    @property
    def accn(self) -> str:
        return self.accession_number

    @property
    def frame(self) -> None:
        return None


@dataclass(frozen=True)
class SecFilingXbrlParse:
    fiscal_year_focus: int
    fiscal_period_focus: str
    period_end: date
    facts: tuple[SecFilingXbrlFact, ...]


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1].split(":")[-1]


def _expanded_name(name: str) -> tuple[str, str]:
    if name.startswith("{") and "}" in name:
        namespace, local = name[1:].split("}", 1)
        return namespace, local
    return "", name


def _is_element(element: ET.Element, namespace: str, local: str) -> bool:
    return _expanded_name(element.tag) == (namespace, local)


def _resolve_qname(value: str, namespaces: dict[str, str]) -> tuple[str, str]:
    if not value or len(value) > 256:
        raise SchemaMismatchError("SEC filing XBRL QName is missing or invalid")
    if ":" in value:
        prefix, local = value.split(":", 1)
        namespace = namespaces.get(prefix)
    else:
        local = value
        namespace = namespaces.get("")
    if not namespace or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9._-]*", local):
        raise SchemaMismatchError("SEC filing XBRL QName is unbound or invalid")
    return namespace, local


def _iso_date(value: str, name: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise SchemaMismatchError(f"SEC filing XBRL {name} is invalid") from exc
    if parsed.isoformat() != value:
        raise SchemaMismatchError(f"SEC filing XBRL {name} is invalid")
    return parsed


def _simple_text(element: ET.Element, name: str) -> str:
    value = "".join(element.itertext()).strip()
    if not value or len(value) > 128:
        raise SchemaMismatchError(f"SEC filing XBRL {name} is missing or invalid")
    return value


def _parse_decimal(raw: str) -> Decimal:
    value = raw.strip().replace(",", "")
    if not _DECIMAL.fullmatch(value):
        raise SchemaMismatchError("SEC filing XBRL numeric fact is malformed")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise SchemaMismatchError("SEC filing XBRL numeric fact is malformed") from exc
    if not result.is_finite() or len(result.as_tuple().digits) > 64:
        raise SchemaMismatchError("SEC filing XBRL numeric fact exceeds bounds")
    return result


def parse_sec_filing_xbrl_instance(
    body: bytes,
    *,
    cik: str,
    accession_number: str,
    form: str,
    report_date: date,
    filing_date: date,
    accepted_at: datetime,
) -> SecFilingXbrlParse:
    """Parse canonical facts with exact entity, filing focus, context and unit checks."""
    if type(body) is not bytes or len(body) > _MAX_BYTES:
        raise ResourceLimitError("SEC filing XBRL instance exceeds byte limit")
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise SchemaMismatchError("SEC filing XBRL declarations are unsupported")
    if not _CIK.fullmatch(cik) or form not in {"10-Q", "10-Q/A"}:
        raise ValueError("invalid SEC filing XBRL identity")
    try:
        iterator = ET.iterparse(io.BytesIO(body), events=("start-ns", "start"))
        namespaces: dict[str, str] = {}
        root: ET.Element | None = None
        for event, payload in iterator:
            if event == "start-ns":
                prefix, namespace = payload
                if prefix in namespaces and namespaces[prefix] != namespace:
                    raise SchemaMismatchError("SEC filing XBRL namespace prefix is redefined")
                namespaces[prefix] = namespace
            elif root is None:
                root = payload
    except ET.ParseError as exc:
        raise SchemaMismatchError("malformed SEC filing XBRL instance") from exc
    if root is None:
        raise SchemaMismatchError("SEC filing XBRL root element is missing")
    elements = list(root.iter())
    if len(elements) > _MAX_ELEMENTS:
        raise ResourceLimitError("SEC filing XBRL element limit exceeded")
    if not _is_element(root, _XBRLI_NAMESPACE, "xbrl"):
        raise SchemaMismatchError("SEC filing XBRL root namespace is invalid")

    year_values: list[str] = []
    period_values: list[str] = []
    date_values: list[str] = []
    focus_elements: list[ET.Element] = []
    for element in elements:
        namespace, local = _expanded_name(element.tag)
        is_dei = _DEI_NAMESPACE.fullmatch(namespace) is not None
        if is_dei and local == "DocumentFiscalYearFocus":
            year_values.append(_simple_text(element, "fiscal year focus"))
            focus_elements.append(element)
        elif is_dei and local == "DocumentFiscalPeriodFocus":
            period_values.append(_simple_text(element, "fiscal period focus"))
            focus_elements.append(element)
        elif is_dei and local == "DocumentPeriodEndDate":
            date_values.append(_simple_text(element, "period end"))
            focus_elements.append(element)
    if len(set(year_values)) != 1 or len(set(period_values)) != 1 or len(set(date_values)) != 1:
        raise SchemaMismatchError("SEC filing XBRL DEI focus is missing or conflicting")
    try:
        fiscal_year = int(year_values[0])
    except ValueError as exc:
        raise SchemaMismatchError("SEC filing XBRL fiscal year focus is invalid") from exc
    period = period_values[0]
    end_date = _iso_date(date_values[0], "period end")
    if not 1 <= fiscal_year <= 9999 or period not in {"Q1", "Q2", "Q3"}:
        raise SchemaMismatchError("SEC filing XBRL fiscal focus is invalid for a 10-Q")
    if end_date != report_date:
        raise SchemaMismatchError("SEC filing XBRL period end conflicts with submissions")

    contexts: dict[str, tuple[str, date | None, date, bool]] = {}
    for element in elements:
        if not _is_element(element, _XBRLI_NAMESPACE, "context"):
            continue
        context_id = element.get("id")
        if not context_id or context_id in contexts:
            raise SchemaMismatchError("SEC filing XBRL context identity is missing or duplicate")
        identifiers = [
            child for child in element.iter() if _is_element(child, _XBRLI_NAMESPACE, "identifier")
        ]
        if (
            len(identifiers) != 1
            or identifiers[0].get("scheme") != _SEC_CIK_SCHEME
            or _simple_text(identifiers[0], "entity CIK") != cik
        ):
            raise SchemaMismatchError("SEC filing XBRL context entity mismatch")
        dimensional = any(
            _is_element(child, _XBRLI_NAMESPACE, "segment")
            or _is_element(child, _XBRLI_NAMESPACE, "scenario")
            or _is_element(child, _XBRLDI_NAMESPACE, "explicitMember")
            or _is_element(child, _XBRLDI_NAMESPACE, "typedMember")
            for child in element.iter()
        )
        starts = [
            child for child in element.iter() if _is_element(child, _XBRLI_NAMESPACE, "startDate")
        ]
        ends = [
            child for child in element.iter() if _is_element(child, _XBRLI_NAMESPACE, "endDate")
        ]
        instants = [
            child for child in element.iter() if _is_element(child, _XBRLI_NAMESPACE, "instant")
        ]
        if len(ends) + len(instants) != 1 or len(starts) > 1 or (starts and instants):
            raise SchemaMismatchError("SEC filing XBRL context period is malformed")
        end = _iso_date(
            _simple_text(ends[0] if ends else instants[0], "context end"), "context end"
        )
        start = (
            _iso_date(_simple_text(starts[0], "context start"), "context start") if starts else None
        )
        contexts[context_id] = ("duration" if start else "instant", start, end, dimensional)

    for element in focus_elements:
        context_id = element.get("contextRef")
        focus_context = contexts.get(context_id or "")
        if focus_context is None or focus_context[2] != report_date or focus_context[3]:
            raise SchemaMismatchError("SEC filing XBRL DEI focus context is invalid")

    units: dict[str, str] = {}
    for element in elements:
        if not _is_element(element, _XBRLI_NAMESPACE, "unit"):
            continue
        unit_id = element.get("id")
        measures = [
            child for child in element.iter() if _is_element(child, _XBRLI_NAMESPACE, "measure")
        ]
        numerators = [
            child
            for child in element.iter()
            if _is_element(child, _XBRLI_NAMESPACE, "unitNumerator")
        ]
        denominators = [
            child
            for child in element.iter()
            if _is_element(child, _XBRLI_NAMESPACE, "unitDenominator")
        ]
        if not unit_id or unit_id in units:
            raise SchemaMismatchError("SEC filing XBRL unit identity is missing or duplicate")
        if numerators or denominators:
            numerator_measures = (
                [
                    child
                    for child in numerators[0].iter()
                    if _is_element(child, _XBRLI_NAMESPACE, "measure")
                ]
                if len(numerators) == 1
                else []
            )
            denominator_measures = (
                [
                    child
                    for child in denominators[0].iter()
                    if _is_element(child, _XBRLI_NAMESPACE, "measure")
                ]
                if len(denominators) == 1
                else []
            )
            if len(numerator_measures) != 1 or len(denominator_measures) != 1:
                raise SchemaMismatchError("SEC filing XBRL divided unit is malformed")
            numerator_qname, denominator_qname = (
                _resolve_qname(_simple_text(item, "unit measure"), namespaces)
                for item in (numerator_measures[0], denominator_measures[0])
            )
            if numerator_qname == (_ISO4217_NAMESPACE, "USD") and denominator_qname == (
                _XBRLI_NAMESPACE,
                "shares",
            ):
                units[unit_id] = "USD/shares"
            else:
                units[unit_id] = "invalid"
        else:
            if len(measures) != 1:
                raise SchemaMismatchError("SEC filing XBRL unit is malformed")
            measure_qname = _resolve_qname(_simple_text(measures[0], "unit measure"), namespaces)
            units[unit_id] = "USD" if measure_qname == (_ISO4217_NAMESPACE, "USD") else "invalid"

    rows: list[SecFilingXbrlFact] = []
    for element in elements:
        namespace, local = _expanded_name(element.tag)
        if namespace == _IX_NAMESPACE and local in {"nonFraction", "fraction"}:
            fact_namespace, tag = _resolve_qname(element.get("name", ""), namespaces)
            if _US_GAAP_NAMESPACE.fullmatch(fact_namespace) is None:
                continue
        elif _US_GAAP_NAMESPACE.fullmatch(namespace) is not None:
            tag = local
        else:
            continue
        if tag not in _ALLOWED_TAGS:
            continue
        context = contexts.get(element.get("contextRef", ""))
        unit = units.get(element.get("unitRef", ""))
        if context is None or unit is None:
            raise SchemaMismatchError("SEC filing XBRL canonical fact has unknown context or unit")
        period_type, start, end, dimensional = context
        if period_type != "duration" or start is None or end != report_date or dimensional:
            continue
        metric = next(name for name, tags in SEC_CANONICAL_CONCEPTS.items() if tag in tags)
        normalized_unit = unit.replace("iso4217:", "")
        allowed_unit = "USD/shares" if metric == "GAAP_DILUTED_EPS" else "USD"
        if normalized_unit != allowed_unit:
            continue
        days = (end - start).days + 1
        if not 60 <= days <= 120:
            continue
        nil = next(
            (value for name, value in element.attrib.items() if _local(name) == "nil"), "false"
        )
        if nil.lower() == "true":
            continue
        raw = "".join(element.itertext()).strip()
        if namespace == _IX_NAMESPACE and local in {"nonFraction", "fraction"}:
            scale = element.get("scale")
            sign = element.get("sign")
            if scale is not None:
                try:
                    power = int(scale)
                except ValueError as exc:
                    raise SchemaMismatchError("SEC filing XBRL scale is invalid") from exc
                if not -18 <= power <= 18:
                    raise SchemaMismatchError("SEC filing XBRL scale is outside bounds")
                value = _parse_decimal(raw) * (Decimal(10) ** power)
            else:
                value = _parse_decimal(raw)
            if sign == "-":
                value = value.copy_negate()
            elif sign not in {None, "+"}:
                raise SchemaMismatchError("SEC filing XBRL sign is invalid")
        else:
            value = _parse_decimal(raw)
        rows.append(
            SecFilingXbrlFact(
                tag,
                normalized_unit,
                value,
                start,
                end,
                fiscal_year,
                period,
                accession_number,
                form,
                filing_date,
                accepted_at,
            )
        )
    return SecFilingXbrlParse(fiscal_year, period, end_date, tuple(rows))


__all__ = ["SecFilingXbrlFact", "SecFilingXbrlParse", "parse_sec_filing_xbrl_instance"]

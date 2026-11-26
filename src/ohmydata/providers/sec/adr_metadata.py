"""Typed ADR share-basis, reporting-currency, and DEI cover-page share metadata."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from io import BytesIO
from typing import Any

SEC_ADR_METADATA_SCHEMA = "sec-adr-share-metadata-v1"
_ISO_CURRENCY_RE = re.compile(r"[A-Z]{3}\Z")
_SHARE_UNIT_RE = re.compile(r"(?:[A-Za-z0-9_-]+:)?shares?\Z", re.IGNORECASE)
_UNSAFE_XML_DTD_RE = re.compile(rb"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_DEI_SHARES_NORMALIZED = "dei_EntityCommonStockSharesOutstanding"
_FPI_FORMS = frozenset({"20-F", "20-F/A", "40-F", "40-F/A", "6-K"})
_RAW_XML_MAX_BYTES = 16 * 1024 * 1024
_RAW_XML_MAX_ELEMENTS = 200_000
_RAW_XML_MAX_DEPTH = 128


def _is_share_unit(unit: str | None) -> bool:
    return isinstance(unit, str) and bool(_SHARE_UNIT_RE.fullmatch(unit.strip()))


class SecShareBasis(StrEnum):
    """Explicit share-count basis reported in SEC filings."""

    ORDINARY_SHARES = "ordinary_shares"
    ADR_EQUIVALENT_SHARES = "adr_equivalent_shares"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SecDeiSharesMetadata:
    """Cover-page common shares outstanding reported via DEI XBRL facts."""

    shares_outstanding: Decimal
    as_of_date: date | None
    unit: str
    native_tag: str
    context_ref: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.shares_outstanding, Decimal)
            or not self.shares_outstanding.is_finite()
            or self.shares_outstanding < 0
        ):
            raise ValueError("shares_outstanding must be a finite non-negative Decimal")
        if self.as_of_date is not None and type(self.as_of_date) is not date:
            raise ValueError("as_of_date must be a date or None")
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise ValueError("unit must be a non-empty string")
        if not isinstance(self.native_tag, str) or not self.native_tag.strip():
            raise ValueError("native_tag must be a non-empty string")
        if self.context_ref is not None and (
            not isinstance(self.context_ref, str) or not self.context_ref.strip()
        ):
            raise ValueError("context_ref must be a non-empty string or None")
        object.__setattr__(self, "unit", self.unit.strip())
        object.__setattr__(self, "native_tag", self.native_tag.strip())
        if self.context_ref is not None:
            object.__setattr__(self, "context_ref", self.context_ref.strip())

    @property
    def canonical_payload(self) -> dict[str, Any]:
        return {
            "shares_outstanding": str(self.shares_outstanding),
            "as_of_date": self.as_of_date.isoformat() if self.as_of_date is not None else None,
            "unit": self.unit,
            "native_tag": self.native_tag,
            "context_ref": self.context_ref,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SecDeiSharesMetadata:
        expected = {"shares_outstanding", "as_of_date", "unit", "native_tag", "context_ref"}
        if not isinstance(payload, Mapping) or set(payload) != expected:
            raise ValueError("invalid DEI shares metadata payload")
        raw_shares = payload["shares_outstanding"]
        if not isinstance(raw_shares, str) or not raw_shares.strip():
            raise ValueError("DEI shares_outstanding must be a decimal string")
        try:
            shares = Decimal(raw_shares)
        except InvalidOperation as exc:
            raise ValueError("invalid DEI shares_outstanding decimal") from exc
        raw_date = payload["as_of_date"]
        as_of: date | None = None
        if raw_date is not None:
            if not isinstance(raw_date, str) or not raw_date.strip():
                raise ValueError("DEI as_of_date must be an ISO date string or None")
            as_of = date.fromisoformat(raw_date)
        return cls(
            shares_outstanding=shares,
            as_of_date=as_of,
            unit=payload["unit"],
            native_tag=payload["native_tag"],
            context_ref=payload["context_ref"],
        )


@dataclass(frozen=True)
class SecAdrShareMetadata:
    """Explicit reporting currency, share basis, ADR ratio, and DEI cover-page shares."""

    reporting_currency: str | None
    share_basis: SecShareBasis
    ordinary_shares_per_adr: Decimal | None = None
    ratio_source: str | None = None
    dei_shares: SecDeiSharesMetadata | None = None
    _cached_metadata_identity: str | None = field(
        default=None, repr=False, compare=False, init=False
    )

    def __post_init__(self) -> None:
        if self.reporting_currency is not None and (
            not isinstance(self.reporting_currency, str)
            or not _ISO_CURRENCY_RE.fullmatch(self.reporting_currency)
        ):
            raise ValueError(
                "reporting_currency must be a 3-letter uppercase ISO currency code or None"
            )
        if not isinstance(self.share_basis, SecShareBasis):
            raise TypeError("share_basis must be a SecShareBasis member")
        if self.ordinary_shares_per_adr is not None and (
            not isinstance(self.ordinary_shares_per_adr, Decimal)
            or not self.ordinary_shares_per_adr.is_finite()
            or self.ordinary_shares_per_adr <= 0
        ):
            raise ValueError("ordinary_shares_per_adr must be a finite positive Decimal")
        if self.ratio_source is not None:
            if not isinstance(self.ratio_source, str) or not self.ratio_source.strip():
                raise ValueError("ratio_source must be a non-empty string or None")
            object.__setattr__(self, "ratio_source", self.ratio_source.strip())
        if self.dei_shares is not None and not isinstance(self.dei_shares, SecDeiSharesMetadata):
            raise TypeError("dei_shares must be a SecDeiSharesMetadata instance or None")

    @property
    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SEC_ADR_METADATA_SCHEMA,
            "reporting_currency": self.reporting_currency,
            "share_basis": self.share_basis.value,
            "ordinary_shares_per_adr": (
                str(self.ordinary_shares_per_adr)
                if self.ordinary_shares_per_adr is not None
                else None
            ),
            "ratio_source": self.ratio_source,
            "dei_shares": (
                self.dei_shares.canonical_payload if self.dei_shares is not None else None
            ),
        }

    @property
    def metadata_identity(self) -> str:
        cached = self._cached_metadata_identity
        if cached is not None:
            return cached
        digest = hashlib.sha256(
            json.dumps(self.canonical_payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        object.__setattr__(self, "_cached_metadata_identity", digest)
        return digest

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SecAdrShareMetadata:
        expected = {
            "schema_version",
            "reporting_currency",
            "share_basis",
            "ordinary_shares_per_adr",
            "ratio_source",
            "dei_shares",
        }
        if not isinstance(payload, Mapping) or set(payload) != expected:
            raise ValueError("invalid ADR share metadata payload")
        if payload["schema_version"] != SEC_ADR_METADATA_SCHEMA:
            raise ValueError("unknown ADR share metadata schema")
        raw_basis = payload["share_basis"]
        if not isinstance(raw_basis, str) or not raw_basis:
            raise ValueError("share_basis must be a string")
        try:
            basis = SecShareBasis(raw_basis)
        except ValueError as exc:
            raise ValueError("unknown share_basis value") from exc
        raw_ratio = payload["ordinary_shares_per_adr"]
        ratio: Decimal | None = None
        if raw_ratio is not None:
            if not isinstance(raw_ratio, str) or not raw_ratio.strip():
                raise ValueError("ordinary_shares_per_adr must be a decimal string or None")
            try:
                ratio = Decimal(raw_ratio)
            except InvalidOperation as exc:
                raise ValueError("invalid ordinary_shares_per_adr decimal") from exc
        raw_dei = payload["dei_shares"]
        dei = SecDeiSharesMetadata.from_payload(raw_dei) if raw_dei is not None else None
        return cls(
            reporting_currency=payload["reporting_currency"],
            share_basis=basis,
            ordinary_shares_per_adr=ratio,
            ratio_source=payload["ratio_source"],
            dei_shares=dei,
        )


def convert_ordinary_shares_to_adr_equivalent(
    shares: Decimal,
    ordinary_shares_per_adr: Decimal,
) -> Decimal:
    """Explicit opt-in conversion from ordinary shares to ADR-equivalent shares."""
    if not isinstance(shares, Decimal) or not shares.is_finite() or shares < 0:
        raise ValueError("shares must be a finite non-negative Decimal")
    if (
        not isinstance(ordinary_shares_per_adr, Decimal)
        or not ordinary_shares_per_adr.is_finite()
        or ordinary_shares_per_adr <= 0
    ):
        raise ValueError("ordinary_shares_per_adr must be a finite positive Decimal")
    return shares / ordinary_shares_per_adr


def _parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _select_dei_candidate(
    candidates: list[SecDeiSharesMetadata],
) -> SecDeiSharesMetadata | None:
    if not candidates:
        return None
    dated = [item for item in candidates if item.as_of_date is not None]
    pool = dated if dated else candidates
    if dated:
        latest_date = max(item.as_of_date for item in dated if item.as_of_date is not None)
        pool = [item for item in dated if item.as_of_date == latest_date]
    distinct_values = {item.shares_outstanding for item in pool}
    if len(distinct_values) != 1:
        return None
    return min(
        pool,
        key=lambda item: (item.context_ref or "", item.native_tag, item.unit),
    )


def _extract_dei_from_xbrl(xbrl: Any) -> SecDeiSharesMetadata | None:
    parser = getattr(xbrl, "parser", None)
    facts = getattr(parser, "facts", None)
    contexts = getattr(xbrl, "contexts", None) or getattr(parser, "contexts", None)
    units = getattr(xbrl, "units", None) or getattr(parser, "units", None) or {}
    if not isinstance(facts, Mapping) or not isinstance(contexts, Mapping):
        return None

    candidates: list[SecDeiSharesMetadata] = []
    seen_ids: set[int] = set()
    for fact in facts.values():
        if id(fact) in seen_ids:
            continue
        seen_ids.add(id(fact))
        raw_tag = getattr(fact, "element_id", None)
        if not isinstance(raw_tag, str):
            continue
        if raw_tag.replace(":", "_") != _DEI_SHARES_NORMALIZED:
            continue
        context_ref = getattr(fact, "context_ref", None)
        if not isinstance(context_ref, str) or context_ref not in contexts:
            continue
        context = contexts[context_ref]
        dimensions = getattr(context, "dimensions", None)
        if isinstance(dimensions, Mapping) and dimensions:
            continue
        period = getattr(context, "period", None)
        as_of: date | None = None
        if isinstance(period, Mapping):
            as_of = _parse_iso_date(period.get("instant")) or _parse_iso_date(period.get("endDate"))
        try:
            shares = Decimal(str(getattr(fact, "value", "")).strip())
        except (InvalidOperation, ValueError):
            continue
        if not shares.is_finite() or shares < 0:
            continue
        unit_ref = getattr(fact, "unit_ref", None)
        unit_text: str | None = None
        if isinstance(unit_ref, str) and unit_ref.strip():
            unit_def = units.get(unit_ref) if isinstance(units, Mapping) else None
            if isinstance(unit_def, Mapping):
                measure = unit_def.get("measure")
                if (
                    isinstance(measure, str)
                    and "numerator" not in unit_def
                    and "denominator" not in unit_def
                    and _is_share_unit(measure)
                ):
                    unit_text = measure.strip()
            elif unit_def is None and _is_share_unit(unit_ref):
                unit_text = unit_ref.strip()
        if not unit_text:
            continue
        candidates.append(
            SecDeiSharesMetadata(
                shares_outstanding=shares,
                as_of_date=as_of,
                unit=unit_text,
                native_tag=raw_tag.strip(),
                context_ref=context_ref.strip(),
            )
        )
    return _select_dei_candidate(candidates)


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _extract_dei_from_raw_xml(raw_instance: bytes) -> SecDeiSharesMetadata | None:
    if type(raw_instance) is not bytes or len(raw_instance) > _RAW_XML_MAX_BYTES:
        return None
    if _UNSAFE_XML_DTD_RE.search(raw_instance):
        return None
    if b"EntityCommonStockSharesOutstanding" not in raw_instance:
        return None

    contexts: dict[str, tuple[date | None, bool]] = {}
    units: dict[str, str] = {}
    raw_facts: list[tuple[str, str, str, Decimal]] = []

    element_count = 0
    depth = 0
    try:
        for event, elem in ET.iterparse(BytesIO(raw_instance), events=("start", "end")):
            if event == "start":
                element_count += 1
                depth += 1
                if element_count > _RAW_XML_MAX_ELEMENTS or depth > _RAW_XML_MAX_DEPTH:
                    return None
                continue
            depth -= 1
            local = _local_name(elem.tag)
            if local == "context":
                ctx_id = elem.attrib.get("id")
                if ctx_id:
                    has_dim = any(
                        _local_name(child.tag) in {"explicitMember", "typedMember"}
                        for child in elem.iter()
                    )
                    as_of: date | None = None
                    for child in elem.iter():
                        child_local = _local_name(child.tag)
                        if child_local in {"instant", "endDate"} and child.text:
                            as_of = _parse_iso_date(child.text)
                            if as_of is not None:
                                break
                    contexts[ctx_id] = (as_of, has_dim)
                elem.clear()
            elif local == "unit":
                unit_id = elem.attrib.get("id")
                if unit_id:
                    has_divide = any(_local_name(child.tag) == "divide" for child in elem.iter())
                    measures = [
                        child.text.strip()
                        for child in elem.iter()
                        if _local_name(child.tag) == "measure" and child.text and child.text.strip()
                    ]
                    if not has_divide and len(measures) == 1:
                        units[unit_id] = measures[0]
                elem.clear()
            elif local == "EntityCommonStockSharesOutstanding":
                ns_uri = elem.tag[1:].split("}", 1)[0] if elem.tag.startswith("{") else ""
                if ns_uri and "dei" not in ns_uri.lower():
                    elem.clear()
                    continue
                ctx_ref = elem.attrib.get("contextRef", "").strip()
                unit_ref = elem.attrib.get("unitRef", "").strip()
                text = (elem.text or "").strip()
                if ctx_ref and unit_ref and text:
                    try:
                        value = Decimal(text)
                    except InvalidOperation:
                        value = Decimal("NaN")
                    if value.is_finite() and value >= 0:
                        raw_facts.append(
                            (
                                "dei:EntityCommonStockSharesOutstanding",
                                ctx_ref,
                                unit_ref,
                                value,
                            )
                        )
                elem.clear()
    except ET.ParseError:
        return None

    candidates: list[SecDeiSharesMetadata] = []
    for native_tag, ctx_ref, unit_ref, value in raw_facts:
        ctx_info = contexts.get(ctx_ref)
        if ctx_info is None:
            continue
        as_of_date, has_dim = ctx_info
        if has_dim:
            continue
        unit = units.get(unit_ref)
        if not _is_share_unit(unit):
            continue
        assert unit is not None
        candidates.append(
            SecDeiSharesMetadata(
                shares_outstanding=value,
                as_of_date=as_of_date,
                unit=unit,
                native_tag=native_tag,
                context_ref=ctx_ref,
            )
        )
    return _select_dei_candidate(candidates)


def extract_dei_shares_metadata(
    xbrl: Any | None = None,
    *,
    raw_instance: bytes | None = None,
) -> SecDeiSharesMetadata | None:
    """Extract DEI cover-page common shares outstanding without guessing ratios."""
    if xbrl is not None:
        extracted = _extract_dei_from_xbrl(xbrl)
        if extracted is not None:
            return extracted
    if raw_instance is not None:
        return _extract_dei_from_raw_xml(raw_instance)
    return None


def extract_reporting_currency(rows: Sequence[Any]) -> str | None:
    """Return the unique bare ISO-4217 currency observed across statement rows, or None."""
    currencies = {
        currency
        for row in rows
        if isinstance((currency := getattr(row, "currency", None)), str)
        and _ISO_CURRENCY_RE.fullmatch(currency)
    }
    if len(currencies) == 1:
        return next(iter(currencies))
    return None


def build_adr_share_metadata(
    rows: Sequence[Any],
    *,
    xbrl: Any | None = None,
    raw_instance: bytes | None = None,
    form: str | None = None,
) -> SecAdrShareMetadata | None:
    """Build ADR/DEI share metadata when DEI shares, FPI form, or non-USD currency is observed."""
    reporting_currency = extract_reporting_currency(rows)
    dei_shares = extract_dei_shares_metadata(xbrl, raw_instance=raw_instance)
    is_fpi = isinstance(form, str) and form.strip().upper() in _FPI_FORMS
    is_non_usd = reporting_currency is not None and reporting_currency != "USD"
    if dei_shares is None and not is_non_usd and not (is_fpi and reporting_currency is not None):
        return None
    return SecAdrShareMetadata(
        reporting_currency=reporting_currency,
        share_basis=SecShareBasis.UNKNOWN,
        ordinary_shares_per_adr=None,
        ratio_source=None,
        dei_shares=dei_shares,
    )


__all__ = [
    "SEC_ADR_METADATA_SCHEMA",
    "SecAdrShareMetadata",
    "SecDeiSharesMetadata",
    "SecShareBasis",
    "build_adr_share_metadata",
    "convert_ordinary_shares_to_adr_equivalent",
    "extract_dei_shares_metadata",
    "extract_reporting_currency",
]

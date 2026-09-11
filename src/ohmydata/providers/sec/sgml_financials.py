"""Offline production of SEC financial rows from retained full SGML filings."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import cast
from zoneinfo import ZoneInfo

from ...core import RequestSpec
from ...core.snapshot import SnapshotObservationRef, SnapshotStore
from ._observed_xbrl_units import decode_raw_units
from ._pit_projection import _decode_projection
from .edgartools_adapter import ensure_edgar_available, parse_statement_rows
from .financials import SecCompanyFinancialVintage, SecStatementRow, StatementType
from .pit import SecNormalizedFinancialFactVersion, _version_from_replayed_projection

_EASTERN = ZoneInfo("America/New_York")
_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_CIK = re.compile(r"^[0-9]{10}$")
_FORMS = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})
_STATEMENTS = frozenset({"balance_sheet", "income_statement", "cash_flow"})
_COMPONENTS = frozenset({"EX-101.SCH", "EX-101.PRE", "EX-101.LAB", "EX-101.INS"})
_PARSER_VERSION_V1 = "sec-sgml-financial-parser-v1-edgartools-5.56.0"
_PARSER_VERSION_V2 = "sec-sgml-financial-parser-v2-edgartools-5.56.0"
_PARSER_VERSIONS = frozenset({_PARSER_VERSION_V1, _PARSER_VERSION_V2})
_ADAPTER_VERSIONS = {
    _PARSER_VERSION_V1: "sec-sgml-financial-adapter-v1",
    _PARSER_VERSION_V2: "sec-sgml-financial-adapter-v2",
}
_NORMALIZATION_VERSION = "sec-financial-normalized-v1"
_PROJECTION_SERIALIZATION = "sec-financial-typed-rows-projection-v1"
_NORMALIZED_SCHEMA_VERSION = "sec-financial-normalized-v1"
_SERIALIZATION = "sec-filing-sgml-v1"


def _utc(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _positive(value: int, name: str, maximum: int) -> int:
    if type(value) is not int or value <= 0 or value > maximum:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class SecSgmlFinancialsRequest:
    symbol: str
    cik: str
    accession_number: str
    form: str
    statement_types: tuple[str, ...]
    include_dimensions: bool

    def __post_init__(self) -> None:
        if type(self.symbol) is not str or not self.symbol.strip() or len(self.symbol) > 128:
            raise ValueError("symbol is required")
        if type(self.cik) is not str or _CIK.fullmatch(self.cik) is None:
            raise ValueError("cik must be a 10-digit string")
        if (
            type(self.accession_number) is not str
            or _ACCESSION.fullmatch(self.accession_number) is None
        ):
            raise ValueError("invalid accession_number")
        if self.form not in _FORMS:
            raise ValueError("unsupported form")
        if (
            type(self.statement_types) is not tuple
            or not self.statement_types
            or len(set(self.statement_types)) != len(self.statement_types)
        ):
            raise ValueError("statement_types must be nonempty and unique")
        if any(item not in _STATEMENTS for item in self.statement_types):
            raise ValueError("unsupported statement type")
        if type(self.include_dimensions) is not bool:
            raise TypeError("include_dimensions must be bool")


@dataclass(frozen=True)
class SecSgmlFinancialProduction:
    request: SecSgmlFinancialsRequest
    source_observation: SnapshotObservationRef
    projection_observation: SnapshotObservationRef
    vintage: SecCompanyFinancialVintage
    versions: tuple[SecNormalizedFinancialFactVersion, ...]
    parser_version: str = field(default=_PARSER_VERSION_V1, kw_only=True)


def _header(raw: str, request: SecSgmlFinancialsRequest) -> tuple[datetime, date, date, str]:
    first_document = raw.find("<DOCUMENT>")
    if raw.count("<SEC-HEADER>") != 1 or raw.count("</SEC-HEADER>") != 1 or first_document < 0:
        raise ValueError("unsupported or incomplete SEC header")
    start, end = raw.find("<SEC-HEADER>"), raw.find("</SEC-HEADER>")
    if start < 0 or end < start or end > first_document:
        raise ValueError("unsupported or incomplete SEC header")
    header = raw[start + len("<SEC-HEADER>") : end].replace("\r\n", "\n")
    fields = {
        "ACCESSION NUMBER": r"(?m)^[ \t]*ACCESSION NUMBER:[ \t]*(\S[^\r\n]*)$",
        "CONFORMED SUBMISSION TYPE": r"(?m)^[ \t]*CONFORMED SUBMISSION TYPE:[ \t]*(\S[^\r\n]*)$",
        "FILED AS OF DATE": r"(?m)^[ \t]*FILED AS OF DATE:[ \t]*([0-9]{8})[ \t]*$",
        "CONFORMED PERIOD OF REPORT": r"(?m)^[ \t]*CONFORMED PERIOD OF REPORT:[ \t]*([0-9]{8})[ \t]*$",
        "CENTRAL INDEX KEY": r"(?m)^[ \t]*CENTRAL INDEX KEY:[ \t]*([0-9]{1,10})[ \t]*$",
        "ACCEPTANCE-DATETIME": r"(?m)^[ \t]*<ACCEPTANCE-DATETIME>([0-9]{14})[ \t]*$",
    }
    values: dict[str, str] = {}
    for name, pattern in fields.items():
        label = re.escape(name if name != "ACCEPTANCE-DATETIME" else f"<{name}>")
        if len(re.findall(rf"(?m)^[ \t]*{label}", header)) != 1:
            raise ValueError(f"missing, duplicate, or malformed SEC header {name}")
        found = re.findall(pattern, header)
        if len(found) != 1:
            raise ValueError(f"missing, duplicate, or malformed SEC header {name}")
        values[name] = found[0].strip()
    company_name_labels = (
        "COMPANY CONFORMED NAME",
        "CONFORMED NAME",
    )
    label_count = sum(
        len(re.findall(rf"(?m)^[ \t]*{re.escape(label)}", header)) for label in company_name_labels
    )
    company_name_matches = [
        match
        for label in company_name_labels
        for match in re.findall(rf"(?m)^[ \t]*{re.escape(label)}:[ \t]*([^\r\n]*)$", header)
    ]
    if label_count != 1 or len(company_name_matches) != 1 or not company_name_matches[0].strip():
        raise ValueError("missing, duplicate, or malformed SEC header COMPANY/CONFORMED NAME")
    company_name = company_name_matches[0].strip()
    if (
        values["ACCESSION NUMBER"] != request.accession_number
        or values["CONFORMED SUBMISSION TYPE"] != request.form
    ):
        raise ValueError("SEC header identity does not match request")
    cik = values["CENTRAL INDEX KEY"].zfill(10)
    if _CIK.fullmatch(cik) is None or cik != request.cik:
        raise ValueError("SEC header CIK does not match request")
    try:
        filed = date.fromisoformat(values["FILED AS OF DATE"])
        period = date.fromisoformat(values["CONFORMED PERIOD OF REPORT"])
        wall = datetime.strptime(  # noqa: DTZ007 -- SEC header is a timezone-less wall time.
            values["ACCEPTANCE-DATETIME"], "%Y%m%d%H%M%S"
        )
    except ValueError as exc:
        raise ValueError("malformed SEC header date") from exc
    first = wall.replace(tzinfo=_EASTERN, fold=0)
    second = wall.replace(tzinfo=_EASTERN, fold=1)
    if (
        first.utcoffset() != second.utcoffset()
        or first.astimezone(UTC).astimezone(_EASTERN).replace(tzinfo=None) != wall
    ):
        raise ValueError("ambiguous or nonexistent SEC acceptance time")
    return first.astimezone(UTC), filed, period, company_name


def _documents(raw: str, *, require_traditional: bool = True) -> dict[str, str]:
    if raw.count("<DOCUMENT>") != raw.count("</DOCUMENT>"):
        raise ValueError("unbalanced SEC document blocks")
    blocks = re.findall(r"<DOCUMENT>\s*(.*?)</DOCUMENT>", raw, flags=re.DOTALL)
    if not blocks or len(blocks) > 64 or len(blocks) != raw.count("<DOCUMENT>"):
        raise ValueError("invalid SEC document count")
    found: dict[str, str] = {}
    for block in blocks:
        if "<DOCUMENT>" in block or "</DOCUMENT>" in block:
            raise ValueError("nested SEC document block")
        kinds = re.findall(r"(?m)^\s*<TYPE>\s*([^\n\r<]+)\s*$", block)
        texts = re.findall(r"<TEXT>(.*?)</TEXT>", block, flags=re.DOTALL)
        if (
            len(kinds) != 1
            or len(texts) != 1
            or block.count("<TEXT>") != 1
            or block.count("</TEXT>") != 1
        ):
            raise ValueError("malformed SEC document block")
        kind, text = kinds[0], texts[0]
        name = kind.strip().upper()
        if name in _COMPONENTS | {"EX-101.CAL", "EX-101.DEF"}:
            if name in found:
                raise ValueError("duplicate required XBRL component")
            found[name] = _component_text(text)
    missing = _COMPONENTS - set(found)
    if require_traditional and missing:
        raise ValueError("missing required traditional XBRL component")
    return found


def _component_text(text: str) -> str:
    """Remove one exact SGML XBRL wrapper without changing retained raw bytes."""
    component = text.strip()
    opening = "<XBRL>"
    closing = "</XBRL>"
    if not (component.startswith(opening) or component.endswith(closing)):
        return component
    if (
        component.count(opening) != 1
        or component.count(closing) != 1
        or not component.startswith(opening)
        or not component.endswith(closing)
    ):
        raise ValueError("malformed SEC XBRL wrapper")
    unwrapped = component[len(opening) : -len(closing)].strip()
    if not unwrapped:
        raise ValueError("malformed SEC XBRL wrapper")
    return unwrapped


def _validate_xml(text: str) -> int:
    if re.search(r"<!DOCTYPE|<!ENTITY", text, flags=re.IGNORECASE):
        raise ValueError("unsafe XML declaration")
    from xml.etree import ElementTree

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError("malformed embedded XBRL XML") from exc
    count = 0
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > 200_000:
            raise ValueError("embedded XBRL XML element limit exceeded")
        if depth > 128:
            raise ValueError("embedded XBRL XML depth limit exceeded")
        stack.extend((child, depth + 1) for child in element)
    return count


def _validate_instance_identity(text: str, cik: str) -> None:
    from xml.etree import ElementTree

    root = ElementTree.fromstring(text)
    local = lambda tag: tag.rsplit("}", 1)[-1]
    contexts = [item for item in root.iter() if local(item.tag) == "context"]
    if not contexts:
        raise ValueError("XBRL instance contexts are missing")
    for context in contexts:
        entities = [item for item in context if local(item.tag) == "entity"]
        identifiers = [
            item for entity in entities for item in entity if local(item.tag) == "identifier"
        ]
        if len(identifiers) != 1:
            raise ValueError("XBRL context must contain one CIK identifier")
        identifier = identifiers[0]
        value = (identifier.text or "").strip().zfill(10)
        if (
            identifier.get("scheme") != "http://www.sec.gov/CIK"
            or _CIK.fullmatch(value) is None
            or value != cik
        ):
            raise ValueError("XBRL context CIK does not match SEC header")
    for item in (item for item in root.iter() if local(item.tag) == "EntityCentralIndexKey"):
        value = (item.text or "").strip().zfill(10)
        if _CIK.fullmatch(value) is None or value != cik:
            raise ValueError("DEI EntityCentralIndexKey does not match SEC header")


def _rows_from_documents(
    raw: str, documents: dict[str, str], request: SecSgmlFinancialsRequest, max_rows: int
) -> tuple[SecStatementRow, ...]:
    ensure_edgar_available()
    try:
        installed = version("edgartools")
    except PackageNotFoundError as exc:
        raise ImportError("edgartools 5.56.0 is required for SEC SGML financials") from exc
    if installed != "5.56.0":
        raise RuntimeError("SEC SGML financial parser requires edgartools 5.56.0")
    from edgar.financials import Financials
    from edgar.sgml.sgml_common import FilingSGML
    from edgar.xbrl import XBRL

    if sum(_validate_xml(text) for text in documents.values()) > 200_000:
        raise ValueError("embedded XBRL XML aggregate element limit exceeded")
    _validate_instance_identity(documents["EX-101.INS"], request.cik)
    FilingSGML.from_text(raw)
    xbrl = XBRL()
    xbrl.parser.parse_schema_content(documents["EX-101.SCH"])
    xbrl.parser.parse_labels_content(documents["EX-101.LAB"])
    xbrl.parser.parse_presentation_content(documents["EX-101.PRE"])
    for name, parser in (
        ("EX-101.CAL", xbrl.parser.parse_calculation_content),
        ("EX-101.DEF", xbrl.parser.parse_definition_content),
    ):
        if name in documents:
            parser(documents[name])
    xbrl.parser.parse_instance_content(documents["EX-101.INS"])
    financials = Financials(xbrl)
    getters = {
        "balance_sheet": financials.balance_sheet,
        "income_statement": financials.income_statement,
        "cash_flow": financials.cash_flow_statement,
    }
    rows: list[SecStatementRow] = []
    for statement_type in request.statement_types:
        statement = getters[statement_type](include_dimensions=request.include_dimensions)
        if statement is None:
            raise ValueError(f"requested SEC statement is missing: {statement_type}")
        parsed = parse_statement_rows(
            statement,
            cast(StatementType, statement_type),
            include_dimensions=request.include_dimensions,
        )
        if not parsed:
            raise ValueError(f"requested SEC statement is empty: {statement_type}")
        if len(rows) + len(parsed) > max_rows:
            raise ValueError("SEC financial row limit exceeded")
        rows.extend(parsed)
    return tuple(rows)


def _rows(
    raw: str, request: SecSgmlFinancialsRequest, max_rows: int
) -> tuple[SecStatementRow, ...]:
    return _rows_from_documents(raw, _documents(raw), request, max_rows)


def produce_sec_financials_from_sgml(
    *,
    source_store: SnapshotStore,
    source_observation: SnapshotObservationRef,
    projection_store: SnapshotStore,
    request: SecSgmlFinancialsRequest,
    produced_at: datetime,
    max_raw_bytes: int = 8 * 1024 * 1024,
    max_rows: int = 10_000,
    parser_version: str = _PARSER_VERSION_V2,
) -> SecSgmlFinancialProduction:
    """Build replay-bound rows from one retained full SEC SGML submission."""
    _positive(max_raw_bytes, "max_raw_bytes", 8 * 1024 * 1024)
    _positive(max_rows, "max_rows", 10_000)
    if type(parser_version) is not str or parser_version not in _PARSER_VERSIONS:
        raise ValueError("unsupported SEC SGML financial parser version")
    produced = _utc(produced_at, "produced_at")
    expected = RequestSpec(
        "sec",
        "company-filing-sgml",
        {"cik": request.cik, "accession_number": request.accession_number, "form": request.form},
    )
    if source_observation.serialization_identifier != _SERIALIZATION:
        raise ValueError("source observation is not SEC filing SGML")
    replay = source_store.replay_observation(source_observation, expected, max_raw_bytes)
    try:
        raw = replay.payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SEC filing SGML must be UTF-8") from exc
    accepted, filed, period, company_name = _header(raw, request)
    if not accepted <= source_observation.snapshot_fetched_at <= produced:
        raise ValueError("SEC acceptance and production times are not causal")
    if parser_version == _PARSER_VERSION_V1:
        rows = _rows(raw, request, max_rows)
    else:
        documents = _documents(raw)
        units = decode_raw_units(
            documents["EX-101.INS"].encode("utf-8"), max_elements=200_000, max_depth=128
        )
        native_rows = _rows_from_documents(raw, documents, request, max_rows)
        rows_: list[SecStatementRow] = []
        for row in native_rows:
            if row.unit_ref is None:
                raise ValueError("selected raw XBRL unit reference is missing")
            try:
                rows_.append(replace(row, unit=units[row.unit_ref]))
            except KeyError as exc:
                raise ValueError("selected raw XBRL unit reference is missing") from exc
        rows = tuple(rows_)
    vintage = SecCompanyFinancialVintage(
        symbol=request.symbol,
        cik=request.cik,
        company_name=company_name,
        form=request.form,
        accession_number=request.accession_number,
        filing_date=filed,
        period_end=period,
        accepted_at=accepted,
        availability_policy="accepted-at-plus-lag",
        availability_lag_days=0,
        is_amendment=request.form.endswith("/A"),
        rows=rows,
    )
    config = _hash(
        {
            "parser_version": parser_version,
            "statement_types": request.statement_types,
            "include_dimensions": request.include_dimensions,
        }
    )
    from .pit import serialize_sec_typed_rows_projection

    payload = serialize_sec_typed_rows_projection(
        vintage,
        source_artifact_identity=source_observation.fact_version,
        source_available_at=accepted,
    )
    if len(payload) > 8 * 1024 * 1024:
        raise ValueError("SEC typed-row projection exceeds limit")
    projection = projection_store.observe(
        RequestSpec("sec", "financial-typed-rows", {"accession": request.accession_number}, ()),
        payload,
        produced,
        _PROJECTION_SERIALIZATION,
    )
    decoded = _decode_projection(payload)
    versions = tuple(
        _version_from_replayed_projection(
            observation=projection,
            projection=decoded,
            source_available_at=accepted,
            accession_number=request.accession_number,
            vintage_identity=vintage.vintage_identity,
            row_ordinal=index,
            expected_row=row,
            schema_version=_NORMALIZED_SCHEMA_VERSION,
            adapter_version=_ADAPTER_VERSIONS[parser_version],
            normalization_version=_NORMALIZATION_VERSION,
            configuration_identity=config,
            recorded_at=produced,
        )
        for index, row in enumerate(rows)
    )
    return SecSgmlFinancialProduction(
        request, source_observation, projection, vintage, versions, parser_version=parser_version
    )


__all__ = [
    "SecSgmlFinancialProduction",
    "SecSgmlFinancialsRequest",
    "produce_sec_financials_from_sgml",
]

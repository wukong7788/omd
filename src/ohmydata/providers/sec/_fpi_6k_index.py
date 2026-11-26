"""Filing-index parsing, document fetching, and submissions discovery for Form 6-K."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ._companyfacts_models import _accepted, _date
from .edgar import historical_basenames
from .errors import CoverageError, ResourceLimitError, SchemaMismatchError, TransientProviderError
from .http import SecHttpClient, validate_sec_url

_INDEX_BYTES = 256 * 1024
_MAX_HTML_TAGS = 10_000
_MAX_HISTORY_PAGES = 16
_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_BASENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_LINKBASE_SUFFIXES = ("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml")
_SKIP_XML_NAMES = frozenset(
    {"filingsummary.xml", "metalinks.xml", "report.xml", "financial_report.xml"}
)
_R_REPORT_XML = re.compile(r"^r[0-9]+\.xml$")


@dataclass(frozen=True)
class _Filing6K:
    accession: str
    form: str
    filing_date: date
    report_date: date | None
    accepted_at: datetime
    primary_document: str | None


@dataclass(frozen=True)
class SecFpi6KIndexDocuments:
    """Categorized safe document basenames discovered from a 6-K ``-index.htm``."""

    xbrl_documents: tuple[str, ...]
    inline_xbrl_documents: tuple[str, ...]
    exhibit_documents: tuple[tuple[str, str], ...]


class _IndexHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.rows: list[list[tuple[str, str | None]]] = []
        self._row: list[tuple[str, str | None]] | None = None
        self._cell: list[str] | None = None
        self._href: str | None = None
        self._tags = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tags += 1
        if self._tags > _MAX_HTML_TAGS:
            raise ResourceLimitError("SEC 6-K index HTML structure limit exceeded")
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell, self._href = [], None
        elif tag == "a" and self._cell is not None:
            self._href = dict(attrs).get("href")

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append((" ".join(" ".join(self._cell).split()), self._href))
            self._cell, self._href = None, None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def _exact_url(expected: str, actual: str) -> str:
    if validate_sec_url(expected) != validate_sec_url(actual):
        raise SchemaMismatchError("SEC 6-K source redirect changed URL")
    return actual


def _fetch_sec_document(client: SecHttpClient, url: str, accept: str, limit: int) -> bytes:
    response = client.open(
        url,
        accept=accept,
        max_bytes=limit,
        redirect_validator=lambda actual: _exact_url(url, actual),
    )
    try:
        _exact_url(url, response.url)
        try:
            body = response.body.read()
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise TransientProviderError("SEC 6-K source response read failed") from exc
    finally:
        response.body.close()
    if type(body) is not bytes or not 0 < len(body) <= limit:
        raise ResourceLimitError("SEC 6-K source exceeds byte limit")
    return body


def _validate_index_href(href: str, expected_path: str) -> bool:
    if href == expected_path:
        return True
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or parsed.path != "/ix":
        return False
    return parse_qs(parsed.query).get("doc") == [expected_path]


def _xbrl_xml_priority(name: str, row_type: str) -> int | None:
    """Return priority (0=primary instance, 1=fallback XML) or None if not an XBRL instance."""
    lower = name.lower()
    if not lower.endswith(".xml"):
        return None
    if (
        lower in _SKIP_XML_NAMES
        or lower.endswith(_LINKBASE_SUFFIXES)
        or _R_REPORT_XML.fullmatch(lower) is not None
    ):
        return None
    if row_type == "EX-101.INS" or lower.endswith("_htm.xml"):
        return 0
    if row_type == "XML":
        return 1
    return None


def parse_fpi_6k_index(body: bytes, cik: str, accession: str) -> SecFpi6KIndexDocuments:
    """Parse a Form 6-K ``-index.htm`` into XBRL, inline-XBRL, and EX-99.1 candidates."""
    if type(body) is not bytes or not 0 < len(body) <= _INDEX_BYTES:
        raise ResourceLimitError("SEC 6-K index exceeds byte limit")
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaMismatchError("SEC 6-K index must be UTF-8") from exc
    parsed = _IndexHtmlParser()
    parsed.feed(html)
    parsed.close()
    text = " ".join(" ".join(parsed.text).split())
    if f"SEC Accession No. {accession}" not in text:
        raise SchemaMismatchError("SEC 6-K index accession mismatch")

    compact = accession.replace("-", "")
    dir_prefix = f"/Archives/edgar/data/{int(cik)}/{compact}/"
    primary_xbrl_docs: list[str] = []
    fallback_xbrl_docs: list[str] = []
    ixbrl_docs: list[str] = []
    exhibit_docs: list[tuple[str, str]] = []

    for row in parsed.rows:
        if len(row) < 4:
            continue
        raw_name, href = row[2]
        row_type = row[3][0].strip().upper()
        name = raw_name.split()[0] if raw_name else ""
        if not name or href is None:
            continue
        xml_prio = _xbrl_xml_priority(name, row_type)
        is_ix_href = href.startswith("/ix?")
        is_ex99 = row_type in {"EX-99.1", "EX-99", "EX-99.01"}
        if xml_prio is None and not (is_ix_href or is_ex99):
            continue
        if _BASENAME.fullmatch(name) is None:
            raise SchemaMismatchError("SEC 6-K index document basename is invalid")
        expected_path = f"{dir_prefix}{name}"
        if not _validate_index_href(href, expected_path):
            raise SchemaMismatchError("SEC 6-K index document link is unsafe or mismatched")
        if xml_prio == 0:
            if name not in primary_xbrl_docs:
                primary_xbrl_docs.append(name)
        elif xml_prio == 1:
            if name not in fallback_xbrl_docs:
                fallback_xbrl_docs.append(name)
        elif (
            is_ix_href
            and name.lower().endswith((".htm", ".html", ".xhtml"))
            and name not in ixbrl_docs
        ):
            ixbrl_docs.append(name)
        if is_ex99 and name.lower().endswith((".htm", ".html")):
            entry = (name, row_type)
            if entry not in exhibit_docs:
                exhibit_docs.append(entry)

    ordered_xbrl = primary_xbrl_docs + [d for d in fallback_xbrl_docs if d not in primary_xbrl_docs]
    return SecFpi6KIndexDocuments(
        xbrl_documents=tuple(ordered_xbrl),
        inline_xbrl_documents=tuple(ixbrl_docs),
        exhibit_documents=tuple(exhibit_docs),
    )


def _select_history_pages(
    root_payload: dict[str, Any],
    cik: str,
    *,
    start_year: int | None,
    end_year: int | None,
) -> tuple[str, ...]:
    refs = root_payload.get("filings", {}).get("files")
    if not isinstance(refs, (list, tuple)) or not refs:
        return ()
    names = set(historical_basenames(root_payload, cik))
    selected: list[tuple[date, str]] = []
    for raw in refs:
        if not isinstance(raw, Mapping):
            raise SchemaMismatchError("SEC historical reference schema mismatch")
        name = raw.get("name")
        if type(name) is not str or name not in names:
            raise SchemaMismatchError("SEC historical reference name mismatch")
        start_raw, end_raw = raw.get("filingFrom"), raw.get("filingTo")
        if type(start_raw) is not str or type(end_raw) is not str:
            raise CoverageError(
                f"SEC historical reference {name!r} cannot be range-qualified: missing filingFrom or filingTo"
            )
        start, end = _date(start_raw, "filingFrom"), _date(end_raw, "filingTo")
        if start > end:
            raise SchemaMismatchError("SEC historical filing range is reversed")
        if start_year is not None and end.year < start_year:
            continue
        if end_year is not None and start.year > end_year + 1:
            continue
        selected.append((end, name))
    selected.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if len(selected) > _MAX_HISTORY_PAGES:
        raise ResourceLimitError("SEC 6-K history page count exceeds limit")
    return tuple(name for _, name in selected)


def _discover_6k_filings(
    rows: list[dict[str, Any]],
    *,
    start_year: int | None,
    end_year: int | None,
    bound: datetime | None,
) -> list[_Filing6K]:
    filings: list[_Filing6K] = []
    seen: set[str] = set()
    for row in rows:
        form = row.get("form")
        if form not in {"6-K", "6-K/A"}:
            continue
        accession = row.get("accessionNumber")
        if type(accession) is not str or _ACCESSION.fullmatch(accession) is None:
            raise SchemaMismatchError("SEC 6-K accession is invalid")
        if accession in seen:
            continue
        seen.add(accession)
        filing_date = _date(row.get("filingDate"), "filingDate")
        raw_report = row.get("reportDate")
        report_date = _date(raw_report, "reportDate") if raw_report else None
        accepted_at = _accepted(row.get("acceptanceDateTime"))
        if bound is not None and accepted_at > bound:
            continue
        effective_year = report_date.year if report_date is not None else filing_date.year
        if start_year is not None and effective_year < start_year:
            continue
        if end_year is not None and effective_year > end_year:
            continue
        raw_doc = row.get("primaryDocument")
        primary_doc = raw_doc if type(raw_doc) is str and raw_doc else None
        filings.append(
            _Filing6K(
                accession=accession,
                form=str(form),
                filing_date=filing_date,
                report_date=report_date,
                accepted_at=accepted_at,
                primary_document=primary_doc,
            )
        )
    filings.sort(key=lambda f: (f.accepted_at, f.accession))
    return filings


def _validate_year_bounds(start_year: int | None, end_year: int | None) -> None:
    for value, label in ((start_year, "start_year"), (end_year, "end_year")):
        if value is not None and (type(value) is not int or not 1 <= value <= 9999):
            raise ValueError(f"{label} must be an integer in 1..9999")
    if start_year is not None and end_year is not None and start_year > end_year:
        raise ValueError("start_year must be <= end_year")


def _validate_utc_bound(acceptance_upper: datetime | None) -> datetime | None:
    if acceptance_upper is None:
        return None
    if (
        not isinstance(acceptance_upper, datetime)
        or acceptance_upper.tzinfo is None
        or acceptance_upper.utcoffset() is None
    ):
        raise ValueError("acceptance_upper must be timezone-aware")
    return acceptance_upper.astimezone(UTC)


__all__ = [
    "_INDEX_BYTES",
    "SecFpi6KIndexDocuments",
    "_Filing6K",
    "_discover_6k_filings",
    "_fetch_sec_document",
    "_select_history_pages",
    "_validate_utc_bound",
    "_validate_year_bounds",
    "parse_fpi_6k_index",
]

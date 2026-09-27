"""Discovery, history closure, and index parsing for SEC Form 8-K Item 2.02 filings."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

from ._companyfacts_models import _accepted, _date
from .edgar import historical_basenames
from .errors import CoverageError, ResourceLimitError, SchemaMismatchError, TransientProviderError
from .http import SecHttpClient, validate_sec_url

_INDEX_BYTES = 128 * 1024
_EXHIBIT_BYTES = 2 * 1024 * 1024
_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_BASENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_RELEASE_SEARCH_DAYS = 90
_MAX_HISTORY_PAGES = 16
_MAX_HISTORY_BYTES = 64 * 1024**2


@dataclass(frozen=True)
class _Filing8K:
    accession: str
    form: str
    filing_date: date
    report_date: date
    accepted_at: datetime


class _IndexHtml(HTMLParser):
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
        if self._tags > 10_000:
            raise ResourceLimitError("SEC 8-K index HTML structure limit exceeded")
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
        raise SchemaMismatchError("SEC 8-K source redirect changed URL")
    return actual


def _fetch_html(client: SecHttpClient, url: str, limit: int) -> bytes:
    response = client.open(
        url,
        accept="text/html",
        max_bytes=limit,
        redirect_validator=lambda actual: _exact_url(url, actual),
    )
    try:
        _exact_url(url, response.url)
        try:
            body = response.body.read()
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise TransientProviderError("SEC 8-K HTML response read failed") from exc
    finally:
        response.body.close()
    if type(body) is not bytes or not 0 < len(body) <= limit:
        raise ResourceLimitError("SEC 8-K HTML source exceeds byte limit")
    return body


def _exhibit_basenames(body: bytes, cik: str, accession: str) -> tuple[str, ...]:
    if not 0 < len(body) <= _INDEX_BYTES:
        raise ResourceLimitError("SEC 8-K index exceeds byte limit")
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaMismatchError("SEC 8-K index must be UTF-8") from exc
    parsed = _IndexHtml()
    parsed.feed(html)
    parsed.close()
    text = " ".join(" ".join(parsed.text).split())
    if f"SEC Accession No. {accession}" not in text:
        raise SchemaMismatchError("SEC 8-K index accession mismatch")
    matches: list[str] = []
    for row in parsed.rows:
        if len(row) < 4:
            continue
        row_type = row[3][0].strip().upper()
        if row_type not in {"EX-99.1", "EX-99", "EX-99.01", "EX-99.2"}:
            continue
        name, href = row[2]
        if _BASENAME.fullmatch(name) is None or href is None:
            raise SchemaMismatchError("SEC 8-K exhibit document is invalid")
        expected_path = f"/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{name}"
        if href != expected_path or not name.endswith((".htm", ".html")):
            raise SchemaMismatchError("SEC 8-K exhibit link is unsafe or mismatched")
        if name not in matches:
            matches.append(name)
    return tuple(matches)


def _validate_target_quarters(
    target_quarters: tuple[tuple[int, int, date], ...],
) -> None:
    if not isinstance(target_quarters, tuple) or not 1 <= len(target_quarters) <= 8:
        raise ValueError("target_quarters must be a non-empty tuple of at most 8 quarters")
    seen_keys: set[tuple[int, int]] = set()
    seen_dates: set[date] = set()
    for item in target_quarters:
        if not isinstance(item, tuple) or len(item) != 3:
            raise ValueError(
                "target_quarters items must be (fiscal_year, fiscal_quarter, period_end)"
            )
        fy, fq, pend = item
        if type(fy) is not int or not 1 <= fy <= 9999:
            raise ValueError("fiscal_year must be an integer in 1..9999")
        if type(fq) is not int or fq not in {1, 2, 3, 4}:
            raise ValueError("fiscal_quarter must be an integer in 1..4")
        if not isinstance(pend, date) or isinstance(pend, datetime):
            raise TypeError("period_end must be a date instance")
        key = (fy, fq)
        if key in seen_keys:
            raise ValueError(f"duplicate fiscal quarter key: {key}")
        seen_keys.add(key)
        if pend in seen_dates:
            raise ValueError(f"duplicate period_end date: {pend}")
        seen_dates.add(pend)


def _target_windows(
    target_quarters: tuple[tuple[int, int, date], ...],
) -> tuple[tuple[date, date], ...]:
    return tuple(
        (pend, pend + timedelta(days=_RELEASE_SEARCH_DAYS)) for _, _, pend in target_quarters
    )


def _targeted_history_pages(
    root_payload: dict[str, Any],
    cik: str,
    target_quarters: tuple[tuple[int, int, date], ...],
) -> tuple[str, ...]:
    refs = root_payload.get("filings", {}).get("files")
    if not isinstance(refs, (list, tuple)):
        return ()
    names = set(historical_basenames(root_payload, cik))
    selected: set[str] = set()
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
        for _, _, pend in target_quarters:
            win_start = pend
            win_end = pend + timedelta(days=_RELEASE_SEARCH_DAYS)
            if start <= win_end and end >= win_start:
                selected.add(name)
                break
    if len(selected) > _MAX_HISTORY_PAGES:
        raise ResourceLimitError("targeted SEC history page count exceeds limit")
    return tuple(sorted(selected))


def _discover_8k_filings(
    rows: list[dict[str, Any]],
    bound: datetime | None,
    target_windows: tuple[tuple[date, date], ...] | None = None,
) -> list[_Filing8K]:
    filings: list[_Filing8K] = []
    seen: set[str] = set()
    for row in rows:
        form = row.get("form")
        if form not in {"8-K", "8-K/A"}:
            continue
        raw_items = row.get("items")
        if not raw_items:
            continue
        item_list = (
            raw_items
            if isinstance(raw_items, list)
            else [it.strip() for it in str(raw_items).split(",")]
        )
        if "2.02" not in item_list:
            continue
        accession = row.get("accessionNumber")
        if type(accession) is not str or _ACCESSION.fullmatch(accession) is None:
            raise SchemaMismatchError("SEC 8-K accession is invalid")
        if accession in seen:
            continue
        seen.add(accession)
        filing_date = _date(row.get("filingDate"), "filingDate")
        if target_windows is not None:
            in_window = any(start <= filing_date <= end for start, end in target_windows)
            if not in_window:
                continue
        report_date = _date(row.get("reportDate"), "reportDate")
        accepted_at = _accepted(row.get("acceptanceDateTime"))
        if bound is not None and accepted_at > bound:
            continue
        filings.append(_Filing8K(accession, str(form), filing_date, report_date, accepted_at))
    filings.sort(key=lambda f: f.accepted_at)
    return filings


__all__ = [
    "_EXHIBIT_BYTES",
    "_INDEX_BYTES",
    "_MAX_HISTORY_BYTES",
    "_RELEASE_SEARCH_DAYS",
    "_Filing8K",
    "_discover_8k_filings",
    "_exhibit_basenames",
    "_fetch_html",
    "_target_windows",
    "_targeted_history_pages",
    "_validate_target_quarters",
]

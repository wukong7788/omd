"""SEC-only TSMC quarterly earnings releases filed as Form 6-K exhibits."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser

from ...core import RequestSpec, SnapshotMode, SnapshotStore
from ._companyfacts_models import _accepted, _date
from ._companyfacts_submissions import _root_payload
from ._tsm_6k_parse import SecTsm6KValues, parse_sec_tsm_6k_release
from ._tsm_6k_usd import SecTsm6KEstimatedUsdValues, estimate_sec_tsm_6k_usd_from_revenue
from .errors import CoverageError, ResourceLimitError, SchemaMismatchError, TransientProviderError
from .event_discovery import _submission_rows
from .http import SecHttpClient, validate_sec_url
from .submissions import fetch_sec_submissions_root
from .ticker_cik import fetch_sec_ticker_cik_mapping

_TSM_CIK = "0001046179"
_INDEX_BYTES = 128 * 1024
_RELEASE_BYTES = 128 * 1024
_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_RELEASE_DOCUMENT = re.compile(r"^tsm-([0-9]{8})x6k\.htm$")
_BASENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


@dataclass(frozen=True)
class SecTsm6KQuarter:
    """One exact 6-K filing and its SEC-retained earnings-release evidence."""

    values: SecTsm6KValues
    accession_number: str
    filing_date: date
    accepted_at: datetime
    index_url: str
    exhibit_url: str
    index_observation_id: str
    exhibit_observation_id: str


@dataclass(frozen=True)
class SecTsm6KQuarterlyResult:
    """Chronological quarters with explicit gaps in the eight-quarter window."""

    ticker: str
    cik: str
    quarters: tuple[SecTsm6KQuarter, ...]
    mapping_observation_id: str
    submissions_observation_id: str
    coverage_complete: bool
    missing_period_ends: tuple[date, ...]


@dataclass(frozen=True)
class _Filing:
    accession: str
    period_end: date
    filing_date: date
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


def _previous_quarter_end(value: date) -> date:
    return {
        3: date(value.year - 1, 12, 31),
        6: date(value.year, 3, 31),
        9: date(value.year, 6, 30),
        12: date(value.year, 9, 30),
    }[value.month]


def _filings(root: dict[str, object], cik: str, bound: datetime | None) -> dict[date, _Filing]:
    result: dict[date, _Filing] = {}
    for row in _submission_rows(root, cik, child=False):
        if row.get("form") != "6-K":
            continue
        raw_document = row.get("primaryDocument")
        if type(raw_document) is not str:
            continue
        document = _RELEASE_DOCUMENT.fullmatch(raw_document)
        if document is None:
            continue
        accession = row.get("accessionNumber")
        if type(accession) is not str or _ACCESSION.fullmatch(accession) is None:
            raise SchemaMismatchError("TSM 6-K release accession is invalid")
        period_end = _date(row.get("reportDate"), "reportDate")
        if (period_end.month, period_end.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
            continue
        filing_date = _date(row.get("filingDate"), "filingDate")
        if document[1] != filing_date.strftime("%Y%m%d"):
            raise SchemaMismatchError("TSM 6-K document date differs from filingDate")
        if not 0 <= (filing_date - period_end).days <= 31:
            raise SchemaMismatchError("TSM 6-K release filingDate is outside quarter window")
        accepted_at = _accepted(row.get("acceptanceDateTime"))
        if bound is not None and accepted_at > bound:
            continue
        if period_end in result:
            raise SchemaMismatchError("multiple TSM 6-K earnings releases for one quarter")
        result[period_end] = _Filing(accession, period_end, filing_date, accepted_at)
    return result


def _exact_url(expected: str, actual: str) -> str:
    if validate_sec_url(expected) != validate_sec_url(actual):
        raise SchemaMismatchError("SEC 6-K source redirect changed URL")
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
            raise TransientProviderError("SEC 6-K HTML response read failed") from exc
    finally:
        response.body.close()
    if type(body) is not bytes or not 0 < len(body) <= limit:
        raise ResourceLimitError("SEC 6-K HTML source exceeds byte limit")
    return body


def _exhibit_basename(body: bytes, cik: str, accession: str) -> str:
    if not 0 < len(body) <= _INDEX_BYTES:
        raise ResourceLimitError("SEC 6-K index exceeds byte limit")
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaMismatchError("SEC 6-K index must be UTF-8") from exc
    parsed = _IndexHtml()
    parsed.feed(html)
    parsed.close()
    text = " ".join(" ".join(parsed.text).split())
    if f"SEC Accession No. {accession}" not in text:
        raise SchemaMismatchError("SEC 6-K index accession mismatch")
    matches: list[str] = []
    for row in parsed.rows:
        if len(row) < 4 or row[3][0] != "EX-99.1":
            continue
        name, href = row[2]
        if _BASENAME.fullmatch(name) is None or href is None:
            raise SchemaMismatchError("SEC 6-K exhibit document is invalid")
        expected_path = f"/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{name}"
        if href != expected_path or not name.endswith(".htm"):
            raise SchemaMismatchError("SEC 6-K exhibit link is unsafe or mismatched")
        matches.append(name)
    if len(matches) != 1:
        raise SchemaMismatchError("SEC 6-K EX-99.1 is missing or ambiguous")
    return matches[0]


def fetch_sec_tsm_6k_quarters(
    client: SecHttpClient,
    store: SnapshotStore,
    *,
    count: int = 8,
    acceptance_upper: datetime | None = None,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SecTsm6KQuarterlyResult:
    """Fetch up to eight TSMC SEC 6-K earnings releases with source units intact.

    This is deliberately TSMC-specific. It does not classify other ADR issuers,
    convert TWD to USD, or equate an ADR with an ordinary share.
    """
    if not isinstance(client, SecHttpClient) or not isinstance(store, SnapshotStore):
        raise TypeError("client and store must be SEC HTTP and snapshot clients")
    if type(count) is not int or not 1 <= count <= 8:
        raise ValueError("count must be in 1..8")
    if not callable(utc_now):
        raise TypeError("utc_now must be callable")
    if acceptance_upper is not None and (
        not isinstance(acceptance_upper, datetime)
        or acceptance_upper.tzinfo is None
        or acceptance_upper.utcoffset() is None
    ):
        raise ValueError("acceptance_upper must be timezone-aware")
    bound = acceptance_upper.astimezone(UTC) if acceptance_upper is not None else None
    mapping = fetch_sec_ticker_cik_mapping(client, store, utc_now=utc_now)
    resolved = mapping.resolve("TSM")
    if resolved.cik != _TSM_CIK:
        raise CoverageError("SEC TSM ticker no longer resolves to the validated issuer CIK")
    root_source = fetch_sec_submissions_root(store, client, resolved.cik, utc_now=utc_now)
    root = _root_payload(store, root_source, resolved.cik)
    available = _filings(root, resolved.cik, bound)
    if not available:
        return SecTsm6KQuarterlyResult(
            "TSM",
            resolved.cik,
            (),
            mapping.observation.observation_identity,
            root_source.observation.observation_identity,
            False,
            (),
        )
    anchor = max(available)
    requested: list[date] = []
    current = anchor
    for _ in range(count):
        requested.append(current)
        current = _previous_quarter_end(current)
    missing = tuple(sorted(set(requested) - set(available)))
    quarters: list[SecTsm6KQuarter] = []
    for period_end in sorted(set(requested) & set(available)):
        filing = available[period_end]
        base = (
            f"https://www.sec.gov/Archives/edgar/data/{int(resolved.cik)}/"
            f"{filing.accession.replace('-', '')}"
        )
        index_url = f"{base}/{filing.accession}-index.htm"
        index_body = _fetch_html(client, index_url, _INDEX_BYTES)
        document = _exhibit_basename(index_body, resolved.cik, filing.accession)
        index_observation = store.observe(
            RequestSpec(
                "sec", "tsm_6k_index", {"cik": resolved.cik, "accession": filing.accession}
            ),
            index_body,
            utc_now(),
            "sec-tsm-6k-index-html-v1",
            SnapshotMode.APPEND,
        )
        exhibit_url = f"{base}/{document}"
        exhibit_body = _fetch_html(client, exhibit_url, _RELEASE_BYTES)
        values = parse_sec_tsm_6k_release(exhibit_body, expected_period_end=period_end)
        exhibit_observation = store.observe(
            RequestSpec(
                "sec",
                "tsm_6k_release",
                {"cik": resolved.cik, "accession": filing.accession, "document": document},
            ),
            exhibit_body,
            utc_now(),
            "sec-tsm-6k-release-html-v1",
            SnapshotMode.APPEND,
        )
        quarters.append(
            SecTsm6KQuarter(
                values,
                filing.accession,
                filing.filing_date,
                filing.accepted_at,
                index_url,
                exhibit_url,
                index_observation.observation_identity,
                exhibit_observation.observation_identity,
            )
        )
    return SecTsm6KQuarterlyResult(
        "TSM",
        resolved.cik,
        tuple(quarters),
        mapping.observation.observation_identity,
        root_source.observation.observation_identity,
        not missing,
        missing,
    )


__all__ = [
    "SecTsm6KEstimatedUsdValues",
    "SecTsm6KQuarter",
    "SecTsm6KQuarterlyResult",
    "SecTsm6KValues",
    "estimate_sec_tsm_6k_usd_from_revenue",
    "fetch_sec_tsm_6k_quarters",
    "parse_sec_tsm_6k_release",
]

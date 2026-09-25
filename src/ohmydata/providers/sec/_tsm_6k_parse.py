"""Bounded parser for TSMC's SEC-filed quarterly 6-K earnings exhibit."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from html.parser import HTMLParser

from .errors import ResourceLimitError, SchemaMismatchError

_MAX_RELEASE_BYTES = 128 * 1024
_MAX_HTML_TAGS = 10_000
_NUMBER = re.compile(r"^([0-9][0-9,]*(?:\.[0-9]+)?)(?:\s+[a-z])?$", re.IGNORECASE)
_INTRO = re.compile(
    r"today announced consolidated revenue of NT\$(?P<revenue>[0-9,.]+) billion, "
    r"net income of NT\$(?P<net_income>[0-9,.]+) billion, and diluted earnings per share "
    r"of NT\$(?P<eps_twd>[0-9,.]+) \(US\$(?P<eps_adr_usd>[0-9,.]+) per ADR unit\) for the "
    r"(?P<ordinal>first|second|third|fourth) quarter ended "
    r"(?P<month>March|June|September|December) (?P<day>[0-9]{1,2}), "
    r"(?P<year>[0-9]{4})\.",
    re.IGNORECASE,
)
_USD_REVENUE = re.compile(
    r"In US dollars, (?:first|second|third|fourth) quarter revenue was "
    r"\$([0-9,.]+) billion",
    re.IGNORECASE,
)
_MARGINS = re.compile(
    r"Gross margin for the quarter was ([0-9.]+)%, operating margin was ([0-9.]+)%",
    re.IGNORECASE,
)
_ORDINAL = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_MONTH = {"march": 3, "june": 6, "september": 9, "december": 12}
_END_DAY = {3: 31, 6: 30, 9: 30, 12: 31}
_REQUIRED_ROWS = (
    "Net sales",
    "Gross profit",
    "Income from operations",
    "Income before tax",
    "Net income",
    "EPS (NT$)",
)


@dataclass(frozen=True)
class SecTsm6KValues:
    """Exact EX-99.1 row labels/units; net income is the release's row.

    USD/ADR EPS is not USD/ordinary-share EPS. TWD amounts are not translated.
    """

    period_end: date
    fiscal_year: int
    fiscal_quarter: int
    revenue_twd_million: Decimal
    gross_profit_twd_million: Decimal
    operating_income_twd_million: Decimal
    income_before_tax_twd_million: Decimal
    net_income_twd_million: Decimal
    diluted_eps_twd_per_ordinary_share: Decimal
    revenue_usd_billion: Decimal
    diluted_eps_usd_per_adr: Decimal
    gross_margin_pct: Decimal
    operating_margin_pct: Decimal


class _ReleaseHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.tables: list[list[list[str]]] = []
        self._table_stack: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._skip = 0
        self._tags = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tags += 1
        if self._tags > _MAX_HTML_TAGS:
            raise ResourceLimitError("TSM 6-K exhibit HTML structure limit exceeded")
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag == "table":
            self._table_stack.append([])
        elif tag == "tr" and self._table_stack:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.text.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)
        elif tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(_space(" ".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table_stack:
            self._table_stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._table_stack:
            self.tables.append(self._table_stack.pop())


def _space(value: str) -> str:
    return " ".join(value.split())


def _one(pattern: re.Pattern[str], text: str, name: str) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise SchemaMismatchError(f"TSM 6-K {name} is missing or ambiguous")
    return matches[0]


def _number(value: str, name: str) -> Decimal:
    match = _NUMBER.fullmatch(value)
    if match is None:
        raise SchemaMismatchError(f"TSM 6-K {name} is invalid")
    parsed = Decimal(match[1].replace(",", ""))
    if not parsed.is_finite() or parsed < 0:
        raise SchemaMismatchError(f"TSM 6-K {name} is invalid")
    return parsed


def parse_sec_tsm_6k_release(body: bytes, *, expected_period_end: date) -> SecTsm6KValues:
    """Parse one SEC EX-99.1 earnings release, preserving every native unit.

    Only the current-quarter column is read. Comparative and guidance columns
    never stand in for the requested quarter. Inconsistent disclosure fails.
    """
    if type(body) is not bytes or not 0 < len(body) <= _MAX_RELEASE_BYTES:
        raise ResourceLimitError("TSM 6-K exhibit exceeds byte limit")
    if type(expected_period_end) is not date:
        raise TypeError("expected_period_end must be a date")
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaMismatchError("TSM 6-K exhibit must be UTF-8") from exc
    parsed = _ReleaseHtml()
    try:
        parsed.feed(html)
        parsed.close()
    except (ValueError, AssertionError) as exc:
        raise SchemaMismatchError("TSM 6-K exhibit HTML is invalid") from exc
    text = _space(" ".join(parsed.text))
    if not re.search(r"TSMC Reports (?:First|Second|Third|Fourth) Quarter EPS", text):
        raise SchemaMismatchError("TSM 6-K exhibit is not an earnings release")
    if "All figures were prepared in accordance with TIFRS on a consolidated basis." not in text:
        raise SchemaMismatchError("TSM 6-K accounting and consolidation basis is missing")
    if "(Unit: NT$ million, except for EPS)" not in text:
        raise SchemaMismatchError("TSM 6-K earnings table unit is missing")
    intro = _one(_INTRO, text, "quarterly disclosure")
    usd_revenue = _one(_USD_REVENUE, text, "USD revenue")
    margins = _one(_MARGINS, text, "margins")
    quarter = _ORDINAL[intro.group("ordinal").lower()]
    month = _MONTH[intro.group("month").lower()]
    try:
        reported_end = date(int(intro.group("year")), month, int(intro.group("day")))
    except ValueError as exc:
        raise SchemaMismatchError("TSM 6-K quarter date is invalid") from exc
    if (
        month != quarter * 3
        or reported_end.day != _END_DAY[month]
        or reported_end != expected_period_end
    ):
        raise SchemaMismatchError("TSM 6-K quarter does not match SEC report date")
    heading = re.compile(
        rf"^{quarter}Q{reported_end.year % 100:02d} Amount(?:\s+[a-z])?$", re.IGNORECASE
    )
    matching_tables: list[list[list[str]]] = []
    for table in parsed.tables:
        labels = {row[0] for row in table if row}
        if set(_REQUIRED_ROWS).issubset(labels) and any(
            len(row) > 1 and heading.fullmatch(row[1]) for row in table
        ):
            matching_tables.append(table)
    if len(matching_tables) != 1:
        raise SchemaMismatchError("TSM 6-K current-quarter earnings table is missing or ambiguous")
    rows: dict[str, Decimal] = {}
    for row in matching_tables[0]:
        if row and row[0] in _REQUIRED_ROWS:
            if row[0] in rows or len(row) < 2:
                raise SchemaMismatchError("TSM 6-K earnings row is duplicated or incomplete")
            rows[row[0]] = _number(row[1], row[0])
    revenue_twd = rows["Net sales"]
    gross_twd = rows["Gross profit"]
    operating_twd = rows["Income from operations"]
    pretax_twd = rows["Income before tax"]
    net_twd = rows["Net income"]
    eps_twd = rows["EPS (NT$)"]
    if revenue_twd <= 0 or gross_twd > revenue_twd or operating_twd > revenue_twd:
        raise SchemaMismatchError("TSM 6-K earnings table values are inconsistent")
    intro_twd_billion = _number(intro.group("revenue"), "TWD revenue")
    intro_net_twd_billion = _number(intro.group("net_income"), "TWD net income")
    intro_eps_twd = _number(intro.group("eps_twd"), "TWD diluted EPS")
    eps_adr_usd = _number(intro.group("eps_adr_usd"), "USD per ADR")
    revenue_usd_billion = _number(usd_revenue[1], "USD revenue")
    gross_margin = _number(margins[1], "gross margin")
    operating_margin = _number(margins[2], "operating margin")
    with localcontext() as context:
        context.prec = 34
        gross_calculated = (gross_twd / revenue_twd * 100).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
        operating_calculated = (operating_twd / revenue_twd * 100).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
    if (
        abs(revenue_twd / 1000 - intro_twd_billion) > Decimal("0.005")
        or abs(net_twd / 1000 - intro_net_twd_billion) > Decimal("0.005")
        or eps_twd != intro_eps_twd
        or gross_margin != gross_calculated
        or operating_margin != operating_calculated
    ):
        raise SchemaMismatchError("TSM 6-K prose and current-quarter table disagree")
    return SecTsm6KValues(
        reported_end,
        reported_end.year,
        quarter,
        revenue_twd,
        gross_twd,
        operating_twd,
        pretax_twd,
        net_twd,
        eps_twd,
        revenue_usd_billion,
        eps_adr_usd,
        gross_margin,
        operating_margin,
    )


__all__ = ["SecTsm6KValues", "parse_sec_tsm_6k_release"]

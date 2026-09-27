"""Bounded HTML parser for SEC Form 8-K Item 2.02 earnings release exhibits."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from .errors import ResourceLimitError, SchemaMismatchError

_MAX_EXHIBIT_BYTES = 2 * 1024 * 1024
_MAX_HTML_TAGS = 30_000
_MAX_TABLES = 100

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_EXCLUDED_HEADER_TERMS = frozenset(
    {
        "year ended",
        "years ended",
        "twelve months",
        "twelve month",
        "nine months",
        "nine month",
        "six months",
        "six month",
        "guidance",
        "outlook",
        "forecast",
        "projected",
        "constant currency",
        "non-gaap",
        "adjusted",
        "pro forma",
        "pro-forma",
    }
)

_EXCLUDED_LABEL_TERMS = frozenset(
    {
        "non-gaap",
        "adjusted",
        "excluding",
        "exclusion",
        "one-time",
        "guidance",
        "outlook",
        "forecast",
        "projected",
        "constant currency",
        "pro forma",
        "pro-forma",
        "free cash flow",
        "shares",
        "weighted",
        "average",
        "dividend",
    }
)

_QUARTER_DATE_PATTERN = re.compile(
    r"(?:three\s+months|quarter|fourth\s+quarter|4th\s+quarter|q4)\s+ended\s*([a-z]+)\.?\s+([0-9]{1,2}),?\s+([0-9]{4})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Sec8KEpsCandidate:
    """One candidate GAAP diluted EPS value extracted from an exhibit table."""

    period_end: date
    value: Decimal
    native_label: str
    table_index: int


class Sec8KEpsAmbiguousError(SchemaMismatchError):
    """Multiple conflicting GAAP diluted EPS candidates found in an SEC 8-K exhibit."""

    def __init__(
        self,
        message: str,
        period_end: date,
        candidates: tuple[Sec8KEpsCandidate, ...],
    ) -> None:
        super().__init__(message)
        self.period_end = period_end
        self.candidates = candidates


class _GridTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._cur_grid: list[list[str]] = []
        self._cur_row = 0
        self._cur_col = 0
        self._cell_text: list[str] = []
        self._in_cell = False
        self._cell_colspan = 1
        self._tags = 0
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tags += 1
        if self._tags > _MAX_HTML_TAGS:
            raise ResourceLimitError("SEC 8-K exhibit HTML structure limit exceeded")
        if tag in {"script", "style"}:
            self._skip += 1
        elif not self._skip:
            if tag == "table":
                if len(self.tables) >= _MAX_TABLES:
                    raise ResourceLimitError("SEC 8-K exhibit table count limit exceeded")
                self._cur_grid = []
                self._cur_row = 0
            elif tag == "tr":
                self._cur_col = 0
                if len(self._cur_grid) <= self._cur_row:
                    self._cur_grid.append([])
            elif tag in {"td", "th"}:
                attr_dict = dict(attrs)
                raw_span = attr_dict.get("colspan")
                if raw_span is not None:
                    try:
                        self._cell_colspan = max(1, min(64, int(raw_span)))
                    except ValueError:
                        self._cell_colspan = 1
                else:
                    self._cell_colspan = 1
                self._cell_text = []
                self._in_cell = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)
        elif not self._skip:
            if tag in {"td", "th"}:
                self._in_cell = False
                text = " ".join("".join(self._cell_text).split())
                row = self._cur_grid[self._cur_row]
                while len(row) <= self._cur_col:
                    row.append("")
                for _ in range(self._cell_colspan):
                    while len(row) <= self._cur_col:
                        row.append("")
                    row[self._cur_col] = text
                    self._cur_col += 1
            elif tag == "tr":
                self._cur_row += 1
                self._cur_col = 0
            elif tag == "table":
                if any(any(c for c in r) for r in self._cur_grid):
                    self.tables.append(self._cur_grid)

    def handle_data(self, data: str) -> None:
        if self._in_cell and not self._skip:
            self._cell_text.append(data)


def _parse_decimal_value(text: str) -> Decimal | None:
    cleaned = text.strip().replace("$", "").replace("\xa0", "").replace(",", "").replace(" ", "")
    if not cleaned:
        return None
    # Strip trailing footnote markers like (1) or (3) when preceded by a digit
    m_foot = re.fullmatch(r"^([+-]?[0-9]+(?:\.[0-9]+)?)\([0-9]+\)$", cleaned)
    if m_foot:
        cleaned = m_foot.group(1)
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    # Disallow range values e.g. "1.77-1.79"
    if "-" in cleaned[1:]:
        return None
    try:
        val = Decimal(cleaned)
        return val if val.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def parse_sec_8k_earnings_release(
    body: bytes,
    *,
    expected_period_end: date | None = None,
) -> dict[date, tuple[Decimal, str]]:
    """Parse direct GAAP diluted EPS from an SEC 8-K Item 2.02 earnings exhibit.

    Extracts GAAP diluted EPS matching expected discrete quarter ends.
    Raises:
    - Sec8KEpsAmbiguousError (subclass of SchemaMismatchError) if multiple
      candidate tables or rows yield conflicting GAAP diluted EPS values for the
      same period.
    - SchemaMismatchError if the exhibit is malformed, not valid UTF-8, or
      exhibits invalid table structure.
    - ResourceLimitError if byte, tag, or table limits are exceeded.

    Malformed UTF-8, schema mismatches, and resource failures propagate to callers
    and are not converted to AMBIGUOUS.

    Returns a mapping of period_end -> (value, native_label).
    """
    if type(body) is not bytes or not 0 < len(body) <= _MAX_EXHIBIT_BYTES:
        raise ResourceLimitError("SEC 8-K exhibit HTML source exceeds byte limit")
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaMismatchError("SEC 8-K exhibit must be valid UTF-8") from exc

    parser = _GridTableParser()
    parser.feed(html)
    parser.close()

    candidates_by_period: dict[date, list[Sec8KEpsCandidate]] = {}

    for t_idx, grid in enumerate(parser.tables):
        if not grid:
            continue
        max_cols = max(len(r) for r in grid)
        if max_cols < 2:
            continue

        col_periods: dict[int, date] = {}
        for c in range(max_cols):
            header_parts = [
                grid[r][c] for r in range(min(12, len(grid))) if c < len(grid[r]) and grid[r][c]
            ]
            col_text = " ".join(header_parts)
            col_lower = col_text.lower()
            if any(term in col_lower for term in _EXCLUDED_HEADER_TERMS):
                continue
            m = _QUARTER_DATE_PATTERN.search(col_lower)
            if m:
                mon = _MONTHS.get(m.group(1).lower().rstrip("."))
                day = int(m.group(2))
                yr = int(m.group(3))
                if mon is not None:
                    try:
                        col_periods[c] = date(yr, mon, day)
                    except ValueError:
                        continue

        if not col_periods:
            continue

        cur_section = ""
        for r_idx, r in enumerate(grid):
            raw_label = ""
            for cell in r[:3]:
                if cell:
                    raw_label = cell
                    break
            if not raw_label:
                continue

            label_lower = raw_label.lower()
            if any(term in label_lower for term in _EXCLUDED_LABEL_TERMS):
                # When label explicitly declares a non-GAAP / guidance section, track it
                if any(k in label_lower for k in ("non-gaap", "adjusted", "guidance")):
                    cur_section = raw_label
                continue

            if any(
                k in label_lower
                for k in ("section", "net income per share", "per share", "diluted")
            ):
                cur_section = raw_label

            if any(term in cur_section.lower() for term in ("non-gaap", "adjusted", "guidance")):
                continue

            # The row label itself must indicate a diluted per-share metric
            is_diluted = "diluted" in label_lower
            is_basic = "basic" in label_lower
            if not is_diluted or is_basic:
                continue
            # Must not be share count or non-per-share row
            if any(
                term in label_lower
                for term in ("shares", "weighted", "average", "count", "dividend")
            ):
                continue
            # Must be a per share metric or under a per-share section
            has_per_share = (
                "per share" in label_lower
                or "eps" in label_lower
                or "per share" in cur_section.lower()
                or "eps" in cur_section.lower()
            )
            if not has_per_share:
                continue

            # Column scan for this row
            for col_idx, period_dt in col_periods.items():
                if expected_period_end is not None and period_dt != expected_period_end:
                    continue
                val: Decimal | None = None
                if col_idx < len(r):
                    val = _parse_decimal_value(r[col_idx])
                    # SEC tables may place a currency marker in its own cell,
                    # or omit the empty label header so a single period header
                    # occupies the metric-label cell. Consume the next cell
                    # only when the header proves a shared period or the current
                    # cell is the diluted-EPS label and the next cell is not a
                    # separately dated column. Never shift across a blank cell
                    # or into an adjacent comparative-period column.
                    if (
                        val is None
                        and col_idx + 1 < len(r)
                        and (
                            (
                                r[col_idx].strip() in {"$", "US$"}
                                and col_periods.get(col_idx + 1) == period_dt
                            )
                            or (is_diluted and has_per_share and col_idx + 1 not in col_periods)
                        )
                    ):
                        val = _parse_decimal_value(r[col_idx + 1])
                if val is not None:
                    candidates_by_period.setdefault(period_dt, []).append(
                        Sec8KEpsCandidate(period_dt, val, raw_label, t_idx)
                    )

    resolved: dict[date, tuple[Decimal, str]] = {}
    for period_dt, cand_list in candidates_by_period.items():
        distinct_values = {cand.value for cand in cand_list}
        if len(distinct_values) > 1:
            raise Sec8KEpsAmbiguousError(
                f"SEC 8-K exhibit has conflicting GAAP diluted EPS candidates for {period_dt}: {distinct_values}",
                period_dt,
                tuple(cand_list),
            )
        winner = cand_list[0]
        resolved[period_dt] = (winner.value, winner.native_label)

    return resolved


__all__ = [
    "Sec8KEpsAmbiguousError",
    "Sec8KEpsCandidate",
    "parse_sec_8k_earnings_release",
]

"""Multi-metric HTML table parser (Revenue, Gross Profit, GAAP Diluted EPS) for Form 8-K exhibits."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum

from ._earnings_8k_parse import (
    _EXCLUDED_HEADER_TERMS,
    _MAX_EXHIBIT_BYTES,
    _MONTHS,
    Sec8KEpsAmbiguousError,
    _GridTableParser,
    _parse_decimal_value,
    parse_sec_8k_earnings_release,
)
from .errors import ResourceLimitError, SchemaMismatchError

_QUARTER_HEADER_PATTERN = re.compile(
    r"(?:three\s+months|(?:(first|1st|second|2nd|third|3rd|fourth|4th)\s+)?quarter|(q[1-4]))"
    r"\s+ended\s*([a-z]+)\.?\s+([0-9]{1,2}),?\s+([0-9]{4})",
    re.IGNORECASE,
)

_ORDINAL_TO_Q = {
    "first": 1,
    "1st": 1,
    "q1": 1,
    "second": 2,
    "2nd": 2,
    "q2": 2,
    "third": 3,
    "3rd": 3,
    "q3": 3,
    "fourth": 4,
    "4th": 4,
    "q4": 4,
}
_MONETARY_EXCLUDED_HEADER_TERMS = _EXCLUDED_HEADER_TERMS | frozenset(
    {
        "yoy",
        "year-over-year",
        "year over year",
        "% change",
        "change",
        "basis points",
        "bps",
        "margin",
        "%",
    }
)
_EXCLUDED_TABLE_CONTEXT_TERMS = frozenset(
    {
        "guidance",
        "outlook",
        "forecast",
        "projected",
        "constant currency",
        "pro forma",
        "pro-forma",
        "non-gaap",
        "adjusted",
        "segment",
        "disaggregated",
        "by region",
        "by geography",
        "geographic",
        "by product",
    }
)
_EXCLUDED_SECTION_TERMS = _EXCLUDED_TABLE_CONTEXT_TERMS | frozenset({"reconciliation"})

_NON_USD_CURRENCY_PATTERN = re.compile(
    r"(?:€|£|¥|(?<![a-z])nt\$|\b(?:cny|rmb|eur|gbp|jpy|cad|twd|dkk|chf"
    r"|euros?|pounds?|sterling|yen|renminbi|yuan|nt\s*dollars?|canadian\s*dollars?)\b)",
    re.IGNORECASE,
)

_EXCEPT_CLAUSE = re.compile(r"\bexcept\b[^)\n;]*", re.IGNORECASE)
_SCALE_BILLIONS = re.compile(
    r"\b(?:(?:dollars?|\$|u\.?s\.?\s*dollars?|usd)\s+)?in\s+billions?\b|\$\s*in\s+billions?\b",
    re.IGNORECASE,
)
_SCALE_MILLIONS = re.compile(
    r"\b(?:(?:dollars?|\$|u\.?s\.?\s*dollars?|usd)\s+)?in\s+millions?\b|\$\s*in\s+millions?\b",
    re.IGNORECASE,
)
_SCALE_THOUSANDS = re.compile(
    r"\b(?:(?:dollars?|\$|u\.?s\.?\s*dollars?|usd)\s+)?in\s+thousands?\b|\$\s*in\s+thousands?\b",
    re.IGNORECASE,
)
_SCALE_EXACT_DOLLARS = re.compile(
    r"\b(?:in\s+(?:exact\s+|actual\s+|whole\s+)?(?:u\.?s\.?\s*)?(?:dollars?|usd)|"
    r"actual\s+(?:u\.?s\.?\s*)?dollars?|"
    r"exact\s+(?:u\.?s\.?\s*)?dollars?)\b"
    r"(?!\s*,?\s*(?:in\s+)?(?:thousands?|millions?|billions?))",
    re.IGNORECASE,
)

_FOOTNOTE_SUFFIX = re.compile(r"(?:\s*(?:\([0-9a-zA-Z*†‡]+\)|\[[0-9a-zA-Z*]+\]|[*†‡]+))+$")
_REVENUE_LABELS = frozenset(
    {
        "revenue",
        "revenues",
        "total revenue",
        "total revenues",
        "net revenue",
        "net revenues",
        "net sales",
        "total net revenue",
        "total net revenues",
        "total net sales",
        "gaap revenue",
        "gaap revenues",
        "total gaap revenue",
        "total gaap revenues",
        "gaap net revenue",
        "gaap net revenues",
        "consolidated revenue",
        "consolidated revenues",
    }
)
_GROSS_PROFIT_LABELS = frozenset(
    {
        "gross profit",
        "total gross profit",
        "gaap gross profit",
        "total gaap gross profit",
        "consolidated gross profit",
    }
)


class Sec8KMetric(str, Enum):
    """Canonical GAAP quarterly metrics supported by direct Form 8-K exhibit parsing."""

    REVENUE = "REVENUE"
    GROSS_PROFIT = "GROSS_PROFIT"
    GAAP_DILUTED_EPS = "GAAP_DILUTED_EPS"


@dataclass(frozen=True)
class Sec8KMetricCandidate:
    """One direct GAAP metric candidate extracted from an 8-K Item 2.02 exhibit table."""

    metric: Sec8KMetric
    period_end: date
    value: Decimal
    unit: str
    native_label: str
    table_index: int
    declared_scale: str | None = None
    explicit_quarter: int | None = None


@dataclass(frozen=True)
class Sec8KMetricAmbiguity:
    """Explicit fail-closed ambiguity for one metric and period within an exhibit."""

    metric: Sec8KMetric
    period_end: date
    reason: str
    candidates: tuple[Sec8KMetricCandidate, ...]


@dataclass(frozen=True)
class Sec8KParsedMetrics:
    """Per-exhibit parsed direct metrics and explicit ambiguities."""

    values: dict[tuple[Sec8KMetric, date], Sec8KMetricCandidate]
    ambiguities: dict[tuple[Sec8KMetric, date], Sec8KMetricAmbiguity]


class Sec8KMetricAmbiguousError(SchemaMismatchError):
    """Conflicting direct GAAP metric candidates found in an SEC 8-K exhibit."""

    def __init__(
        self,
        message: str,
        metric: Sec8KMetric,
        period_end: date,
        candidates: tuple[Sec8KMetricCandidate, ...],
    ) -> None:
        super().__init__(message)
        self.metric = metric
        self.period_end = period_end
        self.candidates = candidates


def _detect_monetary_scale(
    table_context: str,
    header_text: str,
) -> tuple[Decimal | None, str | None, str | None]:
    """Return ``(factor, scale_label, ambiguity_reason)`` for a monetary table.

    Strictly detects ``in billions``, ``in millions``, ``in thousands``, or
    explicit bare currency (``in dollars`` / ``in USD``). Strips ``except ...``
    clauses (e.g. ``except share counts in thousands and per share data``).
    Returns ``(None, None, None)`` if undeclared, or ``(None, None, reason)`` if
    contradictory scale declarations appear.
    """
    combined = f"{table_context} {header_text}".strip()
    if _NON_USD_CURRENCY_PATTERN.search(combined):
        return None, None, None
    sanitized = _EXCEPT_CLAUSE.sub("", combined)
    matched: list[tuple[Decimal, str]] = []
    if _SCALE_BILLIONS.search(sanitized):
        matched.append((Decimal(1_000_000_000), "in billions"))
    if _SCALE_MILLIONS.search(sanitized):
        matched.append((Decimal(1_000_000), "in millions"))
    if _SCALE_THOUSANDS.search(sanitized):
        matched.append((Decimal(1_000), "in thousands"))
    if _SCALE_EXACT_DOLLARS.search(sanitized):
        matched.append((Decimal(1), "in dollars"))

    if not matched:
        return None, None, None
    if len(matched) > 1:
        labels = ", ".join(label for _, label in matched)
        return None, None, f"contradictory monetary scale headers in 8-K table: {labels}"
    return matched[0][0], matched[0][1], None


def _normalize_row_label(raw_label: str) -> str:
    cleaned = " ".join(raw_label.strip().split())
    cleaned = _FOOTNOTE_SUFFIX.sub("", cleaned).strip()
    cleaned = cleaned.rstrip(":").strip()
    return cleaned.lower()


def _classify_monetary_label(raw_label: str) -> Sec8KMetric | None:
    lower = raw_label.lower()
    if "%" in lower or "margin" in lower or "percent" in lower:
        return None
    normalized = _normalize_row_label(raw_label)
    if normalized in _REVENUE_LABELS:
        return Sec8KMetric.REVENUE
    if normalized in _GROSS_PROFIT_LABELS:
        return Sec8KMetric.GROSS_PROFIT
    return None


def _parse_monetary_cell(text: str) -> Decimal | None:
    if "%" in text or _NON_USD_CURRENCY_PATTERN.search(text):
        return None
    return _parse_decimal_value(text)


def _extract_quarter_columns(
    grid: list[list[str]],
    max_cols: int,
) -> dict[int, tuple[date, int | None]]:
    col_periods: dict[int, tuple[date, int | None]] = {}
    for c in range(max_cols):
        header_parts: list[str] = []
        matched_row: int | None = None
        match_obj: re.Match[str] | None = None
        for r in range(min(12, len(grid))):
            if c >= len(grid[r]) or not grid[r][c]:
                continue
            header_parts.append(grid[r][c])
            col_lower = " ".join(header_parts).lower()
            m = _QUARTER_HEADER_PATTERN.search(col_lower)
            if m is not None:
                matched_row = r
                match_obj = m
                break
        if match_obj is None or matched_row is None:
            continue
        # Include an immediate sub-header row only if its label column is empty
        if (
            matched_row + 1 < len(grid)
            and c < len(grid[matched_row + 1])
            and grid[matched_row + 1][c]
            and (not grid[matched_row + 1][0])
        ):
            header_parts.append(grid[matched_row + 1][c])
        col_lower = " ".join(header_parts).lower()
        if any(term in col_lower for term in _MONETARY_EXCLUDED_HEADER_TERMS):
            continue
        mon = _MONTHS.get(match_obj.group(3).lower().rstrip("."))
        day = int(match_obj.group(4))
        yr = int(match_obj.group(5))
        if mon is None:
            continue
        try:
            period_dt = date(yr, mon, day)
        except ValueError:
            continue
        explicit_q: int | None = None
        if match_obj.group(1):
            explicit_q = _ORDINAL_TO_Q.get(match_obj.group(1).lower())
        elif match_obj.group(2):
            explicit_q = _ORDINAL_TO_Q.get(match_obj.group(2).lower())
        col_periods[c] = (period_dt, explicit_q)
    return col_periods


def _extract_eps_metrics(
    body: bytes,
    expected_period_end: date | None,
    values: dict[tuple[Sec8KMetric, date], Sec8KMetricCandidate],
    ambiguities: dict[tuple[Sec8KMetric, date], Sec8KMetricAmbiguity],
) -> None:
    try:
        eps_map = parse_sec_8k_earnings_release(body, expected_period_end=expected_period_end)
        for period_dt, (eps_val, eps_label) in eps_map.items():
            values[(Sec8KMetric.GAAP_DILUTED_EPS, period_dt)] = Sec8KMetricCandidate(
                Sec8KMetric.GAAP_DILUTED_EPS, period_dt, eps_val, "USD/shares", eps_label, -1
            )
    except Sec8KEpsAmbiguousError as exc:
        ambig_cands = tuple(
            Sec8KMetricCandidate(
                Sec8KMetric.GAAP_DILUTED_EPS,
                c.period_end,
                c.value,
                "USD/shares",
                c.native_label,
                c.table_index,
            )
            for c in exc.candidates
        )
        ambiguities[(Sec8KMetric.GAAP_DILUTED_EPS, exc.period_end)] = Sec8KMetricAmbiguity(
            Sec8KMetric.GAAP_DILUTED_EPS,
            exc.period_end,
            "conflicting GAAP diluted EPS candidate rows in 8-K exhibit",
            ambig_cands,
        )


def parse_sec_8k_earnings_metrics(
    body: bytes,
    *,
    metrics: tuple[Sec8KMetric, ...] = (
        Sec8KMetric.REVENUE,
        Sec8KMetric.GROSS_PROFIT,
        Sec8KMetric.GAAP_DILUTED_EPS,
    ),
    expected_period_end: date | None = None,
) -> Sec8KParsedMetrics:
    """Parse direct GAAP Revenue, Gross Profit, and/or Diluted EPS from an 8-K exhibit.

    Strictly enforces declared table unit scale headers for monetary metrics and
    rejects non-GAAP, guidance, segment-only, percentage margin, and YTD rows.
    """
    if type(body) is not bytes or not 0 < len(body) <= _MAX_EXHIBIT_BYTES:
        raise ResourceLimitError("SEC 8-K exhibit HTML source exceeds byte limit")
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaMismatchError("SEC 8-K exhibit must be valid UTF-8") from exc

    requested = set(metrics)
    values: dict[tuple[Sec8KMetric, date], Sec8KMetricCandidate] = {}
    ambiguities: dict[tuple[Sec8KMetric, date], Sec8KMetricAmbiguity] = {}
    if Sec8KMetric.GAAP_DILUTED_EPS in requested:
        _extract_eps_metrics(body, expected_period_end, values, ambiguities)

    monetary_requested = requested & {Sec8KMetric.REVENUE, Sec8KMetric.GROSS_PROFIT}
    if not monetary_requested:
        return Sec8KParsedMetrics(values=values, ambiguities=ambiguities)

    parser = _GridTableParser()
    parser.feed(html)
    parser.close()

    candidates: dict[tuple[Sec8KMetric, date], list[Sec8KMetricCandidate]] = {}
    scale_ambiguities: dict[tuple[Sec8KMetric, date], tuple[str, list[Sec8KMetricCandidate]]] = {}

    for t_idx, grid in enumerate(parser.tables):
        if not grid or max(len(r) for r in grid) < 2:
            continue
        max_cols = max(len(r) for r in grid)
        table_ctx = parser.table_contexts[t_idx] if t_idx < len(parser.table_contexts) else ""
        if any(term in table_ctx.lower() for term in _EXCLUDED_TABLE_CONTEXT_TERMS):
            continue
        col_periods = _extract_quarter_columns(grid, max_cols)
        if not col_periods:
            continue
        header_rows_text = " ".join(cell for r in grid[:5] for cell in r if cell)
        scale_factor, scale_label, scale_err = _detect_monetary_scale(table_ctx, header_rows_text)
        if scale_factor is None and scale_err is None:
            continue

        cur_section = ""
        for r in grid:
            raw_label, label_col = next(((c, i) for i, c in enumerate(r[:3]) if c), ("", -1))
            if not raw_label:
                continue
            label_lower = raw_label.lower()
            if any(term in label_lower for term in _EXCLUDED_SECTION_TERMS):
                cur_section = raw_label
                continue
            if "consolidated statements of operations" in label_lower or (
                "gaap" in label_lower
                and "non-gaap" not in label_lower
                and all(not c for c in r[label_col + 1 :])
            ):
                cur_section = raw_label
            if any(term in cur_section.lower() for term in _EXCLUDED_SECTION_TERMS):
                continue
            metric = _classify_monetary_label(raw_label)
            if metric is None or metric not in monetary_requested:
                continue

            consumed_cols: set[int] = set()
            for col_idx in sorted(col_periods):
                if col_idx in consumed_cols or col_idx >= len(r):
                    continue
                period_dt, explicit_q = col_periods[col_idx]
                if expected_period_end is not None and period_dt != expected_period_end:
                    continue
                raw_cell = r[col_idx]
                val = _parse_monetary_cell(raw_cell)
                if val is None and col_idx + 1 < len(r):
                    next_same = (
                        col_idx + 1 in col_periods and col_periods[col_idx + 1][0] == period_dt
                    )
                    if (raw_cell.strip() in {"$", "US$"} and next_same) or (
                        col_idx == label_col and col_idx + 1 not in col_periods
                    ):
                        val = _parse_monetary_cell(r[col_idx + 1])
                        if val is not None:
                            consumed_cols.add(col_idx + 1)
                if val is None:
                    continue
                key = (metric, period_dt)
                if scale_err is not None:
                    scale_ambiguities.setdefault(key, (scale_err, []))[1].append(
                        Sec8KMetricCandidate(
                            metric, period_dt, val, "USD", raw_label, t_idx, "ambiguous", explicit_q
                        )
                    )
                else:
                    assert scale_factor is not None
                    candidates.setdefault(key, []).append(
                        Sec8KMetricCandidate(
                            metric,
                            period_dt,
                            val * scale_factor,
                            "USD",
                            raw_label,
                            t_idx,
                            scale_label,
                            explicit_q,
                        )
                    )

    for key, (reason, scale_cands) in scale_ambiguities.items():
        ambiguities[key] = Sec8KMetricAmbiguity(
            key[0], key[1], reason, tuple(scale_cands + candidates.get(key, []))
        )
    for key, cand_list in candidates.items():
        if key in ambiguities:
            continue
        if len({c.value for c in cand_list}) > 1:
            ambiguities[key] = Sec8KMetricAmbiguity(
                key[0],
                key[1],
                f"conflicting {key[0].value} values in 8-K exhibit",
                tuple(cand_list),
            )
        else:
            values[key] = cand_list[0]
    return Sec8KParsedMetrics(values=values, ambiguities=ambiguities)


__all__ = [
    "Sec8KMetric",
    "Sec8KMetricAmbiguity",
    "Sec8KMetricAmbiguousError",
    "Sec8KMetricCandidate",
    "Sec8KParsedMetrics",
    "parse_sec_8k_earnings_metrics",
]

"""Endpoint models, policies, and shape normalization for yfinance daily bars."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType

import pandas as pd

from ohmydata.core.policy import RetryPolicy
from ohmydata.core.provenance import FetchProvenance

from .errors import SchemaMismatchError, YFinanceRepairReceipt

# Pattern allowing US stocks, hyphenated/dotted share classes, indices (^VIX),
# regional tickers (.HK, .SS, etc.), and FX pairs (=X, =F).
_VALID_SYMBOL_PATTERN = re.compile(r"^\^?[A-Za-z0-9]+([.-][A-Za-z0-9]+)*(=[A-Za-z0-9]+)?$")

STANDARD_COLUMNS: tuple[str, ...] = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
)


def validate_yfinance_symbol(symbol: str) -> str:
    """Validate and sanitize a single symbol for Yahoo Finance."""
    cleaned = symbol.strip()
    if not cleaned:
        raise ValueError("Symbol cannot be empty or whitespace")
    if not _VALID_SYMBOL_PATTERN.match(cleaned):
        raise ValueError(f"Invalid yfinance symbol format: {symbol!r}")
    return cleaned


class YFinanceAdjustmentMode(str, Enum):
    """Adjustment mode for daily bars."""

    RAW_WITH_ADJ_CLOSE = "RAW_WITH_ADJ_CLOSE"
    AUTO_ADJUSTED = "AUTO_ADJUSTED"


class YFinanceBatchPolicy(str, Enum):
    """Batch handling policy."""

    STRICT = "STRICT"
    ALLOW_PARTIAL = "ALLOW_PARTIAL"


class YFinanceRepairPolicy(str, Enum):
    """Recovery policy for failed or partial symbols in a batch."""

    NONE = "NONE"
    PER_SYMBOL = "PER_SYMBOL"


class YFinanceCoveragePolicy(str, Enum):
    """Coverage validation policy against expected trading sessions."""

    STRICT_EXPECTED_SESSIONS = "STRICT_EXPECTED_SESSIONS"
    FROM_FIRST_VALID_BAR = "FROM_FIRST_VALID_BAR"
    EXPLICIT_COVERAGE_START = "EXPLICIT_COVERAGE_START"


class YFinanceSymbolOutcome(str, Enum):
    """Outcome for an individual symbol in a batch request."""

    COMPLETE = "COMPLETE"
    EMPTY = "EMPTY"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    INVALID_VALUES = "INVALID_VALUES"
    MISSING_EXPECTED_SESSIONS = "MISSING_EXPECTED_SESSIONS"
    RECOVERED = "RECOVERED"
    RECOVERY_FAILED = "RECOVERY_FAILED"


def _format_date(date_val: str | date | datetime) -> str:
    if isinstance(date_val, (date, datetime)):
        return date_val.strftime("%Y-%m-%d")
    s = str(date_val).strip()
    date.fromisoformat(s)
    return s


@dataclass(frozen=True)
class YFinanceDailyBarsRequest:
    """Request contract for yfinance daily bars."""

    symbols: tuple[str, ...]
    start_date: str
    end_date_exclusive: str
    interval: str = "1d"
    adjustment_mode: YFinanceAdjustmentMode = YFinanceAdjustmentMode.RAW_WITH_ADJ_CLOSE
    batch_policy: YFinanceBatchPolicy = YFinanceBatchPolicy.STRICT
    repair_policy: YFinanceRepairPolicy = YFinanceRepairPolicy.NONE
    retry_policy: RetryPolicy | None = None
    timeout: float = 30.0

    def __post_init__(self):
        if not self.symbols:
            raise ValueError("Symbols tuple cannot be empty")
        # Validate and deduplicate while asserting uniqueness
        validated = [validate_yfinance_symbol(s) for s in self.symbols]
        if len(set(validated)) != len(validated):
            raise ValueError(f"Duplicate symbols provided in request: {self.symbols}")
        object.__setattr__(self, "symbols", tuple(validated))

        start_str = _format_date(self.start_date)
        end_str = _format_date(self.end_date_exclusive)
        if start_str >= end_str:
            raise ValueError(
                f"start_date ({start_str}) must be strictly before end_date_exclusive ({end_str})"
            )
        object.__setattr__(self, "start_date", start_str)
        object.__setattr__(self, "end_date_exclusive", end_str)

        if self.interval != "1d":
            raise ValueError(
                f"Only daily interval '1d' is supported in v0.2.0, got: {self.interval!r}"
            )


@dataclass(frozen=True)
class YFinanceDailyBarsResult:
    """Result container for yfinance daily bars with provenance and audit receipts."""

    data: pd.DataFrame
    requested_symbols: tuple[str, ...]
    returned_symbols: tuple[str, ...]
    start_date: str
    end_date_exclusive: str
    yfinance_version: str
    adjustment_mode: YFinanceAdjustmentMode
    symbol_outcomes: MappingProxyType[str, YFinanceSymbolOutcome]
    repair_receipts: tuple[YFinanceRepairReceipt, ...] = field(default_factory=tuple)
    provenance: FetchProvenance | None = None

    def __post_init__(self):
        object.__setattr__(self, "requested_symbols", tuple(self.requested_symbols))
        object.__setattr__(self, "returned_symbols", tuple(self.returned_symbols))
        object.__setattr__(self, "repair_receipts", tuple(self.repair_receipts))
        if isinstance(self.symbol_outcomes, dict):
            object.__setattr__(
                self, "symbol_outcomes", MappingProxyType(dict(self.symbol_outcomes))
            )

    def to_pandas(self) -> pd.DataFrame:
        """Return a defensive copy of the normalized daily bars frame."""
        return self.data.copy()


def normalize_yfinance_download_df(
    raw_df: pd.DataFrame,
    requested_symbols: tuple[str, ...],
    adjustment_mode: YFinanceAdjustmentMode,
) -> pd.DataFrame:
    """Normalize raw yfinance DataFrame into a standard tabular DataFrame."""
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(columns=list(STANDARD_COLUMNS))

    records: list[pd.DataFrame] = []

    # Check if DataFrame has MultiIndex columns
    if isinstance(raw_df.columns, pd.MultiIndex):
        # In yfinance, columns MultiIndex can be (Field, Symbol) or (Symbol, Field)
        # Identify which level corresponds to symbol
        level0_vals = set(raw_df.columns.get_level_values(0))

        # Check which level matches requested symbols
        symbol_level = 1
        if any(s in level0_vals for s in requested_symbols):
            symbol_level = 0

        for sym in requested_symbols:
            try:
                sym_df = raw_df.xs(sym, axis=1, level=symbol_level, drop_level=True).copy()
            except KeyError:
                continue

            if isinstance(sym_df, pd.Series):
                sym_df = sym_df.to_frame()
            if not isinstance(sym_df, pd.DataFrame):
                continue

            extracted = _extract_single_symbol_frame(sym_df, sym, adjustment_mode)
            if extracted is not None and not extracted.empty:
                records.append(extracted)
    else:
        # Flat columns: usually for single symbol requests
        sym = requested_symbols[0] if len(requested_symbols) == 1 else "UNKNOWN"
        extracted = _extract_single_symbol_frame(raw_df, sym, adjustment_mode)
        if extracted is not None and not extracted.empty:
            records.append(extracted)

    if not records:
        return pd.DataFrame(columns=list(STANDARD_COLUMNS))

    merged = pd.concat(records, ignore_index=True)
    merged.sort_values(by=["symbol", "date"], inplace=True)
    merged.reset_index(drop=True, inplace=True)
    return merged[list(STANDARD_COLUMNS)]


def _extract_single_symbol_frame(
    df_sym: pd.DataFrame,
    symbol: str,
    adjustment_mode: YFinanceAdjustmentMode,
) -> pd.DataFrame | None:
    """Extract and sanitize OHLCV + adj_close for one symbol."""
    if df_sym is None or df_sym.empty:
        return None

    work = df_sym.copy()
    if (
        isinstance(work.index, pd.DatetimeIndex)
        or str(work.index.name).lower() in ("date", "datetime")
    ) and work.index.name is None:
        work.index.name = "Date"
    # Flatten any tuple columns if present
    work.columns = [str(c[0]) if isinstance(c, tuple) else str(c) for c in work.columns]
    work.reset_index(inplace=True)

    date_col = next((c for c in work.columns if c.lower() in ("date", "datetime")), None)
    if date_col is None and "index" in work.columns:
        try:
            pd.to_datetime(work["index"])
            date_col = "index"
        except (ValueError, TypeError, KeyError):
            pass
    if date_col is None:
        raise SchemaMismatchError(
            f"No Date or Datetime column found in response for symbol {symbol}"
        )

    col_map: dict[str, str] = {}
    for c in work.columns:
        norm_c = c.lower().replace(" ", "_")
        if norm_c == "open":
            col_map[c] = "open"
        elif norm_c == "high":
            col_map[c] = "high"
        elif norm_c == "low":
            col_map[c] = "low"
        elif norm_c == "close":
            col_map[c] = "close"
        elif norm_c in ("adj_close", "adjclose"):
            col_map[c] = "adj_close"
        elif norm_c == "volume":
            col_map[c] = "volume"

    work.rename(columns=col_map, inplace=True)

    required_base = {"open", "high", "low", "close"}
    missing = required_base - set(work.columns)
    if missing:
        raise SchemaMismatchError(f"Missing required base columns {missing} for symbol {symbol}")

    if "volume" not in work.columns:
        work["volume"] = 0

    if adjustment_mode == YFinanceAdjustmentMode.RAW_WITH_ADJ_CLOSE:
        if "adj_close" not in work.columns:
            work["adj_close"] = pd.NA
    elif adjustment_mode == YFinanceAdjustmentMode.AUTO_ADJUSTED:
        work["adj_close"] = work["close"]

    # Convert date to datetime.date
    dates_raw = pd.to_datetime(work[date_col])
    # If timezone-aware, convert to UTC date or naive date
    work["date"] = dates_raw.dt.date
    work["symbol"] = symbol

    for col in ("open", "high", "low", "close", "adj_close"):
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work["volume"] = pd.to_numeric(work["volume"], errors="coerce").fillna(0).astype("int64")

    # Drop rows where all price fields are NaN
    work.dropna(subset=["open", "high", "low", "close"], how="all", inplace=True)
    if work.empty:
        return None

    return work[list(STANDARD_COLUMNS)]

"""Internal normalization, enums, and finding construction helpers for EOD QC."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

import numpy as np
import pandas as pd

PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "adj_close")
OHLC_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")
SPLIT_ACTIONS: frozenset[str] = frozenset({"split", "stock split", "stock_split"})
SPINOFF_ACTIONS: frozenset[str] = frozenset({"spinoff", "spinoffdividend", "spin-off", "spin_off"})
DIVIDEND_ACTIONS: frozenset[str] = frozenset({"dividend", "cash dividend", "dividends"})
CAPITAL_GAIN_ACTIONS: frozenset[str] = frozenset(
    {"capital_gain", "capital gain", "capital_gains", "capital gains"}
)
DISTRIBUTION_ACTIONS: frozenset[str] = DIVIDEND_ACTIONS | CAPITAL_GAIN_ACTIONS


class QualitySeverity(StrEnum):
    """Severity level of an EOD quality control finding."""

    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class QualityCategory(StrEnum):
    """Taxonomy of EOD market-data quality findings."""

    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    OHLC_GEOMETRY = "OHLC_GEOMETRY"
    SUSPECT_MISSING_SPLIT = "SUSPECT_MISSING_SPLIT"
    DISTRIBUTION_EX_DATE = "DISTRIBUTION_EX_DATE"
    SPINOFF_BASIS_DIFFERENCE = "SPINOFF_BASIS_DIFFERENCE"
    OSCILLATING_CURRENCY_JUMP = "OSCILLATING_CURRENCY_JUMP"
    REVERSE_SPLIT_DIVIDEND_MISMATCH = "REVERSE_SPLIT_DIVIDEND_MISMATCH"
    TRAILING_STALENESS = "TRAILING_STALENESS"
    ZERO_VOLUME = "ZERO_VOLUME"


def norm_date_str(val: Any) -> str:
    """Normalize a date-like value into ISO YYYY-MM-DD text."""
    if isinstance(val, datetime):
        return val.date().isoformat()
    if isinstance(val, date):
        return val.isoformat()
    return pd.to_datetime(val, errors="raise").strftime("%Y-%m-%d")


def symbol_col(df: pd.DataFrame) -> str:
    """Resolve the symbol column name ('symbol' or 'canonical_symbol')."""
    if "symbol" in df.columns:
        return "symbol"
    if "canonical_symbol" in df.columns:
        return "canonical_symbol"
    raise ValueError("DataFrame must contain a 'symbol' or 'canonical_symbol' column")


def make_finding(
    cls: Any,
    symbol: str,
    date_str: str,
    category: QualityCategory,
    severity: QualitySeverity,
    message: str,
    metrics: dict[str, Any],
) -> Any:
    return cls(
        symbol=symbol,
        date=date_str,
        category=category,
        severity=severity,
        message=message,
        metrics=MappingProxyType(metrics),
    )


def prepare_bars(bars_df: pd.DataFrame | None) -> pd.DataFrame:
    """Normalize dates/numerics and sort bars by (symbol, date) on a copy."""
    if bars_df is None or bars_df.empty:
        return pd.DataFrame()
    s_col = symbol_col(bars_df)
    if "date" not in bars_df.columns:
        raise ValueError("bars_df must contain a 'date' column")
    work = bars_df.copy()
    work["_symbol"] = work[s_col].astype(str)
    work["_dt"] = pd.to_datetime(work["date"], errors="coerce").dt.tz_localize(None).dt.normalize()
    work = work[work["_dt"].notna()].copy()
    work["_date_str"] = work["_dt"].dt.strftime("%Y-%m-%d")
    work["_dt"] = work["_dt"].astype("datetime64[ns]")
    for col in (*PRICE_COLUMNS, "volume"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce").astype("float64")
    work.sort_values(["_symbol", "_dt"], kind="mergesort", inplace=True)
    work.reset_index(drop=True, inplace=True)
    return work


def prepare_actions(actions_df: pd.DataFrame | None) -> pd.DataFrame:
    """Normalize corporate actions DataFrame sorted by (date, symbol)."""
    if actions_df is None or actions_df.empty:
        return pd.DataFrame(columns=["_symbol", "_dt", "_action", "_value"])
    s_col = symbol_col(actions_df)
    if "date" not in actions_df.columns or "action" not in actions_df.columns:
        raise ValueError("actions_df must contain 'date' and 'action' columns")
    dt_s = (
        pd.to_datetime(actions_df["date"], errors="coerce")
        .dt.tz_localize(None)
        .dt.normalize()
        .astype("datetime64[ns]")
    )
    val_s = (
        pd.to_numeric(actions_df["value"], errors="coerce").astype("float64")
        if "value" in actions_df.columns
        else np.nan
    )
    act = pd.DataFrame(
        {
            "_symbol": actions_df[s_col].astype(str),
            "_dt": dt_s,
            "_action": actions_df["action"].astype(str).str.strip().str.lower(),
            "_value": val_s,
        }
    )
    return act[act["_dt"].notna()].sort_values(["_dt", "_symbol"], kind="mergesort")

"""Quality, numeric validation, batch completeness, and coverage validation for yfinance data."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from types import MappingProxyType

import numpy as np
import pandas as pd

from .endpoints import (
    STANDARD_COLUMNS,
    YFinanceAdjustmentMode,
    YFinanceCoveragePolicy,
    YFinanceSymbolOutcome,
)
from .errors import CoverageError, SchemaMismatchError


def validate_daily_bars_dataframe(
    df: pd.DataFrame,
    adjustment_mode: YFinanceAdjustmentMode = YFinanceAdjustmentMode.RAW_WITH_ADJ_CLOSE,
) -> None:
    """Perform structural and numeric validation on normalized daily bars DataFrame."""
    if df is None:
        raise SchemaMismatchError("DataFrame cannot be None")
    if df.empty:
        return

    missing_cols = set(STANDARD_COLUMNS) - set(df.columns)
    if missing_cols:
        raise SchemaMismatchError(f"DataFrame is missing standard columns: {missing_cols}")

    # Check date column
    if df["date"].isna().any():
        raise SchemaMismatchError("date column contains null or NaN values")

    # Numeric checks on core price fields
    for col in ("open", "high", "low", "close"):
        vals = df[col]
        if vals.isna().any():
            raise SchemaMismatchError(f"Price column {col!r} contains NaN/null values")
        if not pd.api.types.is_numeric_dtype(vals.dtype):
            raise SchemaMismatchError(f"Price column {col!r} is not numeric")
        if (vals <= 0).any() or np.isinf(vals).any():
            raise SchemaMismatchError(
                f"Price column {col!r} contains non-positive or infinite values"
            )

    # High / Low relationship checks
    if (df["high"] < df["low"]).any():
        raise SchemaMismatchError("Found rows where high < low")
    if (df["high"] < df["open"]).any() or (df["high"] < df["close"]).any():
        raise SchemaMismatchError("Found rows where high is less than open or close")
    if (df["low"] > df["open"]).any() or (df["low"] > df["close"]).any():
        raise SchemaMismatchError("Found rows where low is greater than open or close")

    # Volume check
    if (df["volume"] < 0).any() or np.isinf(df["volume"]).any():
        raise SchemaMismatchError("volume contains negative or infinite values")

    # Check adj_close if present
    if adjustment_mode == YFinanceAdjustmentMode.AUTO_ADJUSTED:
        if df["adj_close"].isna().any():
            raise SchemaMismatchError("adj_close contains NaN in AUTO_ADJUSTED mode")
    elif adjustment_mode == YFinanceAdjustmentMode.RAW_WITH_ADJ_CLOSE:
        non_null_adj = df["adj_close"].dropna()
        if not non_null_adj.empty and ((non_null_adj <= 0).any() or np.isinf(non_null_adj).any()):
            raise SchemaMismatchError("adj_close contains non-positive or infinite values")

    # Key uniqueness: (symbol, date) must be distinct
    duplicates = df.duplicated(subset=["symbol", "date"])
    if duplicates.any():
        raise SchemaMismatchError("Duplicate (symbol, date) keys found in DataFrame")


def evaluate_symbol_outcomes(
    df: pd.DataFrame,
    requested_symbols: tuple[str, ...],
    adjustment_mode: YFinanceAdjustmentMode,
) -> dict[str, YFinanceSymbolOutcome]:
    """Evaluate individual outcome for each requested symbol in the batch."""
    outcomes: dict[str, YFinanceSymbolOutcome] = {}
    if df is None or df.empty:
        return {sym: YFinanceSymbolOutcome.EMPTY for sym in requested_symbols}

    grouped = dict(tuple(df.groupby("symbol")))

    for sym in requested_symbols:
        if sym not in grouped:
            outcomes[sym] = YFinanceSymbolOutcome.EMPTY
            continue

        sym_df = grouped[sym]
        if sym_df.empty:
            outcomes[sym] = YFinanceSymbolOutcome.EMPTY
            continue

        # Check core prices
        has_invalid_prices = False
        for col in ("open", "high", "low", "close"):
            v = sym_df[col]
            if v.isna().any() or (v <= 0).any() or np.isinf(v).any():
                has_invalid_prices = True
                break

        if has_invalid_prices or (sym_df["high"] < sym_df["low"]).any():
            outcomes[sym] = YFinanceSymbolOutcome.INVALID_VALUES
            continue

        # Check adj_close under RAW_WITH_ADJ_CLOSE
        if (
            adjustment_mode == YFinanceAdjustmentMode.RAW_WITH_ADJ_CLOSE
            and sym_df["adj_close"].isna().any()
        ):
            outcomes[sym] = YFinanceSymbolOutcome.INVALID_VALUES
            continue

        outcomes[sym] = YFinanceSymbolOutcome.COMPLETE

    return outcomes


@dataclass(frozen=True)
class YFinanceCoverageReport:
    """Coverage audit report for caller-supplied expected sessions."""

    is_covered: bool
    policy: YFinanceCoveragePolicy
    missing_symbols: tuple[str, ...]
    missing_sessions: MappingProxyType[str, tuple[datetime.date, ...]]
    invalid_sessions: MappingProxyType[str, tuple[datetime.date, ...]]
    unverified_prefix_sessions: MappingProxyType[str, tuple[datetime.date, ...]]


def validate_yfinance_daily_bar_coverage(
    df: pd.DataFrame,
    symbols: tuple[str, ...],
    expected_sessions: tuple[datetime.date | str, ...],
    policy: YFinanceCoveragePolicy = YFinanceCoveragePolicy.STRICT_EXPECTED_SESSIONS,
    coverage_starts: dict[str, datetime.date | str] | None = None,
    fail_fast: bool = True,
) -> YFinanceCoverageReport:
    """Audit coverage of valid daily bars against caller-supplied expected sessions."""
    coverage_starts = coverage_starts or {}
    expected_dates: list[datetime.date] = []
    for s in expected_sessions:
        if isinstance(s, (datetime.date, datetime.datetime)):
            expected_dates.append(s.date() if isinstance(s, datetime.datetime) else s)
        else:
            expected_dates.append(datetime.date.fromisoformat(str(s).strip()))
    expected_dates.sort()

    missing_symbols: list[str] = []
    missing_sessions_map: dict[str, tuple[datetime.date, ...]] = {}
    invalid_sessions_map: dict[str, tuple[datetime.date, ...]] = {}
    unverified_prefix_map: dict[str, tuple[datetime.date, ...]] = {}

    grouped = dict(tuple(df.groupby("symbol"))) if (df is not None and not df.empty) else {}

    for sym in symbols:
        if sym not in grouped:
            missing_symbols.append(sym)
            missing_sessions_map[sym] = tuple(expected_dates)
            continue

        sym_df = grouped[sym].set_index("date")
        sym_missing: list[datetime.date] = []
        sym_invalid: list[datetime.date] = []
        sym_unverified_prefix: list[datetime.date] = []

        eff_start = None
        if policy == YFinanceCoveragePolicy.EXPLICIT_COVERAGE_START and sym in coverage_starts:
            start_raw = coverage_starts[sym]
            if isinstance(start_raw, (datetime.date, datetime.datetime)):
                eff_start = (
                    start_raw.date() if isinstance(start_raw, datetime.datetime) else start_raw
                )
            else:
                eff_start = datetime.date.fromisoformat(str(start_raw).strip())

        first_valid_bar_date = None
        if policy == YFinanceCoveragePolicy.FROM_FIRST_VALID_BAR:
            # Find earliest date where price is valid
            for d in sorted(sym_df.index):
                row = sym_df.loc[d]
                c = row["close"] if not isinstance(row, pd.DataFrame) else row["close"].iloc[0]
                if pd.notna(c) and c > 0:
                    first_valid_bar_date = d
                    break

        for expected_dt in expected_dates:
            if eff_start is not None and expected_dt < eff_start:
                continue

            if policy == YFinanceCoveragePolicy.FROM_FIRST_VALID_BAR and (
                first_valid_bar_date is None or expected_dt < first_valid_bar_date
            ):
                sym_unverified_prefix.append(expected_dt)
                continue

            if expected_dt not in sym_df.index:
                sym_missing.append(expected_dt)
                continue

            row = sym_df.loc[expected_dt]
            close_val = row["close"] if not isinstance(row, pd.DataFrame) else row["close"].iloc[0]
            adj_val = (
                row["adj_close"] if not isinstance(row, pd.DataFrame) else row["adj_close"].iloc[0]
            )
            if pd.isna(close_val) or close_val <= 0 or (pd.notna(adj_val) and adj_val <= 0):
                sym_invalid.append(expected_dt)

        if sym_missing:
            missing_sessions_map[sym] = tuple(sym_missing)
        if sym_invalid:
            invalid_sessions_map[sym] = tuple(sym_invalid)
        if sym_unverified_prefix:
            unverified_prefix_map[sym] = tuple(sym_unverified_prefix)

    is_covered = (
        len(missing_symbols) == 0
        and len(missing_sessions_map) == 0
        and len(invalid_sessions_map) == 0
    )

    report = YFinanceCoverageReport(
        is_covered=is_covered,
        policy=policy,
        missing_symbols=tuple(missing_symbols),
        missing_sessions=MappingProxyType(missing_sessions_map),
        invalid_sessions=MappingProxyType(invalid_sessions_map),
        unverified_prefix_sessions=MappingProxyType(unverified_prefix_map),
    )

    if fail_fast and not is_covered:
        raise CoverageError(
            f"Coverage validation failed under {policy.value}: "
            f"missing_symbols={len(missing_symbols)} missing_sessions={len(missing_sessions_map)} "
            f"invalid_sessions={len(invalid_sessions_map)}"
        )

    return report

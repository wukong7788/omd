"""Unit tests for quality checks, numeric validation, batch completeness, and coverage."""

from __future__ import annotations

import datetime

import numpy as np
import pandas as pd
import pytest

from ohmydata.providers.yfinance.endpoints import (
    YFinanceAdjustmentMode,
    YFinanceCoveragePolicy,
    YFinanceSymbolOutcome,
)
from ohmydata.providers.yfinance.errors import CoverageError, SchemaMismatchError
from ohmydata.providers.yfinance.quality import (
    evaluate_symbol_outcomes,
    validate_daily_bars_dataframe,
    validate_yfinance_daily_bar_coverage,
)


def _make_valid_df(symbols=("AAPL",), dates=("2026-01-02", "2026-01-05")):
    rows = []
    for s in symbols:
        for d in dates:
            dt = datetime.date.fromisoformat(d)
            rows.append(
                {
                    "symbol": s,
                    "date": dt,
                    "open": 100.0,
                    "high": 105.0,
                    "low": 95.0,
                    "close": 102.0,
                    "adj_close": 101.0,
                    "volume": 1000000,
                }
            )
    return pd.DataFrame(rows)


class TestDailyBarsValidation:
    def test_valid_df_passes(self):
        df = _make_valid_df()
        validate_daily_bars_dataframe(df)

    def test_missing_column_fails(self):
        df = _make_valid_df().drop(columns=["close"])
        with pytest.raises(SchemaMismatchError, match="missing standard columns"):
            validate_daily_bars_dataframe(df)

    def test_null_date_fails(self):
        df = _make_valid_df()
        df.loc[0, "date"] = None
        with pytest.raises(SchemaMismatchError, match="date column contains null"):
            validate_daily_bars_dataframe(df)

    def test_nan_price_fails(self):
        df = _make_valid_df()
        df.loc[0, "close"] = np.nan
        with pytest.raises(SchemaMismatchError, match="contains NaN/null"):
            validate_daily_bars_dataframe(df)

    def test_negative_price_fails(self):
        df = _make_valid_df()
        df.loc[0, "close"] = -10.0
        with pytest.raises(SchemaMismatchError, match="non-positive"):
            validate_daily_bars_dataframe(df)

    def test_zero_price_fails(self):
        df = _make_valid_df()
        df.loc[0, "close"] = 0.0
        with pytest.raises(SchemaMismatchError, match="non-positive"):
            validate_daily_bars_dataframe(df)

    def test_infinite_price_fails(self):
        df = _make_valid_df()
        df.loc[0, "close"] = np.inf
        with pytest.raises(SchemaMismatchError, match="non-positive or infinite"):
            validate_daily_bars_dataframe(df)

    def test_high_less_than_low_fails(self):
        df = _make_valid_df()
        df.loc[0, "high"] = 90.0
        df.loc[0, "low"] = 100.0
        with pytest.raises(SchemaMismatchError, match="high < low"):
            validate_daily_bars_dataframe(df)

    def test_negative_volume_fails(self):
        df = _make_valid_df()
        df.loc[0, "volume"] = -5
        with pytest.raises(SchemaMismatchError, match="volume contains negative"):
            validate_daily_bars_dataframe(df)

    def test_duplicate_keys_fails(self):
        df = _make_valid_df()
        dup_row = df.iloc[0:1].copy()
        df = pd.concat([df, dup_row], ignore_index=True)
        with pytest.raises(SchemaMismatchError, match="Duplicate"):
            validate_daily_bars_dataframe(df)


class TestBatchCompleteness:
    def test_evaluate_symbol_outcomes(self):
        df = _make_valid_df(symbols=("AAPL",))
        outcomes = evaluate_symbol_outcomes(
            df, ("AAPL", "MSFT"), YFinanceAdjustmentMode.RAW_WITH_ADJ_CLOSE
        )
        assert outcomes["AAPL"] == YFinanceSymbolOutcome.COMPLETE
        assert outcomes["MSFT"] == YFinanceSymbolOutcome.EMPTY

    def test_evaluate_symbol_outcomes_with_invalid_adj_close(self):
        df = _make_valid_df(symbols=("AAPL",))
        df.loc[0, "adj_close"] = np.nan
        outcomes = evaluate_symbol_outcomes(
            df, ("AAPL",), YFinanceAdjustmentMode.RAW_WITH_ADJ_CLOSE
        )
        assert outcomes["AAPL"] == YFinanceSymbolOutcome.INVALID_VALUES


class TestCoverageValidation:
    def test_coverage_success(self):
        df = _make_valid_df(symbols=("AAPL",), dates=("2026-01-02", "2026-01-05"))
        expected = ("2026-01-02", "2026-01-05")
        report = validate_yfinance_daily_bar_coverage(
            df, ("AAPL",), expected, YFinanceCoveragePolicy.STRICT_EXPECTED_SESSIONS
        )
        assert report.is_covered is True
        assert len(report.missing_symbols) == 0

    def test_coverage_missing_session_strict_fails(self):
        df = _make_valid_df(symbols=("AAPL",), dates=("2026-01-02",))
        expected = ("2026-01-02", "2026-01-05")
        with pytest.raises(CoverageError):
            validate_yfinance_daily_bar_coverage(
                df, ("AAPL",), expected, YFinanceCoveragePolicy.STRICT_EXPECTED_SESSIONS
            )

    def test_coverage_from_first_valid_bar(self):
        # Data starts on 2026-01-05, expected has 2026-01-02
        df = _make_valid_df(symbols=("AAPL",), dates=("2026-01-05",))
        expected = ("2026-01-02", "2026-01-05")
        report = validate_yfinance_daily_bar_coverage(
            df,
            ("AAPL",),
            expected,
            YFinanceCoveragePolicy.FROM_FIRST_VALID_BAR,
            fail_fast=False,
        )
        assert report.is_covered is True
        assert datetime.date(2026, 1, 2) in report.unverified_prefix_sessions["AAPL"]

    def test_coverage_explicit_start(self):
        df = _make_valid_df(symbols=("AAPL",), dates=("2026-01-05",))
        expected = ("2026-01-02", "2026-01-05")
        report = validate_yfinance_daily_bar_coverage(
            df,
            ("AAPL",),
            expected,
            YFinanceCoveragePolicy.EXPLICIT_COVERAGE_START,
            coverage_starts={"AAPL": "2026-01-05"},
            fail_fast=False,
        )
        assert report.is_covered is True

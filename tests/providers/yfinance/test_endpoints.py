"""Unit tests for yfinance endpoints, symbol validation, and shape normalization."""

from __future__ import annotations

import datetime

import numpy as np
import pandas as pd
import pytest

from ohmydata.providers.yfinance.endpoints import (
    STANDARD_COLUMNS,
    YFinanceAdjustmentMode,
    YFinanceBatchPolicy,
    YFinanceDailyBarsRequest,
    YFinanceRepairPolicy,
    normalize_yfinance_download_df,
    validate_yfinance_symbol,
)


class TestSymbolValidation:
    def test_valid_symbols(self):
        valid = [
            "AAPL",
            "MSFT",
            "BRK-B",
            "BRK.B",
            "^VIX",
            "^GSPC",
            "^NDX",
            "0700.HK",
            "9988.HK",
            "GBPUSD=X",
            "EURUSD=X",
            "USDCNY=X",
            "CL=F",
        ]
        for s in valid:
            assert validate_yfinance_symbol(s) == s

    def test_invalid_symbols(self):
        invalid = [
            "",
            "   ",
            "AAPL MSFT",
            "AAPL;DROP TABLE",
            "AAPL/MSFT",
            "AAPL!@",
        ]
        for s in invalid:
            with pytest.raises(ValueError):
                validate_yfinance_symbol(s)


class TestDailyBarsRequest:
    def test_request_valid(self):
        req = YFinanceDailyBarsRequest(
            symbols=("AAPL", "MSFT"),
            start_date="2026-01-01",
            end_date_exclusive="2026-01-10",
        )
        assert req.symbols == ("AAPL", "MSFT")
        assert req.start_date == "2026-01-01"
        assert req.end_date_exclusive == "2026-01-10"
        assert req.interval == "1d"
        assert req.adjustment_mode == YFinanceAdjustmentMode.RAW_WITH_ADJ_CLOSE
        assert req.batch_policy == YFinanceBatchPolicy.STRICT
        assert req.repair_policy == YFinanceRepairPolicy.NONE

    def test_request_duplicate_symbols(self):
        with pytest.raises(ValueError, match="Duplicate symbols"):
            YFinanceDailyBarsRequest(
                symbols=("AAPL", "AAPL"),
                start_date="2026-01-01",
                end_date_exclusive="2026-01-10",
            )

    def test_request_invalid_dates(self):
        with pytest.raises(ValueError, match="must be strictly before"):
            YFinanceDailyBarsRequest(
                symbols=("AAPL",),
                start_date="2026-01-10",
                end_date_exclusive="2026-01-05",
            )

    def test_request_unsupported_interval(self):
        with pytest.raises(ValueError, match="Only daily interval"):
            YFinanceDailyBarsRequest(
                symbols=("AAPL",),
                start_date="2026-01-01",
                end_date_exclusive="2026-01-10",
                interval="1h",
            )


class TestShapeNormalization:
    def test_normalize_multiindex_field_symbol(self):
        dates = pd.date_range("2026-01-01", periods=3)
        cols = pd.MultiIndex.from_tuples(
            [
                ("Open", "AAPL"),
                ("High", "AAPL"),
                ("Low", "AAPL"),
                ("Close", "AAPL"),
                ("Adj Close", "AAPL"),
                ("Volume", "AAPL"),
                ("Open", "MSFT"),
                ("High", "MSFT"),
                ("Low", "MSFT"),
                ("Close", "MSFT"),
                ("Adj Close", "MSFT"),
                ("Volume", "MSFT"),
            ]
        )
        data = np.random.uniform(100, 200, size=(3, 12))
        raw_df = pd.DataFrame(data, index=dates, columns=cols)

        res = normalize_yfinance_download_df(
            raw_df, ("AAPL", "MSFT"), YFinanceAdjustmentMode.RAW_WITH_ADJ_CLOSE
        )
        assert list(res.columns) == list(STANDARD_COLUMNS)
        assert len(res) == 6
        assert set(res["symbol"]) == {"AAPL", "MSFT"}
        assert isinstance(res["date"].iloc[0], datetime.date)

    def test_normalize_multiindex_symbol_field(self):
        dates = pd.date_range("2026-01-01", periods=2)
        cols = pd.MultiIndex.from_tuples(
            [
                ("AAPL", "Open"),
                ("AAPL", "High"),
                ("AAPL", "Low"),
                ("AAPL", "Close"),
                ("AAPL", "Adj Close"),
                ("AAPL", "Volume"),
            ]
        )
        raw_df = pd.DataFrame(
            [[100, 105, 95, 102, 101, 1000], [102, 107, 101, 106, 105, 1200]],
            index=dates,
            columns=cols,
        )
        res = normalize_yfinance_download_df(
            raw_df, ("AAPL",), YFinanceAdjustmentMode.RAW_WITH_ADJ_CLOSE
        )
        assert len(res) == 2
        assert res["symbol"].iloc[0] == "AAPL"
        assert res["close"].iloc[0] == 102
        assert res["adj_close"].iloc[0] == 101

    def test_normalize_single_flat_df(self):
        dates = pd.date_range("2026-01-01", periods=2)
        raw_df = pd.DataFrame(
            {
                "Open": [100.0, 102.0],
                "High": [105.0, 107.0],
                "Low": [95.0, 101.0],
                "Close": [102.0, 106.0],
                "Adj Close": [101.0, 105.0],
                "Volume": [1000, 1200],
            },
            index=dates,
        )
        res = normalize_yfinance_download_df(
            raw_df, ("AAPL",), YFinanceAdjustmentMode.RAW_WITH_ADJ_CLOSE
        )
        assert len(res) == 2
        assert list(res.columns) == list(STANDARD_COLUMNS)
        assert res["adj_close"].iloc[0] == 101.0

    def test_raw_adj_close_missing_does_not_fallback(self):
        dates = pd.date_range("2026-01-01", periods=2)
        raw_df = pd.DataFrame(
            {
                "Open": [100.0, 102.0],
                "High": [105.0, 107.0],
                "Low": [95.0, 101.0],
                "Close": [102.0, 106.0],
                "Volume": [1000, 1200],
            },
            index=dates,
        )
        res = normalize_yfinance_download_df(
            raw_df, ("AAPL",), YFinanceAdjustmentMode.RAW_WITH_ADJ_CLOSE
        )
        assert res["adj_close"].isna().all()

    def test_auto_adjusted_mode_sets_adj_close_equal_close(self):
        dates = pd.date_range("2026-01-01", periods=2)
        raw_df = pd.DataFrame(
            {
                "Open": [100.0, 102.0],
                "High": [105.0, 107.0],
                "Low": [95.0, 101.0],
                "Close": [102.0, 106.0],
                "Volume": [1000, 1200],
            },
            index=dates,
        )
        res = normalize_yfinance_download_df(
            raw_df, ("AAPL",), YFinanceAdjustmentMode.AUTO_ADJUSTED
        )
        assert (res["adj_close"] == res["close"]).all()

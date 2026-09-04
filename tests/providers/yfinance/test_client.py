"""Unit tests for YFinanceClient, per-symbol repair, provenance, and fundamentals fetching."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from ohmydata.providers.yfinance.client import (
    YFinanceClient,
    assert_yfinance_version,
)
from ohmydata.providers.yfinance.endpoints import (
    YFinanceBatchPolicy,
    YFinanceDailyBarsRequest,
    YFinanceRepairPolicy,
    YFinanceSymbolOutcome,
)
from ohmydata.providers.yfinance.errors import (
    CoverageError,
    YFinanceVersionMismatchError,
)
from ohmydata.providers.yfinance.fundamentals import YFinanceFundamentalsRequest


class TestVersionAssertion:
    def test_version_match(self):
        fake_module = SimpleNamespace(__version__="1.5.1")
        assert_yfinance_version(fake_module)

    def test_version_mismatch(self):
        fake_module = SimpleNamespace(__version__="1.6.0")
        with pytest.raises(YFinanceVersionMismatchError, match="yfinance version mismatch"):
            assert_yfinance_version(fake_module)


class TestClientDailyBars:
    def test_fetch_daily_bars_batch_success(self):
        dates = pd.date_range("2026-01-02", periods=2)
        cols = pd.MultiIndex.from_tuples(
            [
                ("Open", "AAPL"),
                ("High", "AAPL"),
                ("Low", "AAPL"),
                ("Close", "AAPL"),
                ("Adj Close", "AAPL"),
                ("Volume", "AAPL"),
            ]
        )
        fake_batch_df = pd.DataFrame(
            [[100, 105, 95, 102, 101, 1000], [102, 107, 101, 106, 105, 1200]],
            index=dates,
            columns=cols,
        )

        def mock_download(symbols, **kwargs):
            return fake_batch_df

        client = YFinanceClient(
            yf_module=SimpleNamespace(__version__="1.5.1"),
            download_fn=mock_download,
        )

        req = YFinanceDailyBarsRequest(
            symbols=("AAPL",),
            start_date="2026-01-01",
            end_date_exclusive="2026-01-10",
        )

        res = client.fetch_daily_bars(req)
        assert len(res.data) == 2
        assert res.symbol_outcomes["AAPL"] == YFinanceSymbolOutcome.COMPLETE
        assert res.provenance is not None
        assert res.provenance.provider == "yfinance"
        assert res.provenance.row_count == 2

    def test_fetch_daily_bars_per_symbol_repair(self):
        dates = pd.date_range("2026-01-02", periods=2)
        # First batch call only returns AAPL, MSFT is missing (simulating partial batch drop)
        cols = pd.MultiIndex.from_tuples(
            [
                ("Open", "AAPL"),
                ("High", "AAPL"),
                ("Low", "AAPL"),
                ("Close", "AAPL"),
                ("Adj Close", "AAPL"),
                ("Volume", "AAPL"),
            ]
        )
        aapl_df = pd.DataFrame(
            [[100, 105, 95, 102, 101, 1000], [102, 107, 101, 106, 105, 1200]],
            index=dates,
            columns=cols,
        )

        # Single symbol download for MSFT
        msft_cols = pd.MultiIndex.from_tuples(
            [
                ("Open", "MSFT"),
                ("High", "MSFT"),
                ("Low", "MSFT"),
                ("Close", "MSFT"),
                ("Adj Close", "MSFT"),
                ("Volume", "MSFT"),
            ]
        )
        msft_df = pd.DataFrame(
            [[200, 205, 195, 202, 201, 2000], [202, 207, 201, 206, 205, 2200]],
            index=dates,
            columns=msft_cols,
        )

        download_calls = []

        def mock_download(symbols, **kwargs):
            download_calls.append(symbols)
            if isinstance(symbols, list) and len(symbols) == 2:
                # Batch returns only AAPL
                return aapl_df
            elif symbols == "MSFT":
                # Single symbol repair returns MSFT
                return msft_df
            return pd.DataFrame()

        client = YFinanceClient(
            yf_module=SimpleNamespace(__version__="1.5.1"),
            download_fn=mock_download,
        )

        req = YFinanceDailyBarsRequest(
            symbols=("AAPL", "MSFT"),
            start_date="2026-01-01",
            end_date_exclusive="2026-01-10",
            batch_policy=YFinanceBatchPolicy.STRICT,
            repair_policy=YFinanceRepairPolicy.PER_SYMBOL,
        )

        res = client.fetch_daily_bars(req)
        assert len(res.data) == 4
        assert set(res.data["symbol"]) == {"AAPL", "MSFT"}
        assert res.symbol_outcomes["AAPL"] == YFinanceSymbolOutcome.COMPLETE
        assert res.symbol_outcomes["MSFT"] == YFinanceSymbolOutcome.RECOVERED
        assert len(res.repair_receipts) == 1
        assert res.repair_receipts[0].symbol == "MSFT"
        assert res.repair_receipts[0].final_status == "SUCCESS"

    def test_strict_batch_failure_without_repair(self):
        dates = pd.date_range("2026-01-02", periods=2)
        cols = pd.MultiIndex.from_tuples(
            [
                ("Open", "AAPL"),
                ("High", "AAPL"),
                ("Low", "AAPL"),
                ("Close", "AAPL"),
                ("Adj Close", "AAPL"),
                ("Volume", "AAPL"),
            ]
        )
        aapl_df = pd.DataFrame(
            [[100, 105, 95, 102, 101, 1000], [102, 107, 101, 106, 105, 1200]],
            index=dates,
            columns=cols,
        )

        def mock_download(symbols, **kwargs):
            return aapl_df

        client = YFinanceClient(
            yf_module=SimpleNamespace(__version__="1.5.1"),
            download_fn=mock_download,
        )

        req = YFinanceDailyBarsRequest(
            symbols=("AAPL", "MSFT"),
            start_date="2026-01-01",
            end_date_exclusive="2026-01-10",
            batch_policy=YFinanceBatchPolicy.STRICT,
            repair_policy=YFinanceRepairPolicy.NONE,
        )

        with pytest.raises(CoverageError, match="Strict batch policy failed"):
            client.fetch_daily_bars(req)


class TestClientFundamentals:
    def test_fetch_fundamentals(self):
        class FakeTicker:
            def __init__(self):
                self.info = {
                    "quoteType": "EQUITY",
                    "currency": "USD",
                    "marketCap": 2_500_000_000_000,
                    "trailingPE": 28.0,
                    "forwardPE": 24.0,
                    "pegRatio": 1.5,
                    "priceToSalesTrailing12Months": 8.0,
                    "revenueGrowth": 0.10,
                    "earningsGrowth": 0.14,
                }
                self.quarterly_income_stmt = pd.DataFrame(
                    {"2025-09-30": [50_000_000]}, index=["Total Revenue"]
                )
                self.quarterly_balance_sheet = pd.DataFrame(
                    {"2025-09-30": [20_000_000]}, index=["Total Debt"]
                )
                self.quarterly_cashflow = pd.DataFrame(
                    {"2025-09-30": [10_000_000]}, index=["Operating Cash Flow"]
                )
                self.fast_info = SimpleNamespace(
                    market_cap=2_500_000_000_000, shares=10_000_000_000
                )

            def get_revenue_estimate(self):
                return pd.DataFrame(
                    {"avg": [100.0, 110.0, 400.0, 450.0]}, index=["0q", "+1q", "0y", "+1y"]
                )

            def get_earnings_estimate(self):
                return pd.DataFrame({"avg": [2.0, 2.2, 8.5, 9.5]}, index=["0q", "+1q", "0y", "+1y"])

        client = YFinanceClient(
            yf_module=SimpleNamespace(__version__="1.5.1"),
            ticker_factory=lambda sym: FakeTicker(),
        )

        req = YFinanceFundamentalsRequest(symbols=("AAPL",))
        res = client.fetch_fundamentals(req)

        assert "AAPL" in res.records
        aapl = res.records["AAPL"]
        assert aapl.symbol == "AAPL"
        assert aapl.valuation.trailing_pe == 28.0
        assert aapl.estimates.revenue_est_current_q == 100.0
        assert aapl.estimates.eps_est_next_y == 9.5

        df = res.to_dataframe()
        assert len(df) == 1
        assert df["symbol"].iloc[0] == "AAPL"
        assert df["trailing_pe"].iloc[0] == 28.0

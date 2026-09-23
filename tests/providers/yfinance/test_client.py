"""Unit tests for YFinanceClient, per-symbol repair, provenance, and fundamentals fetching."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from ohmydata.core.policy import RetryPolicy
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
from ohmydata.providers.yfinance.fundamentals import (
    YFinanceFundamentalsOutcome,
    YFinanceFundamentalsRequest,
    YFinanceFundamentalsSourceStatus,
)


class TestVersionAssertion:
    def test_version_match(self):
        fake_module = SimpleNamespace(__version__="1.7.0")
        assert_yfinance_version(fake_module)

    def test_version_mismatch(self):
        fake_module = SimpleNamespace(__version__="1.5.1")
        with pytest.raises(YFinanceVersionMismatchError, match="yfinance version mismatch"):
            assert_yfinance_version(fake_module)

    def test_real_installed_yfinance_version(self):
        import yfinance

        assert_yfinance_version(yfinance)


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
            yf_module=SimpleNamespace(__version__="1.7.0"),
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
            yf_module=SimpleNamespace(__version__="1.7.0"),
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
            yf_module=SimpleNamespace(__version__="1.7.0"),
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
            yf_module=SimpleNamespace(__version__="1.7.0"),
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

    @staticmethod
    def _client(factory, **kwargs):
        return YFinanceClient(
            yf_module=SimpleNamespace(__version__="1.7.0"), ticker_factory=factory, **kwargs
        )

    def test_all_empty_is_unavailable_with_read_evidence(self):
        ticker = SimpleNamespace(info={})
        result = self._client(lambda symbol: ticker).fetch_fundamentals(
            YFinanceFundamentalsRequest(symbols=("EMPTY",))
        )

        assert result.records == {}
        symbol = result.symbol_results["EMPTY"]
        assert symbol.record is None
        assert symbol.outcome == YFinanceFundamentalsOutcome.UNAVAILABLE
        assert symbol.errors == ()
        assert symbol.sources["info"].status == YFinanceFundamentalsSourceStatus.EMPTY
        assert symbol.sources["info"].attempts[0].attempt == 1
        assert result.provenance.row_count == 0
        assert result.provenance.attempt_count == 8

    def test_partial_coverage_retains_data_and_missing_source_states(self):
        ticker = SimpleNamespace(info={"marketCap": 123.0})
        result = self._client(lambda symbol: ticker).fetch_fundamentals(
            YFinanceFundamentalsRequest(symbols=("PART",), include_estimates=False)
        )

        symbol = result.symbol_results["PART"]
        assert symbol.outcome == YFinanceFundamentalsOutcome.INCOMPLETE
        assert symbol.record is result.records["PART"]
        assert symbol.record.valuation.market_cap == 123.0
        assert (
            symbol.sources["quarterly_income_stmt"].status == YFinanceFundamentalsSourceStatus.EMPTY
        )
        assert "earnings_estimate" not in symbol.sources

    def test_success_after_transient_retry_records_attempts(self):
        calls = 0
        delays = []
        now = datetime(2026, 9, 23, tzinfo=UTC)

        def factory(symbol):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("credential=secret")
            return SimpleNamespace(info={"marketCap": 123.0})

        result = self._client(
            factory, sleep_fn=delays.append, random_value_fn=lambda: 0.5, clock=lambda: now
        ).fetch_fundamentals(
            YFinanceFundamentalsRequest(
                symbols=("GOOD",),
                include_financials=False,
                include_estimates=False,
                retry_policy=RetryPolicy(2, 1, 2, 1, 0),
            )
        )

        symbol = result.symbol_results["GOOD"]
        assert symbol.outcome == YFinanceFundamentalsOutcome.COMPLETE
        assert [a.exception_type for a in symbol.sources["ticker"].attempts] == [
            "TimeoutError",
            None,
        ]
        assert result.provenance.attempt_count == 4
        assert result.provenance.retrieved_at == now
        assert delays == [1.0]
        assert "secret" not in repr(symbol)
        assert "secret" not in repr(result.provenance.to_dict())

    def test_exhausted_transient_failure_is_per_symbol(self):
        def factory(symbol):
            if symbol == "BAD":
                raise TimeoutError("credential=secret")
            return SimpleNamespace(info={"marketCap": 123.0})

        result = self._client(factory).fetch_fundamentals(
            YFinanceFundamentalsRequest(
                symbols=("BAD", "GOOD"),
                include_financials=False,
                include_estimates=False,
                retry_policy=RetryPolicy(2, 0, 2, 0, 0),
            )
        )

        bad = result.symbol_results["BAD"]
        assert bad.outcome == YFinanceFundamentalsOutcome.TRANSIENT_FAILURE
        assert bad.record is None
        assert bad.errors[0].exception_type == "TimeoutError"
        assert len(bad.errors[0].attempts) == 2
        assert result.symbol_results["GOOD"].outcome == YFinanceFundamentalsOutcome.COMPLETE
        assert tuple(result.records) == ("GOOD",)
        assert result.provenance.row_count == 1

    def test_accessor_exception_and_malformed_info_are_permanent(self):
        class RaisingInfo:
            @property
            def info(self):
                raise RuntimeError("credential=secret")

        class RaisingAttributeError:
            @property
            def info(self):
                raise AttributeError("credential=secret")

        for ticker, expected in (
            (RaisingInfo(), "RuntimeError"),
            (RaisingAttributeError(), "AttributeError"),
            (SimpleNamespace(info=[]), "TypeError"),
        ):
            result = self._client(lambda symbol, ticker=ticker: ticker).fetch_fundamentals(
                YFinanceFundamentalsRequest(
                    symbols=("BAD",), include_financials=False, include_estimates=False
                )
            )
            symbol = result.symbol_results["BAD"]
            assert symbol.outcome == YFinanceFundamentalsOutcome.PERMANENT_FAILURE
            assert symbol.errors[0].source == "info"
            assert symbol.errors[0].exception_type == expected
            assert result.records == {}
            assert "secret" not in repr(result)

    def test_failed_estimate_read_retains_partial_record_and_error(self):
        class Ticker:
            def __init__(self):
                self.info = {"marketCap": 123.0}

            def get_revenue_estimate(self):
                raise OSError("credential=secret")

        result = self._client(lambda symbol: Ticker()).fetch_fundamentals(
            YFinanceFundamentalsRequest(
                symbols=("PART",),
                include_financials=False,
                retry_policy=RetryPolicy(2, 0, 2, 0, 0),
            )
        )

        symbol = result.symbol_results["PART"]
        assert symbol.outcome == YFinanceFundamentalsOutcome.INCOMPLETE
        assert symbol.record.valuation.market_cap == 123.0
        assert symbol.errors[0].source == "revenue_estimate"
        assert symbol.errors[0].status == YFinanceFundamentalsSourceStatus.TRANSIENT_FAILURE
        assert len(symbol.errors[0].attempts) == 2

    @pytest.mark.parametrize(
        ("status_code", "expected", "attempt_count"),
        [
            (429, YFinanceFundamentalsOutcome.TRANSIENT_FAILURE, 2),
            (401, YFinanceFundamentalsOutcome.PERMANENT_FAILURE, 1),
        ],
    )
    def test_http_status_classification(self, status_code, expected, attempt_count):
        class HTTPReadError(Exception):
            def __init__(self):
                self.response = SimpleNamespace(status_code=status_code)

        def factory(symbol):
            raise HTTPReadError()

        result = self._client(factory).fetch_fundamentals(
            YFinanceFundamentalsRequest(
                symbols=("BAD",),
                retry_policy=RetryPolicy(2, 0, 2, 0, 0),
            )
        )
        symbol = result.symbol_results["BAD"]
        assert symbol.outcome == expected
        assert len(symbol.errors[0].attempts) == attempt_count
        assert symbol.errors[0].exception_type == "HTTPReadError"

    def test_permission_error_is_not_retried(self):
        calls = 0

        def factory(symbol):
            nonlocal calls
            calls += 1
            raise PermissionError("credential=secret")

        result = self._client(factory).fetch_fundamentals(
            YFinanceFundamentalsRequest(symbols=("BAD",), retry_policy=RetryPolicy(3, 0, 2, 0, 0))
        )
        assert calls == 1
        assert result.symbol_results["BAD"].outcome == YFinanceFundamentalsOutcome.PERMANENT_FAILURE

"""Unit tests for fundamentals extraction, valuation, statements, and analyst estimates."""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from ohmydata.providers.yfinance.fundamentals import (
    YFinanceFundamentalsRequest,
    extract_estimates_horizons,
    extract_metric_pair,
    extract_quarterly_pair,
    extract_report_date,
    parse_symbol_fundamentals,
)


class TestFundamentalsExtractionHelpers:
    def test_extract_quarterly_pair_full(self):
        # 5 quarters: Q0, Q1, Q2, Q3, Q4 (YoY is Q4)
        series = pd.Series([100.0, 90.0, 80.0, 70.0, 85.0])
        latest, prev_year = extract_quarterly_pair(series)
        assert latest == 100.0
        assert prev_year == 85.0

    def test_extract_quarterly_pair_short(self):
        # Fewer than 5 quarters
        series = pd.Series([100.0, 90.0])
        latest, prev_year = extract_quarterly_pair(series)
        assert latest == 100.0
        assert prev_year is None

    def test_extract_metric_pair_with_aliases(self):
        stmt = pd.DataFrame(
            [[100.0, 90.0, 80.0, 70.0, 85.0]],
            index=["Operating Revenue"],
            columns=["2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31", "2024-12-31"],
        )
        latest, prev = extract_metric_pair(stmt, ["Total Revenue", "Operating Revenue", "Revenue"])
        assert latest == 100.0
        assert prev == 85.0

    def test_extract_report_date_rejects_epoch_zero(self):
        # 0 timestamp (1970-01-01) should be rejected
        info = {"mostRecentQuarter": 0}
        income_stmt = pd.DataFrame()
        assert extract_report_date(info, income_stmt) is None

    def test_extract_report_date_valid_timestamp(self):
        # 2025-09-30 UTC timestamp is ~1759276800
        dt_val = datetime.datetime(2025, 9, 30, tzinfo=datetime.UTC)
        ts = int(dt_val.timestamp())
        info = {"mostRecentQuarter": ts}
        income_stmt = pd.DataFrame()
        res = extract_report_date(info, income_stmt)
        assert res == datetime.date(2025, 9, 30)

    def test_extract_estimates_horizons(self):
        est_df = pd.DataFrame(
            {
                "avg": [1.5, 2.0, 6.0, 8.0],
            },
            index=["0q", "+1q", "0y", "+1y"],
        )
        cq, nq, cy, ny = extract_estimates_horizons(est_df)
        assert cq == 1.5
        assert nq == 2.0
        assert cy == 6.0
        assert ny == 8.0


class TestSymbolFundamentalsParsing:
    def test_parse_equity_fundamentals(self):
        info = {
            "quoteType": "EQUITY",
            "currency": "USD",
            "marketCap": 3_000_000_000_000,
            "enterpriseValue": 3_100_000_000_000,
            "sharesOutstanding": 15_000_000_000,
            "trailingPE": 30.5,
            "forwardPE": 26.2,
            "pegRatio": 1.8,
            "priceToSalesTrailing12Months": 7.5,
            "forwardEps": 6.5,
            "revenueGrowth": 0.12,
            "earningsGrowth": 0.15,
            "mostRecentQuarter": int(
                datetime.datetime(2025, 9, 30, tzinfo=datetime.UTC).timestamp()
            ),
        }
        income_stmt = pd.DataFrame(
            {
                "2025-09-30": [100_000_000, 15_000_000, 1.25],
                "2025-06-30": [95_000_000, 14_000_000, 1.20],
                "2025-03-31": [90_000_000, 13_000_000, 1.15],
                "2024-12-31": [85_000_000, 12_000_000, 1.10],
                "2024-09-30": [80_000_000, 11_000_000, 1.05],
            },
            index=["Total Revenue", "Operating Income", "Diluted EPS"],
        )

        res = parse_symbol_fundamentals("AAPL", info, income_stmt=income_stmt)

        assert res.symbol == "AAPL"
        assert res.is_excluded is False
        assert res.exclusion_reason is None
        assert res.report_date == datetime.date(2025, 9, 30)

        assert res.valuation.trailing_pe == 30.5
        assert res.valuation.forward_pe == 26.2
        assert res.valuation.currency == "USD"
        assert res.valuation.market_cap == 3_000_000_000_000

        assert res.financials.total_revenue_latest == 100_000_000
        assert res.financials.total_revenue_prev_year == 80_000_000
        assert res.financials.diluted_eps_latest == 1.25
        assert res.financials.diluted_eps_prev_year == 1.05

        # Strict null preservation: cashflow wasn't provided, must be None, NOT 0.0
        assert res.financials.operating_cash_flow_latest is None
        assert res.financials.free_cash_flow_latest is None

        # Flattened dictionary contains all expected fields
        d = res.to_dict()
        assert d["symbol"] == "AAPL"
        assert d["trailing_pe"] == 30.5
        assert d["total_revenue_latest"] == 100_000_000
        assert d["operating_cash_flow_latest"] is None

    def test_parse_etf_is_excluded(self):
        info = {
            "quoteType": "ETF",
            "marketCap": 500_000_000_000,
        }
        res = parse_symbol_fundamentals("SPY", info)
        assert res.symbol == "SPY"
        assert res.is_excluded is True
        assert res.exclusion_reason is not None and "non-equity" in res.exclusion_reason


class TestFundamentalsRequest:
    def test_request_validation(self):
        req = YFinanceFundamentalsRequest(symbols=("AAPL", "MSFT"))
        assert req.symbols == ("AAPL", "MSFT")

        with pytest.raises(ValueError, match="Duplicate symbols"):
            YFinanceFundamentalsRequest(symbols=("AAPL", "AAPL"))

        with pytest.raises(ValueError, match="empty"):
            YFinanceFundamentalsRequest(symbols=())

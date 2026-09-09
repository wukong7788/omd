"""Synthetic regressions for actual period identity; no provider calls."""

from datetime import date

import pandas as pd
import pytest

from ohmydata.providers.yfinance.fundamentals import (
    YFinanceAnalystEstimates,
    YFinanceQuarterlyFinancials,
    YFinanceSymbolFundamentals,
    extract_metric_pair,
    extract_quarterly_pair,
    parse_symbol_fundamentals,
)


def test_latest_missing_stays_missing():
    series = pd.Series([None, 11, 7], index=["2026-06-30", "2026-03-31", "2025-06-30"])
    assert extract_quarterly_pair(series) == (None, 7)


@pytest.mark.parametrize("reverse", [False, True])
def test_missing_quarter_and_order_do_not_shift_yoy(reverse):
    series = pd.Series(
        [12, 10, 9, 7], index=["2026-06-30", "2026-03-31", "2025-12-31", "2025-06-30"]
    )
    assert extract_quarterly_pair(series.iloc[::-1] if reverse else series) == (12, 7)


def test_metadata_does_not_relabel_statement():
    stmt = pd.DataFrame({"2026-03-31": [11]}, index=["Total Revenue"])
    rec = parse_symbol_fundamentals("FAKE", {"mostRecentQuarter": "2026-06-30"}, stmt)
    assert rec.report_date == date(2026, 3, 31)


def test_cross_table_old_value_does_not_join_new_period():
    income = pd.DataFrame({"2026-06-30": [12]}, index=["Total Revenue"])
    cash = pd.DataFrame({"2026-03-31": [3]}, index=["Operating Cash Flow"])
    rec = parse_symbol_fundamentals("FAKE", {}, income, cashflow_stmt=cash)
    assert rec.financials.operating_cash_flow_latest is None


def test_ebit_is_not_operating_income():
    income = pd.DataFrame({"2026-06-30": [12]}, index=["EBIT"])
    assert parse_symbol_fundamentals("FAKE", {}, income).financials.operating_income_latest is None


def test_conflicting_duplicate_period_rejected():
    series = pd.Series([12, 13], index=["2026-06-30", "2026-06-30"])
    with pytest.raises(ValueError, match="conflict"):
        extract_quarterly_pair(series)


def test_week_calendar_yoy():
    series = pd.Series([12, 7], index=["2025-02-01", "2024-02-03"])
    assert extract_quarterly_pair(series) == (12, 7)


def test_no_quote_does_not_calibrate_implied_price():
    rec = parse_symbol_fundamentals(
        "FAKE",
        {"forwardPE": 20, "forwardEps": 4},
        eps_estimate_df=pd.DataFrame({"avg": [2]}, index=["0y"]),
    )
    assert rec.valuation.forward_pe == 20
    assert rec.valuation.forward_pe_source == "RAW_FALLBACK"


def test_metadata_and_native_values_survive_flattening():
    income = pd.DataFrame(
        {"2026-06-30": [None], "2025-06-30": ["7.125"]}, index=["Operating Income"]
    )
    rec = parse_symbol_fundamentals("FAKE", {}, income)
    period = next(p for p in rec.financials.metric_periods if p.metric == "operating_income")
    assert period.latest_period == date(2026, 6, 30)
    assert period.prior_period == date(2025, 6, 30)
    assert period.prior_native == "7.125"
    assert period.source_row == "Operating Income"
    assert period.coverage == "missing_value"
    assert "operating_income:missing_value" in rec.coverage_flags
    assert rec.to_dict()["metric_periods"][2]["prior_native"] == "7.125"


def test_duplicate_identical_and_null_values():
    assert extract_quarterly_pair(pd.Series([7, 7], index=["2026-06-30"] * 2)) == (7, None)
    assert extract_quarterly_pair(pd.Series([pd.NA, pd.NA], index=["2026-06-30"] * 2)) == (
        None,
        None,
    )


def test_ambiguous_anniversary_rejected():
    series = pd.Series([12, 7, 8], index=["2026-06-30", "2025-06-28", "2025-06-30"])
    with pytest.raises(ValueError, match="ambiguous"):
        extract_quarterly_pair(series)


def test_undated_series_cannot_provide_quarterly_identity():
    with pytest.raises(TypeError, match="actual dates"):
        extract_quarterly_pair(pd.Series([1, 2]))


@pytest.mark.parametrize("currency", [None, "EUR"])
def test_quote_currency_unknown_or_mismatched_keeps_raw(currency):
    rec = parse_symbol_fundamentals(
        "FAKE",
        {
            "forwardPE": 20,
            "forwardEps": 4,
            "currentPrice": 100,
            "currency": "USD",
            "financialCurrency": currency,
        },
        eps_estimate_df=pd.DataFrame({"avg": [2]}, index=["0y"]),
    )
    assert rec.valuation.forward_pe == 20
    assert rec.valuation.quote_price == 100
    assert rec.valuation.forward_eps_period is None


def test_actual_quote_and_forecast_provenance():
    rec = parse_symbol_fundamentals(
        "FAKE",
        {
            "forwardPE": 20,
            "forwardEps": 4,
            "regularMarketPrice": 100,
            "regularMarketTime": 1788278400,
            "currency": "USD",
            "financialCurrency": "USD",
        },
        eps_estimate_df=pd.DataFrame({"avg": [2]}, index=["0y"]),
    )
    assert rec.valuation.forward_pe == 50
    assert rec.valuation.quote_source == "info.regularMarketPrice"
    assert rec.valuation.quote_time is not None
    assert rec.valuation.quote_time.timestamp() == 1788278400
    assert rec.valuation.forward_eps_period == "FY1"
    assert rec.valuation.forward_eps_source == "earnings_estimate.0y.avg"
    assert rec.estimates.accounting_basis_comparability == "unknown"


def test_prior_null_is_not_replaced_by_another_quarter():
    series = pd.Series([12, None, 8], index=["2026-06-30", "2025-06-30", "2025-03-31"])
    assert extract_quarterly_pair(series) == (12, None)


def test_each_metric_has_distinct_source_identity():
    cash = pd.DataFrame({"2026-06-30": [10, 6]}, index=["Operating Cash Flow", "Free Cash Flow"])
    balance = pd.DataFrame({"2026-06-30": [30, 20]}, index=["Total Debt", "Net Debt"])
    rec = parse_symbol_fundamentals("FAKE", {}, balance_stmt=balance, cashflow_stmt=cash)
    periods = {p.metric: p for p in rec.financials.metric_periods}
    assert len(periods) == len(rec.financials.metric_periods)
    assert periods["free_cash_flow"].latest_native == "6"
    assert periods["operating_cash_flow"].latest_native == "10"
    assert periods["net_debt"].source_row == "Net Debt"
    assert periods["total_debt"].source_row == "Total Debt"


def test_existing_positional_dataclass_constructors():
    financials = YFinanceQuarterlyFinancials(100, 90)
    assert financials.total_revenue_latest == 100
    assert financials.total_revenue_prev_year == 90
    assert financials.metric_periods == ()
    symbol = YFinanceSymbolFundamentals("FAKE", None, "EQUITY", False)
    assert symbol.quote_type == "EQUITY"
    assert symbol.provider_report_date is None
    estimates = YFinanceAnalystEstimates(
        None, None, None, None, None, None, None, None, None, None, True
    )
    assert estimates.has_gaap_distortion is True
    assert estimates.accounting_basis_comparability == "unknown"


def test_normalized_duplicate_metric_rows_rejected_by_public_helper():
    stmt = pd.DataFrame({"2026-06-30": [1, 2]}, index=[" Total Revenue", "total revenue"])
    with pytest.raises(ValueError, match="duplicate"):
        extract_metric_pair(stmt, ["Total Revenue"])


def test_current_price_cannot_borrow_regular_market_time():
    rec = parse_symbol_fundamentals(
        "FAKE",
        {
            "currentPrice": 101,
            "regularMarketPrice": 100,
            "regularMarketTime": 1788278400,
        },
    )
    assert rec.valuation.quote_price == 101
    assert rec.valuation.quote_source == "info.currentPrice"
    assert rec.valuation.quote_time is None

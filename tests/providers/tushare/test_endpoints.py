# pyright: reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportCallIssue=false, reportArgumentType=false

import inspect

import pytest

from ohmydata.providers import tushare
from ohmydata.providers.tushare import (
    DailyBasicRequest,
    EmptyPolicy,
    FundAdjustmentRequest,
    FundBasicRequest,
    FundDailyRequest,
    FundDividendRequest,
    FundNavRequest,
    FundPortfolioRequest,
    FundShareRequest,
    IndexWeightRequest,
    TradeCalendarRequest,
)


def test_request_spec_is_canonical_and_validates_dates():
    request = TradeCalendarRequest(
        empty_policy=EmptyPolicy.ALLOW, exchange="SSE", start_date="20240101", end_date="20240102"
    )
    assert request.spec.effective_parameters["exchange"] == "SSE"
    assert request.spec.fields[1] == "cal_date"
    with pytest.raises(ValueError):
        FundDailyRequest(
            empty_policy=EmptyPolicy.ALLOW, ts_code="X", start_date="20240102", end_date="20240101"
        )


def test_empty_policy_rejects_raw_strings():
    with pytest.raises(TypeError):
        TradeCalendarRequest(empty_policy="ALLOW")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TradeCalendarRequest(),
        lambda: FundBasicRequest(),
        lambda: FundDailyRequest(ts_code="FAKE"),
        lambda: FundAdjustmentRequest(ts_code="FAKE"),
        lambda: FundNavRequest(ts_code="FAKE"),
        lambda: FundShareRequest(ts_code="FAKE"),
        lambda: FundDividendRequest(ts_code="FAKE"),
        lambda: FundPortfolioRequest(ts_code="FAKE", period="20231231"),
        lambda: DailyBasicRequest(ts_code="FAKE"),
        lambda: IndexWeightRequest(index_code="FAKE", trade_date="20240101"),
    ],
)
def test_empty_policy_is_required(factory):
    with pytest.raises(TypeError):
        factory()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TradeCalendarRequest(empty_policy="ALLOW"),
        lambda: FundBasicRequest(empty_policy="ALLOW"),
        lambda: FundDailyRequest(empty_policy="ALLOW", ts_code="FAKE"),
        lambda: FundAdjustmentRequest(empty_policy="ALLOW", ts_code="FAKE"),
        lambda: FundNavRequest(empty_policy="ALLOW", ts_code="FAKE"),
        lambda: FundShareRequest(empty_policy="ALLOW", ts_code="FAKE"),
        lambda: FundDividendRequest(empty_policy="ALLOW", ts_code="FAKE"),
        lambda: FundPortfolioRequest(empty_policy="ALLOW", ts_code="FAKE", period="20231231"),
        lambda: DailyBasicRequest(empty_policy="ALLOW", ts_code="FAKE"),
        lambda: IndexWeightRequest(empty_policy="ALLOW", index_code="FAKE", trade_date="20240101"),
    ],
)
def test_all_requests_reject_raw_empty_policy(factory):
    with pytest.raises(TypeError):
        factory()


def test_request_validation_and_explicit_fields():
    with pytest.raises(ValueError):
        FundDailyRequest(empty_policy=EmptyPolicy.ALLOW)
    with pytest.raises(ValueError):
        FundDailyRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="X", trade_date="20240101")
    with pytest.raises(ValueError):
        FundShareRequest(empty_policy=EmptyPolicy.ALLOW)
    with pytest.raises(ValueError):
        FundNavRequest(empty_policy=EmptyPolicy.ALLOW, nav_date="20240101", start_date="20240101")
    request = FundAdjustmentRequest(
        empty_policy=EmptyPolicy.ALLOW,
        ts_code="X",
        page_size=2,
        max_pages=3,
        fields=("ts_code", "trade_date", "adj_factor"),
    )
    assert request.spec.effective_parameters["limit"] == 2
    assert request.spec.fields == ("ts_code", "trade_date", "adj_factor")


def test_public_exports_are_exact_and_no_arbitrary_fetch():
    assert set(tushare.__all__) == {
        "AdjustedEtfBarsRequest",
        "AdjustedEtfBarsResult",
        "AdjustmentCoveragePolicy",
        "DividendYieldCoveragePolicy",
        "DividendYieldWeightSource",
        "DailyBasicRequest",
        "EmptyPolicy",
        "FundAdjustmentRequest",
        "FundBasicRequest",
        "FundDailyRequest",
        "FundDividendRequest",
        "FundNavRequest",
        "FundPortfolioRequest",
        "FundShareRequest",
        "IndexWeightRequest",
        "TradeCalendarRequest",
        "TushareClient",
        "TushareFetchResult",
        "WeightedDividendYieldResult",
        "build_index_dividend_yield",
        "build_portfolio_dividend_yield",
        "classify_tushare_exception",
        "build_adjusted_etf_bars",
        "fetch_adjusted_etf_bars",
    }
    assert "fetch" not in dir(tushare.TushareClient)
    source = inspect.getsource(tushare.TushareClient)
    assert "os.environ" not in source and "dotenv" not in source and "tushare.pro_api" not in source


def test_phase2b_request_shapes_and_bounds():
    with pytest.raises(ValueError):
        FundDividendRequest(empty_policy=EmptyPolicy.ALLOW, ts_code=" ")
    assert (
        FundDividendRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A").spec.fields[0] == "ts_code"
    )
    with pytest.raises(ValueError):
        FundDividendRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", ann_date="20240101")
    with pytest.raises(ValueError):
        FundPortfolioRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A")
    assert (
        FundPortfolioRequest(
            empty_policy=EmptyPolicy.ALLOW, ts_code="A", period="20231231"
        ).spec.effective_parameters["period"]
        == "20231231"
    )
    with pytest.raises(ValueError):
        FundPortfolioRequest(
            empty_policy=EmptyPolicy.ALLOW, ts_code="A", start_date="20230101", end_date="20240201"
        )
    with pytest.raises(ValueError):
        DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", trade_date="20240101")
    with pytest.raises(ValueError):
        IndexWeightRequest(empty_policy=EmptyPolicy.ALLOW, index_code=" ", trade_date="20240101")
    with pytest.raises(ValueError):
        IndexWeightRequest(
            empty_policy=EmptyPolicy.ALLOW,
            index_code="I",
            start_date="20240101",
            end_date="20240201",
        )


def test_phase2b_fund_dividend_selector_matrix_and_dates():
    for field in ("ts_code", "ann_date", "ex_date", "pay_date"):
        kwargs = {field: "A" if field == "ts_code" else "20240101"}
        request = FundDividendRequest(empty_policy=EmptyPolicy.ALLOW, **kwargs)
        assert request.spec.effective_parameters[field] == kwargs[field]
    for kwargs in (
        {},
        {"ts_code": "A", "ann_date": "20240101"},
        {"ts_code": " "},
        {"ann_date": "2024-01-01"},
    ):
        with pytest.raises(ValueError):
            FundDividendRequest(empty_policy=EmptyPolicy.ALLOW, **kwargs)


def test_phase2b_fund_portfolio_report_selector_matrix():
    FundPortfolioRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", ann_date="20240101")
    FundPortfolioRequest(
        empty_policy=EmptyPolicy.ALLOW, ts_code="A", period="20231231", ann_date="20240101"
    )
    FundPortfolioRequest(
        empty_policy=EmptyPolicy.ALLOW,
        ts_code="A",
        start_date="20240101",
        end_date="20241231",
        ann_date="20250101",
    )
    invalid = [
        {},
        {"period": "20231231", "start_date": "20230101", "end_date": "20230131"},
        {"start_date": "20230101"},
        {"start_date": "20230101", "end_date": "20240201"},
    ]
    for extra in invalid:
        with pytest.raises(ValueError):
            FundPortfolioRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", **extra)


def test_phase2b_daily_basic_selector_matrix_and_range_rules():
    DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A")
    DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, trade_date="20240101")
    DailyBasicRequest(
        empty_policy=EmptyPolicy.ALLOW, ts_code="A", start_date="20240101", end_date="20240131"
    )
    for kwargs in (
        {},
        {"ts_code": "A", "trade_date": "20240101"},
        {"start_date": "20240101", "end_date": "20240131"},
        {"ts_code": "A", "start_date": "20240102", "end_date": "20240101"},
    ):
        with pytest.raises(ValueError):
            DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, **kwargs)


def test_phase2b_index_weight_selector_matrix_and_default_fields():
    exact = IndexWeightRequest(
        empty_policy=EmptyPolicy.ALLOW, index_code="I", trade_date="20240101"
    )
    month = IndexWeightRequest(
        empty_policy=EmptyPolicy.ALLOW, index_code="I", start_date="20240101", end_date="20240131"
    )
    assert exact.spec.fields == ("index_code", "con_code", "trade_date", "weight")
    assert month.spec.effective_parameters["start_date"] == "20240101"
    for kwargs in (
        {},
        {"trade_date": "20240101", "start_date": "20240101", "end_date": "20240131"},
        {"start_date": "20240101"},
        {"start_date": "20240101", "end_date": "20240201"},
        {"trade_date": "2024-01-01"},
    ):
        with pytest.raises(ValueError):
            IndexWeightRequest(empty_policy=EmptyPolicy.ALLOW, index_code="I", **kwargs)


def test_phase2b_custom_fields_preserve_order_and_required_keys():
    request = DailyBasicRequest(
        empty_policy=EmptyPolicy.ALLOW, ts_code="A", fields=("ts_code", "trade_date", "dv_ttm")
    )
    assert request.fields == ("ts_code", "trade_date", "dv_ttm")
    with pytest.raises(ValueError):
        DailyBasicRequest(
            empty_policy=EmptyPolicy.ALLOW, ts_code="A", fields=("ts_code", "ts_code")
        )

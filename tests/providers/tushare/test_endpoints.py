# pyright: reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportCallIssue=false, reportArgumentType=false

import inspect

import pytest

from ohmydata.providers import tushare
from ohmydata.providers.tushare import (
    DailyBasicRequest,
    EmptyPolicy,
    EtfBasicRequest,
    EtfShConsRequest,
    EtfSzConsRequest,
    FundAdjustmentRequest,
    FundBasicRequest,
    FundDailyRequest,
    FundDividendRequest,
    FundNavRequest,
    FundPortfolioRequest,
    FundShareRequest,
    IndexWeightRequest,
    StockAdjustmentRequest,
    StockDailyRequest,
    StockDividendRequest,
    TradeCalendarRequest,
)

SH_FIELDS = (
    "trade_date",
    "ts_code",
    "con_code",
    "con_name",
    "qty",
    "sub_flag",
    "cpr",
    "rdr",
    "sca",
    "exchange",
)
SZ_FIELDS = (
    "trade_date",
    "ts_code",
    "con_code",
    "con_name",
    "qty",
    "sub_flag",
    "cpr",
    "rdr",
    "sub_cc",
    "red_cc",
    "exchange",
)


@pytest.mark.parametrize(
    "request_type, suffix, fields",
    [(EtfShConsRequest, ".SH", SH_FIELDS), (EtfSzConsRequest, ".SZ", SZ_FIELDS)],
)
def test_etf_cons_request_contract(request_type, suffix, fields):
    request = request_type(
        empty_policy=EmptyPolicy.ALLOW,
        ts_code=f"510050{suffix}",
        start_date="20240101",
        end_date="20240131",
    )
    assert request.endpoint in {"etf_sh_cons", "etf_sz_cons"}
    assert request.fields == fields
    assert request.spec.effective_parameters == {
        "ts_code": f"510050{suffix}",
        "start_date": "20240101",
        "end_date": "20240131",
    }
    explicit = request_type(
        empty_policy=EmptyPolicy.ERROR,
        con_code="000001.SZ",
        trade_date="20240102",
        fields=("trade_date", "ts_code", "con_code"),
    )
    assert explicit.fields == ("trade_date", "ts_code", "con_code")


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"ts_code": "   "},
        {"ts_code": ","},
        {"ts_code": "510050.SH,,"},
        {"trade_date": ""},
        {"con_code": "  "},
        {"con_code": ","},
        {"con_code": "000001.SZ,,000002.SZ"},
        {"start_date": "20240101"},
        {"end_date": "20240102"},
        {"start_date": "20240102", "end_date": "20240101"},
        {"trade_date": "20240230"},
        {"start_date": "2024010x", "end_date": "20240102"},
    ],
)
def test_etf_cons_rejects_invalid_selectors(kwargs):
    with pytest.raises(ValueError):
        EtfShConsRequest(empty_policy=EmptyPolicy.ALLOW, **kwargs)


def test_etf_cons_suffix_mismatch_and_schema_separation():
    with pytest.raises(ValueError):
        EtfShConsRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="159001.SZ")
    with pytest.raises(ValueError):
        EtfSzConsRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="510050.SH")
    assert "sca" in SH_FIELDS and "sub_cc" not in SH_FIELDS
    assert "sub_cc" in SZ_FIELDS and "red_cc" in SZ_FIELDS and "sca" not in SZ_FIELDS


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


def test_stock_request_orientations_defaults_and_custom_fields():
    daily = StockDailyRequest(empty_policy=EmptyPolicy.ALLOW, ts_code=" 000001.SZ ")
    assert daily.spec.endpoint == "daily"
    assert daily.spec.effective_parameters == {"ts_code": " 000001.SZ "}
    assert daily.fields == (
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "change",
        "pct_chg",
        "vol",
        "amount",
    )
    adjustment = StockAdjustmentRequest(
        empty_policy=EmptyPolicy.ALLOW,
        trade_date="20240102",
        fields=("trade_date", "adj_factor", "ts_code"),
    )
    assert adjustment.spec.effective_parameters == {"trade_date": "20240102"}
    assert adjustment.spec.fields == ("trade_date", "adj_factor", "ts_code")


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"ts_code": ""},
        {"ts_code": " "},
        {"ts_code": "A", "trade_date": "20240101"},
        {"trade_date": "2024-01-01"},
        {"trade_date": "20240101", "start_date": "20240101"},
        {"ts_code": "A", "start_date": "20240102", "end_date": "20240101"},
    ],
)
def test_stock_request_validation(kwargs):
    with pytest.raises(ValueError):
        StockDailyRequest(empty_policy=EmptyPolicy.ALLOW, **kwargs)


def test_stock_public_exports():
    assert StockDailyRequest is tushare.StockDailyRequest
    assert StockAdjustmentRequest is tushare.StockAdjustmentRequest
    assert StockDividendRequest is tushare.StockDividendRequest


def test_stock_dividend_request_selectors_fields_and_validation():
    request = StockDividendRequest(
        empty_policy=EmptyPolicy.ALLOW,
        ts_code=" 000001.SZ ",
        ann_date="20240102",
        record_date="20240103",
        ex_date="20240104",
        imp_ann_date="20240105",
    )
    assert request.spec.effective_parameters == {
        "ts_code": " 000001.SZ ",
        "ann_date": "20240102",
        "record_date": "20240103",
        "ex_date": "20240104",
        "imp_ann_date": "20240105",
    }
    assert request.fields == (
        "ts_code",
        "end_date",
        "ann_date",
        "div_proc",
        "stk_div",
        "stk_bo_rate",
        "stk_co_rate",
        "cash_div",
        "cash_div_tax",
        "record_date",
        "ex_date",
        "pay_date",
        "div_listdate",
        "imp_ann_date",
        "base_date",
        "base_share",
    )
    assert StockDividendRequest(
        empty_policy=EmptyPolicy.ALLOW, ex_date="20240101", fields=("cash_div", "ts_code")
    ).fields == ("cash_div", "ts_code")
    for kwargs in ({}, {"ts_code": ""}, {"ts_code": " "}, {"ann_date": "2024-01-01"}):
        with pytest.raises(ValueError):
            StockDividendRequest(empty_policy=EmptyPolicy.ALLOW, **kwargs)
    with pytest.raises(ValueError):
        StockDividendRequest(
            empty_policy=EmptyPolicy.ALLOW, ex_date="20240101", fields=("cash_div",)
        )


@pytest.mark.parametrize(
    "selector", ["ts_code", "ann_date", "record_date", "ex_date", "imp_ann_date"]
)
def test_stock_dividend_each_selector_is_forwardable(selector):
    value = "A" if selector == "ts_code" else "20240101"
    request = StockDividendRequest(empty_policy=EmptyPolicy.ALLOW, **{selector: value})
    assert request.spec.effective_parameters == {selector: value}


def test_stock_dividend_requires_explicit_empty_policy():
    with pytest.raises(TypeError):
        StockDividendRequest(ts_code="A")
    with pytest.raises(TypeError):
        StockDividendRequest(empty_policy="ALLOW", ts_code="A")


@pytest.mark.parametrize("request_type", [StockDailyRequest, StockAdjustmentRequest])
def test_stock_requests_require_explicit_empty_policy(request_type):
    with pytest.raises(TypeError):
        request_type(ts_code="A")
    with pytest.raises(TypeError):
        request_type(empty_policy="ALLOW", ts_code="A")


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TradeCalendarRequest(),
        lambda: FundBasicRequest(),
        lambda: EtfBasicRequest(),
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
        lambda: EtfBasicRequest(empty_policy="ALLOW"),
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


@pytest.mark.parametrize("request_type", [FundNavRequest, FundShareRequest])
@pytest.mark.parametrize("value", ["20240230", "2024-02-01", "2024010"])
def test_fund_nav_share_reject_non_calendar_dates(request_type, value):
    kwargs = {"ts_code": "A", "start_date": value}
    with pytest.raises(ValueError):
        request_type(empty_policy=EmptyPolicy.ALLOW, **kwargs)


def test_fund_nav_share_selectors_require_nonempty_values():
    with pytest.raises(ValueError):
        FundNavRequest(empty_policy=EmptyPolicy.ALLOW, ts_code=" ")
    with pytest.raises(ValueError):
        FundNavRequest(empty_policy=EmptyPolicy.ALLOW, nav_date=" ")
    with pytest.raises(ValueError):
        FundShareRequest(empty_policy=EmptyPolicy.ALLOW, market=" ")


@pytest.mark.parametrize("request_type", [FundNavRequest, FundShareRequest])
@pytest.mark.parametrize("kwargs", [{"start_date": "20240101"}, {"end_date": "20240102"}])
def test_fund_nav_share_partial_ranges_with_symbol(request_type, kwargs):
    request = request_type(empty_policy=EmptyPolicy.ALLOW, ts_code="A", **kwargs)
    assert request.spec.effective_parameters["ts_code"] == "A"


def test_fund_nav_share_ranges_require_symbol_when_date_selector_used():
    with pytest.raises(ValueError):
        FundNavRequest(empty_policy=EmptyPolicy.ALLOW, nav_date="20240101", end_date="20240102")
    with pytest.raises(ValueError):
        FundShareRequest(
            empty_policy=EmptyPolicy.ALLOW, trade_date="20240101", start_date="20240101"
        )
    with pytest.raises(ValueError):
        FundShareRequest(empty_policy=EmptyPolicy.ALLOW, market="E", end_date="20240102")


def test_fund_nav_share_range_provider_parameters_are_exact():
    nav = FundNavRequest(
        empty_policy=EmptyPolicy.ALLOW, ts_code="A", start_date="20240101", end_date="20240102"
    )
    share = FundShareRequest(
        empty_policy=EmptyPolicy.ALLOW, ts_code="A,B", start_date="20240101", end_date="20240102"
    )
    assert nav.spec.effective_parameters == {
        "ts_code": "A",
        "start_date": "20240101",
        "end_date": "20240102",
    }
    assert share.spec.effective_parameters == {
        "ts_code": "A,B",
        "start_date": "20240101",
        "end_date": "20240102",
    }


def test_etf_basic_spec_defaults_filters_and_date_validation():
    request = EtfBasicRequest(
        empty_policy=EmptyPolicy.ALLOW,
        index_code="000300.SH",
        list_date="20240101",
        list_status="L",
        exchange="SH",
        mgr="FAKE_MANAGER",
        market="E",
    )
    assert request.spec.endpoint == "etf_basic"
    assert request.spec.fields == (
        "ts_code",
        "csname",
        "extname",
        "cname",
        "index_code",
        "index_name",
        "setup_date",
        "list_date",
        "list_status",
        "exchange",
        "mgr_name",
        "custod_name",
        "mgt_fee",
        "etf_type",
    )
    assert request.spec.effective_parameters == {
        "index_code": "000300.SH",
        "list_date": "20240101",
        "list_status": "L",
        "exchange": "SH",
        "mgr": "FAKE_MANAGER",
        "market": "E",
    }
    with pytest.raises(ValueError):
        EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, list_date="2024-01-01")
    assert EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, fields=("cname",)).fields == ("cname",)


def test_public_exports_are_exact_and_no_arbitrary_fetch():
    assert set(tushare.__all__) == {
        "AdjustedEtfBarsRequest",
        "AdjustedEtfBarsResult",
        "AdjustmentCoveragePolicy",
        "DividendYieldCoveragePolicy",
        "DividendYieldWeightSource",
        "DailyBasicRequest",
        "EmptyPolicy",
        "EtfBasicRequest",
        "EtfShConsRequest",
        "EtfSzConsRequest",
        "EtfPcfHistoryRequest",
        "EtfPcfHistoryResult",
        "FundAdjustmentRequest",
        "FundBasicRequest",
        "FundDailyRequest",
        "FundDividendRequest",
        "FundNavRequest",
        "FundPortfolioRequest",
        "FundShareRequest",
        "IndexWeightRequest",
        "TradeCalendarRequest",
        "StockAdjustmentRequest",
        "StockDailyRequest",
        "StockDividendRequest",
        "TushareClient",
        "TushareFetchResult",
        "WeightedDividendYieldResult",
        "build_index_dividend_yield",
        "build_portfolio_dividend_yield",
        "classify_tushare_exception",
        "build_adjusted_etf_bars",
        "fetch_adjusted_etf_bars",
        "fetch_etf_pcf_history",
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
        {"end_date": "20240131"},
        {"trade_date": "20240101", "start_date": "20240101"},
        {"trade_date": "20240101", "end_date": "20240131"},
        {"start_date": "20240101", "end_date": "20240201"},
        {"start_date": "20240132", "end_date": "20240131"},
        {"trade_date": "20240230"},
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
    index = IndexWeightRequest(
        empty_policy=EmptyPolicy.ALLOW,
        index_code="I",
        trade_date="20240101",
        fields=("weight", "trade_date", "index_code", "con_code"),
    )
    assert index.fields == ("weight", "trade_date", "index_code", "con_code")


@pytest.mark.parametrize("value", ["20240230", "20241301", "2024-01-01", "2024011"])
def test_daily_basic_rejects_non_calendar_dates(value):
    with pytest.raises(ValueError):
        DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", start_date=value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trade_date": "20240101", "start_date": "20240101"},
        {"trade_date": "20240101", "end_date": "20240101"},
        {"start_date": "20240101"},
        {"end_date": "20240101"},
        {"ts_code": " "},
    ],
)
def test_daily_basic_rejects_mixed_or_missing_orientation(kwargs):
    with pytest.raises(ValueError):
        DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, **kwargs)

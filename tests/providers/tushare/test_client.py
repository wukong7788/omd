# pyright: reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false

from datetime import UTC, datetime

import pandas as pd
import pytest

from ohmydata.core import (
    AuthenticationError,
    CoverageError,
    EmptyDisposition,
    EmptyResponseError,
    PaginationError,
    PermanentProviderError,
    PermissionDeniedError,
    RateLimiter,
    RateLimitPolicy,
    RetryExhaustedError,
    RetryPolicy,
    SchemaMismatchError,
    TransientProviderError,
)
from ohmydata.core.snapshot import SnapshotStore
from ohmydata.providers.tushare import (
    DailyBasicRequest,
    EmptyPolicy,
    EtfBasicRequest,
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
    TushareClient,
)

DAILY_FIELDS = [
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
]


def daily_frame(rows=(("FAKE.ETF", "20240102"),)):
    return pd.DataFrame(
        {
            "ts_code": [r[0] for r in rows],
            "trade_date": [r[1] for r in rows],
            "open": pd.Series([1.0] * len(rows), dtype="float64"),
            "high": [1.1] * len(rows),
            "low": [0.9] * len(rows),
            "close": [1.05] * len(rows),
            "pre_close": [1.0] * len(rows),
            "change": [0.05] * len(rows),
            "pct_chg": [5.0] * len(rows),
            "vol": pd.Series([100] * len(rows), dtype="int64"),
            "amount": [250.0] * len(rows),
        }
    )


class Fake:
    def __init__(self, result=None, errors=()):
        self.result = result if result is not None else daily_frame()
        self.errors = list(errors)
        self.calls = []

    def fund_daily(self, **kwargs):
        self.calls.append(kwargs)
        if self.errors:
            error = self.errors.pop(0)
            raise error
        return self.result

    def daily(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    def adj_factor(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    def fund_basic(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    def fund_nav(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    def etf_basic(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    def dividend(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def client(fake, **kwargs):
    kwargs.setdefault("clock", lambda: datetime(2024, 1, 2, tzinfo=UTC))
    return TushareClient(fake, **kwargs)


def test_client_sorts_exact_kwargs_and_isolates_input_and_result():
    source = daily_frame((("FAKE.ETF", "20240102"), ("FAKE.ETF", "20240101")))
    fake = Fake(source)
    result = client(fake).fetch_fund_daily(
        FundDailyRequest(empty_policy=EmptyPolicy.ERROR, ts_code="FAKE.ETF")
    )
    assert fake.calls == [{"ts_code": "FAKE.ETF", "fields": ",".join(DAILY_FIELDS)}]
    assert result.frame.trade_date.tolist() == ["20240101", "20240102"]
    assert result.frame.vol.dtype == "int64"
    source.loc[0, "close"] = 99
    returned = result.frame
    returned.loc[0, "close"] = 88
    assert result.frame.loc[0, "close"] != 88
    assert result.provenance.endpoint == "fund_daily"
    assert result.provenance.columns == tuple(DAILY_FIELDS)
    assert result.provenance.empty_disposition is EmptyDisposition.NOT_EMPTY
    assert result.page_count == 1


def test_readme_injected_client_example_contract():
    fake = Fake(daily_frame())
    result = client(fake).fetch_fund_daily(
        FundDailyRequest(
            empty_policy=EmptyPolicy.ERROR,
            ts_code="FAKE.ETF",
            start_date="20240101",
            end_date="20240102",
        )
    )
    assert result.frame.loc[0, "ts_code"] == "FAKE.ETF"


def test_stock_daily_and_adjustment_preserve_native_values_and_kwargs():
    source = daily_frame((("B", "20240102"), ("A", "20240101")))
    source.loc[0, "pct_chg"] = None
    source.loc[0, "vol"] = None
    source.loc[0, "amount"] = None
    fake = Fake(source)
    result = client(fake).fetch_stock_daily(
        StockDailyRequest(
            empty_policy=EmptyPolicy.ALLOW, ts_code="A", start_date="20240101", end_date="20240102"
        )
    )
    assert fake.calls[0] == {
        "ts_code": "A",
        "start_date": "20240101",
        "end_date": "20240102",
        "fields": ",".join(DAILY_FIELDS),
    }
    assert result.frame.trade_date.tolist() == ["20240101", "20240102"]
    assert pd.isna(result.frame.loc[1, "pct_chg"])
    assert pd.isna(result.frame.loc[1, "vol"])
    assert pd.isna(result.frame.loc[1, "amount"])

    adj = pd.DataFrame({"ts_code": ["A"], "trade_date": ["20240101"], "adj_factor": [None]})
    fake = Fake(adj)
    adjustment = client(fake).fetch_stock_adjustment(
        StockAdjustmentRequest(empty_policy=EmptyPolicy.ALLOW, trade_date="20240101")
    )
    assert fake.calls == [{"trade_date": "20240101", "fields": "ts_code,trade_date,adj_factor"}]
    assert pd.isna(adjustment.frame.loc[0, "adj_factor"])


@pytest.mark.parametrize(
    "method, req, fields",
    [
        (
            "fetch_stock_daily",
            StockDailyRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A"),
            DAILY_FIELDS,
        ),
        (
            "fetch_stock_adjustment",
            StockAdjustmentRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A"),
            ["ts_code", "trade_date", "adj_factor"],
        ),
    ],
)
def test_stock_endpoints_reject_missing_methods_and_ambiguous_cap(method, req, fields):
    with pytest.raises(SchemaMismatchError):
        getattr(client(object()), method)(req)
    frame = pd.DataFrame({field: [None] * 6000 for field in fields})
    frame["ts_code"] = [f"A{i}" for i in range(6000)]
    frame["trade_date"] = ["20240101"] * 6000
    with pytest.raises(PaginationError):
        getattr(client(Fake(frame)), method)(req)


@pytest.mark.parametrize(
    "method, request_type, endpoint_fields",
    [
        ("fetch_stock_daily", StockDailyRequest, DAILY_FIELDS),
        ("fetch_stock_adjustment", StockAdjustmentRequest, ["ts_code", "trade_date", "adj_factor"]),
    ],
)
def test_stock_client_schema_and_empty_failures(method, request_type, endpoint_fields):
    class NoCall:
        def __init__(self, result):
            self.result = result
            self.calls = 0

        def daily(self, **kwargs):
            self.calls += 1
            return self.result

        def adj_factor(self, **kwargs):
            self.calls += 1
            return self.result

    endpoint = "daily" if request_type is StockDailyRequest else "adj_factor"
    request = request_type(empty_policy=EmptyPolicy.ALLOW, ts_code="A")
    for missing in ("ts_code", "trade_date"):
        fields = tuple(field for field in endpoint_fields if field != missing)
        fake = NoCall(pd.DataFrame(columns=fields))
        with pytest.raises(SchemaMismatchError):
            getattr(client(fake), method)(
                request_type(empty_policy=EmptyPolicy.ALLOW, ts_code="A", fields=fields)
            )
        assert fake.calls == 0

    empty = pd.DataFrame(columns=endpoint_fields)
    assert getattr(client(NoCall(empty)), method)(request).frame.empty
    with pytest.raises(EmptyResponseError):
        getattr(client(NoCall(empty)), method)(
            request_type(empty_policy=EmptyPolicy.ERROR, ts_code="A")
        )

    valid = (
        daily_frame()
        if endpoint == "daily"
        else pd.DataFrame({"ts_code": ["A"], "trade_date": ["20240101"], "adj_factor": [1.0]})
    )
    missing_column = valid.drop(columns=[endpoint_fields[-1]])
    with pytest.raises(SchemaMismatchError):
        getattr(client(NoCall(missing_column)), method)(request)
    for key in ("ts_code", "trade_date"):
        null_key = valid.copy()
        null_key.loc[0, key] = None
        with pytest.raises(SchemaMismatchError):
            getattr(client(NoCall(null_key)), method)(request)
    duplicate = pd.concat([valid, valid], ignore_index=True)
    with pytest.raises(SchemaMismatchError):
        getattr(client(NoCall(duplicate)), method)(request)
    with pytest.raises(SchemaMismatchError):
        getattr(client(NoCall("not a dataframe")), method)(request)


def test_stock_daily_defensive_copies():
    source = daily_frame()
    fake = Fake(source)
    result = client(fake).fetch_stock_daily(
        StockDailyRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="FAKE.ETF")
    )
    source.loc[0, "close"] = 99
    returned = result.frame
    returned.loc[0, "close"] = 88
    assert result.frame.loc[0, "close"] != 88
    assert result.frame.loc[0, "close"] != 99


def test_stock_dividend_preserves_revisions_duplicates_nulls_and_native_values():
    fields = StockDividendRequest(empty_policy=EmptyPolicy.ALLOW, ann_date="20240101").fields
    source = pd.DataFrame(
        {
            "ts_code": ["A", "A", "A"],
            "end_date": ["2023", "2023", "2023"],
            "ann_date": ["20240101"] * 3,
            "div_proc": ["实施", "预案", "预案"],
            "stk_div": [None, 0.1, 0.1],
            "stk_bo_rate": [None, 1.0, 1.0],
            "stk_co_rate": [None, 0.0, 0.0],
            "cash_div": [1.2, 0.0, 0.0],
            "cash_div_tax": [1.0, None, None],
            "record_date": [None] * 3,
            "ex_date": ["20240110", "20240111", "20240111"],
            "pay_date": [None] * 3,
            "div_listdate": [None] * 3,
            "imp_ann_date": ["20240109", "20240108", "20240108"],
            "base_date": [None] * 3,
            "base_share": [123.4, 10.0, 10.0],
        }
    )
    source = source.iloc[[1, 0, 2]].reset_index(drop=True)
    fake = Fake(source)
    result = client(fake).fetch_stock_dividend(
        StockDividendRequest(empty_policy=EmptyPolicy.ALLOW, ann_date="20240101")
    )
    assert fake.calls == [{"ann_date": "20240101", "fields": ",".join(fields)}]
    assert result.frame[["ts_code", "div_proc", "imp_ann_date", "cash_div"]].to_records(
        index=False
    ).tolist() == [
        ("A", "预案", "20240108", 0.0),
        ("A", "预案", "20240108", 0.0),
        ("A", "实施", "20240109", 1.2),
    ]
    assert len(result.frame) == 3
    assert pd.isna(result.frame.loc[0, "record_date"])
    assert result.frame.loc[2, "base_share"] == 123.4


def test_stock_dividend_no_duplicate_or_cap_claim_and_failures():
    request = StockDividendRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A")
    fields = list(request.fields)
    row: dict[str, object] = {field: None for field in fields}
    row["ts_code"] = "A"
    duplicate = pd.DataFrame([row, row])
    assert len(client(Fake(duplicate)).fetch_stock_dividend(request).frame) == 2
    many = pd.DataFrame({field: ["A"] * 6000 for field in fields})
    assert len(client(Fake(many)).fetch_stock_dividend(request).frame) == 6000
    with pytest.raises(SchemaMismatchError):
        client(Fake(pd.DataFrame({field: [None] for field in fields}))).fetch_stock_dividend(
            request
        )
    with pytest.raises(SchemaMismatchError):
        client(Fake("not a dataframe")).fetch_stock_dividend(request)
    with pytest.raises(SchemaMismatchError):
        client(Fake(many.drop(columns=[fields[-1]]))).fetch_stock_dividend(request)
    with pytest.raises(SchemaMismatchError):
        client(object()).fetch_stock_dividend(request)
    empty = pd.DataFrame(columns=fields)
    assert client(Fake(empty)).fetch_stock_dividend(request).frame.empty
    with pytest.raises(EmptyResponseError):
        client(Fake(empty)).fetch_stock_dividend(
            StockDividendRequest(empty_policy=EmptyPolicy.ERROR, ts_code="A")
        )


def test_stock_dividend_defensive_copy():
    request = StockDividendRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A")
    source = pd.DataFrame({field: [None] for field in request.fields})
    source["ts_code"] = ["A"]
    result = client(Fake(source)).fetch_stock_dividend(request)
    source.loc[0, "ts_code"] = "B"
    returned = result.frame
    returned.loc[0, "ts_code"] = "C"
    assert result.frame.loc[0, "ts_code"] == "A"


def test_empty_policy_and_validation_errors():
    empty = Fake(pd.DataFrame(columns=DAILY_FIELDS))
    allowed = client(empty).fetch_fund_daily(
        FundDailyRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="FAKE")
    )
    assert allowed.frame.empty
    with pytest.raises(EmptyResponseError):
        client(Fake(pd.DataFrame(columns=DAILY_FIELDS))).fetch_fund_daily(
            FundDailyRequest(empty_policy=EmptyPolicy.ERROR, ts_code="FAKE")
        )
    with pytest.raises(SchemaMismatchError):
        client(
            Fake(pd.DataFrame({"ts_code": ["FAKE"], "trade_date": ["20240101"]}))
        ).fetch_fund_daily(FundDailyRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="FAKE"))
    with pytest.raises(SchemaMismatchError):
        client(Fake(result="not a dataframe")).fetch_fund_daily(
            FundDailyRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="FAKE")
        )


def test_etf_basic_empty_policy_and_null_key_validation():
    fields = ("ts_code", "cname")
    empty = Fake(pd.DataFrame(columns=fields))
    allowed = client(empty).fetch_etf_basic(
        EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, fields=fields)
    )
    assert allowed.frame.empty
    with pytest.raises(EmptyResponseError):
        client(Fake(pd.DataFrame(columns=fields))).fetch_etf_basic(
            EtfBasicRequest(empty_policy=EmptyPolicy.ERROR, fields=fields)
        )
    null_key = pd.DataFrame({"ts_code": [None], "cname": ["missing"]})
    with pytest.raises(SchemaMismatchError):
        client(Fake(null_key)).fetch_etf_basic(
            EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, fields=fields)
        )


def test_transient_retry_exhaustion_and_permanent_no_retry():
    fake = Fake(daily_frame(), errors=[ConnectionError("network")])
    result = client(
        fake, retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0)
    ).fetch_fund_daily(FundDailyRequest(empty_policy=EmptyPolicy.ERROR, ts_code="FAKE"))
    assert len(fake.calls) == 2
    assert result.provenance.attempt_count == 2
    denied = Fake(errors=[RuntimeError("permission denied for token")])
    with pytest.raises(PermissionDeniedError):
        client(denied).fetch_fund_daily(
            FundDailyRequest(empty_policy=EmptyPolicy.ERROR, ts_code="FAKE")
        )
    assert len(denied.calls) == 1


def test_limiter_acquired_per_attempt():
    fake = Fake(daily_frame(), errors=[TimeoutError("slow")])
    ticks = iter([0.0, 0.0, 1.0])
    waits = []
    limiter = RateLimiter(RateLimitPolicy(1), clock=lambda: next(ticks), sleep=waits.append)
    client(
        fake, limiter=limiter, retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0)
    ).fetch_fund_daily(FundDailyRequest(empty_policy=EmptyPolicy.ERROR, ts_code="FAKE"))
    assert len(fake.calls) == 2
    assert waits == [1.0]


def test_non_dataframe_and_duplicate_and_cap_failures():
    with pytest.raises(SchemaMismatchError):
        client(Fake(result=[])).fetch_fund_daily(
            FundDailyRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="FAKE")
        )
    dup = daily_frame((("FAKE", "20240101"), ("FAKE", "20240101")))
    with pytest.raises(SchemaMismatchError):
        client(Fake(dup)).fetch_fund_daily(
            FundDailyRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="FAKE")
        )
    basic = pd.DataFrame({"ts_code": [f"F{i}" for i in range(15000)]})
    with pytest.raises(PaginationError):
        client(Fake(basic)).fetch_fund_basic(
            FundBasicRequest(empty_policy=EmptyPolicy.ALLOW, fields=("ts_code",))
        )


def test_etf_basic_sorts_validates_keys_cap_and_defensive_copy():
    fields = ["ts_code", "cname"]
    source = pd.DataFrame({"ts_code": ["B", "A"], "cname": ["b", "a"]})
    fake = Fake(source)
    result = client(fake).fetch_etf_basic(
        EtfBasicRequest(empty_policy=EmptyPolicy.ERROR, market="E", fields=tuple(fields))
    )
    assert fake.calls == [{"market": "E", "fields": "ts_code,cname"}]
    assert result.frame.ts_code.tolist() == ["A", "B"]
    assert result.provenance.endpoint == "etf_basic"
    returned = result.frame
    returned.loc[0, "cname"] = "changed"
    assert result.frame.loc[0, "cname"] == "a"
    with pytest.raises(SchemaMismatchError):
        client(Fake(pd.DataFrame({"cname": ["a"]}))).fetch_etf_basic(
            EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, fields=("cname",))
        )
    with pytest.raises(SchemaMismatchError):
        client(Fake(pd.DataFrame({"ts_code": ["A", "A"], "cname": ["a", "a"]}))).fetch_etf_basic(
            EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, fields=tuple(fields))
        )
    cap = pd.DataFrame({"ts_code": [f"E{i}" for i in range(5000)], "cname": ["x"] * 5000})
    with pytest.raises(PaginationError):
        client(Fake(cap)).fetch_etf_basic(
            EtfBasicRequest(empty_policy=EmptyPolicy.ALLOW, fields=tuple(fields))
        )


def test_basic_required_coverage_and_nav_duplicates_preserved():
    basic = pd.DataFrame({"ts_code": ["A"]})
    with pytest.raises(CoverageError):
        client(Fake(basic)).fetch_fund_basic(
            FundBasicRequest(
                empty_policy=EmptyPolicy.ALLOW, fields=("ts_code",), required_ts_codes=("A", "B")
            )
        )
    nav = pd.DataFrame(
        {
            "ts_code": ["A", "A"],
            "nav_date": ["20240101", "20240101"],
            "ann_date": ["20240102", "20240103"],
        }
    )
    result = client(Fake(nav)).fetch_fund_nav(
        FundNavRequest(
            empty_policy=EmptyPolicy.ALLOW, ts_code="A", fields=("ts_code", "nav_date", "ann_date")
        )
    )
    assert len(result.frame) == 2


def test_adjustment_pagination_offsets_duplicates_and_max_pages():
    pages = [
        pd.DataFrame({"ts_code": ["A"], "trade_date": ["20240102"], "adj_factor": [1.0]}),
        pd.DataFrame({"ts_code": ["A"], "trade_date": ["20240101"], "adj_factor": [0.9]}),
        pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"]),
    ]

    class Adj:
        def __init__(self):
            self.calls = []

        def fund_adj(self, **kwargs):
            self.calls.append(kwargs)
            return pages[len(self.calls) - 1]

    fake = Adj()
    result = client(fake).fetch_fund_adjustment(
        FundAdjustmentRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", page_size=1)
    )
    assert [c["offset"] for c in fake.calls] == [0, 1, 2]
    assert result.page_count == 3
    assert result.frame.trade_date.tolist() == ["20240101", "20240102"]

    class Full:
        def fund_adj(self, **kwargs):
            return pages[0]

    with pytest.raises(PaginationError):
        client(Full()).fetch_fund_adjustment(
            FundAdjustmentRequest(
                empty_policy=EmptyPolicy.ALLOW, ts_code="A", page_size=1, max_pages=1
            )
        )


def test_all_endpoint_methods_send_exact_kwargs_and_preserve_units_and_order():
    class All:
        def __init__(self):
            self.calls = []

        def trade_cal(self, **kw):
            self.calls.append(("trade_cal", kw))
            return pd.DataFrame(
                {
                    "exchange": ["SSE"],
                    "cal_date": ["20240101"],
                    "is_open": [1],
                    "pretrade_date": ["20231229"],
                }
            )

        def fund_basic(self, **kw):
            self.calls.append(("fund_basic", kw))
            return pd.DataFrame({"ts_code": ["B"]})

        def fund_daily(self, **kw):
            self.calls.append(("fund_daily", kw))
            return daily_frame()

        def fund_nav(self, **kw):
            self.calls.append(("fund_nav", kw))
            return pd.DataFrame(
                {"ts_code": ["A"], "nav_date": ["20240101"], "ann_date": ["20240102"]}
            )

        def fund_share(self, **kw):
            self.calls.append(("fund_share", kw))
            return pd.DataFrame(
                {"ts_code": ["A"], "trade_date": ["20240101"], "fd_share": [float("nan")]}
            )

    fake = All()
    c = client(fake)
    c.fetch_trade_calendar(
        TradeCalendarRequest(
            empty_policy=EmptyPolicy.ALLOW,
            exchange="SSE",
            start_date="20240101",
            end_date="20240102",
            is_open="1",
        )
    )
    c.fetch_fund_basic(
        FundBasicRequest(empty_policy=EmptyPolicy.ALLOW, market="E", fields=("ts_code",))
    )
    daily = c.fetch_fund_daily(FundDailyRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="FAKE"))
    c.fetch_fund_nav(
        FundNavRequest(
            empty_policy=EmptyPolicy.ALLOW, ts_code="A", fields=("ts_code", "nav_date", "ann_date")
        )
    )
    share = c.fetch_fund_share(FundShareRequest(empty_policy=EmptyPolicy.ALLOW, market="E"))
    assert fake.calls[0][1]["is_open"] == "1" and fake.calls[0][1]["start_date"] == "20240101"
    assert fake.calls[1][1] == {"market": "E", "fields": "ts_code"}
    assert daily.frame.amount.iloc[0] == 250.0 and daily.frame.vol.iloc[0] == 100
    assert pd.isna(share.frame.fd_share.iloc[0])
    assert fake.calls == [
        (
            "trade_cal",
            {
                "exchange": "SSE",
                "start_date": "20240101",
                "end_date": "20240102",
                "is_open": "1",
                "fields": "exchange,cal_date,is_open,pretrade_date",
            },
        ),
        ("fund_basic", {"market": "E", "fields": "ts_code"}),
        ("fund_daily", {"ts_code": "FAKE", "fields": ",".join(DAILY_FIELDS)}),
        ("fund_nav", {"ts_code": "A", "fields": "ts_code,nav_date,ann_date"}),
        ("fund_share", {"market": "E", "fields": "ts_code,trade_date,fd_share"}),
    ]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: FundDailyRequest(
            empty_policy=EmptyPolicy.ALLOW, ts_code="A", start_date="20240101", end_date="20240102"
        ),
        lambda: FundAdjustmentRequest(
            empty_policy=EmptyPolicy.ALLOW, ts_code="A", start_date="20240101", end_date="20240102"
        ),
        lambda: FundNavRequest(
            empty_policy=EmptyPolicy.ALLOW, ts_code="A", start_date="20240101", end_date="20240102"
        ),
    ],
)
def test_date_selector_validation(factory):
    assert factory().spec
    with pytest.raises(ValueError):
        FundDailyRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", trade_date="20240101")
    with pytest.raises(ValueError):
        FundDailyRequest(empty_policy=EmptyPolicy.ALLOW, start_date="20240101")
    with pytest.raises(ValueError):
        TradeCalendarRequest(empty_policy=EmptyPolicy.ALLOW, start_date="2024-01-01")
    with pytest.raises(ValueError):
        TradeCalendarRequest(
            empty_policy=EmptyPolicy.ALLOW, start_date="20240102", end_date="20240101"
        )


def test_provenance_identity_and_naive_clock_rejection():
    req = FundDailyRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A")
    result = client(
        Fake(),
        clock=lambda: datetime(
            2024,
            1,
            2,
            8,
            tzinfo=__import__("datetime").timezone(__import__("datetime").timedelta(hours=8)),
        ),
    ).fetch_fund_daily(req)
    assert result.provenance.request_identity == req.spec.request_identity
    assert result.provenance.retrieved_at == datetime(2024, 1, 2, tzinfo=UTC)
    assert dict(result.provenance.effective_parameters) == {"ts_code": "A"}
    assert result.provenance.attempt_count == 1
    assert result.provenance.attempts[0].attempt == 1
    assert result.provenance.row_count == 1
    assert result.provenance.columns == tuple(DAILY_FIELDS)
    assert result.provenance.snapshot_identities == ()
    with pytest.raises(ValueError):
        TushareClient(Fake(), clock=lambda: datetime(2024, 1, 2)).fetch_fund_daily(req)  # noqa: DTZ001


def test_retry_exhaustion_cause_redaction_and_passthrough():
    with pytest.raises(RetryExhaustedError) as info:
        client(
            Fake(errors=[TimeoutError("token=FAKE")] * 2),
            retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
        ).fetch_fund_daily(FundDailyRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A"))
    assert isinstance(info.value.__cause__, TransientProviderError)
    existing = SchemaMismatchError("safe")
    from ohmydata.providers.tushare import classify_tushare_exception

    assert classify_tushare_exception(existing) is existing


def test_adjustment_exact_pagination_and_duplicate_failures_and_empty_policy():
    row = lambda d: pd.DataFrame({"ts_code": ["A"], "trade_date": [d], "adj_factor": [1.0]})

    class Adj:
        def __init__(self, pages):
            self.pages, self.calls = pages, []

        def fund_adj(self, **kwargs):
            self.calls.append(kwargs)
            return self.pages[len(self.calls) - 1]

    fake = Adj(
        [
            row("20240102"),
            row("20240101"),
            pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"]),
        ]
    )
    req = FundAdjustmentRequest(
        empty_policy=EmptyPolicy.ALLOW,
        ts_code="A",
        start_date="20240101",
        end_date="20240102",
        page_size=1,
    )
    assert client(fake).fetch_fund_adjustment(req).page_count == 3
    assert fake.calls[0] == {
        "ts_code": "A",
        "start_date": "20240101",
        "end_date": "20240102",
        "limit": 1,
        "offset": 0,
        "fields": "ts_code,trade_date,adj_factor",
    }
    assert fake.calls[1]["offset"] == 1 and fake.calls[2]["offset"] == 2
    with pytest.raises(PaginationError):
        client(
            Adj([pd.concat([row("20240101"), row("20240101")], ignore_index=True)])
        ).fetch_fund_adjustment(
            FundAdjustmentRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", page_size=2)
        )
    with pytest.raises(PaginationError):
        client(
            Adj(
                [
                    row("20240101"),
                    row("20240101"),
                    pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"]),
                ]
            )
        ).fetch_fund_adjustment(
            FundAdjustmentRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", page_size=1)
        )
    empty = Adj([pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])])
    assert (
        client(empty)
        .fetch_fund_adjustment(FundAdjustmentRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A"))
        .page_count
        == 1
    )
    with pytest.raises(EmptyResponseError):
        client(
            Adj([pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])])
        ).fetch_fund_adjustment(FundAdjustmentRequest(empty_policy=EmptyPolicy.ERROR, ts_code="A"))
    with pytest.raises(SchemaMismatchError):
        client(object()).fetch_fund_adjustment(
            FundAdjustmentRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A")
        )


@pytest.mark.parametrize(
    "endpoint, req, frame, cap",
    [
        (
            "fetch_fund_daily",
            FundDailyRequest(
                empty_policy=EmptyPolicy.ALLOW, ts_code="A", fields=("ts_code", "trade_date")
            ),
            pd.DataFrame(
                {"ts_code": ["A"] * 5000, "trade_date": [str(i).zfill(8) for i in range(5000)]}
            ),
            5000,
        ),
        (
            "fetch_fund_share",
            FundShareRequest(
                empty_policy=EmptyPolicy.ALLOW, market="E", fields=("ts_code", "trade_date")
            ),
            pd.DataFrame(
                {
                    "ts_code": [str(i) for i in range(2000)],
                    "trade_date": [str(i).zfill(8) for i in range(2000)],
                }
            ),
            2000,
        ),
    ],
)
def test_nonpageable_caps(endpoint, req, frame, cap):
    class C:
        def __getattr__(self, name):
            return lambda **kwargs: frame

    with pytest.raises(PaginationError):
        getattr(client(C()), endpoint)(req)


def test_null_keys_custom_missing_keys_duplicates_and_no_retry_errors():
    class C:
        def __init__(self, frame, error=None):
            self.frame, self.error, self.calls = frame, error, 0

        def trade_cal(self, **kwargs):
            self.calls += 1
            if self.error:
                raise self.error
            return self.frame

        def fund_share(self, **kwargs):
            self.calls += 1
            return self.frame

    null = pd.DataFrame(
        {"exchange": [None], "cal_date": ["20240101"], "is_open": [1], "pretrade_date": ["x"]}
    )
    with pytest.raises(SchemaMismatchError):
        client(C(null)).fetch_trade_calendar(TradeCalendarRequest(empty_policy=EmptyPolicy.ALLOW))
    dup = pd.DataFrame(
        {
            "exchange": ["SSE", "SSE"],
            "cal_date": ["20240101", "20240101"],
            "is_open": [1, 1],
            "pretrade_date": ["x", "x"],
        }
    )
    with pytest.raises(SchemaMismatchError):
        client(C(dup)).fetch_trade_calendar(TradeCalendarRequest(empty_policy=EmptyPolicy.ALLOW))
    with pytest.raises(SchemaMismatchError):
        client(C(pd.DataFrame({"ts_code": ["A"], "trade_date": ["1"]}))).fetch_fund_share(
            FundShareRequest(empty_policy=EmptyPolicy.ALLOW, market="E")
        )
    for err, typ in [
        (RuntimeError("unknown"), PermanentProviderError),
        (RuntimeError("invalid api token"), AuthenticationError),
    ]:
        c = C(None, err)
        with pytest.raises(typ):
            client(c).fetch_trade_calendar(TradeCalendarRequest(empty_policy=EmptyPolicy.ALLOW))
        assert c.calls == 1


PHASE2B_FIELDS = {
    "fund_div": (
        "ts_code",
        "ann_date",
        "imp_anndate",
        "base_date",
        "div_proc",
        "record_date",
        "ex_date",
        "pay_date",
        "earpay_date",
        "net_ex_date",
        "div_cash",
        "base_unit",
        "ear_distr",
        "ear_amount",
        "account_date",
        "base_year",
    ),
    "fund_portfolio": (
        "ts_code",
        "ann_date",
        "end_date",
        "symbol",
        "mkv",
        "amount",
        "stk_mkv_ratio",
        "stk_float_ratio",
    ),
    "daily_basic": (
        "ts_code",
        "trade_date",
        "close",
        "turnover_rate",
        "turnover_rate_f",
        "volume_ratio",
        "pe",
        "pe_ttm",
        "pb",
        "ps",
        "ps_ttm",
        "dv_ratio",
        "dv_ttm",
        "total_share",
        "float_share",
        "free_share",
        "total_mv",
        "circ_mv",
    ),
    "index_weight": ("index_code", "con_code", "trade_date", "weight"),
}


def test_phase2b_exact_method_kwargs_and_default_fields():
    class Provider:
        def __init__(self):
            self.calls = []

        def fund_div(self, **kwargs):
            self.calls.append(("fund_div", kwargs))
            return pd.DataFrame(columns=PHASE2B_FIELDS["fund_div"])

        def fund_portfolio(self, **kwargs):
            self.calls.append(("fund_portfolio", kwargs))
            return pd.DataFrame(columns=PHASE2B_FIELDS["fund_portfolio"])

        def daily_basic(self, **kwargs):
            self.calls.append(("daily_basic", kwargs))
            return pd.DataFrame(columns=PHASE2B_FIELDS["daily_basic"])

        def index_weight(self, **kwargs):
            self.calls.append(("index_weight", kwargs))
            return pd.DataFrame(columns=PHASE2B_FIELDS["index_weight"])

    provider = Provider()
    api = client(provider)
    api.fetch_fund_dividend(FundDividendRequest(empty_policy=EmptyPolicy.ALLOW, ex_date="20240102"))
    api.fetch_fund_portfolio(
        FundPortfolioRequest(
            empty_policy=EmptyPolicy.ALLOW, ts_code="F", period="20231231", symbol="X"
        )
    )
    api.fetch_daily_basic(
        DailyBasicRequest(
            empty_policy=EmptyPolicy.ALLOW, ts_code="A", start_date="20240101", end_date="20240102"
        )
    )
    api.fetch_index_weight(
        IndexWeightRequest(
            empty_policy=EmptyPolicy.ALLOW,
            index_code="I",
            start_date="20240101",
            end_date="20240131",
        )
    )
    assert provider.calls == [
        ("fund_div", {"ex_date": "20240102", "fields": ",".join(PHASE2B_FIELDS["fund_div"])}),
        (
            "fund_portfolio",
            {
                "ts_code": "F",
                "period": "20231231",
                "symbol": "X",
                "fields": ",".join(PHASE2B_FIELDS["fund_portfolio"]),
            },
        ),
        (
            "daily_basic",
            {
                "ts_code": "A",
                "start_date": "20240101",
                "end_date": "20240102",
                "fields": ",".join(PHASE2B_FIELDS["daily_basic"]),
            },
        ),
        (
            "index_weight",
            {
                "index_code": "I",
                "start_date": "20240101",
                "end_date": "20240131",
                "fields": ",".join(PHASE2B_FIELDS["index_weight"]),
            },
        ),
    ]


@pytest.mark.parametrize(
    "endpoint, req, method",
    [
        (
            "fund_div",
            FundDividendRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A"),
            "fetch_fund_dividend",
        ),
        (
            "fund_portfolio",
            FundPortfolioRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", period="20231231"),
            "fetch_fund_portfolio",
        ),
        (
            "daily_basic",
            DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A"),
            "fetch_daily_basic",
        ),
        (
            "index_weight",
            IndexWeightRequest(
                empty_policy=EmptyPolicy.ALLOW, index_code="I", trade_date="20240101"
            ),
            "fetch_index_weight",
        ),
    ],
)
def test_phase2b_empty_allow_and_error(endpoint, req, method):
    class Provider:
        def __getattr__(self, name):
            return lambda **kwargs: pd.DataFrame(columns=PHASE2B_FIELDS[endpoint])

    assert (
        getattr(client(Provider()), method)(req).provenance.empty_disposition
        is EmptyDisposition.ALLOWED_EMPTY
    )
    if endpoint == "fund_div":
        error_request = FundDividendRequest(empty_policy=EmptyPolicy.ERROR, ts_code="A")
    elif endpoint == "fund_portfolio":
        error_request = FundPortfolioRequest(
            empty_policy=EmptyPolicy.ERROR, ts_code="A", period="20231231"
        )
    elif endpoint == "daily_basic":
        error_request = DailyBasicRequest(empty_policy=EmptyPolicy.ERROR, ts_code="A")
    else:
        error_request = IndexWeightRequest(
            empty_policy=EmptyPolicy.ERROR, index_code="I", trade_date="20240101"
        )
    with pytest.raises(EmptyResponseError):
        getattr(client(Provider()), method)(error_request)


@pytest.mark.parametrize(
    "endpoint, req, method, key",
    [
        (
            "fund_div",
            FundDividendRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A"),
            "fetch_fund_dividend",
            "ts_code",
        ),
        (
            "fund_portfolio",
            FundPortfolioRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", period="20231231"),
            "fetch_fund_portfolio",
            "symbol",
        ),
        (
            "daily_basic",
            DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A"),
            "fetch_daily_basic",
            "trade_date",
        ),
        (
            "index_weight",
            IndexWeightRequest(
                empty_policy=EmptyPolicy.ALLOW, index_code="I", trade_date="20240101"
            ),
            "fetch_index_weight",
            "con_code",
        ),
    ],
)
def test_phase2b_missing_fields_null_keys_and_malformed(endpoint, req, method, key):
    class Provider:
        def __init__(self, result):
            self.result, self.calls = result, 0

        def __getattr__(self, name):
            def call(**kwargs):
                self.calls += 1
                return self.result

            return call

    missing = pd.DataFrame(columns=[f for f in PHASE2B_FIELDS[endpoint] if f != key])
    with pytest.raises(SchemaMismatchError):
        getattr(client(Provider(missing)), method)(req)
    null = pd.DataFrame({f: [None] for f in PHASE2B_FIELDS[endpoint]})
    with pytest.raises(SchemaMismatchError):
        getattr(client(Provider(null)), method)(req)
    with pytest.raises(SchemaMismatchError):
        getattr(client(Provider("not a dataframe")), method)(req)


def test_phase2b_sorting_duplicates_and_native_values():
    div = pd.DataFrame(
        {
            "ts_code": ["A", "A", "A"],
            "ann_date": ["20240103", None, "20240101"],
            "ex_date": ["20240105", None, "20240102"],
            "pay_date": [None] * 3,
            "imp_anndate": [None] * 3,
            **{
                f: [1, 2, 3]
                for f in PHASE2B_FIELDS["fund_div"]
                if f not in {"ts_code", "ann_date", "ex_date", "pay_date", "imp_anndate"}
            },
        }
    )

    class P:
        def fund_div(self, **kwargs):
            return div

    result = client(P()).fetch_fund_dividend(
        FundDividendRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A")
    )
    assert result.frame.ann_date.tolist() == ["20240101", "20240103", None]
    assert len(result.frame) == 3

    portfolio = pd.DataFrame(
        {
            "ts_code": ["A", "A"],
            "ann_date": [None, "20240101"],
            "end_date": ["20231231", "20231231"],
            "symbol": ["X", "X"],
            "mkv": [123.4, 123.4],
            "amount": [10, 11],
            "stk_mkv_ratio": [1.5, 1.5],
            "stk_float_ratio": [2.5, 2.5],
        }
    )

    class PP:
        def fund_portfolio(self, **kwargs):
            return portfolio

    p = client(PP()).fetch_fund_portfolio(
        FundPortfolioRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", period="20231231")
    )
    assert p.frame.ann_date.tolist() == ["20240101", None] and p.frame.mkv.tolist() == [
        123.4,
        123.4,
    ]

    class D:
        def daily_basic(self, **kwargs):
            return pd.DataFrame(
                {f: [1.0, 2.0] for f in PHASE2B_FIELDS["daily_basic"]}
                | {
                    "ts_code": ["A", "A"],
                    "trade_date": ["20240102", "20240101"],
                    "dv_ttm": [float("nan"), 3.0],
                    "total_share": pd.Series([10, 11], dtype="int64"),
                }
            )

    d = client(D()).fetch_daily_basic(
        DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A")
    )
    assert d.frame.trade_date.tolist() == ["20240101", "20240102"] and pd.isna(
        d.frame.dv_ttm.iloc[1]
    )
    assert d.frame.total_share.dtype == "int64"

    class W:
        def index_weight(self, **kwargs):
            return pd.DataFrame(
                {
                    "index_code": ["I", "I"],
                    "con_code": ["B", "A"],
                    "trade_date": ["20240101"] * 2,
                    "weight": [2.5, 1.25],
                }
            )

    w = client(W()).fetch_index_weight(
        IndexWeightRequest(empty_policy=EmptyPolicy.ALLOW, index_code="I", trade_date="20240101")
    )
    assert w.frame.con_code.tolist() == ["A", "B"] and w.frame.weight.tolist() == [1.25, 2.5]


@pytest.mark.parametrize(
    "req, rows",
    [
        (
            IndexWeightRequest(
                empty_policy=EmptyPolicy.ALLOW, index_code="I", trade_date="20240101"
            ),
            [("OTHER", "A", "20240101", 1.0)],
        ),
        (
            IndexWeightRequest(
                empty_policy=EmptyPolicy.ALLOW, index_code="I", trade_date="20240101"
            ),
            [("I", "A", "20240102", 1.0)],
        ),
        (
            IndexWeightRequest(
                empty_policy=EmptyPolicy.ALLOW,
                index_code="I",
                start_date="20240101",
                end_date="20240131",
            ),
            [("I", "A", "20231231", 1.0)],
        ),
        (
            IndexWeightRequest(
                empty_policy=EmptyPolicy.ALLOW,
                index_code="I",
                start_date="20240101",
                end_date="20240131",
            ),
            [("I", "A", "20240201", 1.0)],
        ),
        (
            IndexWeightRequest(
                empty_policy=EmptyPolicy.ALLOW,
                index_code="I",
                start_date="20240101",
                end_date="20240131",
            ),
            [("I", "A", "2024011X", 1.0)],
        ),
        (
            IndexWeightRequest(
                empty_policy=EmptyPolicy.ALLOW,
                index_code="I",
                start_date="20240101",
                end_date="20240131",
            ),
            [("I", "A", 20240115, 1.0)],
        ),
    ],
)
def test_phase2b_index_weight_response_scope_is_fail_closed(req, rows):
    class Provider:
        def index_weight(self, **kwargs):
            return pd.DataFrame(rows, columns=PHASE2B_FIELDS["index_weight"])

    with pytest.raises(SchemaMismatchError):
        client(Provider()).fetch_index_weight(req)


def test_phase2b_index_weight_scope_identity_order_and_native_nonfinite_values():
    class Provider:
        def index_weight(self, **kwargs):
            return pd.DataFrame(
                {
                    "index_code": ["I", "I", "I"],
                    "con_code": ["B", "A", "C"],
                    "trade_date": ["20240102", "20240101", "20240101"],
                    "weight": [float("nan"), None, float("inf")],
                }
            )

    result = client(Provider()).fetch_index_weight(
        IndexWeightRequest(
            empty_policy=EmptyPolicy.ALLOW,
            index_code="I",
            start_date="20240101",
            end_date="20240131",
        )
    )
    assert list(zip(result.frame.trade_date, result.frame.con_code)) == [
        ("20240101", "A"),
        ("20240101", "C"),
        ("20240102", "B"),
    ]
    assert pd.isna(result.frame.weight.iloc[0])
    assert result.frame.weight.iloc[1] == float("inf")
    assert pd.isna(result.frame.weight.iloc[2])


@pytest.mark.parametrize("missing", ["index_code", "con_code", "trade_date"])
def test_phase2b_index_weight_custom_fields_require_all_identity_columns(missing):
    fields = [field for field in PHASE2B_FIELDS["index_weight"] if field != missing]
    request = IndexWeightRequest(
        empty_policy=EmptyPolicy.ALLOW,
        index_code="I",
        trade_date="20240101",
        fields=tuple(fields),
    )
    with pytest.raises(SchemaMismatchError):
        client(object()).fetch_index_weight(request)


def test_phase2b_index_weight_snapshot_revisions_remain_distinct(tmp_path):
    frames = [
        pd.DataFrame(
            {"index_code": ["I"], "con_code": ["A"], "trade_date": ["20240101"], "weight": [1.0]}
        ),
        pd.DataFrame(
            {"index_code": ["I"], "con_code": ["A"], "trade_date": ["20240101"], "weight": [2.0]}
        ),
    ]

    class Provider:
        def __init__(self):
            self.i = 0

        def index_weight(self, **kwargs):
            frame = frames[self.i]
            self.i += 1
            return frame

    request = IndexWeightRequest(
        empty_policy=EmptyPolicy.ALLOW, index_code="I", trade_date="20240101"
    )
    api = client(Provider())
    first, second = api.fetch_index_weight(request), api.fetch_index_weight(request)
    store = SnapshotStore(tmp_path)
    a = store.write(
        request.spec,
        first.frame.to_json(orient="records").encode(),
        datetime(2024, 1, 1, tzinfo=UTC),
        "json-rows-v1",
    )
    b = store.write(
        request.spec,
        second.frame.to_json(orient="records").encode(),
        datetime(2024, 1, 2, tzinfo=UTC),
        "json-rows-v1",
    )
    assert a.path != b.path
    assert store.replay(a).payload != store.replay(b).payload


@pytest.mark.parametrize("weights", ([1.0, 1.0], [1.0, 2.0]))
def test_phase2b_duplicate_rejection_and_daily_cap(weights):
    class D:
        def daily_basic(self, **kwargs):
            fields = PHASE2B_FIELDS["daily_basic"]
            return pd.DataFrame(
                {f: [1.0] * 6000 for f in fields}
                | {"ts_code": [f"A{i}" for i in range(6000)], "trade_date": ["20240101"] * 6000}
            )

    with pytest.raises(PaginationError):
        client(D()).fetch_daily_basic(
            DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, trade_date="20240101")
        )

    class W:
        def index_weight(self, **kwargs):
            return pd.DataFrame(
                {
                    "index_code": ["I", "I"],
                    "con_code": ["A", "A"],
                    "trade_date": ["20240101"] * 2,
                    "weight": list(weights),
                }
            )

    with pytest.raises(SchemaMismatchError):
        client(W()).fetch_index_weight(
            IndexWeightRequest(
                empty_policy=EmptyPolicy.ALLOW, index_code="I", trade_date="20240101"
            )
        )


def test_phase2b_exact_duplicate_rows_preserved_and_fund_nav_regression():
    nav = pd.DataFrame(
        {
            "ts_code": ["A", "A"],
            "nav_date": ["20240101"] * 2,
            "ann_date": ["20240102"] * 2,
            "unit_nav": [1.0, 1.0],
            "accum_nav": [1.0, 1.0],
            "accum_div": [0.0, 0.0],
            "net_asset": [1.0, 1.0],
            "total_netasset": [1.0, 1.0],
            "adj_nav": [1.0, 1.0],
        }
    )

    class N:
        def fund_nav(self, **kwargs):
            return nav

    assert (
        len(
            client(N())
            .fetch_fund_nav(FundNavRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A"))
            .frame
        )
        == 2
    )

    div = pd.DataFrame({field: [1, 1] for field in PHASE2B_FIELDS["fund_div"]})
    div["ts_code"] = "A"
    div["ann_date"] = "20240101"
    div["ex_date"] = "20240102"

    class V:
        def fund_div(self, **kwargs):
            return div

    assert (
        len(
            client(V())
            .fetch_fund_dividend(FundDividendRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A"))
            .frame
        )
        == 2
    )

    portfolio = pd.DataFrame(
        {
            "ts_code": ["A", "A"],
            "ann_date": ["20240101"] * 2,
            "end_date": ["20231231"] * 2,
            "symbol": ["X", "X"],
            "mkv": [123.4, 123.4],
            "amount": [10, 10],
            "stk_mkv_ratio": [1.0, 1.0],
            "stk_float_ratio": [2.0, 2.0],
        }
    )

    class P:
        def fund_portfolio(self, **kwargs):
            return portfolio

    assert (
        len(
            client(P())
            .fetch_fund_portfolio(
                FundPortfolioRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", period="20231231")
            )
            .frame
        )
        == 2
    )


@pytest.mark.parametrize(
    "endpoint, req, method, keys",
    [
        (
            "fund_portfolio",
            FundPortfolioRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", period="20231231"),
            "fetch_fund_portfolio",
            ("ts_code", "end_date", "symbol"),
        ),
        (
            "index_weight",
            IndexWeightRequest(
                empty_policy=EmptyPolicy.ALLOW, index_code="I", trade_date="20240101"
            ),
            "fetch_index_weight",
            ("index_code", "con_code", "trade_date"),
        ),
    ],
)
def test_phase2b_every_required_null_key_rejected(endpoint, req, method, keys):
    class Provider:
        def __init__(self, frame):
            self.frame = frame

        def __getattr__(self, name):
            return lambda **kwargs: self.frame

    for key in keys:
        frame = pd.DataFrame({field: [1] for field in PHASE2B_FIELDS[endpoint]})
        frame[key] = None
        with pytest.raises(SchemaMismatchError):
            getattr(client(Provider(frame)), method)(req)


def test_phase2b_provenance_is_exact_for_all_new_endpoints_and_result_isolated():
    div_row: dict[str, object] = {field: 1 for field in PHASE2B_FIELDS["fund_div"]}
    div_row.update(ts_code="A", ann_date="20240101", ex_date="20240102")
    daily_row: dict[str, object] = {field: 1.0 for field in PHASE2B_FIELDS["daily_basic"]}
    daily_row.update(ts_code="A", trade_date="20240101", dv_ttm=float("nan"))
    frames = {
        "fund_div": pd.DataFrame([div_row]),
        "fund_portfolio": pd.DataFrame(
            {
                "ts_code": ["A"],
                "ann_date": ["20240101"],
                "end_date": ["20231231"],
                "symbol": ["X"],
                "mkv": pd.Series([123.4], dtype="float64"),
                "amount": pd.Series([10], dtype="int64"),
                "stk_mkv_ratio": [1.0],
                "stk_float_ratio": [2.0],
            }
        ),
        "daily_basic": pd.DataFrame([daily_row]),
        "index_weight": pd.DataFrame(
            {
                "index_code": ["I"],
                "con_code": ["C"],
                "trade_date": ["20240101"],
                "weight": pd.Series([3.25], dtype="float64"),
            }
        ),
    }

    class Provider:
        def __getattr__(self, name):
            return lambda **kwargs: frames[name]

    cases = [
        (
            "fund_div",
            FundDividendRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A"),
            "fetch_fund_dividend",
        ),
        (
            "fund_portfolio",
            FundPortfolioRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", period="20231231"),
            "fetch_fund_portfolio",
        ),
        (
            "daily_basic",
            DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A"),
            "fetch_daily_basic",
        ),
        (
            "index_weight",
            IndexWeightRequest(
                empty_policy=EmptyPolicy.ALLOW, index_code="I", trade_date="20240101"
            ),
            "fetch_index_weight",
        ),
    ]
    results = {}
    for endpoint, request, method in cases:
        result = getattr(client(Provider()), method)(request)
        results[endpoint] = result
        assert result.provenance.endpoint == endpoint
        assert result.provenance.request_identity == request.spec.request_identity
        assert dict(result.provenance.effective_parameters) == dict(
            request.spec.effective_parameters
        )
        assert result.provenance.columns == request.fields
        assert result.provenance.attempt_count == 1 and result.page_count == 1
    assert results["fund_portfolio"].frame.mkv.iloc[0] == 123.4
    assert results["fund_portfolio"].frame.amount.iloc[0] == 10
    assert results["fund_portfolio"].frame.mkv.dtype == "float64"
    assert results["fund_portfolio"].frame.amount.dtype == "int64"
    assert results["index_weight"].frame.weight.iloc[0] == 3.25
    assert results["index_weight"].frame.weight.dtype == "float64"
    assert pd.isna(results["daily_basic"].frame.dv_ttm.iloc[0])
    for endpoint, result in results.items():
        frames[endpoint].iloc[0, 0] = "SOURCE_MUTATED"
        assert result.frame.iloc[0, 0] != "SOURCE_MUTATED"
        returned = result.frame
        returned.iloc[0, 0] = "MUTATED"
        assert result.frame.iloc[0, 0] != "MUTATED"


def test_phase2b_retry_recovery_and_no_retry_errors():
    class D:
        def __init__(self, errors):
            self.errors, self.calls = list(errors), 0

        def daily_basic(self, **kwargs):
            self.calls += 1
            if self.errors:
                raise self.errors.pop(0)
            return pd.DataFrame(
                {f: [1.0] for f in PHASE2B_FIELDS["daily_basic"]}
                | {"ts_code": ["A"], "trade_date": ["20240101"]}
            )

    recovering = D([TimeoutError("temporary")])
    result = client(
        recovering, retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0)
    ).fetch_daily_basic(DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A"))
    assert recovering.calls == 2 and result.provenance.attempt_count == 2
    for exc, expected in [
        (RuntimeError("permanent"), PermanentProviderError),
        (RuntimeError("authentication failed"), AuthenticationError),
        (RuntimeError("permission denied"), PermissionDeniedError),
    ]:
        failing = D([exc])
        with pytest.raises(expected):
            client(failing).fetch_daily_basic(
                DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A")
            )
        assert failing.calls == 1


def test_phase2b_validation_precedes_provider_calls():
    class Never:
        def __init__(self):
            self.calls = 0

        def daily_basic(self, **kwargs):
            self.calls += 1
            raise AssertionError("provider called")

    provider = Never()
    with pytest.raises(ValueError):
        client(provider).fetch_daily_basic(
            DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", trade_date="20240101")
        )
    assert provider.calls == 0


def test_daily_basic_response_scope_and_calendar_validation():
    base = {field: [1.0] for field in PHASE2B_FIELDS["daily_basic"]}

    class Provider:
        def __init__(self, ts_code: object = "A", trade_date: object = "20240101"):
            self.frame = pd.DataFrame(base | {"ts_code": [ts_code], "trade_date": [trade_date]})

        def daily_basic(self, **kwargs):
            return self.frame

    with pytest.raises(SchemaMismatchError):
        client(Provider(ts_code="B")).fetch_daily_basic(
            DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A")
        )
    with pytest.raises(SchemaMismatchError):
        client(Provider(trade_date="20240102")).fetch_daily_basic(
            DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, trade_date="20240101")
        )
    with pytest.raises(SchemaMismatchError):
        client(Provider(trade_date="20240102")).fetch_daily_basic(
            DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", end_date="20240101")
        )
    with pytest.raises(SchemaMismatchError):
        client(Provider(trade_date="20231231")).fetch_daily_basic(
            DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", start_date="20240101")
        )
    with pytest.raises(SchemaMismatchError):
        client(Provider(trade_date="20240230")).fetch_daily_basic(
            DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A")
        )
    with pytest.raises(SchemaMismatchError):
        client(Provider(trade_date=20240101)).fetch_daily_basic(
            DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A")
        )


def test_daily_basic_5999_rows_are_accepted():
    fields = PHASE2B_FIELDS["daily_basic"]
    dates = pd.date_range("2000-01-01", periods=5999).strftime("%Y%m%d").tolist()
    frame = pd.DataFrame(
        {field: [1.0] * 5999 for field in fields} | {"ts_code": ["A"] * 5999, "trade_date": dates}
    )

    class Provider:
        def daily_basic(self, **kwargs):
            return frame

    result = client(Provider()).fetch_daily_basic(
        DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A")
    )
    assert len(result.frame) == 5999


def test_daily_basic_selector_kwargs_and_custom_identity_fields():
    fields = PHASE2B_FIELDS["daily_basic"]

    class Provider:
        def __init__(self):
            self.calls = []

        def daily_basic(self, **kwargs):
            self.calls.append(kwargs)
            return pd.DataFrame(columns=fields)

    provider = Provider()
    api = client(provider)
    for request in (
        DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, trade_date="20240101"),
        DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A"),
        DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", start_date="20240101"),
        DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", end_date="20240102"),
        DailyBasicRequest(
            empty_policy=EmptyPolicy.ALLOW,
            ts_code="A",
            start_date="20240101",
            end_date="20240102",
        ),
    ):
        api.fetch_daily_basic(request)
    assert provider.calls == [
        {"trade_date": "20240101", "fields": ",".join(fields)},
        {"ts_code": "A", "fields": ",".join(fields)},
        {"ts_code": "A", "start_date": "20240101", "fields": ",".join(fields)},
        {"ts_code": "A", "end_date": "20240102", "fields": ",".join(fields)},
        {
            "ts_code": "A",
            "start_date": "20240101",
            "end_date": "20240102",
            "fields": ",".join(fields),
        },
    ]
    custom = ("ts_code", "trade_date", "dv_ttm", "limit_status")

    class Custom:
        def daily_basic(self, **kwargs):
            return pd.DataFrame(
                {field: [1] for field in custom} | {"ts_code": ["A"], "trade_date": ["20240101"]}
            )

    result = client(Custom()).fetch_daily_basic(
        DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", fields=custom)
    )
    assert result.frame.columns.tolist() == list(custom)

    for missing in (("trade_date", "dv_ttm"), ("ts_code", "dv_ttm")):
        with pytest.raises(SchemaMismatchError):
            client(Custom()).fetch_daily_basic(
                DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", fields=missing)
            )


def test_daily_basic_native_values_and_duplicate_identity_rejection():
    fields = PHASE2B_FIELDS["daily_basic"] + ("limit_status",)
    row: dict[str, object] = {field: 1.0 for field in fields}
    row.update(
        ts_code="A",
        trade_date="20240101",
        turnover_rate=12.5,
        dv_ttm=float("nan"),
        total_share=100,
        total_mv=200,
        limit_status=0,
    )
    row["pe"] = float("inf")
    row["ps"] = float("-inf")
    row["pb"] = None

    class Provider:
        def __init__(self, frame):
            self.frame = frame

        def daily_basic(self, **kwargs):
            return self.frame

    request = DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A", fields=fields)
    result = client(Provider(pd.DataFrame([row]))).fetch_daily_basic(request)
    assert result.frame.turnover_rate.iloc[0] == 12.5
    assert pd.isna(result.frame.dv_ttm.iloc[0])
    assert result.frame.pe.iloc[0] == float("inf")
    assert result.frame.ps.iloc[0] == float("-inf")
    assert pd.isna(result.frame.pb.iloc[0])
    assert result.frame.total_share.iloc[0] == 100 and result.frame.total_mv.iloc[0] == 200
    assert result.frame.limit_status.iloc[0] == 0
    duplicate = pd.DataFrame([row, row])
    with pytest.raises(SchemaMismatchError):
        client(Provider(duplicate)).fetch_daily_basic(request)
    changed: dict[str, object] = dict(row)
    changed["dv_ttm"] = 2.0
    with pytest.raises(SchemaMismatchError):
        client(Provider(pd.DataFrame([row, changed]))).fetch_daily_basic(request)


def test_daily_basic_snapshot_revisions_remain_distinct(tmp_path):
    fields = PHASE2B_FIELDS["daily_basic"]

    class Provider:
        def __init__(self):
            self.value = 1.0

        def daily_basic(self, **kwargs):
            row: dict[str, object] = {field: self.value for field in fields}
            row.update(ts_code="A", trade_date="20240101")
            return pd.DataFrame([row])

    provider = Provider()
    api = client(provider)
    request = DailyBasicRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="A")
    first = api.fetch_daily_basic(request)
    provider.value = 2.0
    second = api.fetch_daily_basic(request)
    store = SnapshotStore(tmp_path)
    a = store.write(
        request.spec,
        first.frame.to_json(orient="records").encode(),
        datetime(2024, 1, 1, tzinfo=UTC),
        "json-rows-v1",
    )
    b = store.write(
        request.spec,
        second.frame.to_json(orient="records").encode(),
        datetime(2024, 1, 2, tzinfo=UTC),
        "json-rows-v1",
    )
    assert a.path != b.path and store.replay(a).payload != store.replay(b).payload

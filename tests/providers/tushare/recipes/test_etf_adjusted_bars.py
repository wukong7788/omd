from datetime import UTC, datetime

import pandas as pd
import pytest

from ohmydata.core import CoverageError, EmptyResponseError, SchemaMismatchError
from ohmydata.providers.tushare import (
    AdjustedEtfBarsRequest,
    AdjustmentCoveragePolicy,
    EmptyPolicy,
    TushareClient,
    build_adjusted_etf_bars,
    fetch_adjusted_etf_bars,
)
from ohmydata.providers.tushare.endpoints import FundAdjustmentRequest, FundDailyRequest


def daily() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["A.ETF", "A.ETF"],
            "trade_date": ["20240103", "20240102"],
            "open": [10.0, 9.0],
            "high": [11.0, 10.0],
            "low": [9.0, 8.0],
            "close": [10.5, 9.5],
            "pre_close": [9.8, 8.8],
            "change": [0.7, 0.7],
            "pct_chg": [7.0, 8.0],
            "vol": [100, 200],
            "amount": [1.0, 2.0],
        }
    )


def adj() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["A.ETF", "A.ETF"],
            "trade_date": ["20240102", "20240103"],
            "adj_factor": [1.1, 1.2],
        }
    )


def test_formula_order_and_raw_columns_are_preserved() -> None:
    source_daily, source_adj = daily(), adj()
    result = build_adjusted_etf_bars(source_daily, source_adj, AdjustmentCoveragePolicy.STRICT)
    assert list(result.trade_date) == ["20240102", "20240103"]
    assert list(result.adj_open) == pytest.approx([9.9, 12.0])
    assert list(result.adj_high) == pytest.approx([11.0, 13.2])
    assert list(result.adj_low) == pytest.approx([8.8, 10.8])
    assert list(result.adj_close) == pytest.approx([10.45, 12.6])
    assert list(result.open) == [9.0, 10.0]
    assert list(result.high) == [10.0, 11.0]
    assert list(result.low) == [8.0, 9.0]
    assert list(result.close) == [9.5, 10.5]
    assert list(result.adj_factor) == [1.1, 1.2]
    assert list(result.vol) == [200, 100]
    assert list(result.amount) == [2.0, 1.0]
    source_daily.loc[0, "close"] = 999
    source_adj.loc[0, "adj_factor"] = 999
    assert list(result.adj_close) == pytest.approx([10.45, 12.6])


def test_result_metadata_and_repeated_frame_isolation() -> None:
    fake = Fake(daily(), adj())
    result = fetch_adjusted_etf_bars(
        TushareClient(fake, clock=lambda: datetime(2024, 1, 1, tzinfo=UTC)),
        AdjustedEtfBarsRequest("A.ETF", EmptyPolicy.ERROR, AdjustmentCoveragePolicy.STRICT),
    )
    first = result.frame
    first.loc[0, "close"] = 999
    assert result.frame.loc[0, "close"] != 999
    assert result.formula_identifier == "raw_ohlc_times_provider_adj_factor_v1"
    assert result.coverage_policy is AdjustmentCoveragePolicy.STRICT


@pytest.mark.parametrize("bad", [None, "x", float("nan"), float("inf"), True])
def test_invalid_raw_values_rejected(bad: object) -> None:
    frame = daily()
    frame["close"] = pd.Series([bad, 9.5], dtype="object")
    with pytest.raises(SchemaMismatchError):
        build_adjusted_etf_bars(frame, adj(), AdjustmentCoveragePolicy.STRICT)


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
@pytest.mark.parametrize("bad", [None, "bad", float("nan"), float("inf"), float("-inf"), True])
def test_invalid_raw_matrix(field: str, bad: object) -> None:
    frame = daily()
    frame[field] = pd.Series([bad, 1.0], dtype="object")
    with pytest.raises(SchemaMismatchError):
        build_adjusted_etf_bars(frame, adj(), AdjustmentCoveragePolicy.STRICT)


@pytest.mark.parametrize("bad", ["bad", float("inf"), float("-inf"), True])
def test_invalid_factor_rejected(bad: object) -> None:
    factors = adj()
    factors["adj_factor"] = pd.Series([bad, 1.2], dtype="object")
    with pytest.raises(SchemaMismatchError):
        build_adjusted_etf_bars(daily(), factors, AdjustmentCoveragePolicy.PRESERVE_MISSING_FACTOR)


def test_non_dataframe_and_request_validation() -> None:
    with pytest.raises(TypeError):
        build_adjusted_etf_bars([], adj(), AdjustmentCoveragePolicy.STRICT)
    with pytest.raises((TypeError, ValueError)):
        AdjustedEtfBarsRequest(1, EmptyPolicy.ERROR, AdjustmentCoveragePolicy.STRICT)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AdjustedEtfBarsRequest("A.ETF", "ERROR", AdjustmentCoveragePolicy.STRICT)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_adjusted_etf_bars(daily(), adj(), "STRICT")  # type: ignore[arg-type]


def test_missing_columns_null_keys_and_duplicate_daily() -> None:
    frame = daily().drop(columns=["high"])
    with pytest.raises(SchemaMismatchError):
        build_adjusted_etf_bars(frame, adj(), AdjustmentCoveragePolicy.STRICT)
    frame = daily()
    frame.loc[0, "ts_code"] = None
    with pytest.raises(SchemaMismatchError):
        build_adjusted_etf_bars(frame, adj(), AdjustmentCoveragePolicy.STRICT)
    factors = adj()
    factors.loc[0, "trade_date"] = None
    with pytest.raises(SchemaMismatchError):
        build_adjusted_etf_bars(daily(), factors, AdjustmentCoveragePolicy.STRICT)
    with pytest.raises(SchemaMismatchError):
        build_adjusted_etf_bars(
            pd.concat([daily(), daily().iloc[:1]]), adj(), AdjustmentCoveragePolicy.STRICT
        )
    with pytest.raises(SchemaMismatchError):
        build_adjusted_etf_bars(
            daily(), adj().drop(columns=["adj_factor"]), AdjustmentCoveragePolicy.STRICT
        )


def test_empty_helper_columns_and_mutation_isolation() -> None:
    empty_daily = daily().iloc[:0]
    result = build_adjusted_etf_bars(
        empty_daily, adj().iloc[:0], AdjustmentCoveragePolicy.PRESERVE_MISSING_FACTOR
    )
    assert list(result.columns) == list(empty_daily.columns) + [
        "adj_factor",
        *[f"adj_{x}" for x in ("open", "high", "low", "close")],
    ]
    empty_daily.loc[:, "close"] = 99
    assert result.empty
    assert result["close"].dtype == "float64"
    assert result["vol"].dtype == "int64"
    assert list(result.columns)[-5:] == [
        "adj_factor",
        "adj_open",
        "adj_high",
        "adj_low",
        "adj_close",
    ]


def test_missing_factor_policy_retains_nan_without_fill() -> None:
    factors = adj()
    factors.loc[0, "adj_factor"] = None
    result = build_adjusted_etf_bars(
        daily(), factors, AdjustmentCoveragePolicy.PRESERVE_MISSING_FACTOR
    )
    assert pd.isna(result.loc[0, "adj_factor"])
    assert pd.isna(result.loc[0, "adj_close"])
    with pytest.raises(CoverageError):
        build_adjusted_etf_bars(daily(), factors, AdjustmentCoveragePolicy.STRICT)


def test_duplicate_and_foreign_keys_rejected() -> None:
    with pytest.raises(SchemaMismatchError):
        build_adjusted_etf_bars(
            daily(), pd.concat([adj(), adj().iloc[:1]]), AdjustmentCoveragePolicy.STRICT
        )
    foreign = adj()
    foreign.loc[0, "ts_code"] = "OTHER.ETF"
    with pytest.raises(SchemaMismatchError):
        build_adjusted_etf_bars(daily(), foreign, AdjustmentCoveragePolicy.PRESERVE_MISSING_FACTOR)


class Fake:
    def __init__(self, daily_frame: pd.DataFrame, adj_frame: pd.DataFrame | None = None):
        self.daily_frame, self.adj_frame = daily_frame, adj_frame
        self.calls: list[tuple[str, dict[str, object]]] = []

    def fund_daily(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append(("fund_daily", kwargs))
        return self.daily_frame

    def fund_adj(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append(("fund_adj", kwargs))
        return self.adj_frame  # type: ignore[return-value]


class RaisingFake(Fake):
    def __init__(self, error: Exception):
        super().__init__(daily(), adj())
        self.error = error

    def fund_daily(self, **kwargs: object) -> pd.DataFrame:
        raise self.error


def test_fetch_short_circuits_allowed_empty_and_provenance() -> None:
    fake = Fake(daily().iloc[:0], adj())
    client = TushareClient(fake, clock=lambda: datetime(2024, 1, 1, tzinfo=UTC))
    result = fetch_adjusted_etf_bars(
        client, AdjustedEtfBarsRequest("A.ETF", EmptyPolicy.ALLOW, AdjustmentCoveragePolicy.STRICT)
    )
    assert [name for name, _ in fake.calls] == ["fund_daily"]
    assert result.adjustment_provenance is None
    assert result.frame.empty
    assert list(result.frame.columns)[-5:] == [
        "adj_factor",
        "adj_open",
        "adj_high",
        "adj_low",
        "adj_close",
    ]
    assert result.frame["close"].dtype == "float64"
    assert result.frame["vol"].dtype == "int64"


def test_fetch_propagates_forbidden_empty_and_exact_requests() -> None:
    fake = Fake(daily().iloc[:0], adj())
    client = TushareClient(fake, clock=lambda: datetime(2024, 1, 1, tzinfo=UTC))
    with pytest.raises(EmptyResponseError):
        fetch_adjusted_etf_bars(
            client,
            AdjustedEtfBarsRequest("A.ETF", EmptyPolicy.ERROR, AdjustmentCoveragePolicy.STRICT),
        )


def test_fetch_propagates_endpoint_error_object() -> None:
    error = EmptyResponseError("synthetic")
    with pytest.raises(EmptyResponseError) as caught:
        fetch_adjusted_etf_bars(
            TushareClient(RaisingFake(error)),
            AdjustedEtfBarsRequest("A.ETF", EmptyPolicy.ERROR, AdjustmentCoveragePolicy.STRICT),
        )
    assert caught.value is error


def test_fetch_composes_both_provenances() -> None:
    fake = Fake(daily(), adj())
    client = TushareClient(fake, clock=lambda: datetime(2024, 1, 1, tzinfo=UTC))
    result = fetch_adjusted_etf_bars(
        client,
        AdjustedEtfBarsRequest(
            "A.ETF",
            EmptyPolicy.ERROR,
            AdjustmentCoveragePolicy.PRESERVE_MISSING_FACTOR,
            start_date="20240101",
            end_date="20240131",
        ),
    )
    assert result.daily_provenance.endpoint == "fund_daily"
    assert result.adjustment_provenance is not None
    assert result.adjustment_provenance.endpoint == "fund_adj"
    assert (
        fake.calls[0][1]["fields"]
        == "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
    )
    assert fake.calls[1][1]["fields"] == "ts_code,trade_date,adj_factor"
    assert fake.calls[0][1]["ts_code"] == fake.calls[1][1]["ts_code"] == "A.ETF"
    assert fake.calls[0][1]["start_date"] == fake.calls[1][1]["start_date"] == "20240101"
    assert fake.calls[0][1]["end_date"] == fake.calls[1][1]["end_date"] == "20240131"
    assert fake.calls[1][1]["limit"] == 2000 and fake.calls[1][1]["offset"] == 0
    assert result.formula_identifier == "raw_ohlc_times_provider_adj_factor_v1"
    assert result.coverage_policy is AdjustmentCoveragePolicy.PRESERVE_MISSING_FACTOR
    assert (
        result.daily_provenance.request_identity
        == FundDailyRequest(
            empty_policy=EmptyPolicy.ERROR,
            ts_code="A.ETF",
            start_date="20240101",
            end_date="20240131",
        ).spec.request_identity
    )
    assert (
        result.adjustment_provenance.request_identity
        == FundAdjustmentRequest(
            empty_policy=EmptyPolicy.ERROR,
            ts_code="A.ETF",
            start_date="20240101",
            end_date="20240131",
        ).spec.request_identity
    )


def test_multi_symbol_alignment() -> None:
    frame = pd.DataFrame(
        {
            "ts_code": ["B", "A"],
            "trade_date": ["20240102", "20240102"],
            "open": [2.0, 1.0],
            "high": [2.0, 1.0],
            "low": [2.0, 1.0],
            "close": [2.0, 1.0],
            "vol": [2, 1],
            "amount": [2.0, 1.0],
        }
    )
    factors = pd.DataFrame(
        {"ts_code": ["B", "A"], "trade_date": ["20240102", "20240102"], "adj_factor": [3.0, 4.0]}
    )
    result = build_adjusted_etf_bars(frame, factors, AdjustmentCoveragePolicy.STRICT)
    assert list(zip(result.ts_code, result.adj_close)) == [("A", 4.0), ("B", 6.0)]


def test_characterization_fixture_parity() -> None:
    import json
    from pathlib import Path

    root = Path(__file__).parents[3] / "fixtures" / "characterization"
    daily_rows = json.loads((root / "fund_daily.json").read_text())["rows"]
    adj_rows = json.loads((root / "fund_adj.json").read_text())["rows"]
    result = build_adjusted_etf_bars(
        pd.DataFrame(daily_rows), pd.DataFrame(adj_rows), AdjustmentCoveragePolicy.STRICT
    )
    assert list(result.trade_date) == ["20240102", "20240103", "20240104"]
    assert result.loc[0, "adj_close"] == pytest.approx(11.0)

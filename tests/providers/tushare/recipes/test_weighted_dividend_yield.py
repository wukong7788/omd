import pandas as pd
import pytest

from ohmydata.core import CoverageError, SchemaMismatchError
from ohmydata.providers.tushare import (
    DividendYieldCoveragePolicy,
    DividendYieldWeightSource,
    build_index_dividend_yield,
    build_portfolio_dividend_yield,
)


def _portfolio() -> tuple[pd.DataFrame, pd.DataFrame]:
    return (
        pd.DataFrame(
            {
                "ts_code": ["F.OF", "F.OF"],
                "end_date": ["20231231"] * 2,
                "symbol": ["A", "B"],
                "mkv": [3_000_000.0, 1_000_000.0],
            }
        ),
        pd.DataFrame({"ts_code": ["A", "B"], "trade_date": ["20240102"] * 2, "dv_ttm": [4.0, 8.0]}),
    )


def test_portfolio_formula_and_metadata() -> None:
    weights, daily = _portfolio()
    result = build_portfolio_dividend_yield(
        weights, daily, DividendYieldCoveragePolicy.REQUIRE_COMPLETE
    )
    assert result.dividend_yield == pytest.approx(0.05)
    assert result.provider_total_weight == 4_000_000
    assert result.provider_supported_weight == 4_000_000
    assert result.finite_weight_coverage == 1
    assert result.weight_source is DividendYieldWeightSource.FUND_PORTFOLIO
    assert result.formula_identifier == "fund_portfolio_mkv_weighted_daily_basic_dv_ttm_v1"


def test_index_zero_and_negative_are_observations() -> None:
    weights = pd.DataFrame(
        {
            "index_code": ["000001.SZ"] * 3,
            "con_code": ["A", "B", "C"],
            "trade_date": ["20240102"] * 3,
            "weight": [60.0, 40.0, 0.0],
        }
    )
    daily = pd.DataFrame(
        {"ts_code": ["A", "B"], "trade_date": ["20240102"] * 2, "dv_ttm": [0.0, -2.0]}
    )
    result = build_index_dividend_yield(
        weights, daily, DividendYieldCoveragePolicy.REQUIRE_COMPLETE
    )
    assert result.dividend_yield == pytest.approx(-0.008)
    assert result.constituent_count == 3
    assert result.supported_constituent_count == 2


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), True, "4"])
def test_missing_dv_ttm_is_incomplete(value: object) -> None:
    weights, daily = _portfolio()
    daily = daily.astype(object)
    daily.loc[0, "dv_ttm"] = value  # type: ignore[reportArgumentType]
    preserved = build_portfolio_dividend_yield(
        weights, daily, DividendYieldCoveragePolicy.PRESERVE_INCOMPLETE
    )
    assert preserved.dividend_yield is None
    assert preserved.finite_weight_coverage == pytest.approx(0.25)
    with pytest.raises(CoverageError):
        build_portfolio_dividend_yield(weights, daily, DividendYieldCoveragePolicy.REQUIRE_COMPLETE)


def test_supported_weight_normalization_is_explicit_and_preserves_coverage() -> None:
    weights, daily = _portfolio()
    daily = daily.astype(object)
    daily.loc[0, "dv_ttm"] = None
    result = build_portfolio_dividend_yield(
        weights,
        daily,
        DividendYieldCoveragePolicy.NORMALIZE_SUPPORTED,
    )
    assert result.dividend_yield == pytest.approx(0.08)
    assert result.finite_weight_coverage == pytest.approx(0.25)
    assert result.provider_total_weight == 4_000_000
    assert result.provider_supported_weight == 1_000_000
    assert result.coverage_policy is DividendYieldCoveragePolicy.NORMALIZE_SUPPORTED
    assert (
        result.formula_identifier
        == "fund_portfolio_mkv_supported_weight_normalized_daily_basic_dv_ttm_v1"
    )


def test_supported_weight_normalization_with_zero_coverage_is_unknown() -> None:
    weights, daily = _portfolio()
    daily = daily.astype(object)
    daily.loc[:, "dv_ttm"] = None
    result = build_portfolio_dividend_yield(
        weights,
        daily,
        DividendYieldCoveragePolicy.NORMALIZE_SUPPORTED,
    )
    assert result.dividend_yield is None
    assert result.finite_weight_coverage == 0


def test_validation_and_input_isolation() -> None:
    weights, daily = _portfolio()
    before_weights, before_daily = weights.copy(deep=True), daily.copy(deep=True)
    build_portfolio_dividend_yield(weights, daily, DividendYieldCoveragePolicy.REQUIRE_COMPLETE)
    pd.testing.assert_frame_equal(weights, before_weights)
    pd.testing.assert_frame_equal(daily, before_daily)
    bad = daily.assign(trade_date=["20240102", "20240103"])
    with pytest.raises(SchemaMismatchError):
        build_portfolio_dividend_yield(weights, bad, DividendYieldCoveragePolicy.REQUIRE_COMPLETE)


def test_empty_daily_follows_policy() -> None:
    weights, _ = _portfolio()
    daily = pd.DataFrame(columns=["ts_code", "trade_date", "dv_ttm"])
    result = build_portfolio_dividend_yield(
        weights, daily, DividendYieldCoveragePolicy.PRESERVE_INCOMPLETE
    )
    assert result.dividend_yield is None
    assert result.provider_supported_weight == 0


def test_invalid_portfolio_weight_is_schema_error() -> None:
    weights, daily = _portfolio()
    weights = weights.astype(object)
    weights.loc[0, "mkv"] = "not-a-number"
    with pytest.raises(SchemaMismatchError):
        build_portfolio_dividend_yield(weights, daily, DividendYieldCoveragePolicy.REQUIRE_COMPLETE)


@pytest.mark.parametrize(
    "bad", [None, float("nan"), float("inf"), float("-inf"), True, "x", 0.0, -1.0]
)
def test_invalid_portfolio_weights_all_fail(bad: object) -> None:
    weights, daily = _portfolio()
    weights = weights.astype(object)
    weights.loc[0, "mkv"] = bad  # type: ignore[reportArgumentType]
    with pytest.raises(SchemaMismatchError):
        build_portfolio_dividend_yield(weights, daily, DividendYieldCoveragePolicy.REQUIRE_COMPLETE)


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), float("-inf"), True, "x", -1.0])
def test_invalid_index_weights_all_fail(bad: object) -> None:
    weights = pd.DataFrame(
        {
            "index_code": ["I"] * 2,
            "con_code": ["A", "B"],
            "trade_date": ["20240102"] * 2,
            "weight": [60.0, 40.0],
        }
    )
    daily = pd.DataFrame(
        {"ts_code": ["A", "B"], "trade_date": ["20240102"] * 2, "dv_ttm": [1.0, 2.0]}
    )
    weights = weights.astype(object)
    weights.loc[0, "weight"] = bad  # type: ignore[reportArgumentType]
    with pytest.raises(SchemaMismatchError):
        build_index_dividend_yield(weights, daily, DividendYieldCoveragePolicy.REQUIRE_COMPLETE)


def test_schema_and_policy_validation_branches() -> None:
    weights, daily = _portfolio()
    with pytest.raises(TypeError):
        build_portfolio_dividend_yield([], daily, DividendYieldCoveragePolicy.REQUIRE_COMPLETE)
    with pytest.raises(TypeError):
        build_portfolio_dividend_yield(weights, [], DividendYieldCoveragePolicy.REQUIRE_COMPLETE)
    with pytest.raises(TypeError):
        build_portfolio_dividend_yield(weights, daily, "REQUIRE_COMPLETE")  # type: ignore[arg-type]
    with pytest.raises(SchemaMismatchError):
        build_portfolio_dividend_yield(
            weights.drop(columns=["mkv"]), daily, DividendYieldCoveragePolicy.REQUIRE_COMPLETE
        )
    with pytest.raises(SchemaMismatchError):
        build_portfolio_dividend_yield(
            weights, daily.drop(columns=["dv_ttm"]), DividendYieldCoveragePolicy.REQUIRE_COMPLETE
        )


def test_key_date_and_empty_frame_validation() -> None:
    weights, daily = _portfolio()
    with pytest.raises(SchemaMismatchError):
        build_portfolio_dividend_yield(
            weights.assign(ts_code=["", "F.OF"]),
            daily,
            DividendYieldCoveragePolicy.REQUIRE_COMPLETE,
        )
    with pytest.raises(SchemaMismatchError):
        build_portfolio_dividend_yield(
            weights.assign(symbol=["A", "A"]), daily, DividendYieldCoveragePolicy.REQUIRE_COMPLETE
        )
    with pytest.raises(SchemaMismatchError):
        build_portfolio_dividend_yield(
            weights.assign(end_date=["20231231", "20240131"]),
            daily,
            DividendYieldCoveragePolicy.REQUIRE_COMPLETE,
        )
    with pytest.raises(SchemaMismatchError):
        build_portfolio_dividend_yield(
            weights,
            daily.assign(trade_date=["20240102", "20240103"]),
            DividendYieldCoveragePolicy.REQUIRE_COMPLETE,
        )
    empty_w = weights.iloc[0:0]
    with pytest.raises(CoverageError):
        build_portfolio_dividend_yield(
            empty_w, daily, DividendYieldCoveragePolicy.PRESERVE_INCOMPLETE
        )
    empty_i = pd.DataFrame(columns=["index_code", "con_code", "trade_date", "weight"])
    with pytest.raises(CoverageError):
        build_index_dividend_yield(empty_i, daily, DividendYieldCoveragePolicy.PRESERVE_INCOMPLETE)


def test_empty_daily_both_policies_and_extra_symbols() -> None:
    weights, daily = _portfolio()
    extra = pd.concat(
        [daily, pd.DataFrame({"ts_code": ["EXTRA"], "trade_date": ["20240102"], "dv_ttm": [99.0]})],
        ignore_index=True,
    )
    result = build_portfolio_dividend_yield(
        weights, extra, DividendYieldCoveragePolicy.REQUIRE_COMPLETE
    )
    assert result.dividend_yield == pytest.approx(0.05)
    empty = pd.DataFrame(columns=["ts_code", "trade_date", "dv_ttm"])
    for policy in (DividendYieldCoveragePolicy.PRESERVE_INCOMPLETE,):
        result = build_portfolio_dividend_yield(weights, empty, policy)
        assert result.dividend_yield is None
    with pytest.raises(CoverageError):
        build_portfolio_dividend_yield(weights, empty, DividendYieldCoveragePolicy.REQUIRE_COMPLETE)


def test_index_metadata_and_input_unchanged_on_failure() -> None:
    weights = pd.DataFrame(
        {
            "index_code": ["I", "I"],
            "con_code": ["A", "B"],
            "trade_date": ["20240102"] * 2,
            "weight": [60.0, 40.0],
        }
    )
    daily = pd.DataFrame(
        {"ts_code": ["A", "B"], "trade_date": ["20240102"] * 2, "dv_ttm": [0.0, -2.0]}
    )
    before_w, before_d = weights.copy(deep=True), daily.copy(deep=True)
    result = build_index_dividend_yield(
        weights, daily, DividendYieldCoveragePolicy.REQUIRE_COMPLETE
    )
    assert result.provider_total_weight == 100.0
    assert result.provider_supported_weight == 100.0
    assert result.finite_weight_coverage == 1.0
    assert result.constituent_count == result.supported_constituent_count == 2
    assert result.weight_source is DividendYieldWeightSource.INDEX_WEIGHT
    assert result.coverage_policy is DividendYieldCoveragePolicy.REQUIRE_COMPLETE
    assert result.formula_identifier == "index_weight_weighted_daily_basic_dv_ttm_v1"
    pd.testing.assert_frame_equal(weights, before_w)
    pd.testing.assert_frame_equal(daily, before_d)


def test_index_identity_and_zero_total_validation() -> None:
    weights = pd.DataFrame(
        {
            "index_code": ["I", "J"],
            "con_code": ["A", "B"],
            "trade_date": ["20240102"] * 2,
            "weight": [60.0, 40.0],
        }
    )
    daily = pd.DataFrame(
        {"ts_code": ["A", "B"], "trade_date": ["20240102"] * 2, "dv_ttm": [1.0, 2.0]}
    )
    with pytest.raises(SchemaMismatchError):
        build_index_dividend_yield(weights, daily, DividendYieldCoveragePolicy.REQUIRE_COMPLETE)
    with pytest.raises(SchemaMismatchError):
        build_index_dividend_yield(
            weights.assign(trade_date=["20240102", "20240103"]),
            daily,
            DividendYieldCoveragePolicy.REQUIRE_COMPLETE,
        )
    with pytest.raises(SchemaMismatchError):
        build_index_dividend_yield(
            weights.assign(con_code=[None, "B"]),  # type: ignore[arg-type]
            daily,
            DividendYieldCoveragePolicy.REQUIRE_COMPLETE,
        )
    zero = weights.assign(index_code=["I", "I"], weight=[0.0, 0.0])
    with pytest.raises(CoverageError):
        build_index_dividend_yield(zero, daily, DividendYieldCoveragePolicy.PRESERVE_INCOMPLETE)
    with pytest.raises(SchemaMismatchError):
        build_index_dividend_yield(
            weights.assign(con_code=["A", "A"]), daily, DividendYieldCoveragePolicy.REQUIRE_COMPLETE
        )

"""Offline weighted dividend-yield recipes for Tushare-native frames."""

from __future__ import annotations

import math
import numbers
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ....core import CoverageError, SchemaMismatchError

PORTFOLIO_FORMULA_IDENTIFIER = "fund_portfolio_mkv_weighted_daily_basic_dv_ttm_v1"
INDEX_FORMULA_IDENTIFIER = "index_weight_weighted_daily_basic_dv_ttm_v1"


class DividendYieldCoveragePolicy(str, Enum):
    REQUIRE_COMPLETE = "REQUIRE_COMPLETE"
    PRESERVE_INCOMPLETE = "PRESERVE_INCOMPLETE"


class DividendYieldWeightSource(str, Enum):
    FUND_PORTFOLIO = "FUND_PORTFOLIO"
    INDEX_WEIGHT = "INDEX_WEIGHT"


@dataclass(frozen=True)
class WeightedDividendYieldResult:
    dividend_yield: float | None
    finite_weight_coverage: float
    provider_total_weight: float
    provider_supported_weight: float
    constituent_count: int
    supported_constituent_count: int
    weight_source: DividendYieldWeightSource
    coverage_policy: DividendYieldCoveragePolicy
    formula_identifier: str


def _pd() -> Any:
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError("install ohmydata[tushare]") from exc
    return pd


def _finite_real(value: Any) -> bool:
    return (
        isinstance(value, numbers.Real)
        and not isinstance(value, bool)  # type: ignore[reportUnnecessaryIsInstance]
        and math.isfinite(float(value))
    )


def _key_valid(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _validate_frame(frame: Any, name: str, required: tuple[str, ...]) -> Any:
    pd = _pd()
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{name} must be a Pandas DataFrame")
    missing = [field for field in required if field not in frame.columns]
    if missing:
        raise SchemaMismatchError("required recipe fields are missing")
    return frame.copy(deep=True)


def _validate_daily(frame: Any) -> Any:
    daily = _validate_frame(frame, "daily_basic_frame", ("ts_code", "trade_date", "dv_ttm"))
    if daily.empty:
        return daily
    if daily["ts_code"].isna().any() or any(not _key_valid(x) for x in daily["ts_code"].tolist()):
        raise SchemaMismatchError("invalid daily-basic symbol")
    if daily["ts_code"].duplicated().any():
        raise SchemaMismatchError("duplicate daily-basic symbol")
    if daily["trade_date"].isna().any() or daily["trade_date"].nunique(dropna=False) != 1:
        raise SchemaMismatchError("daily-basic frame must contain exactly one trade date")
    return daily


def _validate_policy(policy: Any) -> DividendYieldCoveragePolicy:
    if not isinstance(policy, DividendYieldCoveragePolicy):  # type: ignore[reportUnnecessaryIsInstance]
        raise TypeError("coverage_policy must DividendYieldCoveragePolicy")
    return policy


def _calculate(
    weights: Any,
    daily_frame: Any,
    policy: DividendYieldCoveragePolicy,
    source: DividendYieldWeightSource,
    key: str,
    formula_identifier: str,
    weight_field: str,
    minimum: float,
) -> WeightedDividendYieldResult:
    _validate_policy(policy)
    daily = _validate_daily(daily_frame)
    required = (
        ("ts_code", "end_date", "symbol", "mkv")
        if source is DividendYieldWeightSource.FUND_PORTFOLIO
        else ("index_code", "con_code", "trade_date", "weight")
    )
    frame = _validate_frame(weights, "weight_frame", required)
    if frame.empty:
        raise CoverageError("weight frame is empty")
    if source is DividendYieldWeightSource.FUND_PORTFOLIO:
        if frame["ts_code"].isna().any() or frame["ts_code"].nunique(dropna=False) != 1:
            raise SchemaMismatchError("portfolio frame must contain exactly one fund")
        if frame["end_date"].isna().any() or frame["end_date"].nunique(dropna=False) != 1:
            raise SchemaMismatchError("portfolio frame must contain exactly one report date")
        if frame["symbol"].isna().any() or any(not _key_valid(x) for x in frame["symbol"].tolist()):
            raise SchemaMismatchError("invalid portfolio symbol")
        if frame["symbol"].duplicated().any():
            raise SchemaMismatchError("duplicate portfolio symbol")
    else:
        if frame["index_code"].isna().any() or frame["index_code"].nunique(dropna=False) != 1:
            raise SchemaMismatchError("index frame must contain exactly one index")
        if frame["trade_date"].isna().any() or frame["trade_date"].nunique(dropna=False) != 1:
            raise SchemaMismatchError("index frame must contain exactly one trade date")
        if frame["con_code"].isna().any() or any(
            not _key_valid(x) for x in frame["con_code"].tolist()
        ):
            raise SchemaMismatchError("invalid index constituent")
        if frame["con_code"].duplicated().any():
            raise SchemaMismatchError("duplicate index constituent")
    values = frame[weight_field].tolist()
    invalid_weight = any(not _finite_real(value) or float(value) < minimum for value in values)
    if source is DividendYieldWeightSource.FUND_PORTFOLIO:
        invalid_weight = invalid_weight or any(
            _finite_real(value) and float(value) <= 0 for value in values
        )
    if invalid_weight:
        raise SchemaMismatchError("invalid provider weight")
    total = float(sum(float(value) for value in values))
    if total <= 0 or not math.isfinite(total):
        raise CoverageError("provider total weight must be positive")
    # A zero-weight index constituent is not coverage-relevant.
    positive = frame.loc[frame[weight_field].astype(float) > 0, [key, weight_field]].copy()
    lookup = {row.ts_code: row.dv_ttm for row in daily.itertuples(index=False)}
    supported_weight = 0.0
    supported_count = 0
    weighted_sum = 0.0
    for row in positive.itertuples(index=False):
        weight = float(getattr(row, weight_field))
        value = lookup.get(getattr(row, key))
        if _finite_real(value):
            supported_weight += weight
            supported_count += 1
            weighted_sum += (weight / total) * float(value)  # type: ignore[arg-type]
    coverage = supported_weight / total
    complete = math.isclose(coverage, 1.0, rel_tol=0.0, abs_tol=1e-12)
    if not complete and policy is DividendYieldCoveragePolicy.REQUIRE_COMPLETE:
        raise CoverageError("finite dividend-yield weight coverage is incomplete")
    result_yield = weighted_sum / 100.0 if complete else None
    return WeightedDividendYieldResult(
        result_yield,
        coverage,
        total,
        supported_weight,
        len(frame),
        supported_count,
        source,
        policy,
        formula_identifier,
    )


def build_portfolio_dividend_yield(
    weight_frame: Any,
    daily_basic_frame: Any,
    coverage_policy: DividendYieldCoveragePolicy,
) -> WeightedDividendYieldResult:
    return _calculate(
        weight_frame,
        daily_basic_frame,
        coverage_policy,
        DividendYieldWeightSource.FUND_PORTFOLIO,
        "symbol",
        PORTFOLIO_FORMULA_IDENTIFIER,
        "mkv",
        0.0,
    )


def build_index_dividend_yield(
    weight_frame: Any,
    daily_basic_frame: Any,
    coverage_policy: DividendYieldCoveragePolicy,
) -> WeightedDividendYieldResult:
    return _calculate(
        weight_frame,
        daily_basic_frame,
        coverage_policy,
        DividendYieldWeightSource.INDEX_WEIGHT,
        "con_code",
        INDEX_FORMULA_IDENTIFIER,
        "weight",
        0.0,
    )


__all__ = [
    "DividendYieldCoveragePolicy",
    "DividendYieldWeightSource",
    "WeightedDividendYieldResult",
    "build_index_dividend_yield",
    "build_portfolio_dividend_yield",
]

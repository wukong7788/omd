"""Composable, provider-semantic adjusted ETF bars recipe."""

from __future__ import annotations

import math
import numbers
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ....core import CoverageError, FetchProvenance, SchemaMismatchError
from ..client import TushareClient
from ..endpoints import EmptyPolicy, FundAdjustmentRequest, FundDailyRequest

FORMULA_IDENTIFIER = "raw_ohlc_times_provider_adj_factor_v1"
_DAILY_REQUIRED = ("ts_code", "trade_date", "open", "high", "low", "close")
_KEYS = ["ts_code", "trade_date"]
_DERIVED = ["adj_open", "adj_high", "adj_low", "adj_close"]


class AdjustmentCoveragePolicy(str, Enum):
    STRICT = "STRICT"
    PRESERVE_MISSING_FACTOR = "PRESERVE_MISSING_FACTOR"


@dataclass(frozen=True)
class AdjustedEtfBarsRequest:
    ts_code: str
    empty_policy: EmptyPolicy
    coverage_policy: AdjustmentCoveragePolicy
    start_date: str | None = None
    end_date: str | None = None

    def __post_init__(self) -> None:
        if type(self.ts_code) is not str or not self.ts_code:
            raise ValueError("ts_code must be a non-empty string")
        if not isinstance(self.empty_policy, EmptyPolicy):  # type: ignore[reportUnnecessaryIsInstance]
            raise TypeError("empty_policy must be EmptyPolicy")
        if not isinstance(self.coverage_policy, AdjustmentCoveragePolicy):  # type: ignore[reportUnnecessaryIsInstance]
            raise TypeError("coverage_policy must AdjustmentCoveragePolicy")
        # Let the endpoint contract perform canonical date validation.
        FundDailyRequest(
            empty_policy=self.empty_policy,
            ts_code=self.ts_code,
            start_date=self.start_date,
            end_date=self.end_date,
        )


@dataclass(frozen=True, init=False)
class AdjustedEtfBarsResult:
    _frame: Any
    daily_provenance: FetchProvenance
    adjustment_provenance: FetchProvenance | None
    formula_identifier: str
    coverage_policy: AdjustmentCoveragePolicy

    def __init__(
        self,
        frame: Any,
        daily_provenance: FetchProvenance,
        adjustment_provenance: FetchProvenance | None,
        coverage_policy: AdjustmentCoveragePolicy,
    ) -> None:
        object.__setattr__(self, "_frame", frame.copy(deep=True))
        object.__setattr__(self, "daily_provenance", daily_provenance)
        object.__setattr__(self, "adjustment_provenance", adjustment_provenance)
        object.__setattr__(self, "formula_identifier", FORMULA_IDENTIFIER)
        object.__setattr__(self, "coverage_policy", coverage_policy)

    @property
    def frame(self) -> Any:
        return self._frame.copy(deep=True)


def _pd() -> Any:
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError("install ohmydata[tushare]") from exc
    return pd


def _number(value: Any) -> bool:
    value_any: Any = value
    return (
        isinstance(value_any, numbers.Real)
        and not isinstance(  # type: ignore[reportUnnecessaryIsInstance]
            value_any, bool
        )
        and math.isfinite(value_any)
    )


def build_adjusted_etf_bars(
    daily_frame: Any, adjustment_frame: Any, coverage_policy: AdjustmentCoveragePolicy
) -> Any:
    pd = _pd()
    if not isinstance(daily_frame, pd.DataFrame) or not isinstance(adjustment_frame, pd.DataFrame):
        raise TypeError("daily_frame and adjustment_frame must be Pandas DataFrames")
    if not isinstance(coverage_policy, AdjustmentCoveragePolicy):  # type: ignore[reportUnnecessaryIsInstance]
        raise TypeError("coverage_policy must AdjustmentCoveragePolicy")
    missing_daily = [x for x in _DAILY_REQUIRED if x not in daily_frame.columns]
    missing_adj = [x for x in (*_KEYS, "adj_factor") if x not in adjustment_frame.columns]
    if missing_daily or missing_adj:
        raise SchemaMismatchError("required recipe fields are missing")
    daily = daily_frame.copy(deep=True)
    adj = adjustment_frame.copy(deep=True)
    for frame in (daily, adj):
        if frame[_KEYS].isna().any().any():
            raise SchemaMismatchError("null recipe key")
    if daily.duplicated(_KEYS).any() or adj.duplicated(_KEYS).any():
        raise SchemaMismatchError("duplicate recipe key")
    for field in ("open", "high", "low", "close"):
        if any(not _number(value) for value in daily[field].tolist()):
            raise SchemaMismatchError("invalid raw OHLC value")
    for value in adj["adj_factor"].tolist():
        if pd.isna(value):
            continue
        if not _number(value):
            raise SchemaMismatchError("invalid adjustment factor")
    daily_keys = pd.MultiIndex.from_frame(daily[_KEYS])
    adj_keys = pd.MultiIndex.from_frame(adj[_KEYS])
    daily_symbols = set(daily["ts_code"].tolist())
    if any(symbol not in daily_symbols for symbol in adj["ts_code"].tolist()):
        raise SchemaMismatchError("adjustment symbol is not present in daily data")
    # Tushare may return additional factor dates for the requested symbol even
    # when identical date bounds are supplied to both endpoints. Those rows
    # cannot create output bars and are safe to discard. Coverage remains
    # strict in the opposite direction: every daily key must still have a
    # finite factor after the left join below.
    adj = adj.loc[adj_keys.isin(daily_keys)].copy()
    merged = daily.merge(
        adj[_KEYS + ["adj_factor"]], on=_KEYS, how="left", sort=False, validate="one_to_one"
    )
    factor = merged["adj_factor"]
    missing_factor = factor.isna()
    if coverage_policy is AdjustmentCoveragePolicy.STRICT and missing_factor.any():
        raise CoverageError("adjustment factor coverage is incomplete")
    for raw, derived in zip(("open", "high", "low", "close"), _DERIVED):
        merged[derived] = merged[raw] * factor
    merged = merged.sort_values(_KEYS, kind="mergesort", ignore_index=True)
    return merged


def fetch_adjusted_etf_bars(
    client: TushareClient, request: AdjustedEtfBarsRequest
) -> AdjustedEtfBarsResult:
    daily_request = FundDailyRequest(
        empty_policy=request.empty_policy,
        ts_code=request.ts_code,
        start_date=request.start_date,
        end_date=request.end_date,
    )
    daily_result = client.fetch_fund_daily(daily_request)
    if daily_result.frame.empty:
        pd = _pd()
        empty_adjustment = pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])
        frame = build_adjusted_etf_bars(
            daily_result.frame, empty_adjustment, request.coverage_policy
        )
        return AdjustedEtfBarsResult(frame, daily_result.provenance, None, request.coverage_policy)
    adjustment_request = FundAdjustmentRequest(
        empty_policy=EmptyPolicy.ERROR,
        ts_code=request.ts_code,
        start_date=request.start_date,
        end_date=request.end_date,
    )
    adjustment_result = client.fetch_fund_adjustment(adjustment_request)
    frame = build_adjusted_etf_bars(
        daily_result.frame, adjustment_result.frame, request.coverage_policy
    )
    return AdjustedEtfBarsResult(
        frame, daily_result.provenance, adjustment_result.provenance, request.coverage_policy
    )


__all__ = [
    "AdjustedEtfBarsRequest",
    "AdjustedEtfBarsResult",
    "AdjustmentCoveragePolicy",
    "build_adjusted_etf_bars",
    "fetch_adjusted_etf_bars",
]

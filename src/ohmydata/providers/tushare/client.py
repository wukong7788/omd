"""Offline-testable Tushare endpoint adapter."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from ...core import (
    EmptyDisposition,
    FetchProvenance,
    RateLimiter,
    RequestSpec,
    RetryPolicy,
    execute_with_retry,
)
from ...core.errors import CoverageError, EmptyResponseError, PaginationError, SchemaMismatchError
from .endpoints import (
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
)
from .errors import classify_tushare_exception

_KEYS: dict[str, tuple[str, ...]] = {
    "trade_cal": ("exchange", "cal_date"),
    "fund_basic": ("ts_code",),
    "fund_daily": ("ts_code", "trade_date"),
    "fund_adj": ("ts_code", "trade_date"),
    "daily": ("ts_code", "trade_date"),
    "adj_factor": ("ts_code", "trade_date"),
    "fund_nav": ("ts_code", "nav_date", "ann_date"),
    "fund_share": ("ts_code", "trade_date"),
    "etf_basic": ("ts_code",),
    "dividend": ("ts_code",),
}
_NON_NULL_KEYS = {
    **_KEYS,
    "fund_div": ("ts_code",),
    "fund_portfolio": ("ts_code", "end_date", "symbol"),
    "daily_basic": ("ts_code", "trade_date"),
    "index_weight": ("index_code", "trade_date", "con_code"),
    "etf_basic": ("ts_code",),
    "dividend": ("ts_code",),
}
_UNIQUE_KEYS = {
    **_KEYS,
    "fund_nav": (),
    "fund_div": (),
    "fund_portfolio": (),
    "daily_basic": ("ts_code", "trade_date"),
    "index_weight": ("index_code", "trade_date", "con_code"),
    "etf_basic": ("ts_code",),
    "dividend": (),
}
_SORT_KEYS = {
    "fund_div": ("ts_code", "ex_date", "ann_date", "imp_anndate", "pay_date"),
    "fund_portfolio": ("ts_code", "end_date", "ann_date", "symbol"),
    "daily_basic": ("ts_code", "trade_date"),
    "index_weight": ("index_code", "trade_date", "con_code"),
    "dividend": (
        "ts_code",
        "end_date",
        "ann_date",
        "imp_ann_date",
        "record_date",
        "ex_date",
        "pay_date",
        "div_listdate",
        "div_proc",
        "base_date",
        "stk_div",
        "stk_bo_rate",
        "stk_co_rate",
        "cash_div",
        "cash_div_tax",
        "base_share",
    ),
}
_CAPS = {
    "fund_basic": 15000,
    "fund_daily": 5000,
    "daily": 6000,
    "adj_factor": 6000,
    "fund_share": 2000,
    "daily_basic": 6000,
    "etf_basic": 5000,
}


@dataclass(frozen=True, init=False)
class TushareFetchResult:
    _frame: Any
    provenance: FetchProvenance
    page_count: int

    def __init__(self, frame: Any, provenance: FetchProvenance, page_count: int = 1) -> None:
        object.__setattr__(self, "_frame", frame.copy(deep=True))
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "page_count", page_count)

    @property
    def frame(self) -> Any:
        return self._frame.copy(deep=True)


class TushareClient:
    def __init__(
        self,
        client: Any,
        *,
        retry_policy: RetryPolicy | None = None,
        limiter: RateLimiter | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._retry_policy = retry_policy or RetryPolicy()
        self._limiter = limiter
        self._clock = clock or (lambda: datetime.now(UTC))

    def fetch_trade_calendar(self, request: TradeCalendarRequest) -> TushareFetchResult:
        return self._fetch_single(request)

    def fetch_fund_basic(self, request: FundBasicRequest) -> TushareFetchResult:
        return self._fetch_single(request)

    def fetch_etf_basic(self, request: EtfBasicRequest) -> TushareFetchResult:
        return self._fetch_single(request)

    def fetch_fund_daily(self, request: FundDailyRequest) -> TushareFetchResult:
        return self._fetch_single(request)

    def fetch_stock_daily(self, request: StockDailyRequest) -> TushareFetchResult:
        return self._fetch_single(request)

    def fetch_stock_adjustment(self, request: StockAdjustmentRequest) -> TushareFetchResult:
        return self._fetch_single(request)

    def fetch_stock_dividend(self, request: StockDividendRequest) -> TushareFetchResult:
        return self._fetch_single(request)

    def fetch_fund_adjustment(self, request: FundAdjustmentRequest) -> TushareFetchResult:
        return self._fetch_adjustment(request)

    def fetch_fund_nav(self, request: FundNavRequest) -> TushareFetchResult:
        return self._fetch_single(request)

    def fetch_fund_share(self, request: FundShareRequest) -> TushareFetchResult:
        return self._fetch_single(request)

    def fetch_fund_dividend(self, request: FundDividendRequest) -> TushareFetchResult:
        return self._fetch_single(request)

    def fetch_fund_portfolio(self, request: FundPortfolioRequest) -> TushareFetchResult:
        return self._fetch_single(request)

    def fetch_daily_basic(self, request: DailyBasicRequest) -> TushareFetchResult:
        return self._fetch_single(request)

    def fetch_index_weight(self, request: IndexWeightRequest) -> TushareFetchResult:
        return self._fetch_single(request)

    def _fetch_single(self, request: Any) -> TushareFetchResult:
        self._validate_request_keys(request)
        method = getattr(self._client, request.endpoint, None)
        if not callable(method):
            raise SchemaMismatchError("required Tushare endpoint is unavailable")
        parameters = self._provider_parameters(request.spec)
        raw, attempts = self._invoke(method, parameters)
        frame = self._validate_frame(raw, request)
        self._check_empty(frame, request.empty_policy)
        if request.endpoint == "index_weight" and not frame.empty:
            self._validate_index_weight_scope(frame, request)
        frame = self._sort_and_validate(frame, request)
        if isinstance(request, FundBasicRequest) and request.required_ts_codes:
            present = set(frame["ts_code"].tolist())
            if set(request.required_ts_codes) - present:
                raise CoverageError("required symbols missing")
        return self._result(frame, request.spec, 1, attempts)

    def _fetch_adjustment(self, request: FundAdjustmentRequest) -> TushareFetchResult:
        self._validate_request_keys(request)
        method = getattr(self._client, "fund_adj", None)
        if not callable(method):
            raise SchemaMismatchError("required Tushare endpoint is unavailable")
        import pandas as pd

        pages: list[Any] = []
        attempts: list[Any] = []
        for page in range(request.max_pages):
            params = self._provider_parameters(request.spec)
            params.update({"offset": page * request.page_size, "limit": request.page_size})
            raw, records = self._invoke(method, params)
            attempts.extend(records)
            frame = self._validate_frame(raw, request)
            if not frame.empty and frame.duplicated(list(_KEYS["fund_adj"])).any():
                raise PaginationError("duplicate fund_adj key")
            pages.append(frame)
            if len(frame) < request.page_size:
                break
        else:
            raise PaginationError("fund_adj max_pages exhausted")
        nonempty_pages = [page for page in pages if not page.empty]
        merged = (
            pd.concat(nonempty_pages, ignore_index=True)
            if nonempty_pages
            else pd.DataFrame(columns=request.fields)
        )
        self._check_empty(merged, request.empty_policy)
        merged = self._sort_and_validate(merged, request)
        return self._result(merged, request.spec, len(pages), tuple(attempts))

    @staticmethod
    def _provider_parameters(spec: RequestSpec) -> dict[str, Any]:
        parameters = dict(spec.effective_parameters)
        parameters["fields"] = ",".join(spec.fields)
        return parameters

    def _invoke(
        self, method: Callable[..., Any], parameters: dict[str, Any]
    ) -> tuple[Any, tuple[Any, ...]]:
        def call() -> Any:
            if self._limiter is not None:
                self._limiter.acquire()
            try:
                return method(**parameters)
            except Exception as exc:
                raise classify_tushare_exception(exc) from exc

        result = execute_with_retry(call, self._retry_policy)
        return result.value, result.attempts

    @staticmethod
    def _validate_request_keys(request: Any) -> None:
        missing = [key for key in _NON_NULL_KEYS[request.endpoint] if key not in request.fields]
        if missing:
            raise SchemaMismatchError("endpoint key fields must be requested")

    @staticmethod
    def _validate_frame(raw: Any, request: Any) -> Any:
        try:
            import pandas as pd
        except ImportError as exc:
            raise ImportError("install ohmydata[tushare]") from exc
        if not isinstance(raw, pd.DataFrame):
            raise SchemaMismatchError("provider result is not a DataFrame")
        frame = raw.copy(deep=True)
        if any(field not in frame.columns for field in request.fields):
            raise SchemaMismatchError("requested fields missing")
        for key in _NON_NULL_KEYS[request.endpoint]:
            if frame[key].isna().any():
                raise SchemaMismatchError("null endpoint key")
        return frame.loc[:, list(request.fields)].copy(deep=True)

    @staticmethod
    def _check_empty(frame: Any, policy: EmptyPolicy) -> None:
        if frame.empty and policy is EmptyPolicy.ERROR:
            raise EmptyResponseError("empty response")

    @staticmethod
    def _validate_index_weight_scope(frame: Any, request: IndexWeightRequest) -> None:
        if (frame["index_code"] != request.index_code).any():
            raise SchemaMismatchError("response index_code is outside request scope")
        dates = frame["trade_date"]
        for value in dates.tolist():
            if not isinstance(value, str) or re.fullmatch(r"\d{8}", value) is None:
                raise SchemaMismatchError("response trade_date is malformed")
            try:
                date(int(value[:4]), int(value[4:6]), int(value[6:]))
            except ValueError as exc:
                raise SchemaMismatchError("response trade_date is malformed") from exc
        if request.trade_date is not None:
            if (dates != request.trade_date).any():
                raise SchemaMismatchError("response trade_date is outside request scope")
            return
        assert request.start_date is not None and request.end_date is not None
        try:
            out_of_range = (dates < request.start_date) | (dates > request.end_date)
        except (TypeError, ValueError) as exc:
            raise SchemaMismatchError("response trade_date is malformed") from exc
        if out_of_range.any():
            raise SchemaMismatchError("response trade_date is outside request range")

    @staticmethod
    def _sort_and_validate(frame: Any, request: Any) -> Any:
        sort_fields = (
            _SORT_KEYS[request.endpoint]
            if request.endpoint in _SORT_KEYS
            else _KEYS[request.endpoint]
        )
        keys = [key for key in sort_fields if key in frame.columns]
        result = (
            frame.sort_values(keys, kind="mergesort", ignore_index=True)
            if not frame.empty
            else frame
        )
        unique = (
            _UNIQUE_KEYS[request.endpoint]
            if request.endpoint in _UNIQUE_KEYS
            else _KEYS[request.endpoint]
        )
        if unique and result.duplicated(list(unique)).any():
            if request.endpoint == "fund_adj":
                raise PaginationError("duplicate endpoint key")
            raise SchemaMismatchError("duplicate endpoint key")
        cap = _CAPS.get(request.endpoint)
        if cap is not None and len(result) == cap:
            raise PaginationError("response reached ambiguous row cap")
        return result.copy(deep=True)

    def _result(
        self, frame: Any, spec: RequestSpec, pages: int, attempts: tuple[Any, ...]
    ) -> TushareFetchResult:
        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None:
            raise ValueError("clock must return timezone-aware datetime")
        empty = EmptyDisposition.ALLOWED_EMPTY if frame.empty else EmptyDisposition.NOT_EMPTY
        provenance = FetchProvenance.from_request(
            spec,
            retrieved_at=retrieved_at,
            attempts=attempts,
            row_count=len(frame),
            columns=tuple(frame.columns),
            warnings=(),
            snapshot_identities=(),
            empty_disposition=empty,
        )
        return TushareFetchResult(frame.copy(deep=True), provenance, pages)

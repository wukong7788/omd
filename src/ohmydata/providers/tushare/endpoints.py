"""Typed request objects for the supported Tushare endpoints."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ...core import RequestSpec

_DATE = re.compile(r"^\d{8}$")


class EmptyPolicy(str, Enum):
    ALLOW = "ALLOW"
    ERROR = "ERROR"


def _validate_empty(policy: Any) -> None:
    if not isinstance(policy, EmptyPolicy):
        raise TypeError("empty_policy must be EmptyPolicy")


def _date(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise ValueError("dates must use YYYYMMDD")
    return value


def _date_range(start: str | None, end: str | None) -> tuple[str | None, str | None]:
    start, end = _date(start), _date(end)
    if start is not None and end is not None and start > end:
        raise ValueError("reversed date range")
    return start, end


def _fields(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    result = tuple(value) if value else default
    if not result or any(not isinstance(item, str) or not item for item in result):
        raise ValueError("fields must be a non-empty tuple of strings")
    if len(set(result)) != len(result):
        raise ValueError("fields must not contain duplicates")
    return result


def _nonempty(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class _BaseRequest:
    empty_policy: EmptyPolicy
    fields: tuple[str, ...] = ()
    endpoint: str = field(init=False, default="")

    def _spec(self, parameters: dict[str, Any]) -> RequestSpec:
        return RequestSpec(
            "tushare",
            self.endpoint,
            {key: value for key, value in parameters.items() if value is not None},
            self.fields,
        )


@dataclass(frozen=True)
class TradeCalendarRequest(_BaseRequest):
    exchange: str | None = "SSE"
    start_date: str | None = None
    end_date: str | None = None
    is_open: str | None = None
    fields: tuple[str, ...] = ()
    endpoint: str = field(init=False, default="trade_cal")

    def __post_init__(self) -> None:
        _validate_empty(self.empty_policy)
        start, end = _date_range(self.start_date, self.end_date)
        if self.is_open not in (None, "0", "1"):
            raise ValueError("is_open must be '0' or '1'")
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)
        object.__setattr__(
            self,
            "fields",
            _fields(self.fields, ("exchange", "cal_date", "is_open", "pretrade_date")),
        )

    @property
    def spec(self) -> RequestSpec:
        return self._spec(
            {
                "exchange": self.exchange,
                "start_date": self.start_date,
                "end_date": self.end_date,
                "is_open": self.is_open,
            }
        )


_BASIC_FIELDS = (
    "ts_code",
    "name",
    "management",
    "custodian",
    "fund_type",
    "found_date",
    "list_date",
    "issue_date",
    "delist_date",
    "issue_amount",
    "m_fee",
    "c_fee",
    "duration_year",
    "p_value",
    "benchmark",
    "status",
    "invest_type",
    "type",
    "trustee",
    "purc_startdate",
    "redm_startdate",
    "market",
)


@dataclass(frozen=True)
class FundBasicRequest(_BaseRequest):
    ts_code: str | None = None
    market: str | None = None
    status: str | None = None
    required_ts_codes: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    endpoint: str = field(init=False, default="fund_basic")

    def __post_init__(self) -> None:
        _validate_empty(self.empty_policy)
        raw_required: Any = self.required_ts_codes
        required = tuple(raw_required)
        if any(not isinstance(code, str) or not code for code in required) or len(
            set(required)
        ) != len(required):
            raise ValueError("required_ts_codes must contain unique non-empty strings")
        object.__setattr__(self, "required_ts_codes", required)
        object.__setattr__(self, "fields", _fields(self.fields, _BASIC_FIELDS))

    @property
    def spec(self) -> RequestSpec:
        return self._spec({"ts_code": self.ts_code, "market": self.market, "status": self.status})


_ETF_BASIC_FIELDS = (
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


@dataclass(frozen=True)
class EtfBasicRequest(_BaseRequest):
    ts_code: str | None = None
    index_code: str | None = None
    list_date: str | None = None
    list_status: str | None = None
    exchange: str | None = None
    mgr: str | None = None
    market: str | None = None
    fields: tuple[str, ...] = ()
    endpoint: str = field(init=False, default="etf_basic")

    def __post_init__(self) -> None:
        _validate_empty(self.empty_policy)
        object.__setattr__(self, "list_date", _date(self.list_date))
        object.__setattr__(self, "fields", _fields(self.fields, _ETF_BASIC_FIELDS))

    @property
    def spec(self) -> RequestSpec:
        return self._spec(
            {
                "ts_code": self.ts_code,
                "index_code": self.index_code,
                "list_date": self.list_date,
                "list_status": self.list_status,
                "exchange": self.exchange,
                "mgr": self.mgr,
                "market": self.market,
            }
        )


@dataclass(frozen=True)
class _FundDateRequest(_BaseRequest):
    ts_code: str | None = None
    trade_date: str | None = None
    start_date: str | None = None
    end_date: str | None = None

    def _validate_dates(self) -> None:
        _validate_empty(self.empty_policy)
        start, end = _date_range(self.start_date, self.end_date)
        if (self.ts_code is None) == (self.trade_date is None):
            raise ValueError("exactly one of ts_code or trade_date is required")
        if (start is not None or end is not None) and self.ts_code is None:
            raise ValueError("date range requires ts_code")
        object.__setattr__(self, "trade_date", _date(self.trade_date))
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)


_DAILY_FIELDS = (
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


@dataclass(frozen=True)
class FundDailyRequest(_FundDateRequest):
    fields: tuple[str, ...] = ()
    endpoint: str = field(init=False, default="fund_daily")

    def __post_init__(self) -> None:
        self._validate_dates()
        object.__setattr__(self, "fields", _fields(self.fields, _DAILY_FIELDS))

    @property
    def spec(self) -> RequestSpec:
        return self._spec(
            {
                "ts_code": self.ts_code,
                "trade_date": self.trade_date,
                "start_date": self.start_date,
                "end_date": self.end_date,
            }
        )


@dataclass(frozen=True)
class _StockDateRequest(_BaseRequest):
    ts_code: str | None = None
    trade_date: str | None = None
    start_date: str | None = None
    end_date: str | None = None

    def _validate_dates(self) -> None:
        _validate_empty(self.empty_policy)
        if self.ts_code is not None:
            _nonempty(self.ts_code, "ts_code")
        if self.trade_date is not None:
            _date(self.trade_date)
        if (self.ts_code is None) == (self.trade_date is None):
            raise ValueError("exactly one of ts_code or trade_date is required")
        start, end = _date_range(self.start_date, self.end_date)
        if (start is not None or end is not None) and self.ts_code is None:
            raise ValueError("date range requires ts_code")
        object.__setattr__(self, "trade_date", _date(self.trade_date))
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)


@dataclass(frozen=True)
class StockDailyRequest(_StockDateRequest):
    fields: tuple[str, ...] = ()
    endpoint: str = field(init=False, default="daily")

    def __post_init__(self) -> None:
        self._validate_dates()
        object.__setattr__(self, "fields", _fields(self.fields, _DAILY_FIELDS))

    @property
    def spec(self) -> RequestSpec:
        return self._spec(
            {
                "ts_code": self.ts_code,
                "trade_date": self.trade_date,
                "start_date": self.start_date,
                "end_date": self.end_date,
            }
        )


@dataclass(frozen=True)
class StockAdjustmentRequest(_StockDateRequest):
    fields: tuple[str, ...] = ()
    endpoint: str = field(init=False, default="adj_factor")

    def __post_init__(self) -> None:
        self._validate_dates()
        object.__setattr__(
            self, "fields", _fields(self.fields, ("ts_code", "trade_date", "adj_factor"))
        )

    @property
    def spec(self) -> RequestSpec:
        return self._spec(
            {
                "ts_code": self.ts_code,
                "trade_date": self.trade_date,
                "start_date": self.start_date,
                "end_date": self.end_date,
            }
        )


@dataclass(frozen=True)
class FundAdjustmentRequest(_FundDateRequest):
    page_size: int = 2000
    max_pages: int = 100
    fields: tuple[str, ...] = ()
    endpoint: str = field(init=False, default="fund_adj")

    def __post_init__(self) -> None:
        self._validate_dates()
        if type(self.page_size) is not int or not 1 <= self.page_size <= 2000:
            raise ValueError("page_size must be between 1 and 2000")
        if type(self.max_pages) is not int or self.max_pages < 1:
            raise ValueError("max_pages must be positive")
        object.__setattr__(
            self, "fields", _fields(self.fields, ("ts_code", "trade_date", "adj_factor"))
        )

    @property
    def spec(self) -> RequestSpec:
        return self._spec(
            {
                "ts_code": self.ts_code,
                "trade_date": self.trade_date,
                "start_date": self.start_date,
                "end_date": self.end_date,
                "limit": self.page_size,
            }
        )


@dataclass(frozen=True)
class FundNavRequest(_BaseRequest):
    ts_code: str | None = None
    nav_date: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    market: str | None = None
    fields: tuple[str, ...] = ()
    endpoint: str = field(init=False, default="fund_nav")

    def __post_init__(self) -> None:
        _validate_empty(self.empty_policy)
        start, end = _date_range(self.start_date, self.end_date)
        if (self.ts_code is None) == (self.nav_date is None):
            raise ValueError("exactly one of ts_code or nav_date is required")
        if (start is not None or end is not None) and self.ts_code is None:
            raise ValueError("date range requires ts_code")
        object.__setattr__(self, "nav_date", _date(self.nav_date))
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)
        object.__setattr__(
            self,
            "fields",
            _fields(
                self.fields,
                (
                    "ts_code",
                    "ann_date",
                    "nav_date",
                    "unit_nav",
                    "accum_nav",
                    "accum_div",
                    "net_asset",
                    "total_netasset",
                    "adj_nav",
                ),
            ),
        )

    @property
    def spec(self) -> RequestSpec:
        return self._spec(
            {
                "ts_code": self.ts_code,
                "nav_date": self.nav_date,
                "start_date": self.start_date,
                "end_date": self.end_date,
                "market": self.market,
            }
        )


@dataclass(frozen=True)
class FundShareRequest(_BaseRequest):
    ts_code: str | None = None
    trade_date: str | None = None
    market: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    fields: tuple[str, ...] = ()
    endpoint: str = field(init=False, default="fund_share")

    def __post_init__(self) -> None:
        _validate_empty(self.empty_policy)
        start, end = _date_range(self.start_date, self.end_date)
        if not any((self.ts_code, self.trade_date, self.market)):
            raise ValueError("one filter is required")
        if (start is not None or end is not None) and self.ts_code is None:
            raise ValueError("date range requires ts_code")
        object.__setattr__(self, "trade_date", _date(self.trade_date))
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)
        object.__setattr__(
            self, "fields", _fields(self.fields, ("ts_code", "trade_date", "fd_share"))
        )

    @property
    def spec(self) -> RequestSpec:
        return self._spec(
            {
                "ts_code": self.ts_code,
                "trade_date": self.trade_date,
                "market": self.market,
                "start_date": self.start_date,
                "end_date": self.end_date,
            }
        )


@dataclass(frozen=True)
class FundDividendRequest(_BaseRequest):
    ts_code: str | None = None
    ann_date: str | None = None
    ex_date: str | None = None
    pay_date: str | None = None
    fields: tuple[str, ...] = ()
    endpoint: str = field(init=False, default="fund_div")

    def __post_init__(self) -> None:
        _validate_empty(self.empty_policy)
        selectors = (self.ts_code, self.ann_date, self.ex_date, self.pay_date)
        if sum(value not in (None, "") for value in selectors) != 1:
            raise ValueError("exactly one fund_div selector is required")
        if self.ts_code is not None:
            _nonempty(self.ts_code, "ts_code")
        for name in ("ann_date", "ex_date", "pay_date"):
            object.__setattr__(self, name, _date(getattr(self, name)))
        object.__setattr__(
            self,
            "fields",
            _fields(
                self.fields,
                (
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
            ),
        )

    @property
    def spec(self) -> RequestSpec:
        return self._spec(
            {
                "ts_code": self.ts_code,
                "ann_date": self.ann_date,
                "ex_date": self.ex_date,
                "pay_date": self.pay_date,
            }
        )


@dataclass(frozen=True)
class FundPortfolioRequest(_BaseRequest):
    ts_code: str | None = None
    ann_date: str | None = None
    period: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    symbol: str | None = None
    fields: tuple[str, ...] = ()
    endpoint: str = field(init=False, default="fund_portfolio")

    def __post_init__(self) -> None:
        _validate_empty(self.empty_policy)
        _nonempty(self.ts_code, "ts_code")
        if self.symbol is not None:
            _nonempty(self.symbol, "symbol")
        ann = _date(self.ann_date)
        period = _date(self.period)
        start, end = _date_range(self.start_date, self.end_date)
        if (start is None) != (end is None):
            raise ValueError("start_date and end_date must be provided together")
        if start is not None:
            assert end is not None
            if start[:4] != end[:4] or period is not None:
                raise ValueError(
                    "report range must stay within one year and cannot combine with period"
                )
        if ann is None and period is None and start is None:
            raise ValueError("a bounded report selector is required")
        object.__setattr__(self, "ann_date", ann)
        object.__setattr__(self, "period", period)
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)
        object.__setattr__(
            self,
            "fields",
            _fields(
                self.fields,
                (
                    "ts_code",
                    "ann_date",
                    "end_date",
                    "symbol",
                    "mkv",
                    "amount",
                    "stk_mkv_ratio",
                    "stk_float_ratio",
                ),
            ),
        )

    @property
    def spec(self) -> RequestSpec:
        return self._spec(
            {
                "ts_code": self.ts_code,
                "ann_date": self.ann_date,
                "period": self.period,
                "start_date": self.start_date,
                "end_date": self.end_date,
                "symbol": self.symbol,
            }
        )


_DAILY_BASIC_FIELDS = (
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
)


@dataclass(frozen=True)
class DailyBasicRequest(_BaseRequest):
    ts_code: str | None = None
    trade_date: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    fields: tuple[str, ...] = ()
    endpoint: str = field(init=False, default="daily_basic")

    def __post_init__(self) -> None:
        _validate_empty(self.empty_policy)
        if self.ts_code is not None:
            _nonempty(self.ts_code, "ts_code")
        if self.trade_date is not None:
            _nonempty(self.trade_date, "trade_date")
        if (self.ts_code in (None, "")) == (self.trade_date in (None, "")):
            raise ValueError("exactly one of ts_code or trade_date is required")
        start, end = _date_range(self.start_date, self.end_date)
        if (start is not None or end is not None) and self.ts_code in (None, ""):
            raise ValueError("date range requires ts_code")
        object.__setattr__(self, "trade_date", _date(self.trade_date))
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)
        object.__setattr__(self, "fields", _fields(self.fields, _DAILY_BASIC_FIELDS))

    @property
    def spec(self) -> RequestSpec:
        return self._spec(
            {
                "ts_code": self.ts_code,
                "trade_date": self.trade_date,
                "start_date": self.start_date,
                "end_date": self.end_date,
            }
        )


@dataclass(frozen=True)
class IndexWeightRequest(_BaseRequest):
    index_code: str | None = None
    trade_date: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    fields: tuple[str, ...] = ()
    endpoint: str = field(init=False, default="index_weight")

    def __post_init__(self) -> None:
        _validate_empty(self.empty_policy)
        _nonempty(self.index_code, "index_code")
        if (self.trade_date is None) == (self.start_date is None or self.end_date is None):
            raise ValueError("exactly one index weight date selector is required")
        trade = _date(self.trade_date)
        start, end = _date_range(self.start_date, self.end_date)
        if start is not None:
            assert end is not None
            if start[:6] != end[:6]:
                raise ValueError("index weight range must stay within one calendar month")
        object.__setattr__(self, "trade_date", trade)
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)
        object.__setattr__(
            self, "fields", _fields(self.fields, ("index_code", "con_code", "trade_date", "weight"))
        )

    @property
    def spec(self) -> RequestSpec:
        return self._spec(
            {
                "index_code": self.index_code,
                "trade_date": self.trade_date,
                "start_date": self.start_date,
                "end_date": self.end_date,
            }
        )

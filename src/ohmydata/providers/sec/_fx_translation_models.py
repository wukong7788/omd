"""Typed models and PIT-safe FX rate lookup table for SEC USD fundamentals translation."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from enum import Enum
from types import MappingProxyType
from typing import Any

BULK_FUNDAMENTALS_USD_DATASET_SCHEMA = "sec-bulk-fundamentals-usd-v1"
BULK_FUNDAMENTALS_USD_WRITER_PROFILE = "sec-bulk-fundamentals-usd-parquet-v1"
FX_FILING_IMPLIED_SOURCE = "FILING_IMPLIED_CONVENIENCE_TRANSLATION"
DEFAULT_FX_MAX_STALENESS_DAYS = 10
DEFAULT_FX_MAX_IMPLIED_SPREAD = Decimal("0.02")

_ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class SecFxTranslationStatus(str, Enum):
    """Explicit translation outcome for a single SEC bulk fundamental row."""

    NATIVE_USD = "NATIVE_USD"
    TRANSLATED_FILING_IMPLIED = "TRANSLATED_FILING_IMPLIED"
    TRANSLATED_EXTERNAL_FX = "TRANSLATED_EXTERNAL_FX"
    MISSING_FX_RATE = "MISSING_FX_RATE"
    AMBIGUOUS_FX_RATE = "AMBIGUOUS_FX_RATE"
    MISSING_CURRENCY = "MISSING_CURRENCY"


class SecFxDatePolicy(str, Enum):
    """PIT-safe spot anchor date policy for external FX lookups."""

    PERIOD_END_SPOT = "PERIOD_END_SPOT"
    FILING_DATE_SPOT = "FILING_DATE_SPOT"


class SecFxFlowMethod(str, Enum):
    """External FX method for duration (income statement / cash flow) line items."""

    PERIOD_AVERAGE = "PERIOD_AVERAGE"
    SPOT = "SPOT"


def _validate_currency(currency: str, field_name: str = "currency") -> str:
    if type(currency) is not str or _ISO_CURRENCY_RE.fullmatch(currency) is None:
        raise ValueError(f"{field_name} must be a 3-letter uppercase ISO 4217 code")
    return currency


def _validate_date(value: date, field_name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime.date (not datetime)")
    return value


def _validate_utc_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _validate_positive_decimal(value: Decimal, field_name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{field_name} must be a strictly positive finite Decimal")
    return value


@dataclass(frozen=True)
class SecFxRateObservation:
    """One dated FX rate observation in USD-per-unit convention.

    Convention: ``usd_per_unit`` means ``1 unit of <currency> = <usd_per_unit> USD``
    (for example ``EUR -> ~1.08``, ``CNY -> ~0.138``, ``TWD -> ~0.031``,
    ``GBP -> ~1.27``), so ``usd_amount = native_amount * usd_per_unit``.
    """

    currency: str
    rate_date: date
    usd_per_unit: Decimal
    source: str
    available_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_currency(self.currency, "currency")
        _validate_date(self.rate_date, "rate_date")
        _validate_positive_decimal(self.usd_per_unit, "usd_per_unit")
        if type(self.source) is not str or not self.source.strip():
            raise ValueError("source must be a non-empty string")
        if self.available_at is not None:
            normalized = _validate_utc_datetime(self.available_at, "available_at")
            object.__setattr__(self, "available_at", normalized)


@dataclass(frozen=True)
class SecFxPeriodAverage:
    """PIT-bounded arithmetic mean FX rate across an observation window.

    ``usd_per_unit`` follows the same convention as :class:`SecFxRateObservation`:
    ``usd_amount = native_amount * usd_per_unit``.
    """

    currency: str
    usd_per_unit: Decimal
    window_start: date
    window_end: date
    first_rate_date: date
    last_rate_date: date
    observation_count: int
    source: str


class SecFxRateTable:
    """Immutable, strictly PIT-safe FX rate table indexed by ISO currency and date."""

    __slots__ = ("_dates_by_currency", "_obs_by_currency", "_observation_count")

    def __init__(
        self,
        obs_by_currency: Mapping[str, tuple[SecFxRateObservation, ...]],
        dates_by_currency: Mapping[str, tuple[date, ...]],
        observation_count: int,
    ) -> None:
        self._obs_by_currency: Mapping[str, tuple[SecFxRateObservation, ...]] = MappingProxyType(
            dict(obs_by_currency)
        )
        self._dates_by_currency: Mapping[str, tuple[date, ...]] = MappingProxyType(
            dict(dates_by_currency)
        )
        self._observation_count = observation_count

    @classmethod
    def from_observations(
        cls,
        observations: Iterable[SecFxRateObservation],
    ) -> SecFxRateTable:
        """Build an immutable rate table, failing closed on conflicting duplicate keys."""
        by_key: dict[tuple[str, date], SecFxRateObservation] = {}
        for obs in observations:
            if not isinstance(obs, SecFxRateObservation):
                raise TypeError("observations must contain SecFxRateObservation instances")
            key = (obs.currency, obs.rate_date)
            existing = by_key.get(key)
            if existing is not None:
                if existing != obs:
                    raise ValueError(
                        f"conflicting duplicate FX rate for ({obs.currency}, {obs.rate_date.isoformat()})"
                    )
                continue
            by_key[key] = obs

        grouped: dict[str, list[SecFxRateObservation]] = {}
        for obs in by_key.values():
            grouped.setdefault(obs.currency, []).append(obs)

        obs_map: dict[str, tuple[SecFxRateObservation, ...]] = {}
        dates_map: dict[str, tuple[date, ...]] = {}
        total = 0
        for ccy in sorted(grouped):
            ordered = tuple(sorted(grouped[ccy], key=lambda item: item.rate_date))
            obs_map[ccy] = ordered
            dates_map[ccy] = tuple(item.rate_date for item in ordered)
            total += len(ordered)
        return cls(obs_map, dates_map, total)

    @property
    def currencies(self) -> tuple[str, ...]:
        return tuple(self._obs_by_currency.keys())

    @property
    def observation_count(self) -> int:
        return self._observation_count

    def __len__(self) -> int:
        return self._observation_count

    def lookup_spot(
        self,
        currency: str,
        anchor_date: date,
        *,
        accepted_at: datetime | None = None,
        max_staleness_days: int = DEFAULT_FX_MAX_STALENESS_DAYS,
    ) -> SecFxRateObservation | None:
        """Return latest PIT-safe observation on or before ``anchor_date`` within staleness bound.

        Never looks ahead past ``anchor_date`` or ``accepted_at`` and never fabricates
        a fallback value such as ``0`` or ``1.0`` when missing or stale.
        """
        _validate_currency(currency, "currency")
        _validate_date(anchor_date, "anchor_date")
        if type(max_staleness_days) is not int or max_staleness_days < 0:
            raise ValueError("max_staleness_days must be a non-negative integer")
        accepted_utc = (
            _validate_utc_datetime(accepted_at, "accepted_at") if accepted_at is not None else None
        )
        effective_anchor = (
            min(anchor_date, accepted_utc.date()) if accepted_utc is not None else anchor_date
        )

        obs_list = self._obs_by_currency.get(currency)
        if not obs_list:
            return None
        date_list = self._dates_by_currency[currency]
        min_allowed_date = effective_anchor - timedelta(days=max_staleness_days)
        idx = bisect_right(date_list, effective_anchor) - 1
        while idx >= 0:
            obs = obs_list[idx]
            if obs.rate_date < min_allowed_date:
                return None
            if accepted_utc is None or obs.available_at is None or obs.available_at <= accepted_utc:
                return obs
            idx -= 1
        return None

    def lookup_period_average(
        self,
        currency: str,
        period_start: date,
        period_end: date,
        *,
        accepted_at: datetime | None = None,
    ) -> SecFxPeriodAverage | None:
        """Return exact Decimal arithmetic mean over ``[period_start, min(period_end, accepted_at)]``."""
        _validate_currency(currency, "currency")
        _validate_date(period_start, "period_start")
        _validate_date(period_end, "period_end")
        if period_start > period_end:
            raise ValueError("period_start must be <= period_end")
        accepted_utc = (
            _validate_utc_datetime(accepted_at, "accepted_at") if accepted_at is not None else None
        )
        effective_end = (
            min(period_end, accepted_utc.date()) if accepted_utc is not None else period_end
        )
        if effective_end < period_start:
            return None

        obs_list = self._obs_by_currency.get(currency)
        if not obs_list:
            return None
        date_list = self._dates_by_currency[currency]
        lo = bisect_left(date_list, period_start)
        hi = bisect_right(date_list, effective_end)
        matched = [
            obs
            for obs in obs_list[lo:hi]
            if accepted_utc is None or obs.available_at is None or obs.available_at <= accepted_utc
        ]
        if not matched:
            return None

        with localcontext() as ctx:
            ctx.prec = 34
            total = Decimal(0)
            for obs in matched:
                total += obs.usd_per_unit
            mean_rate = total / Decimal(len(matched))
        sources = sorted({obs.source for obs in matched})
        return SecFxPeriodAverage(
            currency=currency,
            usd_per_unit=mean_rate,
            window_start=period_start,
            window_end=effective_end,
            first_rate_date=matched[0].rate_date,
            last_rate_date=matched[-1].rate_date,
            observation_count=len(matched),
            source=",".join(sources),
        )


@dataclass(frozen=True)
class SecFilingImpliedFxRate:
    """Implied FX rate extracted from dual-currency facts of one kind (flow or balance sheet)."""

    status: str
    rate: Decimal | None
    spread: Decimal
    tags: tuple[str, ...]
    pair_count: int
    min_rate: Decimal
    max_rate: Decimal


@dataclass(frozen=True)
class SecFilingImpliedFxPair:
    """Filing-implied flow and balance-sheet FX rates for one ``(cik, accession, currency)``."""

    cik: str
    accession_number: str
    native_currency: str
    flow: SecFilingImpliedFxRate | None
    bs: SecFilingImpliedFxRate | None


@dataclass(frozen=True)
class SecUsdTranslatedFundamentalRow:
    """One PIT SEC bulk fundamental row with explicit USD translation and FX provenance."""

    cik: str
    entity_name: str | None
    symbols: tuple[str, ...]
    accession_number: str
    form: str
    is_amendment: bool
    filing_date: date
    report_date: date
    accepted_at: datetime
    fiscal_year: int
    fiscal_period: str
    period_kind: str
    period_start: date | None
    period_end: date
    taxonomy: str
    original_currency: str | None
    fx_status: SecFxTranslationStatus
    fx_flow_rate_to_usd: Decimal | None = None
    fx_flow_source: str | None = None
    fx_flow_date: date | None = None
    fx_bs_rate_to_usd: Decimal | None = None
    fx_bs_source: str | None = None
    fx_bs_date: date | None = None
    fx_provenance_json: str = "{}"
    revenue_usd: Decimal | None = None
    gross_profit_usd: Decimal | None = None
    operating_income_usd: Decimal | None = None
    net_income_usd: Decimal | None = None
    ocf_usd: Decimal | None = None
    capex_usd: Decimal | None = None
    total_assets_usd: Decimal | None = None
    total_debt_usd: Decimal | None = None
    eps_diluted_usd: Decimal | None = None
    diluted_shares: Decimal | None = None
    dei_shares_outstanding: Decimal | None = None
    dei_shares_outstanding_end: date | None = None


@dataclass(frozen=True)
class SecUsdTranslationResult:
    """Aggregate result of translating SEC bulk fundamental rows into USD."""

    rows: tuple[SecUsdTranslatedFundamentalRow, ...]
    counts: dict[str, int]
    policy: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "BULK_FUNDAMENTALS_USD_DATASET_SCHEMA",
    "BULK_FUNDAMENTALS_USD_WRITER_PROFILE",
    "DEFAULT_FX_MAX_IMPLIED_SPREAD",
    "DEFAULT_FX_MAX_STALENESS_DAYS",
    "FX_FILING_IMPLIED_SOURCE",
    "SecFilingImpliedFxPair",
    "SecFilingImpliedFxRate",
    "SecFxDatePolicy",
    "SecFxFlowMethod",
    "SecFxPeriodAverage",
    "SecFxRateObservation",
    "SecFxRateTable",
    "SecFxTranslationStatus",
    "SecUsdTranslatedFundamentalRow",
    "SecUsdTranslationResult",
    "_validate_currency",
    "_validate_positive_decimal",
    "_validate_utc_datetime",
]

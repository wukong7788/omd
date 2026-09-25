"""Explicit, approximate USD translation of TSMC SEC 6-K income rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext

from ._tsm_6k_parse import SecTsm6KValues

_USD_MILLION = Decimal(1)


@dataclass(frozen=True)
class SecTsm6KEstimatedUsdValues:
    """Reported USD revenue/EPS and estimated USD P&L, in distinct fields.

    Estimates assume the exchange relationship implied by the two reported
    revenue figures also applies to other income rows. The reported USD revenue
    is rounded to 0.01 billion, so estimates are not issuer-disclosed USD facts.
    """

    period_end: date
    implied_twd_per_usd: Decimal
    reported_revenue_usd_million: Decimal
    estimated_gross_profit_usd_million: Decimal
    estimated_operating_income_usd_million: Decimal
    estimated_income_before_tax_usd_million: Decimal
    estimated_net_income_usd_million: Decimal
    reported_diluted_eps_usd_per_adr: Decimal


def estimate_sec_tsm_6k_usd_from_revenue(values: SecTsm6KValues) -> SecTsm6KEstimatedUsdValues:
    """Translate TWD P&L by the *implied* revenue rate, rounded to USD million.

    Callers must opt in. No forward-guidance exchange rate is used. The
    translation is an estimate, suitable for approximate comparisons rather
    than a replacement for directly reported USD financial statements.
    """
    if not isinstance(values, SecTsm6KValues):
        raise TypeError("values must be SEC TSM 6-K parsed values")
    if values.revenue_twd_million <= 0 or values.revenue_usd_billion <= 0:
        raise ValueError("reported revenue must be positive for an implied exchange rate")
    with localcontext() as context:
        context.prec = 34
        reported_usd_million = values.revenue_usd_billion * 1000
        implied_rate = values.revenue_twd_million / reported_usd_million

        def estimate(twd_million: Decimal) -> Decimal:
            return (twd_million / implied_rate).quantize(_USD_MILLION, rounding=ROUND_HALF_UP)

        return SecTsm6KEstimatedUsdValues(
            values.period_end,
            implied_rate,
            reported_usd_million,
            estimate(values.gross_profit_twd_million),
            estimate(values.operating_income_twd_million),
            estimate(values.income_before_tax_twd_million),
            estimate(values.net_income_twd_million),
            values.diluted_eps_usd_per_adr,
        )


__all__ = ["SecTsm6KEstimatedUsdValues", "estimate_sec_tsm_6k_usd_from_revenue"]

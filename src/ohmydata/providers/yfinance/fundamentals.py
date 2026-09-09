"""Fundamentals, financial statements, valuation metrics, and analyst estimates models."""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any

import pandas as pd

from ohmydata.core.policy import RetryPolicy
from ohmydata.core.provenance import FetchProvenance

from ._fundamentals_parsers import (
    _safe_float,
    calibrate_forward_pe,
    derive_implied_price,
    detect_gaap_distortion,
    extract_estimates_horizons,
    extract_metric_pair,
    extract_quarterly_pair,
    extract_report_date,
)
from ._fundamentals_periods import (
    YFinanceMetricPeriod,
    latest_statement_date,
    select_metric,
)
from .endpoints import validate_yfinance_symbol

NON_EQUITY_QUOTE_TYPES = frozenset(
    {"ETF", "INDEX", "MUTUALFUND", "FUND", "ETN", "CURRENCY", "CRYPTOCURRENCY"}
)


@dataclass(frozen=True)
class YFinanceValuationSnapshot:
    """Snapshot of valuation ratios and market size."""

    trailing_pe: float | None = None
    forward_pe: float | None = None  # Calibrated FY1 institutional Forward P/E
    raw_forward_pe: float | None = None  # Provider-native raw value from info.get("forwardPE")
    forward_pe_source: str | None = None  # "FY1_CONSENSUS" | "RAW_FALLBACK" | None
    peg_ratio: float | None = None
    price_to_sales: float | None = None
    forward_eps: float | None = None  # Calibrated FY1 forward EPS (or raw if uncalibrated)
    raw_forward_eps: float | None = None  # Provider-native raw value from info.get("forwardEps")
    market_cap: float | None = None
    enterprise_value: float | None = None
    shares_outstanding: float | None = None
    currency: str | None = None
    quote_price: float | None = None
    quote_time: datetime.datetime | None = None
    quote_source: str | None = None
    forward_eps_period: str | None = None
    forward_eps_source: str | None = None
    eps_accounting_basis: str = "unknown"
    currency_comparability: str = "unknown"


@dataclass(frozen=True)
class YFinanceQuarterlyFinancials:
    """Quarterly financial metrics (latest quarter and prior year same quarter YoY)."""

    metric_periods: tuple[YFinanceMetricPeriod, ...] = field(default=(), kw_only=True)

    total_revenue_latest: float | None = None
    total_revenue_prev_year: float | None = None
    gross_profit_latest: float | None = None
    gross_profit_prev_year: float | None = None
    operating_income_latest: float | None = None
    operating_income_prev_year: float | None = None
    ebitda_latest: float | None = None
    ebitda_prev_year: float | None = None
    diluted_eps_latest: float | None = None
    diluted_eps_prev_year: float | None = None
    basic_eps_latest: float | None = None
    basic_eps_prev_year: float | None = None
    normalized_eps_latest: float | None = None
    normalized_eps_prev_year: float | None = None
    net_income_latest: float | None = None
    net_income_prev_year: float | None = None
    operating_cash_flow_latest: float | None = None
    operating_cash_flow_prev_year: float | None = None
    free_cash_flow_latest: float | None = None
    free_cash_flow_prev_year: float | None = None
    capital_expenditure_latest: float | None = None
    capital_expenditure_prev_year: float | None = None
    total_debt_latest: float | None = None
    total_debt_prev_year: float | None = None
    cash_latest: float | None = None
    cash_prev_year: float | None = None
    net_debt_latest: float | None = None
    net_debt_prev_year: float | None = None
    shareholders_equity_latest: float | None = None
    shareholders_equity_prev_year: float | None = None


@dataclass(frozen=True)
class YFinanceAnalystEstimates:
    """Analyst consensus estimates across horizons and Non-GAAP cross-validation."""

    revenue_est_current_q: float | None = None
    revenue_est_next_q: float | None = None
    revenue_est_current_y: float | None = None
    revenue_est_next_y: float | None = None
    eps_est_current_q: float | None = None
    eps_est_next_q: float | None = None
    eps_est_current_y: float | None = None  # Provider earnings_estimate 0y.avg
    eps_est_next_y: float | None = None  # Sell-side consensus +1y.avg
    eps_current_year: float | None = None  # Provider info.epsCurrentYear; basis unspecified
    gaap_diff_pct: float | None = None  # Legacy numeric divergence, not proof of GAAP basis
    accounting_basis_comparability: str = field(default="unknown", kw_only=True)
    eps_est_current_y_source: str = field(default="earnings_estimate.0y.avg", kw_only=True)
    eps_current_year_source: str = field(default="info.epsCurrentYear", kw_only=True)
    has_gaap_distortion: bool = False  # Legacy name: numeric divergence > 0.25, basis unknown


@dataclass(frozen=True)
class YFinanceSymbolFundamentals:
    """Consolidated fundamentals observation for a single symbol."""

    symbol: str
    report_date: datetime.date | None = None
    provider_report_date: datetime.date | None = field(default=None, kw_only=True)
    coverage_flags: tuple[str, ...] = field(default=(), kw_only=True)
    quote_type: str | None = None
    is_excluded: bool = False
    exclusion_reason: str | None = None
    revenue_growth_hint: float | None = None
    eps_growth_hint: float | None = None
    valuation: YFinanceValuationSnapshot = field(default_factory=YFinanceValuationSnapshot)
    financials: YFinanceQuarterlyFinancials = field(default_factory=YFinanceQuarterlyFinancials)
    estimates: YFinanceAnalystEstimates = field(default_factory=YFinanceAnalystEstimates)

    def to_dict(self) -> dict[str, Any]:
        """Flatten into a dictionary matching consumer schema."""
        out: dict[str, Any] = {
            "symbol": self.symbol,
            "report_date": self.report_date,
            "provider_report_date": self.provider_report_date,
            "coverage_flags": self.coverage_flags,
            "quote_type": self.quote_type,
            "is_excluded": self.is_excluded,
            "exclusion_reason": self.exclusion_reason,
            "revenue_growth_hint": self.revenue_growth_hint,
            "eps_growth_hint": self.eps_growth_hint,
        }
        if self.valuation:
            out.update(asdict(self.valuation))
        if self.financials:
            out.update(asdict(self.financials))
        if self.estimates:
            out.update(asdict(self.estimates))
        return out


@dataclass(frozen=True)
class YFinanceFundamentalsRequest:
    """Request contract for fundamentals."""

    symbols: tuple[str, ...]
    include_financials: bool = True
    include_valuation: bool = True
    include_estimates: bool = True
    retry_policy: RetryPolicy | None = None

    def __post_init__(self):
        if not self.symbols:
            raise ValueError("symbols tuple cannot be empty")
        validated = [validate_yfinance_symbol(s) for s in self.symbols]
        if len(set(validated)) != len(validated):
            raise ValueError(f"Duplicate symbols provided: {self.symbols}")
        object.__setattr__(self, "symbols", tuple(validated))


@dataclass(frozen=True)
class YFinanceFundamentalsResult:
    """Result contract for fundamentals."""

    records: MappingProxyType[str, YFinanceSymbolFundamentals]
    requested_symbols: tuple[str, ...]
    yfinance_version: str
    provenance: FetchProvenance | None = None

    def __post_init__(self):
        object.__setattr__(self, "requested_symbols", tuple(self.requested_symbols))
        if isinstance(self.records, dict):
            object.__setattr__(self, "records", MappingProxyType(dict(self.records)))

    def to_records(self) -> list[dict[str, Any]]:
        """Return list of flattened dictionaries."""
        return [rec.to_dict() for rec in self.records.values()]

    def to_dataframe(self) -> pd.DataFrame:
        """Return pandas DataFrame representation."""
        return pd.DataFrame(self.to_records())


def parse_symbol_fundamentals(
    symbol: str,
    info: dict[str, Any] | None,
    income_stmt: pd.DataFrame | None = None,
    balance_stmt: pd.DataFrame | None = None,
    cashflow_stmt: pd.DataFrame | None = None,
    rev_estimate_df: pd.DataFrame | None = None,
    eps_estimate_df: pd.DataFrame | None = None,
    fast_info: Any | None = None,
) -> YFinanceSymbolFundamentals:
    """Parse raw yfinance objects into typed YFinanceSymbolFundamentals."""
    info = info or {}
    quote_type = str(info.get("quoteType") or "").strip().upper() or None

    is_excluded = False
    exclusion_reason = None
    if quote_type in NON_EQUITY_QUOTE_TYPES:
        is_excluded = True
        exclusion_reason = f"non-equity quoteType: {quote_type}"

    # Extract valuation
    market_cap = None
    shares_outstanding = None
    if fast_info is not None:
        market_cap = _safe_float(getattr(fast_info, "market_cap", None))
        shares_outstanding = _safe_float(getattr(fast_info, "shares", None))

    if market_cap is None:
        market_cap = _safe_float(info.get("marketCap"))
    if shares_outstanding is None:
        shares_outstanding = _safe_float(info.get("sharesOutstanding"))

    currency = (
        str(info.get("currency") or info.get("financialCurrency") or "").strip().upper() or None
    )

    trailing_pe = _safe_float(info.get("trailingPE"))
    raw_forward_pe = _safe_float(info.get("forwardPE"))
    raw_forward_eps = _safe_float(info.get("forwardEps"))
    eps_current_year = _safe_float(info.get("epsCurrentYear"))

    dates = [
        d
        for stmt in (income_stmt, balance_stmt, cashflow_stmt)
        if (d := latest_statement_date(stmt)) is not None
    ]
    report_dt = max(dates) if dates else None
    metric_periods: list[YFinanceMetricPeriod] = []
    coverage_flags: list[str] = []
    provider_report_date = None
    mrq = info.get("mostRecentQuarter")
    try:
        if isinstance(mrq, (int, float)) and mrq > 0:
            provider_report_date = datetime.datetime.fromtimestamp(mrq, datetime.UTC).date()
        elif isinstance(mrq, str):
            provider_report_date = pd.Timestamp(mrq).date()
    except (ValueError, TypeError, OSError, OverflowError):
        pass
    if provider_report_date and provider_report_date != report_dt:
        coverage_flags.append("provider_report_date_differs")

    def metric_pair(
        metric: str, statement: str, stmt: pd.DataFrame | None, keys: list[str]
    ) -> tuple[float | None, float | None]:
        metadata, latest, previous = select_metric(metric, statement, stmt, keys, report_dt)
        metric_periods.append(metadata)
        if metadata.coverage != "present":
            coverage_flags.append(f"{metric}:{metadata.coverage}")
        return _safe_float(latest), _safe_float(previous)

    # All metrics share the actual report column; older values remain missing.
    rev_latest, rev_prev = metric_pair(
        "total_revenue",
        "income_statement",
        income_stmt,
        ["Total Revenue", "Operating Revenue", "Revenue"],
    )
    gp_latest, gp_prev = metric_pair(
        "gross_profit", "income_statement", income_stmt, ["Gross Profit"]
    )
    op_latest, op_prev = metric_pair(
        "operating_income", "income_statement", income_stmt, ["Operating Income"]
    )
    ebitda_latest, ebitda_prev = metric_pair("ebitda", "income_statement", income_stmt, ["EBITDA"])
    deps_latest, deps_prev = metric_pair(
        "diluted_eps", "income_statement", income_stmt, ["Diluted EPS"]
    )
    beps_latest, beps_prev = metric_pair(
        "basic_eps", "income_statement", income_stmt, ["Basic EPS"]
    )
    neps_latest, neps_prev = metric_pair(
        "normalized_eps",
        "income_statement",
        income_stmt,
        ["Normalized EPS", "Normalized Basic EPS"],
    )
    ni_latest, ni_prev = metric_pair(
        "net_income",
        "income_statement",
        income_stmt,
        ["Net Income", "Net Income Common Stockholders"],
    )

    cf_latest, cf_prev = metric_pair(
        "operating_cash_flow",
        "cash_flow",
        cashflow_stmt,
        [
            "Operating Cash Flow",
            "Cash Flow From Continuing Operating Activities",
            "Net Cash Flow From Operating Activities",
        ],
    )
    fcf_latest, fcf_prev = metric_pair(
        "free_cash_flow", "cash_flow", cashflow_stmt, ["Free Cash Flow"]
    )
    capex_latest, capex_prev = metric_pair(
        "capital_expenditure", "cash_flow", cashflow_stmt, ["Capital Expenditure"]
    )

    debt_latest, debt_prev = metric_pair(
        "total_debt", "balance_sheet", balance_stmt, ["Total Debt"]
    )
    cash_latest, cash_prev = metric_pair(
        "cash",
        "balance_sheet",
        balance_stmt,
        ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"],
    )
    net_debt_latest, net_debt_prev = metric_pair(
        "net_debt", "balance_sheet", balance_stmt, ["Net Debt"]
    )
    equity_latest, equity_prev = metric_pair(
        "shareholders_equity",
        "balance_sheet",
        balance_stmt,
        ["Stockholders Equity", "Total Equity Gross Minority Interest"],
    )

    financials = YFinanceQuarterlyFinancials(
        metric_periods=tuple(metric_periods),
        total_revenue_latest=rev_latest,
        total_revenue_prev_year=rev_prev,
        gross_profit_latest=gp_latest,
        gross_profit_prev_year=gp_prev,
        operating_income_latest=op_latest,
        operating_income_prev_year=op_prev,
        ebitda_latest=ebitda_latest,
        ebitda_prev_year=ebitda_prev,
        diluted_eps_latest=deps_latest,
        diluted_eps_prev_year=deps_prev,
        basic_eps_latest=beps_latest,
        basic_eps_prev_year=beps_prev,
        normalized_eps_latest=neps_latest,
        normalized_eps_prev_year=neps_prev,
        net_income_latest=ni_latest,
        net_income_prev_year=ni_prev,
        operating_cash_flow_latest=cf_latest,
        operating_cash_flow_prev_year=cf_prev,
        free_cash_flow_latest=fcf_latest,
        free_cash_flow_prev_year=fcf_prev,
        capital_expenditure_latest=capex_latest,
        capital_expenditure_prev_year=capex_prev,
        total_debt_latest=debt_latest,
        total_debt_prev_year=debt_prev,
        cash_latest=cash_latest,
        cash_prev_year=cash_prev,
        net_debt_latest=net_debt_latest,
        net_debt_prev_year=net_debt_prev,
        shareholders_equity_latest=equity_latest,
        shareholders_equity_prev_year=equity_prev,
    )

    # Extract estimates
    rcq, rnq, rcy, rny = extract_estimates_horizons(rev_estimate_df)
    ecq, enq, ecy, eny = extract_estimates_horizons(eps_estimate_df)
    eps_0y = ecy

    quote_price = None
    quote_source = None
    for key in ("currentPrice", "regularMarketPrice"):
        value = _safe_float(info.get(key))
        if value is not None and value > 0:
            quote_price, quote_source = value, f"info.{key}"
            break
    quote_time = None
    timestamp = _safe_float(info.get("regularMarketTime"))
    if quote_source == "info.regularMarketPrice" and timestamp is not None and timestamp > 0:
        try:
            quote_time = datetime.datetime.fromtimestamp(timestamp, datetime.UTC)
        except (ValueError, OverflowError, OSError):
            pass
    financial_currency = str(info.get("financialCurrency") or "").strip().upper()
    quote_currency = str(info.get("currency") or "").strip().upper()
    comparability = (
        "same_currency"
        if financial_currency and financial_currency == quote_currency
        else "currency_mismatch"
        if financial_currency and quote_currency
        else "unknown"
    )
    # Currency compatibility is necessary; accounting basis remains provider-unspecified.
    implied_price = quote_price if comparability == "same_currency" else None

    # Calibrate FY1 Forward P/E
    calibrated_fpe, calibrated_feps, fpe_source = calibrate_forward_pe(
        implied_price=implied_price,
        raw_forward_pe=raw_forward_pe,
        raw_forward_eps=raw_forward_eps,
        eps_0y=eps_0y,
    )

    # GAAP vs. Non-GAAP Distortion Detection
    gaap_diff_pct, has_gaap_distortion = detect_gaap_distortion(
        eps_0y=eps_0y,
        eps_current_year=eps_current_year,
    )

    valuation = YFinanceValuationSnapshot(
        trailing_pe=trailing_pe,
        forward_pe=calibrated_fpe,
        raw_forward_pe=raw_forward_pe,
        forward_pe_source=fpe_source,
        peg_ratio=_safe_float(info.get("pegRatio")),
        price_to_sales=_safe_float(info.get("priceToSalesTrailing12Months")),
        forward_eps=calibrated_feps,
        raw_forward_eps=raw_forward_eps,
        market_cap=market_cap,
        enterprise_value=_safe_float(info.get("enterpriseValue")),
        shares_outstanding=shares_outstanding,
        currency=currency,
        quote_price=quote_price,
        quote_time=quote_time,
        quote_source=quote_source,
        forward_eps_period="FY1" if fpe_source == "FY1_CONSENSUS" else None,
        forward_eps_source=(
            "earnings_estimate.0y.avg"
            if fpe_source == "FY1_CONSENSUS"
            else "info.forwardEps"
            if raw_forward_eps is not None
            else None
        ),
        currency_comparability=comparability,
    )

    estimates = YFinanceAnalystEstimates(
        revenue_est_current_q=rcq,
        revenue_est_next_q=rnq,
        revenue_est_current_y=rcy,
        revenue_est_next_y=rny,
        eps_est_current_q=ecq,
        eps_est_next_q=enq,
        eps_est_current_y=ecy,
        eps_est_next_y=eny,
        eps_current_year=eps_current_year,
        gaap_diff_pct=gaap_diff_pct,
        has_gaap_distortion=has_gaap_distortion,
    )

    return YFinanceSymbolFundamentals(
        symbol=symbol,
        report_date=report_dt,
        provider_report_date=provider_report_date,
        coverage_flags=tuple(coverage_flags),
        quote_type=quote_type,
        is_excluded=is_excluded,
        exclusion_reason=exclusion_reason,
        valuation=valuation,
        financials=financials,
        estimates=estimates,
    )


__all__ = [
    "NON_EQUITY_QUOTE_TYPES",
    "YFinanceAnalystEstimates",
    "YFinanceFundamentalsRequest",
    "YFinanceFundamentalsResult",
    "YFinanceMetricPeriod",
    "YFinanceQuarterlyFinancials",
    "YFinanceSymbolFundamentals",
    "YFinanceValuationSnapshot",
    "_safe_float",
    "calibrate_forward_pe",
    "derive_implied_price",
    "detect_gaap_distortion",
    "extract_estimates_horizons",
    "extract_metric_pair",
    "extract_quarterly_pair",
    "extract_report_date",
    "parse_symbol_fundamentals",
]

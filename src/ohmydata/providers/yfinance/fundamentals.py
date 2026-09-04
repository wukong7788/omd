"""Fundamentals, financial statements, valuation metrics, and analyst estimates models."""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any

import pandas as pd

from ohmydata.core.policy import RetryPolicy
from ohmydata.core.provenance import FetchProvenance

from .endpoints import validate_yfinance_symbol

NON_EQUITY_QUOTE_TYPES = frozenset(
    {"ETF", "INDEX", "MUTUALFUND", "FUND", "ETN", "CURRENCY", "CRYPTOCURRENCY"}
)


def _safe_float(val: Any) -> float | None:
    """Convert arbitrary numeric value to float or None."""
    try:
        if val is None or pd.isna(val):
            return None
        f = float(val)
        return f if pd.notna(f) else None
    except (ValueError, TypeError):
        return None


def extract_quarterly_pair(series: pd.Series | None) -> tuple[float | None, float | None]:
    """Return (latest_quarter, same_quarter_last_year) from a quarterly series."""
    if series is None or series.empty:
        return (None, None)
    cleaned = pd.to_numeric(series, errors="coerce").dropna()
    if len(cleaned) < 5:
        # If at least 1 quarter exists, return latest, but prev_year is None
        latest = _safe_float(cleaned.iloc[0]) if len(cleaned) >= 1 else None
        return (latest, None)
    return (_safe_float(cleaned.iloc[0]), _safe_float(cleaned.iloc[4]))


def extract_metric_pair(
    stmt: pd.DataFrame | None, candidate_keys: list[str]
) -> tuple[float | None, float | None]:
    """Search for metric rows across aliases and extract (latest, prev_year)."""
    if stmt is None or stmt.empty:
        return (None, None)
    # Search index names ignoring case and whitespace
    norm_index = {str(idx).strip().lower(): idx for idx in stmt.index}
    for candidate in candidate_keys:
        cand_key = candidate.strip().lower()
        if cand_key in norm_index:
            row_key = norm_index[cand_key]
            return extract_quarterly_pair(stmt.loc[row_key])
    return (None, None)


def extract_report_date(
    info: dict[str, Any] | None, income_stmt: pd.DataFrame | None
) -> datetime.date | None:
    """Extract report date from mostRecentQuarter or statement columns, rejecting epoch 0."""
    info = info or {}
    candidates: list[datetime.date] = []

    mrq = info.get("mostRecentQuarter")
    if mrq is not None:
        try:
            if isinstance(mrq, (int, float)) and mrq > 0:
                dt = datetime.datetime.fromtimestamp(mrq, tz=datetime.UTC).date()
                candidates.append(dt)
            elif isinstance(mrq, str):
                dt = pd.to_datetime(mrq).date()
                candidates.append(dt)
        except (ValueError, TypeError, OSError):
            pass

    if income_stmt is not None and not income_stmt.empty:
        for col in income_stmt.columns:
            try:
                dt = pd.to_datetime(col).date()
                candidates.append(dt)
                break
            except (ValueError, TypeError, OSError):
                pass

    for dt in candidates:
        # Must be after 1990-01-01 and not epoch 1970-01-01
        if dt.year >= 1990:
            return dt

    return None


@dataclass(frozen=True)
class YFinanceValuationSnapshot:
    """Snapshot of valuation ratios and market size."""

    trailing_pe: float | None = None
    forward_pe: float | None = None
    peg_ratio: float | None = None
    price_to_sales: float | None = None
    forward_eps: float | None = None
    market_cap: float | None = None
    enterprise_value: float | None = None
    shares_outstanding: float | None = None
    currency: str | None = None


@dataclass(frozen=True)
class YFinanceQuarterlyFinancials:
    """Quarterly financial metrics (latest quarter and prior year same quarter YoY)."""

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
    """Analyst consensus estimates across 4 horizons."""

    revenue_est_current_q: float | None = None
    revenue_est_next_q: float | None = None
    revenue_est_current_y: float | None = None
    revenue_est_next_y: float | None = None
    eps_est_current_q: float | None = None
    eps_est_next_q: float | None = None
    eps_est_current_y: float | None = None
    eps_est_next_y: float | None = None


@dataclass(frozen=True)
class YFinanceSymbolFundamentals:
    """Consolidated fundamentals observation for a single symbol."""

    symbol: str
    report_date: datetime.date | None = None
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


def extract_estimates_horizons(
    df_est: pd.DataFrame | None,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Extract (current_q, next_q, current_y, next_y) from estimate DataFrame."""
    if df_est is None or df_est.empty:
        return (None, None, None, None)

    # Clean index
    df_clean = df_est.copy()
    df_clean.index = [str(idx).strip().lower() for idx in df_clean.index]

    # Find avg / mean column
    target_col = None
    for col in df_clean.columns:
        c_low = str(col).lower()
        if "avg" in c_low or "mean" in c_low:
            target_col = col
            break
    if target_col is None and len(df_clean.columns) > 0:
        target_col = df_clean.columns[0]

    if target_col is None:
        return (None, None, None, None)

    series = pd.to_numeric(df_clean[target_col], errors="coerce")

    def _get_val(keys: list[str]) -> float | None:
        for k in keys:
            if k in series.index:
                val = series.loc[k]
                return _safe_float(val)
        return None

    cq = _get_val(["0q", "current quarter", "currentq"])
    nq = _get_val(["+1q", "next quarter", "nextq", "1q"])
    cy = _get_val(["0y", "current year", "currenty"])
    ny = _get_val(["+1y", "next year", "nexty", "1y"])
    return (cq, nq, cy, ny)


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

    valuation = YFinanceValuationSnapshot(
        trailing_pe=_safe_float(info.get("trailingPE")),
        forward_pe=_safe_float(info.get("forwardPE")),
        peg_ratio=_safe_float(info.get("pegRatio")),
        price_to_sales=_safe_float(info.get("priceToSalesTrailing12Months")),
        forward_eps=_safe_float(info.get("forwardEps")),
        market_cap=market_cap,
        enterprise_value=_safe_float(info.get("enterpriseValue")),
        shares_outstanding=shares_outstanding,
        currency=currency,
    )

    # Extract statements YoY pairs
    rev_latest, rev_prev = extract_metric_pair(
        income_stmt, ["Total Revenue", "Operating Revenue", "Revenue"]
    )
    gp_latest, gp_prev = extract_metric_pair(income_stmt, ["Gross Profit"])
    op_latest, op_prev = extract_metric_pair(income_stmt, ["Operating Income", "EBIT"])
    ebitda_latest, ebitda_prev = extract_metric_pair(income_stmt, ["EBITDA"])
    deps_latest, deps_prev = extract_metric_pair(income_stmt, ["Diluted EPS"])
    beps_latest, beps_prev = extract_metric_pair(income_stmt, ["Basic EPS"])
    neps_latest, neps_prev = extract_metric_pair(
        income_stmt, ["Normalized EPS", "Normalized Basic EPS"]
    )
    ni_latest, ni_prev = extract_metric_pair(
        income_stmt, ["Net Income", "Net Income Common Stockholders"]
    )

    cf_latest, cf_prev = extract_metric_pair(
        cashflow_stmt,
        [
            "Operating Cash Flow",
            "Cash Flow From Continuing Operating Activities",
            "Net Cash Flow From Operating Activities",
        ],
    )
    fcf_latest, fcf_prev = extract_metric_pair(cashflow_stmt, ["Free Cash Flow"])
    capex_latest, capex_prev = extract_metric_pair(
        cashflow_stmt, ["Capital Expenditure", "Capital Expenditures"]
    )

    debt_latest, debt_prev = extract_metric_pair(balance_stmt, ["Total Debt"])
    cash_latest, cash_prev = extract_metric_pair(
        balance_stmt,
        ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments", "Cash"],
    )
    net_debt_latest, net_debt_prev = extract_metric_pair(balance_stmt, ["Net Debt"])
    equity_latest, equity_prev = extract_metric_pair(
        balance_stmt,
        ["Stockholders Equity", "Total Stockholder Equity", "Common Stock Equity"],
    )

    financials = YFinanceQuarterlyFinancials(
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

    estimates = YFinanceAnalystEstimates(
        revenue_est_current_q=rcq,
        revenue_est_next_q=rnq,
        revenue_est_current_y=rcy,
        revenue_est_next_y=rny,
        eps_est_current_q=ecq,
        eps_est_next_q=enq,
        eps_est_current_y=ecy,
        eps_est_next_y=eny,
    )

    report_dt = extract_report_date(info, income_stmt)

    return YFinanceSymbolFundamentals(
        symbol=symbol,
        report_date=report_dt,
        quote_type=quote_type,
        is_excluded=is_excluded,
        exclusion_reason=exclusion_reason,
        revenue_growth_hint=_safe_float(info.get("revenueGrowth")),
        eps_growth_hint=_safe_float(info.get("earningsGrowth")),
        valuation=valuation,
        financials=financials,
        estimates=estimates,
    )

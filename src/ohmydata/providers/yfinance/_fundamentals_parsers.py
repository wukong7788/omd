"""Internal helper functions for parsing financial statements, valuation, and analyst estimates."""

from __future__ import annotations

import datetime
from typing import Any

import pandas as pd


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


def derive_implied_price(
    raw_forward_pe: float | None,
    raw_forward_eps: float | None,
    trailing_pe: float | None,
    deps_latest: float | None,
    market_cap: float | None,
    shares_outstanding: float | None,
) -> float | None:
    """Derive implied market price defensively across priority cascade."""
    if (
        raw_forward_pe is not None
        and raw_forward_eps is not None
        and raw_forward_pe > 0
        and raw_forward_eps > 0
    ):
        return raw_forward_pe * raw_forward_eps
    if trailing_pe is not None and deps_latest is not None and trailing_pe > 0 and deps_latest > 0:
        return trailing_pe * deps_latest
    if market_cap is not None and shares_outstanding is not None and shares_outstanding > 0:
        return market_cap / shares_outstanding
    return None


def calibrate_forward_pe(
    implied_price: float | None,
    raw_forward_pe: float | None,
    raw_forward_eps: float | None,
    eps_0y: float | None,
) -> tuple[float | None, float | None, str | None]:
    """Calibrate institutional FY1 Forward P/E against sell-side consensus with ADR protection.

    Returns:
        tuple of (calibrated_forward_pe, calibrated_forward_eps, forward_pe_source)
    """
    calibrated_fpe = raw_forward_pe
    calibrated_feps = raw_forward_eps
    fpe_source = "RAW_FALLBACK" if raw_forward_pe is not None else None

    if implied_price is not None and eps_0y is not None and eps_0y > 0:
        is_mismatched = False
        if raw_forward_eps is not None and raw_forward_eps > 0:
            ratio = eps_0y / raw_forward_eps
            if ratio > 3.0 or ratio < 0.25:
                is_mismatched = True

        if not is_mismatched:
            calibrated_fpe = round(implied_price / eps_0y, 4)
            calibrated_feps = round(eps_0y, 4)
            fpe_source = "FY1_CONSENSUS"

    return (calibrated_fpe, calibrated_feps, fpe_source)


def detect_gaap_distortion(
    eps_0y: float | None,
    eps_current_year: float | None,
) -> tuple[float | None, bool]:
    """Detect relative GAAP vs Non-GAAP consensus divergence (e.g. spin-offs, M&A windfalls).

    Returns:
        tuple of (gaap_diff_pct, has_gaap_distortion)
    """
    gaap_diff_pct = None
    has_gaap_distortion = False

    if eps_0y is not None and eps_current_year is not None and eps_0y > 0 and eps_current_year > 0:
        denom = min(eps_0y, eps_current_year)
        if denom > 0:
            gap = abs(eps_0y - eps_current_year) / denom
            gaap_diff_pct = round(gap, 4)
            if gap > 0.25:
                has_gaap_distortion = True

    return (gaap_diff_pct, has_gaap_distortion)

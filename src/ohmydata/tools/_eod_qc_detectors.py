"""Internal vectorized pandas detectors for EOD market-data quality control."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from ._eod_qc_prep import (
    DIVIDEND_ACTIONS,
    OHLC_COLUMNS,
    PRICE_COLUMNS,
    SPINOFF_ACTIONS,
    SPLIT_ACTIONS,
    QualityCategory,
    QualitySeverity,
    make_finding,
    norm_date_str,
)


def detect_non_positive_prices_work(work: pd.DataFrame, finding_cls: Any) -> list[Any]:
    cols = [c for c in PRICE_COLUMNS if c in work.columns]
    if work.empty or not cols:
        return []
    arr = work[cols].to_numpy(dtype="float64")
    bad_matrix = (~np.isfinite(arr)) | (arr <= 0.0)
    findings: list[Any] = []
    for i in np.flatnonzero(bad_matrix.any(axis=1)):
        bad_fields = {
            cols[j]: (None if np.isnan(arr[i, j]) else float(arr[i, j]))
            for j in range(len(cols))
            if bad_matrix[i, j]
        }
        findings.append(
            make_finding(
                finding_cls,
                str(work.at[i, "_symbol"]),
                str(work.at[i, "_date_str"]),
                QualityCategory.NON_POSITIVE_PRICE,
                QualitySeverity.ERROR,
                f"Non-positive or non-finite price in {sorted(bad_fields)}",
                {"bad_fields": bad_fields},
            )
        )
    return findings


def detect_ohlc_violations_work(
    work: pd.DataFrame, ulp_rel_tol: float, finding_cls: Any
) -> list[Any]:
    if work.empty or not set(OHLC_COLUMNS).issubset(work.columns):
        return []
    o, h, low, c = (work[k].to_numpy(dtype="float64") for k in OHLC_COLUMNS)
    finite = np.isfinite(o) & np.isfinite(h) & np.isfinite(low) & np.isfinite(c)
    tol = np.abs(c) * ulp_rel_tol
    hl_bad, hi_bad, lo_bad = (
        finite & (h < low),
        finite & (h < np.maximum(o, c) - tol),
        finite & (low > np.minimum(o, c) + tol),
    )
    findings: list[Any] = []
    for i in np.flatnonzero(hl_bad | hi_bad | lo_bad):
        rules = tuple(
            n
            for n, flag in (
                ("high_lt_low", hl_bad[i]),
                ("high_lt_max_oc", hi_bad[i]),
                ("low_gt_min_oc", lo_bad[i]),
            )
            if flag
        )
        metrics = {
            "open": float(o[i]),
            "high": float(h[i]),
            "low": float(low[i]),
            "close": float(c[i]),
            "rules": rules,
        }
        findings.append(
            make_finding(
                finding_cls,
                str(work.at[i, "_symbol"]),
                str(work.at[i, "_date_str"]),
                QualityCategory.OHLC_GEOMETRY,
                QualitySeverity.ERROR,
                f"OHLC geometry violation: {', '.join(rules)}",
                metrics,
            )
        )
    return findings


def _merge_nearest_action(
    cand: pd.DataFrame, action_df: pd.DataFrame, col_name: str, tol: pd.Timedelta
) -> pd.DataFrame:
    if action_df.empty:
        cand[col_name] = pd.NaT
        return cand
    ref = action_df[["_symbol", "_dt"]].rename(columns={"_dt": col_name})
    return pd.merge_asof(
        cand,
        ref,
        left_on="_dt",
        right_on=col_name,
        by="_symbol",
        direction="nearest",
        tolerance=tol,
    )


def detect_suspect_missing_splits_work(
    work: pd.DataFrame, act: pd.DataFrame, jump_threshold: float, finding_cls: Any
) -> list[Any]:
    if work.empty or "close" not in work.columns:
        return []
    prev_close = work.groupby("_symbol", sort=False)["close"].shift(1)
    valid_prev = prev_close > 0.0
    close_ret = np.where(valid_prev, work["close"] / prev_close - 1.0, np.nan)
    overnight_gap = (
        np.where(valid_prev, work["open"] / prev_close - 1.0, np.nan)
        if "open" in work.columns
        else np.nan
    )
    work = work.assign(_prev_close=prev_close, _close_ret=close_ret, _overnight_gap=overnight_gap)
    cand = work[
        (work["_close_ret"].abs() >= jump_threshold)
        | (work["_overnight_gap"].abs() >= jump_threshold)
    ].copy()
    if cand.empty:
        return []

    cand.sort_values(["_dt", "_symbol"], kind="mergesort", inplace=True)
    tol = pd.Timedelta(days=3)
    splits = act[act["_action"].isin(SPLIT_ACTIONS) & (act["_value"].isna() | (act["_value"] > 0))]
    cand = _merge_nearest_action(cand, splits, "_split_dt", tol)
    cand = cand[cand["_split_dt"].isna()].copy()
    if cand.empty:
        return []

    spinoffs = act[act["_action"].isin(SPINOFF_ACTIONS)]
    cand = _merge_nearest_action(cand, spinoffs, "_spinoff_dt", tol)
    cand.sort_values(["_symbol", "_dt"], kind="mergesort", inplace=True)

    findings: list[Any] = []
    cols = (
        cand["_symbol"],
        cand["_date_str"],
        cand["_close_ret"],
        cand["_overnight_gap"],
        cand["_prev_close"],
        cand["close"],
        cand["_spinoff_dt"],
    )
    for sym, d_str, cr_v, og_v, pc_v, c_v, sp_dt in zip(*cols, strict=True):
        has_spin = pd.notna(sp_dt)
        cat = (
            QualityCategory.SPINOFF_BASIS_DIFFERENCE
            if has_spin
            else QualityCategory.SUSPECT_MISSING_SPLIT
        )
        sev = QualitySeverity.INFO if has_spin else QualitySeverity.ERROR
        msg = (
            "Large price jump coincides with +/-3d spinoff/spinoffdividend action"
            if has_spin
            else "Large price jump without +/-3d split action"
        )
        metrics = {
            "close_ret": None if pd.isna(cr_v) else float(cr_v),
            "overnight_gap": None if pd.isna(og_v) else float(og_v),
            "prev_close": float(pc_v),
            "close": float(c_v),
            "spinoff_date": pd.Timestamp(sp_dt).strftime("%Y-%m-%d") if has_spin else None,
        }
        findings.append(make_finding(finding_cls, str(sym), str(d_str), cat, sev, msg, metrics))
    return findings


def detect_oscillating_currency_jumps_work(
    work: pd.DataFrame, jump_threshold: float, max_gap_days: int, finding_cls: Any
) -> list[Any]:
    if work.empty or "close" not in work.columns:
        return []
    prev_close = work.groupby("_symbol", sort=False)["close"].shift(1)
    valid = (prev_close > 0.0) & (work["close"] > 0.0)
    work = work.assign(
        _ret=np.where(valid, work["close"] / prev_close - 1.0, np.nan),
        _log_ret=np.where(valid, np.log(work["close"] / prev_close), np.nan),
    )
    jumps = work[work["_ret"].abs() >= (1.0 - 1.0 / (1.0 + jump_threshold))].copy()
    if jumps.empty:
        return []

    jumps["_prev_dt"] = jumps.groupby("_symbol", sort=False)["_dt"].shift(1)
    jumps["_prev_ret"] = jumps.groupby("_symbol", sort=False)["_ret"].shift(1)
    jumps["_prev_log_ret"] = jumps.groupby("_symbol", sort=False)["_log_ret"].shift(1)
    jumps["_gap_days"] = (jumps["_dt"] - jumps["_prev_dt"]).dt.days
    a1, a2 = jumps["_prev_log_ret"].abs(), jumps["_log_ret"].abs()
    log_ratio = np.minimum(a1, a2) / np.maximum(a1, a2)
    max_abs_ret = np.maximum(jumps["_prev_ret"].abs(), jumps["_ret"].abs())
    min_abs_ret = np.minimum(jumps["_prev_ret"].abs(), jumps["_ret"].abs())
    is_osc = (
        jumps["_prev_dt"].notna()
        & (jumps["_gap_days"] <= max_gap_days)
        & ((jumps["_prev_log_ret"] * jumps["_log_ret"]) < 0.0)
        & (max_abs_ret >= jump_threshold)
        & (log_ratio >= 0.50)
    )
    flagged = jumps[is_osc].copy()
    flagged["_log_ratio"] = log_ratio[is_osc]
    flagged["_min_abs_ret"] = min_abs_ret[is_osc]

    findings: list[Any] = []
    cols = (
        flagged["_symbol"],
        flagged["_date_str"],
        flagged["_prev_dt"],
        flagged["_prev_ret"],
        flagged["_ret"],
        flagged["_gap_days"],
        flagged["_log_ratio"],
        flagged["_min_abs_ret"],
    )
    for sym, d_str, p_dt, p_ret, c_ret, gap_d, ratio, min_r in zip(*cols, strict=True):
        sev = (
            QualitySeverity.ERROR
            if (float(min_r) >= 0.25 and float(ratio) >= 0.75)
            else QualitySeverity.WARNING
        )
        msg = (
            f"Reversing price jumps ({float(p_ret):+.2%} then {float(c_ret):+.2%}) "
            f"within {int(gap_d)}d suggest currency/unit mixing"
        )
        metrics = {
            "first_date": pd.Timestamp(p_dt).strftime("%Y-%m-%d"),
            "first_ret": float(p_ret),
            "second_ret": float(c_ret),
            "gap_days": int(gap_d),
            "log_magnitude_ratio": float(ratio),
        }
        findings.append(
            make_finding(
                finding_cls,
                str(sym),
                str(d_str),
                QualityCategory.OSCILLATING_CURRENCY_JUMP,
                sev,
                msg,
                metrics,
            )
        )
    return findings


def detect_reverse_split_dividend_mismatch_work(
    work: pd.DataFrame, act: pd.DataFrame, tolerance: float, finding_cls: Any
) -> list[Any]:
    if work.empty or act.empty or "close" not in work.columns or "adj_close" not in work.columns:
        return []
    rev = act[act["_action"].isin(SPLIT_ACTIONS) & (act["_value"] > 0.0) & (act["_value"] < 1.0)]
    if rev.empty:
        return []
    work = work.merge(
        rev.groupby("_symbol")["_dt"].max().rename("_rev_split_dt"),
        left_on="_symbol",
        right_index=True,
        how="inner",
    )
    if work.empty:
        return []
    divs = (
        act[act["_action"].isin(DIVIDEND_ACTIONS) & act["_value"].notna() & (act["_value"] > 0.0)]
        .groupby(["_symbol", "_dt"])["_value"]
        .sum()
        .rename("_div")
        .reset_index()
    )
    work = work.merge(divs, on=["_symbol", "_dt"], how="left")
    work["_div"] = work["_div"].fillna(0.0)
    work["_prev_close"] = work.groupby("_symbol", sort=False)["close"].shift(1)
    work["_prev_adj"] = work.groupby("_symbol", sort=False)["adj_close"].shift(1)
    sub = work[
        (work["_dt"] < work["_rev_split_dt"])
        & (work["_prev_close"] > 0.0)
        & (work["close"] > 0.0)
        & (work["_prev_adj"] > 0.0)
        & (work["adj_close"] > 0.0)
    ].copy()
    if sub.empty:
        return []

    adj_ret = sub["adj_close"] / sub["_prev_adj"] - 1.0
    denom = sub["_prev_close"] - sub["_div"]
    div_ge_close = (sub["_div"] > 0.0) & (denom <= 0.0)
    expected_adj_ret = np.where(denom > 0.0, sub["close"] / denom - 1.0, np.nan)
    ret_diff = (adj_ret - expected_adj_ret).abs()
    bad = div_ge_close | (ret_diff > tolerance)
    if not bad.any():
        return []

    bad_sub = sub[bad]
    cols = (
        bad_sub["_symbol"],
        bad_sub["_date_str"],
        bad_sub["_rev_split_dt"],
        bad_sub["_div"],
        bad_sub["_prev_close"],
        adj_ret[bad],
        expected_adj_ret[bad],
        ret_diff[bad],
        div_ge_close[bad],
    )
    return [
        make_finding(
            finding_cls,
            str(sym),
            str(d_str),
            QualityCategory.REVERSE_SPLIT_DIVIDEND_MISMATCH,
            QualitySeverity.ERROR,
            "Pre-reverse-split adj_close return diverges from close/dividend implied return",
            {
                "reverse_split_date": pd.Timestamp(rev_dt).strftime("%Y-%m-%d"),
                "dividend": float(div_v),
                "prev_close": float(pc_v),
                "adj_ret": float(a_ret),
                "expected_adj_ret": None if pd.isna(exp_ret) else float(exp_ret),
                "ret_diff": None if pd.isna(r_diff) else float(r_diff),
                "dividend_ge_prev_close": bool(d_ge_c),
            },
        )
        for sym, d_str, rev_dt, div_v, pc_v, a_ret, exp_ret, r_diff, d_ge_c in zip(
            *cols, strict=True
        )
    ]


def detect_trailing_staleness_work(
    work: pd.DataFrame,
    expected_latest_date: str | date | datetime | None,
    max_lag_days: int,
    active_symbols: Iterable[str] | None,
    finding_cls: Any,
) -> list[Any]:
    if expected_latest_date is None:
        return []
    exp_dt = pd.to_datetime(norm_date_str(expected_latest_date))
    exp_str = exp_dt.strftime("%Y-%m-%d")
    max_dates = (
        {str(k): pd.Timestamp(v) for k, v in work.groupby("_symbol")["_dt"].max().items()}
        if not work.empty
        else {}
    )
    symbols = (
        sorted({str(s) for s in active_symbols})
        if active_symbols is not None
        else sorted(max_dates.keys())
    )
    findings: list[Any] = []
    for sym in symbols:
        latest = max_dates.get(sym)
        if latest is None:
            metrics: dict[str, Any] = {
                "expected_latest_date": exp_str,
                "actual_latest_date": None,
                "lag_days": None,
            }
            findings.append(
                make_finding(
                    finding_cls,
                    sym,
                    exp_str,
                    QualityCategory.TRAILING_STALENESS,
                    QualitySeverity.WARNING,
                    f"Active symbol has no bars through expected latest date {exp_str}",
                    metrics,
                )
            )
            continue
        lag = int((exp_dt - latest).days)
        if lag > max_lag_days:
            latest_str = latest.strftime("%Y-%m-%d")
            metrics = {
                "expected_latest_date": exp_str,
                "actual_latest_date": latest_str,
                "lag_days": lag,
                "max_lag_days": max_lag_days,
            }
            findings.append(
                make_finding(
                    finding_cls,
                    sym,
                    latest_str,
                    QualityCategory.TRAILING_STALENESS,
                    QualitySeverity.WARNING,
                    f"Latest bar {latest_str} lags expected {exp_str} by {lag} days",
                    metrics,
                )
            )
    return findings


def detect_zero_volume_work(work: pd.DataFrame, finding_cls: Any) -> list[Any]:
    if work.empty or "volume" not in work.columns:
        return []
    vol = work["volume"].to_numpy(dtype="float64")
    findings: list[Any] = []
    for i in np.flatnonzero((~np.isfinite(vol)) | (vol <= 0.0)):
        v = vol[i]
        is_invalid = (not np.isfinite(v)) or (v < 0.0)
        findings.append(
            make_finding(
                finding_cls,
                str(work.at[i, "_symbol"]),
                str(work.at[i, "_date_str"]),
                QualityCategory.ZERO_VOLUME,
                QualitySeverity.WARNING if is_invalid else QualitySeverity.INFO,
                "Negative or non-finite volume" if is_invalid else "Zero trading volume",
                {"volume": None if np.isnan(v) else float(v)},
            )
        )
    return findings

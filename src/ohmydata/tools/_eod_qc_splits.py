"""Vectorized split, spinoff, and distribution ex-date jump detector for EOD QC."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from ._eod_qc_prep import (
    DISTRIBUTION_ACTIONS,
    DIVIDEND_ACTIONS,
    SPINOFF_ACTIONS,
    SPLIT_ACTIONS,
    QualityCategory,
    QualitySeverity,
    make_finding,
)

_DISTRIBUTION_UNADJUSTED_BASES = frozenset(
    {"SPLIT_ADJUSTED_WITH_ADJ_CLOSE", "UNADJUSTED_WITH_ADJ_CLOSE"}
)


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


def _merge_exact_date_amount(
    cand: pd.DataFrame, act: pd.DataFrame, action_names: frozenset[str], col_name: str
) -> pd.DataFrame:
    if act.empty:
        cand[col_name] = 0.0
        return cand
    sub = act[
        act["_action"].isin(action_names)
        & act["_value"].notna()
        & np.isfinite(act["_value"])
        & (act["_value"] > 0.0)
    ]
    if sub.empty:
        cand[col_name] = 0.0
        return cand
    agg = (
        sub.groupby(["_symbol", "_dt"], as_index=False)["_value"]
        .sum()
        .rename(columns={"_value": col_name})
    )
    out = cand.merge(agg, on=["_symbol", "_dt"], how="left")
    out[col_name] = out[col_name].fillna(0.0)
    return out


def _opt_float(val: Any) -> float | None:
    if val is None or pd.isna(val):
        return None
    f = float(val)
    return f if math.isfinite(f) else None


def detect_suspect_missing_splits_work(
    work: pd.DataFrame,
    act: pd.DataFrame,
    jump_threshold: float,
    finding_cls: Any,
    *,
    adjustment_basis: Any = None,
    error_jump_threshold: float | None = None,
) -> list[Any]:
    """Flag >= jump_threshold price jumps, distinguishing spinoffs and distribution ex-dates."""
    if not math.isfinite(jump_threshold) or jump_threshold <= 0.0:
        raise ValueError(f"jump_threshold must be finite and > 0, got {jump_threshold!r}")
    eff_error_threshold = (
        max(jump_threshold, 0.35) if error_jump_threshold is None else error_jump_threshold
    )
    if not math.isfinite(eff_error_threshold) or eff_error_threshold < jump_threshold:
        raise ValueError(
            "error_jump_threshold must be finite and >= jump_threshold, got "
            f"{error_jump_threshold!r} (jump_threshold={jump_threshold!r})"
        )

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
    if "adj_close" in work.columns:
        prev_adj = work.groupby("_symbol", sort=False)["adj_close"].shift(1)
        valid_adj = (prev_adj > 0.0) & (work["adj_close"] > 0.0)
        adj_close_ret = np.where(valid_adj, work["adj_close"] / prev_adj - 1.0, np.nan)
    else:
        adj_close_ret = np.nan

    work = work.assign(
        _prev_close=prev_close,
        _close_ret=close_ret,
        _overnight_gap=overnight_gap,
        _adj_close_ret=adj_close_ret,
    )
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
    cand = _merge_exact_date_amount(cand, act, DIVIDEND_ACTIONS, "_div_amt")
    cand = _merge_exact_date_amount(cand, act, DISTRIBUTION_ACTIONS, "_dist_amt")
    cand.sort_values(["_symbol", "_dt"], kind="mergesort", inplace=True)

    basis_val = (
        adjustment_basis.value
        if hasattr(adjustment_basis, "value")
        else (
            str(adjustment_basis)
            if adjustment_basis is not None
            else "SPLIT_ADJUSTED_WITH_ADJ_CLOSE"
        )
    )
    is_dist_unadjusted_basis = basis_val in _DISTRIBUTION_UNADJUSTED_BASES
    open_series = cand["open"] if "open" in cand.columns else pd.Series(np.nan, index=cand.index)

    findings: list[Any] = []
    cols = (
        cand["_symbol"],
        cand["_date_str"],
        cand["_close_ret"],
        cand["_overnight_gap"],
        cand["_prev_close"],
        cand["close"],
        open_series,
        cand["_adj_close_ret"],
        cand["_div_amt"],
        cand["_dist_amt"],
        cand["_spinoff_dt"],
    )
    for (
        sym,
        d_str,
        cr_v,
        og_v,
        pc_v,
        c_v,
        o_v,
        adj_v,
        div_v,
        dist_v,
        sp_dt,
    ) in zip(*cols, strict=True):
        has_spin = pd.notna(sp_dt)
        cr = _opt_float(cr_v)
        og = _opt_float(og_v)
        pc = float(pc_v)
        close_f = float(c_v)
        open_f = _opt_float(o_v)
        adj_ret = _opt_float(adj_v)
        div_amt = float(div_v)
        dist_amt = float(dist_v)

        ex_dist_ret = (
            float(close_f / (pc - dist_amt) - 1.0) if (dist_amt > 0.0 and pc > dist_amt) else None
        )
        ex_dist_gap = (
            float(open_f / (pc - dist_amt) - 1.0)
            if (dist_amt > 0.0 and pc > dist_amt and open_f is not None)
            else None
        )
        close_adj_ret_diff = (
            float(cr - adj_ret) if (cr is not None and adj_ret is not None) else None
        )

        confirmed_by_action: bool | None = None
        if has_spin:
            cat = QualityCategory.SPINOFF_BASIS_DIFFERENCE
            sev = QualitySeverity.INFO
            msg = "Large price jump coincides with +/-3d spinoff/spinoffdividend action"
        elif (
            is_dist_unadjusted_basis
            and dist_amt > 0.0
            and ex_dist_ret is not None
            and abs(ex_dist_ret) < jump_threshold
            and (ex_dist_gap is None or abs(ex_dist_gap) < jump_threshold)
            and (adj_ret is None or abs(adj_ret) < jump_threshold)
        ):
            cat = QualityCategory.DISTRIBUTION_EX_DATE
            sev = QualitySeverity.INFO
            confirmed_by_action = True
            msg = "Large raw price drop explained by distribution ex-date"
        elif (
            is_dist_unadjusted_basis
            and adj_ret is not None
            and abs(adj_ret) < jump_threshold
            and (
                (cr is not None and cr <= -jump_threshold)
                or (og is not None and og <= -jump_threshold and (cr is None or cr < 0.0))
            )
        ):
            cat = QualityCategory.DISTRIBUTION_EX_DATE
            sev = QualitySeverity.INFO
            confirmed_by_action = False
            msg = "Large downward raw price jump with smooth adj_close return (inferred distribution ex-date)"
        else:
            cat = QualityCategory.SUSPECT_MISSING_SPLIT
            max_abs_jump = max(abs(v) for v in (cr, og) if v is not None)
            sev = (
                QualitySeverity.ERROR
                if max_abs_jump >= eff_error_threshold
                else QualitySeverity.WARNING
            )
            msg = "Large price jump without +/-3d split action"

        metrics = {
            "close_ret": cr,
            "overnight_gap": og,
            "prev_close": pc,
            "close": close_f,
            "adj_close_ret": adj_ret,
            "dividend_value": div_amt,
            "distribution_amount": dist_amt,
            "dividend_adjusted_ret": ex_dist_ret,
            "close_adj_ret_diff": close_adj_ret_diff,
            "distribution_confirmed_by_action": confirmed_by_action,
            "spinoff_date": pd.Timestamp(sp_dt).strftime("%Y-%m-%d") if has_spin else None,
        }
        findings.append(make_finding(finding_cls, str(sym), str(d_str), cat, sev, msg, metrics))
    return findings

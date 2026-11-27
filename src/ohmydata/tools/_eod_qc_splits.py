"""Vectorized Polars split, spinoff, and distribution ex-date jump detector for EOD QC."""

from __future__ import annotations

import math
from typing import Any

from ._eod_qc_prep import (
    DISTRIBUTION_ACTIONS,
    DIVIDEND_ACTIONS,
    SPINOFF_ACTIONS,
    SPLIT_ACTIONS,
    QualityCategory,
    QualitySeverity,
    make_finding,
    opt_float,
    require_polars,
)

_DISTRIBUTION_UNADJUSTED_BASES = frozenset(
    {"SPLIT_ADJUSTED_WITH_ADJ_CLOSE", "UNADJUSTED_WITH_ADJ_CLOSE"}
)


def _merge_nearest_action(cand: Any, action_df: Any, col_name: str, pl_mod: Any) -> Any:
    if action_df.is_empty():
        return cand.with_columns(pl_mod.lit(None, dtype=pl_mod.Date).alias(col_name))
    ref = action_df.select(["_symbol", "_dt", pl_mod.col("_dt").alias(col_name)]).sort(
        ["_symbol", "_dt"]
    )
    return cand.join_asof(
        ref,
        on="_dt",
        by="_symbol",
        strategy="nearest",
        tolerance="3d",
        check_sortedness=False,
    )


def _merge_exact_date_amount(
    cand: Any, act: Any, action_names: frozenset[str], col_name: str, pl_mod: Any
) -> Any:
    if act.is_empty():
        return cand.with_columns(pl_mod.lit(0.0, dtype=pl_mod.Float64).alias(col_name))
    sub = act.filter(
        pl_mod.col("_action").is_in(sorted(action_names))
        & pl_mod.col("_value").is_not_null()
        & pl_mod.col("_value").is_finite()
        & (pl_mod.col("_value") > 0.0)
    )
    if sub.is_empty():
        return cand.with_columns(pl_mod.lit(0.0, dtype=pl_mod.Float64).alias(col_name))
    agg = sub.group_by(["_symbol", "_dt"]).agg(pl_mod.col("_value").sum().alias(col_name))
    return cand.join(agg, on=["_symbol", "_dt"], how="left").with_columns(
        pl_mod.col(col_name).fill_null(0.0)
    )


def detect_suspect_missing_splits_work(
    work: Any,
    act: Any,
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

    if work.is_empty() or "close" not in work.columns:
        return []

    pl_mod = require_polars()
    prev_close = pl_mod.col("close").shift(1).over("_symbol")
    valid_prev = (
        pl_mod.col("_prev_close").is_not_null()
        & pl_mod.col("_prev_close").is_finite()
        & (pl_mod.col("_prev_close") > 0.0)
    )
    close_finite = pl_mod.col("close").is_not_null() & pl_mod.col("close").is_finite()

    exprs = [prev_close.alias("_prev_close")]
    enriched = work.with_columns(exprs)

    ret_exprs = [
        pl_mod.when(valid_prev & close_finite)
        .then(pl_mod.col("close") / pl_mod.col("_prev_close") - 1.0)
        .otherwise(None)
        .alias("_close_ret")
    ]
    if "open" in enriched.columns:
        open_finite = pl_mod.col("open").is_not_null() & pl_mod.col("open").is_finite()
        ret_exprs.append(
            pl_mod.when(valid_prev & open_finite)
            .then(pl_mod.col("open") / pl_mod.col("_prev_close") - 1.0)
            .otherwise(None)
            .alias("_overnight_gap")
        )
    else:
        ret_exprs.append(pl_mod.lit(None, dtype=pl_mod.Float64).alias("_overnight_gap"))

    if "adj_close" in enriched.columns:
        enriched = enriched.with_columns(
            pl_mod.col("adj_close").shift(1).over("_symbol").alias("_prev_adj")
        )
        valid_adj = (
            pl_mod.col("_prev_adj").is_not_null()
            & pl_mod.col("_prev_adj").is_finite()
            & (pl_mod.col("_prev_adj") > 0.0)
            & pl_mod.col("adj_close").is_not_null()
            & pl_mod.col("adj_close").is_finite()
            & (pl_mod.col("adj_close") > 0.0)
        )
        ret_exprs.append(
            pl_mod.when(valid_adj)
            .then(pl_mod.col("adj_close") / pl_mod.col("_prev_adj") - 1.0)
            .otherwise(None)
            .alias("_adj_close_ret")
        )
    else:
        ret_exprs.append(pl_mod.lit(None, dtype=pl_mod.Float64).alias("_adj_close_ret"))

    enriched = enriched.with_columns(ret_exprs)

    cand_mask = (pl_mod.col("_close_ret").abs() >= jump_threshold).fill_null(False) | (
        pl_mod.col("_overnight_gap").abs() >= jump_threshold
    ).fill_null(False)
    cand = enriched.filter(cand_mask).sort(["_symbol", "_dt"], maintain_order=True)
    if cand.is_empty():
        return []

    splits = act.filter(
        pl_mod.col("_action").is_in(sorted(SPLIT_ACTIONS))
        & (
            pl_mod.col("_value").is_null()
            | pl_mod.col("_value").is_nan()
            | (pl_mod.col("_value").is_finite() & (pl_mod.col("_value") > 0.0))
        )
    )
    cand = _merge_nearest_action(cand, splits, "_split_dt", pl_mod)
    cand = cand.filter(pl_mod.col("_split_dt").is_null())
    if cand.is_empty():
        return []

    spinoffs = act.filter(pl_mod.col("_action").is_in(sorted(SPINOFF_ACTIONS)))
    cand = _merge_nearest_action(cand, spinoffs, "_spinoff_dt", pl_mod)
    cand = _merge_exact_date_amount(cand, act, DIVIDEND_ACTIONS, "_div_amt", pl_mod)
    cand = _merge_exact_date_amount(cand, act, DISTRIBUTION_ACTIONS, "_dist_amt", pl_mod)
    cand = cand.sort(["_symbol", "_dt"], maintain_order=True)

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
    has_open = "open" in cand.columns

    select_cols = [
        "_symbol",
        "_date_str",
        "_close_ret",
        "_overnight_gap",
        "_prev_close",
        "close",
        "open" if has_open else pl_mod.lit(None, dtype=pl_mod.Float64).alias("open"),
        "_adj_close_ret",
        "_div_amt",
        "_dist_amt",
        "_spinoff_dt",
    ]
    findings: list[Any] = []
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
    ) in cand.select(select_cols).iter_rows():
        has_spin = sp_dt is not None
        cr = opt_float(cr_v)
        og = opt_float(og_v)
        pc = float(pc_v)
        close_f = opt_float(c_v)
        open_f = opt_float(o_v)
        adj_ret = opt_float(adj_v)
        div_amt = float(div_v)
        dist_amt = float(dist_v)

        ex_dist_ret = (
            float(close_f / (pc - dist_amt) - 1.0)
            if (dist_amt > 0.0 and pc > dist_amt and close_f is not None)
            else None
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
            "spinoff_date": sp_dt.isoformat() if sp_dt is not None else None,
        }
        findings.append(make_finding(finding_cls, str(sym), str(d_str), cat, sev, msg, metrics))
    return findings

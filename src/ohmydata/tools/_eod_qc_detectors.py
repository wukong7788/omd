"""Internal vectorized Polars detectors for EOD market-data quality control."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from ._eod_qc_prep import (
    DISTRIBUTION_ACTIONS,
    OHLC_COLUMNS,
    PRICE_COLUMNS,
    SPLIT_ACTIONS,
    QualityCategory,
    QualitySeverity,
    make_finding,
    norm_date_str,
    opt_float,
    require_polars,
)


def detect_non_positive_prices_work(work: Any, finding_cls: Any) -> list[Any]:
    cols = [c for c in PRICE_COLUMNS if c in work.columns]
    if work.is_empty() or not cols:
        return []
    pl_mod = require_polars()
    bad_exprs = [
        (pl_mod.col(c).is_null() | (~pl_mod.col(c).is_finite()) | (pl_mod.col(c) <= 0.0)).fill_null(
            True
        )
        for c in cols
    ]
    bad_rows = work.filter(pl_mod.any_horizontal(bad_exprs))
    if bad_rows.is_empty():
        return []

    findings: list[Any] = []
    for row in bad_rows.select(["_symbol", "_date_str", *cols]).iter_rows():
        sym, d_str = row[0], row[1]
        bad_fields: dict[str, float | None] = {}
        for col_name, raw_v in zip(cols, row[2:], strict=True):
            num = opt_float(raw_v)
            if num is None or num <= 0.0:
                bad_fields[col_name] = num
        findings.append(
            make_finding(
                finding_cls,
                str(sym),
                str(d_str),
                QualityCategory.NON_POSITIVE_PRICE,
                QualitySeverity.ERROR,
                f"Non-positive or non-finite price in {sorted(bad_fields)}",
                {"bad_fields": bad_fields},
            )
        )
    return findings


def detect_ohlc_violations_work(work: Any, ulp_rel_tol: float, finding_cls: Any) -> list[Any]:
    if work.is_empty() or not set(OHLC_COLUMNS).issubset(work.columns):
        return []
    pl_mod = require_polars()
    o = pl_mod.col("open")
    h = pl_mod.col("high")
    low = pl_mod.col("low")
    c = pl_mod.col("close")
    finite = (
        o.is_not_null()
        & o.is_finite()
        & h.is_not_null()
        & h.is_finite()
        & low.is_not_null()
        & low.is_finite()
        & c.is_not_null()
        & c.is_finite()
    ).fill_null(False)
    tol = c.abs() * ulp_rel_tol
    hl_bad = (finite & (h < low)).fill_null(False)
    hi_bad = (finite & (h < pl_mod.max_horizontal(o, c) - tol)).fill_null(False)
    lo_bad = (finite & (low > pl_mod.min_horizontal(o, c) + tol)).fill_null(False)

    bad_rows = work.with_columns(
        hl_bad.alias("_hl_bad"),
        hi_bad.alias("_hi_bad"),
        lo_bad.alias("_lo_bad"),
    ).filter(pl_mod.col("_hl_bad") | pl_mod.col("_hi_bad") | pl_mod.col("_lo_bad"))
    if bad_rows.is_empty():
        return []

    findings: list[Any] = []
    select_cols = [
        "_symbol",
        "_date_str",
        "open",
        "high",
        "low",
        "close",
        "_hl_bad",
        "_hi_bad",
        "_lo_bad",
    ]
    for sym, d_str, o_v, h_v, l_v, c_v, hl_f, hi_f, lo_f in bad_rows.select(
        select_cols
    ).iter_rows():
        rules = tuple(
            n
            for n, flag in (
                ("high_lt_low", hl_f),
                ("high_lt_max_oc", hi_f),
                ("low_gt_min_oc", lo_f),
            )
            if flag
        )
        metrics = {
            "open": float(o_v),
            "high": float(h_v),
            "low": float(l_v),
            "close": float(c_v),
            "rules": rules,
        }
        findings.append(
            make_finding(
                finding_cls,
                str(sym),
                str(d_str),
                QualityCategory.OHLC_GEOMETRY,
                QualitySeverity.ERROR,
                f"OHLC geometry violation: {', '.join(rules)}",
                metrics,
            )
        )
    return findings


def detect_oscillating_currency_jumps_work(
    work: Any, jump_threshold: float, max_gap_days: int, finding_cls: Any
) -> list[Any]:
    if work.is_empty() or "close" not in work.columns:
        return []
    pl_mod = require_polars()
    base = work.with_columns(pl_mod.col("close").shift(1).over("_symbol").alias("_prev_close"))
    valid = (
        pl_mod.col("_prev_close").is_not_null()
        & pl_mod.col("_prev_close").is_finite()
        & (pl_mod.col("_prev_close") > 0.0)
        & pl_mod.col("close").is_not_null()
        & pl_mod.col("close").is_finite()
        & (pl_mod.col("close") > 0.0)
    )
    ratio_col = pl_mod.col("close") / pl_mod.col("_prev_close")
    jumps = base.with_columns(
        pl_mod.when(valid).then(ratio_col - 1.0).otherwise(None).alias("_ret"),
        pl_mod.when(valid).then(ratio_col.log()).otherwise(None).alias("_log_ret"),
    ).filter((pl_mod.col("_ret").abs() >= (1.0 - 1.0 / (1.0 + jump_threshold))).fill_null(False))
    if jumps.is_empty():
        return []

    jumps = jumps.with_columns(
        pl_mod.col("_dt").shift(1).over("_symbol").alias("_prev_dt"),
        pl_mod.col("_ret").shift(1).over("_symbol").alias("_prev_ret"),
        pl_mod.col("_log_ret").shift(1).over("_symbol").alias("_prev_log_ret"),
    )
    a1 = pl_mod.col("_prev_log_ret").abs()
    a2 = pl_mod.col("_log_ret").abs()
    jumps = jumps.with_columns(
        (pl_mod.col("_dt") - pl_mod.col("_prev_dt")).dt.total_days().alias("_gap_days"),
        (pl_mod.min_horizontal(a1, a2) / pl_mod.max_horizontal(a1, a2)).alias("_log_ratio"),
        pl_mod.max_horizontal(pl_mod.col("_prev_ret").abs(), pl_mod.col("_ret").abs()).alias(
            "_max_abs_ret"
        ),
        pl_mod.min_horizontal(pl_mod.col("_prev_ret").abs(), pl_mod.col("_ret").abs()).alias(
            "_min_abs_ret"
        ),
    )
    flagged = jumps.filter(
        (
            pl_mod.col("_prev_dt").is_not_null()
            & (pl_mod.col("_gap_days") <= max_gap_days)
            & ((pl_mod.col("_prev_log_ret") * pl_mod.col("_log_ret")) < 0.0)
            & (pl_mod.col("_max_abs_ret") >= jump_threshold)
            & (pl_mod.col("_log_ratio") >= 0.50)
        ).fill_null(False)
    )
    if flagged.is_empty():
        return []

    select_cols = [
        "_symbol",
        "_date_str",
        "_prev_dt",
        "_prev_ret",
        "_ret",
        "_gap_days",
        "_log_ratio",
        "_min_abs_ret",
    ]
    findings: list[Any] = []
    for sym, d_str, p_dt, p_ret, c_ret, gap_d, ratio, min_r in flagged.select(
        select_cols
    ).iter_rows():
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
            "first_date": p_dt.isoformat(),
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
    work: Any, act: Any, tolerance: float, finding_cls: Any
) -> list[Any]:
    if (
        work.is_empty()
        or act.is_empty()
        or "close" not in work.columns
        or "adj_close" not in work.columns
    ):
        return []
    pl_mod = require_polars()
    rev = act.filter(
        pl_mod.col("_action").is_in(sorted(SPLIT_ACTIONS))
        & pl_mod.col("_value").is_not_null()
        & pl_mod.col("_value").is_finite()
        & (pl_mod.col("_value") > 0.0)
        & (pl_mod.col("_value") < 1.0)
    )
    if rev.is_empty():
        return []
    rev_max = rev.group_by("_symbol").agg(pl_mod.col("_dt").max().alias("_rev_split_dt"))
    joined = work.join(rev_max, on="_symbol", how="inner")
    if joined.is_empty():
        return []

    divs = (
        act.filter(
            pl_mod.col("_action").is_in(sorted(DISTRIBUTION_ACTIONS))
            & pl_mod.col("_value").is_not_null()
            & pl_mod.col("_value").is_finite()
            & (pl_mod.col("_value") > 0.0)
        )
        .group_by(["_symbol", "_dt"])
        .agg(pl_mod.col("_value").sum().alias("_div"))
    )
    joined = (
        joined.join(divs, on=["_symbol", "_dt"], how="left")
        .sort(["_symbol", "_dt"], maintain_order=True)
        .with_columns(
            pl_mod.col("_div").fill_null(0.0),
            pl_mod.col("close").shift(1).over("_symbol").alias("_prev_close"),
            pl_mod.col("adj_close").shift(1).over("_symbol").alias("_prev_adj"),
        )
    )
    sub = joined.filter(
        (
            (pl_mod.col("_dt") < pl_mod.col("_rev_split_dt"))
            & pl_mod.col("_prev_close").is_not_null()
            & pl_mod.col("_prev_close").is_finite()
            & (pl_mod.col("_prev_close") > 0.0)
            & pl_mod.col("close").is_not_null()
            & pl_mod.col("close").is_finite()
            & (pl_mod.col("close") > 0.0)
            & pl_mod.col("_prev_adj").is_not_null()
            & pl_mod.col("_prev_adj").is_finite()
            & (pl_mod.col("_prev_adj") > 0.0)
            & pl_mod.col("adj_close").is_not_null()
            & pl_mod.col("adj_close").is_finite()
            & (pl_mod.col("adj_close") > 0.0)
        ).fill_null(False)
    )
    if sub.is_empty():
        return []

    denom = pl_mod.col("_prev_close") - pl_mod.col("_div")
    sub = sub.with_columns(
        (pl_mod.col("adj_close") / pl_mod.col("_prev_adj") - 1.0).alias("_adj_ret"),
        ((pl_mod.col("_div") > 0.0) & (denom <= 0.0)).alias("_div_ge_close"),
        pl_mod.when(denom > 0.0)
        .then(pl_mod.col("close") / denom - 1.0)
        .otherwise(None)
        .alias("_expected_adj_ret"),
    ).with_columns(
        (pl_mod.col("_adj_ret") - pl_mod.col("_expected_adj_ret")).abs().alias("_ret_diff")
    )
    bad = sub.filter(
        (pl_mod.col("_div_ge_close") | (pl_mod.col("_ret_diff") > tolerance)).fill_null(False)
    )
    if bad.is_empty():
        return []

    select_cols = [
        "_symbol",
        "_date_str",
        "_rev_split_dt",
        "_div",
        "_prev_close",
        "_adj_ret",
        "_expected_adj_ret",
        "_ret_diff",
        "_div_ge_close",
    ]
    return [
        make_finding(
            finding_cls,
            str(sym),
            str(d_str),
            QualityCategory.REVERSE_SPLIT_DIVIDEND_MISMATCH,
            QualitySeverity.ERROR,
            "Pre-reverse-split adj_close return diverges from close/dividend implied return",
            {
                "reverse_split_date": rev_dt.isoformat(),
                "dividend": float(div_v),
                "prev_close": float(pc_v),
                "adj_ret": float(a_ret),
                "expected_adj_ret": opt_float(exp_ret),
                "ret_diff": opt_float(r_diff),
                "dividend_ge_prev_close": bool(d_ge_c),
            },
        )
        for sym, d_str, rev_dt, div_v, pc_v, a_ret, exp_ret, r_diff, d_ge_c in bad.select(
            select_cols
        ).iter_rows()
    ]


def detect_trailing_staleness_work(
    work: Any,
    expected_latest_date: str | date | datetime | None,
    max_lag_days: int,
    active_symbols: Iterable[str] | None,
    finding_cls: Any,
) -> list[Any]:
    if expected_latest_date is None:
        return []
    exp_str = norm_date_str(expected_latest_date)
    exp_dt = date.fromisoformat(exp_str)
    max_dates: dict[str, date] = {}
    if not work.is_empty():
        pl_mod = require_polars()
        for sym, latest in work.group_by("_symbol").agg(pl_mod.col("_dt").max()).iter_rows():
            if latest is not None:
                max_dates[str(sym)] = latest

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
            latest_str = latest.isoformat()
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


def detect_zero_volume_work(work: Any, finding_cls: Any) -> list[Any]:
    if work.is_empty() or "volume" not in work.columns:
        return []
    pl_mod = require_polars()
    vol = pl_mod.col("volume")
    bad = work.filter((vol.is_null() | (~vol.is_finite()) | (vol <= 0.0)).fill_null(True))
    if bad.is_empty():
        return []
    findings: list[Any] = []
    for sym, d_str, raw_v in bad.select(["_symbol", "_date_str", "volume"]).iter_rows():
        num = opt_float(raw_v)
        is_invalid = num is None or num < 0.0
        findings.append(
            make_finding(
                finding_cls,
                str(sym),
                str(d_str),
                QualityCategory.ZERO_VOLUME,
                QualitySeverity.WARNING if is_invalid else QualitySeverity.INFO,
                "Negative or non-finite volume" if is_invalid else "Zero trading volume",
                {"volume": num},
            )
        )
    return findings

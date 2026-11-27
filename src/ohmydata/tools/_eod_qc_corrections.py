"""Dual Pandas/Polars explicit correction overlay helpers for EOD QC."""

from __future__ import annotations

import importlib
import math
from collections.abc import Iterable, Sequence
from typing import Any

from ._eod_qc_prep import (
    date_expr_from_dtype,
    is_pandas_dataframe,
    is_polars_dataframe,
    norm_date_str,
    require_polars,
    symbol_col,
)


def _is_missing(val: Any) -> bool:
    if val is None or type(val).__name__ in ("NAType", "NaTType"):
        return True
    if isinstance(val, bool):
        return False
    if isinstance(val, (float, int)) or hasattr(val, "__float__"):
        try:
            return math.isnan(float(val))
        except (TypeError, ValueError):
            pass
    return False


def _is_numeric(val: Any) -> bool:
    if isinstance(val, bool):
        return False
    return isinstance(val, (int, float)) or (hasattr(val, "__float__") and not isinstance(val, str))


def _is_bool(val: Any) -> bool:
    return isinstance(val, bool) or type(val).__name__ == "bool_"


def _values_match(current: Any, expected: Any) -> bool:
    cur_missing = _is_missing(current)
    exp_missing = _is_missing(expected)
    if cur_missing and exp_missing:
        return True
    if cur_missing or exp_missing:
        return False
    if _is_bool(current) or _is_bool(expected):
        return _is_bool(current) and _is_bool(expected) and bool(current) is bool(expected)
    if _is_numeric(current) and _is_numeric(expected):
        try:
            cf, ef = float(current), float(expected)
            if math.isfinite(cf) and math.isfinite(ef):
                return math.isclose(cf, ef, rel_tol=1e-9, abs_tol=1e-12)
        except (TypeError, ValueError):
            pass
    return current == expected


def _make_receipt(receipt_cls: Any, rec: Any, *, applied: bool, status: str) -> Any:
    return receipt_cls(
        symbol=rec.symbol,
        date=rec.date,
        field=rec.field,
        raw_value=rec.raw_value,
        corrected_value=rec.corrected_value,
        reason=rec.reason,
        evidence_source=rec.evidence_source,
        version=rec.version,
        applied=applied,
        status=status,
    )


def _apply_polars(
    df: Any,
    rec_list: list[Any],
    receipt_cls: Any,
) -> tuple[Any, list[Any]]:
    pl_mod = require_polars()
    out = df.clone()
    if not rec_list or out.is_empty():
        return out, [
            _make_receipt(receipt_cls, r, applied=False, status="NOT_FOUND") for r in rec_list
        ]

    s_col = symbol_col(out)
    if "date" not in out.columns:
        raise ValueError("df must contain a 'date' column")

    for r in rec_list:
        if r.field in ("symbol", "canonical_symbol", "date") or r.field not in out.columns:
            raise ValueError(f"Invalid or missing correction target field: {r.field!r}")
        raw_col = f"raw_{r.field}"
        if raw_col not in out.columns:
            out = out.with_columns(pl_mod.col(r.field).alias(raw_col))

    sym_keys = out.get_column(s_col).cast(pl_mod.String)
    date_keys = out.select(
        date_expr_from_dtype("date", out.schema["date"], pl_mod)
        .dt.strftime("%Y-%m-%d")
        .alias("_dk")
    ).get_column("_dk")

    receipts: list[Any] = []
    for r in rec_list:
        mask = (sym_keys == r.symbol) & (date_keys == r.date)
        matches = mask.arg_true().to_list()
        if len(matches) == 0:
            receipts.append(_make_receipt(receipt_cls, r, applied=False, status="NOT_FOUND"))
            continue
        if len(matches) > 1:
            raise ValueError(f"Duplicate rows found for ({r.symbol}, {r.date})")
        idx = matches[0]
        current_val = out.get_column(r.field)[idx]
        if not _values_match(current_val, r.raw_value):
            raise ValueError(
                f"raw_value mismatch for ({r.symbol}, {r.date}, {r.field}): "
                f"expected {r.raw_value!r}, found {current_val!r}"
            )
        col_dtype = out.schema[r.field]
        if col_dtype.is_integer() and (
            _is_missing(r.corrected_value) or isinstance(r.corrected_value, float)
        ):
            out = out.with_columns(pl_mod.col(r.field).cast(pl_mod.Float64))
            col_dtype = pl_mod.Float64

        repl_val = (
            None
            if _is_missing(r.corrected_value) and not col_dtype.is_float()
            else r.corrected_value
        )
        if repl_val is not None and isinstance(repl_val, float) and math.isnan(repl_val):
            lit_expr = pl_mod.lit(float("nan"), dtype=pl_mod.Float64)
        else:
            lit_expr = pl_mod.lit(repl_val, dtype=col_dtype if repl_val is None else None)
        out = out.with_columns(
            pl_mod.when(pl_mod.int_range(pl_mod.len()) == idx)
            .then(lit_expr)
            .otherwise(pl_mod.col(r.field))
            .alias(r.field)
        )
        receipts.append(_make_receipt(receipt_cls, r, applied=True, status="APPLIED"))
    return out, receipts


def _apply_pandas(
    df: Any,
    rec_list: list[Any],
    receipt_cls: Any,
) -> tuple[Any, list[Any]]:
    pd_mod = importlib.import_module("pandas")
    out = df.copy(deep=True)
    if not rec_list or out.empty:
        return out, [
            _make_receipt(receipt_cls, r, applied=False, status="NOT_FOUND") for r in rec_list
        ]

    s_col = symbol_col(out)
    if "date" not in out.columns:
        raise ValueError("df must contain a 'date' column")
    sym_keys = out[s_col].astype(str)
    date_keys = pd_mod.Series(
        [norm_date_str(v) if not _is_missing(v) else None for v in out["date"]], index=out.index
    )

    for r in rec_list:
        if r.field in ("symbol", "canonical_symbol", "date") or r.field not in out.columns:
            raise ValueError(f"Invalid or missing correction target field: {r.field!r}")
        raw_col = f"raw_{r.field}"
        if raw_col not in out.columns:
            out[raw_col] = out[r.field]

    receipts: list[Any] = []
    for r in rec_list:
        matches = out.index[(sym_keys == r.symbol) & (date_keys == r.date)]
        if len(matches) == 0:
            receipts.append(_make_receipt(receipt_cls, r, applied=False, status="NOT_FOUND"))
            continue
        if len(matches) > 1:
            raise ValueError(f"Duplicate rows found for ({r.symbol}, {r.date})")
        idx = matches[0]
        current_val = out.at[idx, r.field]
        if not _values_match(current_val, r.raw_value):
            raise ValueError(
                f"raw_value mismatch for ({r.symbol}, {r.date}, {r.field}): "
                f"expected {r.raw_value!r}, found {current_val!r}"
            )
        if pd_mod.api.types.is_integer_dtype(out[r.field].dtype) and (
            _is_missing(r.corrected_value) or isinstance(r.corrected_value, float)
        ):
            out[r.field] = out[r.field].astype("float64")
        out.at[idx, r.field] = r.corrected_value
        receipts.append(_make_receipt(receipt_cls, r, applied=True, status="APPLIED"))
    return out, receipts


def apply_corrections_work(
    df: Any,
    records: Sequence[Any] | Iterable[Any],
    receipt_cls: Any,
) -> tuple[Any, list[Any]]:
    """Apply CorrectionRecords non-destructively preserving caller DataFrame type."""
    rec_list = list(records)
    if is_polars_dataframe(df):
        return _apply_polars(df, rec_list, receipt_cls)
    if is_pandas_dataframe(df):
        return _apply_pandas(df, rec_list, receipt_cls)
    raise TypeError(f"Expected polars.DataFrame or pandas.DataFrame, got {type(df).__name__}")

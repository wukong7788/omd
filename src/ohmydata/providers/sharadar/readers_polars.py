"""Native Polars LazyFrame/DataFrame readers for local Sharadar CSV and Parquet datasets."""

from __future__ import annotations

import csv
import importlib
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ohmydata.core.errors import SchemaMismatchError

from .endpoints import STANDARD_COLUMNS, SharadarAdjustmentMode
from .readers import _normalize_symbols, _parse_date_bound

if TYPE_CHECKING:
    import polars as pl

_STRING_COLUMN_KEYS: frozenset[str] = frozenset(
    {"ticker", "symbol", "date", "action", "name", "contraticker", "contraname"}
)
_NUMERIC_COLUMN_KEYS: frozenset[str] = frozenset(
    {"open", "high", "low", "close", "volume", "closeadj", "closeunadj", "value"}
)


def _require_polars() -> Any:
    try:
        return importlib.import_module("polars")
    except ImportError as exc:
        raise ImportError(
            "polars is required for Sharadar Polars readers. "
            "Install it with `pip install ohmydata[polars]`."
        ) from exc


def _csv_columns(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle), None)
    if header is None:
        return []
    return list(header)


def _scan_raw(
    path: Path,
    pl_mod: Any,
    *,
    required_cols: set[str],
    context_label: str,
) -> tuple[Any, dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"Sharadar local data file not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".csv":
        raw_cols = _csv_columns(path)
        override_keys = _STRING_COLUMN_KEYS | _NUMERIC_COLUMN_KEYS
        schema_overrides = {
            col: pl_mod.String for col in raw_cols if str(col).strip().lower() in override_keys
        }
        lf = pl_mod.scan_csv(
            path,
            null_values=[""],
            schema_overrides=schema_overrides or None,
        )
    elif suffix == ".parquet":
        lf = pl_mod.scan_parquet(path)
        raw_cols = list(lf.collect_schema().names())
    else:
        raise ValueError(f"Unsupported local file format {suffix!r}; expected .csv or .parquet")

    lowered = {col: str(col).strip().lower() for col in raw_cols}
    norm_names = set(lowered.values())
    if "ticker" not in norm_names and "symbol" not in norm_names:
        raise SchemaMismatchError("Missing required 'ticker' or 'symbol' column in Sharadar data")
    if "date" not in norm_names:
        raise SchemaMismatchError("Missing required 'date' column in Sharadar data")

    missing = required_cols - norm_names
    if missing:
        raise SchemaMismatchError(
            f"Missing required Sharadar {context_label} columns {sorted(missing)}"
        )

    rename_map: dict[str, str] = {}
    has_symbol = "symbol" in norm_names
    for orig, norm in lowered.items():
        target = "symbol" if (norm == "ticker" and not has_symbol) else norm
        if orig != target:
            rename_map[orig] = target
    if rename_map:
        lf = lf.rename(rename_map)

    return lf, dict(lf.collect_schema())


def _apply_common_filters(
    lf: Any,
    schema: dict[str, Any],
    pl_mod: Any,
    *,
    symbols: Iterable[str] | None,
    start_date: str | date | datetime | None,
    end_date: str | date | datetime | None,
) -> Any:
    date_dtype = schema["date"]
    if date_dtype == pl_mod.Date:
        date_expr = pl_mod.col("date")
    elif isinstance(date_dtype, pl_mod.Datetime):
        date_expr = pl_mod.col("date").dt.replace_time_zone(None).dt.date()
    else:
        text = pl_mod.col("date").cast(pl_mod.String).str.strip_chars()
        head10 = text.str.slice(0, 10)
        norm_date_text = (
            pl_mod.when(head10.str.to_date("%Y-%m-%d", strict=False).is_not_null())
            .then(head10)
            .when(head10.str.to_date("%Y/%m/%d", strict=False).is_not_null())
            .then(head10.str.replace_all("/", "-"))
            .when(text.str.to_date("%Y%m%d", strict=False).is_not_null())
            .then(
                pl_mod.concat_str(
                    [
                        text.str.slice(0, 4),
                        pl_mod.lit("-"),
                        text.str.slice(4, 2),
                        pl_mod.lit("-"),
                        text.str.slice(6, 2),
                    ]
                )
            )
            .otherwise(text)
        )
        date_expr = norm_date_text.str.to_date("%Y-%m-%d", strict=True)

    lf = lf.with_columns(
        pl_mod.col("symbol").cast(pl_mod.String).str.strip_chars().alias("symbol"),
        date_expr.alias("date"),
    )

    symbol_set = _normalize_symbols(symbols)
    if symbol_set is not None:
        lf = lf.filter(pl_mod.col("symbol").is_in(sorted(symbol_set)))
    if start_date is not None:
        start_bound = _parse_date_bound(start_date)
        lf = lf.filter(pl_mod.col("date") >= start_bound)
    if end_date is not None:
        end_bound = _parse_date_bound(end_date)
        lf = lf.filter(pl_mod.col("date") <= end_bound)
    return lf


def scan_local_bars_pl(
    path: Path | str,
    *,
    symbols: Iterable[str] | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    adjustment_mode: SharadarAdjustmentMode = SharadarAdjustmentMode.SPLIT_ADJUSTED,
) -> pl.LazyFrame:
    """Scan local Sharadar EOD price bars (.csv or .parquet) lazily into Polars."""
    pl_mod = _require_polars()
    mode = SharadarAdjustmentMode(adjustment_mode)

    required = {"open", "high", "low", "close", "volume"}
    if mode in (SharadarAdjustmentMode.SPLIT_ADJUSTED, SharadarAdjustmentMode.TOTAL_RETURN):
        required.add("closeadj")
    if mode == SharadarAdjustmentMode.UNADJUSTED:
        required.add("closeunadj")

    lf, schema = _scan_raw(
        Path(path),
        pl_mod,
        required_cols=required,
        context_label=f"bar ({mode.value})",
    )
    lf = _apply_common_filters(
        lf,
        schema,
        pl_mod,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
    )

    numeric_cols = [
        c
        for c in ("open", "high", "low", "close", "volume", "closeadj", "closeunadj")
        if c in schema
    ]
    lf = lf.with_columns(
        [pl_mod.col(c).cast(pl_mod.Float64, strict=False).alias(c) for c in numeric_cols]
    )

    if mode == SharadarAdjustmentMode.SPLIT_ADJUSTED:
        lf = lf.with_columns(pl_mod.col("closeadj").alias("adj_close"))
    elif mode == SharadarAdjustmentMode.UNADJUSTED:
        valid_denom = (
            pl_mod.col("close").is_not_null()
            & pl_mod.col("close").is_finite()
            & (pl_mod.col("close") != 0.0)
            & pl_mod.col("closeunadj").is_not_null()
            & pl_mod.col("closeunadj").is_finite()
        )
        factor = (
            pl_mod.when(valid_denom)
            .then(pl_mod.col("closeunadj") / pl_mod.col("close"))
            .otherwise(None)
        )
        adj_expr = (
            pl_mod.col("closeadj")
            if "closeadj" in schema
            else pl_mod.lit(None, dtype=pl_mod.Float64)
        )
        lf = lf.with_columns(
            (pl_mod.col("open") * factor).alias("open"),
            (pl_mod.col("high") * factor).alias("high"),
            (pl_mod.col("low") * factor).alias("low"),
            pl_mod.col("closeunadj").alias("close"),
            adj_expr.alias("adj_close"),
        )
    elif mode == SharadarAdjustmentMode.TOTAL_RETURN:
        valid_denom = (
            pl_mod.col("close").is_not_null()
            & pl_mod.col("close").is_finite()
            & (pl_mod.col("close") != 0.0)
            & pl_mod.col("closeadj").is_not_null()
            & pl_mod.col("closeadj").is_finite()
        )
        factor = (
            pl_mod.when(valid_denom)
            .then(pl_mod.col("closeadj") / pl_mod.col("close"))
            .otherwise(None)
        )
        lf = lf.with_columns(
            (pl_mod.col("open") * factor).alias("open"),
            (pl_mod.col("high") * factor).alias("high"),
            (pl_mod.col("low") * factor).alias("low"),
            pl_mod.col("closeadj").alias("close"),
            pl_mod.col("closeadj").alias("adj_close"),
        )

    return lf.select(list(STANDARD_COLUMNS)).sort(["symbol", "date"])


def read_local_bars_pl(
    path: Path | str,
    *,
    symbols: Iterable[str] | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    adjustment_mode: SharadarAdjustmentMode = SharadarAdjustmentMode.SPLIT_ADJUSTED,
) -> pl.DataFrame:
    """Read local Sharadar EOD price bars (.csv or .parquet) into a Polars DataFrame."""
    return scan_local_bars_pl(
        path,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        adjustment_mode=adjustment_mode,
    ).collect()


def scan_local_actions_pl(
    path: Path | str,
    *,
    symbols: Iterable[str] | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
) -> pl.LazyFrame:
    """Scan local Sharadar corporate actions (.csv or .parquet) lazily into Polars."""
    pl_mod = _require_polars()
    lf, schema = _scan_raw(
        Path(path),
        pl_mod,
        required_cols={"action"},
        context_label="actions",
    )
    lf = _apply_common_filters(
        lf,
        schema,
        pl_mod,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
    )

    exprs = [
        pl_mod.col("action")
        .cast(pl_mod.String)
        .str.strip_chars()
        .str.to_lowercase()
        .alias("action")
    ]
    if "value" in schema:
        exprs.append(pl_mod.col("value").cast(pl_mod.Float64, strict=False).alias("value"))
    lf = lf.with_columns(exprs)

    preferred = ("symbol", "date", "action", "value", "name", "contraticker", "contraname")
    ordered_cols = [c for c in preferred if c in schema] + [c for c in schema if c not in preferred]
    return lf.select(ordered_cols).sort(["symbol", "date", "action"])


def read_local_actions_pl(
    path: Path | str,
    *,
    symbols: Iterable[str] | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
) -> pl.DataFrame:
    """Read local Sharadar corporate actions (.csv or .parquet) into a Polars DataFrame."""
    return scan_local_actions_pl(
        path,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
    ).collect()


__all__ = [
    "read_local_actions_pl",
    "read_local_bars_pl",
    "scan_local_actions_pl",
    "scan_local_bars_pl",
]

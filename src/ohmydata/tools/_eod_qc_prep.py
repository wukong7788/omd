"""Internal normalization, enums, Polars adapter helpers, and finding builders for EOD QC."""

from __future__ import annotations

import importlib
import math
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "adj_close")
OHLC_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")
SPLIT_ACTIONS: frozenset[str] = frozenset({"split", "stock split", "stock_split"})
SPINOFF_ACTIONS: frozenset[str] = frozenset({"spinoff", "spinoffdividend", "spin-off", "spin_off"})
DIVIDEND_ACTIONS: frozenset[str] = frozenset({"dividend", "cash dividend", "dividends"})
CAPITAL_GAIN_ACTIONS: frozenset[str] = frozenset(
    {"capital_gain", "capital gain", "capital_gains", "capital gains"}
)
DISTRIBUTION_ACTIONS: frozenset[str] = DIVIDEND_ACTIONS | CAPITAL_GAIN_ACTIONS


class QualitySeverity(StrEnum):
    """Severity level of an EOD quality control finding."""

    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class QualityCategory(StrEnum):
    """Taxonomy of EOD market-data quality findings."""

    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    OHLC_GEOMETRY = "OHLC_GEOMETRY"
    SUSPECT_MISSING_SPLIT = "SUSPECT_MISSING_SPLIT"
    DISTRIBUTION_EX_DATE = "DISTRIBUTION_EX_DATE"
    SPINOFF_BASIS_DIFFERENCE = "SPINOFF_BASIS_DIFFERENCE"
    OSCILLATING_CURRENCY_JUMP = "OSCILLATING_CURRENCY_JUMP"
    REVERSE_SPLIT_DIVIDEND_MISMATCH = "REVERSE_SPLIT_DIVIDEND_MISMATCH"
    TRAILING_STALENESS = "TRAILING_STALENESS"
    ZERO_VOLUME = "ZERO_VOLUME"


def require_polars() -> Any:
    """Import polars lazily, raising a clear error if the extra is missing."""
    try:
        return importlib.import_module("polars")
    except ImportError as exc:
        raise ImportError(
            "polars is required for ohmydata.tools.eod_qc. "
            "Install it with `pip install ohmydata[polars]`."
        ) from exc


def is_polars_dataframe(df: Any) -> bool:
    """Check whether df is a polars.DataFrame without requiring polars at import time."""
    if df is None:
        return False
    cls = type(df)
    return cls.__module__.startswith("polars.") and cls.__name__ == "DataFrame"


def is_pandas_dataframe(df: Any) -> bool:
    """Check whether df is a pandas.DataFrame without requiring pandas at import time."""
    if df is None:
        return False
    cls = type(df)
    return cls.__module__.startswith("pandas.") and cls.__name__ == "DataFrame"


def to_polars_frame(df: Any) -> Any:
    """Convert a Polars or Pandas DataFrame into a Polars DataFrame without mutation."""
    pl_mod = require_polars()
    if is_polars_dataframe(df):
        return df
    if is_pandas_dataframe(df):
        if df.empty and len(df.columns) == 0:
            return pl_mod.DataFrame()
        try:
            pa_mod = importlib.import_module("pyarrow")
        except ImportError as exc:
            raise ImportError(
                "pyarrow is required when passing a pandas.DataFrame to ohmydata.tools.eod_qc. "
                "Install it with `pip install ohmydata[polars]`."
            ) from exc
        try:
            return pl_mod.from_pandas(df)
        except (
            TypeError,
            ValueError,
            pl_mod.exceptions.PolarsError,
            pa_mod.ArrowInvalid,
            pa_mod.ArrowTypeError,
        ):
            # Handle mixed Python object columns (e.g. date + ISO str in tests) safely
            pd_mod = importlib.import_module("pandas")
            cleaned = df.copy()
            for col in cleaned.columns:
                if str(cleaned[col].dtype) == "object":
                    cleaned[col] = [
                        v.isoformat()
                        if isinstance(v, (date, datetime))
                        else (None if pd_mod.isna(v) else str(v))
                        for v in cleaned[col]
                    ]
            return pl_mod.from_pandas(cleaned)
    raise TypeError(f"Expected polars.DataFrame or pandas.DataFrame, got {type(df).__name__}")


def opt_float(val: Any) -> float | None:
    """Convert numeric scalar to finite float or None."""
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def norm_date_str(val: Any) -> str:
    """Normalize a date-like value into ISO YYYY-MM-DD text without pandas."""
    if val is None or type(val).__name__ in ("NaTType", "NAType"):
        raise ValueError(f"Unsupported date value: {val!r}")
    if isinstance(val, datetime):
        return val.date().isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, str):
        text = val.strip()
        if not text:
            raise ValueError("date value cannot be empty")
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text).date().isoformat()
        except ValueError:
            pass
        if len(text) >= 10 and text[4] == "/" and text[7] == "/":
            try:
                return date.fromisoformat(text[:10].replace("/", "-")).isoformat()
            except ValueError as exc:
                raise ValueError(f"Unsupported date value: {val!r}") from exc
        raise ValueError(f"Unsupported date value: {val!r}")
    if hasattr(val, "to_pydatetime"):
        return val.to_pydatetime().date().isoformat()
    raise ValueError(f"Unsupported date value: {val!r}")


def symbol_col(df: Any) -> str:
    """Resolve the symbol column name ('symbol' or 'canonical_symbol')."""
    cols = df.columns
    if "symbol" in cols:
        return "symbol"
    if "canonical_symbol" in cols:
        return "canonical_symbol"
    raise ValueError("DataFrame must contain a 'symbol' or 'canonical_symbol' column")


def date_expr_from_dtype(col_name: str, dtype: Any, pl_mod: Any) -> Any:
    """Build a Polars expression converting Date, Datetime, or String column to pl.Date."""
    if dtype == pl_mod.Date:
        return pl_mod.col(col_name)
    if isinstance(dtype, pl_mod.Datetime):
        return pl_mod.col(col_name).dt.replace_time_zone(None).dt.date()
    text = pl_mod.col(col_name).cast(pl_mod.String).str.strip_chars()
    head10 = text.str.slice(0, 10)
    return pl_mod.coalesce(
        [
            head10.str.to_date("%Y-%m-%d", strict=False),
            head10.str.to_date("%Y/%m/%d", strict=False),
            text.str.to_date("%Y%m%d", strict=False),
        ]
    )


def make_finding(
    cls: Any,
    symbol: str,
    date_str: str,
    category: QualityCategory,
    severity: QualitySeverity,
    message: str,
    metrics: dict[str, Any],
) -> Any:
    return cls(
        symbol=symbol,
        date=date_str,
        category=category,
        severity=severity,
        message=message,
        metrics=MappingProxyType(metrics),
    )


def prepare_bars(bars_df: Any) -> Any:
    """Normalize dates/numerics and sort bars by (_symbol, _dt) into a Polars DataFrame."""
    pl_mod = require_polars()
    if bars_df is None:
        return pl_mod.DataFrame()
    frame = to_polars_frame(bars_df)
    if frame.is_empty():
        return pl_mod.DataFrame()
    s_col = symbol_col(frame)
    if "date" not in frame.columns:
        raise ValueError("bars_df must contain a 'date' column")

    dt_expr = date_expr_from_dtype("date", frame.schema["date"], pl_mod)
    work = frame.with_columns(
        pl_mod.col(s_col).cast(pl_mod.String).alias("_symbol"),
        dt_expr.alias("_dt"),
    ).filter(pl_mod.col("_dt").is_not_null())

    cast_exprs = [pl_mod.col("_dt").dt.strftime("%Y-%m-%d").alias("_date_str")]
    for col in (*PRICE_COLUMNS, "volume"):
        if col in work.columns:
            cast_exprs.append(pl_mod.col(col).cast(pl_mod.Float64, strict=False).alias(col))

    return work.with_columns(cast_exprs).sort(["_symbol", "_dt"], maintain_order=True)


def prepare_actions(actions_df: Any) -> Any:
    """Normalize corporate actions Polars DataFrame sorted by (_symbol, _dt)."""
    pl_mod = require_polars()
    empty_schema = {
        "_symbol": pl_mod.String,
        "_dt": pl_mod.Date,
        "_action": pl_mod.String,
        "_value": pl_mod.Float64,
    }
    if actions_df is None:
        return pl_mod.DataFrame(schema=empty_schema)
    frame = to_polars_frame(actions_df)
    if frame.is_empty():
        return pl_mod.DataFrame(schema=empty_schema)
    s_col = symbol_col(frame)
    if "date" not in frame.columns or "action" not in frame.columns:
        raise ValueError("actions_df must contain 'date' and 'action' columns")

    dt_expr = date_expr_from_dtype("date", frame.schema["date"], pl_mod)
    val_expr = (
        pl_mod.col("value").cast(pl_mod.Float64, strict=False)
        if "value" in frame.columns
        else pl_mod.lit(None, dtype=pl_mod.Float64)
    )
    return (
        frame.select(
            pl_mod.col(s_col).cast(pl_mod.String).alias("_symbol"),
            dt_expr.alias("_dt"),
            pl_mod.col("action")
            .cast(pl_mod.String)
            .str.strip_chars()
            .str.to_lowercase()
            .alias("_action"),
            val_expr.alias("_value"),
        )
        .filter(pl_mod.col("_dt").is_not_null())
        .sort(["_symbol", "_dt"], maintain_order=True)
    )

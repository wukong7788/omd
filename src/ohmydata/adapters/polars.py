"""Explicit, eager Pandas/Polars representation conversion.

The dataframe dependencies are intentionally imported inside the public
functions so core-only installations remain dependency free.
"""

from __future__ import annotations

import datetime as _dt
import decimal
from typing import Any

from ..core import SchemaMismatchError


def _missing() -> ImportError:
    return ImportError("install the optional dataframe dependencies with `ohmydata[polars]`")


def _pandas_supported(series: Any, pd: Any) -> None:
    dtype = series.dtype
    if isinstance(dtype, (pd.CategoricalDtype, pd.PeriodDtype)):
        raise SchemaMismatchError(f"unsupported Pandas dtype {dtype}")
    if isinstance(dtype, pd.IntervalDtype) or getattr(dtype, "kind", None) == "m":
        raise SchemaMismatchError(f"unsupported Pandas dtype {dtype}")
    if "Sparse" in type(dtype).__name__ or "Decimal" in type(dtype).__name__:
        raise SchemaMismatchError(f"unsupported Pandas dtype {dtype}")
    if pd.api.types.is_datetime64_any_dtype(dtype):
        tz = getattr(dtype, "tz", None)

        def timezone_of(value: Any) -> Any:
            return value.tzinfo

        if tz is not None and series.dropna().map(timezone_of).nunique() > 1:
            raise SchemaMismatchError("mixed timezone values")
        return
    if pd.api.types.is_bool_dtype(dtype) or pd.api.types.is_integer_dtype(dtype):
        return
    if pd.api.types.is_float_dtype(dtype):
        if dtype.itemsize not in (4, 8):
            raise SchemaMismatchError(f"unsupported float width {dtype.itemsize}")
        return
    if pd.api.types.is_string_dtype(dtype) and not pd.api.types.is_object_dtype(dtype):
        return
    if pd.api.types.is_object_dtype(dtype):
        values: list[Any] = []
        for value in series.tolist():
            if value is None:
                continue
            missing = pd.isna(value)
            if getattr(missing, "ndim", 0) == 0 and bool(missing):
                continue
            values.append(value)
        if not values:
            raise SchemaMismatchError("empty object dtype is unsupported")
        kinds: set[type[Any]] = {type(value) for value in values}
        if kinds <= {str} or kinds <= {bytes} or kinds <= {_dt.date}:
            return
        if all(isinstance(value, decimal.Decimal) for value in values):
            raise SchemaMismatchError("decimal object dtype is unsupported")
        raise SchemaMismatchError("heterogeneous or unsupported object dtype")
    raise SchemaMismatchError(f"unsupported Pandas dtype {dtype}")


def pandas_to_polars(frame: Any) -> Any:
    """Convert a Pandas DataFrame to an independent eager Polars DataFrame."""
    try:
        import pandas as pd
        import polars as pl

        __import__("pyarrow")
    except ImportError as exc:
        raise _missing() from exc
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("pandas_to_polars requires a pandas.DataFrame")
    if not frame.columns.is_unique:
        raise SchemaMismatchError("duplicate column names")
    for name in frame.columns:
        _pandas_supported(frame[name], pd)
    try:
        result = pl.from_pandas(frame, nan_to_null=False)
    except (TypeError, ValueError, OverflowError, RuntimeError) as exc:
        raise SchemaMismatchError(
            f"Pandas-to-Polars conversion failed: {type(exc).__name__}"
        ) from exc
    if result.columns != list(frame.columns) or result.shape != frame.shape:
        raise SchemaMismatchError("conversion changed columns or shape")
    return result


def _polars_supported(dtype: Any, pl: Any) -> None:
    allowed = (
        pl.Boolean,
        pl.Int8,
        pl.Int16,
        pl.Int32,
        pl.Int64,
        pl.UInt8,
        pl.UInt16,
        pl.UInt32,
        pl.UInt64,
        pl.Float32,
        pl.Float64,
        pl.String,
        pl.Binary,
        pl.Date,
        pl.Datetime,
    )
    if not isinstance(dtype, allowed):
        raise SchemaMismatchError(f"unsupported Polars dtype {dtype}")


def polars_to_pandas(frame: Any) -> Any:
    """Convert a Polars DataFrame to an independent eager Pandas DataFrame."""
    try:
        __import__("pandas")
        import polars as pl

        __import__("pyarrow")  # required for null-preserving conversion
    except ImportError as exc:
        raise _missing() from exc
    if not isinstance(frame, pl.DataFrame):
        raise TypeError("polars_to_pandas requires a polars.DataFrame")
    if len(set(frame.columns)) != len(frame.columns):
        raise SchemaMismatchError("duplicate column names")
    for dtype in frame.dtypes:
        _polars_supported(dtype, pl)
    try:
        result = frame.to_pandas(use_pyarrow_extension_array=True)
    except (TypeError, ValueError, OverflowError, RuntimeError) as exc:
        raise SchemaMismatchError(
            f"Polars-to-Pandas conversion failed: {type(exc).__name__}"
        ) from exc
    if list(result.columns) != frame.columns or result.shape != frame.shape:
        raise SchemaMismatchError("conversion changed columns or shape")
    return result


__all__ = ["pandas_to_polars", "polars_to_pandas"]

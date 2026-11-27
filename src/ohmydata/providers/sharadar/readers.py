"""Local CSV/Parquet readers with explicit Sharadar price adjustment semantics."""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ohmydata.core.errors import SchemaMismatchError

from .endpoints import STANDARD_COLUMNS, SharadarAdjustmentMode

if TYPE_CHECKING:
    import pandas as pd


def _require_pandas() -> Any:
    try:
        return importlib.import_module("pandas")
    except ImportError as exc:
        raise ImportError(
            "pandas is required to read local Sharadar CSV/Parquet files. "
            "Install pandas (e.g. `pip install ohmydata[polars]` or `pip install pandas`)."
        ) from exc


def _parse_date_bound(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("date bound cannot be empty")
        return date.fromisoformat(cleaned)
    raise TypeError(f"Unsupported date bound type: {type(value).__name__}")


def _normalize_symbols(symbols: Iterable[str] | None) -> set[str] | None:
    if symbols is None:
        return None
    if isinstance(symbols, str):
        cleaned = symbols.strip()
        return {cleaned} if cleaned else set()
    return {str(s).strip() for s in symbols if str(s).strip()}


def _read_frame(path: Path, pd_mod: Any) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"Sharadar local data file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd_mod.read_csv(path)
    if suffix == ".parquet":
        return pd_mod.read_parquet(path)
    raise ValueError(f"Unsupported local file format {suffix!r}; expected .csv or .parquet")


def _normalize_common_filters(
    df: pd.DataFrame,
    pd_mod: Any,
    *,
    symbols: Iterable[str] | None,
    start_date: str | date | datetime | None,
    end_date: str | date | datetime | None,
) -> pd.DataFrame:
    work = df.copy()
    work.columns = [str(c).strip().lower() for c in work.columns]

    if "ticker" in work.columns and "symbol" not in work.columns:
        work.rename(columns={"ticker": "symbol"}, inplace=True)
    elif "symbol" not in work.columns:
        raise SchemaMismatchError("Missing required 'ticker' or 'symbol' column in Sharadar data")

    if "date" not in work.columns:
        raise SchemaMismatchError("Missing required 'date' column in Sharadar data")

    work["symbol"] = work["symbol"].astype(str).str.strip()
    work["date"] = pd_mod.to_datetime(work["date"]).dt.date

    symbol_set = _normalize_symbols(symbols)
    if symbol_set is not None:
        work = work[work["symbol"].isin(symbol_set)]
    if start_date is not None:
        start_bound = _parse_date_bound(start_date)
        work = work[work["date"] >= start_bound]
    if end_date is not None:
        end_bound = _parse_date_bound(end_date)
        work = work[work["date"] <= end_bound]
    return work


def read_local_bars(
    path: Path | str,
    *,
    symbols: Iterable[str] | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    adjustment_mode: SharadarAdjustmentMode = SharadarAdjustmentMode.SPLIT_ADJUSTED,
) -> pd.DataFrame:
    """Read local Sharadar EOD price bars (.csv or .parquet) into standard OHLCV columns.

    Adjustment modes:
    - ``SPLIT_ADJUSTED``: preserves native Sharadar split-adjusted OHLC and uses ``closeadj``
      as ``adj_close``.
    - ``UNADJUSTED``: rescales OHLC by ``closeunadj / close`` (setting ``close = closeunadj``)
      and retains ``closeadj`` as ``adj_close`` when present.
    - ``TOTAL_RETURN``: rescales OHLC by ``closeadj / close`` (setting ``close = closeadj`` and
      ``adj_close = closeadj``).

    Volume is preserved from the native Sharadar ``volume`` column without synthetic scaling or
    zero-filling of missing values.
    """
    pd_mod = _require_pandas()
    mode = SharadarAdjustmentMode(adjustment_mode)
    raw = _read_frame(Path(path), pd_mod)
    work = _normalize_common_filters(
        raw,
        pd_mod,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
    )

    required = {"open", "high", "low", "close", "volume"}
    if mode in (SharadarAdjustmentMode.SPLIT_ADJUSTED, SharadarAdjustmentMode.TOTAL_RETURN):
        required.add("closeadj")
    if mode == SharadarAdjustmentMode.UNADJUSTED:
        required.add("closeunadj")

    missing = required - set(work.columns)
    if missing:
        raise SchemaMismatchError(
            f"Missing required Sharadar bar columns {sorted(missing)} for mode {mode.value}"
        )

    if work.empty:
        return pd_mod.DataFrame(columns=list(STANDARD_COLUMNS))

    for col in ("open", "high", "low", "close"):
        work[col] = pd_mod.to_numeric(work[col], errors="coerce").astype("float64")
    if "closeadj" in work.columns:
        work["closeadj"] = pd_mod.to_numeric(work["closeadj"], errors="coerce").astype("float64")
    if "closeunadj" in work.columns:
        work["closeunadj"] = pd_mod.to_numeric(work["closeunadj"], errors="coerce").astype(
            "float64"
        )
    work["volume"] = pd_mod.to_numeric(work["volume"], errors="coerce")

    if mode == SharadarAdjustmentMode.SPLIT_ADJUSTED:
        work["adj_close"] = work["closeadj"]
    elif mode == SharadarAdjustmentMode.UNADJUSTED:
        valid_denom = work["close"].notna() & (work["close"] != 0.0) & work["closeunadj"].notna()
        factor = work["closeunadj"].where(valid_denom) / work["close"].where(valid_denom)
        for col in ("open", "high", "low"):
            work[col] = work[col] * factor
        work["close"] = work["closeunadj"]
        work["adj_close"] = (
            work["closeadj"]
            if "closeadj" in work.columns
            else pd_mod.Series(float("nan"), index=work.index, dtype="float64")
        )
    elif mode == SharadarAdjustmentMode.TOTAL_RETURN:
        valid_denom = work["close"].notna() & (work["close"] != 0.0) & work["closeadj"].notna()
        factor = work["closeadj"].where(valid_denom) / work["close"].where(valid_denom)
        for col in ("open", "high", "low"):
            work[col] = work[col] * factor
        work["close"] = work["closeadj"]
        work["adj_close"] = work["closeadj"]

    work.sort_values(by=["symbol", "date"], inplace=True)
    work.reset_index(drop=True, inplace=True)
    return work[list(STANDARD_COLUMNS)]


def read_local_actions(
    path: Path | str,
    *,
    symbols: Iterable[str] | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
) -> pd.DataFrame:
    """Read local Sharadar corporate actions (.csv or .parquet) with normalized keys."""
    pd_mod = _require_pandas()
    raw = _read_frame(Path(path), pd_mod)
    work = _normalize_common_filters(
        raw,
        pd_mod,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
    )

    if "action" not in work.columns:
        raise SchemaMismatchError("Missing required 'action' column in Sharadar actions data")

    work["action"] = work["action"].astype(str).str.strip().str.lower()
    if "value" in work.columns:
        work["value"] = pd_mod.to_numeric(work["value"], errors="coerce")

    preferred = ("symbol", "date", "action", "value", "name", "contraticker", "contraname")
    ordered_cols = [c for c in preferred if c in work.columns] + [
        c for c in work.columns if c not in preferred
    ]

    if work.empty:
        return pd_mod.DataFrame(columns=ordered_cols)

    work.sort_values(by=["symbol", "date", "action"], inplace=True)
    work.reset_index(drop=True, inplace=True)
    return work[ordered_cols]


__all__ = [
    "read_local_actions",
    "read_local_bars",
]

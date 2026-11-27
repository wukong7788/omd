"""Corporate actions extraction helpers for yfinance daily bar responses."""

from __future__ import annotations

import pandas as pd

from .errors import SchemaMismatchError

ACTIONS_COLUMNS: tuple[str, ...] = ("symbol", "date", "action", "value")


def extract_yfinance_actions_df(
    raw_df: pd.DataFrame,
    requested_symbols: tuple[str, ...],
) -> pd.DataFrame:
    """Extract non-zero Dividends and Stock Splits rows from a raw yfinance DataFrame."""
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(columns=list(ACTIONS_COLUMNS))

    frames: list[pd.DataFrame] = []
    if isinstance(raw_df.columns, pd.MultiIndex):
        level0_vals = set(raw_df.columns.get_level_values(0))
        symbol_level = 0 if any(sym in level0_vals for sym in requested_symbols) else 1

        for sym in requested_symbols:
            try:
                sym_df = raw_df.xs(sym, axis=1, level=symbol_level, drop_level=True).copy()
            except KeyError:
                continue

            if isinstance(sym_df, pd.Series):
                sym_df = sym_df.to_frame()
            if not isinstance(sym_df, pd.DataFrame):
                continue

            extracted = _extract_single_symbol_actions(sym_df, sym)
            if extracted is not None and not extracted.empty:
                frames.append(extracted)
    else:
        sym = requested_symbols[0] if len(requested_symbols) == 1 else "UNKNOWN"
        extracted = _extract_single_symbol_actions(raw_df, sym)
        if extracted is not None and not extracted.empty:
            frames.append(extracted)

    if not frames:
        return pd.DataFrame(columns=list(ACTIONS_COLUMNS))

    merged = pd.concat(frames, ignore_index=True)
    merged.sort_values(by=["symbol", "date", "action"], inplace=True)
    merged.reset_index(drop=True, inplace=True)
    return merged[list(ACTIONS_COLUMNS)]


def _extract_single_symbol_actions(df_sym: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    """Extract non-zero dividend and split events for a single symbol."""
    if df_sym is None or df_sym.empty:
        return None

    work = df_sym.copy()
    if (
        isinstance(work.index, pd.DatetimeIndex)
        or str(work.index.name).lower() in ("date", "datetime")
    ) and work.index.name is None:
        work.index.name = "Date"
    work.columns = [str(c[0]) if isinstance(c, tuple) else str(c) for c in work.columns]
    work.reset_index(inplace=True)

    date_col = next((c for c in work.columns if c.lower() in ("date", "datetime")), None)
    if date_col is None and "index" in work.columns:
        try:
            pd.to_datetime(work["index"])
            date_col = "index"
        except (ValueError, TypeError, KeyError):
            pass
    if date_col is None:
        raise SchemaMismatchError(
            f"No Date or Datetime column found in response for symbol {symbol}"
        )

    col_map: dict[str, str] = {}
    for col in work.columns:
        norm_col = col.strip().lower().replace(" ", "_")
        if norm_col == "dividends":
            col_map[col] = "dividends"
        elif norm_col == "stock_splits":
            col_map[col] = "stock_splits"

    if not col_map:
        return None

    work.rename(columns=col_map, inplace=True)
    dates_raw = pd.to_datetime(work[date_col])
    work["date"] = dates_raw.dt.date

    action_frames: list[pd.DataFrame] = []
    for source_col, action_name in (("dividends", "dividend"), ("stock_splits", "split")):
        if source_col not in work.columns:
            continue
        values = pd.to_numeric(work[source_col], errors="coerce")
        non_zero = values.notna() & (values != 0.0)
        if not non_zero.any():
            continue
        action_frames.append(
            pd.DataFrame(
                {
                    "symbol": symbol,
                    "date": work.loc[non_zero, "date"].values,
                    "action": action_name,
                    "value": values.loc[non_zero].astype(float).values,
                }
            )
        )

    if not action_frames:
        return None
    return pd.concat(action_frames, ignore_index=True)

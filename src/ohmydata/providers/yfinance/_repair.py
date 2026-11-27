"""Per-symbol bounded repair helpers for yfinance daily bars."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

from ohmydata.core.errors import ProviderError

from ._actions import extract_yfinance_actions_df
from .endpoints import (
    YFinanceAdjustmentMode,
    YFinanceSymbolOutcome,
    normalize_yfinance_download_df,
)
from .errors import YFinanceRepairReceipt
from .quality import evaluate_symbol_outcomes, validate_daily_bars_dataframe


def repair_failed_symbols(
    *,
    download_fn: Callable[..., Any],
    download_kwargs: dict[str, Any],
    norm_df: pd.DataFrame,
    actions_df: pd.DataFrame | None,
    outcomes: dict[str, YFinanceSymbolOutcome],
    adjustment_mode: YFinanceAdjustmentMode,
    include_actions: bool,
) -> tuple[pd.DataFrame, pd.DataFrame | None, list[YFinanceRepairReceipt]]:
    """Attempt single-symbol recovery for any batch symbol not marked COMPLETE."""
    repair_receipts: list[YFinanceRepairReceipt] = []
    failed_symbols = [sym for sym, out in outcomes.items() if out != YFinanceSymbolOutcome.COMPLETE]
    if not failed_symbols:
        return norm_df, actions_df, repair_receipts

    repaired_dfs: list[pd.DataFrame] = []
    repaired_actions: list[pd.DataFrame] = []

    for sym in failed_symbols:
        orig_outcome = outcomes[sym].value
        try:
            single_raw = download_fn(sym, **download_kwargs)
            single_df = normalize_yfinance_download_df(single_raw, (sym,), adjustment_mode)
            validate_daily_bars_dataframe(single_df, adjustment_mode)
            single_outcomes = evaluate_symbol_outcomes(single_df, (sym,), adjustment_mode)

            if single_outcomes.get(sym) == YFinanceSymbolOutcome.COMPLETE and not single_df.empty:
                outcomes[sym] = YFinanceSymbolOutcome.RECOVERED
                repaired_dfs.append(single_df)
                if include_actions:
                    single_actions = extract_yfinance_actions_df(single_raw, (sym,))
                    if not single_actions.empty:
                        repaired_actions.append(single_actions)
                repair_receipts.append(
                    YFinanceRepairReceipt(
                        symbol=sym,
                        original_outcome=orig_outcome,
                        repair_parameters={
                            "symbol": sym,
                            "mode": "single_symbol_download",
                        },
                        attempts=1,
                        returned_rows=len(single_df),
                        final_status="SUCCESS",
                    )
                )
            else:
                outcomes[sym] = YFinanceSymbolOutcome.RECOVERY_FAILED
                repair_receipts.append(
                    YFinanceRepairReceipt(
                        symbol=sym,
                        original_outcome=orig_outcome,
                        repair_parameters={
                            "symbol": sym,
                            "mode": "single_symbol_download",
                        },
                        attempts=1,
                        returned_rows=len(single_df) if single_df is not None else 0,
                        final_status="FAILED",
                    )
                )
        except (
            ProviderError,
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            outcomes[sym] = YFinanceSymbolOutcome.RECOVERY_FAILED
            repair_receipts.append(
                YFinanceRepairReceipt(
                    symbol=sym,
                    original_outcome=orig_outcome,
                    repair_parameters={"symbol": sym, "error": str(exc)},
                    attempts=1,
                    returned_rows=0,
                    final_status="FAILED",
                )
            )

    if repaired_dfs:
        recovered_symbols = {r.symbol for r in repair_receipts if r.final_status == "SUCCESS"}
        cleaned_base = norm_df[~norm_df["symbol"].isin(recovered_symbols)]
        norm_df = pd.concat([cleaned_base, *repaired_dfs], ignore_index=True)
        norm_df.sort_values(by=["symbol", "date"], inplace=True)
        norm_df.reset_index(drop=True, inplace=True)

        if actions_df is not None:
            cleaned_actions = actions_df[~actions_df["symbol"].isin(recovered_symbols)]
            actions_df = pd.concat([cleaned_actions, *repaired_actions], ignore_index=True)
            if not actions_df.empty:
                actions_df.sort_values(by=["symbol", "date", "action"], inplace=True)
                actions_df.reset_index(drop=True, inplace=True)

    return norm_df, actions_df, repair_receipts

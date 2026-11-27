"""Per-symbol bounded repair helpers for yfinance daily bars."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import pandas as pd

from ohmydata.core.errors import ProviderError, RetryExhaustedError, TransientProviderError
from ohmydata.core.policy import RetryPolicy, execute_with_retry

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
    retry_policy: RetryPolicy | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    random_value_fn: Callable[[], float] | None = None,
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
        attempts_count = 1
        try:
            if retry_policy is not None:

                def _single_download(sym: str = sym) -> Any:
                    try:
                        return download_fn(sym, **download_kwargs)
                    except (TimeoutError, ConnectionError, OSError) as exc:
                        raise TransientProviderError(
                            f"yfinance single-symbol transport error for {sym}: "
                            f"{type(exc).__name__}: {exc}"
                        ) from exc

                retry_res = execute_with_retry(
                    _single_download,
                    policy=retry_policy,
                    sleep=sleep_fn or time.sleep,
                    random_value=random_value_fn or random.random,
                )
                single_raw = retry_res.value
                attempts_count = len(retry_res.attempts)
            else:
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
                        attempts=attempts_count,
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
                        attempts=attempts_count,
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
            if isinstance(exc, RetryExhaustedError):
                attempts_count = len(exc.attempts)
            outcomes[sym] = YFinanceSymbolOutcome.RECOVERY_FAILED
            repair_receipts.append(
                YFinanceRepairReceipt(
                    symbol=sym,
                    original_outcome=orig_outcome,
                    repair_parameters={"symbol": sym, "error": str(exc)},
                    attempts=attempts_count,
                    returned_rows=0,
                    final_status="FAILED",
                )
            )

    if repaired_dfs:
        recovered_symbols = {r.symbol for r in repair_receipts if r.final_status == "SUCCESS"}
        cleaned_base = norm_df[~norm_df["symbol"].isin(recovered_symbols)]
        non_empty_dfs = [f for f in (cleaned_base, *repaired_dfs) if not f.empty]
        if non_empty_dfs:
            norm_df = (
                non_empty_dfs[0].copy()
                if len(non_empty_dfs) == 1
                else pd.concat(non_empty_dfs, ignore_index=True)
            )
            norm_df.sort_values(by=["symbol", "date"], inplace=True)
            norm_df.reset_index(drop=True, inplace=True)

        if actions_df is not None:
            cleaned_actions = actions_df[~actions_df["symbol"].isin(recovered_symbols)]
            non_empty_actions = [f for f in (cleaned_actions, *repaired_actions) if not f.empty]
            if non_empty_actions:
                actions_df = (
                    non_empty_actions[0].copy()
                    if len(non_empty_actions) == 1
                    else pd.concat(non_empty_actions, ignore_index=True)
                )
                actions_df.sort_values(by=["symbol", "date", "action"], inplace=True)
                actions_df.reset_index(drop=True, inplace=True)
            else:
                actions_df = cleaned_actions.iloc[0:0].copy()

    return norm_df, actions_df, repair_receipts

"""Client boundary and high-level orchestrator for yfinance daily bars and fundamentals."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import pandas as pd

from ohmydata.core.errors import CoverageError, ProviderError
from ohmydata.core.policy import AttemptRecord, RetryPolicy, execute_with_retry
from ohmydata.core.provenance import EmptyDisposition, FetchProvenance

from .endpoints import (
    STANDARD_COLUMNS,
    YFinanceAdjustmentMode,
    YFinanceBatchPolicy,
    YFinanceDailyBarsRequest,
    YFinanceDailyBarsResult,
    YFinanceRepairPolicy,
    YFinanceSymbolOutcome,
    normalize_yfinance_download_df,
)
from .errors import (
    YFinanceEmptyBatchError,
    YFinanceRepairReceipt,
    YFinanceVersionMismatchError,
)
from .fundamentals import (
    YFinanceFundamentalsRequest,
    YFinanceFundamentalsResult,
    YFinanceSymbolFundamentals,
    parse_symbol_fundamentals,
)
from .quality import evaluate_symbol_outcomes, validate_daily_bars_dataframe

EXPECTED_YFINANCE_VERSION = "1.7.0"


def assert_yfinance_version(module: Any = None) -> None:
    """Assert that yfinance is installed and matches the reviewed 1.7.0 baseline."""
    if module is None:
        try:
            module = importlib.import_module("yfinance")
        except ImportError as exc:
            raise YFinanceVersionMismatchError(
                "yfinance is not installed. Install with `pip install ohmydata[yfinance]`"
            ) from exc

    actual_version = getattr(module, "__version__", None)
    if actual_version != EXPECTED_YFINANCE_VERSION:
        raise YFinanceVersionMismatchError(
            f"yfinance version mismatch: expected {EXPECTED_YFINANCE_VERSION!r}, got {actual_version!r}. "
            f"OMD is locked against yfinance=={EXPECTED_YFINANCE_VERSION}."
        )


def _safe_get_attr(obj: Any, attr: str) -> Any:
    """Safely get an attribute from a ticker or similar object, returning None if missing or errored."""
    try:
        return getattr(obj, attr, None)
    except (AttributeError, TypeError, ValueError, KeyError, OSError, RuntimeError):
        return None


def _safe_call_or_get(obj: Any, method_name: str, attr_name: str) -> Any:
    """Try calling method_name() if present; otherwise get attr_name."""
    try:
        if hasattr(obj, method_name):
            fn = getattr(obj, method_name)
            if callable(fn):
                return fn()
        if hasattr(obj, attr_name):
            return getattr(obj, attr_name)
    except (AttributeError, TypeError, ValueError, KeyError, OSError, RuntimeError):
        return None
    return None


class YFinanceClient:
    """High-level offline-testable yfinance provider client."""

    def __init__(
        self,
        yf_module: Any | None = None,
        download_fn: Callable[..., Any] | None = None,
        ticker_factory: Callable[[str], Any] | None = None,
        default_retry_policy: RetryPolicy | None = None,
    ):
        if yf_module is not None:
            assert_yfinance_version(yf_module)

        if download_fn is not None:
            self._download_fn: Callable[..., Any] | None = download_fn
        elif yf_module is not None and hasattr(yf_module, "download"):
            self._download_fn = yf_module.download
        else:
            try:
                yf = importlib.import_module("yfinance")
                assert_yfinance_version(yf)
                self._download_fn = yf.download
            except ImportError:
                self._download_fn = None

        if ticker_factory is not None:
            self._ticker_factory: Callable[[str], Any] | None = ticker_factory
        elif yf_module is not None and hasattr(yf_module, "Ticker"):
            self._ticker_factory = yf_module.Ticker
        else:
            try:
                yf = importlib.import_module("yfinance")
                assert_yfinance_version(yf)
                self._ticker_factory = yf.Ticker
            except ImportError:
                self._ticker_factory = None

        self._retry_policy = default_retry_policy or RetryPolicy(3, 1.0, 3.0, 5.0, 0.0)

    def fetch_daily_bars(self, request: YFinanceDailyBarsRequest) -> YFinanceDailyBarsResult:
        """Fetch daily bars for the requested symbols with shape normalization and repair."""
        download_fn = self._download_fn
        if download_fn is None:
            raise YFinanceVersionMismatchError(
                "yfinance is not installed or download_fn was not provided."
            )
        retry_policy = request.retry_policy or self._retry_policy
        attempts_log: list[AttemptRecord] = []

        auto_adjust_arg = request.adjustment_mode == YFinanceAdjustmentMode.AUTO_ADJUSTED
        symbols_arg = list(request.symbols)

        def _do_download() -> pd.DataFrame:
            return download_fn(
                symbols_arg,
                start=request.start_date,
                end=request.end_date_exclusive,
                interval=request.interval,
                auto_adjust=auto_adjust_arg,
                threads=False,
                progress=False,
                timeout=request.timeout,
            )

        try:
            retry_res = execute_with_retry(
                _do_download,
                policy=retry_policy,
            )
            raw_data = retry_res.value
            attempts_log.extend(retry_res.attempts)
        except (ProviderError, OSError, RuntimeError, ValueError, KeyError):
            raw_data = pd.DataFrame()

        # Shape normalization
        norm_df = normalize_yfinance_download_df(raw_data, request.symbols, request.adjustment_mode)

        # Initial validation & outcome evaluation
        validate_daily_bars_dataframe(norm_df, request.adjustment_mode)
        outcomes = evaluate_symbol_outcomes(norm_df, request.symbols, request.adjustment_mode)

        repair_receipts: list[YFinanceRepairReceipt] = []

        # Per-symbol bounded repair if requested
        if request.repair_policy == YFinanceRepairPolicy.PER_SYMBOL:
            failed_symbols = [
                sym for sym, out in outcomes.items() if out != YFinanceSymbolOutcome.COMPLETE
            ]
            if failed_symbols:
                repaired_dfs: list[pd.DataFrame] = []
                for sym in failed_symbols:
                    orig_outcome = outcomes[sym].value
                    try:
                        single_raw = download_fn(
                            sym,
                            start=request.start_date,
                            end=request.end_date_exclusive,
                            interval=request.interval,
                            auto_adjust=auto_adjust_arg,
                            threads=False,
                            progress=False,
                            timeout=request.timeout,
                        )
                        single_df = normalize_yfinance_download_df(
                            single_raw, (sym,), request.adjustment_mode
                        )
                        validate_daily_bars_dataframe(single_df, request.adjustment_mode)
                        single_outcomes = evaluate_symbol_outcomes(
                            single_df, (sym,), request.adjustment_mode
                        )

                        if (
                            single_outcomes.get(sym) == YFinanceSymbolOutcome.COMPLETE
                            and not single_df.empty
                        ):
                            outcomes[sym] = YFinanceSymbolOutcome.RECOVERED
                            repaired_dfs.append(single_df)
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
                    # Remove old partial rows for recovered symbols and merge new ones
                    recovered_symbols = {
                        r.symbol for r in repair_receipts if r.final_status == "SUCCESS"
                    }
                    cleaned_base = norm_df[~norm_df["symbol"].isin(recovered_symbols)]
                    all_dfs = [cleaned_base] + repaired_dfs
                    norm_df = pd.concat(all_dfs, ignore_index=True)
                    norm_df.sort_values(by=["symbol", "date"], inplace=True)
                    norm_df.reset_index(drop=True, inplace=True)

        returned_symbols = tuple(sorted(norm_df["symbol"].unique())) if not norm_df.empty else ()

        # Check strict batch policy
        if request.batch_policy == YFinanceBatchPolicy.STRICT:
            if norm_df.empty:
                raise YFinanceEmptyBatchError(
                    f"yfinance returned empty batch for symbols: {request.symbols}"
                )
            incomplete = [
                sym
                for sym, out in outcomes.items()
                if out not in (YFinanceSymbolOutcome.COMPLETE, YFinanceSymbolOutcome.RECOVERED)
            ]
            if incomplete:
                raise CoverageError(
                    f"Strict batch policy failed: symbols {incomplete} did not complete successfully. "
                    f"Outcomes: {dict(outcomes)}"
                )

        provenance = FetchProvenance(
            provider="yfinance",
            endpoint="daily_bars",
            request_identity=f"yfinance:daily_bars:{','.join(request.symbols)}:{request.start_date}:{request.end_date_exclusive}",
            effective_parameters={
                "symbols": list(request.symbols),
                "start_date": request.start_date,
                "end_date_exclusive": request.end_date_exclusive,
                "interval": request.interval,
                "adjustment_mode": request.adjustment_mode.value,
                "batch_policy": request.batch_policy.value,
                "repair_policy": request.repair_policy.value,
            },
            requested_fields=STANDARD_COLUMNS,
            retrieved_at=datetime.now(UTC),
            attempts=tuple(attempts_log),
            row_count=len(norm_df),
            columns=STANDARD_COLUMNS,
            warnings=(),
            snapshot_identities=(),
            empty_disposition=EmptyDisposition.ALLOWED_EMPTY
            if norm_df.empty
            else EmptyDisposition.NOT_EMPTY,
        )

        return YFinanceDailyBarsResult(
            data=norm_df,
            requested_symbols=request.symbols,
            returned_symbols=returned_symbols,
            start_date=request.start_date,
            end_date_exclusive=request.end_date_exclusive,
            yfinance_version=EXPECTED_YFINANCE_VERSION,
            adjustment_mode=request.adjustment_mode,
            symbol_outcomes=MappingProxyType(outcomes),
            repair_receipts=tuple(repair_receipts),
            provenance=provenance,
        )

    def fetch_fundamentals(
        self, request: YFinanceFundamentalsRequest
    ) -> YFinanceFundamentalsResult:
        """Fetch company fundamentals, valuation multiples, statements, and analyst estimates."""
        ticker_factory = self._ticker_factory
        if ticker_factory is None:
            raise YFinanceVersionMismatchError(
                "yfinance is not installed or ticker_factory was not provided."
            )
        records: dict[str, YFinanceSymbolFundamentals] = {}
        attempts_log: list[AttemptRecord] = []

        for sym in request.symbols:
            ticker = ticker_factory(sym)
            info = getattr(ticker, "info", {})
            if not isinstance(info, dict):
                info = {}

            income_stmt = None
            balance_stmt = None
            cashflow_stmt = None
            if request.include_financials:
                income_stmt = _safe_get_attr(ticker, "quarterly_income_stmt")
                balance_stmt = _safe_get_attr(ticker, "quarterly_balance_sheet")
                cashflow_stmt = _safe_get_attr(ticker, "quarterly_cashflow")

            rev_est_df = None
            eps_est_df = None
            if request.include_estimates:
                rev_est_df = _safe_call_or_get(ticker, "get_revenue_estimate", "revenue_estimate")
                eps_est_df = _safe_call_or_get(ticker, "get_earnings_estimate", "earnings_estimate")

            fast_info = _safe_get_attr(ticker, "fast_info")

            fund_record = parse_symbol_fundamentals(
                symbol=sym,
                info=info,
                income_stmt=income_stmt,
                balance_stmt=balance_stmt,
                cashflow_stmt=cashflow_stmt,
                rev_estimate_df=rev_est_df,
                eps_estimate_df=eps_est_df,
                fast_info=fast_info,
            )
            records[sym] = fund_record

        provenance = FetchProvenance(
            provider="yfinance",
            endpoint="fundamentals",
            request_identity=f"yfinance:fundamentals:{','.join(request.symbols)}",
            effective_parameters={
                "symbols": list(request.symbols),
                "include_financials": request.include_financials,
                "include_valuation": request.include_valuation,
                "include_estimates": request.include_estimates,
            },
            requested_fields=("symbol", "report_date", "valuation", "financials", "estimates"),
            retrieved_at=datetime.now(UTC),
            attempts=tuple(attempts_log),
            row_count=len(records),
            columns=("symbol", "report_date", "quote_type", "valuation", "financials", "estimates"),
            warnings=(),
            snapshot_identities=(),
            empty_disposition=EmptyDisposition.ALLOWED_EMPTY
            if not records
            else EmptyDisposition.NOT_EMPTY,
        )

        return YFinanceFundamentalsResult(
            records=MappingProxyType(records),
            requested_symbols=request.symbols,
            yfinance_version=EXPECTED_YFINANCE_VERSION,
            provenance=provenance,
        )

"""Client boundary and high-level orchestrator for yfinance daily bars and fundamentals."""

from __future__ import annotations

import importlib
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pandas as pd

from ohmydata.core.errors import CoverageError, ProviderError
from ohmydata.core.policy import AttemptRecord, RetryPolicy, execute_with_retry
from ohmydata.core.provenance import EmptyDisposition, FetchProvenance

from ._fundamentals_reads import call_or_get, get_attribute, has_material_data, read_source
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
    YFinanceFundamentalsOutcome,
    YFinanceFundamentalsRequest,
    YFinanceFundamentalsResult,
    YFinanceFundamentalsSourceError,
    YFinanceFundamentalsSourceResult,
    YFinanceFundamentalsSourceStatus,
    YFinanceFundamentalsSymbolResult,
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


class YFinanceClient:
    """High-level offline-testable yfinance provider client."""

    def __init__(
        self,
        yf_module: Any | None = None,
        download_fn: Callable[..., Any] | None = None,
        ticker_factory: Callable[[str], Any] | None = None,
        default_retry_policy: RetryPolicy | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        random_value_fn: Callable[[], float] | None = None,
        clock: Callable[[], datetime] | None = None,
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
        self._sleep = sleep_fn or time.sleep
        self._random_value = random_value_fn or random.random
        self._clock = clock or (lambda: datetime.now(UTC))

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
                sleep=self._sleep,
                random_value=self._random_value,
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
            retrieved_at=self._clock(),
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
        symbol_results: dict[str, YFinanceFundamentalsSymbolResult] = {}
        attempts_log: list[AttemptRecord] = []
        retry_policy = request.retry_policy or self._retry_policy

        for sym in request.symbols:
            sources: dict[str, YFinanceFundamentalsSourceResult] = {}

            def read(
                name: str,
                fn: Callable[[], Any],
                *,
                _sources: dict[str, YFinanceFundamentalsSourceResult] = sources,
                **kwargs: Any,
            ) -> Any:
                value, evidence = read_source(
                    name,
                    fn,
                    retry_policy,
                    sleep=self._sleep,
                    random_value=self._random_value,
                    **kwargs,
                )
                _sources[name] = evidence
                attempts_log.extend(evidence.attempts)
                return value

            ticker = read("ticker", lambda sym=sym: ticker_factory(sym), require_value=True)
            if ticker is None:
                status = sources["ticker"].status
                outcome = (
                    YFinanceFundamentalsOutcome.TRANSIENT_FAILURE
                    if status == YFinanceFundamentalsSourceStatus.TRANSIENT_FAILURE
                    else YFinanceFundamentalsOutcome.PERMANENT_FAILURE
                )
                symbol_results[sym] = YFinanceFundamentalsSymbolResult(
                    sym, outcome, None, MappingProxyType(sources)
                )
                continue

            def get_info(ticker: Any = ticker) -> Any:
                value = get_attribute(ticker, "info")
                return {} if value is None else value

            info = read("info", get_info, require_dict=True)
            income_stmt = balance_stmt = cashflow_stmt = None
            if request.include_financials:
                income_stmt = read(
                    "quarterly_income_stmt",
                    lambda ticker=ticker: get_attribute(ticker, "quarterly_income_stmt"),
                )
                balance_stmt = read(
                    "quarterly_balance_sheet",
                    lambda ticker=ticker: get_attribute(ticker, "quarterly_balance_sheet"),
                )
                cashflow_stmt = read(
                    "quarterly_cashflow",
                    lambda ticker=ticker: get_attribute(ticker, "quarterly_cashflow"),
                )

            rev_est_df = eps_est_df = None
            if request.include_estimates:
                rev_est_df = read(
                    "revenue_estimate",
                    lambda ticker=ticker: call_or_get(
                        ticker, "get_revenue_estimate", "revenue_estimate"
                    ),
                )
                eps_est_df = read(
                    "earnings_estimate",
                    lambda ticker=ticker: call_or_get(
                        ticker, "get_earnings_estimate", "earnings_estimate"
                    ),
                )

            fast_info_raw = read(
                "fast_info", lambda ticker=ticker: get_attribute(ticker, "fast_info")
            )
            fast_info = None
            if fast_info_raw is not None:
                fast_info = SimpleNamespace(
                    market_cap=read(
                        "fast_info.market_cap",
                        lambda raw=fast_info_raw: get_attribute(raw, "market_cap"),
                    ),
                    shares=read(
                        "fast_info.shares",
                        lambda raw=fast_info_raw: get_attribute(raw, "shares"),
                    ),
                )

            try:
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
            except Exception as exc:  # noqa: BLE001 - isolate malformed provider payloads by symbol
                attempts = (AttemptRecord(1, type(exc).__name__, None),)
                status = YFinanceFundamentalsSourceStatus.PERMANENT_FAILURE
                error = YFinanceFundamentalsSourceError(
                    "parse", status, type(exc).__name__, attempts
                )
                sources["parse"] = YFinanceFundamentalsSourceResult(status, attempts, error)
                attempts_log.extend(attempts)
                symbol_results[sym] = YFinanceFundamentalsSymbolResult(
                    sym,
                    YFinanceFundamentalsOutcome.PERMANENT_FAILURE,
                    None,
                    MappingProxyType(sources),
                )
                continue

            failures = [source.status for source in sources.values() if source.error is not None]
            if not has_material_data(fund_record):
                if YFinanceFundamentalsSourceStatus.PERMANENT_FAILURE in failures:
                    outcome = YFinanceFundamentalsOutcome.PERMANENT_FAILURE
                elif failures:
                    outcome = YFinanceFundamentalsOutcome.TRANSIENT_FAILURE
                else:
                    outcome = YFinanceFundamentalsOutcome.UNAVAILABLE
                fund_record = None
            elif failures or any(
                source.status == YFinanceFundamentalsSourceStatus.EMPTY
                for name, source in sources.items()
                if name not in {"fast_info", "fast_info.market_cap", "fast_info.shares"}
            ):
                outcome = YFinanceFundamentalsOutcome.INCOMPLETE
            else:
                outcome = YFinanceFundamentalsOutcome.COMPLETE

            if fund_record is not None:
                records[sym] = fund_record
            symbol_results[sym] = YFinanceFundamentalsSymbolResult(
                sym, outcome, fund_record, MappingProxyType(sources)
            )

        provenance = FetchProvenance(
            provider="yfinance",
            endpoint="fundamentals",
            request_identity=(
                f"yfinance:fundamentals:{','.join(request.symbols)}:"
                f"{int(request.include_financials)}:{int(request.include_valuation)}:"
                f"{int(request.include_estimates)}"
            ),
            effective_parameters={
                "symbols": list(request.symbols),
                "include_financials": request.include_financials,
                "include_valuation": request.include_valuation,
                "include_estimates": request.include_estimates,
                "max_attempts_per_source": retry_policy.max_attempts,
            },
            requested_fields=("symbol", "report_date", "valuation", "financials", "estimates"),
            retrieved_at=self._clock(),
            attempts=tuple(attempts_log),
            row_count=len(records),
            columns=("symbol", "report_date", "quote_type", "valuation", "financials", "estimates"),
            warnings=tuple(
                f"{sym}:{result.outcome.value}"
                for sym, result in symbol_results.items()
                if result.outcome != YFinanceFundamentalsOutcome.COMPLETE
            ),
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
            symbol_results=MappingProxyType(symbol_results),
        )

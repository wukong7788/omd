"""Offline, single-core Polars EOD market-data quality control supporting Polars and Pandas."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from ._eod_qc_corrections import apply_corrections_work
from ._eod_qc_detectors import (
    detect_non_positive_prices_work,
    detect_ohlc_violations_work,
    detect_oscillating_currency_jumps_work,
    detect_reverse_split_dividend_mismatch_work,
    detect_trailing_staleness_work,
    detect_zero_volume_work,
)
from ._eod_qc_prep import (
    QualityCategory,
    QualitySeverity,
    norm_date_str,
    prepare_actions,
    prepare_bars,
    require_polars,
)
from ._eod_qc_splits import detect_suspect_missing_splits_work

if TYPE_CHECKING:
    import pandas as pd
    import polars as pl

    DataFrameInput = pd.DataFrame | pl.DataFrame
else:
    DataFrameInput = Any


class EodAdjustmentBasis(StrEnum):
    """Explicit price adjustment basis declared for an EOD bars frame."""

    SPLIT_ADJUSTED_WITH_ADJ_CLOSE = "SPLIT_ADJUSTED_WITH_ADJ_CLOSE"
    UNADJUSTED_WITH_ADJ_CLOSE = "UNADJUSTED_WITH_ADJ_CLOSE"
    AUTO_ADJUSTED = "AUTO_ADJUSTED"


def _freeze_mapping(raw: MappingProxyType[str, Any] | dict[str, Any]) -> MappingProxyType[str, Any]:
    return raw if isinstance(raw, MappingProxyType) else MappingProxyType(dict(raw))


@dataclass(frozen=True)
class QualityFinding:
    """Single quality control finding for a symbol/date observation."""

    symbol: str
    date: str
    category: QualityCategory
    severity: QualitySeverity
    message: str
    metrics: MappingProxyType[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", str(self.symbol))
        object.__setattr__(self, "date", norm_date_str(self.date))
        object.__setattr__(self, "metrics", _freeze_mapping(self.metrics))

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "date": self.date,
            "category": self.category.value,
            "severity": self.severity.value,
            "message": self.message,
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True)
class CorrectionRecord:
    """Explicit, versioned data correction overlay entry."""

    symbol: str
    date: str
    field: str
    raw_value: Any
    corrected_value: Any
    reason: str
    evidence_source: str
    version: str = "1.0.0"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", str(self.symbol))
        object.__setattr__(self, "date", norm_date_str(self.date))


@dataclass(frozen=True)
class CorrectionReceipt:
    """Audit receipt emitted for each attempted CorrectionRecord application."""

    symbol: str
    date: str
    field: str
    raw_value: Any
    corrected_value: Any
    reason: str
    evidence_source: str
    version: str
    applied: bool
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "date": self.date,
            "field": self.field,
            "raw_value": self.raw_value,
            "corrected_value": self.corrected_value,
            "reason": self.reason,
            "evidence_source": self.evidence_source,
            "version": self.version,
            "applied": self.applied,
            "status": self.status,
        }


@dataclass(frozen=True)
class EodQcReport:
    """Aggregated EOD market-data quality control report."""

    provider: str
    adjustment_basis: EodAdjustmentBasis
    status: str
    total_rows: int
    symbol_count: int
    findings: tuple[QualityFinding, ...]
    summary_counts: MappingProxyType[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(self.findings))
        if not isinstance(self.summary_counts, MappingProxyType):
            object.__setattr__(self, "summary_counts", MappingProxyType(dict(self.summary_counts)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "adjustment_basis": self.adjustment_basis.value,
            "status": self.status,
            "total_rows": self.total_rows,
            "symbol_count": self.symbol_count,
            "summary_counts": dict(self.summary_counts),
            "findings": [f.to_dict() for f in self.findings],
        }

    def to_markdown(self) -> str:
        err_c = self.summary_counts.get("ERROR", 0)
        warn_c = self.summary_counts.get("WARNING", 0)
        info_c = self.summary_counts.get("INFO", 0)
        lines = [
            f"# EOD Quality Control Report ({self.provider})",
            "",
            f"- **Status**: **{self.status}**",
            f"- **Adjustment Basis**: `{self.adjustment_basis.value}`",
            f"- **Total Rows**: {self.total_rows} | **Symbols**: {self.symbol_count}",
            f"- **Findings**: {len(self.findings)} (ERROR={err_c}, WARNING={warn_c}, INFO={info_c})",
            "",
            "| Category | Count |",
            "| :--- | :---: |",
            *(
                f"| `{cat.value}` | {self.summary_counts.get(cat.value, 0)} |"
                for cat in QualityCategory
            ),
        ]
        if self.findings:
            lines.extend(
                [
                    "",
                    "| Symbol | Date | Severity | Category | Message |",
                    "| :--- | :---: | :---: | :--- | :--- |",
                ]
            )
            for f in self.findings[:50]:
                lines.append(
                    f"| `{f.symbol}` | `{f.date}` | **{f.severity.value}** | "
                    f"`{f.category.value}` | {f.message} |"
                )
        return "\n".join(lines)


def apply_corrections(
    df: DataFrameInput,
    records: Sequence[CorrectionRecord] | Iterable[CorrectionRecord],
) -> tuple[Any, list[CorrectionReceipt]]:
    """Apply explicit CorrectionRecords to a copy of a Polars or Pandas DataFrame."""
    return apply_corrections_work(df, records, CorrectionReceipt)


def detect_non_positive_prices(bars_df: DataFrameInput) -> list[QualityFinding]:
    """Flag rows with <= 0 or non-finite values in open/high/low/close/adj_close."""
    return detect_non_positive_prices_work(prepare_bars(bars_df), QualityFinding)


def detect_ohlc_violations(
    bars_df: DataFrameInput, ulp_rel_tol: float = 1e-9
) -> list[QualityFinding]:
    """Flag rows violating high >= max(open, close) >= min(open, close) >= low."""
    return detect_ohlc_violations_work(prepare_bars(bars_df), ulp_rel_tol, QualityFinding)


def detect_suspect_missing_splits(
    bars_df: DataFrameInput,
    actions_df: DataFrameInput | None = None,
    jump_threshold: float = 0.15,
    *,
    adjustment_basis: EodAdjustmentBasis = EodAdjustmentBasis.SPLIT_ADJUSTED_WITH_ADJ_CLOSE,
    error_jump_threshold: float | None = None,
) -> list[QualityFinding]:
    """Flag >= jump_threshold close/gap moves lacking a +/-3d split or distribution explanation."""
    return detect_suspect_missing_splits_work(
        prepare_bars(bars_df),
        prepare_actions(actions_df),
        jump_threshold,
        QualityFinding,
        adjustment_basis=adjustment_basis,
        error_jump_threshold=error_jump_threshold,
    )


def detect_oscillating_currency_jumps(
    bars_df: DataFrameInput,
    jump_threshold: float = 0.20,
    max_gap_days: int = 3,
) -> list[QualityFinding]:
    """Flag consecutive opposing jumps of comparable log-magnitude within max_gap_days."""
    return detect_oscillating_currency_jumps_work(
        prepare_bars(bars_df), jump_threshold, max_gap_days, QualityFinding
    )


def detect_reverse_split_dividend_mismatch(
    bars_df: DataFrameInput,
    actions_df: DataFrameInput | None,
    tolerance: float = 0.10,
) -> list[QualityFinding]:
    """Flag pre-split adj_close vs dividend return distortions on symbols with <1.0 splits."""
    return detect_reverse_split_dividend_mismatch_work(
        prepare_bars(bars_df), prepare_actions(actions_df), tolerance, QualityFinding
    )


def detect_trailing_staleness(
    bars_df: DataFrameInput,
    expected_latest_date: str | date | datetime | None,
    max_lag_days: int = 2,
    active_symbols: Iterable[str] | None = None,
) -> list[QualityFinding]:
    """Flag symbols whose latest bar lags expected_latest_date by more than max_lag_days."""
    return detect_trailing_staleness_work(
        prepare_bars(bars_df), expected_latest_date, max_lag_days, active_symbols, QualityFinding
    )


def detect_zero_volume(bars_df: DataFrameInput) -> list[QualityFinding]:
    """Flag rows with zero, negative, or non-finite volume."""
    return detect_zero_volume_work(prepare_bars(bars_df), QualityFinding)


def run_eod_qc(
    bars_df: DataFrameInput,
    *,
    actions_df: DataFrameInput | None = None,
    provider: str = "unknown",
    adjustment_basis: EodAdjustmentBasis = EodAdjustmentBasis.SPLIT_ADJUSTED_WITH_ADJ_CLOSE,
    expected_latest_date: str | date | datetime | None = None,
    active_symbols: Iterable[str] | None = None,
    skip_corporate_action_symbols: Iterable[str] | None = None,
    jump_threshold: float = 0.15,
    error_jump_threshold: float | None = None,
    currency_jump_threshold: float = 0.20,
    currency_max_gap_days: int = 3,
    reverse_split_tolerance: float = 0.10,
    max_lag_days: int = 2,
    ulp_rel_tol: float = 1e-9,
) -> EodQcReport:
    """Run all vectorized Polars EOD quality control detectors and assemble an EodQcReport."""
    work = prepare_bars(bars_df)
    act = prepare_actions(actions_df)
    skip_ca_set = (
        {str(s) for s in skip_corporate_action_symbols}
        if skip_corporate_action_symbols is not None
        else set()
    )
    if skip_ca_set and not work.is_empty():
        pl_mod = require_polars()
        ca_work = work.filter(~pl_mod.col("_symbol").is_in(sorted(skip_ca_set)))
    else:
        ca_work = work

    findings: list[QualityFinding] = [
        *detect_non_positive_prices_work(work, QualityFinding),
        *detect_ohlc_violations_work(work, ulp_rel_tol, QualityFinding),
        *detect_suspect_missing_splits_work(
            ca_work,
            act,
            jump_threshold,
            QualityFinding,
            adjustment_basis=adjustment_basis,
            error_jump_threshold=error_jump_threshold,
        ),
        *detect_oscillating_currency_jumps_work(
            ca_work, currency_jump_threshold, currency_max_gap_days, QualityFinding
        ),
        *detect_reverse_split_dividend_mismatch_work(
            ca_work, act, reverse_split_tolerance, QualityFinding
        ),
        *detect_trailing_staleness_work(
            work, expected_latest_date, max_lag_days, active_symbols, QualityFinding
        ),
        *detect_zero_volume_work(ca_work, QualityFinding),
    ]
    counts: dict[str, int] = {sev.value: 0 for sev in QualitySeverity}
    counts.update({cat.value: 0 for cat in QualityCategory})
    for f in findings:
        counts[f.severity.value] += 1
        counts[f.category.value] += 1
    status = (
        "BLOCKED_DATA" if counts["ERROR"] > 0 else ("WARN" if counts["WARNING"] > 0 else "PASSED")
    )
    return EodQcReport(
        provider=provider,
        adjustment_basis=adjustment_basis,
        status=status,
        total_rows=0 if bars_df is None else len(bars_df),
        symbol_count=int(work["_symbol"].n_unique()) if not work.is_empty() else 0,
        findings=tuple(findings),
        summary_counts=MappingProxyType(counts),
    )

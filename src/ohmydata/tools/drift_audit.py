"""Zero-drift audit engine for cross-version and cross-provider market data validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import numpy as np
import pandas as pd

from ohmydata.providers.yfinance.endpoints import STANDARD_COLUMNS

NUMERIC_COLUMNS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
)


@dataclass(frozen=True)
class ColumnDriftMetrics:
    """Drift metrics for an individual column."""

    column: str
    diff_count: int
    max_abs_diff: float
    max_rel_diff: float
    mean_abs_diff: float


@dataclass(frozen=True)
class RowDriftRecord:
    """Individual bar mismatch record."""

    symbol: str
    date: str
    column: str
    baseline_value: float | None
    target_value: float | None
    abs_diff: float
    rel_diff: float


@dataclass(frozen=True)
class SymbolDriftReport:
    """Drift analysis report for a single symbol."""

    symbol: str
    baseline_rows: int
    target_rows: int
    common_dates_count: int
    missing_in_target: tuple[str, ...]
    extra_in_target: tuple[str, ...]
    column_metrics: MappingProxyType[str, ColumnDriftMetrics]
    mismatches: tuple[RowDriftRecord, ...]
    is_zero_drift: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "baseline_rows": self.baseline_rows,
            "target_rows": self.target_rows,
            "common_dates_count": self.common_dates_count,
            "missing_in_target": list(self.missing_in_target),
            "extra_in_target": list(self.extra_in_target),
            "column_metrics": {k: asdict(v) for k, v in self.column_metrics.items()},
            "mismatches_count": len(self.mismatches),
            "is_zero_drift": self.is_zero_drift,
        }


@dataclass(frozen=True)
class UniverseDriftReport:
    """Audit report for an entire universe of symbols."""

    universe_name: str
    start_date: str
    end_date: str
    generated_at: str
    symbol_reports: MappingProxyType[str, SymbolDriftReport]
    total_symbols: int
    zero_drift_symbols: int
    drifted_symbols: int
    is_all_zero_drift: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "universe_name": self.universe_name,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "generated_at": self.generated_at,
            "total_symbols": self.total_symbols,
            "zero_drift_symbols": self.zero_drift_symbols,
            "drifted_symbols": self.drifted_symbols,
            "is_all_zero_drift": self.is_all_zero_drift,
            "symbol_reports": {k: v.to_dict() for k, v in self.symbol_reports.items()},
        }

    def to_markdown(self) -> str:
        """Format report into a GitHub-flavored markdown document."""
        lines: list[str] = [
            f"# Zero-Drift Audit Report: {self.universe_name.upper()}",
            "",
            f"- **Generated At**: {self.generated_at}",
            f"- **Date Range**: `{self.start_date}` to `{self.end_date}`",
            f"- **Overall Status**: **{'PASS (0 Drift)' if self.is_all_zero_drift else 'DRIFT DETECTED'}**",
            f"- **Summary**: {self.zero_drift_symbols}/{self.total_symbols} symbols passed bit-exact zero drift.",
            "",
            "## 1. Symbol Summary Table",
            "",
            "| Symbol | Baseline Rows | Target Rows | Missing Dates | Extra Dates | Max Abs Diff | Max Rel Diff | Status |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
        ]

        for sym, rep in sorted(self.symbol_reports.items()):
            max_abs = max((m.max_abs_diff for m in rep.column_metrics.values()), default=0.0)
            max_rel = max((m.max_rel_diff for m in rep.column_metrics.values()), default=0.0)
            status = "PASS" if rep.is_zero_drift else "DRIFT"
            lines.append(
                f"| `{sym}` | {rep.baseline_rows} | {rep.target_rows} | {len(rep.missing_in_target)} "
                f"| {len(rep.extra_in_target)} | `{max_abs:.6g}` | `{max_rel:.4%}` | **{status}** |"
            )

        lines.append("")
        if not self.is_all_zero_drift:
            lines.append("## 2. Drift Detail Breakdown")
            lines.append("")
            for sym, rep in sorted(self.symbol_reports.items()):
                if rep.is_zero_drift:
                    continue
                lines.append(f"### `{sym}` Mismatches")
                if rep.missing_in_target:
                    lines.append(
                        f"- **Missing Dates in Target ({len(rep.missing_in_target)})**: {list(rep.missing_in_target[:10])}"
                    )
                if rep.extra_in_target:
                    lines.append(
                        f"- **Extra Dates in Target ({len(rep.extra_in_target)})**: {list(rep.extra_in_target[:10])}"
                    )
                if rep.mismatches:
                    lines.append(
                        f"- **Sample Numeric Mismatches (showing up to 10 of {len(rep.mismatches)})**:"
                    )
                    lines.append("")
                    lines.append("| Date | Column | Baseline | Target | Abs Diff | Rel Diff |")
                    lines.append("| :--- | :--- | :---: | :---: | :---: | :---: |")
                    for m in rep.mismatches[:10]:
                        lines.append(
                            f"| `{m.date}` | `{m.column}` | {m.baseline_value} | {m.target_value} "
                            f"| `{m.abs_diff:.6g}` | `{m.rel_diff:.4%}` |"
                        )
                    lines.append("")
        else:
            lines.append("## 2. Audit Conclusion")
            lines.append("")
            lines.append(
                "All daily bars across all symbols match **bit-for-bit** across all open, high, low, close, adj_close, and volume observations."
            )

        return "\n".join(lines)


def audit_symbol_drift(
    symbol: str,
    baseline_df: pd.DataFrame,
    target_df: pd.DataFrame,
    abs_tolerance: float = 1e-6,
    rel_tolerance: float = 1e-6,
    columns_to_check: tuple[str, ...] | None = None,
) -> SymbolDriftReport:
    """Compare daily bars for a single symbol between baseline and target DataFrames."""
    active_columns = columns_to_check or NUMERIC_COLUMNS
    b_df = (
        baseline_df.copy()
        if not baseline_df.empty
        else pd.DataFrame(columns=list(STANDARD_COLUMNS))
    )
    t_df = target_df.copy() if not target_df.empty else pd.DataFrame(columns=list(STANDARD_COLUMNS))

    # Normalize date formats to YYYY-MM-DD strings
    if "date" in b_df.columns and not b_df.empty:
        b_df["date"] = pd.to_datetime(b_df["date"]).dt.strftime("%Y-%m-%d")
    if "date" in t_df.columns and not t_df.empty:
        t_df["date"] = pd.to_datetime(t_df["date"]).dt.strftime("%Y-%m-%d")

    b_dates = set(b_df["date"].dropna().unique()) if "date" in b_df.columns else set()
    t_dates = set(t_df["date"].dropna().unique()) if "date" in t_df.columns else set()

    missing_in_target = tuple(sorted(b_dates - t_dates))
    extra_in_target = tuple(sorted(t_dates - b_dates))
    common_dates = sorted(b_dates & t_dates)

    col_metrics: dict[str, ColumnDriftMetrics] = {}
    mismatches: list[RowDriftRecord] = []

    if common_dates:
        b_sub = b_df[b_df["date"].isin(common_dates)].sort_values("date").reset_index(drop=True)
        t_sub = t_df[t_df["date"].isin(common_dates)].sort_values("date").reset_index(drop=True)

        for col in active_columns:
            if col not in b_sub.columns or col not in t_sub.columns:
                col_metrics[col] = ColumnDriftMetrics(
                    column=col,
                    diff_count=0,
                    max_abs_diff=0.0,
                    max_rel_diff=0.0,
                    mean_abs_diff=0.0,
                )
                continue

            b_vals = pd.to_numeric(b_sub[col], errors="coerce").fillna(0.0).to_numpy()
            t_vals = pd.to_numeric(t_sub[col], errors="coerce").fillna(0.0).to_numpy()

            abs_diff = np.abs(b_vals - t_vals)
            denom = np.where(np.abs(b_vals) > 1e-12, np.abs(b_vals), 1.0)
            rel_diff = abs_diff / denom

            diff_mask = (abs_diff > abs_tolerance) & (rel_diff > rel_tolerance)
            diff_indices = np.where(diff_mask)[0]

            diff_count = len(diff_indices)
            max_abs = float(np.max(abs_diff)) if len(abs_diff) > 0 else 0.0
            max_rel = float(np.max(rel_diff[diff_mask])) if diff_count > 0 else 0.0
            mean_abs = float(np.mean(abs_diff)) if len(abs_diff) > 0 else 0.0

            col_metrics[col] = ColumnDriftMetrics(
                column=col,
                diff_count=diff_count,
                max_abs_diff=max_abs,
                max_rel_diff=max_rel,
                mean_abs_diff=mean_abs,
            )

            for idx in diff_indices:
                row_date = b_sub.at[idx, "date"]
                bv = float(b_vals[idx])
                tv = float(t_vals[idx])
                mismatches.append(
                    RowDriftRecord(
                        symbol=symbol,
                        date=str(row_date),
                        column=col,
                        baseline_value=bv,
                        target_value=tv,
                        abs_diff=float(abs_diff[idx]),
                        rel_diff=float(rel_diff[idx]),
                    )
                )
    else:
        for col in active_columns:
            col_metrics[col] = ColumnDriftMetrics(
                column=col,
                diff_count=0,
                max_abs_diff=0.0,
                max_rel_diff=0.0,
                mean_abs_diff=0.0,
            )

    total_diffs = sum(m.diff_count for m in col_metrics.values())
    is_zero_drift = (
        (len(missing_in_target) == 0) and (len(extra_in_target) == 0) and (total_diffs == 0)
    )

    return SymbolDriftReport(
        symbol=symbol,
        baseline_rows=len(b_df),
        target_rows=len(t_df),
        common_dates_count=len(common_dates),
        missing_in_target=missing_in_target,
        extra_in_target=extra_in_target,
        column_metrics=MappingProxyType(col_metrics),
        mismatches=tuple(mismatches),
        is_zero_drift=is_zero_drift,
    )


def audit_universe_drift(
    baseline_data: dict[str, pd.DataFrame],
    target_data: dict[str, pd.DataFrame],
    universe_name: str = "custom",
    start_date: str = "",
    end_date: str = "",
    abs_tolerance: float = 1e-6,
    rel_tolerance: float = 1e-6,
    columns_to_check: tuple[str, ...] | None = None,
) -> UniverseDriftReport:
    """Run drift audit across a collection of symbol DataFrames."""
    all_symbols = sorted(set(baseline_data.keys()) | set(target_data.keys()))
    symbol_reports: dict[str, SymbolDriftReport] = {}

    zero_count = 0
    drift_count = 0

    for sym in all_symbols:
        b_df = baseline_data.get(sym, pd.DataFrame())
        t_df = target_data.get(sym, pd.DataFrame())
        rep = audit_symbol_drift(
            symbol=sym,
            baseline_df=b_df,
            target_df=t_df,
            abs_tolerance=abs_tolerance,
            rel_tolerance=rel_tolerance,
            columns_to_check=columns_to_check,
        )
        symbol_reports[sym] = rep
        if rep.is_zero_drift:
            zero_count += 1
        else:
            drift_count += 1

    return UniverseDriftReport(
        universe_name=universe_name,
        start_date=start_date,
        end_date=end_date,
        generated_at=datetime.now(UTC).isoformat(),
        symbol_reports=MappingProxyType(symbol_reports),
        total_symbols=len(all_symbols),
        zero_drift_symbols=zero_count,
        drifted_symbols=drift_count,
        is_all_zero_drift=(drift_count == 0 and len(all_symbols) > 0),
    )

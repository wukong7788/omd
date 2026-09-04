"""CLI handler for OMD developer and audit tools."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from .drift_audit import audit_universe_drift
from .universes import get_universe


def add_audit_commands(subparsers: argparse._SubParsersAction) -> None:
    """Register audit subcommands into top-level parser."""
    parser = subparsers.add_parser(
        "audit-drift",
        help="Run zero-drift audit across two daily bar datasets or directories",
    )
    parser.add_argument(
        "--universe",
        type=str,
        default="r10a0",
        help="Predefined universe name (e.g. 'r10a0')",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="",
        help="Comma-separated symbols list (overrides --universe)",
    )
    parser.add_argument(
        "--baseline-dir",
        type=str,
        required=True,
        help="Path to baseline data directory (containing <symbol>.parquet or <symbol>.csv)",
    )
    parser.add_argument(
        "--target-dir",
        type=str,
        required=True,
        help="Path to target data directory (containing <symbol>.parquet or <symbol>.csv)",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default="",
        help="Optional start date filter (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default="",
        help="Optional end date filter (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--output-md",
        type=str,
        default="",
        help="Optional path to output markdown report",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default="",
        help="Optional path to output JSON report",
    )
    parser.add_argument(
        "--abs-tolerance",
        type=float,
        default=1e-6,
        help="Absolute tolerance for zero-drift comparison (default: 1e-6)",
    )
    parser.add_argument(
        "--rel-tolerance",
        type=float,
        default=1e-6,
        help="Relative tolerance for zero-drift comparison (default: 1e-6)",
    )
    parser.add_argument(
        "--raw-only",
        action="store_true",
        help="Audit raw OHLCV only (excluding adj_close)",
    )


def _load_symbol_df(folder: Path, symbol: str) -> pd.DataFrame:
    pq_path = folder / f"{symbol}.parquet"
    if pq_path.exists():
        return pd.read_parquet(pq_path)
    csv_path = folder / f"{symbol}.csv"
    if csv_path.exists():
        return pd.read_csv(csv_path)
    return pd.DataFrame()


def run_audit(args: argparse.Namespace) -> int:
    """Execute audit-drift command."""
    if args.symbols.strip():
        symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
        universe_name = "custom"
    else:
        universe = get_universe(args.universe)
        symbols = universe.all_symbols
        universe_name = universe.name

    b_dir = Path(args.baseline_dir)
    t_dir = Path(args.target_dir)

    if not b_dir.exists():
        print(f"Error: Baseline directory not found: {b_dir}", file=sys.stderr)
        return 2
    if not t_dir.exists():
        print(f"Error: Target directory not found: {t_dir}", file=sys.stderr)
        return 2

    baseline_data: dict[str, pd.DataFrame] = {}
    target_data: dict[str, pd.DataFrame] = {}

    for sym in symbols:
        b_df = _load_symbol_df(b_dir, sym)
        t_df = _load_symbol_df(t_dir, sym)
        if args.start_date:
            if not b_df.empty and "date" in b_df.columns:
                b_df = b_df[b_df["date"] >= args.start_date]
            if not t_df.empty and "date" in t_df.columns:
                t_df = t_df[t_df["date"] >= args.start_date]
        if args.end_date:
            if not b_df.empty and "date" in b_df.columns:
                b_df = b_df[b_df["date"] < args.end_date]
            if not t_df.empty and "date" in t_df.columns:
                t_df = t_df[t_df["date"] < args.end_date]
        baseline_data[sym] = b_df
        target_data[sym] = t_df

    columns_to_check = ("open", "high", "low", "close", "volume") if args.raw_only else None

    report = audit_universe_drift(
        baseline_data=baseline_data,
        target_data=target_data,
        universe_name=universe_name,
        start_date=args.start_date,
        end_date=args.end_date,
        abs_tolerance=args.abs_tolerance,
        rel_tolerance=args.rel_tolerance,
        columns_to_check=columns_to_check,
    )

    md = report.to_markdown()
    print(md)

    if args.output_md:
        out_path = Path(args.output_md)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md, encoding="utf-8")
        print(f"\nMarkdown report written to: {out_path}")

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print(f"JSON report written to: {out_path}")

    return 0 if report.is_all_zero_drift else 1

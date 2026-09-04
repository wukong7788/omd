"""Unit tests for the drift audit engine, universes, and CLI."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ohmydata.cli import main
from ohmydata.tools.drift_audit import (
    audit_symbol_drift,
    audit_universe_drift,
)
from ohmydata.tools.universes import get_universe


def test_r10a0_universe_specification():
    u = get_universe("r10a0")
    assert u.name == "r10a0"
    assert len(u.all_symbols) == 13
    assert set(u.all_symbols) == {
        "SPY",
        "QQQ",
        "XLK",
        "IWM",
        "SMH",
        "XLF",
        "XLE",
        "XLV",
        "TLT",
        "GLD",
        "USMV",
        "SHY",
        "IEF",
    }

    cluster_names = [c.name for c in u.clusters]
    assert cluster_names == ["equity_risk", "sector_cyclicals", "defensive"]

    # Verify cluster contents
    eq = next(c for c in u.clusters if c.name == "equity_risk")
    assert eq.symbols == ("SPY", "QQQ", "XLK", "IWM", "SMH")
    assert eq.max_select == 1

    sec = next(c for c in u.clusters if c.name == "sector_cyclicals")
    assert sec.symbols == ("XLF", "XLE", "XLV")

    defn = next(c for c in u.clusters if c.name == "defensive")
    assert defn.symbols == ("TLT", "GLD", "USMV")

    # Verify regime pools
    r_on = next(p for p in u.regime_pools if p.name == "risk_on")
    assert len(r_on.symbols) == 11
    r_off = next(p for p in u.regime_pools if p.name == "risk_off")
    assert r_off.symbols == ("SHY", "IEF", "GLD")
    assert r_off.select_count == 2


def test_audit_symbol_drift_perfect_match():
    dates = ["2026-01-02", "2026-01-05", "2026-01-06"]
    df = pd.DataFrame(
        {
            "symbol": ["SPY"] * 3,
            "date": dates,
            "open": [500.0, 502.0, 501.0],
            "high": [505.0, 506.0, 504.0],
            "low": [499.0, 500.0, 498.0],
            "close": [503.0, 504.0, 500.0],
            "adj_close": [502.5, 503.5, 499.5],
            "volume": [1000000, 1200000, 1100000],
        }
    )

    rep = audit_symbol_drift("SPY", df, df.copy())
    assert rep.is_zero_drift is True
    assert rep.common_dates_count == 3
    assert len(rep.missing_in_target) == 0
    assert len(rep.extra_in_target) == 0
    assert len(rep.mismatches) == 0
    assert rep.column_metrics["close"].max_abs_diff == 0.0
    assert rep.column_metrics["close"].diff_count == 0


def test_audit_symbol_drift_date_mismatches():
    df_base = pd.DataFrame(
        {
            "date": ["2026-01-02", "2026-01-05"],
            "close": [100.0, 101.0],
        }
    )
    df_target = pd.DataFrame(
        {
            "date": ["2026-01-05", "2026-01-06"],
            "close": [101.0, 102.0],
        }
    )

    rep = audit_symbol_drift("QQQ", df_base, df_target)
    assert rep.is_zero_drift is False
    assert rep.common_dates_count == 1
    assert rep.missing_in_target == ("2026-01-02",)
    assert rep.extra_in_target == ("2026-01-06",)


def test_audit_symbol_drift_numeric_divergence():
    dates = ["2026-01-02", "2026-01-05"]
    df_base = pd.DataFrame(
        {
            "date": dates,
            "open": [100.0, 105.0],
            "close": [102.0, 106.0],
            "adj_close": [102.0, 106.0],
        }
    )
    df_target = pd.DataFrame(
        {
            "date": dates,
            "open": [100.0, 105.0],
            "close": [102.0, 107.0],  # 1.0 diff on 2026-01-05
            "adj_close": [102.0, 106.0],
        }
    )

    rep = audit_symbol_drift("SMH", df_base, df_target)
    assert rep.is_zero_drift is False
    assert rep.column_metrics["close"].diff_count == 1
    assert rep.column_metrics["close"].max_abs_diff == 1.0
    assert len(rep.mismatches) == 1
    m = rep.mismatches[0]
    assert m.symbol == "SMH"
    assert m.date == "2026-01-05"
    assert m.column == "close"
    assert m.baseline_value == 106.0
    assert m.target_value == 107.0


def test_audit_universe_drift_report_formatting():
    dates = ["2026-01-02"]
    df1 = pd.DataFrame({"date": dates, "close": [100.0], "adj_close": [100.0], "volume": [500]})
    df2 = pd.DataFrame({"date": dates, "close": [200.0], "adj_close": [200.0], "volume": [600]})

    base_dict = {"SPY": df1, "QQQ": df2}
    target_dict = {"SPY": df1.copy(), "QQQ": df2.copy()}

    rep = audit_universe_drift(
        base_dict,
        target_dict,
        universe_name="test_pool",
        start_date="2026-01-01",
        end_date="2026-01-10",
    )
    assert rep.is_all_zero_drift is True
    assert rep.total_symbols == 2
    assert rep.zero_drift_symbols == 2

    md = rep.to_markdown()
    assert "# Zero-Drift Audit Report: TEST_POOL" in md
    assert "PASS (0 Drift)" in md
    assert "| `SPY` |" in md
    assert "| `QQQ` |" in md

    dct = rep.to_dict()
    assert dct["universe_name"] == "test_pool"
    assert dct["is_all_zero_drift"] is True


def test_cli_audit_command(tmp_path: Path):
    base_dir = tmp_path / "base"
    target_dir = tmp_path / "target"
    base_dir.mkdir()
    target_dir.mkdir()

    df = pd.DataFrame(
        {"date": ["2026-01-02"], "close": [100.0], "adj_close": [100.0], "volume": [1000]}
    )
    df.to_parquet(base_dir / "SPY.parquet")
    df.to_parquet(target_dir / "SPY.parquet")

    out_md = tmp_path / "report.md"
    out_json = tmp_path / "report.json"

    ret = main(
        [
            "audit-drift",
            "--symbols",
            "SPY",
            "--baseline-dir",
            str(base_dir),
            "--target-dir",
            str(target_dir),
            "--output-md",
            str(out_md),
            "--output-json",
            str(out_json),
        ]
    )
    assert ret == 0
    assert out_md.exists()
    assert out_json.exists()
    assert "Zero-Drift Audit Report" in out_md.read_text(encoding="utf-8")

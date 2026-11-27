"""Unit tests for offline EOD market-data quality control and correction overlay."""

from __future__ import annotations

from datetime import date
from types import MappingProxyType

import numpy as np
import pandas as pd
import pytest

from ohmydata.tools import (
    CorrectionRecord,
    EodAdjustmentBasis,
    EodQcReport,
    QualityCategory,
    QualitySeverity,
    apply_corrections,
    detect_non_positive_prices,
    detect_ohlc_violations,
    detect_oscillating_currency_jumps,
    detect_reverse_split_dividend_mismatch,
    detect_suspect_missing_splits,
    detect_trailing_staleness,
    detect_zero_volume,
    run_eod_qc,
)


def _bars(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_detect_non_positive_prices_catches_negative_zero_nan_inf() -> None:
    df = _bars(
        [
            {
                "symbol": "NFE",
                "date": "2026-01-02",
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "adj_close": -1.25,
                "volume": 1000,
            },
            {
                "symbol": "NFE",
                "date": "2026-01-05",
                "open": 0.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "adj_close": 10.2,
                "volume": 1000,
            },
            {
                "symbol": "NFE",
                "date": "2026-01-06",
                "open": 10.0,
                "high": np.inf,
                "low": 9.8,
                "close": np.nan,
                "adj_close": 10.2,
                "volume": 1000,
            },
            {
                "symbol": "SPY",
                "date": "2026-01-02",
                "open": 500.0,
                "high": 505.0,
                "low": 499.0,
                "close": 504.0,
                "adj_close": 504.0,
                "volume": 50000,
            },
        ]
    )

    findings = detect_non_positive_prices(df)
    assert len(findings) == 3
    assert all(f.category == QualityCategory.NON_POSITIVE_PRICE for f in findings)
    assert all(f.severity == QualitySeverity.ERROR for f in findings)
    assert findings[0].symbol == "NFE"
    assert findings[0].date == "2026-01-02"
    assert findings[0].metrics["bad_fields"] == {"adj_close": -1.25}
    assert findings[1].metrics["bad_fields"] == {"open": 0.0}
    assert set(findings[2].metrics["bad_fields"].keys()) == {"high", "close"}


def test_detect_ohlc_violations_with_ulp_tolerance() -> None:
    df = _bars(
        [
            {
                "symbol": "OK_ULP",
                "date": "2026-01-02",
                "open": 100.0,
                "high": 100.0 - 1e-11,  # within 1e-9 * 100 = 1e-7 tolerance
                "low": 99.0,
                "close": 99.5,
                "adj_close": 99.5,
                "volume": 1000,
            },
            {
                "symbol": "BAD1",
                "date": "2026-01-02",
                "open": 100.0,
                "high": 98.0,  # high < low and high < max(open, close)
                "low": 99.0,
                "close": 99.5,
                "adj_close": 99.5,
                "volume": 1000,
            },
            {
                "symbol": "BAD2",
                "date": "2026-01-02",
                "open": 100.0,
                "high": 102.0,
                "low": 100.5,  # low > min(open, close)
                "close": 101.0,
                "adj_close": 101.0,
                "volume": 1000,
            },
        ]
    )

    findings = detect_ohlc_violations(df, ulp_rel_tol=1e-9)
    assert len(findings) == 2
    assert {f.symbol for f in findings} == {"BAD1", "BAD2"}
    assert all(f.category == QualityCategory.OHLC_GEOMETRY for f in findings)
    assert all(f.severity == QualitySeverity.ERROR for f in findings)
    bad1 = next(f for f in findings if f.symbol == "BAD1")
    assert "high_lt_low" in bad1.metrics["rules"]
    assert "high_lt_max_oc" in bad1.metrics["rules"]


def test_detect_suspect_missing_splits_handles_splits_and_spinoffs() -> None:
    df = _bars(
        [
            # HEI.A missing split (-50% overnight & close jump with no action)
            {
                "symbol": "HEI.A",
                "date": "2026-01-02",
                "open": 200.0,
                "high": 202.0,
                "low": 199.0,
                "close": 200.0,
            },
            {
                "symbol": "HEI.A",
                "date": "2026-01-05",
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 100.0,
            },
            # NVDA legitimate 10:1 split covered by split action within +/-3d
            {
                "symbol": "NVDA",
                "date": "2026-01-02",
                "open": 1000.0,
                "high": 1010.0,
                "low": 995.0,
                "close": 1000.0,
            },
            {
                "symbol": "NVDA",
                "date": "2026-01-05",
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 100.0,
            },
            # SPTI-like spinoff price drop covered by spinoff action within +/-3d -> INFO
            {
                "symbol": "SPTI",
                "date": "2026-01-02",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
            },
            {
                "symbol": "SPTI",
                "date": "2026-01-05",
                "open": 75.0,
                "high": 76.0,
                "low": 74.0,
                "close": 75.0,
            },
            # Spin-off using spinoffdividend action label
            {
                "symbol": "SPN2",
                "date": "2026-01-02",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
            },
            {
                "symbol": "SPN2",
                "date": "2026-01-05",
                "open": 80.0,
                "high": 81.0,
                "low": 79.0,
                "close": 80.0,
            },
        ]
    )
    actions = pd.DataFrame(
        [
            {"symbol": "NVDA", "date": "2026-01-05", "action": "split", "value": 10.0},
            {"symbol": "SPTI", "date": "2026-01-06", "action": "spinoff", "value": 25.0},
            {"symbol": "SPN2", "date": "2026-01-04", "action": "spinoffdividend", "value": 20.0},
        ]
    )

    findings = detect_suspect_missing_splits(df, actions_df=actions, jump_threshold=0.15)
    by_sym = {f.symbol: f for f in findings}
    assert "NVDA" not in by_sym
    assert by_sym["HEI.A"].category == QualityCategory.SUSPECT_MISSING_SPLIT
    assert by_sym["HEI.A"].severity == QualitySeverity.ERROR
    assert by_sym["SPTI"].category == QualityCategory.SPINOFF_BASIS_DIFFERENCE
    assert by_sym["SPTI"].severity == QualitySeverity.INFO
    assert by_sym["SPN2"].category == QualityCategory.SPINOFF_BASIS_DIFFERENCE
    assert by_sym["SPN2"].severity == QualitySeverity.INFO


def test_detect_oscillating_currency_jumps_catches_wpm_not_one_way_crash() -> None:
    df = _bars(
        [
            # WPM CAD/USD jump (+36%) then reverse (-26.5%) across weekend (3 calendar days)
            {"symbol": "WPM", "date": "2026-01-08", "close": 50.0},
            {"symbol": "WPM", "date": "2026-01-09", "close": 68.0},
            {"symbol": "WPM", "date": "2026-01-12", "close": 50.0},
            # Milder oscillating jump (+22% then -18% next day) -> WARNING
            {"symbol": "MILD", "date": "2026-01-08", "close": 100.0},
            {"symbol": "MILD", "date": "2026-01-09", "close": 122.0},
            {"symbol": "MILD", "date": "2026-01-10", "close": 100.0},
            # One-way earnings crash (-40%) with minor bounce (+5%) -> not flagged
            {"symbol": "EARN", "date": "2026-01-08", "close": 100.0},
            {"symbol": "EARN", "date": "2026-01-09", "close": 60.0},
            {"symbol": "EARN", "date": "2026-01-12", "close": 63.0},
        ]
    )

    findings = detect_oscillating_currency_jumps(df, jump_threshold=0.20, max_gap_days=3)
    by_sym = {f.symbol: f for f in findings}
    assert set(by_sym.keys()) == {"MILD", "WPM"}
    assert by_sym["WPM"].category == QualityCategory.OSCILLATING_CURRENCY_JUMP
    assert by_sym["WPM"].severity == QualitySeverity.ERROR
    assert by_sym["WPM"].metrics["gap_days"] == 3
    assert by_sym["MILD"].severity == QualitySeverity.WARNING


def test_detect_reverse_split_dividend_mismatch_catches_nvd_distortion() -> None:
    df = _bars(
        [
            # NVD: 1:5 reverse split (0.2) on 2026-01-10; pre-split div $1.00 on 2026-01-05
            # Expected adj_ret = 20 / (20 - 1) - 1 = +5.26%; actual adj_ret = 26 / 20 - 1 = +30%
            {"symbol": "NVD", "date": "2026-01-02", "close": 20.0, "adj_close": 20.0},
            {"symbol": "NVD", "date": "2026-01-05", "close": 20.0, "adj_close": 26.0},
            {"symbol": "NVD", "date": "2026-01-10", "close": 20.0, "adj_close": 26.0},
            # CLEAN_REV: 1:5 reverse split with accurate adj_close (+5.26%)
            {"symbol": "CLEAN_REV", "date": "2026-01-02", "close": 20.0, "adj_close": 19.0},
            {"symbol": "CLEAN_REV", "date": "2026-01-05", "close": 20.0, "adj_close": 20.0},
            {"symbol": "CLEAN_REV", "date": "2026-01-10", "close": 20.0, "adj_close": 20.0},
            # FWD: forward split (2.0) is ignored by reverse-split detector
            {"symbol": "FWD", "date": "2026-01-02", "close": 20.0, "adj_close": 20.0},
            {"symbol": "FWD", "date": "2026-01-05", "close": 20.0, "adj_close": 30.0},
            {"symbol": "FWD", "date": "2026-01-10", "close": 20.0, "adj_close": 30.0},
        ]
    )
    actions = pd.DataFrame(
        [
            {"symbol": "NVD", "date": "2026-01-05", "action": "dividend", "value": 1.0},
            {"symbol": "NVD", "date": "2026-01-10", "action": "split", "value": 0.2},
            {"symbol": "CLEAN_REV", "date": "2026-01-05", "action": "dividend", "value": 1.0},
            {"symbol": "CLEAN_REV", "date": "2026-01-10", "action": "split", "value": 0.2},
            {"symbol": "FWD", "date": "2026-01-05", "action": "dividend", "value": 1.0},
            {"symbol": "FWD", "date": "2026-01-10", "action": "split", "value": 2.0},
        ]
    )

    findings = detect_reverse_split_dividend_mismatch(df, actions, tolerance=0.10)
    assert len(findings) == 1
    assert findings[0].symbol == "NVD"
    assert findings[0].date == "2026-01-05"
    assert findings[0].category == QualityCategory.REVERSE_SPLIT_DIVIDEND_MISMATCH
    assert findings[0].severity == QualitySeverity.ERROR


def test_detect_trailing_staleness_catches_bito_and_missing_active() -> None:
    df = _bars(
        [
            {"symbol": "SPY", "date": date(2026, 11, 25), "close": 500.0},
            {"symbol": "BITO", "date": date(2026, 11, 18), "close": 25.0},
        ]
    )

    findings = detect_trailing_staleness(
        df,
        expected_latest_date="2026-11-26",
        max_lag_days=2,
        active_symbols=("SPY", "BITO", "VTI"),
    )
    by_sym = {f.symbol: f for f in findings}
    assert "SPY" not in by_sym
    assert by_sym["BITO"].category == QualityCategory.TRAILING_STALENESS
    assert by_sym["BITO"].severity == QualitySeverity.WARNING
    assert by_sym["BITO"].metrics["lag_days"] == 8
    assert by_sym["VTI"].metrics["actual_latest_date"] is None


def test_apply_corrections_preserves_raw_and_validates_raw_value() -> None:
    df = _bars(
        [
            {
                "symbol": "NFE",
                "date": date(2026, 1, 2),
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "adj_close": -1.25,
                "volume": 1000,
            },
            {
                "symbol": "SPY",
                "date": date(2026, 1, 2),
                "open": 500.0,
                "high": 505.0,
                "low": 499.0,
                "close": 504.0,
                "adj_close": 504.0,
                "volume": 50000,
            },
        ]
    )
    rec_applied = CorrectionRecord(
        symbol="NFE",
        date="2026-01-02",
        field="adj_close",
        raw_value=-1.25,
        corrected_value=10.2,
        reason="Fix negative dividend-over-adjusted adj_close",
        evidence_source="nasdaq_official_close",
        version="1.0.0",
    )
    rec_missing = CorrectionRecord(
        symbol="QQQ",
        date="2026-01-02",
        field="adj_close",
        raw_value=400.0,
        corrected_value=401.0,
        reason="Not in slice",
        evidence_source="manual",
        version="1.0.0",
    )

    corrected, receipts = apply_corrections(df, [rec_applied, rec_missing])
    # Original df must remain completely untouched
    assert df.loc[0, "adj_close"] == -1.25
    assert "raw_adj_close" not in df.columns

    # Corrected df preserves provider-native raw_adj_close and updates adj_close
    assert corrected.loc[0, "adj_close"] == 10.2
    assert corrected.loc[0, "raw_adj_close"] == -1.25
    assert corrected.loc[1, "raw_adj_close"] == 504.0

    assert len(receipts) == 2
    assert receipts[0].applied is True
    assert receipts[0].status == "APPLIED"
    assert receipts[1].applied is False
    assert receipts[1].status == "NOT_FOUND"

    # Mismatched raw_value must fail fast
    bad_rec = CorrectionRecord(
        symbol="NFE",
        date="2026-01-02",
        field="adj_close",
        raw_value=999.0,
        corrected_value=10.2,
        reason="wrong raw",
        evidence_source="manual",
        version="1.0.0",
    )
    with pytest.raises(ValueError, match="raw_value mismatch"):
        apply_corrections(df, [bad_rec])


def test_run_eod_qc_statuses_and_serialization() -> None:
    clean_df = _bars(
        [
            {
                "symbol": "SPY",
                "date": "2026-01-02",
                "open": 500.0,
                "high": 505.0,
                "low": 499.0,
                "close": 504.0,
                "adj_close": 504.0,
                "volume": 10000,
            }
        ]
    )
    passed = run_eod_qc(
        clean_df,
        provider="yfinance",
        adjustment_basis=EodAdjustmentBasis.SPLIT_ADJUSTED_WITH_ADJ_CLOSE,
        expected_latest_date="2026-01-02",
    )
    assert isinstance(passed, EodQcReport)
    assert passed.status == "PASSED"
    assert passed.total_rows == 1
    assert passed.symbol_count == 1
    assert passed.summary_counts["ERROR"] == 0
    assert passed.to_dict()["status"] == "PASSED"
    assert "PASSED" in passed.to_markdown()

    # Stale only -> WARN
    warn_rep = run_eod_qc(clean_df, provider="yfinance", expected_latest_date="2026-01-10")
    assert warn_rep.status == "WARN"
    assert warn_rep.summary_counts["TRAILING_STALENESS"] == 1

    # Negative adj_close -> BLOCKED_DATA
    bad_df = clean_df.copy()
    bad_df["adj_close"] = -1.0
    blocked_rep = run_eod_qc(bad_df, provider="yfinance")
    assert blocked_rep.status == "BLOCKED_DATA"
    assert blocked_rep.summary_counts["NON_POSITIVE_PRICE"] == 1
    assert "BLOCKED_DATA" in blocked_rep.to_markdown()

    # Zero volume -> INFO only -> PASSED
    zero_vol_df = clean_df.copy()
    zero_vol_df["volume"] = 0
    zv_findings = detect_zero_volume(zero_vol_df)
    assert len(zv_findings) == 1
    assert zv_findings[0].severity == QualitySeverity.INFO
    zv_rep = run_eod_qc(zero_vol_df)
    assert zv_rep.status == "PASSED"
    assert zv_rep.summary_counts["ZERO_VOLUME"] == 1
    assert isinstance(zv_findings[0].metrics, MappingProxyType)


def test_edge_cases_empty_canonical_symbol_and_boundaries() -> None:
    empty_df = pd.DataFrame()
    assert detect_non_positive_prices(empty_df) == []
    assert detect_ohlc_violations(empty_df) == []
    assert detect_suspect_missing_splits(empty_df) == []
    assert detect_oscillating_currency_jumps(empty_df) == []
    assert detect_reverse_split_dividend_mismatch(empty_df, None) == []
    assert detect_trailing_staleness(empty_df, expected_latest_date=None) == []
    assert detect_zero_volume(empty_df) == []
    empty_rep = run_eod_qc(empty_df, provider="test")
    assert empty_rep.status == "PASSED"
    assert empty_rep.total_rows == 0

    # canonical_symbol alias + overnight_gap-only jump + exact 3d split boundary
    canon_df = _bars(
        [
            {
                "canonical_symbol": "GAP1",
                "date": "2026-01-02",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": -5,
            },
            {
                "canonical_symbol": "GAP1",
                "date": "2026-01-05",
                "open": 50.0,
                "high": 102.0,
                "low": 49.0,
                "close": 100.0,
                "volume": 100,
            },
            {
                "canonical_symbol": "COV3",
                "date": "2026-01-02",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 100,
            },
            {
                "canonical_symbol": "COV3",
                "date": "2026-01-05",
                "open": 50.0,
                "high": 51.0,
                "low": 49.0,
                "close": 50.0,
                "volume": 100,
            },
        ]
    )
    actions = pd.DataFrame(
        [{"canonical_symbol": "COV3", "date": "2026-01-08", "action": "split", "value": 2.0}]
    )
    rep = run_eod_qc(
        canon_df, actions_df=actions, expected_latest_date="2026-01-07", max_lag_days=2
    )
    cats = {(f.symbol, f.category) for f in rep.findings}
    assert ("GAP1", QualityCategory.SUSPECT_MISSING_SPLIT) in cats
    assert ("GAP1", QualityCategory.ZERO_VOLUME) in cats
    assert ("COV3", QualityCategory.SUSPECT_MISSING_SPLIT) not in cats
    assert ("COV3", QualityCategory.TRAILING_STALENESS) not in cats


def test_apply_corrections_sequential_overlay_and_error_cases() -> None:
    df = _bars(
        [
            {"symbol": "A", "date": "2026-01-02", "close": np.nan, "volume": 100},
            {"symbol": "B", "date": "2026-01-02", "close": 10.0, "volume": 100},
            {"symbol": "B", "date": "2026-01-02", "close": 10.0, "volume": 100},
        ]
    )
    r1 = CorrectionRecord("A", "2026-01-02", "close", np.nan, 15.0, "fill missing", "sec", "1.0")
    r2 = CorrectionRecord("A", "2026-01-02", "close", 15.0, 15.5, "refine", "exchange", "2.0")
    out1, _ = apply_corrections(df, [r1])
    out2, rec2 = apply_corrections(out1, [r2])
    assert out2.loc[0, "close"] == 15.5
    assert pd.isna(out2.loc[0, "raw_close"])
    assert rec2[0].to_dict()["applied"] is True

    with pytest.raises(ValueError, match="Duplicate rows"):
        apply_corrections(
            df, [CorrectionRecord("B", "2026-01-02", "close", 10.0, 11.0, "dup", "src")]
        )
    with pytest.raises(ValueError, match="Invalid or missing correction target field"):
        apply_corrections(
            df,
            [CorrectionRecord("A", "2026-01-02", "date", "2026-01-02", "2026-01-03", "bad", "src")],
        )

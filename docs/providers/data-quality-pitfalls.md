# Market-Data Provider Quality Pitfalls Registry

This document is the canonical, single-source-of-truth registry of empirically
verified EOD market-data pitfalls in `ohmydata` (OMD). Consumer repositories
must link to this registry rather than copying or diverging pitfall lists.

All quantitative findings below were measured over a 10-year window
(`2016-10-01` to `2026-11-26`) comparing **Sharadar Direct** (`stocks.csv`,
`funds.csv`, `actions.csv`, `tickers.csv`) against pinned **`yfinance==1.7.0`**
under OMD's canonical defaults (`auto_adjust=False, repair=False, actions=True,
keepna=True`) across three audit tiers:

1. **Stratified Benchmark (60 symbols)**: 15 large-cap stocks, 3 multi-class
   shares, 10 ADRs, 18 ETFs, and 14 delisted/recycled probes.
2. **ETF Liquidity & Delisted Audit (330 ETFs)**: Top 300 active ETFs by 90-day
   average dollar volume plus 30 delisted/recycled ETFs.
3. **Dynamic Top 1000 Universe (2,294 symbols)**: 1,712 active symbols (including
   250 ADRs and 76 suffix-reuse probes) and 582 delisted symbols.

> **Upgrade Note (`yfinance==1.8.0`)**: Re-verified against pinned `yfinance==1.8.0` (`curl_cffi==0.17.0`) via `omd audit-drift --universe r10a0` across 13 ETFs (`2016-01-01` to `2026-11-27`, `2,741` rows/symbol): `0` raw OHLCV diffs (`--raw-only`) and `<= 1.83e-4` `adj_close` rounding diff. Spot checks confirm upstream `NFE` negative `adj_close` (`988` rows) and `WIT` missing 2019 split persist in `1.8.0` (`repair=False`), and `earnings_estimate` / `eps_trend` / `eps_revisions` now include a `methodology` column.

---

## Provider × Asset Class Matrix

| Asset Class | `yfinance==1.7.0` | Sharadar Direct | Primary Recommendation |
| :--- | :--- | :--- | :--- |
| **US Active Common Stocks** | High close parity (`2.18e-8` median rel diff), but batch downloads silently drop active tickers without `PER_SYMBOL` repair; isolated `adj_close <= 0` and OHLC corruption (`988` / `5` rows in Top 1000). | `0` OHLC violations and `0` non-positive prices across `4,739,096` active rows; rare missed splits (`LBTYK`, `LGND`, `FBIN`) and isolated currency/IPO glitches (`WPM`, `FIGR`). | Sharadar for research lakes; `yfinance` acceptable only with `PER_SYMBOL` repair and `eod_qc` gates. |
| **Multi-Class Shares** | Hyphen ticker mapping (`HEI-A`, `BRK-B`); correctly recorded `HEI-A` splits missed by Sharadar. | Dot ticker mapping (`HEI.A`, `BRK.B`); missed 3 `1.25x` splits on `HEI.A` (and mislabeled `2017-04-19` as a dividend). | Cross-check via `eod_qc.detect_suspect_missing_splits`. |
| **ADRs / Foreign Issuers** | Reports **gross** pre-withholding dividends (`SAP`, `NVO`, `ITUB`), causing `0.7%–1.6%` 10-year `adj_close` drift vs net providers; missed `WIT` `1.333x` split. | Reports **net** post-tax/fee dividends (~`0.72x` of gross) and explicit `adrratiosplit` records (`39` in Top 1000). | Never mix gross and net ADR dividend series in one strategy; verify `adrratiosplit` actions. |
| **US ETFs** | Consolidated Tape pricing (`USMV` reflects full market volume); un-repaired batches silently dropped `VTI`, `VCSH`, `UDOW`, `SPYV`, `STIP` (pulling Round 2 overlap to `98.052%`). | Captures single-venue Cboe BZX prints on affected listings (`USMV` diverged `10bp–70bp` on `90/2,220` days); missed `SPTI` `2:1` split and mis-scaled `NVD`/`NVDL` reverse-split dividends; trailing lag on `BITO`. | Keep Consolidated Tape (`yfinance` + `PER_SYMBOL` + `eod_qc`) for active ETFs calibrated on Consolidated Tape (`us_r10`); use Sharadar for delisted ETF completeness. |
| **Delisted & Recycled Tickers** | **Unsuitable for historical backtests**: `85.1%` completely missing (`495/582`), `11.0%` ticker-recycle contamination (`64/582`), `3.3%` truncated (`19/582`), only `0.7%` usable (`4/582`). | Full `permaticker`-backed historical coverage for delisted equities and liquidated ETFs. | **Mandatory Sharadar (or equivalent delisted-complete source)** for any historical universe containing delisted securities. |

---

## Verified Empirical Pitfalls

### 1. `yfinance` Pitfalls

1. **Delisted Missingness and Ticker-Recycle Contamination (Critical)**
   - **Empirical evidence**: Across `582` delisted Dynamic Top 1000 symbols:
     - `495` (**85.1%**) returned `TARGET_EMPTY` (including `TWTR`, `ATVI`, `VMW`, `SPLK`, `PXD`, `MRO`, `HES`, `DFS`, `JNPR`, `SIVBQ`, `RSX`, `ERUS`, `BSCO`).
     - `64` (**11.0%**) returned `TARGET_RECYCLED_CONTAMINATION`—querying either stripped-suffix delisted codes (`PINC1→PINC`, `WOLF2→WOLF`, `ANSS1→ANSS`, `DOC2→DOC`, `OILU1→OILU`) or un-suffixed reused tickers (`BSCL`, `TELFY`, `BIGGQ`, `LNWO`, `VXRT`) silently spliced a later company's prices onto the delisted symbol.
     - `19` (**3.3%**) returned `TARGET_TRUNCATED_OR_PARTIAL`; only `4` (**0.7%**) matched (`TARGET_PRESENT_MATCH`).
     - Smaller cohorts showed the same failure mode: `10/14` empty and `3/14` recycled in Round 1; `20/30` empty, `6/30` recycled, and `4/30` truncated in Round 2 delisted ETFs.
   - **OMD mitigation**: `YFINANCE_CAPABILITIES` explicitly declares `supports_delisted=False` and `recycled_ticker_risk=True`. Consumers must not use `yfinance` for delisted or survivorship-free PIT backtests.

2. **Silent Whole-Symbol Batch Drops**
   - **Empirical evidence**: In Round 2 (300 active ETFs), batch `yf.download` silently dropped `VTI`, `VCSH`, `UDOW`, `SPYV`, and `STIP` (accounting for `99.9%` of the `12,774` missing active bars and lowering overlap to `98.052%` = `643,064 / 655,838`).
   - **OMD mitigation**: `YFinanceRepairPolicy.PER_SYMBOL` isolates missing symbols and retries them individually, eliminating whole-symbol active drops across all `1,712` active Dynamic Top 1000 symbols (`99.768%` = `3,960,700 / 3,969,903`).

3. **Non-Positive / Negative `adj_close` and OHLC Geometry Violations**
   - **Empirical evidence**: Across `3,984,931` active Dynamic Top 1000 rows, `yfinance` produced `988` non-positive price rows and `5` OHLC geometry violations (`8,838` zero/null-volume days). Notably, `NFE` had `2019–2025` `adj_close` driven negative down to **`-23.59`** by large dividend/restructuring adjustments, producing a spurious **`+4768.42%`** daily return on `2026-01-02` (`1.14 → 55.5`). `WBD`, `LPSN`, and `OLPX` each exhibited non-positive/OHLC anomalies.
   - **OMD mitigation**: `ohmydata.providers.yfinance` marks `adj_close <= 0` or non-finite values as `YFinanceSymbolOutcome.INVALID_VALUES` (and fails closed under `YFinanceBatchPolicy.STRICT`); `eod_qc.detect_non_positive_prices` and `eod_qc.detect_ohlc_violations` emit `ERROR` findings (`BLOCKED_DATA`).

4. **Corporate Action and Single-Day Print Glitches**
   - **Empirical evidence**: `AVGO` (`2018-03-21` and `2018-06-19`) quarterly dividends were doubled to `$0.35` (actual `$0.175`); `WIT` (`2019-03-15`) missed a `1.333x` split (`-23.7%` false drop); `UVXY` (`2018-10-11`) printed an erroneous close (`147,750` vs `132,150`, reverting the following session).

---

### 2. Sharadar Direct Pitfalls

1. **Missing Pure Splits (`HEI.A`, `SPTI`, `LBTYK`, `LGND`, `FBIN`)**
   - **Empirical evidence**:
     - `HEI.A` missed three `1.25x` (5-for-4) splits (`2017-04-19`, `2018-01-18`, `2018-06-28`), and `2017-04-19` was misrecorded as a `$14.7375` dividend—creating false `-20.72%` and `-18.91%` drops in 2018 and overstating pre-2017 prices by `48.8%` (`yfinance` `HEI-A` was accurate).
     - `SPTI` (`2019-09-23`) missed a `2:1` split, producing a `-49.94%` false drop (`62.28 → 31.175` vs `yfinance` `31.14 → 31.17`).
     - `LBTYK` (`2024-11-13`), `LGND` (`2022-11-02`), and `FBIN` (`2022-12-15`) also exhibited non-spinoff split divergences.
   - **OMD mitigation**: `eod_qc.detect_suspect_missing_splits` flags `|close_ret| >= 15%` or `|overnight_gap| >= 15%` without a `±3d` `split` (while separating `spinoff`/`spinoffdividend` into `INFO` `SPINOFF_BASIS_DIFFERENCE`).

2. **Reverse-Split Dividend Scaling Distortion (`NVD`, `NVDL`)**
   - **Empirical evidence**: On leveraged ETFs undergoing reverse splits, historical pre-split dividends were not consistently scaled into `closeadj`:
     - `NVDL` (`2023-12-27` dividend `$3.37049`) had prior `closeadj` miscomputed as `3.279` (vs `close=5.505`), generating a spurious **`+51.45%`** jump (`yfinance`: `+0.45%`).
     - `NVD` (`2023-12-27` dividend `$3.00511`) was not scaled `25x` to `$75.125` after its `2024-11-04` `1:25` reverse split, causing a **`-13.02%`** return error (`yfinance` scaled it properly).
   - **OMD mitigation**: `eod_qc.detect_reverse_split_dividend_mismatch` detects pre-split dividend vs `adj_close` return distortion on symbols with `<1.0` reverse splits.

3. **Oscillating Mixed-Currency Quotes (`WPM`)**
   - **Empirical evidence**: Between `2016-12` and `2017-05` (~70 trading days), Sharadar alternated `WPM` quotes between USD and CAD (e.g., `17.34 → 23.74 → 17.95 → 25.06`), producing `35%–41%` oscillating round-trip jumps while `yfinance` stayed smoothly in USD.
   - **OMD mitigation**: `eod_qc.detect_oscillating_currency_jumps` catches opposing large jumps within `max_gap_days=3` without flagging one-way earnings moves.

4. **Single-Venue Cboe BZX Prints (`USMV` / `us_r10` Parity Gate)**
   - **Empirical evidence**: Across `2,220` trading days (`2018-01-03` to `2026-11-02`) on `funmoney_backtest`'s `us_r10` 13-ETF pool, `USMV` (Cboe BZX-listed) diverged by **`10bp–70bp`** on **90 days** (max `close` diff `0.681%`, max `adj_close` diff `0.701%`, max daily return diff `0.698%`). For example, on `2024-05-31`, `yfinance` reported `close=82.80, vol=2.87M` (Consolidated Tape) while Sharadar reported `close=82.24, vol=0.887M` (~1/3 market volume, single venue).
   - **OMD mitigation**: `SHARADAR_CAPABILITIES` declares `single_venue_caveat_exchanges=("BATS", "CBOE_BZX")`. Strategies calibrated on Consolidated Tape (`us_r10`) must not switch `USMV` (or other Cboe BZX ETFs) to Sharadar without explicit parity validation.

5. **IPO Day-1 Glitch, Outliers, and Trailing Bar Lag (`FIGR`, `BITO`, `VUG`, `SCHD`)**
   - **Empirical evidence**: `FIGR` (`2025-09-12`) recorded `close=12.35` before jumping to `37.33` (`yfinance`: `32.50 → 37.33`); `BITO` in local `funds.csv` stopped at `2026-11-19`, lagging `2026-11-26` by 4 trading days; `VUG` (`2023-02-02`) showed a single-day `1.89%` divergence (`40.443` vs `41.207`); `SCHD` had `3` days `>10bp` (max `0.82%` near `2019-12-09/10` ex-div).
   - **OMD mitigation**: `eod_qc.detect_trailing_staleness` flags trailing lag beyond `max_lag_days`.

---

### 3. Structural Basis Differences (Expected Divergences, Not Errors)

1. **Spin-Off `close` vs `closeadj` Basis Difference (66 / 72 Flagged Events)**
   - **Empirical evidence**: In Dynamic Top 1000, `66` of `72` Sharadar unsplit jumps were corporate spin-offs (including `GE 2024-04-02`, `DELL 2021-11-02`, `SNX 2020-12-01`, `AIV 2020-12-15`, `CTVA 2026-10-01`, `MTCH 2020-07-01`, `DD 2025-11-03`, `RTX 2020-04-03`).
   - **Mechanism**: `yfinance` encodes spin-offs as fractional `Stock Splits` (e.g., `GE` `1.253x`, `DELL` `1.973x`) and adjusts historical `close`. Sharadar keeps `close` unadjusted for spin-offs, logs `spinoff` / `spinoffdividend` in `actions.csv`, and adjusts only `closeadj` (total-return parity holds: `GE` `2024-04-01 → 04-02` Sharadar `closeadj` `137.52 → 134.22` matches `yfinance` `adj_close` `137.66 → 134.24`).
   - **OMD classification**: Both `eod_qc.detect_suspect_missing_splits` and `market-data-audit` classify jumps with `±3d` `spinoff`/`spinoffdividend` actions as `SPINOFF_BASIS_DIFFERENCE` (`INFO`), separate from `SUSPECT_MISSING_SPLIT` / `CONFIRMED_MISSING_SPLIT`.

2. **ADR Tax Withholding, Ex-Date ±1d Offsets, Listing Phase, and Volume**
   - **ADR dividends**: Sharadar records net dividends (~`0.72x`) whereas `yfinance` records gross dividends (`SAP`, `NVO`, `ITUB`), causing `0.7%–1.6%` 10-year `adj_close` drift while `close` matches.
   - **Ex-date ±1d shift**: Example `XOM` `$0.75` (`2017-02-07` vs `2017-02-08`), causing `±0.90%` offsetting adjacent-day diffs.
   - **Listing phase**: Example `GBTC` (`722` Sharadar rows from `2024-01-11` spot ETF conversion vs `2,552` `yfinance` rows including OTC trust era).
   - **Volume**: `8.5%–13.6%` of active days differ by `>1%` due to dark-pool/after-hours/venue conventions.
   - **Adjustment basis mismatch**: Never compare unadjusted OHLC (e.g., `closeunadj / close`-rescaled tables) directly against `SPLIT_ADJUSTED_WITH_ADJ_CLOSE` feeds; verify `adj_close` parity and declare `EodAdjustmentBasis` explicitly.

---

## OMD Control & Detection Mapping

| Pitfall | OMD Prevention / Detection Entry Point | Outcome / Classification |
| :--- | :--- | :--- |
| `yfinance` delisted / recycled tickers | `YFINANCE_CAPABILITIES` (`supports_delisted=False`, `recycled_ticker_risk=True`); use `ohmydata.providers.sharadar` | Caller universe guard; `SHARADAR_CAPABILITIES.supports_delisted=True` |
| `yfinance` silent batch drops (`VTI`, etc.) | `YFinanceDailyBarsRequest(..., repair_policy=YFinanceRepairPolicy.PER_SYMBOL)` | Single-symbol fallback recovery & outcome tracking |
| Negative / non-positive prices (`NFE`, `WBD`) | `YFinanceSymbolOutcome.INVALID_VALUES`; `eod_qc.detect_non_positive_prices` | `QualityCategory.NON_POSITIVE_PRICE` (`ERROR` → `BLOCKED_DATA`) |
| OHLC geometry violations | `eod_qc.detect_ohlc_violations` | `QualityCategory.OHLC_GEOMETRY` (`ERROR` → `BLOCKED_DATA`) |
| Missing pure splits (`HEI.A`, `SPTI`) | `eod_qc.detect_suspect_missing_splits` | `QualityCategory.SUSPECT_MISSING_SPLIT` (`ERROR` → `BLOCKED_DATA`) |
| Spin-off basis difference (`GE`, `DELL`) | `eod_qc.detect_suspect_missing_splits` + `market-data-audit` | `QualityCategory.SPINOFF_BASIS_DIFFERENCE` (`INFO`) |
| Reverse-split dividend mismatch (`NVD`, `NVDL`) | `eod_qc.detect_reverse_split_dividend_mismatch` | `QualityCategory.REVERSE_SPLIT_DIVIDEND_MISMATCH` (`ERROR` → `BLOCKED_DATA`) |
| Currency oscillation (`WPM`) | `eod_qc.detect_oscillating_currency_jumps` | `QualityCategory.OSCILLATING_CURRENCY_JUMP` (`WARNING`/`ERROR`) |
| Trailing staleness (`BITO`) | `eod_qc.detect_trailing_staleness` | `QualityCategory.TRAILING_STALENESS` (`WARNING` → `WARN`) |
| Single-venue prints (`USMV` Cboe BZX) | `SHARADAR_CAPABILITIES.single_venue_caveat_exchanges` + `market-data-audit` | Explicit provider/venue selection rule |
| Explicit repair lineage | `CorrectionRecord` + `eod_qc.apply_corrections(df, records)` | Non-destructive copy retaining `raw_<field>` + versioned receipt |

---

## Mandatory Consumer Repo Onboarding Checklist

Every downstream consumer repository (existing or newly created) that ingests
or backtests US EOD market data must complete this checklist:

1. **Pin an immutable OMD release**: Depend on a fixed published `ohmydata`
   version (never a moving branch). If `ohmydata[yfinance]` is enabled, preserve
   pinned `yfinance==1.8.0` and canonical defaults (`auto_adjust=False,
   repair=False, actions=True, keepna=True`); validate upgrades with
   `omd audit-drift`.
2. **Declare `EodAdjustmentBasis` explicitly**: Bind every ingested bar table to
   `SPLIT_ADJUSTED_WITH_ADJ_CLOSE`, `UNADJUSTED_WITH_ADJ_CLOSE`, or
   `AUTO_ADJUSTED`. Never mix unadjusted OHLC with split-adjusted OHLC.
3. **Select provider by universe & venue contract**:
   - Use **Sharadar Direct** (`ohmydata.providers.sharadar`) or another
     delisted-complete feed for any historical/PIT universe containing delisted
     symbols.
   - Never use `yfinance` for delisted or recycled-ticker backtests; enable
     `YFinanceRepairPolicy.PER_SYMBOL` when fetching active-only symbols.
   - Preserve Consolidated Tape continuity where production strategies depend
     on it (e.g., do not swap Cboe BZX-listed ETFs such as `USMV` onto
     single-venue prints without parity evidence).
4. **Wire `ohmydata.tools.run_eod_qc` as a pre-backtest gate**:
   ```python
   from ohmydata.tools import EodAdjustmentBasis, apply_corrections, run_eod_qc

   report = run_eod_qc(
       bars_df,
       actions_df=actions_df,
       provider="sharadar",
       adjustment_basis=EodAdjustmentBasis.SPLIT_ADJUSTED_WITH_ADJ_CLOSE,
       expected_latest_date=expected_session_date,
       active_symbols=active_universe,
   )
   if report.status == "BLOCKED_DATA":
       raise RuntimeError(f"EOD QC blocked ingestion:\n{report.to_markdown()}")
   ```
   - Block ingestion/backtesting whenever `report.status == "BLOCKED_DATA"` (any
     `ERROR` finding, including `NON_POSITIVE_PRICE`, `OHLC_GEOMETRY`,
     `SUSPECT_MISSING_SPLIT`, `REVERSE_SPLIT_DIVIDEND_MISMATCH`, and severe
     `OSCILLATING_CURRENCY_JUMP`).
   - Investigate and resolve `WARNING` findings (`OSCILLATING_CURRENCY_JUMP`,
     `TRAILING_STALENESS`, and invalid `ZERO_VOLUME`) before promotion.
   - If applying manual or secondary-source overrides, use `CorrectionRecord`
     and `apply_corrections(...)` so provider-native `raw_<field>` values and
     audit lineage remain intact.
5. **Reference—do not copy—this registry**: Add a pointer to OMD's
   `docs/providers/data-quality-pitfalls.md` in the consumer repository's
   `AGENTS.md` / `RULES.md`, and run the global `market-data-audit` skill when
   onboarding a new provider or expanding the asset universe.

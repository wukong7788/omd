# yfinance Provider Governance and Zero-Drift Auditing

### Baseline Governance and Invariants

1. **Immutable Pinning**:
   `yfinance` must always be pinned to an exact version (`yfinance==<version>`)
   in `pyproject.toml` and asserted at runtime via `EXPECTED_YFINANCE_VERSION`
   in `src/ohmydata/providers/yfinance/client.py`. Never use a loose range
   (e.g. `>=...`), as yfinance frequently alters parsing, column structures,
   and heuristic price repair algorithms across minor releases.

2. **Default `repair=False` Invariant**:
   Production ingestion and historical backtest pipelines must strictly default
   to `auto_adjust=False, repair=False, actions=True, keepna=True`.
   - Global `repair=True` is prohibited in canonical ingestion: it is an
     active heuristic mutator with known 100x unit scaling bugs and
     unflagged dividend modifications, and introduces an undeclared dependency
     on `scipy`.
   - OMD owns Quality Control (QC) anomaly detection (`check_ohlc_anomalies`,
     `check_price_jump_anomalies`, `check_volume_anomalies`). `repair=True` may
     only be evaluated as an isolated candidate repair engine for targeted
     anomalies, recording explicit `raw_value`, `canonical_value`,
     `repair_status`, and `repair_reason`.

3. **Predefined Benchmark Universe (`r10a0`)**:
   The `r10a0` multi-asset ETF universe serves as the canonical regression
   benchmark for US market data stability:
   - **Clusters (Cluster Variant v3, max 1 each)**:
     - `equity_risk`: `[SPY, QQQ, XLK, IWM, SMH]`
     - `sector_cyclicals`: `[XLF, XLE, XLV]`
     - `defensive`: `[TLT, GLD, USMV]`
   - **Regime Pools**:
     - `risk_on`: 11 symbols
     - `risk_off` (SPY < MA200): `[SHY, IEF, GLD]` (Top 2 selected)
   - **Unique Set (13 ETFs)**: `SPY`, `QQQ`, `XLK`, `IWM`, `SMH`, `XLF`, `XLE`,
     `XLV`, `TLT`, `GLD`, `USMV`, `SHY`, `IEF`.

4. **Zero-Drift Audit Tool (`omd audit-drift`)**:
   Before approving any future `yfinance` version upgrade, run the automated
   zero-drift audit tool across the full 10+ year history of the `r10a0`
   universe:
   ```bash
   # Strict unadjusted market bar zero-drift gate (must be bit-exact 0.0 error)
   omd audit-drift --universe r10a0 --baseline-dir <old_version_dir> --target-dir <new_version_dir> --raw-only

   # Full audit including adj_close with sub-cent rounding tolerance
   omd audit-drift --universe r10a0 --baseline-dir <old_version_dir> --target-dir <new_version_dir> --abs-tolerance 0.001
   ```
   **Acceptance Criteria for Upgrade**:
   - `raw-only` (Open, High, Low, Close, Volume) must achieve **0 row difference
     and 0 numeric difference** across all 13 ETFs over the full historical
     window.
   - Any divergence in `adj_close` must be bounded by sub-cent floating point
     rounding (<= 0.001) or accompanied by an explicit, auditable rationale.


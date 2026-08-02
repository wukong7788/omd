# Phase 3c/4a — Polars Adapter and `funmoney_backtest` Shadow Migration

Status: `OMD ADAPTER + ETF_BASIC SLICES ACCEPTED — B6-B1-2 BASELINE FROZEN — CONSUMER MIGRATION PENDING`

The user authorized Sol–Luna execution on 2026-08-01 with an explicit stop
condition: no A-share live migration may begin until drift evidence has been
reviewed. Two OMD-only slices are now accepted: the explicit Polars adapter and
the typed `etf_basic` endpoint. Gates C–F and all `funmoney_backtest` runtime
changes remain unauthorized.

## 1. Objective

Prepare `funmoney_backtest` to consume OMD without changing its Polars data
contracts, normalized bar schema, data-quality output, Parquet artifacts,
backtests, signals, or live behavior.

The dataframe boundary is explicit:

```text
official Tushare-compatible client
  -> OMD typed endpoint / provider-semantic Pandas result
  -> explicit OMD Pandas-to-Polars adapter
  -> funmoney-owned Polars normalization and orchestration
  -> funmoney-owned Parquet, features, backtests, signals, and live consumers
```

`funmoney_backtest` is a Polars consumer. Pandas is not its target data
contract; it remains the provider-native representation used by Tushare and
the current OMD Tushare implementation.

## 2. Evidence and Current Boundaries

The migration is based on these concrete consumer contracts:

- `funmoney_backtest/data_provider/base.py` defines `RAW_BAR_SCHEMA`,
  `QUALITY_SCHEMA`, `DataProviderRequest`, and `DataProviderResult` with
  `pl.DataFrame` results.
- `funmoney_backtest/data_provider/tushare.py` converts official-client Pandas
  responses with `pl.from_pandas`, performs Polars joins and normalization,
  scales `fund_daily.amount` from thousand yuan to yuan, and returns Polars
  frames.
- `funmoney_backtest/data_pipeline/parquet_pipeline.py` owns date conversion,
  normalized bars, liquidity and listing filters, `adj_close`, incremental
  merge behavior, Parquet schemas, and quality artifacts.
- `funmoney_backtest/data_pipeline/tushare_ext/` is also Polars-based, but its
  generic pagination, broad retry, and snapshot behavior are legacy evidence,
  not an API to preserve in OMD.
- OMD typed Tushare endpoints and recipes currently accept and return defensive
  Pandas copies because that is the official provider-native boundary.

## 3. Ownership Freeze

### OMD owns

- typed endpoint requests, effective parameters, fields, pagination, retry
  classification, empty policy, provenance, and stable public errors;
- provider-native Pandas results and provider-semantic recipes;
- an optional, explicit, representation-only Polars adapter;
- validation that conversion does not silently alter names, order, values,
  missingness, temporal meaning, or numeric range.

### `funmoney_backtest` owns

- symbol/universe selection and the `all_symbols` operating mode;
- the Asia/Shanghai 21:00 batch cutoff and pending-session behavior;
- `RAW_BAR_SCHEMA`, `QUALITY_SCHEMA`, `DataProviderResult`, and metadata shape;
- renaming `trade_date -> date`, `vol -> volume`, and `unit_nav -> nav`;
- converting Tushare `amount` from thousand yuan to yuan;
- NAV selection/deduplication, consumer adjustment-factor tolerance, and
  quality-report construction;
- incremental refresh, storage paths, Parquet schemas, canonical sessions,
  PIT transforms, features, strategies, backtests, signals, and live behavior;
- credential discovery and official-client creation, after which the
  initialized client is injected into OMD.

OMD must not import `funmoney_backtest`, read its configuration, discover its
credentials, or expose its normalized schemas as generic SDK contracts.

## 4. Accepted Polars Adapter Contract

### 4.1 Public surface

Add an optional `ohmydata[polars]` extra and an adapter module:

```python
from ohmydata.adapters.polars import pandas_to_polars, polars_to_pandas

polars_frame = pandas_to_polars(pandas_frame)
pandas_frame = polars_to_pandas(polars_frame)
```

These names and the dependency range were accepted by implementation review.
The functions remain explicit; endpoint fetches and recipes do not silently
switch dataframe engines based on installed packages.

The adapter is eager and representation-only. It does not rename columns,
parse provider date strings, scale units, sort rows, deduplicate keys, fill
missing values, compute adjusted prices, or apply consumer schemas. LazyFrame
support is out of scope for the first contract because the source is already
an eagerly materialized provider response.

### 4.2 Required conversion semantics

For a supported frame, conversion must preserve:

- column names and column order exactly;
- row count and row order exactly;
- string values, including empty strings;
- Python/Pandas nulls as Polars nulls;
- IEEE NaN and positive/negative infinity as distinct floating-point values,
  not silently converted to null or zero;
- signedness and width of supported integers without overflow;
- supported float width, with any widening documented and tested;
- booleans as booleans, never integers;
- `date` values as `pl.Date` and timestamps with explicit units/timezones;
- raw/provider columns unchanged and distinguishable from derived columns.

Conversion must fail closed with `SchemaMismatchError` before returning a
partial or lossy frame for:

- duplicate column names;
- heterogeneous or arbitrary Python objects in an object-dtype column;
- unsupported nested objects, periods, intervals, sparse arrays, or extension
  types without a frozen mapping;
- timezone loss, ambiguous mixed-timezone objects, decimal scale/precision
  loss, or integer/temporal overflow;
- a conversion result whose column names, order, shape, or declared dtype
  mapping differs from the preflight contract.

Categorical and decimal support must be either explicitly mapped and tested or
rejected in the first release. It must not be inferred from whatever behavior
the locally installed Arrow version happens to provide.

### 4.3 Dependency isolation

- Core OMD remains free of Pandas, Polars, and PyArrow dependencies.
- `ohmydata[tushare]` remains usable without Polars.
- `ohmydata[polars]` installs only the reviewed Polars/Arrow compatibility
  range needed by the adapter.
- A combined installation such as `ohmydata[tushare,polars]` is the intended
  `funmoney_backtest` dependency.
- The selected Polars range must overlap the pinned consumer range and pass on
  Python 3.11 and 3.12 before it is written into `pyproject.toml` and
  `uv.lock`.

## 5. Pandas/Polars Parity Matrix

Parity means equal market-data semantics, not identical internal dtype names
or byte-for-byte dataframe storage.

| Dimension | Pandas reference | Polars candidate | Acceptance |
|---|---|---|---|
| Structure | columns, order, rows, keys | adapter output | exact |
| Values | provider-native cell values | adapter output | exact for strings/integers/bools; tolerance only for declared floats |
| Missingness | null mask, NaN, `+/-inf` | null mask, NaN, `+/-inf` | each state compared separately |
| Time | date/timestamp value, unit, timezone | mapped Polars dtype/value | no timezone or date-boundary drift |
| Ordering | stable provider/recipe order | adapter order | exact |
| Joins | Pandas recipe keys/results | equivalent Polars observation | identical cardinality and duplicate rejection |
| Errors | malformed input category | adapter/consumer category | same public category at the same boundary |
| Consumer output | legacy Polars `DataProviderResult` | OMD-backed Polars result | exact schema, row/key/order/missingness; numeric tolerance only where frozen |
| Behavior | current Parquet/backtest/signal artifacts | shadow artifacts | identical trade dates and signal sequence |

The comparison report must separate structural, dtype, missingness, numeric,
error, and behavioral results. A single `assert_frame_equal` or successful
command is insufficient evidence.

### 5.1 Synthetic adapter fixtures

At minimum cover:

- empty frames with declared columns;
- nullable and non-nullable integers at width/range boundaries;
- `float32`/`float64`, `-0.0`, NaN, `+inf`, and `-inf`;
- strings, empty strings, booleans, bytes if supported, dates, naive
  timestamps, and timezone-aware timestamps;
- Pandas nullable dtypes and Arrow-backed dtypes selected for support;
- duplicate names, mixed object columns, decimal scale, categoricals, and
  overflow as explicit success or explicit rejection cases;
- mutation isolation in both directions;
- round trip for types promised as lossless, plus one-way comparison for
  intentionally non-round-trippable but supported mappings.

### 5.2 Market-data and threshold fixtures

Reuse one synthetic provider response for both paths and include:

- `fund_daily`, `fund_adj`, `fund_nav`, `fund_basic`, `trade_cal`, and the
  proposed `etf_basic` contract;
- shuffled rows, duplicate keys, missing required fields, optional-empty and
  required-empty responses, partial adjustment coverage, and extra adjustment
  dates;
- Tushare date strings, `vol` in hands, and `amount` in thousand yuan before
  the consumer-owned scaling to yuan;
- values exactly at and immediately around listing, liquidity, adjustment
  coverage, and strategy thresholds;
- null, NaN, and infinity in every market-data numeric class where the public
  contract distinguishes them.

Float comparison tolerances must be named per output boundary. Threshold
decisions and signal sequences must be exact; tolerance must never be used to
excuse a changed boolean decision.

## 6. Migration Blockers Requiring Explicit Decisions

### 6.1 Typed `etf_basic` endpoint

The current `all_symbols=True` path requires `etf_basic(market="E",
list_status="L", ...)`. OMD now exposes this as a distinct typed endpoint;
universe filtering itself remains in `funmoney_backtest`.

The first OMD contract is frozen as follows:

- public request: `EtfBasicRequest`;
- public client method: `TushareClient.fetch_etf_basic(request)`;
- endpoint identity: `etf_basic`;
- official optional filters: `ts_code`, `index_code`, `list_date`,
  `list_status`, `exchange`, and `mgr`;
- compatibility filter: optional `market`, preserved only because the current
  funmoney all-symbol path sends `market="E"`; it is forwarded and recorded in
  effective parameters but is not presented as an official Tushare filter;
- default fields, in provider order: `ts_code`, `csname`, `extname`, `cname`,
  `index_code`, `index_name`, `setup_date`, `list_date`, `list_status`,
  `exchange`, `mgr_name`, `custod_name`, `mgt_fee`, `etf_type`;
- caller-selected field subsets are allowed but must contain `ts_code` because
  it is the endpoint key;
- `list_date` uses `YYYYMMDD`; `list_status`, exchange, manager, index, and
  symbol values remain provider-native strings rather than generic-core enums;
- empty handling is the request's explicit `EmptyPolicy.ALLOW` or
  `EmptyPolicy.ERROR`;
- non-empty results require non-null, unique `ts_code`, preserve requested
  columns, and are stably sorted by `ts_code`;
- the documented single-call limit is 5,000 rows; an exactly full response is
  ambiguous and fails with `PaginationError` rather than pretending coverage;
- provider authentication, permission, retry, provenance, defensive-copy, and
  malformed-frame behavior reuse the existing Tushare client contract;
- OMD does not filter `list_status="L"`, `market="E"`, symbol suffixes, target
  indices, or clone groups after fetching. Those remain caller assertions and
  funmoney universe policy.

The contract is based on Tushare's official ETF basic-information endpoint
documentation (document 385, reviewed 2026-08-02) plus the concrete funmoney
caller. The official page documents the 5,000-row limit and filters other than
`market`; therefore the compatibility parameter must remain visibly separate.

The endpoint's implementation does not authorize consumer migration. During
shadow migration, the factory must not silently downgrade `all_symbols=True`
to another universe source.

### 6.2 Adjustment-factor policy disagreement

The current consumer permits up to 10 missing adjustment rows when
`strict_adj_factor=True`; OMD's recipe offers fail-closed strict coverage or
preserved missing factors. The shadow-parity path must use
`PRESERVE_MISSING_FACTOR` and reproduce the existing named consumer threshold
outside OMD. Moving to OMD strict coverage is a separate behavior change with
its own rerun evidence and approval.

### 6.3 Retry and empty behavior

The legacy provider retries broad exceptions and selected empty frames. OMD
retries only classified transient failures and treats empty results according
to an explicit request policy. Tests must map each legacy outcome to an OMD
exception/empty disposition. Any intentional correction is reported
separately from parity and cannot be hidden in dataframe conversion.

### 6.4 Batch calendar and optional NAV

The 21:00 Asia/Shanghai cutoff, incomplete latest-session handling, per-symbol
fallback, optional NAV behavior, and NAV row selection remain consumer policy.
OMD supplies typed endpoint results and provenance only. These orchestration
branches need direct shadow tests because endpoint-level parity alone cannot
prove them unchanged.

## 7. Delivery Sequence

### Gate 0 — Freeze the live-strategy behavioral baseline

No OMD-backed `funmoney_backtest` provider, shadow, or A-share live path may be
implemented before this gate passes. The canonical behavioral identity is:

| Contract | Frozen value |
|---|---|
| strategy | `B6_B1_2_159599_Live` |
| strategy runtime | `strategy_id=exp54_split_2day`, `runtime_family=exp54_alpha_upgrade` |
| live strategy YAML | `config/strategies/cn_iter03/live/b6_b1_2_159599_live.yaml` |
| semantic replay YAML | `config/strategies/cn_iter04/stage2a/R0_baseline.yaml` |
| canonical run | `outputs/runs/cn_iter03_live/B6_B1_2_159599_Live_20260715_153917` |
| processed bars | `data/processed/etf_daily_bars.parquet` |
| processed-bars SHA-256 | `2159546b0c6330a2a47c7d1268e51bebeebc8865b4cb24823aafb7f0b7e6ae8c` |
| evaluation window | `2018-01-03` through `2026-05-26` |
| annualized return | `0.217717` |
| Sharpe | `1.251109` |
| maximum drawdown | `-0.136980` |
| Calmar | `1.589403` |
| mean turnover per rebalance | `0.516465` |
| execution | monthly phase 19; temporal split `[-1, 0]` / `[0.5, 0.5]`; one-session signal freeze; next-open fill |
| costs | `fee_rate=0.0005`, `slippage=0.00005`, `min_fee=5 CNY` |

The authoritative existing evidence is
`funmoney_backtest/docs/cn_iter4/stage0_baseline_freeze_report.md` and its
machine-readable files under
`outputs/reports/cn_iter04/stage0_baseline_freeze/`. That investigation already
classified the older 22.1721% annualized result as historical adjusted-price
revision evidence, not the current baseline.

Sol reran `uv run python strategies/validation/baseline_matrix.py` in check-only
mode on 2026-08-02. The overall matrix passed and the dedicated
`cn_iter04_b6_b1_2_r0` entry returned `canonical_replay_matched`. That check runs
the current strategy implementation and compares canonical and replay
`equity_curve.parquet`, `positions.parquet`, and `trades.parquet` row by row
with exact value comparison.

The migration acceptance rule is deliberately stricter than dataframe-level
floating-point comparison:

- request/representation parity may report a named float tolerance only before
  consumer decisions, with exact structure, null/NaN/infinity, date, ordering,
  and dtype-category checks reported separately;
- normalized bar keys, filtering, duplicate handling, threshold booleans,
  rankings, rebalance dates, selected symbols, target weights, target-position
  CSV, trades, and equity artifacts must match the canonical path exactly;
- a tolerance must never turn a changed threshold, ordering, target, trade, or
  signal into a pass;
- any mismatch is `BLOCKED_REQUIRES_INVESTIGATION`; it cannot be relabeled as an
  intentional migration correction without a separate strategy-behavior
  decision and new user authorization.

- [x] Identify the current canonical run and distinguish it from archived
      adjusted-price evidence.
- [x] Record strategy, data, date, execution, cost, metric, and artifact
      identities.
- [x] Reproduce the canonical run with current strategy code using the existing
      exact `canonical_replay` gate.
- [ ] Re-run this exact gate against OMD-backed offline shadow artifacts before
      any read-only provider shadow is proposed.

Acceptance: the current legacy path is reproducible exactly. This freezes the
comparison target; it does not authorize an OMD consumer path or live change.

### Gate A — Freeze adapter and endpoint contracts

- [x] Review and freeze supported dtype mappings and fail-closed cases.
- [x] Select compatible Polars/PyArrow versions for Python 3.11 and 3.12.
- [x] Freeze and implement the typed `etf_basic` contract required by the first
      all-symbol shadow.
- [ ] Map legacy runtime/empty failures to OMD public exceptions.
- [x] Record that consumer adjustment tolerance remains unchanged for parity.

Acceptance: no unresolved field, unit, null, date, timezone, ordering, empty,
pagination, or coverage behavior at the planned boundary.

### Gate B — Implement the OMD adapter

- [x] Add optional dependencies, adapter module, public exports, tests, README,
      changelog, and lockfile update in one change.
- [x] Add preflight validation and post-conversion structural checks.
- [x] Test mutation isolation, supported mappings, rejection cases, and
      Pandas/Polars parity offline on Python 3.11 and 3.12.
- [x] Run all OMD canonical checks and inspect built wheel/sdist contents.

Acceptance: core and `ohmydata[tushare]` still work without Polars, while the
combined extra produces deterministic Polars frames without semantic drift.

### Gate C — Add a side-by-side consumer adapter

- [ ] Pin an immutable OMD release in `funmoney_backtest`; never use a moving
      branch or editable cross-repository import for acceptance evidence.
- [ ] Add an OMD-backed provider beside the legacy provider; do not replace the
      factory default yet.
- [ ] Keep credential/client creation, universe selection, cutoff, schema,
      units, normalization, quality reports, and storage consumer-owned.
- [ ] Use the explicit OMD Polars adapter immediately after every OMD Pandas
      result; prohibit scattered local `pl.from_pandas` calls in the new path.
- [ ] Preserve request and provenance metadata without placing tokens or raw
      provider data in artifacts.

Acceptance: both providers can execute against the same offline fake client
and fixture without network access or shared mutable frames.

### Gate D — Offline golden parity

- [ ] Compare exact endpoint call sequence, effective parameters, fields,
      page count, retry classification, and empty/error outcomes.
- [ ] Compare provider-native Pandas versus adapted Polars semantics before
      consumer normalization.
- [ ] Compare final `DataProviderResult`, quality reports, normalized bars,
      incremental merge results, and Parquet schemas/hashes.
- [ ] Rerun representative backtests and signal generation from identical
      frozen input artifacts.
- [ ] Produce a machine-readable parity report with separate sections for
      structure, dtype, null/NaN/infinity, numeric values, requests, errors,
      artifacts, and signals.

Acceptance: zero unexplained differences. Intentional corrections require a
named migration decision and new expected artifacts; they are not parity.

### Gate E — Authorized read-only live shadow

- [ ] Obtain explicit user authorization before any live provider call.
- [ ] Run legacy and OMD-backed paths with the same symbols, date bounds,
      initialized client, and operational cutoff.
- [ ] Do not publish, overwrite, or delete consumer datasets; write only to an
      ignored, isolated comparison location.
- [ ] Compare request identities, coverage, row/key counts, schemas, units,
      missingness, and content hashes without committing provider data.

Acceptance: live differences are either zero or documented and accepted;
licensed/raw provider responses remain outside Git.

### Gate F — Cutover and removal

- [ ] Switch the research ingestion path first and observe at least one normal
      scheduled cycle.
- [ ] Keep a reversible configuration switch during the observation window.
- [ ] Migrate production daily-bar ingestion only after unchanged backtest and
      signal evidence is accepted.
- [ ] Remove legacy Tushare request/retry/pagination code only after all modes,
      including `all_symbols`, are covered.
- [ ] Update consumer README/runbooks/changelog and bump its version according
      to consumer repository rules.

Acceptance: immutable OMD pin, no duplicate provider logic left in active
paths, unchanged consumer contracts and behavior, and a documented rollback
target.

## 8. Required Validation

OMD gates:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
git diff --check
```

Consumer gates follow `funmoney_backtest/AGENTS.md`: focused tests during
development, then full parallel pytest, Ruff, Pyright, artifact inspection,
and backtest/signal rerun evidence before cutover. A live provider shadow is
never implied by these offline commands.

## 9. Out of Scope

- rewriting funmoney's Polars pipeline in Pandas;
- making OMD recipes engine-dispatched or duplicating each recipe in Polars;
- moving consumer normalization, schemas, storage, PIT alignment, strategies,
  or operational scheduling into OMD;
- changing missing-data or adjustment policies during a parity migration;
- yfinance/FMP work, LazyFrame/streaming, GPU execution, or performance claims
  without a separately defined benchmark.

## 10. Frozen Execution Slice 1 — OMD Adapter

Contract status: `ACCEPTED — 2026-08-01`

Delegation-time frozen SHA-256:
`eeb30163ea2ec287cd83de442818e2befe097a3e4d4d4297192cbd3a4eb56a0c`.
The status-only as-built update above occurred after Sol independently reviewed
the implementation, Python 3.11/3.12 tests, static checks, and build artifacts.

### Authorized files

- `src/ohmydata/adapters/__init__.py`
- `src/ohmydata/adapters/polars.py`
- `tests/adapters/test_polars.py`
- `pyproject.toml`
- `uv.lock`
- `README.md`
- `CHANGELOG.md`

Luna must not edit this contract, `PLAN.md`, the behavioral inventory, any
consumer repository, or any other OMD module in this slice.

### Frozen public API

`ohmydata.adapters.polars` exposes exactly:

```python
pandas_to_polars(frame)
polars_to_pandas(frame)
```

The adapter module imports optional dataframe dependencies lazily. Importing
`ohmydata` and its core must continue to work when no dataframe extra is
installed. Calling either function without its required optional dependencies
raises `ImportError` with installation guidance for `ohmydata[polars]` and
without exposing input data.

Both functions:

- require the documented source dataframe type and raise `TypeError` for a
  different source type;
- return an independent eager dataframe and never mutate the input;
- preserve column names/order, row order/count, values, and the distinct
  semantic states null, NaN, positive infinity, and negative infinity;
- perform no rename, date-string parsing, sorting, deduplication, imputation,
  scaling, recipe calculation, or consumer normalization;
- reject duplicate column names and unsupported/lossy dtypes with
  `SchemaMismatchError` containing only structural/dtype context, never row
  values.

The supported v1 surface is intentionally narrow:

- booleans;
- signed and unsigned 8/16/32/64-bit integers, including nullable Pandas
  integer extension dtypes;
- 32/64-bit floating point;
- Pandas object/string extension columns containing only strings and nulls;
- Pandas object columns containing only bytes and nulls;
- Pandas object columns containing only `datetime.date` values and nulls;
- Pandas/Polars dates;
- naive and single-timezone timestamps at supported Pandas/Polars time units.

Categorical, decimal, duration, time-of-day, nested/list/struct/array, mixed
object, period, interval, sparse, complex, and arbitrary extension dtypes are
rejected in v1. Empty object-dtype columns are rejected because their intended
semantic type is unknowable; callers must declare a supported dtype first.

Pandas-to-Polars conversion must explicitly preserve float NaN rather than
coercing it to null. Polars-to-Pandas conversion must use Arrow-backed Pandas
extension arrays where necessary to keep null distinct from NaN. A round trip
may change the dataframe library's dtype spelling, but its value/null/NaN/
infinity semantics and temporal timezone must remain unchanged.

### Frozen dependencies

Add:

```toml
polars = [
  "pandas>=2.0,<3.0",
  "polars>=1.38.1,<2.0",
  "pyarrow>=23.0.1,<24.0",
]
```

Add matching Polars and PyArrow constraints to the development dependency
group and update `uv.lock`. This range intentionally matches the current
`funmoney_backtest` baseline while preserving `ohmydata[tushare]` and core-only
installations.

### Required offline tests

Tests must separately verify:

- supported primitive widths, nullable integers, strings, bytes, dates,
  naive timestamps, timezone-aware timestamps, float null/NaN/infinities, and
  empty frames with explicitly supported dtypes;
- exact column and row order, input/output mutation isolation, and round-trip
  semantic parity;
- wrong source type, duplicate columns, empty object dtype, mixed objects,
  categorical, decimal, duration, nested objects, and integer/temporal loss or
  overflow fail closed without including row values in error text;
- the documented lazy optional-import behavior;
- core-only and Tushare behavior remain covered by the existing test suite.

Required checks are the repository canonical test, Ruff, format, Pyright,
build, and `git diff --check` gates. Luna must inspect the wheel and sdist to
confirm the adapter is packaged and no consumer or provider data is included.

### Stop conditions

Stop with `EXECUTION_BLOCKED` rather than broadening the contract if the frozen
dtype semantics cannot be preserved with the selected versions. No live
provider call, consumer edit, credential access, commit, push, release, or
publication is authorized.

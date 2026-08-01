# Oh My Data (OMD) — Tushare First Plan

## 1. Objective

Build a reusable, provider-aware market-data SDK shared by `funmoney_backtest`
and `stock_notify`.

The first release implements Tushare only. The repository and core contracts
must allow future yfinance and FMP providers without forcing their different
request, adjustment, timezone, quota, and error semantics into a false common
API.

The repository name is `omd`, short for Oh My Data. The confirmed Python
distribution and import namespace are both `ohmydata`.

## 2. Source Repositories

Initial extraction evidence comes from:

- `/Users/ron/Documents/funmoney_backtest`
  - `data_pipeline/tushare_ext/`
  - `data_provider/tushare.py`
  - Tushare-specific research dataset builders and tests
- `/Users/ron/Documents/stock_notify`
  - `data/build_aetf_prices.py`
  - `data/build_etf_dividend_yield.py`
  - `data/build_underlying_yield.py`
  - `data/build_underlying_yield_ts.py`
  - `data/build_index_proxy_prices.py`

These repositories are consumers and behavioral evidence. The SDK must not
import them at runtime or copy their strategy, storage, UI, notification, or
portfolio logic.

## 3. Non-goals for v0.1

- Implement yfinance or FMP.
- Create a universal provider endpoint API.
- Move strategy, backtest, signal, broker, notification, or UI behavior.
- Own consumer universe selection or data publication workflows.
- Own consumer-specific ETF dividend, index-yield, scoring, or publication
  definitions. Narrow provider-semantic calculations may be promoted as
  reusable recipes only when at least two concrete callers share the same
  units, missing-data, coverage, and date semantics.
- Read `.env` files or environment variables inside the library.
- Store real provider responses, tokens, account data, or licensed datasets in
  Git.
- Replace both consumer implementations in one migration.
- Publish to public PyPI before the API, naming, licensing, and support
  expectations are reviewed.

## 4. Architectural Boundaries

### 4.1 Shared core

The provider-independent core may own:

- typed request and fetch-result metadata;
- retry policy and transient/permanent error classification;
- rate-limit policy and injectable clock/sleep functions;
- request identity and canonical parameter encoding;
- snapshot provenance, hashing, validation, and replay;
- common observability hooks;
- provider capability protocols where at least two real consumers need them.

The core must not depend on Pandas, Polars, Tushare, yfinance, FMP, consumer
configuration files, or consumer storage paths.

### 4.2 Tushare provider

The Tushare provider may own:

- injected official Tushare client calls;
- Tushare-specific authentication errors without loading credentials itself;
- endpoint-specific date parameters, page/window behavior, fields, and units;
- retryable Tushare throttling/network failures;
- required response-column and empty-response policies;
- provider-native Pandas responses;
- explicit conversion helpers such as records and an optional Polars adapter;
- reusable provider-semantic recipes, initially adjusted ETF daily bars and
  offline weighted constituent dividend-yield calculations.

It must not own:

- a consumer's symbol universe;
- a consumer's final Parquet/DuckDB schema or paths;
- backtest-ready or consumer-specific feature construction;
- PIT alignment against a consumer's canonical sessions;
- operational cutoffs such as a particular project's nightly sync time;
- silent missing-value imputation.

### 4.3 Consumer-owned logic

`funmoney_backtest` keeps:

- `DataProviderRequest`, `DataProviderResult`, `RAW_BAR_SCHEMA`, and quality
  reports;
- amount/unit mapping into its normalized backtest contract;
- canonical-session and point-in-time research transforms;
- dataset registry paths and research promotion status;
- backtest/signal/live parity and all strategy behavior.

`stock_notify` keeps:

- YAML universe selection and dividend ETF discovery;
- DuckDB/Parquet publication and atomic application refresh;
- dividend-event/AUM calculations, portfolio report and index snapshot
  selection, PIT/as-of policy, expenses, final schemas, and UI features; OMD's
  pure recipes own only the shared weighted-dividend-yield arithmetic and
  finite-value coverage calculation;
- desktop scheduling and notification behavior.

## 5. Provisional Package Layout

```text
omd/
  AGENTS.md
  CHANGELOG.md
  PLAN.md
  README.md
  pyproject.toml
  src/
    ohmydata/
      __init__.py
      core/
        errors.py
        policy.py
        provenance.py
        rate_limit.py
        snapshot.py
        specs.py
      providers/
        tushare/
          client.py
          endpoints.py
          errors.py
          recipes/
            etf_adjusted_bars.py
            weighted_dividend_yield.py
      adapters/
        records.py
        polars.py
  tests/
    fixtures/
    core/
    providers/
      tushare/
```

Do not create empty yfinance or FMP modules in v0.1. Future providers are added
only when a concrete integration task supplies real contracts and fixtures.

## 6. Public Contract Direction

Avoid a generic public API such as:

```python
client.fetch(endpoint, params)
```

as the only consumer-facing abstraction. Provider escape hatches may exist,
but reusable workflows should expose explicit capabilities or typed requests:

```python
result = client.fetch_fund_daily(request)
result = client.fetch_fund_adjustment(request)
result = client.fetch_daily_basic(request)
```

Every fetch result must make these facts inspectable:

- provider and endpoint;
- canonical request identity;
- effective parameters and requested fields;
- attempt count and retry history;
- row/column summary;
- retrieval timestamp and timezone;
- snapshot identities when snapshotting is enabled;
- warnings and explicit empty-result disposition.

The library accepts a token or initialized provider client from the caller. It
must never discover credentials implicitly.

## 7. Error and Missing-data Contract

Define stable exception categories before migrating consumers:

- `AuthenticationError`
- `PermissionDeniedError`
- `RateLimitError`
- `TransientProviderError`
- `PermanentProviderError`
- `EmptyResponseError`
- `SchemaMismatchError`
- `PaginationError`
- `SnapshotIntegrityError`
- `CoverageError`

Only explicitly transient failures are retried by default. Permission, invalid
parameter, and schema failures fail immediately.

Empty results are endpoint/request-policy decisions, not globally success or
failure. A delisted fund outside the requested window may legitimately be
empty; an active required symbol's daily history may not.

Missing numeric values remain missing unless a named consumer transform
explicitly imputes them. In particular, missing `dv_ttm` must not become zero
inside the provider layer, and coverage must measure finite-value weight rather
than total portfolio weight.

## 8. Snapshot and Reproducibility Contract

- Canonical request identity excludes secrets and unstable object
  representations.
- Raw response hash is deterministic for the declared serialization.
- Writes use a temporary path plus atomic rename.
- Concurrent writers cannot create partial response/manifest pairs.
- Existing snapshot content is never overwritten.
- Same-request/different-response behavior is explicit:
  - append a new observation when drift is allowed;
  - fail closed when a frozen/replay policy requires a single identity.
- Replay validates manifest, request identity, response hash, endpoint, and
  serialization version before returning data.
- Tests use synthetic or irreversibly sanitized fixtures only.

## 9. Delivery Phases

### Phase 0 — Bootstrap and behavioral inventory

- [x] Confirm distribution/import names (`ohmydata` for both distribution and import).
- [x] Add `pyproject.toml`, `src/` layout, README, CHANGELOG, and CI.
- [x] Support Python 3.11 and 3.12.
- [x] Inventory duplicated Tushare calls, retry rules, empty semantics, units,
      and adjustment behavior in both consumers.
- [x] Capture offline characterization fixtures and expected results.
- [x] Record unresolved behavior conflicts rather than silently choosing one.

Acceptance:

- package imports on Python 3.11 and 3.12;
- test, lint, and type-check commands run in a clean checkout;
- inventory maps every initial consumer path to SDK or consumer ownership.

### Phase 1 — Core policies and provenance

- [x] Implement canonical request specs and identities.
- [x] Implement error taxonomy.
- [x] Implement bounded retry with injectable sleep/random functions.
- [x] Implement rate limiting without hidden global credentials or paths.
- [x] Implement atomic immutable snapshots and replay validation.
- [x] Test concurrency, tampering, retry classification, and secret redaction.

Acceptance:

- unit tests perform no real network calls;
- permanent failures are never retried;
- snapshot interruption cannot produce a valid-looking partial asset;
- logs and manifests contain no credentials.

### Phase 2 — Tushare endpoint client

- [x] Add injected official-client adapter.
- [x] Define endpoint-specific specs instead of universal `limit/offset`.
- [x] Implement `trade_cal`, `fund_basic`, `fund_daily`, `fund_adj`,
      `fund_nav`, and `fund_share`.
- [x] Add `fund_div`, `fund_portfolio`, `daily_basic`, and `index_weight` only
      after their field, permission, pagination, and empty contracts are fixed.
- [x] Preserve provider-native values and document units.

Acceptance:

- deterministic offline tests cover every endpoint;
- page/window merges have explicit ordering and duplicate policy;
- required fields, empty responses, permission failures, and throttling have
  distinct outcomes.

### Phase 3 — First reusable recipe

- [x] Implement adjusted ETF daily-bar retrieval from `fund_daily + fund_adj`.
- [x] Preserve raw OHLC, raw adjustment factor, and explicit adjustment
      formula inputs.
- [x] Reject incomplete factor coverage unless the caller selects a documented
      alternative policy.
- [x] Do not embed either consumer's final storage schema.

Acceptance:

- golden fixtures match both consumers where their existing semantics agree;
- disagreements are resolved explicitly before migration;
- no float, unit, date, or adjustment drift is hidden by dataframe conversion.

### Phase 3b — Offline weighted dividend-yield recipes

- [x] Add pure, offline calculations for `fund_portfolio + daily_basic.dv_ttm`
      and `index_weight + daily_basic.dv_ttm`.
- [x] Accept already-downloaded provider-native Pandas frames without reading
      files, discovering credentials, or making provider calls.
- [x] Preserve Tushare units explicitly: portfolio `mkv` is yuan, index
      `weight` and `dv_ttm` are percentages, and recipe output is a decimal
      yield.
- [x] Treat zero `dv_ttm` as an observed zero while preserving null, non-finite,
      and absent constituent yields as missing.
- [x] Expose finite-value weight coverage and require an explicit policy for
      incomplete coverage; never turn missing yield into zero. Supported-weight
      renormalization is available only through the explicitly selected
      `NORMALIZE_SUPPORTED` policy, preserves the original coverage metadata,
      and leaves the caller responsible for a named minimum-coverage threshold.
- [x] Require callers to select one portfolio/index snapshot and one
      `daily_basic` observation date before calculation; do not infer PIT
      availability, report selection, staleness, or canonical-session alignment.
- [x] Keep ETF discovery, AUM/fee filters, rolling dividend-event alignment,
      consumer schemas, storage, scoring, scheduling, and publication outside
      the SDK.

Acceptance:

- synthetic offline tests cover portfolio and index weights, complete and
  incomplete coverage, observed zero and negative yields, null/non-finite
  values, duplicates, mixed snapshots, invalid units/weights, empty frames,
  input isolation, and deterministic result metadata;
- the same pure functions can consume frames returned by OMD typed endpoints or
  frames loaded by a consumer from immutable local data;
- `stock_notify` holdings/index yield calculations and `funmoney_backtest` PIT
  dividend datasets are documented as concrete callers, without importing
  either repository or moving their PIT/storage policy into OMD;
- public API documentation states formulas, units, missingness, coverage, and
  non-PIT semantics.

With the published and pinned `0.0.4` registry release, `stock_notify` uses
`NORMALIZE_SUPPORTED` only when finite provider-weight coverage is strictly
greater than `99%`. Coverage at or below that threshold, including unsupported
Hong Kong/QDII constituents, remains unknown rather than being filled from a
different yield definition.

Follow-up recipe candidates require separate reviewed contracts. In
particular, rolling ETF distribution yield from `fund_div + fund_nav` must
freeze ex-date/announcement-date availability, revision/deduplication, NAV
selection, rolling-window, and no-look-ahead rules before implementation.

### Phase 3c — Explicit Polars adapters (planned, not authorized)

- [ ] Add an optional `ohmydata[polars]` extra with a reviewed dependency and
      compatibility range; core and Tushare/Pandas installations must remain
      usable without Polars.
- [ ] Add explicit adapters under `ohmydata.adapters.polars`; do not make
      implicit Pandas/Polars conversion part of endpoint fetching or recipes.
- [ ] Preserve column names, ordering, provider-native units, nulls, dates,
      timezones, integer/float behavior, and raw-versus-derived traceability.
- [ ] Define fail-closed handling for unsupported object values, duplicate
      columns, timezone-aware values, categorical/decimal types, and conversion
      overflow before implementation.
- [ ] Cover round-trip and one-way conversion with synthetic frames containing
      nulls, NaN/infinity, dates, timestamps, large integers, and mixed numeric
      dtypes on Python 3.11 and 3.12.
- [ ] Add a Pandas/Polars parity test group that feeds the same synthetic
      fixtures through both paths and compares structure, numeric results, and
      final signal behavior.
- [ ] Document the explicit boundary for `funmoney_backtest`: its PIT alignment,
      canonical sessions, Polars dataset schemas, storage, and feature logic
      remain consumer-owned after conversion.

#### Pandas/Polars parity contract

The adapter must target semantic parity, not accidental bit-for-bit identity.
The same fixture must be used for both implementations, and parity tests must
cover:

- structural equality: column names, row counts, key uniqueness, ordering,
  date/timezone representation, and raw-versus-derived columns;
- numeric equality: explicit absolute/relative tolerances for floating-point
  results, with deterministic rounding at a named output boundary;
- missingness equality: null, NaN, infinity, empty groups, and divide-by-zero
  outcomes must map to the same contract state;
- join/group behavior: duplicate-key policy, join cardinality, grouping order,
  and stable sorting must match;
- behavioral equality: the final signal date and signal sequence must be equal,
  not merely the intermediate floating-point columns;
- error parity: malformed columns, unsupported dtypes, invalid keys, and
  incomplete coverage must raise the same public error category.

Fixtures must include threshold-boundary values such as a threshold itself and
values immediately above and below it, because a tiny floating-point difference
must not silently flip a signal. Monetary amounts, quantities, fees, and other
fixed-unit values must use the repository's explicit Decimal or integer-minor-
unit policy; statistical ratios may use floating point only with the frozen
tolerance. The test report must show structure, numeric, missingness, and signal
comparisons separately.

This phase is roadmap-only. It requires a separate reviewed contract and is not
authorized by the Phase 3b implementation or its acceptance. No Polars
dependency, adapter module, public export, lockfile change, or consumer migration
may be added until that contract is frozen.

### Phase 4 — Consumer shadow migrations

Migration order:

1. `funmoney_backtest` research Tushare client;
2. `stock_notify` adjusted A-share ETF price fetch;
3. `stock_notify` special Tushare endpoints;
4. `funmoney_backtest` production daily-bar provider last.

For every consumer:

- [ ] pin an SDK tag/commit, never a moving branch;
- [ ] run old and new implementations against the same offline fixtures;
- [ ] compare request sequence, schema, row count, values, missingness, units,
      dates, hashes, retry outcomes, and error outcomes;
- [ ] shadow real-data execution only when explicitly authorized;
- [ ] remove old code only after parity evidence is accepted.

#### Accepted Stock Notify evidence — 2026-08-01

- [x] Pin the exact public registry release `ohmydata[tushare]==0.0.3`.
- [x] Pass offline request, schema, value, missingness, unit, error-boundary,
      client-reuse, and empty-response parity tests for the adjusted ETF bars,
      typed special endpoints, and portfolio/index dividend-yield recipes.
- [x] Complete an explicitly authorized read-only Tushare shadow without
      writing consumer data, credentials, configuration, or publication
      artifacts.
- [x] Verify a `2025-01-27` sample for portfolio `510880.SH` and index
      `000922.CSI`: request order, fields, schemas, row counts, dates, and
      missingness matched the accepted consumer contract.
- [x] Accept and regression-test one explicit correction rather than hiding it
      as parity: the index provider weights totaled `99.998%`, so the legacy
      direct-percent result (`6.569159491%`) was replaced by the recipe's
      actual-total-normalized result (`6.569290876817539%`), a correction of
      approximately `0.0131` basis points with unchanged units.
- [x] Remove the duplicated Stock Notify portfolio/index weighting formulas
      after offline and live evidence were accepted. Stock Notify retains all
      consumer-owned selection, PIT/as-of, fee, storage, and publication logic.

`funmoney_backtest` production migration additionally requires unchanged
backtest/signal behavior and its repository-specific gates.

## 10. Public Repository, Versioning, and Distribution

- This is a public GitHub repository. All committed content must be safe for
  unrestricted public disclosure.
- Never commit provider tokens, `.env` files, credentials, account identifiers,
  real request headers, private repository URLs, consumer configuration, or
  downloaded/licensed provider responses.
- Examples use unmistakably fake placeholders such as
  `TUSHARE_TOKEN=example-not-a-real-token`.
- Before every push, inspect staged changes and run secret scanning. Enable
  GitHub secret scanning/push protection when available for the repository.
- If a secret is ever committed, revoke/rotate it first; deleting it in a later
  commit is not sufficient because Git history remains public.
- Use semantic versions and annotated tags.
- Consumer lockfiles pin an immutable tag or commit.
- Start with provider extras:

```text
ohmydata[tushare]
ohmydata[polars]
```

- Do not make yfinance a dependency of the Tushare-only release.
- Do not publish public packages or real provider fixtures without reviewing
  naming, licensing, data redistribution, and support expectations.

Target milestones:

- `v0.1.0`: stable Tushare core, endpoint client, snapshots, and first recipe;
- `v0.2.0`: yfinance provider after separate inventory and plan update;
- FMP version is intentionally unscheduled.

## 11. Global Acceptance Gates for v0.1.0

1. Python 3.11 and 3.12 pass.
2. Offline pytest, Ruff, Pyright, and packaging checks pass.
3. No test performs an unmarked live provider call.
4. No credential lookup or secret logging exists in library code.
5. Public-repository secret scanning finds no credentials or sensitive
   consumer configuration.
6. Retry behavior is bounded, classified, observable, and deterministic in
   tests.
7. Endpoint pagination/window logic is explicit and tested per endpoint.
8. Snapshots are atomic, immutable, replayable, and integrity checked.
9. Missing and empty data never become plausible fabricated values.
10. Tushare-specific assumptions do not leak into the core package.
11. At least one consumer completes an accepted shadow migration before
    declaring the SDK production-ready.

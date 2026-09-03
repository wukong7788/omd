# Changelog

## 0.1.6 — 2026-09-03

- Added SEC N-PORT Structural Qualification V1 subsystem (`omd sec nport qualify`
  and public Python API `qualify_sec_nport`).
- Implemented offline-only local artifact closure and catalog validation prerequisite
  to ensure qualification never proceeds on partial or tampered local states.
- Implemented exact partition resolution per quarter with strict ambiguity rejection
  (`AmbiguousPartitionError`) and canonical partition set resolution.
- Added comprehensive fact extractors:
  - Quarter & fund coverage facts (`quarter_fund_coverage.parquet`)
  - Vintage quality & exact decimal weight facts (`vintage_quality.parquet`)
  - Amendment & revision family relations (`amendment_facts.parquet`)
  - Identifier presence, multiplicity, duplicates, and conflict facts (`identifier_quality.parquet`)
  - Tamper-evident qualification receipt (`qualification_receipt.json`)
- Added post-persist independent replay validator ensuring byte, logical hash, schema,
  and row count integrity without trusting receipt gates.
- Added new core error types: `AmbiguousPartitionError` and `ResourceLimitError`.

## 0.1.5 — 2026-09-03

- Added SEC EDGAR company financials ingestion and Point-in-Time (PIT) dataset
  generator with isolated optional dependency `sec-financials` (`edgartools` + `pyarrow`).
- Implemented `SecFinancialsClient`, `SecFinancialsRequest`, `SecStatementRow`,
  and `SecCompanyFinancialVintage` for extracting three core financial statements
  (Balance Sheet, Income Statement, Cash Flow Statement) with standardized and native
  XBRL concepts, exact Decimal precision, and official EDGAR acceptance timestamps.
- Added `omd sec financials {sync,inspect,validate}` CLI subcommands with `--config`
  file support, `--latest` and `--limit` single/multi-filing filters, and SHA-256
  signed Parquet lake partitions.
- Migrated codebase static type checker from Pyright to Astral's `ty`.
- Updated `us-data-update` skill to support company financial statements and latest
  quarter reports.

## 0.1.4 — 2026-09-03

- Added offline SEC N-PORT request, transport, artifact, parsing, and EDGAR
  metadata primitives for caller-selected equity ETF series. Tests remain
  offline; network access is explicit and caller-injected.
- Added the resumable `omd sec nport` batch CLI and immutable
  `fund_vintages`/`holdings`/`identifiers` Parquet core dataset, with exact
  receipt lineage, `--config` file support (JSON, YAML, TOML), `--quarters full`
  and single-quarter shorthands, real-time stderr progress reporting and
  `--quiet` suppression, offline validation/inspection, explicit availability
  policy, and Git-safe output-root enforcement.
- Optimized core batch extraction performance with TSV predicate pushdown,
  in-memory collision verification, and cached vintage payload hash properties,
  achieving a >10x extraction speedup across multi-million row quarterly N-PORT
  archives.
- Added support for SEC EDGAR historical submissions files with raw column
  dictionaries and resilient single-series CIK matching across UIT Series ID
  registrations.

## 0.1.3 — 2026-08-09

- Added provider-neutral append-only source-fact registry and bounded offline ETF benchmark/constituent vintage artifacts. Tushare observations remain current-only where source availability is undocumented; completeness is fail-closed.

## 0.1.2 — 2026-08-04

- Added deterministic observed-result capture
  (`capture_tushare_result`, `TushareObservedResult`) that serializes
  validated provider-native frames into append-only snapshots with explicit
  `observed_at`, replay and round-trip verification, and no provider or
  credential access.

- Added typed `stock_basic` and `index_member_all` endpoints with strict
  selector/scope/date validation, explicit all-market and complete-history
  opt-ins, duplicate and conflicting-membership fail-closed semantics,
  provider-native value preservation, and documented row-cap ambiguity
  detection (6000 / 2000).

- Added pure look-through evidence recipes: observed ETF→index mapping
  versions (`build_etf_index_mapping_observations`), index-weight vintage
  audits (`audit_index_weight_vintage`) with separate retrieval, weight-total,
  expected-count, and economic-completeness statuses, and immutable
  manifest-only source bundles (`build_lookthrough_source_bundle`). No
  historical interval, `first_usable_session`, classification, or exposure is
  inferred. Bundles fail closed on missing mapped-index vintages and missing
  per-component industry observations.

- Added `docs/v0.1.2-lookthrough-migration.md` documenting the field-by-field
  consumer boundary and the explicit statement that the historical funmoney
  B6 look-through audit remains blocked.

## 0.1.1 — 2026-08-03

- Added typed Shanghai/Shenzhen ETF PCF constituent endpoints and deterministic
  fail-closed windowed history retrieval around the provider 3000-row limit.

## 0.1.0 — 2026-08-02

- Documented the v0.1.0 public-contract changes, fail-closed upgrade behavior,
  and funmoney/stock_notify consumer ownership boundaries.

- Added offline PIT fail-closed characterization fixtures covering revisions,
  date-only availability, consumer cutoff boundaries, exact snapshot replay,
  `fund_adj` pagination, and historical-vintage evidence.

- Hardened Tushare `fund_nav` and `fund_share` request/response semantics with
  real-calendar validation, scope checks, stable ordering, revision
  preservation, provider-native values, and explicit `fund_share` cap handling.

- Added immutable provider-native `RawFactEnvelope` metadata with conservative
  availability quality flags and explicit same-key row revision evidence.

- Hardened Tushare `daily_basic` selectors and response validation with real
  calendar dates, explicit symbol/date scope checks, stable identity ordering,
  and ambiguous-cap detection while preserving provider-native values.

- Hardened Tushare `index_weight` selectors and response validation: exact or
  complete single-month requests, requested-index/date scope checks, stable
  identity ordering, and provider-native weight preservation.

- Added the typed, injected-client Tushare stock `dividend` endpoint with
  provider-native revision preservation, explicit selectors, fields, and
  empty-result policy.

- Added provider-independent `AvailabilityEvidence` with explicit source,
  observation, UTC normalization, and conservative point-in-time semantics.

- Added immutable provider-independent snapshot observation receipts,
  exact-payload fact versions, and first-observed timestamp semantics while
  preserving legacy snapshot manifests and replay APIs.

- Added typed, injected-client Tushare stock `daily` and `adj_factor` endpoint
  requests with explicit selectors, ordered fields, stable key validation,
  empty-result policy, provenance, and ambiguous-cap detection.

## 0.0.6 — 2026-08-02

- Added an explicit `empty_object_policy` to the Pandas→Polars adapter. The
  default remains fail-closed; callers may opt into nullable Polars `String`
  columns for empty or all-null Pandas `object` columns.

## 0.0.5 — 2026-08-02

- Added the optional, representation-only Pandas↔Polars adapter with eager
  conversions, narrow dtype support, and fail-closed schema validation.
- Added the typed Tushare `etf_basic` endpoint with explicit filters, stable
  key validation and ordering, empty policy, provenance, and cap detection.

## 0.0.4 — 2026-08-01

- Added the explicit `NORMALIZE_SUPPORTED` weighted-dividend-yield coverage
  policy. It normalizes only over finite supported weight, preserves the
  original coverage metadata, reports a distinct formula identifier, and
  leaves minimum-coverage acceptance to the caller.

## 0.0.3 — 2026-07-31

- Added offline weighted portfolio and index dividend-yield recipes with
  explicit finite-coverage policies, provider-native units, and deterministic
  formula metadata.

## Unreleased — 0.0.2

- Fixed adjusted ETF bars to tolerate Tushare factor-date supersets for the
  requested symbol without creating extra output rows. Foreign symbols still
  fail, and strict coverage still requires every daily bar to have a finite
  adjustment factor.

## Unreleased — 0.0.1

- Added the offline-composable adjusted ETF bars recipe with explicit factor
  coverage policy, raw-value preservation, provenance, and validation.

- Added an injected-client Tushare adapter for trade calendar and fund endpoint
  requests with explicit validation, retry, provenance, pagination, and empty
  result policies. Tushare/Pandas remain optional.

- Added the offline-only `ohmydata` package scaffold and Python 3.11/3.12 CI.
- Added synthetic adjusted-ETF bar characterization tests and fixtures.
- Added the initial consumers' Tushare behavioral inventory and conflict log.

- Added provider-independent request identity, stable errors, classified retry,
  rate limiting, provenance, and atomic snapshot/replay primitives.
- Added typed Tushare Phase 2b endpoints for fund dividends, fund portfolios,
  daily basics, and index weights with bounded selectors, explicit fields,
  provenance, duplicate/cap validation, and provider-native units.

# Changelog

## Unreleased

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

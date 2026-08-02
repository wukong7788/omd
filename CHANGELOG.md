# Changelog

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

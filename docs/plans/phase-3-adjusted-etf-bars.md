# Phase 3 — Adjusted ETF Bars Recipe

Status: `ACCEPTED`

## Objective

Implement the first reusable Tushare recipe: retrieve `fund_daily` and
`fund_adj`, preserve provider-native raw values, align them by
`(ts_code, trade_date)`, and expose explicit factor-adjusted OHLC values.

The recipe is provider-semantic infrastructure. It does not own universes,
consumer schemas, storage, point-in-time alignment, operational cutoffs,
feature publication, or notifications.

## Frozen Evidence

- `PLAN.md`
- `docs/behavioral-inventory.md`
- `tests/characterization/test_adjusted_etf_bars.py`
- synthetic `fund_daily.json` and `fund_adj.json` fixtures
- accepted Phase 2 endpoint contracts

The consumers disagree on duplicate-factor handling (keep-first versus
keep-last) and missing-factor tolerance (strict versus up to ten rows).
The SDK must not inherit either implicit choice.

## Authorized Changes

Luna may create or edit only:

- `src/ohmydata/providers/tushare/recipes/__init__.py`
- `src/ohmydata/providers/tushare/recipes/etf_adjusted_bars.py`
- `src/ohmydata/providers/tushare/__init__.py`
- `tests/providers/tushare/recipes/test_etf_adjusted_bars.py`
- `tests/providers/tushare/test_endpoints.py` (only to extend the exact public
  export assertion with the new recipe API)
- `README.md`
- `CHANGELOG.md`

No edits are authorized to `PLAN.md`, this contract, Phase 1 core, Phase 2
client/endpoints/errors, fixtures, consumer repositories, dependencies, or
live-provider configuration.

## Public Contract

Expose:

- `AdjustmentCoveragePolicy = STRICT | PRESERVE_MISSING_FACTOR`
- frozen `AdjustedEtfBarsRequest`
- frozen `AdjustedEtfBarsResult`
- `build_adjusted_etf_bars`
- `fetch_adjusted_etf_bars`

`AdjustedEtfBarsRequest` contains:

- one non-empty `ts_code`;
- optional canonical `start_date`/`end_date`;
- explicit daily `EmptyPolicy`;
- explicit `AdjustmentCoveragePolicy`.

It must create exact Phase 2 `FundDailyRequest` and
`FundAdjustmentRequest` objects using their documented default fields and
`EmptyPolicy.ERROR` for adjustment data when daily rows exist.

`AdjustedEtfBarsResult` contains a defensively copied Pandas frame, the exact
daily provenance, optional adjustment provenance (absent only when an allowed
daily result is empty), the formula identifier
`raw_ohlc_times_provider_adj_factor_v1`, and the selected coverage policy.

## Composition Contract

`build_adjusted_etf_bars(daily_frame, adjustment_frame, coverage_policy)` is a
pure offline composition helper.

- Require Pandas DataFrames.
- Require daily fields `ts_code,trade_date,open,high,low,close` and adjustment
  fields `ts_code,trade_date,adj_factor`.
- Copy inputs defensively.
- Reject null key fields, duplicate daily keys, and duplicate adjustment keys.
  Never keep-first or keep-last.
- Reject adjustment rows for symbols not present in the daily input. Ignore
  additional factor dates for the requested symbol because Tushare may return
  a date superset despite identical request bounds; never expand output from a
  factor-only row, and still require every daily key under strict coverage.
- Preserve every daily column, dtype/value/null semantics where Pandas merge
  permits, and retain raw `open,high,low,close,vol,amount`.
- Preserve the raw provider `adj_factor` as a separate column.
- Stable-sort output by `ts_code,trade_date`.
- Always reject missing, non-numeric, NaN, or infinite raw OHLC values and
  non-numeric/infinite non-null factors. Booleans are not numeric values.
- `STRICT`: every daily key requires one finite factor; otherwise
  `CoverageError`.
- `PRESERVE_MISSING_FACTOR`: missing/null factor is retained as missing and
  every derived adjusted OHLC value for that row remains missing. Never fill
  with zero, one, or another plausible value.
- Derived columns are `adj_open,adj_high,adj_low,adj_close`, each exactly
  `raw_column * adj_factor`.
- Return an empty frame with deterministic columns when daily input is empty;
  do not manufacture rows from adjustment input.

`fetch_adjusted_etf_bars(client, request)`:

1. fetches daily through the accepted typed client;
2. if daily is allowed-empty, returns without calling `fund_adj`;
3. otherwise fetches adjustment data and composes;
4. preserves both endpoint provenances and does not claim PIT availability.

## Error and Safety Contract

- Schema/key/duplicate/numeric failures raise `SchemaMismatchError`.
- Incomplete strict factor coverage raises `CoverageError`.
- Phase 2 endpoint/auth/permission/retry errors propagate unchanged.
- No error text includes raw rows, provider messages, credentials, or
  consumer paths.
- No logging, environment access, live call, file write, scaling, renaming,
  forward fill, backward fill, or imputation is authorized.

## Testing and Acceptance

Offline tests must cover:

- characterization parity and stable ordering;
- exact daily/adjustment request parameters and no dropped dates/fields;
- all four raw and adjusted OHLC columns plus raw factor;
- input/result mutation isolation and formula/policy metadata;
- daily allowed-empty short-circuit and forbidden-empty propagation;
- missing columns, null/duplicate keys, foreign adjustment rows;
- missing/non-numeric/NaN/infinite/bool OHLC and factors;
- strict incomplete coverage failure;
- preserve-missing-factor behavior with derived NaNs and no zero/one fill;
- multi-symbol key alignment without cross-symbol leakage;
- endpoint error propagation and exact provenances;
- no network, environment, storage, strategy, notification, or PIT behavior.

Acceptance requires Python 3.11/3.12 tests, Ruff, format, Pyright, build,
`git diff --check`, and wheel/sdist inspection.

## Sol Review Amendment 1

The initial execution exposed that the accepted Phase 2 test intentionally
asserted the then-exact `ohmydata.providers.tushare.__all__`. The authorized
file list now permits updating only that assertion so the Phase 3 recipe is
actually public rather than merely attached as non-exported module attributes.

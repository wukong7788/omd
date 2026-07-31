# Phase 2 — Tushare Endpoint Client

Status: `ACCEPTED`

## Objective

Implement an offline-testable, injected-client Tushare adapter for
`trade_cal`, `fund_basic`, `fund_daily`, `fund_adj`, `fund_nav`, and
`fund_share`. Preserve provider-native values and nulls, make endpoint
semantics explicit, and reuse the accepted Phase 1 retry, rate-limit, error,
request-identity, and provenance contracts.

This increment does not implement adjusted prices, consumer universes,
point-in-time alignment, publication, notification, AUM/dividend features, or
live Tushare calls.

## Evidence and Scope Freeze

The contract is based on:

- `PLAN.md` and `docs/behavioral-inventory.md`;
- offline consumer fake-client tests;
- official Tushare documentation reviewed on 2026-07-31:
  `trade_cal` doc 26, `fund_basic` doc 19, `fund_nav` doc 119,
  `fund_daily` doc 127, `fund_adj` doc 199, and `fund_share` doc 207.

Relevant documented limits/units are provider semantics:

- `fund_basic`: maximum 15,000 rows;
- `fund_daily`: maximum 5,000 rows; OHLC in yuan, `vol` in hands, `amount`
  in thousand yuan;
- `fund_adj`: maximum 2,000 rows and explicitly supports `offset`/`limit`;
- `fund_share`: maximum 2,000 rows; `fd_share` in ten-thousand shares;
- `fund_nav` values and dates remain provider-native;
- `trade_cal` dates are `YYYYMMDD` provider observation dates.

No observation date is represented as a point-in-time availability timestamp.

## Authorized Changes

Luna may create or edit only:

- `src/ohmydata/providers/__init__.py`
- `src/ohmydata/providers/tushare/__init__.py`
- `src/ohmydata/providers/tushare/client.py`
- `src/ohmydata/providers/tushare/endpoints.py`
- `src/ohmydata/providers/tushare/errors.py`
- `tests/providers/tushare/test_client.py`
- `tests/providers/tushare/test_endpoints.py`
- `tests/providers/tushare/test_errors.py`
- `tests/fixtures/tushare/*.json`
- `pyproject.toml`
- `uv.lock`
- `README.md`
- `CHANGELOG.md`

Luna must not edit `PLAN.md`, this contract, Phase 1 core, characterization
tests, consumer repositories, or add `fund_div`, `fund_portfolio`,
`daily_basic`, `index_weight`, ETF recipes, environment loading, or live tests.

## Dependency Boundary

- The adapter accepts an already initialized official-client-compatible object.
- Library code never imports `.env`, reads environment variables, accepts a
  token, constructs a Tushare client, or inspects client credentials.
- Pandas and Tushare belong in a `tushare` optional extra. Core import must
  continue to work without that extra.
- Offline provider tests may depend on Pandas through the development group,
  but must use fake clients only.

## Public API

Expose from `ohmydata.providers.tushare`:

- `EmptyPolicy = ALLOW | ERROR`;
- frozen endpoint request classes:
  `TradeCalendarRequest`, `FundBasicRequest`, `FundDailyRequest`,
  `FundAdjustmentRequest`, `FundNavRequest`, `FundShareRequest`;
- frozen `TushareFetchResult` with a defensive provider-native Pandas frame,
  `FetchProvenance`, and `page_count`;
- `TushareClient`;
- `classify_tushare_exception`.

Each request must expose its exact `RequestSpec`. Dates accept only canonical
`YYYYMMDD` strings; reversed ranges fail. Fields are ordered tuples and the
client always sends an explicit comma-separated `fields` value.

No arbitrary public `fetch(endpoint, params)` API is authorized.

## Request Contracts

- `TradeCalendarRequest`: `exchange`, optional date range, optional `is_open`,
  explicit `empty_policy`; default fields are
  `exchange,cal_date,is_open,pretrade_date`.
- `FundBasicRequest`: optional `ts_code`, `market`, `status`, explicit
  `empty_policy`, optional ordered `required_ts_codes`; default fields cover
  current consumer metadata including `ts_code`, name/status/list/delist dates,
  management, fund type, benchmark, and market.
- `FundDailyRequest`: exactly one of `ts_code` or `trade_date`; a date range is
  allowed only with `ts_code`; explicit `empty_policy`; default fields are
  `ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount`.
- `FundAdjustmentRequest`: exactly one of `ts_code` or `trade_date`; a date
  range is allowed only with `ts_code`; explicit `empty_policy`; default fields
  are `ts_code,trade_date,adj_factor`; `page_size` is 1..2000 and `max_pages`
  is positive.
- `FundNavRequest`: exactly one of `ts_code` or `nav_date`; a date range is
  allowed only with `ts_code`; optional market; explicit `empty_policy`;
  default fields are the documented native fields.
- `FundShareRequest`: at least one of `ts_code`, `trade_date`, or `market`; a
  date range requires `ts_code`; explicit `empty_policy`; default fields are
  `ts_code,trade_date,fd_share`.

Provider parameters must never be dropped after `TypeError`. Unknown or
unsupported request combinations fail before the provider call.

## Fetch and Validation Contract

For every endpoint:

1. acquire the injected instance-scoped limiter, if configured;
2. invoke the exact official-client method through classified Phase 1 retry;
3. require a Pandas DataFrame result and copy it defensively;
4. apply explicit empty policy;
5. require every requested field and non-null endpoint key fields;
6. preserve column names, values, dtypes, nulls, and provider-native units;
7. return stable endpoint ordering without numeric conversion or imputation;
8. create provenance from the exact effective `RequestSpec`.

Never turn `None`, NaN, or missing market data into zero. Never rename `vol`,
scale `amount`, calculate AUM, calculate adjustment prices, or claim coverage
from row count alone.

Ordering and duplicate rules:

- `trade_cal`: sort by `exchange,cal_date`;
- `fund_basic`: sort by `ts_code`; duplicate `ts_code` fails;
- `fund_daily`: sort by `ts_code,trade_date`; duplicate keys fail;
- `fund_adj`: paginate only with documented `limit`/`offset`, concatenate in
  page order, then sort by `ts_code,trade_date`; any duplicate key within or
  across pages fails with `PaginationError`;
- `fund_nav`: stable sort by `ts_code,nav_date,ann_date`; preserve duplicate
  rows because revisions/announcements may be meaningful;
- `fund_share`: sort by `ts_code,trade_date`; duplicate keys fail.

An exactly full non-pageable response (`fund_basic` 15,000, `fund_daily`
5,000, `fund_share` 2,000) is ambiguous truncation and must raise
`PaginationError`. `fund_adj` continues while a page is full and fails on
`max_pages` exhaustion rather than returning partial data.

`FundBasicRequest.required_ts_codes` is a caller-selected coverage assertion:
missing requested codes raise `CoverageError`; it does not select a universe.

## Error Contract

`classify_tushare_exception` must:

- pass through existing `OhMyDataError` instances;
- classify connection/time-out failures as transient;
- classify stable Tushare authentication, permission, and rate-limit signals
  into `AuthenticationError`, `PermissionDeniedError`, and `RateLimitError`;
- classify every unrecognized provider exception as permanent;
- never include the original exception message, parameter values, or token in
  the mapped exception representation.

Only mapped transient failures are retried. Schema, coverage, empty-policy,
pagination, validation, authentication, and permission errors are never
retried. The original provider exception may be preserved only as an exception
cause; it must not be logged or copied into provenance.

## Testing Contract

Offline fake-client tests must cover:

- exact method name/kwargs and no dropped dates/fields;
- each endpoint success and stable ordering;
- explicit allowed and forbidden empty results;
- missing requested fields, null key fields, malformed non-DataFrame results;
- transient recovery and exhaustion, permanent/auth/permission no-retry;
- message/token redaction;
- `fund_adj` multi-page merge, final short page, duplicate-page failure, and
  max-page exhaustion;
- ambiguous row-cap failure for non-pageable endpoints;
- `fund_basic` partial required-symbol coverage;
- preservation of dtype, null, float, units, and caller/result mutation
  isolation;
- provenance identity, attempts, columns, empty disposition, and page count;
- absence of environment access and real network calls.

Acceptance requires Python 3.11 and 3.12 tests, Ruff, format check, Pyright,
build, `git diff --check`, wheel/sdist inspection, and no secret-like fixture
values.

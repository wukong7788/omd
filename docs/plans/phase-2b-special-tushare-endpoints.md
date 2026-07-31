# Phase 2b — Special Tushare Endpoints

Status: `ACCEPTED`

## Objective

Complete the remaining Phase 2 typed endpoint contracts for `fund_div`,
`fund_portfolio`, `daily_basic`, and `index_weight`. Preserve provider-native
values and missingness, reject ambiguous request shapes before provider calls,
and reuse the accepted Phase 1/2 retry, rate-limit, error, provenance, and
defensive-copy behavior.

This increment does not calculate dividends, AUM, portfolio yield, coverage,
index features, point-in-time alignment, or consumer publication. It does not
perform live provider calls or edit either consumer repository.

## Frozen Evidence and Provider Semantics

The contract is based on:

- `PLAN.md` SHA-256
  `aa0f8a09158307c46a29888be07a556f632ea9fc0f8c7f202dfa385bd4995096`;
- `docs/behavioral-inventory.md` SHA-256
  `90e25cebbbe5f555c4208e97b77a1b9164597d355d1b242c2a6402501ffa115f`;
- accepted Phase 1, Phase 2, and Phase 3 contracts;
- official Tushare documentation reviewed on 2026-07-31:
  `fund_div` doc 120, `fund_portfolio` doc 121, `daily_basic` doc 32,
  and `index_weight` doc 96.

Provider-native semantics that must remain explicit:

- `fund_div.div_cash` is cash per share in yuan and `base_unit` is in
  ten-thousand shares;
- `fund_portfolio.mkv` is yuan, not ten-thousand yuan, and `amount` is shares;
- `daily_basic.dv_ttm` is percent, share fields are in ten-thousand shares,
  and market-value fields are in ten-thousand yuan;
- `index_weight.weight` is the provider percentage;
- `daily_basic` documents a 6,000-row maximum;
- the reviewed official pages do not document a row cap or `limit`/`offset`
  pagination contract for `fund_div`, `fund_portfolio`, or `index_weight`.

No observation, announcement, report, ex-dividend, or trade date is represented
as a point-in-time availability timestamp.

## Authorized Changes

Luna may create or edit only:

- `src/ohmydata/providers/tushare/__init__.py`
- `src/ohmydata/providers/tushare/client.py`
- `src/ohmydata/providers/tushare/endpoints.py`
- `tests/providers/tushare/test_client.py`
- `tests/providers/tushare/test_endpoints.py`
- `README.md`
- `CHANGELOG.md`

Luna must not edit `PLAN.md`, this contract, Phase 1 core, recipe code,
fixtures, dependencies, consumer repositories, environment/loading code, or
live-provider configuration.

## Public API

Expose from `ohmydata.providers.tushare`:

- `FundDividendRequest`
- `FundPortfolioRequest`
- `DailyBasicRequest`
- `IndexWeightRequest`

Add matching typed client methods:

- `fetch_fund_dividend`
- `fetch_fund_portfolio`
- `fetch_daily_basic`
- `fetch_index_weight`

No arbitrary public endpoint fetch API is authorized. Every request is frozen,
requires an explicit `EmptyPolicy`, exposes an exact `RequestSpec`, validates
canonical `YYYYMMDD` dates, and sends an explicit ordered field list.

## Request Contracts

### Fund dividend

`FundDividendRequest` requires exactly one non-empty selector among `ts_code`,
`ann_date`, `ex_date`, and `pay_date`.

Default fields, in order:

`ts_code,ann_date,imp_anndate,base_date,div_proc,record_date,ex_date,pay_date,earpay_date,net_ex_date,div_cash,base_unit,ear_distr,ear_amount,account_date,base_year`.

`ts_code` is the only required non-null key. Stable-sort by
`ts_code,ex_date,ann_date,imp_anndate,pay_date`, with null dates last.
Preserve duplicate rows because announcements and revisions may be meaningful;
never keep-first or keep-last.

### Fund portfolio

`FundPortfolioRequest` requires a non-empty `ts_code` and at least one bounded
report selector: exact `ann_date` or exact `period`. Optional `symbol` may
further narrow the request. A `start_date`/`end_date` report-period range is
allowed only when both bounds are present and within one calendar year; it
counts as the bounded report selector and may not be combined with `period`.
`ann_date` may accompany either exact `period` or a bounded range.

This intentionally rejects the current stock_notify `fund_portfolio(ts_code)`
call. That unbounded call is a migration blocker until the consumer supplies a
report-period policy; the SDK must not silently pretend complete holdings when
the provider page documents no pagination or row cap.

Default fields, in order:

`ts_code,ann_date,end_date,symbol,mkv,amount,stk_mkv_ratio,stk_float_ratio`.

Require non-null `ts_code`, `end_date`, and `symbol`. Stable-sort by
`ts_code,end_date,ann_date,symbol`, with null `ann_date` last. Preserve
duplicate rows because disclosures/revisions may be meaningful.

### Daily basic

`DailyBasicRequest` requires exactly one of non-empty `ts_code` or
`trade_date`. Optional `start_date`/`end_date` are allowed only with `ts_code`.

Default fields are the documented provider-native fields, in order:

`ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,free_share,total_mv,circ_mv`.

Require non-null and unique `(ts_code,trade_date)`, stable-sort by that key,
and raise `PaginationError` when a response contains exactly 6,000 rows because
completeness is ambiguous. Missing `dv_ttm` remains missing.

### Index weight

`IndexWeightRequest` requires a non-empty `index_code` and exactly one of:

- exact `trade_date`; or
- both `start_date` and `end_date` within the same calendar month.

Default fields are `index_code,con_code,trade_date,weight`.
Require non-null and unique `(index_code,con_code,trade_date)` and stable-sort
by that key. Preserve `weight` as the provider percentage.

## Fetch, Validation, and Error Contract

All four methods follow the accepted single-fetch Phase 2 pipeline:

1. validate request identity/key fields before invoking the provider;
2. acquire the injected limiter, if configured;
3. invoke the exact official-client method using classified bounded retry;
4. require and defensively copy a Pandas DataFrame;
5. require every requested field and all endpoint non-null keys;
6. apply the explicit empty policy;
7. stable-sort and apply the endpoint duplicate/cap rule;
8. return exact provenance and a defensive result copy.

Missing numeric values remain missing. No scaling, coercion, fill, aggregation,
deduplication, feature calculation, or coverage claim is authorized.

The implementation may refactor the accepted client’s endpoint metadata so
required fields, non-null keys, sort fields, uniqueness fields, row caps, and
duplicate-preservation rules are represented separately. Existing six endpoint
behavior and public APIs must remain unchanged.

Authentication, permission, rate-limit, transient, permanent, schema, empty,
pagination, and validation outcomes remain distinct under the accepted error
contract. Provider exception text and parameters must not appear in mapped
errors or provenance.

## Testing and Acceptance

Offline fake-client tests must cover:

- request validation before any provider call, including every mutually
  exclusive/bounded selector rule;
- exact method names, parameters, ordered fields, request identities, and
  public exports;
- success, stable ordering, allowed empty, forbidden empty, missing fields,
  null keys, and malformed results for every endpoint;
- duplicate preservation for dividend/portfolio and duplicate rejection for
  daily basic/index weight;
- `daily_basic` ambiguous 6,000-row response failure;
- preservation of missing `dv_ttm`, percent weights, native numeric values,
  dtypes, and defensive-copy isolation;
- transient retry and permanent/auth/permission behavior through at least one
  new endpoint, without weakening the accepted shared classification tests;
- exact provenance, attempts, columns, and empty disposition;
- unchanged behavior of all accepted existing endpoints and the Phase 3
  recipe;
- no network, credential discovery, imputation, consumer logic, storage, or
  point-in-time claims.

Acceptance requires Python 3.11 and 3.12 tests, Ruff, format check, Pyright,
offline build, `git diff --check`, artifact inspection, and secret/boundary
scans. Only Sol may change this contract’s status or check the corresponding
`PLAN.md` item after independent review.

# Phase 0 — Bootstrap and Behavioral Inventory

Status: `READY_WITH_NOTES`

## Objective

Create the first offline-only, installable `ohmydata` package scaffold and a
reviewable behavioral inventory for the two initial consumers. The increment
must make the Phase 0 commands executable without implementing Phase 1 retry,
snapshot, rate-limit, or provider endpoint behavior.

User-visible outcome:

- `ohmydata` imports from a `src/` package on Python 3.11 and 3.12;
- repository test, lint, format, type-check, and build commands are configured;
- CI runs the supported Python matrix without live provider access;
- a durable inventory maps every Tushare call in the initial consumer paths to
  SDK or consumer ownership and records semantic conflicts explicitly;
- synthetic characterization fixtures and tests preserve the agreed adjusted
  ETF bar semantics without containing licensed provider data.

## Governing Contract

This increment is governed by `PLAN.md`. The execution brief must freeze both
this document and `PLAN.md` by SHA-256 before implementation.

## Current-State Evidence

The OMD repository has no package scaffold or committed history yet. All
existing root files are pre-existing, untracked user files and must be
preserved.

The initial consumers contain these relevant implementations:

- `funmoney_backtest/data_provider/tushare.py`
  - `trade_cal`, `fund_basic`, `fund_daily`, `fund_adj`, and optional
    `fund_nav`;
  - consumer-owned symbol selection, cutoff handling, normalized Polars schema,
    quality reporting, and production materialization;
  - retries currently treat broad exceptions and selected empty responses as
    retryable;
  - adjustment-factor gaps are tolerated up to a consumer threshold in one
    path.
- `funmoney_backtest/data_pipeline/tushare_ext/`
  - request identity, generic pagination, retry, snapshots, normalization, and
    research dataset registry behavior;
  - this is extraction evidence, not an API to copy unchanged. In particular,
    generic `limit`/`offset`, broad exception retry, and non-atomic snapshot
    writes do not satisfy the OMD core contract.
- `stock_notify/data/build_aetf_prices.py`
  - fixed YAML universe, `fund_basic`, `fund_daily`, `fund_adj`, `trade_cal`,
    explicit `adj_close = close * adj_factor`, strict complete factor coverage,
    and consumer-owned Parquet publication;
  - retries both exceptions and empty responses with two configured delays.
- `stock_notify/data/build_etf_dividend_yield.py`
  - `fund_basic`, `fund_nav`, `fund_div`, and `fund_share`;
  - ETF discovery, feature calculation, AUM filtering, and publication remain
    consumer-owned.
- `stock_notify/data/build_underlying_yield.py`
  - `fund_portfolio` and `daily_basic`;
  - currently converts missing or unmatched `dv_ttm` to zero and reports total
    portfolio weight as coverage. This is a confirmed consumer bug and must not
    become SDK behavior.
- `stock_notify/data/build_underlying_yield_ts.py`
  - `index_weight` and `daily_basic`;
  - currently converts missing or unmatched `dv_ttm` to zero. This is a
    confirmed consumer bug and must not become SDK behavior.
- `stock_notify/data/build_index_proxy_prices.py`
  - reuses adjusted-price retrieval while retaining consumer-owned pool and
    publication behavior.

## Authorized Files and Modules

Luna may create or edit only:

- `pyproject.toml`
- `uv.lock`
- `.python-version`
- `.github/workflows/ci.yml`
- `README.md`
- `CHANGELOG.md`
- `src/ohmydata/__init__.py`
- `tests/test_package.py`
- `tests/characterization/test_adjusted_etf_bars.py`
- `tests/fixtures/characterization/*.json`
- `docs/behavioral-inventory.md`

The root `PLAN.md`, `AGENTS.md`, `.gitignore`, `.agents/`, `.codex/`, and this
contract are frozen and must not be edited by Luna.

## Package and Tooling Contract

- Distribution and import names are provisionally `ohmydata`.
- Initial version is `0.0.0`; no release or publication is authorized.
- Use a `src/` layout and a minimal PEP 517 build configuration.
- Runtime dependencies are empty in Phase 0.
- Development dependencies may include only tools needed by the canonical
  checks: pytest, Ruff, and Pyright.
- Support Python `>=3.11,<3.13`.
- The package must expose `__version__` matching project metadata without
  importing provider or dataframe integrations.
- CI must run on Python 3.11 and 3.12 and execute offline tests, Ruff lint,
  Ruff format check, Pyright, and a package build.
- Tests and CI must not import Tushare or access the network.

## Characterization Contract

Use unmistakably synthetic JSON fixtures for one fund-daily response and one
fund-adjustment response. Values must not be copied from a real provider
response.

The Phase 0 characterization must assert only semantics already shared by the
initial consumers:

- join `fund_daily` and `fund_adj` by `trade_date`;
- preserve raw `open`, `high`, `low`, and `close`;
- preserve raw `adj_factor`;
- compute `adj_close = close * adj_factor`;
- output stable ascending `trade_date` order;
- reject missing required daily fields;
- reject duplicate adjustment dates instead of silently choosing a row;
- reject incomplete adjustment-factor coverage;
- preserve missing numeric data as missing and reject it for the strict
  adjusted-bar characterization rather than converting it to zero.

The characterization helper belongs in the test module only. Phase 0 must not
create a public recipe or endpoint API before the Phase 1 and Phase 2 contracts
exist.

## Behavioral Inventory Contract

`docs/behavioral-inventory.md` must map every Tushare call in the source paths
named by `PLAN.md` and classify:

- caller and endpoint;
- request fields and date/window/pagination behavior;
- retry and rate-limit behavior;
- empty and required-field behavior;
- units, adjustment, date, timezone, ordering, duplicate, and missing-value
  semantics;
- SDK-owned versus consumer-owned responsibility;
- matching offline test or an explicit evidence gap.

The inventory must include a conflict table. At minimum it must record:

- broad exception retry versus classified transient-only retry;
- generic pagination versus endpoint-specific rules;
- adjustment-factor strictness disagreement;
- consumer operational cutoff disagreement;
- the two `dv_ttm -> 0` bugs and false coverage calculation;
- non-atomic legacy snapshot writes;
- credential discovery in consumers as migration evidence, while affirming
  that the SDK accepts injected credentials/clients only.

Do not claim point-in-time availability from provider observation dates.

## Documentation Contract

- `README.md` owns installation-from-source, the provisional name warning,
  supported Python versions, canonical local checks, offline-only status, and a
  minimal import example.
- `CHANGELOG.md` records this unreleased bootstrap increment without claiming
  Phase 1 behavior.
- `PLAN.md` checklist items remain unchanged until Sol verifies the entire
  relevant acceptance gate.

## Explicit Non-Goals

- No Phase 1 retry, rate-limit, request identity, provenance, or snapshot code.
- No Tushare endpoint client or dependency.
- No Pandas or Polars runtime dependency.
- No yfinance or FMP module.
- No consumer repository edits in this increment.
- No live provider calls, real-data fixtures, credentials, commits, pushes,
  tags, releases, or publication.
- No claim that the confirmed `stock_notify` missing-value bugs are fixed by
  inventory documentation alone.

## Required Checks and Artifact Inspection

Luna must run and report:

```bash
uv lock
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
git diff --check
```

Luna must also inspect:

- the built wheel/sdist file lists;
- fixture contents for synthetic-only data;
- the final diff for files outside the authorized list;
- test collection to confirm no live/integration test is present.

## Acceptance Conditions

Sol may accept this increment only when:

1. all authorized checks pass;
2. both supported Python versions are represented in CI;
3. package metadata, `__version__`, README, and changelog agree;
4. characterization tests demonstrate every listed strict semantic;
5. the inventory covers every initial consumer path and explicitly records all
   known conflicts and evidence gaps;
6. no provider SDK, runtime dataframe dependency, credential lookup, real
   response, or sensitive consumer configuration is present;
7. the actual diff contains no unrelated or unauthorized change.

## Stop Conditions

Return `EXECUTION_BLOCKED` without editing beyond safe partial work if:

- either frozen document hash changes;
- a required consumer behavior cannot be determined from offline source/tests;
- dependency resolution requires adding an unapproved runtime dependency;
- a fixture cannot be proven synthetic;
- an implementation decision would enter Phase 1 or change public data
  semantics.

## Ready Notes

- `ohmydata` is provisional and must be reconfirmed before `v0.1.0`.
- Consumer bug fixes are a separate, consumer-owned increment. Their algorithms
  must use finite-value weight coverage and fail closed below an explicitly
  approved threshold; this Phase 0 increment records but does not repair them.

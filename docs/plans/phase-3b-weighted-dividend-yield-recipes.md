# Phase 3b — Offline Weighted Dividend-Yield Recipes

Status: `READY_FOR_EXECUTION`

## Objective and User-visible Outcome

Add two deterministic, provider-semantic, offline calculations shared by
`stock_notify` and `funmoney_backtest`:

- selected `fund_portfolio` holdings plus one `daily_basic.dv_ttm` cross-section;
- selected `index_weight` constituents plus one `daily_basic.dv_ttm` cross-section.

Both calculations accept already-downloaded provider-native Pandas frames. They
perform no provider calls and no file access. A caller may therefore pass frames
returned by the typed OMD endpoint client or frames loaded from an immutable
local dataset.

## Current-state Evidence and Concrete Callers

`stock_notify/data/build_underlying_yield.py` and
`stock_notify/data/build_underlying_yield_ts.py` independently implement these
weighted calculations. They already expose the required semantics: portfolio
`mkv` is used as a relative yuan weight, index `weight` is a provider percent,
`daily_basic.dv_ttm` is a percent, zero yield is observed data, and incomplete
finite-value coverage must not produce a plausible yield.

`funmoney_backtest/data_pipeline/tushare_ext/datasets/index_weight.py` and
`daily_basic.py`, plus its Iteration 05.4 aggregation, independently normalize
the same provider fields and preserve missing yields. Its PIT session selection,
staleness, snapshot lineage, dataset schemas, and research promotion rules stay
consumer-owned.

The existing roadmap excludes consumer-specific investment features but permits
provider-semantic reusable recipes when concrete callers share the contract.
The two callers above satisfy that promotion gate. This contract does not make
the result a point-in-time feature.

## Authorized Files and Modules

Luna may create or edit only:

- `src/ohmydata/providers/tushare/recipes/weighted_dividend_yield.py`
- `src/ohmydata/providers/tushare/recipes/__init__.py`
- `src/ohmydata/providers/tushare/__init__.py`
- `tests/providers/tushare/recipes/test_weighted_dividend_yield.py`
- `tests/providers/tushare/test_endpoints.py` (only the exact public-export
  assertion required by the new API)
- `README.md`
- `CHANGELOG.md`

Luna must not edit `PLAN.md`, this contract, endpoint/core code, dependencies,
fixtures, either consumer repository, version files, lockfiles, workflow files,
or live-provider configuration.

## Public API

Expose from `ohmydata.providers.tushare`:

- `DividendYieldCoveragePolicy`
- `DividendYieldWeightSource`
- `WeightedDividendYieldResult`
- `build_portfolio_dividend_yield`
- `build_index_dividend_yield`

`DividendYieldCoveragePolicy` is a string enum with exactly:

- `REQUIRE_COMPLETE`: incomplete finite-value weight coverage raises
  `CoverageError`;
- `PRESERVE_INCOMPLETE`: incomplete coverage returns a result whose
  `dividend_yield` is `None`.

No policy may calculate or renormalize a partial-coverage yield.

`DividendYieldWeightSource` is a string enum with exactly `FUND_PORTFOLIO` and
`INDEX_WEIGHT`.

`WeightedDividendYieldResult` is an immutable dataclass with:

- `dividend_yield: float | None`, always a decimal ratio when present;
- `finite_weight_coverage: float`, in `[0, 1]`;
- `provider_total_weight: float`, yuan `mkv` for portfolio input or provider
  percentage points for index input;
- `provider_supported_weight: float`, in the same native unit as total weight;
- `constituent_count: int`;
- `supported_constituent_count: int`;
- `weight_source: DividendYieldWeightSource`;
- `coverage_policy: DividendYieldCoveragePolicy`;
- `formula_identifier: str`.

Formula identifiers are stable constants:

- `fund_portfolio_mkv_weighted_daily_basic_dv_ttm_v1`;
- `index_weight_weighted_daily_basic_dv_ttm_v1`.

Functions take `(weight_frame, daily_basic_frame, coverage_policy)` as explicit
arguments. Keyword-only or positional policy is acceptable only if both public
functions are consistent. Inputs must be Pandas DataFrames and are never
mutated.

## Data and Calculation Contract

### Shared daily-basic input

Require `ts_code`, `trade_date`, and `dv_ttm`. The frame represents exactly one
provider `trade_date`; mixed dates fail with `SchemaMismatchError`. `ts_code`
must be non-null, non-empty, and unique. Extra market symbols are allowed and
ignored after joining to positive-weight constituents.

Finite numeric `dv_ttm`, including zero and negative observations, is supported.
Null, NaN, positive/negative infinity, booleans, strings, and absent constituent
rows are missing. They are never coerced or filled.

### Portfolio input

Require `ts_code`, `end_date`, `symbol`, and `mkv`. The frame represents exactly
one fund and one report `end_date`; mixed funds or periods fail. `symbol` must be
non-null, non-empty, and unique. Every `mkv` must be a finite real number greater
than zero. The provider-native total is the sum of yuan `mkv`; normalized
relative weights exist only inside the formula.

### Index input

Require `index_code`, `con_code`, `trade_date`, and `weight`. The frame represents
exactly one index and one provider `trade_date`; mixed indexes or dates fail.
`con_code` must be non-null, non-empty, and unique. Every provider-native percent
`weight` must be a finite real number greater than or equal to zero, and total
weight must be positive. Zero-weight constituents do not reduce coverage and do
not require a yield observation.

### Formula and coverage

For either source, let `w_i` be the provider-native weight, `W` the sum of all
weights, and `y_i` a finite provider `dv_ttm` percentage. Supported weight is the
sum of `w_i` having a finite `y_i`.

```text
finite_weight_coverage = supported_weight / W
dividend_yield = sum((w_i / W) * y_i) / 100
```

Use `math.isclose(coverage, 1.0, rel_tol=0.0, abs_tol=1e-12)` for complete
coverage. The formula is evaluated only at complete coverage. Do not
renormalize supported weights.

Both empty weight frames and non-positive total weight fail explicitly with
`CoverageError` after required-column validation. An empty but schema-valid
daily-basic frame has zero coverage and follows the selected coverage policy.

## Date, PIT, Provenance, and Security Boundaries

Dates are snapshot identity checks only. The recipe does not parse or infer
announcement availability, first-usable sessions, report publication timing,
staleness, rolling windows, or point-in-time safety. Callers must select inputs
before invoking it.

The result contains deterministic formula and coverage metadata, not fetch or
snapshot provenance. Callers retain endpoint/snapshot provenance alongside the
result. The recipe reads no environment variables, credentials, paths, or
consumer configuration and performs no logging, serialization, or network I/O.

## Explicit Non-goals

- no provider fetch wrapper in this increment;
- no Polars dependency or implicit Pandas/Polars conversion;
- no ETF discovery, portfolio/report selection, index snapshot selection, PIT
  alignment, staleness, fee subtraction, scoring, storage, scheduling,
  notification, UI, or dataset publication;
- no rolling `fund_div + fund_nav` calculation;
- no partial-coverage yield or missing-to-zero behavior;
- no edits to either consumer.

## Implementation Sequence

1. Add enums, immutable result, validation helpers, and shared calculation.
2. Add the two narrow public build functions and exports.
3. Add focused offline tests covering all contract branches and input isolation,
   and update the existing exact Tushare public-export assertion.
4. Document formulas, units, local-data usage, missingness, and non-PIT limits.
5. Add an Unreleased changelog entry without changing the published version.
6. Run focused tests, then all canonical repository gates and inspect the built
   wheel/sdist contents and version.

## Tests and Acceptance Gates

Tests must cover:

- complete portfolio and index calculations with exact decimal outputs;
- observed zero and negative finite `dv_ttm`;
- missing row, null, NaN, infinities, bool, and string `dv_ttm` behavior under
  both policies;
- zero index weight exclusion from coverage requirements;
- invalid/negative/zero-total weights, non-frame inputs, missing fields, null or
  empty symbols, duplicate symbols, mixed funds/indexes, mixed weight dates,
  and mixed daily-basic dates;
- empty schema-valid weight and daily-basic frames;
- extra daily-basic symbols;
- deterministic metadata and exact formula identifiers;
- unchanged input frames after success and failure.

Required commands:

```bash
uv run pytest tests/providers/tushare/recipes/test_weighted_dividend_yield.py
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
git diff --check
```

Inspect the wheel and sdist to confirm the new module and documentation are
included and distribution metadata remains version `0.0.2`. No test may access
the network.

## Acceptance and Stop Conditions

Acceptance requires every public/data contract above, all required checks, no
unexpected files, no secret-bearing content, and documentation consistent with
the implementation. Consumer rerun evidence is required before deleting or
replacing either consumer implementation, but consumer migration is not part of
this increment.

Stop with `EXECUTION_BLOCKED` if the contract would require choosing PIT,
report-selection, staleness, partial-coverage, or consumer storage semantics; if
Pandas is unavailable under the declared Tushare extra; or if pre-existing
changes overlap an authorized file.

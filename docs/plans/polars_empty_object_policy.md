# Explicit Empty Pandas Object Policy

Status: `ACCEPTED`

This contract governs the OMD `0.0.6` adapter slice needed by the
`funmoney_backtest` Tushare shadow migration. A real read-only probe showed
that Tushare may return a validated empty Pandas frame whose requested columns
all have `object` dtype. The current adapter rejects that ambiguity, while the
legacy consumer's direct `polars.from_pandas` path represents those empty
columns as Polars `String` and then applies its existing endpoint-specific
empty policy.

## Objective

Extend `pandas_to_polars` with an explicit caller-selected policy for empty or
all-null Pandas `object` columns, without weakening the existing fail-closed
default or changing any populated-column conversion semantics.

## Public contract

```python
pandas_to_polars(frame, *, empty_object_policy="error")
```

- `empty_object_policy="error"` remains the default and preserves OMD 0.0.5
  behavior.
- `empty_object_policy="string"` permits only `object` columns that contain no
  non-missing values. Before conversion, those columns are copied and cast to
  Pandas' nullable `string` dtype, producing empty/all-null Polars `String`
  columns.
- Any other policy value raises `ValueError` before inspecting/converting
  values.
- Populated homogeneous strings/bytes/dates retain existing behavior.
- Heterogeneous objects, Decimal, nested values, unsupported types, duplicate
  columns, missing optional dependencies, NaN/null distinctions, order, shape,
  and mutation isolation retain existing behavior.
- The source Pandas frame is never mutated.

This is an explicit representation policy, not missing-data imputation. It
does not create rows or replace missing market values with plausible values.

## Authorized files

Luna may modify only:

- `src/ohmydata/adapters/polars.py`;
- `tests/adapters/test_polars.py`;
- `README.md` for the new keyword example and semantics;
- `CHANGELOG.md`;
- `pyproject.toml`;
- `src/ohmydata/__init__.py`;
- `tests/test_package.py`;
- `uv.lock`.

This contract is Sol-owned and must not be edited by Luna.

## Version and publication

- Prepare OMD version `0.0.6` consistently in every release-bearing file.
- Do not alter dependency ranges or add runtime dependencies.
- Do not commit, push, dispatch GitHub Actions, publish, call Tushare, handle
  credentials, or modify either consumer repository.

## Tests

Add offline tests proving:

1. default policy still rejects an empty `object` column;
2. `string` accepts a zero-row object column and yields Polars `String`;
3. `string` accepts an all-null object column, preserves row count/nulls, and
   does not mutate the source;
4. `string` does not permit populated heterogeneous/unsupported object values;
5. invalid policy values fail explicitly;
6. supported typed empty columns and all existing round-trip/null/NaN tests
   remain unchanged.

Required gates:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
git diff --check
```

Inspect built wheel/sdist versions and contents. Before a later release commit,
Sol must also inspect the staged diff and run the repository secret scan.

## Acceptance and stop conditions

Acceptance requires exact default backward compatibility, explicit policy
selection, no source mutation, complete tests/docs/version sync, and all OMD
gates passing on Python 3.11; Python 3.12 verification remains required before
publication.

Stop with `EXECUTION_BLOCKED` if compatibility requires changing the default,
silently guessing populated object types, converting missing values to empty
strings, weakening unsupported-type checks, or changing Tushare endpoint
contracts.

## Non-goals

- Inferring numeric/date types for ambiguous empty provider columns.
- Changing Tushare fetch, empty-response, retry, or schema validation.
- Migrating `funmoney_backtest` or `stock_notify` in this implementation slice.
- Any live provider, data write, strategy, signal, or A-share execution change.

## Sol acceptance evidence

Accepted on 2026-08-02 after independent diff and artifact review:

- Python 3.11: `313 passed`;
- Python 3.12: `313 passed`;
- Ruff lint and format checks: passed;
- Pyright: `0 errors`;
- wheel and sdist built as version `0.0.6` and contain the adapter;
- wheel SHA-256:
  `4c8e4936274dfa6b72df977098ddeaf07b99a0b39ddeb5faa470607f88624728`;
- sdist SHA-256:
  `d3e820a32120918e71f15d09d02be5236e65619204c4e5be7c84dc7619faadb6`;
- the default remains fail-closed, the explicit policy preserves all-null
  values as null, and no provider or consumer code changed.

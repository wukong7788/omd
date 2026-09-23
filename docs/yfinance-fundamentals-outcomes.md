# yfinance fundamentals: per-symbol outcomes

`YFinanceClient.fetch_fundamentals()` returns one
`YFinanceFundamentalsSymbolResult` in `result.symbol_results` for every requested
symbol, in request order. The result holds `outcome`, an optional `record`, and
`sources` keyed by source group (`ticker`, `info`, statement and estimate names,
and `fast_info`). Estimate reads prefer the `get_*` method and fall back to the
corresponding property when that method is absent. Each source has `status` and
`attempts`; failed sources also have a typed `YFinanceFundamentalsSourceError`
with the source,
transient/permanent status, upstream exception **type**, and attempt records.
Upstream exception text is omitted because it can contain request details.

| Outcome | Meaning | `record` |
| --- | --- | --- |
| `COMPLETE` | Material fundamentals were parsed and all requested primary sources returned nonempty data. | Present |
| `INCOMPLETE` | Material data exists, but a requested primary source is empty or a source read failed. | Present |
| `UNAVAILABLE` | Reads succeeded, but no material fundamentals were found. | Absent |
| `TRANSIENT_FAILURE` | No material data was obtained and a transient read failed after retry exhaustion. | Absent |
| `PERMANENT_FAILURE` | No material data was obtained and a permanent read or parse failed. A permanent failure wins if both failure kinds occur. | Absent |

“Material” means a parsed financial or valuation number, a finite numeric
source-info field, an actual report date, or a provider quote type that
explicitly excludes a non-equity security.
The selected source-native `Ticker.info` fields are also retained on
`YFinanceSymbolFundamentals.source_info` as `YFinanceFundamentalsInfoFields`:
`regular_market_time`, `regular_market_price`, `current_price`,
`total_revenue`, and `financial_currency` correspond directly to Yahoo's
`regularMarketTime`, `regularMarketPrice`, `currentPrice`, `totalRevenue`, and
`financialCurrency`. Finite integer/float values retain their Python numeric
type and Yahoo's units (including unscaled revenue); the currency string
retains its provider spelling. Missing, nonnumeric, boolean, NaN, or infinite
numeric values become `None`. The market timestamp remains the original
Unix-seconds value; consumers decide how to render it. It is Yahoo's quote
time, distinct from `result.provenance.retrieved_at`, which is the local fetch
time; neither timestamp alone establishes point-in-time availability. Existing
`valuation.quote_price` and `quote_time` remain normalized OMD fields and keep
their existing selection rules.

The `info` read status and attempts remain available at
`symbol_results[symbol].sources["info"]`. These source-native fields are part
of the material-data test, except `regular_market_time`, which is retained
metadata and does not make a record material by itself. An info response
containing another numeric source field still returns a record and the
ordinary per-symbol outcome.

An empty `info` object, missing accessors, and empty statement/estimate frames
are successful `EMPTY` reads, not failures. Missing fields stay `None`; no value
is converted to zero. `fast_info` is supplemental and its empty state does not
by itself make a record incomplete, but a failed `fast_info` read is reported.
Financial statements and analyst estimates are read only when their request
flags are true. The existing `include_valuation` flag and parsed valuation
fields retain their prior behavior; this change does not redefine that flag.

The request's `retry_policy` (or the client's default) applies **per source**.
`max_attempts` is total attempts, including the first. Timeouts, connection/OS
errors, HTTP 408/429/5xx, and yfinance's rate-limit exception are transient;
permission, missing-file, authentication, other HTTP 4xx, and malformed `info`
errors are permanent. Only transient failures are retried.
`result.provenance.attempts` aggregates source attempts across the
batch; use `symbol_results[symbol].sources[source].attempts` to attribute them.
The client accepts `sleep_fn`, `random_value_fn`, and `clock` injection for
deterministic retry and provenance tests.
Provenance `row_count` counts actual records and its warnings name non-complete
symbols. None of this is evidence of point-in-time availability.

```python
from ohmydata.providers.yfinance import YFinanceFundamentalsOutcome

result = client.fetch_fundamentals(request)
for symbol in result.requested_symbols:
    item = result.symbol_results[symbol]
    if item.outcome in (
        YFinanceFundamentalsOutcome.COMPLETE,
        YFinanceFundamentalsOutcome.INCOMPLETE,
    ):
        use_with_coverage_policy(item.record, item.sources)
    elif item.outcome == YFinanceFundamentalsOutcome.UNAVAILABLE:
        record_known_absence(symbol)
    else:
        report_provider_failure(symbol, item.errors)
```

## Compatibility and release

`YFinanceSymbolFundamentals.source_info` and its nested `to_dict()` export were
added in `ohmydata==0.4.1`. They preserve selected Yahoo-native quote and
revenue fields; a timestamp alone does not make an otherwise empty result
material.

`result.records`, `to_records()`, and `to_dataframe()` keep their shapes for
material records. They now omit unavailable and failed symbols instead of
emitting synthetic all-empty records. Existing callers that index
`result.records[symbol]` for every requested symbol must first inspect
`result.symbol_results[symbol]`; clients must choose their own handling for
`INCOMPLETE`. This behavioral compatibility change is introduced in
`ohmydata==0.4.0`. Consumer repositories still pinned to an earlier immutable
version must migrate deliberately before upgrading their pin. No consumer
scheduling, storage, UI, or data migration is implied by this provider contract.

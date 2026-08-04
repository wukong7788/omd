# Oh My Data (OMD)

`ohmydata` is a provisional, offline-first market-data SDK. Tushare endpoint
adapters accept an already initialized official-compatible client; credentials
are never loaded by this library.

## Supported Python and installation

Python 3.11 and 3.12 are supported (`>=3.11,<3.13`). From a source checkout:

```bash
uv sync
uv run python -c "import ohmydata; print(ohmydata.__version__)"
```

The core has no runtime dependencies. Install `ohmydata[tushare]` for the
Pandas-backed adapter. Provider tests use fake clients and never call a network.

The optional `ohmydata[polars]` extra provides explicit, eager representation
adapters:

```python
from ohmydata.adapters.polars import pandas_to_polars, polars_to_pandas

polars_frame = pandas_to_polars(pandas_frame)
# For validated empty/all-null Pandas object columns, opt into String:
polars_frame = pandas_to_polars(pandas_frame, empty_object_policy="string")
pandas_frame = polars_to_pandas(polars_frame)
```

Conversions preserve columns and row order, provider-native values, nulls,
NaN/infinities, and supported temporal timezones. They do not parse dates,
rename or sort columns, scale units, deduplicate, impute, or apply consumer
schemas. Unsupported or potentially lossy dtypes fail with
`SchemaMismatchError`; the adapter never contacts a provider or reads
credentials. The default `empty_object_policy="error"` rejects ambiguous
empty object columns; the explicit `"string"` policy casts only empty/all-null
object columns to nullable Pandas strings before conversion and never changes
populated object columns or imputes missing values.

## Phase 2 Tushare adapter (offline and injected)

Pass an already initialized official-client-compatible object. The adapter
does not create clients or read credentials; this fake-client example is safe
to run offline:

```python
import pandas as pd
from ohmydata.providers.tushare import EmptyPolicy, FundDailyRequest, TushareClient


class FakeClient:
    def fund_daily(self, **kwargs):
        return pd.DataFrame(
            {
                "ts_code": ["FAKE.ETF"],
                "trade_date": ["20240102"],
                "open": [1.0],
                "high": [1.1],
                "low": [0.9],
                "close": [1.05],
                "pre_close": [1.0],
                "change": [0.05],
                "pct_chg": [5.0],
                "vol": [100],
                "amount": [250.0],
            }
        )


request = FundDailyRequest(
    empty_policy=EmptyPolicy.ERROR, ts_code="FAKE.ETF", start_date="20240101", end_date="20240102"
)
result = TushareClient(FakeClient()).fetch_fund_daily(request)
```

The typed `etf_basic` endpoint preserves Tushare's provider-native metadata and
requires an explicit empty policy. Its official filters are `ts_code`,
`index_code`, `list_date`, `list_status`, `exchange`, and `mgr`; `market` is
forwarded only as a compatibility filter for callers that already use it.

```python
from ohmydata.providers.tushare import EtfBasicRequest

request = EtfBasicRequest(empty_policy=EmptyPolicy.ERROR, market="E", list_status="L")
result = TushareClient(FakeClient()).fetch_etf_basic(request)
```

Typed stock dividend events are available through `StockDividendRequest` and
`fetch_stock_dividend`. Select at least one of `ts_code`, `ann_date`,
`record_date`, `ex_date`, or `imp_ann_date`; selectors may be combined. The
response preserves provider-native dates, process states, values, units, nulls,
and revision or duplicate rows, and does not infer point-in-time availability.

Values and nulls retain Tushare's native semantics: fund daily OHLC and
`change`/`pct_chg` are provider values, `vol` is in hands, and `amount` is in
thousand yuan. Empty responses must be selected explicitly with
`EmptyPolicy.ALLOW` or `EmptyPolicy.ERROR`.
`fund_share.fd_share` remains provider-native in ten-thousand shares (万份);
`fund_adj` and `fund_nav` values are likewise preserved without adjustment or
imputation.
`FundNavRequest` and `FundShareRequest` validate real calendar dates and reject
provider rows outside the requested symbol/date scope. NAV revisions (including
exact duplicates) remain intact; a 2,000-row `fund_share` response is rejected
as an ambiguous provider cap. Announcement and trade dates are date-only
evidence and do not prove an intraday availability timestamp.

### Adjusted ETF bars recipe

`fetch_adjusted_etf_bars` composes `fund_daily` with provider-native
`fund_adj` factors. Choose `AdjustmentCoveragePolicy.STRICT` (the default) or
`PRESERVE_MISSING_FACTOR`; raw OHLC and `adj_factor` remain available beside
the explicitly derived adjusted OHLC columns. The recipe is offline-testable
when supplied an injected `TushareClient` and does not claim point-in-time
availability.
Tushare adjustment responses may contain extra dates for the requested symbol;
the recipe ignores those factor-only dates while strict coverage still requires
a finite factor for every returned daily bar. Rows for foreign symbols fail.

```python
from ohmydata.providers.tushare import (
    AdjustmentCoveragePolicy,
    AdjustedEtfBarsRequest,
    EmptyPolicy,
)

request = AdjustedEtfBarsRequest(
    "FAKE.ETF",
    EmptyPolicy.ERROR,
    AdjustmentCoveragePolicy.STRICT,
    start_date="20240101",
    end_date="20240131",
)
```

### Offline weighted dividend yield recipes

`build_portfolio_dividend_yield` and `build_index_dividend_yield` calculate a
provider-semantic weighted yield from already-downloaded Pandas frames. Portfolio
`mkv` is yuan; index `weight` and `daily_basic.dv_ttm` are provider percentages.
The returned `dividend_yield` is a decimal ratio (`sum((w_i / W) * dv_ttm_i) / 100`),
where `W` is the provider-native total weight.
Choose `DividendYieldCoveragePolicy.REQUIRE_COMPLETE` to reject missing finite
yield coverage, `PRESERVE_INCOMPLETE` to return `None`, or the explicitly named
`NORMALIZE_SUPPORTED` policy to divide only by finite supported weight while
still reporting the original `finite_weight_coverage`. Callers own any minimum
coverage threshold and must not present a normalized partial estimate as full
coverage. Zero supported coverage remains unknown. Inputs are not modified, and
dates are identity checks only: the recipe does not infer point-in-time
availability or report selection.

```python
from ohmydata.providers.tushare import (
    DividendYieldCoveragePolicy,
    build_index_dividend_yield,
)

result = build_index_dividend_yield(
    index_weights_df,
    daily_basic_df,
    DividendYieldCoveragePolicy.REQUIRE_COMPLETE,
)
print(result.dividend_yield)
```

The typed `IndexWeightRequest` accepts either one exact observation date or a
complete inclusive range within one calendar month. Responses are checked for
the requested index and date scope and sorted by index, observation date, and
constituent. `weight` remains the provider-native percentage, including null
or non-finite values; no effective period, availability timestamp, or weight
renormalization is inferred.

## Look-through source facts (v0.1.2)

`capture_tushare_result` serializes an already validated `TushareFetchResult`
deterministically into an append-only `SnapshotStore` and returns an immutable
`TushareObservedResult` binding provenance, snapshot/observation/fact
identities, availability evidence, and a content hash:

```python
from datetime import UTC, datetime
from pathlib import Path

from ohmydata.core import SnapshotStore
from ohmydata.providers.tushare import (
    EmptyPolicy,
    EtfBasicRequest,
    TushareClient,
    capture_tushare_result,
)

request = EtfBasicRequest(empty_policy=EmptyPolicy.ERROR, ts_code="510050.SH")
result = TushareClient(client).fetch_etf_basic(request)
observed = capture_tushare_result(
    SnapshotStore(Path("snapshots")),
    request,
    result,
    observed_at=datetime.now(UTC),
)
```

`observed_at` is explicit and timezone-aware; the capture path never reads
credentials or calls a provider. Typed `stock_basic` and `index_member_all`
endpoints add current stock identity and dated Shenwan industry membership
facts; broad all-market requests require an explicit opt-in, and responses at
the documented row caps (6000 / 2000) fail closed.

The pure recipes `build_etf_index_mapping_observations`,
`audit_index_weight_vintage`, and `build_lookthrough_source_bundle` report
observed ETF→index mapping versions, per-vintage weight/count/retrieval
diagnostics, and a manifest-only source bundle. They never infer historical
effective dates, `first_usable_session`, weight renormalization, industry
backfill, style/cluster labels, or portfolio exposure. Every emitted fact
remains `PIT_UNPROVEN` unless an auditable provider contract proves
otherwise; `index_weight` retrieval completeness is unproven in this release
by contract. Bundle manifests report mapped indices without a captured weight
vintage and record industry-observation coverage for every captured component.

Consumer gaps are documented field-by-field in
[`docs/v0.1.2-lookthrough-migration.md`](docs/v0.1.2-lookthrough-migration.md).

## Local checks

```bash
uv lock
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
git diff --check
```

Behavioral evidence for the initial consumers is in
[`docs/behavioral-inventory.md`](docs/behavioral-inventory.md). The adjusted
ETF characterization is test-only and uses synthetic JSON fixtures.
The public-contract changes and consumer-owned migration boundaries in `v0.1.0`
are summarized in
[`docs/v0.1.0-migration.md`](docs/v0.1.0-migration.md).

Phase 1 core is offline and explicit:

Availability evidence is represented by the dataframe-free
`AvailabilityEvidence` value object. Source-declared timestamps are the only
evidence marked `pit_proven`; inferred schedules, date-only declarations, and
provider-first-observed fallbacks remain conservative. Snapshot construction
uses validated observation receipts and normalizes datetimes to UTC.

```python
from datetime import UTC, datetime
from pathlib import Path
from ohmydata.core import RequestSpec, RetryPolicy, RateLimitPolicy, RateLimiter, execute_with_retry
from ohmydata.core import SnapshotMode, SnapshotStore

try:
    RequestSpec("demo", "bars", {"api_token": "never-serialize"})
except ValueError:
    pass
limiter = RateLimiter(RateLimitPolicy(0.1))
limiter.acquire()
result = execute_with_retry(lambda: "ok", RetryPolicy(max_attempts=1))
store = SnapshotStore(Path("snapshots"))
store.write(
    RequestSpec("demo", "bars", {}), b"[]", datetime.now(UTC), "json-v1", SnapshotMode.APPEND
)
store.write(
    RequestSpec("demo", "bars", {}), b"[]", datetime.now(UTC), "json-v1", SnapshotMode.FROZEN
)
```

`RetryPolicy(max_attempts=3)` counts the first call. APPEND preserves distinct
observations; FROZEN permits one response identity. `SnapshotStore.observe()`
adds immutable, ordered fetch receipts without changing snapshot bytes;
`SnapshotRef.fact_version` identifies the exact request, payload, and
serialization. `provider_first_observed_at()` reports when OMD first persisted
those exact bytes, not provider publication time or consumer usability.
Limiter state is per instance.

Raw provider rows can be wrapped in `RawFactEnvelope`, preserving the
response-level `fact_version` separately from a canonical row hash. Revision
status remains conservative until an explicit same-key prior row is supplied;
point-in-time and date-only availability quality flags are serialized too.
The offline characterization matrix in
[`tests/characterization/test_pit_fail_closed.py`](tests/characterization/test_pit_fail_closed.py)
also proves that late arrivals, date-only evidence, replay mismatches,
pagination truncation, and historical-vintage claims remain fail-closed. OMD
does not choose consumer cutoffs, calendars, dataset commits, or usable
sessions.

## Phase 1 core (offline)

`ohmydata.core` provides canonical request identities, classified retry with
total-attempt semantics, explicit instance-scoped rate limiters, dataframe-free
provenance, and immutable APPEND/FROZEN snapshots. Request parameters reject
secret-bearing keys before serialization. Snapshot callers provide exact bytes;
the core never contacts providers or loads credentials.

## Phase 2b Tushare endpoints

The Tushare adapter exposes typed requests for fund dividends, fund portfolios,
daily basics, and index weights through injected clients. Requests always send
an explicit ordered field list and preserve provider-native values and missing
data. `fund_portfolio` requires a bounded report selector (`ann_date`, exact
`period`, or a same-year `start_date`/`end_date` range); unbounded holdings are
rejected. Native units remain unchanged: dividend cash is yuan per share,
portfolio market value is yuan and amount is shares, daily-basic share and
market-value fields use Tushare's ten-thousand units, and index weights remain
provider percentages. `daily_basic` accepts either a symbol with optional
inclusive calendar-date bounds or one exact trade date; responses are
scope-checked, stably ordered by symbol/date, and exactly 6000 rows are
rejected as an ambiguous provider cap. No availability timestamp or consumer
normalization is inferred.

## Stock daily and adjustment endpoints

The Tushare adapter also exposes typed, injected-client `daily` and
`adj_factor` requests:

```python
from ohmydata.providers.tushare import (
    EmptyPolicy,
    StockAdjustmentRequest,
    StockDailyRequest,
    TushareClient,
)

daily = TushareClient(client).fetch_stock_daily(
    StockDailyRequest(empty_policy=EmptyPolicy.ALLOW, ts_code="000001.SZ")
)
adjustment = TushareClient(client).fetch_stock_adjustment(
    StockAdjustmentRequest(empty_policy=EmptyPolicy.ALLOW, trade_date="20240102")
)
```

Both requests require exactly one symbol (optionally date-bounded) or one
exact trade date, and return stable `ts_code`/`trade_date` ordering. Fields are
explicit and ordered; custom lists must retain both identity fields. Values,
units, and nulls remain provider-native: daily `pct_chg` is a percentage,
`vol` is hands, `amount` is thousand yuan, and `adj_factor` is unmodified.
Suspended rows are not synthesized, and no adjusted-price calculation or
point-in-time availability claim is made.

## ETF PCF constituent endpoints (v0.1.1)

`EtfShConsRequest` and `EtfSzConsRequest` expose the exchange-native
`etf_sh_cons` and `etf_sz_cons` schemas. Shanghai uses `sca` (CNY replacement
amount); Shenzhen uses `sub_cc` and `red_cc` (CNY subscription/redemption
replacement amounts). Quantities are shares and `cpr`/`rdr` are percentages.
Provider values, nulls, sentinels, and duplicate observations remain unchanged.

Each endpoint rejects an ambiguous exactly-3000-row response. Use
`fetch_etf_pcf_history` with an explicit exchange, date range, and
`EmptyPolicy` to recursively bisect calendar windows without offsets. The
recipe reports successful leaf provenances, request and truncation counts, and
returns a defensive provider-native Pandas frame. A `trade_date` is date-only
provider evidence; availability timestamps, point-in-time lag, cross-exchange
normalization, and published dataset policy remain consumer responsibilities.

The recipe accepts an injected offline-capable client; the library performs no
credential or environment lookup:

```python
from ohmydata.providers.tushare import (
    EmptyPolicy,
    EtfPcfHistoryRequest,
    fetch_etf_pcf_history,
)

history = fetch_etf_pcf_history(
    client,
    EtfPcfHistoryRequest(
        ts_code="510050.SH",
        exchange="SH",
        start_date="20240101",
        end_date="20240131",
        empty_policy=EmptyPolicy.ALLOW,
    ),
)
frame = history.frame
```

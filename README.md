# Oh My Data (OMD)

[English](README.md) | [中文说明](README_zh.md)

`ohmydata` is an offline-first market-data ingestion SDK and CLI. Provider endpoint
adapters accept already initialized official-compatible clients; credentials
are never loaded by this library.

## Supported Python and installation

Python 3.11 and 3.12 are supported (`>=3.11,<3.13`). From a source checkout:

```bash
uv sync
uv run python -c "import ohmydata; print(ohmydata.__version__)"
```

The core has no runtime dependencies. Install `ohmydata[tushare]` for the
Pandas-backed Tushare adapter, `ohmydata[yfinance]` for US/global market data and
fundamentals, `ohmydata[sec-cli]` for the SEC N-PORT batch CLI, or
`ohmydata[sec-financials]` for company 10-K/10-Q financial statements and Parquet
dataset writer. Provider tests use fake clients and never call a network.

## Core Architecture (offline & immutable)

`ohmydata.core` provides canonical request identities, classified retry with
total-attempt semantics, explicit instance-scoped rate limiters, dataframe-free
provenance, and immutable APPEND/FROZEN snapshots. Request parameters reject
secret-bearing keys before serialization. Snapshot callers provide exact bytes;
the core never contacts providers or loads credentials.

Availability evidence is represented by the dataframe-free
`AvailabilityEvidence` value object. Source-declared timestamps are the only
evidence marked `pit_proven`; inferred schedules, date-only declarations, and
provider-first-observed fallbacks remain conservative. Snapshot construction
uses validated observation receipts and normalizes datetimes to UTC.

```python
from datetime import UTC, datetime
from pathlib import Path

from ohmydata.core import (
    RateLimiter,
    RateLimitPolicy,
    RequestSpec,
    RetryPolicy,
    SnapshotMode,
    SnapshotStore,
    execute_with_retry,
)

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

### Dated instrument identity declarations

`InstrumentIdentityIndex` separates issuer IDs, securities and dated provider
aliases. Queries require an exact provider/alias/venue, effective date and
knowledge cutoff; missing mappings raise `CoverageError`. All interval overlaps
reject the proposed catalog, even if recorded later. Retain prior catalogs for
replay; v1 has no metadata revision or revocation model. Caller evidence is not
verified listing history, market availability or financial approval.

```python
from datetime import UTC, date, datetime
from ohmydata.core import (
    InstrumentIdentity,
    InstrumentIdentityIndex,
    InstrumentType,
    IssuerIdentity,
    ProviderInstrumentAlias,
)
from ohmydata.providers.sec import SecDataVersionId, SecDataVersionKind

recorded = datetime(2025, 1, 1, tzinfo=UTC)
issuer = IssuerIdentity("demo:issuer", "evidence:issuer", recorded)
security = InstrumentIdentity(
    "demo:class-a",
    issuer.issuer_id,
    InstrumentType.COMMON_SHARE,
    "evidence:security",
    recorded,
)
alias = ProviderInstrumentAlias(
    "demo",
    "EXAMPLE",
    "XNAS",
    "USD",
    security.instrument_id,
    date(2025, 1, 1),
    date(2026, 1, 1),
    "evidence:listing",
    recorded,
)
catalog = InstrumentIdentityIndex([issuer], [security], [alias])
resolution = catalog.resolve(
    provider="demo",
    alias="EXAMPLE",
    venue="XNAS",
    effective_date=date(2025, 1, 1),
    knowledge_cutoff=recorded,
)
dependency_input = SecDataVersionId(
    SecDataVersionKind.INSTRUMENT_IDENTITY,
    resolution.binding_identity,
)
# Explicit inputs for a SecMetricTerminalDeclaration or SecMetricExternalInput:
security_basis = resolution.instrument.instrument_id
declaration_reference = resolution.resolution_identity
```

`binding_identity` includes selected issuer/security/alias evidence and is stable
across unrelated catalog changes and query cutoffs. `resolution_identity`
additionally binds the complete catalog and query. Opaque issuer IDs do not
encode a CIK contract; SEC callers supply CIK separately. Effective intervals are
required half-open dates, not timestamps or trading sessions. Limits and scope
are in the [identity contract](docs/plans/instrument-identity-declarations.md).

## Tushare Provider (A-Share & China ETF Ingestion)

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

### Stock daily and adjustment endpoints

The Tushare adapter exposes typed, injected-client `daily` and `adj_factor` requests:

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

### ETF PCF constituent endpoints

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

### Look-through source facts and vintage plane

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

The optional `ohmydata[vintage-plane]` extra provides the ETF benchmark and
constituent vintage artifact assembler. It accepts only caller-constructed,
bounded requests and synthetic captured observations; it never discovers
credentials or universes. Current Tushare mappings are current-only evidence,
not historical point-in-time availability, and `index_weight` retrieval and
economic completeness remain explicitly unproven under the provider contract.

```python
from datetime import UTC, datetime
from pathlib import Path

from ohmydata.core import SnapshotStore, SourceFactRegistry
from ohmydata.providers.tushare import (
    EtfBenchmarkConstituentScope,
    assemble_etf_benchmark_constituent_vintages,
)

# Offline synthetic assembly; no credentials or network are used.
bundle = assemble_etf_benchmark_constituent_vintages(
    [],
    store=SnapshotStore(Path("snapshots")),
    registry=SourceFactRegistry(Path("registry")),
    scope=EtfBenchmarkConstituentScope(),
    cutoff=datetime(2026, 1, 1, tzinfo=UTC),
    output_dir=Path("bundle"),
)
```

OMD owns source evidence and immutable lineage only. Consumers own canonical
cutoffs, trading calendars, session alignment, normalized datasets, and all
strategy or portfolio semantics.

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
    AdjustedEtfBarsRequest,
    AdjustmentCoveragePolicy,
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

## SEC N-PORT Holdings Provider & CLI

The `ohmydata.providers.sec` package provides offline-testable primitives for
the official quarterly N-PORT data set: caller-selected series, immutable
artifact retention, native Decimal/date/null values, and EDGAR acceptance
metadata. It deliberately does not classify funds, resolve tickers, compute
trading sessions, or perform an implicit live download; network access requires
an explicit caller-created client and contact User-Agent.

Install the Parquet writer and run the batch CLI with a reviewed equity-ETF
universe containing exact CIK/series identities:

```bash
uv sync --extra sec-cli

# Run full-history sync via a configuration file:
uv run omd sec nport sync --config artifacts/sec-sync.yaml

# Or inspect plan, fetch, or validate using the same configuration:
uv run omd sec nport plan --config artifacts/sec-sync.yaml
uv run omd sec nport validate --config artifacts/sec-sync.yaml

# Or with explicit flags (--quarters full expands 2019q4 through the latest completed quarter):
uv run omd sec nport sync \
  --quarters full \
  --root artifacts/sec-nport \
  --universe artifacts/sec-equity-etfs.json \
  --user-agent-file /path/to/private-sec-contact.txt \
  --availability-policy accepted-at-plus-lag \
  --lag-days 0
```

`--config FILE` accepts `.json`, `.yaml`, `.yml`, or `.toml` mappings (such as
`artifacts/sec-sync.yaml` with `quarters: full`, `root: artifacts/sec-nport-full`,
`universe: artifacts/sec-equity-etf-universe.json`, `user_agent_file: artifacts/sec-contact.txt`,
`availability_policy: accepted-at-plus-lag`, and `lag_days: 0`) to eliminate
repetitive flags; explicit CLI options override configuration values.
`--quarters full` expands to `2019q4` through the latest completed calendar
quarter, while single-quarter tokens (e.g. `--quarters 2026q2`, or
`--quarter latest` for `inspect`) are supported as shorthands.

`fetch` retains each replay-valid quarterly ZIP and the exact EDGAR metadata
closure, while `build` works offline from those retained artifacts. `sync`
processes one quarter at a time and resumes without downloading a completed
quarter again. `validate` checks the complete local artifact/catalog closure;
`inspect` prints safe summaries and only emits holding rows when `--rows` is
explicitly supplied. `fetch`, `build`, and `sync` emit safe progress milestones
to stderr (quarter indices, download bytes, and build timing), which can be
silenced with `--quiet`. Quarterly ZIP responses are streamed to immutable
storage; `build` and `sync` also accept positive `--max-selected-rows` and
`--max-output-bytes` limits (defaulting to 5,000,000 rows and 2 GiB) and fail
before partition publication when either bound is exceeded.

Each immutable core partition contains `fund_vintages.parquet`,
`holdings.parquet`, and `identifiers.parquet`, plus quality and manifest JSON.
Provider-native percentages remain percentage points, numeric text is preserved
beside exact Decimal values, and missing values remain missing. Consumers own
security-master mapping, exchange calendars, first-usable-session alignment,
and strategy features.

The output root must be outside the repository or already Git-ignored. Contact
data is accepted only through a file or explicit stdin and is never written to
artifacts or output. Universe classification is caller-reviewed; the intended
equity-ETF research universe excludes GLD, bond ETFs, and money-market/currency
ETFs.

### SEC Company Financials (10-K & 10-Q PIT via EdgarTools)

The `sec-financials` extra wraps `edgartools` with strict credential injection,
core isolation, and explicit filing and availability metadata for
the three core financial statements (**Balance Sheet**, **Income Statement**, and
**Cash Flow Statement**):

```bash
uv sync --extra sec-financials

# Sync company financials via an OMD configuration file:
uv run omd sec financials sync --config artifacts/sec-financials.yaml

# Or inspect local partitions:
uv run omd sec financials inspect --root artifacts/sec-financials --symbol AAPL --rows

# Or validate local Parquet partition checksums:
uv run omd sec financials validate --root artifacts/sec-financials
```

Python SDK example with injected credentials:

```python
from ohmydata.providers.sec import (
    SecFinancialsClient,
    SecFinancialsRequest,
    SecHttpClient,
    write_financials_partition,
)

# Injected client reading identity strictly from contact info (no .env):
user_agent = "MyResearchApp/1.0 (contact@example.com)"
client = SecFinancialsClient(user_agent, http_client=SecHttpClient(user_agent))

request = SecFinancialsRequest(
    symbols=("AAPL", "MSFT"),
    forms=("10-K", "10-Q"),
    availability_policy="accepted-at-plus-lag",
    lag_days=0,
)

vintages = client.fetch_company_financials(request)

# Partitioned Parquet data lake writing:
for symbol in ("AAPL", "MSFT"):
    sym_vintages = [v for v in vintages if v.symbol == symbol]
    write_financials_partition("artifacts/sec-financials", symbol, sym_vintages)
```

Each vintage records EDGAR's official `accepted_at` timestamp and computes
`availability_anchor = accepted_at + lag_days`. Financial statement rows
preserve native line item labels and concepts (`concept`, `label`, `value_native`).
The native presentation-tree path currently sets `standard_concept` to the
native concept; this field does not establish a cross-company taxonomy mapping.
Compatibility inputs may supply a separate `standard_concept`, whose semantics
still require explicit validation before quantitative comparisons.

Live requests default to
`parser_version="sec-live-financial-parser-v2-edgartools-5.56.0"`. This mode
requires the selected filing's bounded raw XML instance, corroborates native
facts against it, and preserves complete units. Missing, ambiguous or mismatched
unit evidence raises `SecUnitEvidenceError`; the call does not return a partial
batch or silently switch versions. The raw instance download has its own byte
budget; this is not a hard deadline on edgartools' other filing requests.

Evidenced vintages carry `SecFinancialUnitEvidence` and use v4 identities and
Parquet partitions. Statement rows in v4 explicitly identify their vintage,
including mixed old/new results for the same accession. V3-only writes and old
vintage identities remain unchanged. Existing immutable roots must be retained;
write changed data to a new root. Older SDK validators may reject v4, so consumers
must adopt a compatible pinned version and validate their reruns before switching.
Evidence records the original instance identity, without retaining its bytes or
claiming authenticated origin, financial quality, publication time, or live replay.

For explicit legacy behavior, select
`sec-live-financial-parser-v1-edgartools-5.56.0` in the request or the financials
CLI's `--parser-version` option. V1 retains its known compound-unit limitations.
Standalone `parse_statement_rows` retains its legacy default; explicit v2 also
requires `raw_instance` and a native statement. See the
[live unit evidence contract](docs/plans/sec-live-unit-evidence.md).

For offline, explicitly versioned PIT research, the SEC package also exposes
`serialize_sec_typed_rows_projection`, `SecNormalizedFinancialFactVersion`,
`SecPitPolicy`, and `select_sec_financial_versions`. The caller writes the
canonical typed-row projection into its `SnapshotStore`, supplies exact
`SOURCE_DECLARED` timestamp evidence, then explicitly selects
`MARKET_KNOWN` or `SYSTEM_REPLAY`. This projection binds an accession, typed
row and caller-attested source-artifact identity, but is not original SEC
XBRL/SGML validation. `SYSTEM_REPLAY` additionally requires an exact PASS
quality record and consumer commit before the requested cutoff. See the
[SEC financial production PIT contract](docs/plans/sec-financial-production-pit-slice.md).

For a runnable offline introduction, execute
`uv run python examples/sec_offline_research.py` from a repository checkout.
The [synthetic research example](examples/sec_offline_research.py) creates four
synthetic quarters, writes and reloads an immutable PIT bundle, and demonstrates
publication, consumer-commit and later-quarantine cutoffs. It verifies a TTM of
100.00 USD with four input identities. All timestamps and quality/commit records
are synthetic declarations; this does not qualify real SEC data for market
reconstruction or prove a research strategy. It uses temporary local files and
needs no credentials or network access.

`compute_sec_four_quarter_ttm` provides offline revenue and net-income sums from
exactly four declared independent fiscal quarters. `SecQuarterTtmConfig` binds
their normalized version IDs, native concepts, fiscal dates, accounting scope,
comparability cohort and declaration references to an explicit PIT mode, cutoff
and policy. The function reruns the existing selector and returns exact Decimal
output, input lineage and an `input_availability_bound`. That bound describes
the selected inputs; the consumer supplies the derived result's publication and
first usable session. Issuer ownership, quarter independence and comparability
remain explicit caller assertions. The recipe validates structural agreement
with the selected rows. See the [four-quarter TTM contract](docs/plans/sec-quarter-ttm-recipe.md)
for the input, arithmetic and declaration limits.

```python
from ohmydata.providers.sec import compute_sec_four_quarter_ttm

# config is a SecQuarterTtmConfig containing four caller declarations and references.
ttm = compute_sec_four_quarter_ttm(
    config=config,
    versions=normalized_versions,
    quality_records=quality_records,
    consumer_commits=consumer_commits,
    max_input_records=10000,
)
```

`compute_sec_metric_graph` evaluates a bounded, topologically ordered graph of
fixed recipes: four-quarter or FY+current-YTD−prior-YTD revenue/net-income TTM,
CFO−CapEx, YoY, margins, PE, PS and FPE. SEC terminals pass through the existing
PIT selector once; market-cap, security-price and forecast-EPS inputs remain
distinct caller attestations with explicit source, observation, quality and
optional commit references. The graph does not authenticate those attestations.

Every terminal declares period/fiscal labels, accounting and attribution scope,
currency, dimensions, comparability cohort and security basis. CapEx sign,
denominator policy, division precision and rounding are explicit. PE/PS require
company-total equity and a graph-derived TTM denominator; FPE preserves the
forecast horizon and requires identical `currency/security` units and security
basis. No implicit FX, share-class/ADR conversion or PE×EPS price reconstruction
is performed. Domain policies can return a labeled missing final value for an
invalid denominator; missing source coverage and incompatible basis fail.

Results retain complete selected SEC evidence or labeled external attestations,
transitive input identities and input availability bounds. Monetary arithmetic
is exact; division uses an isolated Decimal context and reports a conservative
absolute error bound. Rounded ratio outputs are final outputs only. See the
[neutral metric graph contract](docs/plans/sec-neutral-metric-graph.md).

```python
from ohmydata.providers.sec import compute_sec_metric_graph

metrics = compute_sec_metric_graph(
    config=metric_graph_config,
    versions=normalized_versions,
    quality_records=quality_records,
    consumer_commits=consumer_commits,
    external_inputs=external_attestations,
)
final_metric = metrics.final
```

`discover_sec_filing_events` replays a retained SEC submissions root and every
historical file declared by that root. The caller supplies a CIK, selected forms,
UTC acceptance window, overlap duration and incremental/reconciliation mode.
It preserves acceptance metadata, emits filing events and exact 8-K item 2.02
events, and fails on missing pages or conflicting facts. Acceptance is not proof
of the website's first publication time.

`SecEventDiscoveryLedger` atomically stores discovered events and their cursor in
immutable generations. On POSIX filesystems, appenders cooperate through a writer
lock; `load()` replays the retained source observations and reconstructs the
entire committed chain. An exact retry returns its original receipt, even after
later appends. The ledger records discovery; execution, retry scheduling and
consumer publication remain separate. Resource caps and snapshot request
bindings are specified in the [event discovery contract](docs/plans/sec-event-discovery-ledger.md).

```python
from ohmydata.providers.sec import SecEventDiscoveryLedger, discover_sec_filing_events

ledger = SecEventDiscoveryLedger(ledger_path, store=snapshot_store)
head, discovered = ledger.load()
batch = discover_sec_filing_events(
    snapshot_store,
    retained_root_source,
    retained_history_sources,
    policy=discovery_policy,
    prior_cursor=None if head is None else head.cursor,
)
receipt = ledger.append(batch, expected_receipt_id=None if head is None else head.receipt_id)
```

`SecEventWorkLedger` records explicit caller reports through DISCOVERED, QUEUED,
FETCHING and VALIDATING to READY, QUARANTINED or FAILED, with bounded transient
retry waits. It binds one immutable work specification and discovery batch to a
separate caller-selected directory. Entering FETCHING counts a total attempt;
the caller supplies timestamps and retry deadlines. Replaying the journal
reconstructs every transition from retained discovery evidence. READY preserves
the previously reported outputs and requires quality-reference IDs; the report
does not issue quality approval or publish data.

`SecDependencyIndex` holds immutable, caller-declared typed version edges.
`plan_sec_event_invalidation(index, changed_inputs, known_at=...)` returns exact
reachable output IDs and traversed edge IDs using only edges recorded by that
cutoff. Recipe/configuration versions can be explicit inputs. Unknown inputs
have no known dependent output; that result does not prove complete lineage.
The plan identifies existing outputs for reconsideration without changing their
historical availability. See the [work and invalidation contract](docs/plans/sec-event-work-and-invalidation.md).

```python
from ohmydata.providers.sec import SecEventWorkLedger, plan_sec_event_invalidation

work = SecEventWorkLedger(work_path, store=snapshot_store)
head, state, registered_edges = work.load()
report_receipt = work.append(
    batch,
    work_spec,
    work_command,
    expected_receipt_id=None if head is None else head.receipt_id,
)
invalidation = plan_sec_event_invalidation(
    dependency_index, changed_version_ids, known_at=knowledge_cutoff
)
```

`write_sec_pit_bundle` can freeze a caller-selected receipt closure through
`SnapshotStore`; `load_sec_pit_bundle` rebuilds it only from an injected
observation-ID resolver and source store, without persisting source bytes or paths.
Both functions default to at most 10,000 input receipts and 8 MiB per bundle or
replayed source payload; `max_records` and `max_bytes` accept stricter positive
integer limits. See the [bundle replay contract](docs/plans/sec-pit-bundle-replay-slice.md).

`SecQualityFinding` and `select_sec_quality_findings` additionally provide a
separate, bounded history of caller-authored normalized-row quality assertions.
Their evidence references are only caller-supplied observation/fact identities:
they do not prove source correctness. Passing a non-empty `quality_findings`
to `write_sec_pit_bundle` writes a v2 bundle and requires the same injected
source store and observation resolver used at replay; every referenced
observation is replay-verified without persisting payload bytes or paths.
`evaluate_sec_structural_quality` can create bounded OPEN findings for absent
row identity fields, incomplete or unknown periods, and exact duplicate-row
value disagreements in supplied replay-bound normalized versions. Its report
records the exact versions and fixed rules checked; it does not prove a filing
or value is correct, establish availability, or change a PIT/quality-policy
decision. Automatic accounting checks and financial-value corrections are not
implemented. The [quality-finding contract](docs/plans/sec-quality-findings-slice.md),
[structural-rule contract](docs/plans/sec-structural-quality-rules.md), and
[v2 bundle contract](docs/plans/sec-pit-bundle-findings-v2.md) define the
history, assertions, and closure requirements.

For retained traditional-XBRL full submissions, `produce_sec_financials_from_sgml`
builds the same typed projection from one offline raw observation. The source
must use this exact request shape and serialization; the call accepts no URL,
path, credential, or caller-supplied publication time:

The SGML producer uses filing acceptance as an availability proxy. SEC acceptance
does not prove first website publication. `MARKET_KNOWN` raises `ValueError` if
any supplied version uses `sec-sgml-financial-adapter-v1` or
`sec-sgml-financial-adapter-v2`, including versions
that would otherwise be excluded by policy, cutoff, or quality. This applies to
previously saved versions as well. `SYSTEM_REPLAY` retains its existing time,
quality, and consumer-commit requirements; it does not establish first publication.
Do not relabel these versions to bypass the check. Affected market backtests need
review and reruns with qualifying evidence before their results can be relied on.
Snapshots and identities remain unchanged. The
[availability decision](docs/plans/sec-availability-evidence-decision.md) describes
the migration and the separate known-by production capability described below.

```python
from ohmydata.core import RequestSpec
from ohmydata.providers.sec import SecSgmlFinancialsRequest, produce_sec_financials_from_sgml

raw_observation = source_store.observe(
    RequestSpec(
        "sec",
        "company-filing-sgml",
        {
            "cik": "0000320193",
            "accession_number": "0000320193-24-000006",
            "form": "10-Q",
        },
    ),
    retained_full_sgml_bytes,
    observed_at,
    "sec-filing-sgml-v1",
)
production = produce_sec_financials_from_sgml(
    source_store=source_store,
    source_observation=raw_observation,
    projection_store=projection_store,
    request=SecSgmlFinancialsRequest(
        "AAPL",
        "0000320193",
        "0000320193-24-000006",
        "10-Q",
        ("income_statement",),
        include_dimensions=False,
    ),
    produced_at=produced_at,
)
```

It supports full SEC SGML with embedded traditional XBRL schema, presentation,
label, and instance documents. For inline-only submissions, retain a separate
canonical `sec-xbrl-package-v1` observation containing SEC-extracted traditional
components and call `produce_sec_financials_from_xbrl_package`; that entry also
requires explicitly bound `SOURCE_DECLARED` timestamp evidence. The parser does
not fetch filings or parse inline XBRL.
`max_rows` bounds emitted normalized rows; it does not claim to be a hard limit
on memory used inside the third-party XBRL parser.

For a separately retained extracted package, the caller supplies the original
component bytes and evidence of when that exact package became public:

```python
from ohmydata.core import AvailabilityBasis, AvailabilityEvidence, AvailabilityPrecision
from ohmydata.providers.sec import (
    SecXbrlPackageAvailability,
    SecXbrlPackageComponents,
    produce_sec_financials_from_xbrl_package,
    serialize_sec_xbrl_package,
)

package_bytes = serialize_sec_xbrl_package(
    sgml_observation=raw_observation,
    cik=request.cik,
    accession_number=request.accession_number,
    form=request.form,
    source_available_at=package_public_at,
    components=SecXbrlPackageComponents(
        schema=retained_schema_bytes,
        presentation=retained_presentation_bytes,
        labels=retained_labels_bytes,
        instance=retained_instance_bytes,
    ),
)
package_observation = package_store.observe(
    RequestSpec(
        "sec",
        "company-filing-xbrl-package",
        {
            "cik": request.cik,
            "accession_number": request.accession_number,
            "form": request.form,
        },
    ),
    package_bytes,
    package_observed_at,
    "sec-xbrl-package-v1",
)
package_availability = SecXbrlPackageAvailability(
    package_observation,
    AvailabilityEvidence.from_observation(
        package_store,
        package_observation,
        source_available_at=package_public_at,
        availability_basis=AvailabilityBasis.SOURCE_DECLARED,
        availability_precision=AvailabilityPrecision.TIMESTAMP,
    ),
)
production = produce_sec_financials_from_xbrl_package(
    source_store=source_store,
    source_observation=raw_observation,
    package_store=package_store,
    package_observation=package_observation,
    package_availability=package_availability,
    projection_store=projection_store,
    request=request,
    produced_at=produced_at,
)
```

Here `request` is an explicit `SecSgmlFinancialsRequest` as above. The normalized
versions and vintage availability anchor use the later of header acceptance and
the declared package publication timestamp; `accepted_at` retains the original
header timestamp. Fetch time cannot substitute for that declaration.
Retain both source observations and the evidence for rebuilding: bundle replay
alone still verifies only the typed projection. Origin and filing correspondence
remain caller assertions checked against the retained binding and CIK; this is
reproducible parsing, not independent cross-validation. See the
[package contract](docs/plans/sec-xbrl-package-financial-production.md) for limits.

Both offline producers now default to their own parser v2, preserving complete
raw unit definitions before building projections. For exact historical source
reconstruction, explicitly pass the corresponding v1 `parser_version`:
`sec-sgml-financial-parser-v1-edgartools-5.56.0` or
`sec-xbrl-package-financial-parser-v1-edgartools-5.56.0`. Their v2 names replace
`parser-v1` with `parser-v2`; the returned production exposes the selected version.
V2 has new adapter/configuration identities even when simple-unit projection
bytes are unchanged. Old bundles retain their recorded versions, and old quality
or commit records do not qualify corrected versions. SGML v2 remains an
acceptance proxy. See the [offline unit repair contract](docs/plans/sec-legacy-unit-safety.md).

When first publication is unknown, use the separate observed-package path. It
records when the complete local inputs were known, without requiring a claimed
publication timestamp:

```python
from ohmydata.providers.sec import (
    produce_sec_financials_from_observed_xbrl_package,
    serialize_sec_observed_xbrl_package,
)

observed_package_bytes = serialize_sec_observed_xbrl_package(
    sgml_observation=raw_observation,
    cik=request.cik,
    accession_number=request.accession_number,
    form=request.form,
    components=SecXbrlPackageComponents(
        schema=retained_schema_bytes,
        presentation=retained_presentation_bytes,
        labels=retained_labels_bytes,
        instance=retained_instance_bytes,
    ),
)
observed_package = package_store.observe(
    RequestSpec(
        "sec",
        "company-filing-observed-xbrl-package",
        {
            "cik": request.cik,
            "accession_number": request.accession_number,
            "form": request.form,
        },
    ),
    observed_package_bytes,
    package_observed_at,
    "sec-observed-xbrl-package-v1",
)
observed_production = produce_sec_financials_from_observed_xbrl_package(
    source_store=source_store,
    source_observation=raw_observation,
    package_store=package_store,
    package_observation=observed_package,
    output_store=output_store,
    request=request,
    produced_at=produced_at,
)
known_by_at = observed_production.evidence.known_by_at
```

`package_observed_at` records local observation of the complete assembled
envelope. `known_by_at` is the later of that receipt and the selected SGML receipt;
both must be no later than production. The new vintage and output serialization
keep this time separate from acceptance. Retain all three observations and
rerun the producer with the same inputs to reproduce the result. These results
are not inputs to the legacy PIT selector or bundles. Only the producer creates
validated production objects; direct construction and substitutions of their
bound fields are rejected. Persisted observed-row bytes retain their existing
schema. See the [known-by contract](docs/plans/sec-known-by-production.md).
Callers that previously constructed production objects directly must now use
`produce_sec_financials_from_observed_xbrl_package` with their retained inputs.
There is no snapshot migration or promotion of old PIT evidence.

New observed productions default to
`parser_version="sec-observed-xbrl-financial-parser-v2-edgartools-5.56.0"`.
V2 resolves units directly from the retained instance: simple USD/shares/pure
units remain strings; compound units use compact JSON containing `type` and
either `measures` or `numerator`/`denominator` lists. Compound `currency` is
`None`; a USD numerator does not make USD/share a plain currency amount.
To reproduce historical v1 output explicitly pass
`parser_version="sec-observed-xbrl-financial-parser-v1-edgartools-5.56.0"`.
Observed bundle loading selects the version recorded in the retained output
and rebuilds that complete source chain. V1 retains its known compound-unit
defect for historical reconstruction. V2 has a distinct configuration and
production identity and requires its own quality assessment and consumer commit.
The SGML and declared-availability package v2 paths use the same correction;
their explicit v1 paths retain the known unit limitations. Live v2 uses the
separate raw-instance evidence contract described above.
See the
[versioned repair contract](docs/plans/sec-compound-unit-repair.md).

`evaluate_sec_observed_accounting` checks explicitly selected equalities within
one observed production. It returns immutable MATCH/MISMATCH/MISSING/INCOMPARABLE
diagnostics with row evidence, exact residuals and an explicit tolerance policy.
It does not create normalized findings, financial PASS or consumer commits.
Selectors, accounting completeness and cash-change definitions are caller
declarations; absent FX never becomes zero. Example for caller-verified selectors:

```python
from ohmydata.providers.sec import (
    SecAccountingApplicability,
    SecAccountingRule,
    SecAccountingTerm,
    SecAccountingTolerance,
    evaluate_sec_observed_accounting,
)

equation = SecAccountingRule(
    "assets-reported-total",
    SecAccountingApplicability.SAME_CONTEXT,
    (
        SecAccountingTerm("balance_sheet", "us-gaap_Assets", "c1", 1),
        SecAccountingTerm("balance_sheet", "us-gaap_LiabilitiesAndStockholdersEquity", "c1", -1),
    ),
    "iso4217:USD",
    SecAccountingTolerance.EXACT,
    "evidence:caller-verified-same-consolidated-scope",
)
accounting = evaluate_sec_observed_accounting(
    observed_production,
    [equation],
    detected_at=produced_at,
    recorded_at=produced_at,
)
```

Exact tolerance is zero. `ASSUME_NEAREST_REPORTED_DECIMALS` instead explicitly
assumes nearest rounding for every term and derives the summed half-unit bound;
missing or invalid native precision makes the comparison INCOMPARABLE. This is
not SEC-certified rounding. Rules also support explicit three-term cash
rollforwards. Limits and applicability are in the
[accounting contract](docs/plans/sec-observed-accounting.md).

The separate in-memory observed system selector requires caller-attested quality
and consumer-commit records. A production alone is insufficient. A later
quarantine or revocation blocks selection from that time onward; a later PASS
needs a commit referencing that exact quality record. Future records cannot
change an earlier cutoff. This records the caller's decisions, without checking
the truth of a financial assessment or performing a consumer publication.

```python
from ohmydata.providers.sec import (
    SecObservedFinancialConsumerCommit,
    SecObservedFinancialQualityRecord,
    SecObservedFinancialReplayPolicy,
    SecQualityStatus,
    select_sec_observed_financial_productions,
)

# These records represent a PASS assessment and a commit already made by the caller.
quality = SecObservedFinancialQualityRecord(
    observed_production.production_identity,
    "example-quality-v1",
    SecQualityStatus.PASS,
    quality_recorded_at,
)
commit = SecObservedFinancialConsumerCommit(
    observed_production.production_identity,
    quality.quality_record_id,
    consumer_dataset_identity,
    committed_at,
)
replay_policy = SecObservedFinancialReplayPolicy(
    output_schema_version="sec-financial-observed-rows-v1",
    parser_version="sec-observed-xbrl-financial-parser-v2-edgartools-5.56.0",
    configuration_version="sec-observed-xbrl-financial-config-v1",
    configuration_identity=expected_configuration_identity,
    quality_policy_version="example-quality-v1",
    consumer_dataset_identity=consumer_dataset_identity,
    knowledge_cutoff=knowledge_cutoff,
)
selected = select_sec_observed_financial_productions(
    [observed_production],
    [quality],
    [commit],
    replay_policy,
)
```

Selection uses explicit schema/parser/configuration, quality policy and consumer
dataset identities, and returns all eligible complete packages. It has no
MARKET_KNOWN mode and performs no network, snapshot reads or parsing. Retain and
reproduce source productions and supply lifecycle records yourself; durable
observed lifecycle persistence uses the separate bundle API below. See the
[system replay contract](docs/plans/sec-observed-system-replay.md).
Queries are bounded to 100 production inputs, 10,000 quality records, 10,000
commits and 100,000 aggregate rows; optional caller limits can only tighten
these bounds. Over-limit inputs fail explicitly, including iterators.
When several quality policies share a consumer dataset, selection ignores
commits bound to an included quality record for another policy of the same
production. Missing references and cross-production bindings still fail;
commits for the selected policy must satisfy its original time and PASS gates.

Persist observed productions and their complete lifecycle in a separate immutable
bundle. The caller resolves observation identities to retained stores; bundle
JSON contains no storage paths. Save and load both rebuild the original
source/package parsing chain and compare its exact bytes with the retained
output before accepting production objects. Loading performs no writes.

```python
from ohmydata.providers.sec import (
    load_sec_observed_financial_bundle,
    write_sec_observed_financial_bundle,
)

observations = {
    raw_observation.observation_identity: (source_store, raw_observation),
    observed_package.observation_identity: (package_store, observed_package),
    observed_production.output_observation.observation_identity: (
        output_store,
        observed_production.output_observation,
    ),
}
bundle_ref = write_sec_observed_financial_bundle(
    store=bundle_store,
    batch_identity="example-observed-batch-v1",
    productions=[observed_production],
    quality_records=[quality],
    consumer_commits=[commit],
    captured_at=captured_at,
    resolve_observation=observations.__getitem__,
)
restored = load_sec_observed_financial_bundle(
    store=bundle_store,
    bundle_ref=bundle_ref,
    resolve_observation=observations.__getitem__,
)
replayed = select_sec_observed_financial_productions(
    restored.productions,
    restored.quality_records,
    restored.consumer_commits,
    replay_policy,
)
```

`captured_at` must be no earlier than every retained observation, production,
quality record and commit in the bundle. It records persistence, not historical
eligibility. Reusing a batch identity with changed content or capture time fails;
exact repeats are idempotent. Save/load validate all included quality policies
and consumer datasets, including records beyond a later query's cutoff.
Keep every referenced source/package/output observation and the pinned parser
available for restoration. Defaults allow 10 productions, 10,000 quality records,
10,000 commits, 100,000 rows, an 8 MiB bundle and 32 MiB of unique dependency
payloads, with 8 MiB per dependency. Caller limits may only be stricter.
See the [observed bundle contract](docs/plans/sec-observed-lifecycle-bundle.md).

`SnapshotStore.replay` and `replay_observation` also accept optional
`max_payload_bytes` (a non-negative integer). Their default `None` preserves
unlimited payload reads; exceeding an explicit limit raises
`SnapshotIntegrityError`. This limit covers response bytes, not JSON manifest
metadata. Retain source snapshots and observation receipts alongside bundles.

```python
from datetime import UTC, datetime

from ohmydata.core import (
    AvailabilityBasis,
    AvailabilityEvidence,
    AvailabilityPrecision,
    RequestSpec,
)
from ohmydata.providers.sec import (
    SecNormalizedFinancialFactVersion,
    evaluate_sec_structural_quality,
    serialize_sec_typed_rows_projection,
    write_sec_pit_bundle,
)

# ``vintage`` and the source artifact digest are caller-supplied and retained with its source evidence.
source_at = datetime(2024, 5, 1, 21, tzinfo=UTC)
payload = serialize_sec_typed_rows_projection(
    vintage, source_artifact_identity=artifact_sha256, source_available_at=source_at
)
observation = store.observe(
    RequestSpec("sec", "financial-typed-rows", {"accession": vintage.accession_number}, ()),
    payload,
    datetime.now(UTC),
    "sec-financial-typed-rows-projection-v1",
)
evidence = AvailabilityEvidence.from_observation(
    store,
    observation,
    source_available_at=source_at,
    availability_basis=AvailabilityBasis.SOURCE_DECLARED,
    availability_precision=AvailabilityPrecision.TIMESTAMP,
)
version = SecNormalizedFinancialFactVersion.from_projection(
    store=store,
    observation=observation,
    availability=evidence,
    vintage=vintage,
    row_ordinal=0,
    schema_version="sec-financial-normalized-v1",
    adapter_version="adapter-v1",
    normalization_version="normalization-v1",
    configuration_identity=config_sha256,
    recorded_at=datetime.now(UTC),
)
report = evaluate_sec_structural_quality(
    [version], detected_at=datetime.now(UTC), recorded_at=datetime.now(UTC)
)
# ``bundle_store`` is caller-provided; source evidence remains in ``store``.
bundle_ref = write_sec_pit_bundle(
    store=bundle_store,
    batch_identity="structural-1",
    versions=[version],
    quality_records=[],
    quality_findings=report.findings,
    source_store=store,
    resolve_observation=lambda identity: observation,
    captured_at=datetime.now(UTC),
)
```

The SEC extra supports `edgartools==5.56.0`. See the
[financial period contract and v2 migration](docs/financial-period-integrity.md)
before rebuilding existing financial datasets.

### yfinance (US & Global Market Data, Fundamentals, and Zero-Drift Audit)

Install `ohmydata[yfinance]` to access normalized market data, valuation ratios,
and financial statements with strict version pinning (`yfinance==1.7.0`):

```python
from ohmydata.providers.yfinance import (
    YFinanceAdjustmentMode,
    YFinanceBatchPolicy,
    YFinanceClient,
    YFinanceDailyBarsRequest,
    YFinanceFundamentalsRequest,
    YFinanceRepairPolicy,
)

client = YFinanceClient()

# 1. Fetch normalized daily bars (OHLCV) with repair isolation
bars_req = YFinanceDailyBarsRequest(
    symbols=("SPY", "QQQ", "^VIX"),
    start_date="2024-01-01",
    end_date_exclusive="2024-02-01",
    adjustment_mode=YFinanceAdjustmentMode.RAW_WITH_ADJ_CLOSE,
    batch_policy=YFinanceBatchPolicy.STRICT,
    repair_policy=YFinanceRepairPolicy.PER_SYMBOL,
)
bars_result = client.fetch_daily_bars(bars_req)
df = bars_result.dataframe

# 2. Fetch fundamentals with FY1 Forward P/E and source metadata
fund_req = YFinanceFundamentalsRequest(
    symbols=("NVDA", "GEV"),
    include_financials=True,
    include_valuation=True,
    include_estimates=True,
)
fund_result = client.fetch_fundamentals(fund_req)

nvda = fund_result.records["NVDA"]
# Forward P/E is calibrated to current year consensus (FY1 0y.avg) rather than out-year (+1y)
print("NVDA Calibrated FPE:", nvda.valuation.forward_pe, nvda.valuation.forward_pe_source)
print("NVDA Raw Yahoo FPE:", nvda.valuation.raw_forward_pe)

gev = fund_result.records["GEV"]
# Legacy flag measures EPS-source divergence; accounting basis remains unknown.
if gev.estimates.has_gaap_distortion:
    print(f"GEV EPS-source gap: {gev.estimates.gaap_diff_pct * 100:.1f}%")
```

Financial values bind to actual statement columns, with per-metric dates and
coverage. FY1 calibration requires an actual quote and compatible currencies;
otherwise raw values remain available. See the
[period selection, valuation provenance and migration guide](docs/financial-period-integrity.md).

#### Zero-Drift Audit CLI (`omd audit-drift`)

Audit 10+ years of historical data against the 13-ETF `r10a0` benchmark universe before
any provider upgrade:

```bash
# Strict unadjusted market bar zero-drift gate (must be bit-exact 0.0 error)
uv run omd audit-drift --universe r10a0 --baseline-dir <old_version_dir> --target-dir <new_version_dir> --raw-only
```

## Dataframe Adapters (Polars & Pandas)

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

## Local Checks and Verification

```bash
uv lock
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv build
git diff --check
```

Behavioral evidence for the initial consumers is in
[`docs/behavioral-inventory.md`](docs/behavioral-inventory.md). The adjusted
ETF characterization is test-only and uses synthetic JSON fixtures.
The public-contract changes and consumer-owned migration boundaries are summarized in
[`docs/v0.1.0-migration.md`](docs/v0.1.0-migration.md),
[`docs/v0.1.1-migration.md`](docs/v0.1.1-migration.md),
[`docs/v0.1.2-lookthrough-migration.md`](docs/v0.1.2-lookthrough-migration.md), and
[`docs/v0.1.3-vintage-plane-migration.md`](docs/v0.1.3-vintage-plane-migration.md).

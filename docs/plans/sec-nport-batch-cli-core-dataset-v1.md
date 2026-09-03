# SEC N-PORT Batch CLI and Core Dataset V1

Status: **FROZEN FOR LUNA IMPLEMENTATION**

This contract extends the accepted
[`sec-nport-fund-holdings-pit-v1.md`](sec-nport-fund-holdings-pit-v1.md)
provider slice with a resumable command-line workflow and a rebuildable research
input dataset. It does not define strategy signals or consumer trading-session
semantics.

## 1. Outcome

Provide an offline-testable `omd sec nport` CLI that can:

1. plan an inclusive quarterly range from `2019q4` through a caller-bounded
   current quarter;
2. download each official N-PORT ZIP once, validate it, publish it through the
   immutable SEC artifact store, and skip an already replay-valid quarter;
3. load a reviewed equity-ETF identity universe using exact SEC identities;
4. extract all selected fund vintages and enrich required accessions from
   replayed EDGAR submissions JSON;
5. atomically publish one derived core dataset partition per source quarter;
6. validate or inspect local artifacts and derived partitions without network
   access; and
7. resume at quarter boundaries after interruption without rebuilding completed
   partitions.

The initial reviewed universe may include SPY, QQQ, IWM, XLK, SMH, XLF, XLE,
XLV, and USMV. GLD, bond ETFs, money-market ETFs, and currency ETFs are outside
the selected-fund universe. Holdings inside a selected equity ETF remain
provider-native: cash, short-term vehicles, derivatives, debt-like rows, and
negative values are not filtered out.

## 2. Ownership boundary

OMD owns:

- SEC transport, rate/retry/proxy rules and immutable raw artifacts;
- exact CIK/series selection and single-series-CIK selection;
- filing/report/acceptance/observation roles and revision identities;
- provider-native values, normalized typed source fields, provenance and QA;
- quarter-level batch orchestration and rebuildable Parquet partitions; and
- deterministic validation, manifests and content identities.

Consumers own:

- the location passed as `--root` and its retention policy;
- mapping CUSIP/ISIN/native ticker to a price/security master;
- `first_usable_session`, exchange calendar, cutoff and timezone policy;
- equity-only row filters, constituent breadth, overlap, concentration,
  exposures, factors, signals, backtests and publication; and
- converting exact decimals to float for a named research calculation.

The core dataset is a provider-semantic research input, not a consumer's final
feature store.

## 3. CLI and packaging

Add a standard-library `argparse` entry point:

```toml
[project.scripts]
omd = "ohmydata.cli:main"
```

Commands:

```text
omd sec nport plan     --quarters START:END --universe FILE --root DIR
omd sec nport fetch    --quarters START:END --root DIR --universe FILE
                         --user-agent-file FILE [--refresh]
omd sec nport build    --quarters START:END --root DIR --universe FILE
                         --availability-policy POLICY [--lag-days N]
omd sec nport sync     --quarters START:END --root DIR --universe FILE
                         --user-agent-file FILE --availability-policy POLICY
                         [--lag-days N] [--refresh]
omd sec nport validate --quarters START:END --root DIR
omd sec nport inspect  --quarter YYYYqN --root DIR [--symbol SYMBOL]
```

Quarter tokens are lowercase-normalized ASCII `YYYYq[1-4]`. Ranges are
inclusive, non-empty, sorted, and bounded to `2019q4` through the current
quarter from an injectable aware UTC clock. `plan`, `build`, `validate`, and
`inspect` are always offline. `fetch` and the fetch phase of `sync` are the only
network-capable commands.

The CLI never accepts a contact User-Agent inline on the command line. It reads
one UTF-8 visible-ASCII line from `--user-agent-file FILE` or, when explicitly
requested, `--user-agent-stdin`; it removes exactly one line terminator and
passes the value to `SecHttpClient`. The value never appears in output, errors,
repr, manifests, process arguments, request identities or logs. It never reads
environment variables, `.env`, keychains or consumer configuration. Default
HTTP is the provider's explicit no-proxy transport.

`sync` requires the same contact input as `fetch`. Exit code `0` means every
requested quarter completed or was replay-valid and skipped; validation,
coverage, schema, permission and partial failures are non-zero. Machine-readable
`--json` output contains only safe phase/quarter/status/hash/count/timing data.
`fetch` requires the universe because it also resolves and caches the EDGAR
submissions payloads needed for those selected funds; otherwise a later offline
`build` could not guarantee exact accession coverage.

`build` and `sync` require exactly `observation-only` or
`accepted-at-plus-lag`. The inferred policy requires an integer `--lag-days`
within the existing frozen bound; the observation policy rejects a lag option.

The optional extra `sec-cli` contains `pyarrow>=23.0.1,<24.0`; imports remain
lazy, and commands not writing Parquet continue to work without it. A missing
optional dependency produces a stable actionable error, not an import traceback.

## 4. Reviewed universe

The universe is a UTF-8 JSON document:

```json
{
  "schema_version": "sec-equity-etf-universe-v1",
  "funds": [
    {
      "symbol": "XLK",
      "cik": "0001064641",
      "selection_mode": "series",
      "series_id": "S000006415",
      "valid_from_quarter": "2019q4",
      "valid_to_quarter": null
    },
    {
      "symbol": "SPY",
      "cik": "0000884394",
      "selection_mode": "single_series_cik",
      "series_id": null
    }
  ]
}
```

`symbol` is caller metadata, uppercase ASCII and unique. It is not a provider
security identity. `cik` is exactly ten ASCII digits. `series` mode requires an
exact non-empty SEC series ID. `single_series_cik` requires `series_id: null`
and succeeds only when the selected accession has exactly one fund parent for
that CIK and that parent has an empty series ID; ambiguity or a non-empty
conflicting series identity fails. Names are never used for selection. Entries
may additionally contain `valid_from_quarter` and `valid_to_quarter`, each a
nullable normalized quarter token. Omission is equivalent to null. Bounds are
inclusive; at least one bound may be null, and when both exist the start must
not exceed the end. Entries are sorted by
`(symbol, cik, series_id-or-empty, valid_from_quarter-or-empty,
valid_to_quarter-or-empty)` and the canonical JSON SHA-256 is
`universe_hash`. Both `symbol` and the exact source selector key
`(cik, selection_mode, series_id-or-null)` must be unique, so two symbols cannot
duplicate the same source rows.

The internal immutable `SecFundSelector` contains exactly the four source
identity fields `symbol`, `cik`, `selection_mode`, and `series_id`. An immutable
`SecScheduledFundSelector` wraps that selector with the two nullable validity
bounds; scheduling fields are part of universe canonicalization and partition
identity but never part of SEC row identity.
For every quarter, selector resolution scans registrants once to retain only
accessions whose CIK occurs in the universe, then scans fund parents once:

1. `series` retains exactly the `(CIK, SERIES_ID)` parent for each accession;
2. `single_series_cik` retains an accession only when that CIK has exactly one
   fund parent and its native series ID is empty;
3. zero, duplicate, conflicting or multiple matching fund parents fail for a
   required selector; and
4. the submission scan then retains every corresponding `NPORT-P` and
   `NPORT-P/A` accession, including amendments—never just the newest filing.

Every universe entry is required in every requested quarter unless it carries
an explicit inclusive `valid_from_quarter`/`valid_to_quarter`. Absence inside
that interval is `CoverageError`; outside it the selector is not requested.

The CLI does not ship a mutable hard-coded trading universe. Documentation may
show examples, but the caller supplies and reviews the actual file.

## 5. Storage layout and resumability

```text
<root>/
  raw/sec/nport/<year>q<quarter>/<artifact_sha256>/
    source.zip
    manifest.json
  raw/sec/edgar/submissions/<cik>/<payload_sha256>/
    source.json
    manifest.json
  state/sec-nport-quarter-index-v1.json
  state/sec-edgar-receipt-index-v1.json
  state/fetch-receipts/<source_quarter>/<receipt_sha256>.json
  state/sec-state.lock
  core/sec-fund-holdings-pit-v1/
    source_quarter=<year>q<quarter>/
      artifact=<artifact_sha256>/
        partition=<partition_identity>/
          fund_vintages.parquet
          holdings.parquet
          identifiers.parquet
          quality.json
          manifest.json
    catalog.json
```

Raw artifacts are immutable. Derived partitions are rebuildable and immutable;
publication uses a sibling temporary directory, fsync, atomic rename, and final
directory fsync. A partition identity is the SHA-256 of canonical JSON binding
source artifact and manifest hashes, parser/schema versions, universe hash,
selected identities, EDGAR closure hash, availability policy, lag,
writer-profile version and exact PyArrow version. Output hashes are recorded in
the partition manifest but are not inputs to this pre-publication identity. The
catalog path must equal the canonical path containing that exact
`partition_identity`.

The quarter index schema is canonical JSON
`{schema_version, revision, entries[]}`. Each entry is keyed by
`(year, quarter, artifact_sha256, manifest_sha256)` and contains source URL and
retrieved-at only as validated metadata. Entries are lexically sorted by that
key. An entry is skipped only after full replay validation. Missing, malformed,
duplicate or conflicting entries fail closed. A different response for an
indexed quarter is retained under its content hash and requires `--refresh`; it
never overwrites the prior artifact or partition.

Every index/catalog update holds an exclusive cross-platform advisory lock on
`state/sec-state.lock`, rereads the current document after locking, verifies its
revision and canonical hash, applies one append-only change, fsyncs a sibling
temporary file, atomically replaces the document, and fsyncs `state/`. Thus two
CLI processes serialize rather than lose updates. Lock acquisition is bounded
by the operation deadline; a lock or revision conflict fails without repair.

The derived `catalog.json` has the same envelope. Its entry key is
`(source_quarter, artifact_sha256, artifact_manifest_sha256, universe_hash,
edgar_closure_hash, parser_version, dataset_schema_version,
availability_policy, lag_days-or-null, writer_profile_version,
pyarrow_version)`. It records `partition_identity`, its canonical partition
path, manifest hash and output hashes. Same key/same hashes is idempotent; same
key/different hashes is a collision. Multiple entries for the same quarter are
valid when any key dimension differs. Validation rejects missing referenced
data, a path/identity mismatch, unindexed partitions, and index entries whose
raw or derived artifacts do not replay.

V1 resumes at quarter boundaries. A completed replay-valid quarter is never
downloaded again. An interrupted current response is cleaned and that one
quarter restarts; byte-range partial-response resume is explicitly deferred.
Each quarter publishes independently, so a later-quarter failure preserves all
earlier completed work.

No path may resolve inside the repository unless it is already covered by an
ignore rule; the CLI checks this and refuses a tracked or potentially tracked
output root. Raw responses, contact data and consumer private configuration are
never package data or Git fixtures.

## 6. EDGAR metadata cache

For each selected CIK, fetch the canonical current submissions JSON and only
the validated linked historical files needed for the quarter's required
accessions. Persist exact bytes and a canonical immutable manifest containing
`schema_version, request_kind, cik, historical_basename-or-null, canonical_url,
payload_sha256, byte_count, content_type, retrieved_at, parser_version` plus
its external manifest hash.

`state/sec-edgar-receipt-index-v1.json` uses the locked envelope from section 5.
Its canonical request key is `(cik, request_kind, historical_basename-or-null)`;
each key has an append-only, retrieved-at-sorted tuple of
`(payload_sha256, manifest_sha256)`. Mutable current submissions responses and
changed historical responses are retained as additional observations. Default
cache lookup tests observations newest-to-oldest and may reuse one only when
that current payload plus its indexed, link-valid historical closure produces
exact coverage of the quarter's required accessions. Otherwise `fetch` obtains
a new current response; `--refresh` always obtains one. No observation is
silently promoted or deleted.

After fetch, write an immutable quarter fetch receipt keyed by
`(source_quarter, nport artifact/manifest hashes, universe_hash)`. It lists for
each CIK the exact current and historical EDGAR request keys, payload hashes and
manifest hashes that formed complete required-accession coverage. Its canonical
SHA-256 is `edgar_closure_hash`. Offline `build` accepts only exact receipts and
replays every listed payload; it never chooses an arbitrary newest cache entry.
A refreshed closure creates a new receipt and therefore a distinct derived
partition identity.

For each requested quarter, `build` enumerates every replay-valid fetch receipt
whose source quarter and universe hash match, sorts them by
`(nport_artifact_sha256, nport_manifest_sha256, receipt_sha256)`, and builds or
replay-validates every resulting partition. Zero matches fails with the exact
missing quarter/universe identity. Multiple matches are expected and are never
collapsed or resolved by newest time. `sync` first publishes or reuses one
complete receipt for its fetch attempt and then applies the same enumeration,
so previously retained receipts remain buildable.

Use the existing historical basename, CIK, file-count, observed-byte,
deduplication, cycle, exact-coverage and timestamp rules. A required accession
without acceptance metadata is retained with the frozen unknown-availability
semantics; it is never silently assigned filing date or report date as a
timestamp.

## 7. Core Parquet datasets

Parquet is written with PyArrow using fixed column order and explicit schemas.
No Pandas dependency is required. Decimal source fields use
`decimal128(38, 18)` only after an exact representability check; precision/scale
overflow fails rather than rounding. The corresponding `*_native` UTF-8 column
is always retained.

Writer profile `sec-core-parquet-v1` is fixed to PyArrow, Parquet version `2.6`,
data page version `2.0`, Zstandard compression level 9, no dictionary encoding,
statistics enabled, page index disabled, `timestamp[us, tz=UTC]`, no INT96,
no timestamp truncation, and row groups of at most 65,536 rows. Tables contain
only fixed OMD schema metadata keys sorted lexically; wall-clock creation fields
are forbidden. The exact `pyarrow.__version__` is a partition identity field.
With the same locked PyArrow version and inputs, reruns must be byte-identical;
across PyArrow versions the canonical logical row hash must remain equal while
the partition identity and file bytes may differ.

Every table is sorted lexically by its full primary key, with null ordered
first. `quality_flags` is a non-null `list<string>` whose values are unique and
sorted. Before publication, validation rereads each file, compares the exact
Arrow schema including nullability and metadata, verifies strict primary-key
ordering/uniqueness, and recomputes both logical and file hashes.

This core dataset is an intentionally compact, versioned projection rather
than a claim to contain every SEC column. It retains the complete holdings and
identifier fields needed for research plus fiscal/series context. Registrant
addresses/phone, cash-flow/credit-spread fields, source lexical date/timestamp
forms and unrelated N-PORT tables are omitted. Every row retains artifact,
payload and vintage identities that lead back to the immutable raw ZIP; numeric
lexemes are additionally retained because precision loss is material.

### `fund_vintages.parquet`

One row per exact filing vintage:

```text
provider, fund_symbol, cik, registrant_name, registrant_file_number,
registrant_lei, series_id, series_name, series_lei, accession_number,
submission_type, report_ending_period, report_date, filing_date, accepted_at, observed_at,
availability_anchor, availability_basis, availability_precision,
availability_policy, availability_lag_days, total_assets,
total_assets_native, total_liabilities, total_liabilities_native, net_assets,
net_assets_native, artifact_sha256, artifact_manifest_sha256,
payload_hash, vintage_identity, universe_hash, quality_flags
```

Primary key:
`(provider, cik, series_id, report_date, accession_number, artifact_sha256)`.
For explicit `single_series_cik`, `series_id` is null and remains part of the
declared nullable identity; `fund_symbol` does not replace it.

Exact Arrow types/nullability: all identity/hash/version/policy fields are
non-null `string` except nullable `series_id`; provider is the constant `sec`.
Names, LEIs and file number are nullable `string`. `report_ending_period` and
`report_date` are non-null `date32`; `filing_date` is nullable `date32`.
Acceptance and observation/availability fields are nullable or non-null
`timestamp[us, tz=UTC]` according to source evidence (`observed_at` is
non-null). Lag is nullable `int16`. Asset typed columns are nullable
`decimal128(38,18)` and native companions nullable `string`. Hashes and
`universe_hash` are non-null `string`; flags are non-null `list<string>`.

### `holdings.parquet`

One row per holding in one vintage:

```text
fund_symbol, cik, series_id, accession_number, report_date, holding_id,
issuer_name, issuer_lei, issuer_title, issuer_cusip, balance, balance_native,
unit, other_unit_desc, currency_code, currency_value, currency_value_native,
exchange_rate, exchange_rate_native, percentage, percentage_native,
payoff_profile, asset_cat, other_asset, issuer_type, other_issuer,
investment_country, is_restricted_security, fair_value_level, derivative_cat,
artifact_sha256, payload_hash, vintage_identity
```

Primary key:
`(cik, accession_number, holding_id, artifact_sha256)`.
`percentage` is SEC percentage points, not a decimal ratio.

Exact Arrow types/nullability: primary-key and artifact/payload/vintage fields
are non-null `string`; `series_id` and provider text/classification fields are
nullable `string` except non-null `fund_symbol`, `cik`, `accession_number`,
`holding_id`, and `report_date: date32`. Typed numeric columns are nullable
`decimal128(38,18)` and native companions nullable `string`.

### `identifiers.parquet`

One row per native identifier child:

```text
fund_symbol, cik, series_id, accession_number, report_date, holding_id,
identifiers_id, identifier_isin, identifier_ticker, other_identifier,
other_identifier_desc, artifact_sha256, vintage_identity
```

Primary key:
`(cik, accession_number, holding_id, identifiers_id, artifact_sha256)`.
No ticker or ISIN is promoted to canonical identity. Multiple child rows remain
multiple rows.

Exact Arrow types/nullability: primary-key, fund symbol, CIK, accession,
holding/identifier IDs, artifact and vintage identities are non-null `string`;
`report_date` is non-null `date32`; `series_id` and all actual identifier value
fields are nullable `string`.

All timestamps are aware UTC Arrow timestamps; dates are Arrow dates; flags and
provider classifications remain strings; native nulls remain null. No missing
numeric becomes zero.

## 8. Batch behavior

For each quarter, in ascending order:

1. replay the indexed N-PORT artifact or fetch and atomically publish it;
2. create a fresh single-use replay session;
3. select the reviewed universe in one pass per required member;
4. cache and replay EDGAR metadata needed for selected accessions;
5. produce vintages, holdings, identifiers, QA and provenance;
6. write all Parquet/JSON files into a temporary partition;
7. validate schemas, primary keys, ordering, row counts, hashes and exact
   round-trip values;
8. atomically publish the partition and update the catalog; and
9. continue to the next quarter.

No full history or full SEC member is materialized in memory. One quarter is
the maximum concurrent work unit and default concurrency is one. Selected rows
may be buffered for sorting, bounded by configurable selected-row and output
byte limits; exceeding either fails before publication.

`build` never calls the network. If a required N-PORT or EDGAR artifact is
absent, it fails with the exact missing identity. `sync` may fetch missing
artifacts but does not refresh valid existing ones unless explicitly requested.

## 9. Inspect and validation

`inspect` returns safe per-quarter/per-symbol facts: source/report/filing/
acceptance dates, accession, row counts, percentage sum, identifier coverage,
reconciliation deltas, quality flags and hashes. It never prints raw holdings
unless `--rows` is explicitly requested, and never prints the User-Agent.

`validate` is offline and verifies every indexed artifact and requested derived
partition: path safety, manifests and hashes, ZIP/member integrity, Parquet
schema/order/keys, source identities, catalog consistency, QA row counts and
deterministic output hashes. It performs no repair or deletion.

## 10. Errors and output policy

Required quarter, fund, accession, parent, child, EDGAR or partition coverage
fails explicitly. An empty selected portfolio follows the existing explicit
empty policy. Schema drift, decimal overflow, invalid UTF-8/JSON, duplicate
keys, mismatched hashes and unsafe paths never degrade to warnings.

CLI stdout is reserved for requested human or JSON results; safe progress and
errors go to stderr. No exception chain, progress callback or JSON result may
contain the contact string, response body, raw headers or proxy URL.

## 11. Tests and acceptance

Offline synthetic tests must cover:

1. CLI parsing, exit codes, JSON output, redaction and no optional-dependency
   traceback;
2. quarter parsing/ranges, `2019q4` lower bound, future/empty/reversed ranges;
3. universe canonicalization, duplicate/conflicting identities, series mode,
   SPY-style exact single-series-CIK mode and ambiguous CIK failure;
4. fake HTTP success/retry/permanent failure, direct no-proxy default, quarter
   cache hit with zero HTTP calls, refresh collision and interruption cleanup;
5. quarter-index atomicity, replay tampering and immutable raw reuse;
6. exact EDGAR cache/history coverage and offline-only build behavior;
7. Parquet schema, Decimal/null/date/timestamp/native-string round trip,
   deterministic order/hash, primary-key collision and overflow failure;
8. NPORT-P/NPORT-P/A vintages, amendments, selected rows including cash and
   derivatives, missing required coverage and QA propagation;
9. atomic partition interruption, idempotent rerun, same-input/same-output and
   same-key/different-content collision;
10. inspect/validate behavior and absence of strategy/session/ticker inference;
11. wheel/sdist includes CLI modules but excludes raw/core artifacts, universe
    inputs, contact files and local state; and
12. full repository pytest, Ruff check/format, Pyright, build and
    `git diff --check` gates.

At least one offline end-to-end fixture must execute `sync` with injected fake
transport across two quarters, interrupt after quarter one, rerun, prove no
second fetch for quarter one, and reproduce byte-identical core partitions.

## 12. Authorized implementation scope

Authorized files:

- `pyproject.toml`, `uv.lock`;
- `src/ohmydata/cli.py`;
- `src/ohmydata/providers/sec/cli.py`;
- `src/ohmydata/providers/sec/batch.py`;
- `src/ohmydata/providers/sec/core_dataset.py`;
- focused extensions under existing `src/ohmydata/providers/sec/` modules for
  exact selectors, persistent artifact receipts and batch orchestration;
- `tests/providers/sec/test_cli.py`, `test_batch.py`,
  `test_core_dataset.py`, and focused updates to existing SEC tests;
- `README.md`, `CHANGELOG.md`, and this contract.

No consumer repository edit is authorized in this slice.

## 13. Non-goals and stop conditions

Non-goals:

- running a full historical live download;
- byte-range resume within one ZIP;
- legacy N-Q/N-CSR parsing, GLD, bond, money-market or currency ETF support;
- provider-owned ETF classification, security-master resolution or ticker
  repair;
- trading-session availability, research features, strategy changes,
  consumer publication, migration, commit, push or release.

Stop and refreeze if exact Parquet Decimal representation is not feasible,
single-series-CIK selection is ambiguous in official data, a generic core
change is required, new runtime dependencies beyond the `sec-cli` extra are
needed, or safe persistent replay cannot be achieved without changing the
accepted artifact integrity model.

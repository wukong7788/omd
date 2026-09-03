# SEC N-PORT Fund Holdings PIT V1

Status: **REFROZEN FOR P0 IMPLEMENTATION AFTER THE AUTHORIZED 2026Q2 PROBE**

This document is the durable implementation contract for the first SEC data
provider slice in OMD. It is narrower than the proposed `US_FUND_HOLDINGS_PIT_V1`
domain: P0 ingests structured public Form N-PORT data and preserves filing-time
evidence without constructing a consumer trading-session view.

The repository-level [`PLAN.md`](../../PLAN.md) remains the canonical roadmap.
This contract resolves its earlier "Tushare only" scope for this new work by
adding SEC as a separately planned provider after the accepted Tushare-first
release. No Tushare API or behavior is generalized to SEC.

## 1. Objective and visible outcome

Add an offline-testable `ohmydata.providers.sec` capability that can:

1. identify and download one official quarterly N-PORT ZIP without loading it
   wholly into memory;
2. validate and immutably retain the exact downloaded artifact and provenance;
3. stream the SEC tab-separated tables and extract caller-selected equity ETF
   series;
4. preserve submission, registrant, fund, holding, and identifier fields in
   provider-native units and missingness;
5. enrich filings with EDGAR acceptance timestamps when independently supplied
   by the official submissions history;
6. retain original filings and amendments as separate append-only vintages; and
7. expose a resolver by knowledge timestamp, never by report date alone.

The intended first consumer is `funmoney_backtest`, which will later select its
own US ETF universe, trading calendar, cutoff, `first_usable_session`, storage,
and research features. The existing Stock Notify holdings use case is evidence
for a reusable fund-holdings domain, but its Tushare report-selection behavior is
not transplanted into SEC.

## 2. Authoritative source facts

P0 is based only on official SEC sources:

- the quarterly Form N-PORT Data Sets page and its ZIP links;
- the SEC N-PORT data-set readme and W3C table metadata shipped in each ZIP;
- EDGAR submissions history or the complete submission text header for filing
  metadata absent from the bulk N-PORT tables; and
- individual filing XML only for later audit/reconciliation, not the P0 ingest
  path.

The bulk data set is quarterly, contains only publicly disseminated N-PORT
filings, and may omit filing metadata present in EDGAR. The SEC readme defines
`ACCESSION_NUMBER` as the submission key and
`(ACCESSION_NUMBER, HOLDING_ID)` as the holding key. `PERCENTAGE` is the
reported percentage of fund net assets; it is not recomputed or renormalized.

The bulk `SUBMISSION` table provides `FILING_DATE`, not an acceptance timestamp.
EDGAR's `acceptanceDateTime`/`ACCEPTANCE-DATETIME` is therefore a distinct
enrichment field. SEC states that documents are often available after a short
lag but provides no exact public-availability timestamp. Consequently:

- `accepted_at` is preserved as a source event timestamp;
- it is not labeled `source_available_at` with `SOURCE_DECLARED` basis;
- an optional conservative availability anchor derived from it uses
  `INFERRED_SCHEDULE`, remains distinguishable from proof, and never creates an
  OMD `first_usable_session`; and
- the consumer must explicitly choose whether and how to map the evidence to a
  canonical trading session.

## 3. Authorized P0 scope

### Production modules

- `src/ohmydata/providers/sec/__init__.py`
- `src/ohmydata/providers/sec/endpoints.py`
- `src/ohmydata/providers/sec/errors.py`
- `src/ohmydata/providers/sec/http.py`
- `src/ohmydata/providers/sec/artifacts.py`
- `src/ohmydata/providers/sec/nport.py`
- `src/ohmydata/providers/sec/edgar.py`
- `src/ohmydata/providers/__init__.py` only if needed for namespace exposure

### Tests and public documentation

- `tests/providers/sec/`
- `README.md`
- `CHANGELOG.md`
- `PLAN.md`
- `docs/plans/sec-nport-fund-holdings-pit-v1.md`

No dependency change is authorized for P0. Standard-library HTTP, ZIP, CSV,
decimal, XML/HTML primitives and injected test doubles are sufficient. If that
proves false, stop and refreeze this contract before editing `pyproject.toml` or
`uv.lock`.

## 4. Public interfaces

Names may be adjusted only for an objective repository naming conflict; data
and error semantics may not be changed without refreezing the contract.

### 4.1 Requests

`SecNportQuarterRequest` is frozen with:

- `year: int` and `quarter: int` (`1..4`), validated against the documented
  N-PORT data-set era (`2019Q4` through the current quarter supplied by an
  injectable UTC clock; an unpublished quarter may still return 404);
- `series_ids: tuple[str, ...]`, non-empty, unique, sorted canonically, and
  caller-owned; P0 callers supply only equity ETF series;
- `required_series_ids: tuple[str, ...]`, a subset of `series_ids` whose absence
  fails with `CoverageError`;
- `empty_policy: SecEmptyPolicy`, exactly `REQUIRE_ROWS` or `ALLOW_EMPTY`; and
- a `RequestSpec(provider="sec", endpoint="nport_quarter", ...)` that contains
  no contact header, path, credential, transient URL query, or retrieval time.

`SecEdgarSubmissionsRequest` is frozen with one zero-padded ten-digit CIK and an
explicit set of required accession numbers. It may follow official historical
submissions-file references but must not silently omit required accessions.

`SecNportQuarterResult` contains the immutable artifact reference, deterministic
tuple of `SecFundHoldingVintage` values, a safe `FetchProvenance`, phase/member
scan counts, QA facts, and SEC-owned `SecTransportEvidence(proxy_in_use: bool)`.
The existing core `FetchProvenance` schema remains unchanged. HTTP attempt
records expose only attempt number, classified exception type, and retry delay;
they contain no URL, headers, response body, or contact string.

### 4.2 HTTP boundary

`SecHttpClient` requires a caller-supplied declared User-Agent string containing
contact information. It does not read environment variables, `.env`, keychains,
or consumer configuration. An opener, clock, sleep, retry policy, and rate
limiter are injectable.

The User-Agent is 1–256 visible ASCII characters, contains no CR/LF/control
character, and is held only in the private request-construction boundary. It is
redacted from every exception, exception cause/message, attempt record,
provenance object, manifest, log/observability/progress hook, repr, and test
failure constructed by OMD.

The default client explicitly installs an empty `ProxyHandler({})`: it ignores
system proxy settings and `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`, so SEC bulk
downloads do not silently consume a local Clash Verge proxy quota. A direct
connection failure is reported and never falls back to a proxy. Proxy use is
allowed only through an explicitly injected opener, is caller-owned, and is
recorded as a boolean safe-provenance flag without recording the proxy URL.

Only canonical HTTPS URLs on `www.sec.gov:443` and `data.sec.gov:443` are
permitted. Userinfo, non-default ports, fragments, and caller-supplied query
strings are rejected. The client disables the opener's implicit redirects and
handles at most three redirects explicitly; every target is recanonicalized and
revalidated before a request. N-PORT ZIP responses allow only
`application/zip` and `application/octet-stream`; EDGAR submissions responses
allow only `application/json` (ignoring a valid charset parameter). The client
sends no cookies or authorization header.

The default retry policy means three total attempts, `Retry-After` is accepted
only as a non-negative integer no greater than 60 seconds, and total per-request
retry delay is capped at 120 seconds. The default limiter is 5 requests/second
with no internal concurrency, below the SEC's published aggregate maximum; a
caller remains responsible for coordination with its other processes. Only
classified transient network failures, HTTP 429, and HTTP 500/502/503/504 are
retried. HTTP 400/401/403/404, other status codes, invalid content type,
oversized or malformed headers, redirect-policy violations, and schema errors
are permanent. At most 128 response headers, each at most 8 KiB, are accepted.

Tests use injected responses and perform no network calls. Live SEC access is a
separate explicitly authorized integration action.

### 4.3 Artifact store

`SecArtifactStore(root)` owns provider artifacts at a caller-selected root. A
quarter download is streamed to a sibling temporary file while computing
SHA-256, flushed and fsynced, validated as a ZIP, then published immutably under:

```text
<root>/sec/nport/<year>q<quarter>/<artifact_sha256>/
    source.zip
    manifest.json
```

The manifest records schema version, source URL, quarter, byte count, SHA-256,
HTTP validators when present, content type, retrieved UTC timestamp, and parser
contract version. It never records the User-Agent/contact string or response
headers outside an explicit safe allowlist. Existing content is never
overwritten. Same-quarter/different-content creates another artifact version.
Interrupted downloads, ZIP validation failures, and manifest publication
failures leave no valid-looking final artifact. Publication fsyncs the files and
their final parent directory. Every artifact path component is checked with
`lstat`; symlinked roots/components or non-regular final files fail closed.

Replay validates the path, manifest schema, quarter, file size, hash, ZIP
central-directory structure, expected required members, and parser version
before parsing. Member CRC and observed expanded byte limits are validated
during the one permitted streaming scan of each required member; replay does
not call a whole-archive `testzip()` pass.

## 5. N-PORT native table contract

P0 reads only these required members, matched case-insensitively by basename but
rejecting duplicate matches:

- `SUBMISSION`
- `REGISTRANT`
- `FUND_REPORTED_INFO`
- `FUND_REPORTED_HOLDING`
- `IDENTIFIERS`

The reader must honor the ZIP's tab-delimited UTF-8 format and preserve empty
fields as `None`. A bulk TSV date is first required to be ASCII and is then
ASCII-uppercase-normalized. The normalized value must exactly match
`^[0-9]{2}-(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)-[0-9]{4}$`.
The parser maps those twelve tokens with a code-owned table and constructs a
calendar `date`; it never uses `%b`, the process locale, Unicode case folding,
or Unicode digit semantics. Leading/trailing whitespace, wrong widths,
non-ASCII input, unknown month tokens, and invalid calendar dates fail. Thus
upper-, lower-, and mixed-ASCII-case month spellings are accepted by explicit
policy, not because the probe observed every spelling. EDGAR JSON dates remain
ISO `YYYY-MM-DD`; timestamps parse to timezone-aware `datetime`. SEC
fixed-point numbers must first match the ASCII grammar
`^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$`, then parse to a finite `Decimal`.
Whitespace, underscores, Unicode digits, exponent notation, `NaN`, and
infinities fail. The reader must never parse reported money, balances,
percentages, or rates through binary float. Provider-native
strings remain available in the native payload even when typed values are also
exposed; native flag strings are not silently rewritten to booleans and native
numeric text is not rewritten in the preserved row.

Required fields and native roles:

### Submission

- `ACCESSION_NUMBER` — filing identity, exactly ASCII
  `^[0-9]{10}-[0-9]{2}-[0-9]{6}$`;
- `FILING_DATE` — SEC filing date, date precision only;
- `SUB_TYPE` — including `NPORT-P`, `NPORT-P/A`, and notices;
- `REPORT_ENDING_PERIOD` — fiscal year-end role;
- `REPORT_DATE` — holdings economic date;
- `IS_LAST_FILING` — native nullable flag.

Only `NPORT-P` and `NPORT-P/A` produce holding snapshots. A notice form is
preserved in the source artifact but is not treated as an empty portfolio.

### Registrant/fund

- registrant `CIK`, name, file number, LEI where present;
- fund `SERIES_ID`, `SERIES_NAME`, `SERIES_LEI`, `TOTAL_ASSETS`,
  `TOTAL_LIABILITIES`, and `NET_ASSETS`.

N-PORT holdings are series-level. `class_id` and exchange ticker are not
invented from series name. Any class/ticker mapping is a distinct EDGAR entity
mapping with its own source and validity evidence; P0 selection uses
`SERIES_ID`.

### Holding

- identity: `ACCESSION_NUMBER`, `HOLDING_ID`;
- issuer: `ISSUER_NAME`, `ISSUER_LEI`, `ISSUER_TITLE`, `ISSUER_CUSIP`;
- amount/value: `BALANCE`, `UNIT`, `OTHER_UNIT_DESC`, `CURRENCY_CODE`,
  `CURRENCY_VALUE`, `EXCHANGE_RATE`, `PERCENTAGE`;
- classification: `PAYOFF_PROFILE`, `ASSET_CAT`, `OTHER_ASSET`, `ISSUER_TYPE`,
  `OTHER_ISSUER`, `INVESTMENT_COUNTRY`, `IS_RESTRICTED_SECURITY`,
  `FAIR_VALUE_LEVEL`, `DERIVATIVE_CAT`.

### Identifier child rows

- identifier key: `HOLDING_ID`, `IDENTIFIERS_ID`;
- `IDENTIFIER_ISIN`, `IDENTIFIER_TICKER`, `OTHER_IDENTIFIER`,
  `OTHER_IDENTIFIER_DESC`.

No single ticker is promoted to canonical security identity in P0. Multiple
identifier rows are preserved. Joins are scoped by artifact version; canonical
persisted identities include quarter, accession, and holding ID even where a
bulk child table exposes only `HOLDING_ID`.

## 6. Extraction algorithm and resource envelope

One quarterly ZIP is one work unit. P0 must not materialize a full SEC table or
all quarters in memory.

For each replay-validated ZIP:

1. scan small submission, registrant, and fund tables to resolve requested
   series and relevant accessions;
2. fail on duplicate accession parent rows, missing required series, malformed
   keys, or conflicting parent facts;
3. stream the full holding table once, retaining only matching accessions and a
   set of their holding IDs;
4. stream the identifier table once, retaining only matching holding IDs;
5. skip child rows belonging to unselected holdings; fail only when a retained
   child cannot map to exactly one selected holding, when a selected
   `HOLDING_ID` is reused by another accession in the same artifact (making the
   child-table join ambiguous), when a selected holding references inconsistent
   parents, or when required coverage is partial; and
6. return deterministic tuples sorted by explicit native identities, without
   modifying provider values.

Complexity is `O(total rows in required members + selected rows log selected
rows)`. Full-source scans are bounded to one per required member. Peak resident
data is bounded by the selected series' rows and identities plus ZIP/csv buffers,
not by the whole quarterly holding table. Default quarter concurrency is one;
P0 exposes no internal process or thread fan-out.

The frozen safe defaults are: 2 GiB maximum compressed response, 64 archive
members, 16 GiB maximum expanded bytes for one required member, 32 GiB maximum
observed expanded bytes across required members, 250:1 maximum per-member
compression ratio, 1 MiB download/read chunks, and a 3,600-second caller-visible
operation deadline. Callers may lower these bounds; raising them is an explicit
construction parameter recorded in safe provenance. Declared sizes and central
directory metadata are checked before member reads; actual downloaded and
expanded byte counters are checked on every chunk so false metadata cannot
bypass a limit.

The ZIP reader accepts stored or deflated regular files only. It rejects
encryption, unsupported compression, more than one normalized match for a
required basename, NULs, absolute paths, `..` segments, platform drive/UNC
paths, and symlink/device/special-file attributes. It never extracts archive
paths directly to the filesystem.

Progress hooks may report safe phase names, compressed bytes, and row counts;
they must not report response rows or the User-Agent. A caller cancellation or
deadline stops between chunks/rows, closes handles, removes the temporary file,
and publishes no success manifest.

Before any full live history run, a separately authorized bounded probe must
process one quarter and record artifact size/hash, selected accession/holding
counts, member scan counts, elapsed phases, and peak RSS when available. The
2026Q2 probe described in section 13 satisfies this gate for the selected XLK
series only; it does not establish universal history coverage or production
acceptance.

## 7. Time, amendments, and resolver semantics

`SecFundHoldingVintage` is a frozen immutable value containing:

- dataset artifact identity and source URL;
- accession, CIK, series ID/name/LEI;
- report date, fiscal period end, filing date, submission type;
- nullable `accepted_at` and its source lineage;
- exact row/child tuples and deterministic native payload hash;
- observation/retrieval timestamp;
- `availability_anchor`, `availability_basis`, `availability_precision`, named
  availability-policy/version, revision relationship, and quality flags; and
- `vintage_identity`, the SHA-256 of canonical UTF-8 JSON containing artifact
  SHA-256, CIK, series ID, report date, accession, submission type, accepted-at
  encoding, payload hash, and parser schema version.

Dates encode as ISO `YYYY-MM-DD`, timestamps as UTC ISO-8601 ending `Z`,
`Decimal` values as normalized base-10 strings preserving numeric value, tuples
as arrays, objects with lexically sorted keys, and null as JSON null. Native row
ordering is canonicalized by the frozen parent/child identities before hashing.
The public collection is `SecHoldingVintageSet`, constructed only from
replay-validated quarter results. It rejects duplicate vintage identities,
duplicate `(CIK, accession)`, a series ID associated with multiple CIKs unless
the caller queries the CIK explicitly, and the same accession carrying
conflicting payload or availability evidence.

The economic key is `(CIK, SERIES_ID, REPORT_DATE)`. The filing identity is
`(CIK, ACCESSION_NUMBER)`. An amendment never overwrites an original filing.

Availability construction has exactly two P0 policies:

- `OBSERVATION_ONLY_V1` (default): anchor is the first replay-validated OMD
  artifact observation time, basis `PROVIDER_FIRST_OBSERVED`, precision
  `TIMESTAMP`, and historical eligibility begins only then. It makes no claim
  that the value was publicly visible earlier.
- `ACCEPTED_AT_PLUS_LAG_V1`: opt-in and caller supplied with a non-negative lag
  no greater than 30 calendar days; anchor is `accepted_at + lag`, basis
  `INFERRED_SCHEDULE`, precision `TIMESTAMP`, and quality includes
  `PIT_INFERRED`. It asserts only the caller's conservative research policy,
  not an SEC-declared or proven public-availability time. The selected lag and
  policy version are in request/result identity and provenance.

If the selected policy cannot produce an anchor, basis is `UNKNOWN`, precision
is `UNKNOWN`, and the vintage is ineligible. `accepted_at` by itself never gets
`SOURCE_DECLARED` basis. No P0 policy produces `pit_proven=True` under the core
`AvailabilityEvidence` definition.

`SecHoldingVintageSet.resolve(cik, series_id, knowledge_at)` uses the following
total algorithm:

1. require an aware timestamp and exact CIK/series match;
2. discard unknown anchors, anchors after `knowledge_at`, and any record whose
   report date is after `knowledge_at`'s UTC date;
3. group by the full economic key and choose the latest filing using ascending
   `(availability_anchor, accepted_at-or-minimum, filing_date,
   accession_number)`; equal valid timestamps are allowed and accession is the
   deterministic final tie-breaker;
4. from those per-report winners choose the greatest `REPORT_DATE`; and
5. return that vintage with its basis/policy/flags, or a typed not-yet-available
   result—never a nearest report selected without knowledge-time eligibility.

A "collision" means the same filing identity has different anchors, payload
hashes, form/report metadata, or native rows; collection construction fails.
Equal timestamps on different filing identities are not collisions.

OMD does not accept a trading `session` in P0 and does not calculate
`first_usable_session`. The consumer may conservatively map an accepted filing
to a later canonical session using its own exchange calendar and cutoff.

## 8. Error, missing, and coverage semantics

Public SEC errors subclass existing stable OMD categories where applicable:

- HTTP authentication/permission, rate-limit, transient, and permanent errors;
- `SchemaMismatchError` for missing/renamed fields, invalid UTF-8, invalid
  documented types, or conflicting parent records;
- `PaginationError` is not used for bulk tables; truncated stream/member errors
  are schema/integrity failures;
- `SnapshotIntegrityError` for artifact or replay tampering;
- `CoverageError` for absent required series/accessions, partial selected child
  coverage, or reconciliation gates explicitly selected by the caller;
- `EmptyResponseError` only when the request's explicit empty policy requires
  selected holdings.

Native nulls remain null. Missing `PERCENTAGE`, identifiers, debt fields, or
market values never become zero. Negative balances/weights, derivatives, cash,
and liabilities are preserved and may not be silently dropped by an
"equity-only" filter. P0 reports QA facts but does not renormalize:

- sum of non-null reported percentages;
- count and reported-weight coverage by identifier availability;
- duplicate issuer/CUSIP/ticker diagnostics;
- holdings-to-net-assets reconciliation where native values permit it; and
- snapshot age computed only by a consumer-provided query date.

QA thresholds are caller-selected policies. A warning never converts a required
coverage failure into success.

For P0, required coverage means: every `required_series_id` appears in exactly
one consistent fund parent for at least one requested-quarter
`NPORT-P`/`NPORT-P/A` accession; every selected accession has exactly one
submission, registrant, and fund parent; every retained holding has that
selected accession; native primary keys are unique; and every retained
identifier maps unambiguously to one selected holding. Identifier presence on
every holding and numerical reconciliation thresholds are not implicitly
required; callers must select those separate QA gates explicitly.

## 9. EDGAR enrichment contract

The official submissions history is read for each relevant registrant CIK. P0
preserves the exact accession, form, filing date, report date, primary document,
and acceptance datetime when present. Column arrays must have equal lengths;
historical-file references are validated and bounded; required accession
coverage is exact.

Historical references are accepted only when the complete basename matches
`^CIK[0-9]{10}-submissions-[0-9]{3}\.json$` and its embedded CIK equals the
request CIK. They are rebuilt as
`https://data.sec.gov/submissions/<basename>`; source-provided schemes, hosts,
paths, queries, and fragments are never followed. P0 allows at most 16
historical files and 128 MiB total JSON bytes per CIK, rejects duplicate
basenames and cycles, requires each returned CIK to match the request, and stops
once all required accessions are found. Missing required accession coverage is
`CoverageError`, not a partial success.

The enrichment join is `(CIK, ACCESSION_NUMBER)`. Form/report-date disagreement
with N-PORT bulk data fails closed. A missing acceptance timestamp remains
missing and carries `ACCEPTANCE_TIME_MISSING`; it is never replaced with midnight
of filing date. The complete submission header may be a later audit fallback but
is not required for the first parser slice.

## 10. Explicit non-goals

- legacy `N-Q`, `N-CSR`, `N-30D`, or `N-30B-2` parsing;
- GLD and other commodity-trust 10-Q/10-K schedules;
- hard-coded SPY/QQQ/IWM/XLK/SMH/XLF/XLE/XLV/USMV/TLT/IEF/SHY universe data;
- `FULL_PIT` or universal start-date claims;
- issuer-archive/reconstructed source grades;
- canonical security-master resolution or ticker survivorship repair;
- bond ETFs, money-market funds, and currency ETFs;
- `DEBT_SECURITY` extraction and bond maturity/coupon analytics;
- provider-owned equity/bond/commodity classification (the caller supplies the
  reviewed equity ETF series scope);
- breadth, moving averages, dispersion, momentum, HHI, top-N concentration,
  duration, maturity buckets, or other strategy/research features;
- consumer Parquet/DuckDB paths, publication, scheduling, or live deployment;
- committing real downloaded data or SEC responses to Git; and
- live downloads, commits, pushes, releases, or publication in this slice.

## 11. Required offline tests

Synthetic fixtures must cover:

1. request validation/identity and absence of User-Agent/path from identities;
2. HTTP host/redirect/content-type/size/rate/retry classification and redaction;
3. streaming atomic download, interruption cleanup, immutable same/different
   content, concurrent publication, replay, tampering, ZIP bomb/path defenses;
4. success with two series in one registrant and exact caller selection;
5. missing optional and required series, allowed/required empty holdings;
6. native `Decimal`, date, null, negative, zero, and multiple-identifier
   preservation; date tests cover all twelve ASCII month tokens, upper/lower/
   mixed ASCII case, leap-day and invalid calendar values, whitespace/wrong
   width, non-ASCII digits or letters, and behavior under a non-English process
   locale where available;
7. malformed schema/UTF-8/date/number, duplicate parents/keys, orphan children,
   partial coverage, and stable ordering;
8. unrelated child rows, cross-accession holding-ID collision, and one
   structural assertion that each required archive member is scanned at most
   once and no full-table list is constructed;
9. NPORT-P plus NPORT-P/A append-only vintages, equal timestamp tie-breaking,
   old-report amendment versus new-report selection, duplicate filing
   collisions, and knowledge-time resolution under both named policies;
10. missing acceptance time, form/report mismatch, array-length mismatch,
    exact historical accession coverage, and unknown availability fail-closed;
11. deterministic payload hashes and replay-equivalent results; and
12. no network access in the default test suite, including proof that the
    default opener ignores system/environment proxies and never falls back.

Focused SEC tests run first. Acceptance then requires the repository canonical
gates:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
git diff --check
```

Inspect the built wheel/sdist contents and public imports. Inspect the actual
diff and tracked filenames for real data, credentials, request headers, or
consumer configuration. No live SEC call is acceptance evidence for P0.

## 12. Implementation sequence and stop conditions

1. Implement typed requests/errors and injected HTTP behavior.
2. Implement streaming immutable artifact storage and replay validation.
3. Implement N-PORT table parsing/extraction and QA facts.
4. Implement EDGAR submissions metadata parsing/enrichment.
5. Implement append-only snapshot assembly and knowledge-time resolver.
6. Add public exports, README, changelog, and offline tests.
7. Run focused then global validation and inspect artifacts/diff.

Stop and return `EXECUTION_BLOCKED` if:

- the official data schema contradicts a frozen field/key/date/unit assumption;
- P0 requires a new dependency or generic core behavior change;
- required acceptance metadata cannot be obtained without inventing precision;
- resource bounds cannot be structurally enforced;
- a real response or contact identifier would enter a tracked artifact;
- pre-existing user changes overlap the authorized edits incompatibly; or
- any requested work expands into legacy parsing, consumer research semantics,
  a live provider run, or external publication.

## 13. Later slices

- **P1 — N-PORT live bounded probe: COMPLETE FOR XLK / 2026Q2 ONLY.** On
  2026-08-29 an explicitly authorized probe retained the locally downloaded
  `2026q2_nport.zip` outside Git: 440,699,889 compressed bytes,
  SHA-256
  `077cc836a978a593b29012219395fbe9c303d5e930f5be3b5f4353c3b02296fc`,
  and the five required TSV members were fully streamed without a ZIP CRC
  error. This evidence does not independently prove the download transport,
  proxy route, source URL, or the CRC of unconsumed members. The exact selected
  identity was CIK `0001064641`, series `S000006415`, accession
  `0001410368-26-055331`, report date `2026-03-31`, filing date `2026-05-28`,
  and EDGAR acceptance timestamp `2026-05-28T19:10:34.000Z`. One scan of each
  required member examined 5,347,869 holding rows and retained 76 XLK rows;
  one identifier scan retained 76 child rows covering all 76 holdings, with no
  duplicate native identifier key or selected holding-ID cross-accession
  collision. Reported percentages summed to `100.006967057702`; reported
  currency values exceeded net assets by `5866153.04`, so P0 preserves those
  native values and reports reconciliation facts rather than silently
  normalizing them. Total assets minus total liabilities equaled net assets
  exactly. The retained summary concretely records the selected parent date
  lexemes `28-MAY-2026` and `31-MAR-2026`; it does not claim that all source
  dates or every accepted lexical variant were empirically exercised. Probe
  outputs and the ZIP remain ignored local artifacts and are not fixtures or
  acceptance evidence for the offline P0 implementation.
- **P2 — legacy SEC holdings:** filing-family-specific parser probes, beginning
  with simple schedules; each fund family must pass section selection, units,
  weights, totals, amendment, and PIT-evidence gates before `FULL_PIT` status.
- **P3 — consumer integration:** consumer-owned series map, calendar cutoff,
  `first_usable_session`, data publication, and golden parity artifacts.
- **P4 — research features:** separate research workflow after dataset freeze;
  no strategy semantics are selected by this engineering contract.

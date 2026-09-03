# SEC N-PORT Structural Qualification V1

Status: **IMPLEMENTED AND ACCEPTED**

Target release: `0.1.6`

Predecessors:

- [`sec-nport-fund-holdings-pit-v1.md`](sec-nport-fund-holdings-pit-v1.md)
- [`sec-nport-batch-cli-core-dataset-v1.md`](sec-nport-batch-cli-core-dataset-v1.md)

This plan adds an offline, provider-semantic qualification layer above the
existing immutable SEC N-PORT core dataset. It does not define a strategy,
constituent breadth, trading sessions, portfolio weights, an economic test, or
consumer acceptance thresholds.

## 1. Objective

Add an `omd sec nport qualify` command and public Python API that answer:

> For an exact reviewed fund universe, quarter range, availability policy, and
> immutable partition set, what provider-native coverage, vintage, amendment,
> weight, identifier, and availability facts are present?

The result is a deterministic, immutable qualification bundle suitable for a
consumer to bind as research input. Qualification is downstream of
`validate`; it never weakens or replaces artifact integrity validation.

The command must:

1. validate the complete requested local artifact/catalog closure without
   network access;
2. select one exact partition per requested quarter without silently choosing
   between revisions;
3. verify the reviewed ETF identities expected in each quarter;
4. characterize fund vintages and amendment/revision families;
5. measure provider-native holding weights without filtering or
   renormalization;
6. measure identifier presence and conflicts without inventing a canonical
   security identity;
7. report availability evidence without mapping it to trading sessions;
8. publish deterministic Parquet facts plus a canonical receipt atomically and
   without overwrite; and
9. expose structural counters proving bounded scans, rows, partitions, and
   peak resident data when available.

## 2. Current-state evidence

OMD `0.1.5` already has:

- immutable quarterly SEC N-PORT ZIP artifacts and manifests;
- EDGAR submissions closures and append-only fetch receipts;
- a reviewed fund universe with exact CIK/series selectors;
- `fund_vintages.parquet`, `holdings.parquet`, and `identifiers.parquet` core
  partitions;
- catalog, file, logical-table, quality, and partition identities;
- `OBSERVATION_ONLY_V1` and `ACCEPTED_AT_PLUS_LAG_V1` availability evidence;
- offline `validate` and single-quarter `inspect`; and
- atomic/no-replace core publication.

The current ignored local lake contains 27 source quarters from `2019q4`
through `2026q2`. Its reviewed equity-fund rows include SPY, QQQ, IWM, XLK,
XLF, XLE, XLV, SMH, and USMV in every source quarter. This is local discovery
evidence, not a frozen release fixture or a claim that every consumer decision
date is covered.

`validate` currently proves artifact integrity, schema, primary-key ordering,
hashes, row counts, catalog registration, and fetch-receipt closure. `inspect`
reports useful per-partition percentage and identifier counts. Neither command
produces one deterministic cross-quarter structural qualification artifact.

## 3. Ownership boundary

### 3.1 OMD owns

- exact local artifact and partition validation;
- provider-native fund, filing, holding, and identifier facts;
- quarter/universe/availability-policy partition selection;
- deterministic grouping by SEC economic and filing identities;
- amendment and revision-family characterization;
- native percentage sums, null/negative counts, and reconciliation facts;
- identifier presence, multiplicity, and conflict facts;
- availability basis, precision, anchor, policy, lag, and quality flags;
- deterministic qualification schemas, receipts, counters, and publication;
- stable error categories for missing, ambiguous, corrupt, or partial inputs.

### 3.2 Consumers own

- portfolio, strategy, and ETF selection semantics beyond the reviewed source
  universe;
- signal/cycle dates, exchange calendar, timezone, and `first_usable_session`;
- maximum snapshot age and historical eligibility policy;
- mapping native CUSIP/ISIN/ticker observations to a canonical security master;
- constituent type filters and treatment of cash, derivatives, debt, shorts,
  or other non-equity rows;
- acceptable weight and identifier coverage thresholds;
- price sourcing, adjustment policy, return horizons, breadth, exposure,
  factors, A/B tests, and economic decisions.

OMD must not embed consumer names such as `H7`, `strong_70`, `BROAD`, `NARROW`,
`A_WIN`, or `B_WIN` in public qualification schemas.

## 4. CLI and public API

Add the offline command:

```text
omd sec nport qualify \
  --quarters START:END \
  --root DIR \
  --universe FILE \
  --availability-policy POLICY \
  [--lag-days N] \
  [--partition-set FILE] \
  --output DIR \
  [--json] [--quiet]
```

The existing date, universe, availability-policy, and lag validation rules are
reused exactly. `qualify` never accepts contact information and never creates a
network client.

Public Python API:

```python
qualify_sec_nport(
    *,
    root: Path,
    quarters: tuple[Quarter, ...],
    universe: SecEquityEtfUniverse,
    availability_policy: str,
    lag_days: int | None,
    output: Path,
    partition_set: SecNportPartitionSet | None = None,
    progress: QualificationProgress | None = None,
    deadline: Deadline | None = None,
) -> SecNportQualificationRef
```

The return value contains only immutable artifact references, safe structural
counters, and the receipt identity. It does not retain mutable dataframes.

## 5. Exact partition selection

Qualification inputs are identified by:

```text
source_quarter
artifact_sha256
artifact_manifest_sha256
universe_hash
edgar_closure_hash
parser_version
availability_policy
lag_days
writer_profile_version
pyarrow_version
partition_identity
```

For every requested quarter, the catalog is filtered by exact universe hash,
availability policy, and lag. The result must be exactly one partition unless
the caller supplies a canonical partition-set document.

An implicit selection with zero matches raises `CoverageError`. More than one
match raises `AmbiguousPartitionError`; insertion order, catalog recency,
filesystem mtime, lexical hash order, or newest retrieval time must never pick
a winner.

Optional partition-set schema:

```json
{
  "schema_version": "sec-nport-partition-set-v1",
  "entries": [
    {
      "source_quarter": "2024q1",
      "partition_identity": "<sha256>",
      "manifest_hash": "<sha256>"
    }
  ]
}
```

Entries are unique, sorted by quarter, cover the exact requested range, and
must resolve through the validated catalog. The canonical partition-set hash
is part of qualification identity.

## 6. Qualification algorithm

Processing order is fixed.

### 6.1 Integrity prerequisite

Run the same production validation used by `omd sec nport validate` for the
requested quarter set. Qualification stops before reading facts if raw replay,
receipts, catalog, manifests, schemas, primary keys, row counts, file hashes,
logical hashes, or quality hashes fail.

The implementation should expose one shared validated-partition iterator so
`validate` and `qualify` cannot drift. A qualification invocation must not call
the CLI through a subprocess or parse human-readable `validate` output.

### 6.2 Quarter and fund coverage

For each selected partition:

1. resolve active universe selectors for the source quarter;
2. require every active selector to have at least one fund vintage;
3. reject any returned fund symbol or exact SEC selector outside the reviewed
   universe;
4. retain every original and amendment filing; and
5. report source-quarter coverage independently from `report_date`.

The command does not assume all funds report on a common month-end and does not
require one vintage per fund per source quarter.

### 6.3 Vintage and amendment facts

Use these identities:

```text
fund identity:   (cik, series_id-or-null)
filing identity: (cik, accession_number)
economic key:    (cik, series_id-or-null, report_date)
vintage identity: persisted vintage_identity
```

Within an economic key, sort filings by:

```text
availability_anchor-or-max
accepted_at-or-max
filing_date
accession_number
```

Emit all members. Record whether each filing is an original, an amendment
candidate (`NPORT-P/A`), or another retained submission type. Derive a
deterministic predecessor only when ordering and economic identity are
unambiguous. Do not overwrite an original, collapse an amendment, or claim
provider-declared amendment lineage when the source exposes only form and
ordering evidence.

Unknown availability remains unknown. Qualification does not choose the
filing that a consumer would have known at a particular strategy timestamp.

### 6.4 Weight facts

For each vintage, aggregate all provider-native holdings exactly once and emit:

- holding row count and unique holding-ID count;
- non-null, null, zero, positive, and negative percentage counts;
- exact Decimal sum of all non-null percentages;
- Decimal sums by provider-native asset/issuer/payoff/country categories;
- counts and weights for derivatives, restricted securities, debt-like rows,
  cash-like rows where natively identifiable, and unknown categories;
- currency and exchange-rate missingness;
- native balance/value missingness; and
- quality/reconciliation facts already present in the source partition.

Negative, zero, derivative, cash, debt, restricted, foreign, and unknown rows
are retained. Qualification never filters them and never renormalizes the
remaining weights. Decimal values remain Decimal through aggregation and use
the existing canonical decimal encoding in JSON/Parquet metadata.

No universal `percentage_sum == 100` gate is imposed. Exact totals are facts;
consumer thresholds remain external.

### 6.5 Identifier facts

For each vintage, emit:

- holdings with zero, one, or multiple identifier child rows;
- count and native percentage sum having any identifier;
- separate presence counts for CUSIP, ISIN, ticker, and other identifier;
- duplicate native identifier values across different holding IDs;
- one holding associated with multiple distinct values of the same identifier
  type;
- one identifier value associated with multiple issuer names/titles in the
  same vintage; and
- null/empty/native-conflict counts.

These are observations, not a canonical security master. OMD must not choose a
preferred ticker, rewrite historical symbols, infer share-class equivalence, or
declare CUSIP/ISIN permanently stable.

### 6.6 Availability facts

For each vintage, retain and validate:

```text
report_date
filing_date
accepted_at
observed_at
availability_anchor
availability_basis
availability_precision
availability_policy
availability_lag_days
quality_flags
```

Emit counts by basis/precision/quality flag and minimum/maximum known anchors.
An `UNKNOWN` anchor is reported and is never filled from report, filing,
quarter, retrieval, or neighboring-vintage dates.

`ACCEPTED_AT_PLUS_LAG_V1` remains `PIT_INFERRED`; qualification must not rename
it `PIT_PROVEN` or `SOURCE_DECLARED`. No exchange calendar or first usable
session is calculated.

## 7. Output contract

Publication layout:

```text
<output>/
  qualification_request.json
  quarter_fund_coverage.parquet
  vintage_quality.parquet
  amendment_facts.parquet
  identifier_quality.parquet
  qualification_receipt.json
```

The exact output directory must not exist. Build in a sibling private
directory, fsync files and directory, validate the completed private artifact,
and publish by atomic no-replace rename. Symlinked roots/components, special
files, path escape, partial existing output, or concurrent winner all fail.

### 7.1 `quarter_fund_coverage.parquet`

One row per `(source_quarter, fund_symbol)` with:

```text
source_quarter
fund_symbol
cik
series_id
selection_mode
expected
vintage_count
original_count
amendment_count
earliest_report_date
latest_report_date
earliest_availability_anchor
latest_availability_anchor
unknown_availability_count
partition_identity
```

### 7.2 `vintage_quality.parquet`

One row per vintage identity containing the filing/economic identity,
availability fields, holding counts, exact percentage aggregates, category
counts/weights, missingness, reconciliation values, and quality flags described
above.

### 7.3 `amendment_facts.parquet`

One row per filing within an economic-key family:

```text
cik
series_id
report_date
accession_number
submission_type
vintage_identity
family_size
family_order
predecessor_accession_number
relation_basis
availability_anchor
payload_hash
```

`relation_basis` is a closed enum such as `ORIGINAL_ONLY`,
`FORM_AND_ORDER_INFERRED`, or `AMBIGUOUS`. It never claims an official link that
was not present in source evidence.

### 7.4 `identifier_quality.parquet`

One row per vintage with identifier row/holding coverage, exact covered native
weight, per-type presence counts, multiplicity, duplicate-value counts, and
conflict counts.

### 7.5 Receipt

`qualification_receipt.json` contains exactly:

```text
schema_version
request
universe
partition_set
input_partitions
output_artifacts
schemas
counters
coverage_summary
availability_summary
gates
qualification_status
started_at
completed_at
elapsed_seconds
peak_rss_bytes
```

Artifact descriptors contain path relative to the qualification directory,
bytes, SHA-256, row count, schema fingerprint, and logical-table hash. The
receipt is canonical JSON and is excluded from its own artifact manifest.

Closed status enum:

- `STRUCTURALLY_COMPLETE`: exact expected fund/quarter coverage and no
  structural ambiguity; measured null/missing facts may still be unacceptable
  to a consumer;
- `STRUCTURALLY_PARTIAL`: facts are valid but expected source coverage or
  availability/identifier/weight observations are missing;
- `STRUCTURALLY_AMBIGUOUS`: multiple valid partitions or identities cannot be
  uniquely bound without caller input; and
- `INVALID_SOURCE`: integrity, schema, identity, or deterministic replay
  failed.

Only `STRUCTURALLY_COMPLETE` publishes a success bundle. Partial and ambiguous
analysis may publish only to an explicitly separate diagnostic namespace with
the same immutable/no-overwrite rules; it is never labeled success.

Intrinsic gates are limited to:

```text
SOURCE_VALIDATED
PARTITION_SET_EXACT
EXPECTED_FUND_QUARTERS_ACCOUNTED
FUND_IDENTITIES_EXACT
VINTAGE_IDENTITIES_UNIQUE
AMENDMENT_FAMILIES_DETERMINISTIC
WEIGHT_FACTS_RECONSTRUCTED
IDENTIFIER_FACTS_RECONSTRUCTED
AVAILABILITY_FACTS_RECONSTRUCTED
ARTIFACTS_REOPENED
RESOURCE_ENVELOPE_PASSED
```

No gate asserts a consumer-specific minimum identifier percentage, acceptable
snapshot age, equity-only completeness, or economic fitness.

## 8. Determinism and independent replay

All output rows use explicit lexical sort keys. Input Parquet row-group order,
catalog insertion order, filesystem enumeration, dictionary order, and worker
completion order cannot affect bytes.

The post-persist validator must:

1. reopen the canonical request and exact selected partition descriptors;
2. rerun production local validation for those inputs;
3. independently reconstruct every output row and aggregate from validated
   source tables without trusting receipt gates or summaries;
4. compare every Parquet schema, row count, logical hash, file bytes/hash, and
   canonical receipt field; and
5. recheck all input and output descriptors after validation to detect drift.

The independent reconstructor may share frozen scalar codecs and schemas. It
must not call the output writer or accept caller-provided pass flags.

## 9. Resource and progress contract

One core partition is one work unit. The implementation must not build a
quarters × funds × holdings Cartesian expansion or retain all quarters' holding
rows in memory.

Required structure:

1. validate/select the exact partition list once;
2. process partitions in canonical quarter/identity order;
3. read each selected core table at most once per qualification pass;
4. aggregate holdings and identifiers by persisted vintage/holding keys in one
   bounded pass per table;
5. flush deterministic intermediate rows per partition or bounded batch;
6. merge only small qualification rows across quarters; and
7. run the independent replay as a second explicit bounded pass.

The receipt records:

```text
partitions_selected
fund_vintage_rows_read
holding_rows_read
identifier_rows_read
table_scans
row_groups_read
qualification_rows_written
replay_rows_read
replay_table_scans
phase_elapsed_seconds
peak_rss_bytes
```

No multiprocessing or thread fan-out is enabled by default. Progress reports
safe phase, quarter index/count, partition index/count, and row counters; it
never prints holding rows or EDGAR contact information. Cancellation/deadline
checks occur between partitions and record batches. Failure terminates cleanly
and publishes no success directory.

Before a full 27-quarter local qualification, add a deterministic full-shape
synthetic preflight and run one real quarter probe. Tests assert structural
scan counts and cleanup; elapsed wall time alone is not a gate.

## 10. Error semantics

Reuse stable OMD error families:

- `SnapshotIntegrityError`: raw, manifest, hash, replay, or persisted-output
  tampering;
- `SchemaMismatchError`: request, partition-set, source, or output schema
  mismatch;
- `CoverageError`: missing requested quarter, fund, receipt, or partition;
- `AmbiguousPartitionError`: more than one eligible partition without an exact
  partition-set choice; and
- `ResourceLimitError`: row, byte, deadline, or memory envelope exceeded.

Do not downgrade an integrity/ambiguity error to a warning. Missing identifier
or native percentage values are measured source facts unless a required
identity/field contract makes reconstruction impossible.

## 11. Test matrix

Offline tests must cover:

1. one/multiple quarters and scheduled universe bounds;
2. all expected funds present, one missing, and an unexpected fund;
3. implicit unique partition selection, zero match, ambiguous matches, and
   exact partition-set resolution;
4. original-only filing, ordered amendment family, equal timestamps, unknown
   availability, and ambiguous family;
5. null/zero/positive/negative percentages and exact Decimal aggregation;
6. cash, derivative, debt, restricted, foreign, and unknown rows retained;
7. zero/one/multiple identifiers, duplicate values, conflicting values, and
   missing identifier weight;
8. `OBSERVATION_ONLY_V1`, `ACCEPTED_AT_PLUS_LAG_V1`, and unknown anchors;
9. catalog/file/logical/quality/manifest tampering and unindexed partition;
10. stable row ordering and byte-identical repeated builds in separate roots;
11. private-stage failure, concurrent no-replace winner, path escape, symlink,
    special file, partial publication, and post-persist tampering;
12. post-persist mutation of every artifact and receipt gate/summary;
13. injected cancellation/deadline and no orphan/temp residue;
14. structural table-scan/row counters and bounded synthetic full shape; and
15. one ignored local 9-fund/27-quarter characterization command whose receipt
    is reviewed but whose downloaded data is never committed.

The ignored local characterization is acceptance evidence only after exact
command, OMD version/commit, universe hash, partition identities, output hashes,
resource counters, and reviewer verdict are recorded in a non-data receipt.

## 12. Documentation and migration

Implementation must update:

- `README.md` with `validate` versus `qualify` semantics and CLI examples;
- `CHANGELOG.md` and all version-bearing files;
- the batch/core dataset plan only if an existing frozen contract changes;
- CLI help and Python API exports; and
- consumer-capability documentation to state that OMD qualification does not
  perform session alignment or economic eligibility.

Existing `validate` behavior, output keys, and exit semantics remain backward
compatible. Existing `inspect` remains a local exploratory view; `qualify` is
the deterministic cross-quarter artifact producer.

This slice does not add yfinance. A future yfinance provider must be a separate
optional-extra plan and may later provide immutable daily-bar facts. It cannot
serve as historical N-PORT holdings evidence or move consumer session/breadth
logic into OMD.

## 13. Acceptance gates

The plan is ready for implementation only after review confirms:

1. `validate` integrity semantics are not weakened or overloaded;
2. partition selection cannot silently choose a revision;
3. provider-native rows, weights, identifiers, and availability remain
   lossless and unrenormalized;
4. no consumer strategy, threshold, calendar, price, or economic semantics
   enter OMD;
5. output/replay schemas and atomic publication are exact;
6. structural counters prove bounded per-partition processing;
7. the synthetic preflight and real-quarter probe are defined; and
8. consumer repositories can bind the qualification bundle by immutable
   version, partition, file, and receipt identities.

Until those gates pass, the next legal action is plan review and amendment.
Running a full qualification, publishing a release, migrating a consumer, or
starting an economic experiment is outside this draft's authority.

# SEC financial source qualification: first probe

Date: 2026-09-11. Status: parsing and reconstruction passed; independent raw-fact
comparison found eight incorrect compound-unit fields, corrected by the observed
v2 follow-up below. Complete qualification remains open. This is a bounded
first-filing probe, not acceptance of the
eight-symbol pilot or overall financial correctness.

## Scope and retained evidence

The local watchlist's 53-symbol compact-JSON hash matched its frozen value.
Only AAPL was selected. SEC submissions metadata confirmed its CIK and Nasdaq
association; this is a current identity check, not historical listing coverage.
The filing probed was 10-Q accession `0000320193-26-000020`.

Four successful serial requests retrieved submissions metadata, the full
submission, its document index, and the index-listed extracted instance.
Each response was retained in an ignored local SnapshotStore. Contact identity,
request headers, original responses, and local artifacts are not committed.
The full submission was 5,946,811 bytes (8 MiB cap); the extracted instance was
962,405 bytes (2 MiB cap). No other filing or symbol was downloaded.

## Findings and fixes

The initial probe exposed two gaps in the synthetic SGML subset:

- The source uses `COMPANY CONFORMED NAME`, with separate former-name entries.
  The parser now accepts that label or legacy `CONFORMED NAME`, while rejecting
  duplicate, malformed, absent, or empty required names.
- Embedded XBRL components carry an outer SGML `<XBRL>` wrapper. The parser now
  removes exactly one complete wrapper before XML validation. Retained bytes
  remain unchanged; unsafe XML and malformed wrappers still fail explicitly.

Both fixes have synthetic regression coverage. Existing successful inputs retain
their results and parser/configuration versions. The related SGML, boundary,
integration, and package-producer suite passed 73 tests; targeted Ruff, format,
type and whitespace checks passed. Independent Astra review accepted the fixes.

## Actual offline result and remaining gate

After bounded snapshot verification, a network-denied parsing probe combined
the retained embedded components with the separately retained instance. Header
acceptance matched the SEC submission metadata. The probe completed in about
0.66 seconds and emitted 180 rows: 60 each for balance sheet, income statement,
and cash flow. Their concept/context/value sets were distinct. These are parser
coverage observations, not assertions that all reported amounts are correct.

The full submission contains no embedded traditional instance, so the public
SGML producer correctly rejects it as missing a required component. The SEC
index lists the extracted instance, but the inspected index supplies filing
acceptance rather than an exact publication timestamp for that extracted
artifact. No qualifying package publication evidence was established. Fetch
time was not substituted for source time, and no PIT projection was written.

Follow-up investigation is complete: the SEC does not provide a timestamp for
first website availability, so the inspected public metadata cannot close this
exact-time gap. The [evidence decision](sec-availability-evidence-decision.md)
records the sources and an additional acceptance-proxy gap in the existing SGML
producer. The market-known query now explicitly rejects automatic SGML proxy
versions, including retained versions.

A separate [observed-package path](sec-known-by-production.md) now reproduces
the retained filing's 180 rows without declaring a publication time. In the
network-denied follow-up, the complete assembled package was observed at
2026-09-11T10:12:10.254416Z and production was recorded at
2026-09-11T10:12:10.260221Z. The local known-by bound is that package observation,
later than the selected SGML receipt; header acceptance stays separate at
2026-07-31T10:01:02Z. Fresh output storage reproduced identical bytes and vintage
identity. Raw responses, package/output receipts and the probe report remain
ignored local artifacts.

This result uses a new observed-row serialization, not a legacy PIT projection.
The separate [system replay contract](sec-observed-system-replay.md) requires
quality and consumer-commit evidence in addition to this production result.
No such evidence is attested for this real filing by the parsing probe.
Do not mark the P1 real-source gate complete or expand the pilot on this result.

The follow-up [observed lifecycle bundle](sec-observed-lifecycle-bundle.md)
probe captured the retained production at 2026-09-11T11:16:03.033675Z, then
reopened the bundle and dependency stores. With network connections denied and
SnapshotStore writes disabled during load, source reconstruction preserved all
180 rows, the production identity, and the original output bytes. Bundle files
were unchanged after load. The probe includes zero quality records and zero
consumer commits; it establishes durable reconstruction, not a financial PASS
or historical market-availability claim. The report and all source data remain
ignored local artifacts.

## Independent raw-instance comparison

A subsequent network-denied diagnostic replayed the retained instance, package,
and output observations through SnapshotStore integrity checks. It used standard
library XML parsing and Decimal, independently of edgartools and OMD's row parser,
to compare the retained output to instance facts. The package's instance bytes
equal the separately retained instance. All 161 parsed contexts identify the
expected issuer. This validates retained evidence consistency, not authenticated
download origin.

For the 180 emitted rows, concept/context, Decimal value, unit reference, native
decimals, period type/start/end and dimension comparisons found no discrepancy:
123 rows match one raw fact and 57 match repeated facts with identical compared
fields. These counts exclude the separate unit-definition comparison below;
they do not mean 180 entirely correct rows. Non-numeric facts, nil facts and
dimensional facts are outside the selected non-dimensional numeric output.

The diagnostic separately inspected the three named presentation roles in the
retained linkbase. Each has 60 non-dimensional numeric concept/context cells
after identical duplicate reconciliation, and all are represented in its output.
No emitted concept falls outside its corresponding role. This explains the
equal row counts without claiming complete rendered-report, notes, taxonomy or
full-filing coverage. The role selection is specific to this filing; locator
membership is not a general presentation-arc or taxonomy validation engine.

**Required finding: compound-unit denominator loss.** Eight income-statement
rows (basic and diluted EPS, four contexts each) retain `unit_ref=usdPerShare`,
but their `unit` is `iso4217:USD`. The raw instance defines that unit as a divide:
numerator `iso4217:USD`, denominator `shares`. A numerator alone is an incorrect
unit. The other 172 emitted rows' simple unit definitions match. The initial
diagnostic skipped compound definitions; it was corrected before accepting this
evidence. The final report records all eight discrepancies rather than treating
unchanged numeric values as a PASS.

Independent source inspection traced the loss to the pinned edgartools instance
unit parser selecting a descendant measure before its divide branch. OMD's
native row adapter trusts that measure. This is a parser/adapter defect, not a
SEC disclosure error. All 180 `standard_concept` fields also equal native
`concept`; no cross-company mapping was established, and README now states that
limitation.

The ignored `run_raw_fact_qualification.py` and
`raw-fact-qualification-report.json` retain script hash, input hashes, observation
identities, counts and row-level discrepancy references. They remain local with
the original data. The final report binds instance SHA-256
`28f986bb243c8fdd445560d381df4b57912ca290b03b8451ecd518e63cdb5d2b` and output SHA-256
`fb269d9b6bb00343ac6a2f93ba20d937374b65ed5d4ff9ad2e2e6a3830d67fdf`.
No original snapshot, producer result, quality record or consumer commit was
changed. The report is diagnostic evidence, not a persisted SDK quality verdict.

The [versioned observed repair](sec-compound-unit-repair.md) addresses these
eight fields in parser v2 while preserving the v1 path for historical rebuilds.
The network-denied follow-up produced v2 at 2026-09-11T13:23:32.650128Z and
compared every row field against retained v1. Exactly eight `unit` fields changed
to a divide with USD numerator and shares denominator; their derived `currency`
became `None`. All other row fields and all 172 simple units stayed equal.
Independent XML unit inspection confirmed the numerator and denominator without
using the SDK unit decoder as its oracle. Reproducing explicit v1 retained its
original output hash and production identity. A mixed v1/v2 bundle then loaded
with SnapshotStore writes disabled, preserving both production identities.

The ignored `run_compound_unit_repair_probe.py` and timestamped report under
`compound-unit-repair/20260911T132332650128Z/` bind this follow-up. Both productions
still have zero quality records and zero consumer commits. A corrected production
requires new caller quality/commit evidence; v1 attestations cannot transfer.
Legacy live-provider, SGML and declared-availability package paths remain affected
by the compound-unit defect and are outside this observed-only repair. Any
consumer using those paths or old unit assumptions requires a separate impact
assessment and rerun before corrected downstream comparisons can be claimed.
Accounting relationships, period bridges, visible-report comparison, publication
evidence and the broader P1 gate remain unverified; this filing receives no PASS.

The [production plan](pit-data-production-and-event-refresh.md) owns the pilot
limits; the [package contract](sec-xbrl-package-financial-production.md) owns
the exact availability and provenance requirements.

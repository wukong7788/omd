# SEC financial source qualification: first probe

Date: 2026-09-11. Status: parsing probe passed; complete PIT qualification remains
open. This is a bounded first-filing probe, not acceptance of the eight-symbol
pilot or an independent financial correctness check.

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

Next: establish auditable source-availability evidence for the separately
extracted package before attempting complete package/PIT production. If that
evidence cannot be established, retain the explicit qualification gap. Do not
mark the P1 real-source gate complete or expand to the remaining pilot filings
on the strength of parsing success alone.

The [production plan](pit-data-production-and-event-refresh.md) owns the pilot
limits; the [package contract](sec-xbrl-package-financial-production.md) owns
the exact availability and provenance requirements.

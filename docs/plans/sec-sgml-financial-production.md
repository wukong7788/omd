# Offline SEC SGML financial production v1

This slice connects retained original full-submission bytes to the existing SEC
typed-row projection and normalized versions. It supports an explicit traditional
XBRL subset, not every SEC filing. The current manual projection producer and
bundle/structural-quality consumers are the concrete callers. Existing live
financial fetching, N-PORT artifacts, PIT selectors and bundle formats stay unchanged.

## Interface and retained input

Add `SecSgmlFinancialsRequest` with explicit symbol (display alias only), CIK,
accession_number, form, statement_types and include_dimensions. CIK is canonical
10-digit text; accession is canonical dashed SEC accession text. Supported forms
are 10-K, 10-Q and their /A variants. Statement types are a nonempty unique tuple
from balance_sheet, income_statement and cash_flow. Dimensions are an explicit
bool, with no silent inclusion/exclusion policy. No identity/universe lookup occurs.

`produce_sec_financials_from_sgml(*, source_store, source_observation,
projection_store, request, produced_at, max_raw_bytes=8*1024*1024,
max_rows=10_000)` returns a frozen `SecSgmlFinancialProduction` containing request,
source_observation, projection_observation, vintage and normalized versions.
The source is an ordinary SnapshotStore observation: provider sec, endpoint
company-filing-sgml, serialization sec-filing-sgml-v1, with RequestSpec parameters
exactly cik/accession_number/form matching the request. No URL, path or credential
is encoded in the request identity; RequestSpec.fields is empty. Callers retain exact downloaded bytes before
calling this offline API. The builder does not fetch or authenticate their origin.

Replay raw bytes once with max_payload_bytes. Retain the raw fact_version as
the existing projection's source_artifact_identity. Strict UTF-8 decoding is
the only supported encoding; no lossy replacement. The immutable raw snapshot
remains untouched. No caller-authored vintage, row, acceptance timestamp or
extractor can bypass the built-in parser.

## Header and time binding

Require exactly one complete SEC-HEADER before document bodies. Read the single
accession, filing form, filer CIK, filed date, period of report and compact
14-digit ACCEPTANCE-DATETIME from that header, and reject missing, duplicated,
conflicting or malformed required fields. Support the conventional text SEC
header layout only; tagged SUBMISSION alternatives fail explicitly. Identity
fields must match the requested CIK/accession/form. Never scan document bodies
for substitute header metadata.

The filer name accepts exactly one one-line company-name header: the current
`COMPANY CONFORMED NAME` label or the legacy `CONFORMED NAME` label. Empty,
duplicate, simultaneous current-and-legacy values, or a name value starting on
a following line fail. Historical `FORMER COMPANY` and `FORMER CONFORMED NAME`
entries are not substitutes for the filer name and do not alter the selected
company name.

Acceptance wall time is interpreted in America/New_York, as in the existing
financial adapter, and converted to UTC. Reject nonexistent and ambiguous DST
times. The N-PORT compact timestamp parser has a different UTC contract and must
not be reused. The v1 implementation writes acceptance to source_available_at;
this is an acceptance proxy, not proof of exact public availability. The
[availability evidence decision](sec-availability-evidence-decision.md) supersedes
the original exact-publication claim and records the pending query restriction.
The current API has no lag, date-only fallback or caller override.
Require acceptance <= raw observation fetched_at
<= produced_at, with an injected aware produced_at normalized to UTC. The
projection observation and normalized recorded_at use produced_at. This binds
the local byte-derived statement; it does not prove actual consumer publication.

## Offline parser and coverage

Use the pinned edgartools 5.56.0 extra and existing parse_statement_rows. Invoke
only local FilingSGML.from_text and XBRL parser content methods; never from_filing,
from_source, filing.obj(), entity/homepage or fallback downloads. Require exactly
one embedded EX-101.SCH, EX-101.PRE, EX-101.LAB and EX-101.INS component. Optional
EX-101.CAL/DEF are parsed when present; duplicates fail. Inline-only, external-only
or incomplete XBRL packages fail with explicit unsupported/missing errors.

For only those known embedded EX-101 components, the extractor accepts one exact
SGML `<XBRL>…</XBRL>` outer wrapper and passes its nonempty inner text to the
existing XML checks and parser. Bare XML remains unchanged. Missing or repeated
wrapper tags, or non-whitespace content outside the outer wrapper, fail; the
retained SGML snapshot bytes are never rewritten.

Validate bounded document structure and XML before passing it to the permissive
library parser: at most 64 documents; no DOCTYPE/entity declarations, malformed
XML, excessive depth (128) or excessive aggregate XML elements (200,000).
No external schemas or references are fetched. Other embedded documents may be
retained in the raw snapshot but do not widen financial extraction coverage.
Every instance context must have one entity identifier using the SEC CIK scheme
`http://www.sec.gov/CIK`; its trimmed numeric value, padded to 10 digits, must
match the header filer CIK. Any DEI EntityCentralIndexKey facts, when present,
must agree too. Unknown schemes, conflicting/missing context identifiers or
multi-entity instances fail explicitly; they are not merged under the filer.
Require every requested statement to yield nonempty native rows; missing or
failed requested statements reject the entire build. Preserve existing Decimal,
unit, native precision, context, dimension and period semantics without new
financial normalization. No automatic quality PASS or correction is generated.

Fixed version identities describe this parser/adapter and identity normalization;
configuration identity binds requested statements and dimension policy. Derive
vintage metadata from the header (symbol alone is caller-supplied). Serialize the
existing canonical projection, cap it at 8 MiB before publication, then construct
normalized versions through the existing verified projection factory without
replaying/decoding the same projection per row. Fail before writing a projection
if header, parser, coverage, time or resource validation fails. Same inputs and
produced_at reproduce exact identities; a new parser version may produce a new
projection from the same retained raw bytes.

Limits are positive exact integers and stricter-only. Count rows before building
unbounded output. This is a bounded single-filing call, not a background service
or a hard interruptible parser deadline. Keep public modules focused below about
500 lines by separating header/XML parsing from orchestration when necessary.

## Acceptance and limits of the claim

Use a synthetic full SGML fixture that the real installed parser converts to a
native statement (do not replace the parser with a mock). Deny network access
during this path. Verify raw/header/row/projection/version identity and acceptance
offsets, immutable restart/rebuild, and structural findings plus bundle replay.
Negative cases include corrupt raw snapshot, wrong request/header identity,
duplicate/missing/malformed header fields, winter/summer and DST edge times,
missing/duplicate components, malformed/unsafe XML, unsupported encoding,
statement coverage failure and resource limits. Invalid builds leave existing
projections readable. Run relevant existing SEC/PIT regressions and static checks;
inspect package artifacts. Independent Astra review must pass before commit.

Retain both raw and projection observations. Existing bundle replay still
verifies its typed projection only; it does not re-run this parser or verify the
raw chain automatically. Re-run this builder with retained raw and identical
request/produced_at to reconstruct that chain. No live filing qualification,
inline-XBRL support, consumer migration or full P1 source gate is claimed here.

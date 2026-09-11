# SEC document-source observed financial production

Status: source closure and strict restoration ACCEPTED; financial production remains pending.

This path addresses full-submission SGML resource failures without truncating
source data or increasing existing source admission limits. It is separate from
SGML observed production. No existing schema, parser identity or receipt is
reinterpreted. Scope is one explicit 10-K/10-Q (including amendments), selected
by a caller from one retained submissions root's recent records. No discovery
history closure or first-publication claim follows from that selection.

## Required source closure

Retain one submissions root, one filing directory index, the exact primary
document named by the selected submissions row, and four required XBRL components
(schema, presentation, labels, instance), plus explicitly selected calculation
and definition components when present. Each component is a separate original
observation. Source selection validates exactly one accession row and its CIK,
form, filingDate, reportDate and timezone-aware acceptanceDateTime. Company name
comes from the same root. Missing metadata fails explicitly.

The filing index must name `/Archives/edgar/data/{integer_cik}/{compact_accession}`.
All selected document basenames must be unique, safe basenames and occur exactly
once in that index. The primary basename must equal submissions metadata.
Required component selection is an explicit caller role-to-basename map, checked
against instance schemaRef and schema linkbaseRef relationships. Cross-directory
references, duplicate role selections, missing links and conflicting declarations
fail. Taxonomy imports are not silently fetched. Selected XML components retain
the pinned parser's current supported coverage; no inline-only fallback is inferred.

Every observation is replayed against an exact SEC RequestSpec with CIK,
accession and filename as applicable, endpoint and serialization version.
Do not use manifest paths or user-provided URLs for arbitrary I/O. Source URLs
are derived from validated identities. JSON rejects duplicate keys, non-finite
values and over-complex structures before constructing unrestricted graphs.
All XML preserves current DTD/entity rejection, element/depth and unit checks.

## Identities, time and output

Use a new `sec-document-source-package-v1` manifest of typed observation receipts;
never populate fake SGML fact/observation fields. Its sealed source closure binds
all exact receipts, selected filing metadata, role mapping and package observation.
`known_by_at` is the maximum observation timestamp of all required retained
sources and the package. Each source observation and package must precede or
match `produced_at`; acceptance metadata must not postdate the submissions
observation. Acceptance is not first market publication. No MARKET_KNOWN evidence
or financial quality PASS is manufactured.

New financial production has a distinct parser/configuration/output/identity
domain and a separate sealed factory. Reuse native row extraction and raw unit
validation through an explicit component-only internal path; do not synthesize
SGML to invoke the old source validator. Old SGML parsing remains unchanged.
Restore replays every source receipt, reconstructs selection and rows, and compares
canonical output bytes and identities before exposing the result. Bundle support
uses an explicit schema discriminator; existing observed bundles do not accept
this production by shape or duck typing. Quality and consumer integration remain
explicit dependent work until their new-type validation is implemented.

## Bounded rollout and acceptance

One package contains 7–9 source observations: submissions, index, primary and
4–6 components. Submissions/index are each at most 2 MiB, primary at most 4 MiB,
each XML component at most 2 MiB, all unique source bytes at most 16 MiB.
Package receipt envelope is at most 256 KiB; normalized output at most 8 MiB.
Maximum 10,000 filing rows/index entries, 200,000 XML elements across all components combined, XML depth 128,
and 10,000 selected financial rows. Limits are positive exact integers and may
only be stricter. Remaining aggregate limits are passed before dependency reads.
No unbounded HTTP/JSON/XML reads or silent partial closure are permitted.

First implement and review source closure independently, then financial production
and complete restart support. Offline tests cover missing/duplicate/cross-filing
sources, invalid links, malformed metadata, tampering, source/package/production
time ordering, immutable conflict and default upper limits. Old v1/v2 golden
identities must remain unchanged. Full cold build plus write/restore uses actual
pinned parser execution with networking disabled, under 512 MiB RSS and 60 seconds.

Only after complete offline acceptance freeze a new one-filing live protocol:
serial SEC queries, one attempt per URL, 60 seconds per filing, 20 minutes overall,
no automatic retry, stop at any missing source or resource failure. A single
success is evidence for that filing only. Existing failed pilots stay failed;
no universe expansion, financial PASS, consumer cutover or publication is implied.

## Exact source-closure v1 encoding and reference rules

| Role | Endpoint | Exact parameters | Serialization |
| --- | --- | --- | --- |
| submissions | edgar_submissions | cik (10 digits) | sec-submissions-json-v1 |
| index | company-filing-directory | cik, accession_number | sec-filing-directory-json-v1 |
| primary and every component | company-filing-document | cik, accession_number, filename | sec-filing-document-bytes-v1 |
| produced package | company-filing-document-source-package | cik, accession_number, form | sec-document-source-package-v1 |

All requests have no requested fields. No alternate endpoints, parameter aliases,
extra request parameters or serialization identifiers are accepted in v1.
Basenames contain only ASCII letters, digits, underscore, hyphen and dot, begin
with a letter/digit, are at most 255 characters and may not contain `..`.
References are exactly one such basename: no directory segments, fragment,
percent encoding, query, scheme or absolute URL. Required targets must equal
the selected names byte-for-byte, including case.

Primary supports UTF-8 HTML/inline XHTML, using Python HTMLParser only to inspect
schemaRef declarations, not to attest complete browser rendering or HTML validity.
Only the explicit `link:schemaRef` tag with `xlink:href` is supported, and matching
`xmlns:link="http://www.xbrl.org/2003/linkbase"` and
`xmlns:xlink="http://www.w3.org/1999/xlink"` namespace bindings must be present
on that tag or an active ancestor. Count all local-name schemaRef occurrences;
exactly one must exist. Unsupported prefixes, conflicting bindings, duplicate
attributes, malformed nesting encountered by this extractor, missing or duplicate
schemaRef fail. HTML void elements do not add a nesting level. Element count is
bounded by 200,000 and nesting by 128. This deliberately limited extraction is
not an implicit fallback parser. It cannot fetch external references.

XML instance must contain exactly one `{http://www.xbrl.org/2003/linkbase}schemaRef`
with `{http://www.w3.org/1999/xlink}href` equal to the primary's selected schema.
All instance context identifiers and DEI CIK facts must pass the established
issuer identity validation. Selected schema's linkbaseRef declarations must
contain exactly one each for labels/presentation, optionally exactly one each
for calculation/definition. Full standard role URIs are the respective
`http://www.xbrl.org/2003/role/{label,presentation,calculation,definition}LinkbaseRef`.
Unknown roles, duplicate declarations (even identical), undeclared selected
optional components and declared-but-omitted components fail. Each selected
href must equal its caller-mapped basename. Taxonomy schema imports are retained
but not fetched; they do not substitute for filing-specific component links.

All source timestamps must be at least the selected acceptance timestamp;
package captured_at must be at least every source timestamp. Thus neither an
old directory nor a future acceptance declaration can authorize source closure.
This is still observed known-by, not proof of first public availability.

Canonical manifest top-level fields are exactly `schema`, `request`, `filing`,
`captured_at`, `sources`. request contains exactly `cik`, `accession_number`,
`form`. filing contains exactly `company_name`, `filing_date`, `period_end`,
`accepted_at`, `primary_document`. sources is a role-sorted list, each object
containing exactly `role`, `filename`, `receipt`; metadata role filenames are
null. Receipt fields are observation_identity, snapshot_identity, fact_version,
mode, provider, endpoint, request_identity, response_sha256,
serialization_identifier, snapshot_fetched_at, with the existing strict receipt
codec. UTC timestamps use canonical ISO format ending Z; dates use YYYY-MM-DD.
Serialize UTF-8 with sorted keys and separators comma/colon, default JSON ASCII
escaping, no NaN and no additional whitespace. Unknown or duplicated keys fail.
The source package identity is SHA-256 of these exact canonical bytes, whose
schema provides domain separation. Factory validation and restoration invoke the
same source rebuild function; restoration compares exact manifest bytes and
replayed output receipt, never trusts decoded claims alone. Sealing includes the
manifest observation so changed dataclass fields/receipts cannot retain validity.

Source closure acceptance only validates this provenance graph. Financial parsing,
full financial-bundle restoration, financial quality and consumer publication
remain separately gated dependent work.

Review clarification: v1 rejects any primary `base` element with an `href`
attribute, primary `xml:base` attribute, and any XML component `xml:base`
attribute. Thus an inherited base URI cannot redirect a basename outside the
filing. Namespace scope retains only the two supported link/xlink declarations,
so unrelated namespace width cannot multiply across the 128-level stack.

## Source-closure acceptance (2026-09-12)

Independent Astra review accepted the implemented closure and restoration after
fixing namespace-scope amplification and base URI overrides. The combined
173-test suite passes, including 44 new source-closure regressions; global
Ruff, formatting, type checks and build pass. All four public exports and built
wheel/sdist source bytes were inspected; package version remains 0.2.5.

Two isolated offline probes retained the complete 16 MiB maximum source set,
produced its package and restored it three times with identical manifest identity:
normal maximum input used 255,770,624 bytes peak RSS in 0.902 seconds; the primary
with 100,000 irrelevant namespace declarations and 120 nested elements used
476,037,120 bytes in 1.230 seconds. Reports are under ignored
`artifacts/sec-document-source-acceptance/`. These probes validate source closure
only, not financial parsing, complete financial replay, live data or quality PASS.


Financial-path resource hardening (2026-09-12): primary HTML elements now admit
at most 256 attributes, including namespace declarations, for ordinary and
self-closing tags. This narrows the earlier source-v1 admission range; previously
accepted wider elements now fail explicitly. Identities of still-admitted inputs
remain unchanged. The count check precedes dict/scope construction, but HTMLParser
has already tokenized the attributes; failure-path RSS must also be measured.

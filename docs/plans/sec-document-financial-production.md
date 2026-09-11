# Financial production from a retained SEC document source package

Status: synthetic financial production and full source/parser restoration ACCEPTED.

One explicit SecSgmlFinancialsRequest selects symbol, CIK, accession, form,
statement types and dimension policy. Reuse the request as a selection value
only; it does not imply SGML exists. Restore the exact source package and every
source observation through the accepted document-source closure first. Retain
all source admission/reference/metadata/time checks. Then parse the selected
original XML components with pinned edgartools 5.56.0 and the existing native row
extractor. The internal component-only entry point omits only FilingSGML.from_text;
no synthesized/truncated SGML is used and old SGML entry points keep their checks.
Raw instance units are mandatory using the established compound-unit decoder.
All selected row unit_refs must resolve; no fallback to opaque unit IDs.

Output uses `sec-document-financial-rows-v1`, endpoint
`financial-document-observed-rows` with CIK/accession/form parameters.
Parser domain is `sec-document-xbrl-financial-parser-v1-edgartools-5.56.0`;
configuration domain is `sec-document-financial-config-v1`, binding statement
selection, dimension policy and parser version. Existing parser/config/output
identities are unchanged. New sealed SecDocumentFinancialProduction includes
request, the sealed source package, output observation, native observed vintage
and produced_at. Canonical JSON binds schema, complete request, parser/config
identities, exact source-package receipt, selected filing metadata, known_by_at,
produced_at and all ordered native rows. Production identity binds the new domain,
canonical output hash and output observation identity. Private nested seals are
revalidated, including all receipt fields and local path binding; path strings
never enter public manifest identities or output bytes.

known_by_at equals source package observation time; closure already ensures all
source timestamps are earlier or equal. It must not exceed produced_at. Acceptance
is retained submissions metadata and never fills first-market-availability fields.
No financial PASS, quality policy, consumer commit or old observed-production type
is fabricated. The shared native observed vintage is a row value, not proof that
the new production is compatible with old lifecycle selectors.

Production replays source package/dependencies before writing its single output.
Result bytes are at most 8 MiB, rows at most 10,000, XML components/elements/depth
remain bounded by source closure. Restore replays an output observation, validates
its exact endpoint/request/schema and bounded strict JSON, resolves and restores
the source closure, reruns the same build, and compares exact output bytes before
returning a sealed result. It never writes. All receipts claimed in output must
match actual resolved receipts before use. Unknown fields, duplicate JSON keys,
noncanonical bytes, future times and tampering fail explicitly.

Acceptance requires actual parser tests for requested coverage, unit/period/
dimension parity against the existing synthetic source, missing unit or statement
failure without output, nested seal mutation, malformed/redirected source graph,
old parser v1/v2 golden identity parity and cold/restored equality. A complete
maximum-source production plus repeated restoration probe must remain below
512 MiB RSS and 60 seconds with no network. Financial bundle/lifecycle integration
and live sampling remain separate dependent acceptance gates.

Exact binding clarification: production request CIK/accession/form must match
reconstructed source metadata exactly. Symbol is only a caller label and never
replaces company name or source filing identity. Output RequestSpec has provider
sec, endpoint financial-document-observed-rows, parameters exactly cik,
accession_number, form, and empty fields. Canonical output fields are exactly
schema, request, parser_version, configuration_version, configuration_identity,
source_package_receipt, filing, known_by_at, produced_at, rows. Request contains
symbol, cik, accession_number, form, statement_types (ordered list),
include_dimensions (boolean). Filing is exactly the validated source package's
filing object. Rows preserve the shared native row serializer order. JSON uses
sorted keys, comma/colon separators, default ASCII escaping and no NaN.
Configuration identity hashes schema/configuration version, parser version,
ordered statement_types and include_dimensions; full request is independently
bound in output bytes, so symbol/filing changes always change output identity.
Source package observation <= output observation = produced_at. Restore checks
these against actual replayed observations before relying on decoded times.
The shared component parser retains all XML/CIK/requested-statement/empty-result/
row-count checks and omits only the SGML-specific validation.

Resource clarification: the source closure now limits every HTML element to
256 attributes (namespace declarations included), before constructing attribute
maps/scopes. The former 100,000-attribute input must fail without output, with
its tokenizer-path RSS still measured. Financial acceptance uses the complete
16 MiB source maximum plus maximum legal attribute/depth cases. Failed prior
564,281,344-byte and unsuccessful lazy-decoding 589,316,096-byte probes remain
failed. The lazy-decoding experiment was withdrawn; no budget increase occurred.


## Acceptance (2026-09-12)

Independent Astra review accepted the source/request/time bindings, shared parser
path and copy-based nested seal checks. The affected combination passes 212 tests,
including 32 financial production tests and 51 source-closure tests. Global Ruff,
type checks and the 0.2.5 build pass; documentation examples are format-checked.
Golden tests retain all previous v1/v2 identities. New cold/restored rows agree
with the prior actual parser, including compound units and explicit dimensions.

Actual-parser production and three full restorations of the 16 MiB maximum
source set use 310,083,584 bytes peak RSS in 1.310 seconds. The maximum legal
primary attribute/depth case (256 attributes, reference at depth 128) uses
310,771,712 bytes in 1.358 seconds, with equal production identity on every
restore. The former 100,000-attribute input is rejected without package/financial
output at 282,181,632 bytes in 0.305 seconds. Reports are retained under ignored
`artifacts/sec-document-source-acceptance/`; earlier over-budget runs stay failed.

These are synthetic production and strict restoration results. Financial quality,
financial-bundle lifecycle, live filing qualification and consumer publication
are not accepted by this evidence.

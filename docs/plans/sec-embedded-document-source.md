# SEC embedded document source package

`sec-document-embedded-source-package-v1` is an experimental, sealed source
closure for a single SEC filing whose XBRL linkbases are embedded in the filing
schema. It is not a public API or an accepted financial-production path. The
offline financial pipeline gate has passed; real complete-filing acceptance remains pending.

The package records exactly five distinct raw `SecDocumentSource` observations:
submissions, filing-directory index, primary HTML, schema, and instance. The
manifest uses endpoint `company-filing-embedded-document-source-package`,
contains the exact source receipts, and fixes
`view_policy` to `sec-schema-embedded-linkbase-view-v1`. It retains original
source bytes and does not synthesize standalone presentation, labels,
calculation, or definition documents.

The submissions record supplies the accepted timestamp. The package observation
is known no earlier than every retained raw observation; it does not make a
market-time or point-in-time availability claim. Restore resolves every receipt,
replays and revalidates the complete raw graph, and requires byte-for-byte
manifest reconstruction without writing or repairing a package in place.

The source closure permits at most 8 MiB for primary HTML, 12 MiB for the XBRL
instance, and 2 MiB each for submissions, index, and schema. The five raw
payloads total at most 24 MiB and the canonical manifest at most 256 KiB.
Primary HTML and both XML documents must be UTF-8, reject DTDs, entities, and
base-URI overrides, and are bounded by an aggregate 200,000 HTML/XML nodes and
depth 128. Primary HTML also permits no more than 256 attributes on an element.

The primary and instance each bind exactly one schema reference to the selected
schema filename; instance CIK identity must match the filing. The selected three
document filenames and all five observation identities are distinct. The schema
must have an `xs:schema` root, exactly one `xs:appinfo`, and exactly one direct
embedded `link:linkbase`. That linkbase has direct `link:labelLink` and
`link:presentationLink` children; embedded calculation and definition links
remain in the original schema bytes. These supported link types may appear only
as direct children of that single container. Any `link:linkbaseRef` is rejected because
mixed external filing linkbases are unsupported. Taxonomy imports and locators
are retained as raw filing content and are never fetched.

The installed parser serializes an embedded linkbase with a literal `link:`
closing tag. The closure therefore validates the lexical namespace prefix of
the actual embedded linkbase with namespace-aware parsing: it must be the
standard linkbase namespace using prefix `link`. Alternate or default prefixes
fail closed; comments and unused namespace declarations do not satisfy this
predicate.

Offline implementation acceptance: independent review found no required defects.
Actual production, bundle write and three read-only restores passed for a
25,105,832-byte embedded dependency closure and a 33,435,672-byte mixed closure.
Additional 10,000/20,000-fact synthetic shapes passed; worst observed peak RSS
was 510,623,744 bytes and longest elapsed time 2.785 seconds. These are bounded
probe results, not guarantees for every source shape or financial quality PASS.
Evidence is retained under `artifacts/sec-embedded-pipeline-acceptance/`.
The pre-change legacy producer reproduced exactly the same output bytes and
production identity. Real complete-filing acceptance remains separate.

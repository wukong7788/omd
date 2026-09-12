# Experimental embedded document financials

`produce_sec_financials_from_embedded_document_source` produces observed
financial rows from exactly one sealed
`sec-document-embedded-source-package-v1` closure.  It is experimental and
does not change the legacy document-source producer or its byte identity.

The closure retains only submissions, filing index, primary document, intact
schema, and instance receipts.  The schema must contain one direct embedded
`link:linkbase` in `xs:appinfo`; external filing linkbase references are not
accepted.  The parser identity is
`sec-schema-embedded-financial-parser-v1-edgartools-5.56.0`.  It parses the
schema once, allowing the pinned parser to extract its embedded linkbases, and
parses the instance once.  It does not pass the schema again as a presentation
or label document.

Embedded output uses the separate tuple:

- output schema `sec-embedded-document-financial-rows-v1`;
- endpoint `financial-embedded-document-observed-rows`;
- configuration `sec-embedded-document-financial-config-v1`;
- source endpoint `company-filing-embedded-document-source-package`; and
- source view policy `sec-schema-embedded-linkbase-view-v1`.

Restore selects only the two sealed source/output domains and verifies the
complete schema, parser, configuration, endpoint, and source serialization
tuple before rebuilding any rows.  A legacy output cannot be paired with an
embedded source, and an embedded output cannot be paired with a legacy source.
Raw source retention remains bounded: schema is at most 2 MiB, instance at
most 12 MiB, primary at most 8 MiB, and the complete embedded closure at most
24 MiB.  Financial output remains capped at 8 MiB and 10,000 rows.  Bundle
dependency accounting remains capped at 32 MiB; only a verified SEC filing
document receipt may receive the 12 MiB admission cap, after which source-role
validation applies its stricter role-specific limit.

The result records source observation time as `known_by_at`.  It does not make
a market-availability assertion, provide a market-time projection, or approve
financial quality.

Offline implementation acceptance: independent review found no required defects.
Actual production, bundle write and three read-only restores passed for a
25,105,832-byte embedded dependency closure and a 33,435,672-byte mixed closure.
Additional 10,000/20,000-fact synthetic shapes passed; worst observed peak RSS
was 510,623,744 bytes and longest elapsed time 2.785 seconds. These are bounded
probe results, not guarantees for every source shape or financial quality PASS.
Evidence is retained under `artifacts/sec-embedded-pipeline-acceptance/`.
The pre-change legacy producer reproduced exactly the same output bytes and
production identity. Real complete-filing acceptance remains separate.

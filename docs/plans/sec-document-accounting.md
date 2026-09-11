# SEC document accounting diagnostics

`evaluate_sec_document_accounting(production, rules, *, detected_at, recorded_at,
max_rules=64, max_rows=10000)` evaluates declared accounting equalities against exactly
one sealed `SecDocumentFinancialProduction`. It performs no I/O and creates no quality
finding, PASS, availability assertion, normalized ID, consumer commit, or financial
approval. A `MATCH` remains only an arithmetic diagnostic.

The entry point accepts the document-production type exactly. Before rule evaluation it
limits accounting input to 10,000 rows, charges the retained production through the
document replay admission budget (32 MiB, bounded nodes and depth), runs the existing
structural row validator on every row, and copies then revalidates the document seal.
The recomputed production identity must equal the caller value. Tampered nested source,
vintage, output receipt, production identity, malformed row, or future detection fails;
the caller object is never repaired. `detected_at` is no earlier than both production and
output observation time, and `recorded_at` is no earlier than detection.

Rules, statuses, tolerance policies, row selection, duplicate handling, precision
allowances, SAME_CONTEXT and CASH_ROLLFORWARD checks, evidence limits, and report-wire
limits are the exact shared observed-accounting implementation. No new accounting rule
or availability interpretation is introduced here. Missing values and absent rows remain
`MISSING`; unit, context, period, duplicate, and precision conflicts remain
`INCOMPARABLE`; missing data is never converted to zero.

The result intentionally reuses `SecObservedAccountingReport`. Its identity binds only
the supplied document production identity, its exact output observation and fact-version
identities, parser/configuration identities, input row count, diagnostic times, and
checks. This reuse does not admit `SecObservedFinancialProduction`, invoke the old
producer factory, or claim that the two production domains are interchangeable.

Acceptance: independent Astra review accepted the implementation after 34 combined
accounting tests passed. A separately loaded pre-change HEAD implementation generated
the same legacy report bytes and identity as the refactored implementation; the local
evidence is `artifacts/sec-document-accounting-acceptance/report.json`. Actual parsed
synthetic document rows cover MATCH, MISMATCH, missing terms, and incompatible context,
unit, and precision diagnostics. Real-source and consumer acceptance remain separate.

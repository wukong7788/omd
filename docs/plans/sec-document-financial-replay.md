# Document financial known-by replay

Status: ACCEPTED — bounded in-memory selector, 2026-09-12.

Add a separate in-memory selector and result for sealed document productions.
Reuse existing observed quality, consumer commit and explicit replay policy
values, bound only to exact production identities. No fake SGML evidence,
MARKET_KNOWN mode, automatic PASS or implicit consumer publication.

The legacy public selector keeps its exact production type admission. Both
selectors share only the already-validated history/selection algorithm: exact
schema/parser/configuration/policy/dataset, known-by <= production <= cutoff,
chronological quality supersession and a visible commit to the latest PASS.
Future quality does not alter earlier cutoffs; missing approval/commit stays
unavailable. Ordering and conflicts remain identical to the old policy.

New admission allows at most 100 productions, 100,000 rows, 10,000 quality
records and 10,000 commits. Duplicates count before deduplication. A conservative
32MiB serialization-size budget and 500,000 visited nodes are checked before
copying, hashing or serializing caller values; JSON text is budgeted at twelve
bytes per character plus structural overhead. Oversized inputs fail explicitly.
Decimal coefficient storage is limited to 8KiB, adjusted exponent to ±10,000
and integers to 64 bits. These are explicit new-selector admission constraints.
This bounds admission, not a claim of hard OS memory isolation.

Revalidate disposable copies and compare stored identities without repairing
caller objects. Required tests include old-selector identity/result regression,
future isolation, revocation/reapproval, missing commit, wrong binding, ties,
bounded iterables and oversized/tampered values before hashing.

Bundle persistence and real financial qualification remain separate work.
Independent Astra accepted these design boundaries before implementation.

## Acceptance evidence

84 related tests passed, including 20 new selector cases. Global Ruff, formatting
and type checks passed; documentation snippets and local links were checked.
Independent Astra review accepted the fixed 12-byte Unicode admission accounting.
The five-cutoff exact production/quality/commit identity comparison against
`fa8dfa7` is retained in `artifacts/sec-document-replay-acceptance/report.json`.
A 100-occurrence repeated synthetic production query selected one result in
0.0228 seconds with process lifetime peak RSS 178,552,832 bytes. This repeated
small-input probe is not a heterogeneous maximum-batch memory proof.
No consumer migration is required; existing selector evidence remains unchanged.

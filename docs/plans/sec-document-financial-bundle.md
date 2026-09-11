# Document financial immutable bundle

Status: ACCEPTED — synthetic complete restart closure, 2026-09-12.

New `sec-document-financial-bundle-v1` at `document-financial-bundle` uses
SnapshotStore FROZEN atomic immutable writes. A canonical envelope contains
batch/capture, sorted unique production identities plus full output receipts,
and exact caller quality/consumer-commit records. Output receipts transitively
bind the source package and every original observation. Loading strictly
rebuilds that complete closure and actual financial parser without writes,
then compares exact canonical bytes. No market-first-publication or automatic
financial PASS is created. Legacy bundle identities/types remain unchanged.

Limits: 10 productions, 100,000 rows, 10,000 quality records and 10,000 commits,
8MiB envelope, 120 distinct observations and 32MiB total unique dependency bytes.
Dependency bytes include financial outputs, source packages and every original
source; envelope bytes are separate. Each first read receives the smaller of
8MiB and remaining aggregate budget. Recursive restoration uses the same
resolver wrapper; it retains exact receipt/path/verified size, not payloads.
Repeated references retain role-specific validation. Source limits remain
16MiB aggregate, 4MiB primary, 2MiB metadata/XML.

Writer admission applies document selector conservative serialization/node
budgets before copies and hashing. Duplicates count for admission, then only
one complete rebuild per production. Full lifecycle graph validation retains
all-policy causality, supersession and commit rules; capture must follow all
production/quality/commit evidence. Shared graph change is typing-only.

Required evidence: canonical tamper/extra-field/receipt/capture/request rejection,
source tampering even with valid bundle hash, old bundle golden, quality history
and commit binding, atomic idempotence/concurrency/interruption, read-only load,
aggregate resource bounds, and actual parser multi-production write/load near
32MiB under 512MiB/60s. Failed resource experiments require narrower new admission.
Independent Astra accepted these boundaries before implementation.

## Acceptance evidence

116 combined tests and the two subsequently added resolver/generator cases
passed; all 22 document-bundle tests passed together. Global Ruff, formatting,
type checks, README snippets/links and 0.2.5 wheel/sdist source parity passed.
Legacy v1/v2 financial identities and exact mixed-bundle bytes remained unchanged
(SHA-256 `7afc6293800bd2941cc261110e13dcaa5d1707e4b0e7dfe5cb28fc094ec327d0`).
Evidence: `artifacts/sec-document-bundle-acceptance/legacy-golden/report.json`.

Two distinct synthetic source closures totalled 33,436,896 dependency bytes
across 18 observations. Actual financial production, bundle write and three
read-only loads completed in 3.247 seconds with peak RSS 378,503,168 bytes and
identical production identities. Report:
`artifacts/sec-document-bundle-acceptance/multi-source-32mib/report.json`.
This representative resource probe does not claim a hard OS memory guarantee
or prove every possible input shape. Independent Astra review accepted the
scoped implementation. Real filing, financial PASS and consumer migration
remain unaccepted; existing consumer migration is not required by this addition.

# SEC PIT bundle v2: finding and evidence persistence

This slice persists the existing caller-authored `SecQualityFinding` history
inside a receipt bundle. It preserves normalized, quality, commit, finding and
evidence identities and does not infer financial corrections, change PIT query
semantics, or derive PASS/quarantine from a finding. Findings remain independent
annotations on exact normalized versions; quality-policy evaluation is later work.

## Version compatibility

- Existing v1 snapshots remain readable without rewriting or fetching data.
- `write_sec_pit_bundle` with no findings retains exact v1 payload bytes,
  serialization and request identity, including default call compatibility.
- A non-empty `quality_findings` input emits `sec-pit-bundle-v2`, with a matching
  serialization identifier and an additional top-level `quality_findings` array.
  The loader dispatches explicitly on supported schema/serialization pairs and
  rejects mismatches, unknown versions, and unknown/missing fields.
- A loaded `SecPitBundle` exposes `quality_findings` as an immutable tuple;
  v1 yields an empty tuple. Existing positional fields remain compatible.
- A frozen batch identity cannot be reused for different content, including a
  v1-to-v2 conversion. Use a new caller-selected batch identity; retain old roots
  and source snapshots. No in-place migration or SDK package version bump occurs.

## Finding closure and evidence

The v2 codec losslessly encodes every public finding field and nested evidence
observation/fact identity pair, together with the expected finding ID. It does
not serialize private capabilities, absolute paths, raw evidence bytes, or URLs.
The decoder reconstructs exact existing typed objects and recomputes IDs. Same
identity duplicates canonicalize; different content, malformed fields, invalid
enums, naive timestamps, and unsupported values fail explicitly.

Every finding must refer to a normalized version included in the bundle, use
that version's adapter version, and have `recorded_at` no earlier than that
version's production time. The full finding history must contain every
predecessor, with unchanged scope and no forks or independent roots for the
same version/rule-version/rule/key. Existing finding time and status invariants
still apply. No latest-only truncation is allowed when saving history.

The v2 writer additionally requires an injected source `SnapshotStore` and
observation-ID resolver. Before publishing bytes it verifies all normalized
source observations and evidence references through that store. The loader
performs the same verification. A resolver must return the requested observation
ID; store replay validates the reference and payload, and each evidence pair's
fact_version must match. Evidence can refer to another provider's snapshot:
cross-source comparisons are allowed, but this verifies byte identity, not the
claim's truth, accounting comparability, or source-publication time.

Each evidence observation must have been fetched no later than its finding's
`recorded_at`. All finding timestamps and referenced observations must precede
or equal the caller's `captured_at`. Detection/adjudication timestamps can
precede local evidence ingestion, but a stored finding cannot precede the
evidence it records. Capturing a bundle still does not prove actual consumer
publication. Missing, corrupted, wrongly mapped or temporally invalid evidence
rejects the complete write/load; no partial success or silently skipped finding.

## Resource and replay guarantees

The existing stricter-only record and payload bounds apply to v1 and v2.
Finding records and their evidence references count toward the aggregate
10,000-record input budget, including duplicate supplied records before
deduplication. Input consumption must stop at the limit plus one, before
unbounded materialization. Text is bounded before UTF-8/JSON encoding.

Every unique source/evidence observation is resolved and replayed once per
write/load, including when shared by normalized rows and multiple findings.
Each replay retains the explicit payload byte limit. Validation groups finding
chains rather than repeatedly scanning the complete collection for each item.
This is a bounded offline batch, not an event ledger, scheduler or performance
claim for the full historical panel.

## Acceptance

Use synthetic fixtures and actual SnapshotStore operations. Required evidence:
exact v1 golden payload compatibility and v1 read without rewriting; v2
round-trip of OPEN/CONFIRMED/DISMISSED/RETRACTED history and unchanged IDs; same
as-of results before/after restart and before/after adjudication; strict schema,
enum, field, timestamp and ID tampering rejection; wrong/missing/corrupt evidence
and wrong provider mapping; cross-provider evidence identity verification;
adapter/normalized-version/time mismatch; full-chain orphan/fork rejection;
bounded generators and reference budgets; shared observation replay counts;
canonical duplicates/order; frozen v1/v2 conflicts and v2 retry idempotency;
concurrent and interrupted v2 publication preserving old readable batches.
Run affected v1/PIT/finding regression, static checks, and build/archive checks.
Independent review must pass before committing. No live SEC verification,
automatic accounting rules, external publication or consumer cutover is included.

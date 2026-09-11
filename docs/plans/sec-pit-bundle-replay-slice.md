# SEC PIT bundle replay slice

This P1 slice persists a caller-selected, immutable closure of existing SEC
normalized PIT versions and their quality/consumer-commit evidence. It does not
fetch SEC data, create a raw-data store, select a latest version, merge
accessions, calculate metrics, or run an event loop.

This document owns the v1 contract. The
[v2 finding/evidence extension](sec-pit-bundle-findings-v2.md) retains v1 reads
and exact v1 writes when no findings are supplied; it uses a separate explicit
schema for non-empty finding histories.

## Stored object and write contract

`SecPitBundle` contains an explicit `batch_identity`, normalized-version
receipts, every referenced source-observation ID, quality records, and consumer
commits. It contains no source bytes, absolute path, private factory capability,
or `SecCompanyFinancialVintage` object. Its v1 JSON codec has a fixed field
schema, rejects duplicate JSON keys, unknown/missing fields, non-finite numbers,
unsupported schema/serialization, invalid UTC timestamps, and invalid Decimal
encodings. Lists are canonicalized by identity so equivalent duplicate or
unordered caller input writes identical bytes.

The writer verifies the in-memory closure before calling
`SnapshotStore.write(..., mode=FROZEN)`. The request identity is derived from
the caller's explicit batch identity; same bytes are idempotent and different
bytes for that batch fail with the existing frozen conflict. SnapshotStore is
the only persistence/atomic-publication mechanism in this slice.

Callers provide a timezone-aware `captured_at`, which must not precede any
source observation fetch, normalized production record, quality record, or
consumer commit in the bundle. It is a caller capture assertion, not proof that
the bundle was then published to a production consumer. A repeat write of the
same frozen bytes at a later capture time returns the original snapshot and adds
no publication fact. Defaults are bounded: at most 10,000 receipts and 8 MiB
of serialized bundle bytes; callers may inject stricter positive limits. This
slice has no large-scale or 512 MB performance claim.

## Replay contract

Loading accepts the source `SnapshotStore` and an injected resolver from each
stored observation ID to `SnapshotObservationRef`. For every unique ID it checks
that the resolver returns that same ID, then uses `replay_observation`; it never
opens a manifest-supplied path or scans the source store. The resolved replay
must be the exact SEC typed-row projection expected by the existing restricted
factory. That factory reconstructs the row and revalidates the projection's
accession, caller-attested source-artifact identity, declared timestamp, row
ordinal, fact version and observation identity. The result remains a trusted-
caller typed-projection boundary, not validation of original SEC XBRL/SGML.

Because the projection intentionally omits the complete vintage metadata, the
implementation may use a shared restricted internal factory that accepts the
replayed projection, exact row ordinal and stored vintage identity. It must
revalidate that identity from the projection rather than fabricate a vintage or
ask the resolver for one; the existing public vintage factory uses the same
binding logic. Each unique observation is replayed at most once per load.

The loader reconstructs normalized versions rather than trusting serialized
private binding values, and recomputes every content/version, quality and commit
identity. Stored normalized references retain only reconstruction inputs and
expected identities; typed rows and source publication claims are recovered from
the replayed source projection. It verifies the complete supersedes graph (not
only a selected cutoff), PASS/commit causal ordering, and all referenced
sources. A missing,
tampered, cross-source, or causally invalid dependency rejects the whole bundle.
The existing market-known and system-replay selector retains its exact semantics;
the bundle only supplies a durable validated input set.

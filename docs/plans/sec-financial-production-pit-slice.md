# SEC financial production PIT slice

This offline-only slice produces typed SEC financial-row versions from an
immutable `SnapshotStore` observation and selects them under an explicit PIT
mode. It does not fetch SEC data, persist an event ledger, calculate metrics,
or change `SecFinancialsClient`, `accepted_at`, or `lag_days`.

## Source boundary

The bytes captured by `serialize_sec_typed_rows_projection` are canonical
`sec-financial-typed-rows-projection-v1` bytes: an accession, an exact typed
vintage/row projection, caller-attested `source_artifact_identity`, and a
caller-attested exact source-publication timestamp. They are **not** original
SEC XBRL, SGML, or filing bytes. The restricted factory replays those exact
bytes, verifies their internal accession/artifact/timestamp/row binding, and
binds output to the observation's `fact_version` and observation identity. It
cannot verify that the caller's artifact identity or publication assertion
matches original SEC material; that is a trusted-caller boundary.

`SecNormalizedFinancialFactVersion` is factory-only: its internal projection
binding detects direct construction or `dataclasses.replace` changes before a
selector can use the object.

`SOURCE_DECLARED` plus `TIMESTAMP` `AvailabilityEvidence` must be supplied by
the caller and must equal the timestamp embedded in the replayed projection.
`accepted_at` is never automatically treated as that evidence.

## Version and cache identity

`SecNormalizedFinancialFactVersion.content_identity` covers the exact raw
`fact_version`, observation identity, accession, source artifact identity,
typed-row content and ordinal, schema, adapter, normalization, and configuration
identities. `normalized_version_id` additionally includes `recorded_at`.
Implementations may cache only an exact match of those content inputs; a parser,
normalization, configuration, schema, row, or recorded-time change creates a
separate version. Repeated observation does not change an existing raw
`fact_version`.

## Query modes

All queries require `SecPitPolicy`: schema, adapter, normalization,
configuration, quality-policy versions, plus a non-default `quality_cutoff`.
They return every eligible version in stable order; they neither choose latest
nor combine accessions.

- `MARKET_KNOWN` includes only exact source-publication evidence no later than
  `knowledge_cutoff`. It may use a version and quality decision created later,
  provided the caller explicitly supplies their versions and `quality_cutoff`.
  Production and quality records must both exist by that explicit quality
  cutoff; this is research reconstruction, never system replay.
- `SYSTEM_REPLAY` additionally requires that observation, normalized
  `recorded_at`, quality decision and consumer commit all existed by
  `knowledge_cutoff`; its `quality_cutoff` must equal `knowledge_cutoff`. A commit must name the exact normalized version and PASS
  quality-record ID, and cannot predate the observation, production, or PASS.
  Missing evidence fails closed.

Quality records and commits are immutable values. A later `REVOKED` record
supersedes a prior PASS through an explicit ID. Selection uses the unique latest
record at the chosen cutoff; same-time conflicting records fail rather than
depending on input order. A revocation affects system replay only at or after
its recorded time. Chain validation considers only records at that cutoff, so a
future bad record cannot rewrite an earlier replay result.

## Non-goals and storage

The public API accepts caller-injected in-memory iterables and makes no durable
production or consumer-publication claim. Callers that need durable replay own
immutable storage and must retain the referenced snapshot, version, quality and
commit receipts.

The follow-on [bundle replay slice](sec-pit-bundle-replay-slice.md) adds optional
SnapshotStore-backed persistence for these receipts. It retains the same source
evidence and PIT query boundaries; it does not turn a receipt capture into proof
of actual consumer publication or original SEC filing verification.

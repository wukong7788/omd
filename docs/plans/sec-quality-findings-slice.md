# SEC quality findings and adjudication history

This bounded P0/P1 slice adds immutable, caller-authored findings for existing
SEC normalized financial versions. It does not execute accounting checks,
correct financial amounts, infer a source error from a cross-source difference,
or alter existing `SecQualityRecord`, PIT selectors, or bundle v1 bytes.

## Classification and evidence

The explicit issue classes are `PARSER_ERROR`, `SOURCE_DISCLOSURE_SUSPECT`,
`SOURCE_REVISION`, `CROSS_SOURCE_CONFLICT`, and `UNKNOWN`. Missing-data reasons
are separately represented as `FIELD_MISSING`, `SOURCE_NOT_DISCLOSED`,
`PARSE_FAILED`, `CONFLICT_QUARANTINED`, and `PIT_UNPROVEN`; the optional absence
of a missing-data reason does not assert that a value exists or that it passed
quality checks. Callers supply classifications; the library does not guess them.

A finding binds an exact normalized-version ID, affected SEC row field names,
caller-assigned finding key, rule ID/version and adapter version. It contains
a bounded explanatory reason and at least one evidence reference to existing
snapshot observation identity plus fact_version. References are identities,
not raw payloads, paths, URLs or newly invented raw-version IDs. Structural
validation does not prove that an evidence source or a caller's judgment is true.
Callers remain responsible for retaining and verifying the referenced material.

## Immutable history

Each record has a content-derived finding ID, explicit `detected_at` and
`recorded_at`, and a status of `OPEN`, `CONFIRMED`, `DISMISSED` or `RETRACTED`.
All timestamps must be timezone-aware and are stored in UTC. OPEN has no
adjudication timestamp; the other statuses require `adjudicated_at`, with
`detected_at <= adjudicated_at <= recorded_at`. Detection must never follow
recording. RETRACTED must name a preceding record.

A revision uses `supersedes_finding_id`. A chain retains the same normalized
version, finding key, affected fields, rule ID/version and adapter version;
recorded time strictly increases. Different rule versions are separate chains,
chosen explicitly by the caller. Reclassification and explanations may change
only by appending a new record. No source or normalized financial value changes.

The as-of query requires normalized-version ID, rule version and cutoff. It
validates exact record types and visible dependency chains, deduplicates identical
records, rejects orphan references, same-key independent roots and forks, and
returns the latest visible record per rule/finding key in stable order. It
returns dismissed/retracted records too, preserving the explanation. Future
records and future adjudications cannot replace a record at an earlier cutoff.
The input scan is bounded (default 10,000 records; a positive stricter limit may
be supplied), including repeated input records, before materializing a collection.

This query is a quality-history lookup, not a new market-known/system-replay
mode or a claim about source availability. It does not turn a finding into PASS
or automatically quarantine all financial fields. Integrating findings with
quality-policy evaluation and persistent bundle evidence is a later slice;
the existing bundle format remains compatible and unchanged here.

## Acceptance

Offline tests must cover every issue/missing-reason enum, bounded field/reason
and evidence validation, stable IDs for reordered equivalent inputs, exact
version binding, naive/invalid time rejection, initial/revised judgments and
retraction before/after cutoff, missing predecessors, same-time conflicts,
forks, duplicate/order-independent queries, future bad references not changing
past lookup, and stopping an unbounded generator at the configured limit.
Public docs must state that evidence references are caller-authored assertions
and that automatic checks, persistence integration and financial corrections
remain unimplemented. Independent review is required for the time/history
semantics before marking this slice complete.

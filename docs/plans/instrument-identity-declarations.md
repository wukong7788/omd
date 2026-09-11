# Instrument identity declarations

Status: implemented and independently reviewed; bounded offline acceptance passed.

The production plan needs one dated identity boundary for two near-term callers:
security-level metric inputs and issuer-to-security event dependency binding.
Core currently has no issuer/instrument/alias registry. This slice supplies pure,
bounded caller declarations and lookups, not a security master service or
provider verification.

Issuer identity and instrument identity are separate. A CIK belongs to an
issuer; an ADR, another share class, ETF or index cannot be substituted based on
a shared name or ticker. Venue and quote currency belong to the dated provider
alias declaration. Every declaration retains evidence and a UTC recording time.
Queries choose both effective date and knowledge cutoff. Provider, alias and
venue strings are exact, without case folding, suffix repair or latest fallback.

This slice does not calculate ADR ratios, splits, historical membership,
financial quality or market availability. Declaration recording time limits
local knowledge only; it is not source publication or consumer commitment.
No network, storage path, new snapshot format, background service or consumer
configuration access is introduced.

## Frozen v1 contract

- Frozen `IssuerIdentity` declares opaque issuer ID,
  evidence reference and UTC recording time. Frozen `InstrumentIdentity`
  declares security ID, issuer ID, `COMMON_SHARE`/`ADR`/`ETF`/`INDEX`, evidence
  and recording time. Only INDEX may omit issuer.
- Frozen `ProviderInstrumentAlias` declares exact provider/alias/venue,
  currency, instrument ID, required half-open date interval `[from,to)`,
  evidence and recording time. Currency is uppercase three letters; only an
  INDEX alias may omit it. Dates are not timestamps or trading sessions.
- `InstrumentIdentityIndex` validates all references and causal recording
  order. One immutable definition per issuer/security ID; exact duplicates
  dedupe after budget accounting. Any differing definition or overlapping
  interval for one provider/alias/venue fails at construction, including
  conflicts recorded in the future. Adjacent intervals may identify different
  securities. No corrections, revocation or supersession in v1; retain the old
  catalog when constructing a corrected catalog.
  Conflicts raise `IdentityConflictError`; the proposed entire catalog fails,
  not just queries after the conflicting record. SEC callers declare CIK
  separately; core never parses it from an opaque issuer ID.
- `resolve` requires exact provider/alias/venue, effective date and aware
  knowledge cutoff. An indexed binary search locates the date interval; absent
  or future declarations raise `CoverageError`. No latest or cross-venue fallback.
  Frozen result retains issuer, instrument, alias, query fields, catalog and
  result identities, and maximum declaration recording time.
- Explicit schema-tagged canonical JSON hashes bind every named field. Records
  are at most 16,384 bytes. Text is at most 1,024 UTF-8 bytes without control
  characters or surrounding whitespace. Evidence identifiers are opaque public
  references, never credentials or local paths. Maps are read-only, adjacency
  tuples immutable. Build is O(n log n); lookup is O(log matching intervals).
- Aggregate supplied input is at most 10,000 records before deduplication;
  callers may lower the cap. Validate the non-bool integer cap before consuming
  input, and stop after at most one overflow sentinel.
- Explicit consumer examples bind metric security basis to the resolved
  security and configuration evidence to the resolution identity. SEC event
  dependency input adds `INSTRUMENT_IDENTITY` using a stable `binding_identity`
  over the selected issuer/instrument/alias declaration identities; unchanged
  bindings remain stable across query cutoffs and unrelated catalog changes. Neither
  example grants financial eligibility or rewrites CIK automatically.

Tests cover dated ticker reuse, different share classes, ADR isolation, INDEX
nulls, venue separation, temporal/reference failures, conflicts, bounds,
immutable results and deterministic identities. This contract does not claim
full identity qualification of the pilot universe or consumer integration.

## Acceptance

Twenty-three new tests and 347 affected core/SEC tests passed. Independent Astra
review accepted the implementation, explicit metric/dependency integration and
10,000-record synthetic probe. The probe report is retained at
`artifacts/instrument-identity-acceptance-20260912/161053184458/report.json`.
Ruff, formatting, type checks, README runtime example and wheel/sdist source
byte checks passed; package version remains 0.2.5. This is additive functionality
without consumer migration or full production performance claims.

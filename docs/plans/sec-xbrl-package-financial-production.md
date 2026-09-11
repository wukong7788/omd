# Retained SEC XBRL package financial production

The [offline unit repair](sec-legacy-unit-safety.md) adds parser v2 as the default
and explicit v1 reconstruction. Original availability requirements and retained
projection/bundle schemas below remain unchanged.

This slice extends the offline full-SGML financial producer for filings whose
retained full submission does not embed a traditional XBRL instance.  It does
not parse inline XBRL.  A caller must retain one separately captured,
SEC-extracted traditional component package and explicitly attest its source
availability.  The existing traditional embedded-component path is unchanged.

The [availability evidence investigation](sec-availability-evidence-decision.md)
found no qualifying exact publication evidence for the first retained sample.
This API's caller declaration must not be filled from acceptance, fetch time,
or file-modification metadata merely to make production succeed. A separate
known-by production path is pending; the wrapper has not been relaxed.

## Package envelope

`serialize_sec_xbrl_package` produces canonical UTF-8 JSON bytes with schema
`sec-xbrl-package-v1`.  The envelope contains exactly a filing binding, one
`SOURCE_DECLARED` timestamp, and original component bytes encoded as canonical
base64 strings.  Its filing binding contains the full-SGML observation's
`fact_version` and `observation_identity`, plus the canonical CIK, accession
and form.  The components are exactly schema, presentation, labels and
instance; calculation and definition are optional.  The base64 payload is an
encoding, not a transformed fact representation.

The caller records these bytes as one ordinary `SnapshotStore` observation
with provider `sec`, endpoint `company-filing-xbrl-package`, serialization
`sec-xbrl-package-v1`, and RequestSpec parameters exactly `cik`,
`accession_number`, and `form`.  Request fields are empty.  The package has an
8 MiB encoded-byte limit and each decoded component has a 2 MiB limit.  The
decoder rejects duplicate JSON keys, unknown or missing fields, noncanonical
base64, invalid UTF-8, unsafe or malformed component XML, missing or duplicate
required components, and aggregate component XML exceeding the existing
200,000-element limit.  There is no ZIP extraction, filesystem path, URL,
credential, network operation, or taxonomy download.

The package may reference standard external taxonomy URIs.  The producer never
fetches them and does not claim complete taxonomy closure.  It validates only
the retained components supplied to the local edgartools parser and their
locally verifiable identity/context constraints.

## Availability and production API

`produce_sec_financials_from_sgml` keeps its existing arguments and behavior.
`produce_sec_financials_from_xbrl_package` is a separate entry point taking a
full-SGML `source_store`/`source_observation`, a separate package
`package_store`/`package_observation`, the request, `produced_at`, and a
`SecXbrlPackageAvailability` wrapper.  The wrapper names that exact package
observation and contains
`AvailabilityEvidence` with `SOURCE_DECLARED` and `TIMESTAMP` only.  At replay,
the producer verifies that the wrapper observation equals the supplied package
observation, its first-observed and snapshot-fetched timestamps equal the
replayed package manifest and observation receipt, and its declared timestamp
equals the envelope timestamp.  A declared timestamp later than the package
observation is rejected.

The full-SGML header remains the sole source of filing identity and acceptance
time.  The package envelope must bind to that exact raw observation and the
same CIK/accession/form.  For the package branch,
`source_available_at = max(header_accepted_at, package_declared_at)`; neither
timestamp is silently substituted for the other.  Output projection
`source_artifact_identity` is the package observation's `fact_version`, while
the envelope preserves the raw-SGML receipt binding.  This proves only the
replayable local chain and caller declaration, not that the package is original
SEC material, independently cross-validates EDGAR, or establishes a complete
taxonomy closure.

The returned financial vintage retains `accepted_at` from the full-SGML header,
but its `availability_anchor` is that same maximum.  Its package-specific
availability policy is `max-filing-acceptance-package-declared`, with
`SOURCE_DECLARED` basis, `SECOND` precision, and zero lag, so downstream
serialization cannot make package-derived facts available at header acceptance
when the declared package time is later.

The package branch returns a distinct production result that retains both the
full-SGML source observation and the package observation/evidence needed to
replay the chain.  It uses the same installed edgartools 5.56.0
traditional-XBRL parser and `parse_statement_rows` path as embedded components,
but has distinct parser and adapter versions.  It retains the existing strict
context CIK and optional DEI checks, statement coverage,
Decimal/unit/precision/dimension behavior, row limits and projection decoding.
The old embedded-component branch keeps its prior parser configuration,
projection bytes and identities.

## Verification

Synthetic offline fixtures cover successful package replay, Decimal/native
precision, context and dimension preservation, availability maximum, package
and raw binding mismatches, malformed/noncanonical
envelopes, component/XML limits, evidence-manifest mismatch, and no projection
write on failure.  Existing traditional-SGML tests remain the regression proof
that the old path did not drift.  All parser tests deny network access.

# SEC observed-package known-by production v1

This slice produces offline financial rows with local observation evidence.
First public availability stays unknown. It does not grant MARKET_KNOWN or
SYSTEM_REPLAY eligibility and does not introduce a selector or consumer commit.
The retained first-filing qualification probe and prospective system-replay
integration are its concrete callers.

The follow-up [in-memory system replay contract](sec-observed-system-replay.md)
adds a separate selector requiring explicit quality and consumer-commit evidence.
The production receipt alone still does not establish eligibility, and the
original persisted observed-row schema remains unchanged.
The [observed lifecycle bundle](sec-observed-lifecycle-bundle.md) supplies
separate persistence and rebuilds retained source/package inputs before issuing
restored producer-validated objects; it does not promote observation time to
public availability.

## Inputs and package

Add `serialize_sec_observed_xbrl_package`, `decode_sec_observed_xbrl_package`,
and `produce_sec_financials_from_observed_xbrl_package`, with separate typed
package, evidence, vintage, and production results. Reuse the explicit
`SecSgmlFinancialsRequest` and existing component byte type.

The canonical UTF-8 JSON envelope has schema `sec-observed-xbrl-package-v1`,
filing fields CIK/accession/form plus SGML fact_version and observation_identity,
and the original component bytes as canonical base64. It has no declared source
availability timestamp. Require schema/presentation/labels/instance exactly
once; calculation/definition are optional. Unknown fields and duplicate keys
fail. There is no network, filesystem extraction, or taxonomy download.

The caller observes the complete assembled envelope with provider `sec`, endpoint
`company-filing-observed-xbrl-package`, serialization
`sec-observed-xbrl-package-v1`, parameters exactly cik/accession_number/form, and
empty fields. That receipt describes local observation of the assembled bytes,
not SEC publication or individual component download history. Observation clocks
and origins are caller-trusted, as with SnapshotStore generally.

The producer receives source/package/output stores and observations for the
first two, the explicit request, and aware `produced_at`. Replay each input once
with strict request and serialization validation. Source SGML uses the existing
`company-filing-sgml` / `sec-filing-sgml-v1` contract. Verify the package's exact
SGML observation/fact binding, requested filing identity, and parsed header.

## Evidence and output

Require header acceptance <= SGML observation time and
`known_by_at = max(sgml_observed_at, package_observed_at) <= produced_at`.
Use the selected observation receipts, not a historical first-observed timestamp
substituted from the manifest. Normalize aware timestamps to UTC. A private
factory creates validated evidence only after both replays and binding checks;
unchecked references or caller-provided dates cannot bypass those checks.

Use a new vintage type holding filing metadata and existing `SecStatementRow`
tuples. It must not instantiate `SecCompanyFinancialVintage`, whose acceptance
anchor defaults have different semantics. Keep accepted_at distinct from
known_by_at; do not fill any source_available_at field with either value.

Persist a new canonical result serialization with both receipt identities and
fact versions, explicit request, parser/configuration versions, accepted_at,
known_by_at, produced_at, and ordered typed rows. Serialize no paths or contact
details. Reuse the pinned edgartools 5.56.0 parser and native row semantics.
The result never creates AvailabilityEvidence, SecNormalizedFinancialFactVersion,
legacy typed projections, or old bundle inputs.

The producer checks all input, time, parser, coverage, and output bounds before
writing the result. Bound each raw/envelope/result to 8 MiB, each decoded
component to 2 MiB, XML aggregate elements to 200,000, depth to 128, and rows to
10,000. Caller limits are positive exact integers and stricter-only. Invalid
builds leave existing results unchanged. This is a bounded local operation,
not an interruptible third-party parser deadline or a background service.

## Reproduction and acceptance

Retain the two source receipts and the output receipt. SnapshotStore replay
checks retained bytes and identities; rerunning the producer with the same
inputs/request/produced_at checks the parsing chain and reproduces output bytes.
This slice does not add a persisted financial-result loader or historical
selector. Any later such API must also enforce quality and consumer-commit
cutoffs; known_by_at alone is insufficient.

Use synthetic fixtures with the real parser and denied network access. Test
both observation orderings, causal failures, wrong request/receipt/filing,
malformed or unsafe packages, strict limits, native Decimal/unit/period/dimension
preservation, immutable restart/rebuild, and zero output writes on failure.
Verify schema/type isolation from old production and query APIs and unchanged
old identities. Independent Astra review is required before commit.

The [availability decision](sec-availability-evidence-decision.md) remains the
owner of why acceptance and modification metadata are not publication evidence.

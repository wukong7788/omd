# SEC availability evidence: decision and required migration

Date: 2026-09-11. Evidence investigation and the query-boundary remediation are
complete; the real-source P1 gate remains open.

## Source evidence

The SEC states that no timestamp identifies when filing content first becomes
available on sec.gov. Acceptance records EDGAR acceptance; website publication
may follow with an unpredictable lag. Therefore acceptance cannot establish
exact website availability. See the timestamp and lag sections of the
[SEC Webmaster FAQ](https://www.sec.gov/about/webmaster-frequently-asked-questions).

The [SEC API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
describes typical processing delays for submissions and XBRL APIs. These are not
guaranteed bounds or per-artifact publication timestamps, and do not specify the
publication time of a retained extracted instance.

[HTTP Last-Modified](https://www.rfc-editor.org/rfc/rfc9110.html#name-last-modified)
describes when the origin server believes a representation was last modified.
It is not a first-publication field. Neither that header nor an index's modified
field is promoted to publication evidence without an authoritative semantic
definition and binding to the exact retained version.

The AAPL probe therefore remains parsing-only. No additional filing downloads,
fixed-lag substitution, or guessed timestamp are needed to conclude this
investigation. This does not assert that no external dissemination evidence can
ever exist; none qualifying for this retained package has been established.

## Existing implementation gap

The automatic SGML producer writes header acceptance into
`source_available_at`. That value is only an acceptance proxy. `MARKET_KNOWN`
now explicitly rejects every supplied valid normalized version whose exact
adapter provenance is `sec-sgml-financial-adapter-v1`, before policy, cutoff,
or quality filtering. This deliberately fails mixed input instead of returning
a partial or empty result. `SYSTEM_REPLAY` retains its existing causal gates.

The package producer requires caller-declared exact availability. Its validation
binds a declaration to the retained package; it does not establish the truth of
the declaration. Do not manufacture `SOURCE_DECLARED/TIMESTAMP` evidence from
acceptance, retrieval, or modification metadata to satisfy this interface.

A successful retrieval of retained bytes establishes that those bytes were
known by the observation time. It does not identify their first public time.
For a package, any such bound must cover all required components and their
filing binding. Existing `SYSTEM_REPLAY` applies additional production, quality,
and consumer-commit cutoffs; it is not a proof of first public availability.

## Implemented query restriction and migration

- `MARKET_KNOWN` callers supplying legacy automatic-SGML versions must remove
  those versions from their selected input set or use a separately qualified
  future production path; no timestamp is backfilled and no evidence is
  rewritten. Package-producer and caller-declared adapter versions retain their
  existing behavior.
- Preserve retained snapshots and identities. Do not rewrite old evidence or
  reinterpret its time field in place. Document the affected query behavior and
  require reruns of affected market backtests before claiming parity.
- The separate [observed-package production contract](sec-known-by-production.md)
  preserves exact-byte lineage and conservative local observation bounds without
  relaxing the existing package wrapper's exact-time requirement. Its new row
  product is separate from legacy PIT versions. The follow-up
  [in-memory system replay contract](sec-observed-system-replay.md) requires
  explicit quality and consumer-commit evidence; it does not establish public
  availability. The separate [lifecycle bundle](sec-observed-lifecycle-bundle.md)
  preserves these records and rebuilds the retained source chain on restoration;
  neither its hashes nor that rebuild authenticate the caller's attestations.

The query restriction is delivered. No consumer migration or market-backtest
rerun is claimed here.

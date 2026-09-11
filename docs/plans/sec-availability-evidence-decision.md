# SEC availability evidence: decision and required migration

Date: 2026-09-11. Evidence investigation complete. Runtime remediation is not
implemented by this documentation change; the real-source P1 gate remains open.

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

The automatic SGML producer currently writes header acceptance into
`source_available_at`. Its versions can enter `MARKET_KNOWN` queries using that
time, although it is only an acceptance proxy. A cutoff between acceptance and
actual website availability can therefore admit a fact too early. Existing
offline test success does not resolve this gap. The code does not yet enforce
the restriction described below.

The package producer requires caller-declared exact availability. Its validation
binds a declaration to the retained package; it does not establish the truth of
the declaration. Do not manufacture `SOURCE_DECLARED/TIMESTAMP` evidence from
acceptance, retrieval, or modification metadata to satisfy this interface.

A successful retrieval of retained bytes establishes that those bytes were
known by the observation time. It does not identify their first public time.
For a package, any such bound must cover all required components and their
filing binding. Existing `SYSTEM_REPLAY` applies additional production, quality,
and consumer-commit cutoffs; it is not a proof of first public availability.

## Next implementation slice

- Protect the query boundary first: identify automatic acceptance-proxy versions
  through their producer/adapter provenance and explicitly reject their use in
  `MARKET_KNOWN`; do not silently return an empty result or backfill a timestamp.
- Preserve retained snapshots and identities. Do not rewrite old evidence or
  reinterpret its time field in place. Document the affected query behavior and
  require reruns of affected market backtests before claiming parity.
- Then define a separately versioned known-by evidence and production contract,
  with explicit basis, exact-byte lineage, and conservative observation bounds.
  Do not merely relax the existing package wrapper's exact-time requirement.
- Test cutoffs between acceptance and observation, explicit rejection of legacy
  proxy evidence, system-replay causality, and unchanged persisted identities.

These are pending implementation and migration requirements, not delivered API
capabilities. No consumer migration or market-backtest rerun is claimed here.

# SEC representative pilot: bounded acquisition outcome

Status: INCOMPLETE / RESOURCE_LIMIT. Recorded 2026-09-12; acquisition on
2026-09-11 UTC. This is qualification evidence, not a new code slice.

## Scope and retained evidence

The frozen local universe hash was rechecked. The pilot selected eight candidates
within that universe, reused the retained AAPL filing, and requested current SEC
ticker/issuer/exchange metadata plus six company submissions root files. The
six exact current associations were unique. Historical alias validity, share
class, currency and security-level PIT eligibility are not established by those
associations. Submissions history files were not fetched, so these roots do not
constitute a complete discovery closure or justify advancing an event cursor.

The local protocol hash is
`a7159cd6e2627774ed0cc25c390cb4aaa3f08cf43e7d20ac3c02c21b66beeaee`.
Raw data, the complete candidate list, contact and diagnostic scripts remain
ignored. Local reports under `artifacts/sec-pilot-20260912/`:

| Report | SHA-256 |
| --- | --- |
| `20260911T163155011176Z-identity-metadata.json` | `ec6118413e652ef68eba8a8e8f7805c40bc65b816b29aa60dfdf412d35739310` |
| `20260911T163414976799Z-submissions.json` | `85b8b1ad32804bc4d4127ef581a78522ccf781d99f5843b440f226a6a5de5d31` |
| `20260911T163844394529Z-filings.json` | `aab991babf35a60a7adf413171810bcaac4332de61a97b1fb63c8daeaa9e3121` |
| `msft-body-diagnostic.json` | `7dac128cf0bd9c6d6db049f404e04ef64fab6b716f50614cd2c86eb829fcfd4f` |
| `20260911T164608087459Z-continuation-filings.json` | `fde9201e5750e737ab7b044a76d4365b3b1c57492d5640aa453e6ec17591b009` |

## Outcomes and budget deviations

- AAPL: retained known-by production and selected accounting/HTML checks remain
  valid within their [documented scope](sec-financial-source-qualification.md).
- MSFT: the original full-SGML GET failed before retention with `ValueError` in
  9.93 seconds. Its log omitted the failure stage; it is not retrospectively
  relabeled as a proven size failure. One subsequent HEAD provided no length.
  An explicitly recorded diagnostic repeat GET read 8,388,609 bytes and stopped,
  proving that the diagnostic response exceeded the unchanged 8 MiB source cap.
  No complete source snapshot or financial production was created.
- NVDA: an explicit continuation protocol excluded MSFT, preserved the original
  16:51:55.011176 UTC batch deadline and unchanged caps, and stopped at its first
  filing: `source_read / RESOURCE_LIMIT` after 10.37 seconds. The log does not
  distinguish an invalid Content-Length, a declared size overflow and an actual
  body overflow. No source snapshot or production was created.
- Two other selected domestic issuer filings were skipped after the stop; their
  retained metadata is not parsing evidence.
- TSM has a foreign-issuer filing path; the current 10-K/10-Q production scope
  does not cover its 20-F/6-K path. This is unsupported coverage, not no disclosure.
  See [TSMC SEC filings](https://investor.tsmc.com/english/sec-filings).
- SCHD is an ETF and VIX is an index. Corporate-company 10-Q/PE requirements are
  inapplicable to those instrument types; lack of a ticker match alone was not
  used to infer that conclusion. See [Schwab SCHD](https://www.schwabassetmanagement.com/products/schd)
  and [Cboe volatility products](https://www.cboe.com/tradable-products/volatility-trading/).

The initial sandbox metadata attempt failed before the successful network run.
Including that attempt, root metadata, filing attempts and diagnostics gives
12 attempted requests (one HEAD, eleven GETs); this counts attempts rather than
asserting every attempted request reached SEC. MSFT used two GETs and one HEAD.
The diagnostic therefore departed from the original one-attempt constraint;
`diagnostic-amendment.json` records that departure, and the original report is
preserved. The amended diagnostic allowed only one repeat GET, at most 30 seconds
and the original batch deadline, without downstream production. No additional
retry followed. All filing requests were serial; source and component caps
remained 8 MiB and 2 MiB. RSS measurements are sampled, not hard peak enforcement.

## Acceptance consequence

The representative acquisition gate did **not** pass. No new financial PASS,
market-first-publication claim, quality decision, consumer commit or publication
was generated. No broader universe acquisition is justified by this run.

The full-SGML resource limit is now a concrete blocker for this acquisition path.
Any replacement path or revised budget requires an explicit bounded contract,
source identity and retention guarantees, representative resource evidence, and
independent review before expansion. Increasing timeouts does not resolve it.
The missing failure-stage telemetry in the original local harness must also be
corrected in any subsequent acquisition implementation.

## Separate document-source pilot

The separately frozen single-MSFT document-source protocol
`0c9108c24cac62e97213a0513d721c0aacccf45061f7d7aa567ce0c1e7a36bb2`
was run once on 2026-09-11 UTC after source/financial offline acceptance.
It reused the retained submissions observation and allowed at most eight new GETs,
60 seconds per filing, 512MiB sampled RSS and 16MiB aggregate source bytes.
It made two GETs: the directory index returned 200 and retained 10,003 bytes;
the primary HTML returned 200 but exceeded its 4MiB read cap at
`primary:body_limit`. The partial primary was not retained.

The run is **FAILED_STOPPED**, with 4.014 seconds elapsed and peak RSS
49,332,224 bytes. This is a primary admission-size failure, not a time or memory
budget failure; the complete primary size was not measured. No source package,
financial product, quality PASS or consumer commit was created. No retry or
universe expansion followed. This does not supersede the original pilot failure.

Local evidence: `artifacts/sec-document-live-pilot/20260911T182852043611Z/report.json`,
SHA-256 `490eb3fe92cc0c36e756bcdef63276ae49c0a40059d45dd2e56117446487b5a6`.
Independent review confirmed the bounded failure and supported continuing the
offline document lifecycle work without relaxing the acquisition budget.

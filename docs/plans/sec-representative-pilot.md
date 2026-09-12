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

### Directory-declared sizes and deferred primary-only expansion

Offline inspection of that retained directory declares 7,731,948 bytes for
`msft-20260331.htm`, 1,508,093 for its schema and 9,675,172 for its extracted
instance. These are index declarations, not complete body measurements. The
three declared sizes already exceed the 16MiB source budget, and the instance
exceeds the 2MiB XML admission limit. Raising primary alone cannot complete
this filing's current source path.

Two ignored experiments temporarily patched only the primary cap to 8MiB
inside their own processes. Near-32MiB, two-source actual parser/bundle probes
used ASCII and astral-containing primary text; RSS was respectively 326,074,368
and 371,671,040 bytes, with 2.637 and 2.719 seconds elapsed. Reports remain under
`artifacts/sec-primary-admission-acceptance/{ascii,astral}/report.json`. These
partial experiments did not cover larger instances or all structural shapes.
No SDK cap changed, no extra network request followed, and no expanded-source
acceptance is claimed. Independent review deferred isolated cap expansion in
favor of a complete-source design; other offline plan work can proceed.

### Separate embedded-schema diagnostic

A separately frozen one-request diagnostic retained the complete MSFT schema:
1,508,093 bytes, 6,569 XML elements, HTTP 200, 2.118 seconds and 54,722,560 bytes
peak RSS. It found one embedded label link, 85 presentation links, 22 calculation
links and 40 definition links, with zero `linkbaseRef` elements. Thus this filing
uses schema-embedded linkbases; the current separate-linkbase source contract
cannot represent it merely by raising byte limits. No referenced resource was
fetched and no financial output, source package or PASS was produced.

Protocol SHA-256: `0045b4021f97244c42d0200ab4c234ae1208d15b61c009fc21d12754d98fea7b`.
Local report: `artifacts/sec-schema-diagnostic/live/20260911T191719908529Z/report.json`,
SHA-256 `6ba891523e21f83e27944c34bca1fe4d1bf3937365281e8c9d71b93ae9d337fd`.
The diagnostic was independently reviewed after adding HTTP Content-Length
equality validation; four offline transport cases passed before the one live run.
The previous complete-filing pilot remains failed.

A subsequent network-denied schema-only probe passed the intact retained schema
to pinned edgartools 5.56.0. It populated 85 presentation trees, 22 calculation
roles, 40 definition roles and 610 labeled elements in 0.740 seconds, with
193,413,120 bytes peak RSS. The schema has one appinfo/linkbase container.
Evidence: `artifacts/sec-schema-diagnostic/parser-probe.json`. This establishes
only schema parsing compatibility; large-instance production and complete bundle
restoration still need their own resource and correctness acceptance.

### Complete retained MSFT embedded-source pilot

After embedded source/financial code commit `eefc46c` and 1,806 offline tests
passed, a new independently reviewed protocol fetched only the two remaining
files for accession `0001193125-26-191507` (period 2026-03-31). It reused the
exact retained submissions, directory and schema receipts. Primary and instance
returned HTTP 200 with 7,731,948 and 9,675,172 bytes; the five raw sources total
19,109,897 bytes. Production, bundle write and network/write-denied bundle
restoration completed in 5.059 seconds with 432,734,208 bytes peak RSS.

The output contains 257 rows: balance sheet 67, income statement 60, cash flow
130. Production identity:
`13e24a65b6020aad55d15f55e4add512542fe93435f8cfa7add5185434f25084`.
A separate offline XML/Decimal comparison matched all 257 emitted rows on
concept namespace, context/unit references, value, period and native decimals.
Independent review accepted these limited production/restoration and raw-fact
correspondence claims. It does not prove statement completeness, normalized
unit correctness, financial quality, first-publication time or representative
coverage. No quality PASS or consumer commit was created. Previous failed
pilots remain preserved and the larger representative gate remains open.

Protocol SHA-256: `5863d9ea3c4a7760f0c20b54d5ebf4f664fa6c5db6f8ca141a904ce513afa704`.
Local evidence directory: `artifacts/sec-embedded-live-pilot/20260912T033138500251Z/`.
`report.json` SHA-256: `ded58d35ac8da3d526976e1f0bebceffa3405d60ae2b3d8b2857c3f0e6839aa6`.
`raw-corroboration.json` SHA-256: `8283049122149618d9b6a6a63224932c5e2e25b7b9a9794f8dd2774a8ca291a9`.

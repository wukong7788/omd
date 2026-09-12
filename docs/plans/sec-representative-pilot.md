# SEC representative pilot: bounded acquisition outcome

Status: INCOMPLETE — selected evidence accepted; representative gates remain open.
Current consolidation: 2026-09-12. The historical acquisition failures below remain
valid records of those attempts, not the current outcome of later source paths.

## Current status: representative gate consolidation

| Area | Verified scope |
| --- | --- |
| MSFT / TSLA / GOOG source production | Complete source closure, bundle and financial replay, and raw-fact correspondence for 257 / 220 / 207 emitted rows. |
| Units | All 684 emitted rows match raw measure definitions. |
| Cash | Eight balance rollforwards and eight component-total checks MATCH with zero residual and tolerance. |
| Equity | Six MATCH and five historical equity-only contexts remain MISSING. |
| Revenue / gross profit | Four selected TSLA checks and four additional MSFT checks MATCH; GOOG does not disclose the selected gross-profit subtotal. |
| AAPL | Separate retained evidence is reused: 180 rows, 20 MATCH / 2 MISSING selected accounting checks and 46 selected primary HTML cells. Coverage is not identical to the other issuers. |

Canonical detail: [accounting diagnostics](sec-document-accounting.md) and
[AAPL qualification](sec-financial-source-qualification.md). These observations
are not an overall financial-quality PASS or proof of every source field.
Historical unit limitations later in this document were resolved by the linked
684-row audit for those emitted rows only.

The representative design allows **at most eight candidates**, not eight required
corporate issuers. NVDA remains deferred by user instruction; its original failed
attempt remains recorded. TSM 20-F/6-K is unsupported by the current domestic
10-K/10-Q path. SCHD (ETF) and VIX (index) do not require corporate financial
statements under this scope. The 53-candidate universe is an upper bound, not a
promise to produce 53 company financial datasets.

Remaining gates must be distinguished:

| Gate | Remaining evidence / consequence |
| --- | --- |
| Required financial coverage | Freeze the required statement-field and missing-data acceptance matrix, then compare existing AAPL/MSFT/TSLA/GOOG evidence. Selected equalities do not establish all required field coverage. |
| Period and availability | Verify required Q/YTD/FY and TTM inputs and their version-specific availability. Local known-by evidence remains valid for that query mode; SEC acceptance alone does not establish first publication of every source version. |
| Security identity | Complete applicable share-class, currency, provider-code and historical validity evidence. Current issuer associations are not security-level PIT identity. |
| Batch performance | Complete applicable cold/warm and multi-filing budget evidence. Successful single-filing runs do not prove batch acceptance. |
| Downstream P2/P3 | Event recovery/recomputation and representative metric replay remain separate plan gates; existing partial implementations are not reclassified as absent. |
| Downstream P4 | Actual Stock Notify shadow and consumer comparison remain open. The actual consumer data directory has not been supplied; do not guess it. This is separate from source-parser acceptance. |

The next bounded execution is to freeze the required statement-field acceptance
matrix for the four retained issuers and inspect only uncovered requirements.
Reuse passed source, replay, unit and accounting evidence. Do not introduce an
unbounded all-detail-row audit or expand acquisition before the applicable gate.

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

### GOOG and TSLA directory discovery

On 2026-09-12 UTC, the next bounded stage verified both retained submissions
receipts and discovered only the GOOG and TSLA filing directories. Local project
`uv` ran 12 offline tests using retained metadata and synthetic transports;
undefined-name checks and offline candidate verification passed before the run.
The reviewed protocol allowed two serial GETs, no retries or redirects, 2MiB
per directory, 4MiB total, 60 seconds and 512MiB sampled RSS.

Both responses returned HTTP 200: GOOG retained 9,891 bytes for accession
`0001652044-26-000071`; TSLA retained 8,022 bytes for
`0001628280-26-049270`. Elapsed time was 1.670 seconds and peak RSS was
42,663,936 bytes. Both raw receipts replayed with network and snapshot writes
denied, reproducing their directory summaries and observation identities.

Directory filenames are candidates, not validated XBRL layout or financial
production evidence. GOOG's extracted-instance declaration is 3,046,086 bytes,
above the separate-document path's unchanged 2MiB XML cap. TSLA's primary is
declared as 1,573,323 bytes and its instance as 1,471,667 bytes; the listed schema
and separate linkbases also fall below that path's individual limits. Actual
body sizes, source closure and parser compatibility remain unverified. TSLA is
the next bounded complete-source candidate; GOOG requires a separately reviewed
resolution of the admission limit. No component downloads, new financial PASS,
consumer commit or broader-universe expansion occurred in this stage.

Protocol: `artifacts/sec-next-directory-pilot/protocol.json`, SHA-256
`0529bffff6198d1a6ff36d0fe061cf08cdc3ea83129fd81e707846a4239ae771`.
Report: `artifacts/sec-next-directory-pilot/20260912T035930911974Z/report.json`,
SHA-256 `ab8251e5a09ff229b24e34a8fe7ca5b0eacc874a16ecd9dabf7894c3ce73ebd8`.

### Complete retained TSLA separate-document pilot

On 2026-09-12 UTC, a reviewed single-filing protocol reused the exact TSLA
submissions and directory receipts above. Nine local `uv` offline tests passed
for receipt preflight, protocol tampering, transport failure/size/length handling,
redirect rejection and exception redaction. These tests did not constitute a
synthetic end-to-end parser probe; the following real run supplies that evidence.

Seven serial GETs returned HTTP 200 for the primary, schema, instance and four
separate linkbases. Reference selection and the existing source producer
validated the closure without changing SDK admission limits. The nine retained
sources total 4,832,407 bytes. Production and bundle restoration completed in
14.418 seconds with 190,906,368 bytes peak RSS, within the 60-second/512MiB
sampled resource budget. Network was denied during production; network and
snapshot writes were denied during restoration. Both bundle and standalone
financial restoration preserved production identity and standalone row parity.

The result contains 220 rows: balance sheet 62, income statement 80, cash flow
78. A separate network/write-denied XML/Decimal comparison matched all 220
emitted rows on concept namespace, context/unit references, value, period and
native decimals. Review accepted these limited production/restoration and
raw-fact correspondence claims. Statement completeness, normalized unit quality,
financial-quality PASS and first-publication time remain unproven. No consumer
commit, publication or broader-universe expansion occurred; representative
acceptance remains open. The subsequent GOOG admission resolution and pilot are
recorded below.

Production identity:
`85e3cf97a01cdf8d147b0467334430e2dd555f8dcac74b8bf6d1b7900b8be391`.
Protocol SHA-256:
`32da123aaa597671cb6caa0e7c3f4e825105095f6da0774e8d1d326df0686b15`.
Local evidence: `artifacts/sec-tsla-document-pilot/20260912T041356599937Z/`.
Report SHA-256: `68f3193bef68a9d7b4a1ce5ff98d4a17804dd14946ac6c83d5df6a362ef91bb8`.
Raw correspondence SHA-256:
`03f3e802a869a44e20bd0649162eae11f14b0961e5ea08273d3ceab89b8542d3`.

### Complete retained GOOG separate-document pilot

Commit `8713d00` raised only the separate-document instance cap to 4MiB after
214 affected tests, legacy identity/byte parity and a near-16MiB raw-source
resource probe passed. Other per-role caps and the 16MiB aggregate cap remain
unchanged. This resolves the directory-declared GOOG instance admission issue
recorded above; it does not relabel the earlier failed pilots.

The reviewed GOOG harness reused the two exact metadata receipts and passed nine
local `uv` offline checks. On 2026-09-12 UTC, seven serial GETs returned HTTP 200
for accession `0001652044-26-000071`; its extracted instance measured
3,046,086 bytes. Nine raw sources total 8,083,259 bytes. Complete source and
financial production, bundle writing, and read-only bundle/financial restoration
finished in 22.280 seconds with 300,269,568 bytes peak RSS. Production/restoration
had network disabled; restoration also denied snapshot writes. Production
identity and standalone restored rows matched.

The output contains 207 rows: balance sheet 70, income statement 60, cash flow
77. Independent-from-parser XML/Decimal comparison matched all 207 emitted rows
on concept namespace, context/unit references, value, period and native decimals.
Review accepted this limited evidence. It does not prove statement completeness,
normalized unit correctness, financial-quality PASS or first-publication time.
Representative and consumer acceptance remain incomplete. No quality PASS,
consumer commit or publication occurred.

Production identity:
`5853a29bbe341c76d679e9ef508d6fc7dbb5bb59fdb2681e2979780bbae9f2d7`.
Protocol SHA-256:
`ced7eaa621617224a37f4842995a83f6de1f87534dc1143583d69cc220d88cef`.
Local evidence: `artifacts/sec-goog-document-pilot/20260912T044307782126Z/`.
Report SHA-256: `c7c881d4f69f8408cc68c31dfc72be91cec3a40eedd745327d70860b67bde8e5`.
Raw correspondence SHA-256:
`4c712c4ab53f4ac25688a63786ef21af5ea2d359a5de1816e875df59e546ae2e`.

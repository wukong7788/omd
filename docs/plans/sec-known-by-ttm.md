# SEC Known-By TTM Diagnostic

## 1. Goal & Boundaries
This module provides `diagnose_sec_fy_ytd_ttm`, an offline public diagnostic function for evaluating FY+YTD TTM arithmetic directly over sealed `SecObservedFinancialProduction` and `SecDocumentFinancialProduction` objects.

- **Diagnostic-Only**: Operates strictly on local observed known-by facts. It does not issue financial quality `PASS` marks, perform PIT quality/commit gating, or fabricate `SecNormalizedFinancialFactVersion` records.
- **No Publication Claim**: Evaluates inputs using observation timestamps (`max(vintage.known_by_at, produced_at, output_observation.snapshot_fetched_at)`) as availability bounds; does not assert SEC acceptance or market first publication.
- **Scope & Coverage**: Synthetic arithmetic fixtures are verified. FY2025 GOOG,
  TSLA and AAPL productions have subsequently been retained and replayed; their
  independent raw-field audit is separate from successful production replay.
  MSFT annual input remains blocked by source-size admission.

## 2. API Contract
```python
def diagnose_sec_fy_ytd_ttm(
    *,
    productions: Iterable[SecObservedFinancialProduction | SecDocumentFinancialProduction],
    declarations: tuple[SecKnownByTtmInput, ...],
    canonical_cik: str,
    metric: str,
    accounting_scope: str,
    attribution_scope: str,
    comparability_cohort: str,
    security_basis: str,
    scope_reference: str,
    knowledge_cutoff: datetime,
    diagnosed_at: datetime,
    max_rows: int = 10000,
) -> SecKnownByTtmResult:
```

### Parameters & Invariants
- `metric`: Exactly `"REVENUE"` or `"NET_INCOME"`.
- `canonical_cik`: Nonzero unpadded ASCII digits, 1..10 digits. Must match the unpadded CIK of all supplied productions.
- `declarations`: Exactly 3 ordered `SecKnownByTtmInput` declarations: `(prior_fy, current_ytd, prior_ytd)`. Each defines `production_identity`, `statement_type`, `concept`, `context_ref`, `fiscal_year`, `fiscal_end_quarter`, `period_start`, `period_end`, and `declaration_reference`.
- `productions`: Up to 3 unique production objects (evaluated with a 1-item sentinel). Every supplied production must be referenced by at least one declaration, and no unreferenced or duplicate productions are admitted.
- `max_rows`: Hard limit between 1 and 10,000 aggregate rows across all admitted productions.
- `knowledge_cutoff` & `diagnosed_at`: UTC datetimes with `diagnosed_at >= knowledge_cutoff`. Every production's availability bound must be `<= knowledge_cutoff`.

### Row Matching & Fiscal Arithmetic
- **Row Matching**: Each declaration must match exactly one duration row in the referenced production with identical statement type, concept, context, and duration dates. Rows must have no dimensions, valid Decimal values, and matching ISO 4217 currencies/units across all 3 terms.
- **Bridge Rules**:
  - `prior_fy.fiscal_end_quarter == 4`
  - `current_ytd.fiscal_end_quarter == prior_ytd.fiscal_end_quarter in {1, 2, 3}`
  - `current_ytd.fiscal_year == prior_fy.fiscal_year + 1`
  - `prior_ytd.fiscal_year == prior_fy.fiscal_year`
  - `prior_ytd.period_start == prior_fy.period_start` and `prior_ytd.period_end < prior_fy.period_end`
  - `current_ytd.period_start == prior_fy.period_end + 1 day`
- **Result Output**:
  - Value: `prior_fy + current_ytd - prior_ytd` computed exactly using `_decimal_sum` and `copy_negate()`.
  - Period: `prior_ytd.period_end + 1 day .. current_ytd.period_end`.
  - Identity: Deterministic canonical hash under schema `sec-known-by-ttm-diagnostic-v1` bounded to 65,536 canonical bytes.


## 3. Verified implementation scope

Two concrete callers motivated this entry point: retained AAPL observed productions
and MSFT/TSLA/GOOG document productions. FY/YTD labels, metric-to-concept meaning,
accounting/attribution scope and revision comparability remain explicit caller
assertions. Matching CIK, rows, units and intervals does not independently verify those
assertions. A result is not eligible input to the existing PIT metric graph.

The additive implementation preserves existing metric/selector APIs and identities.
25 new offline tests pass, including observed/document/mixed sources, a synthetic
371-day fiscal year, exact Decimal identities under an Inexact trap, source-seal
and declaration tampering, time cutoffs, missing/duplicate rows and count bounds.
34 related accounting regression tests and the previously run 35 metric-graph tests
pass. Ruff, format and targeted type checks pass. Tests use synthetic sources and
mock only the parser boundary where stated; seal validation remains real.

A mixed observed/document diagnostic produces `1000 + 600 - 500 = 1100 USD`
for 2023-04-01 through 2024-03-31, with three retained term evidences and a cutoff
no earlier than every production/observation time. Local ignored evidence:
`artifacts/sec-known-by-ttm-acceptance/20260912T081207053295Z/report.json`, SHA-256
`64c1f83aefd231b7ee8269d40f6a97015b423f089e53d5bbb7cc9a71d3202fbd`.

Full financial quality, MARKET_KNOWN eligibility, consumer shadow and publication
are not granted by this slice.

## 4. Bounded FY2025 acquisition (2026-09-12)

Four retained SEC submissions supplied exact annual accession/primary-document
metadata. Four directory GETs completed in 4.987 seconds, returning 43,459 bytes;
peak RSS was 42,369,024 bytes. Six synthetic discovery checks covered successful
selection, duplicate primary entries, response size/length and metadata tampering.

Three annual productions completed source retention, financial production,
bundle writing and full restore with identical production/output identities:

| Issuer | FY2025 accession | Rows | Retained source bytes | Seconds | Peak RSS bytes |
| --- | --- | ---: | ---: | ---: | ---: |
| GOOG | 0001652044-26-000018 | 202 | 8,223,839 | 17.928 | 286,162,944 |
| TSLA | 0001628280-26-003952 | 232 | 8,097,852 | 48.466 | 294,535,168 |
| AAPL | 0000320193-25-000079 | 190 | 4,803,902 | 16.631 | 199,491,584 |

Each run reused the existing separate-document pilot limits: primary and instance
4 MiB each, other individual sources 2 MiB, aggregate sources 16 MiB, seven HTTP
attempts, 60 seconds and 512 MiB RSS. No limits were expanded. No financial
quality PASS or consumer commit was issued.

MSFT FY2025 accession `0000950170-25-100235` lists an 8,158,067-byte primary and
10,533,273-byte instance. Both exceed this route's 4 MiB limit. Discovery stopped
before downloading those payloads; this is an explicit coverage gap.

Ignored local acquisition evidence:

- `artifacts/sec-fy2025-ttm/20260912T090452096315Z/report.json`, SHA-256
  `805159dbbfe92ac5dbbc27ce59a2496e2225065ae617dc9ed31451e375023837`.
- `artifacts/sec-fy2025-ttm/productions.json`, SHA-256
  `4c4ecdc44b5ded959f36a085f246b64e48332d06bf4f6023892baa81dfdcfc3d`;
  binds each annual report, production identity and output hash.

These checks establish bounded acquisition and replay, not independent accuracy
of every financial field or comparability across filings.

## 5. Real as-reported arithmetic and raw-field audit

The offline run restored the three annual productions and the previously pinned
current productions, denied network and snapshot writes, and called the public
diagnostic API with exact concept/context/date declarations. It completed in
3.081 seconds with peak RSS 311,050,240 bytes. Each result retains three complete
term evidences and an observation-based cutoff. No arithmetic fallback was used.

Values below are USD millions, displayed by exact division of the retained USD
Decimals. The formula is prior FY + current YTD - prior YTD.

| Issuer / metric | FY2025 | FY2026 YTD | FY2025 YTD | Diagnostic TTM | Result interval |
| --- | ---: | ---: | ---: | ---: | --- |
| GOOG revenue | 402,836 | 229,692 | 186,662 | 445,866 | 2025-07-01–2026-06-30 |
| GOOG net income | 132,170 | 174,771 | 62,736 | 244,205 | 2025-07-01–2026-06-30 |
| TSLA revenue | 94,827 | 50,623 | 41,831 | 103,619 | 2025-07-01–2026-06-30 |
| TSLA common-stockholder net income | 3,794 | 1,591 | 1,581 | 3,804 | 2025-07-01–2026-06-30 |
| AAPL revenue | 416,161 | 364,357 | 313,695 | 466,823 | 2025-06-29–2026-06-27 |
| AAPL net income | 112,010 | 101,464 | 84,544 | 128,930 | 2025-06-29–2026-06-27 |

GOOG revenue uses `us-gaap_Revenues`; AAPL/TSLA use
`us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax`. All net-income
inputs use `us-gaap_NetIncomeLoss`, with TSLA explicitly attributed to common
stockholders rather than its separately reported total net income. No concept
aliasing or gross-profit inference occurs. Annual/current/prior YTD dates are
retained exactly, including AAPL's fiscal calendar.

The independent XML audit used expanded QNames, context and unit references,
duration/instant dates, Decimal values and native precision. Of 624 annual rows,
621 matched every corresponding raw occurrence exactly: GOOG 202/202, TSLA
229/232 and AAPL 190/190. The three exceptions were TSLA's 2023/2024/2025
`us-gaap_IncomeTaxExpenseBenefit` rows:

- Each context contains four raw occurrences, including both `decimals=-6` and
  `decimals=-7` disclosures. Values differ at those reported precisions.
- Every selected output value/precision exists exactly in the raw instance and
  agrees with the primary income statement. The selected values are respectively
  -5,001, 1,837 and 1,423 USD millions, all at `decimals=-6`.
- An independent rational-arithmetic check found a nonempty common rounding
  interval for every occurrence in each group, and confirmed selection of the
  highest disclosed precision. This is consistent with the existing
  `_select_native_facts` policy in `src/ohmydata/providers/sec/_statement_parser.py`.
  It is not a claim that all duplicate raw values are identical.

The initial strict audit remains recorded as MISMATCH for those three rows;
the separate classification records precision-compatible duplicates. No SDK
code or precision policy was changed. All six annual TTM inputs passed the
strict XML check and were also checked against the primary income-statement
headings, fiscal columns and million-dollar scale. The twelve current/prior-YTD
terms were not re-audited against raw XML in this harness (`NOT_RUN`); their
production identities and output hashes match the earlier
[retained-field audit](sec-required-field-acceptance.md#5-consolidated-local-audit-2026-09-12).
Those earlier checks have their own stated coverage and do not become a new
18-term raw-XML acceptance claim.

Five independent synthetic harness tests passed, covering duration/instant
facts, conflicting duplicates, wrong period/precision/unit/namespace, nil versus
zero, and rejection of dimensioned, aliased or ambiguous selectors.

Ignored local evidence:

- `artifacts/sec-fy2025-ttm/investigation-20260912T092120203865Z.json`, SHA-256
  `b5535c46a287f4c91b2ec5dafcc8832bfe4ee97f635bd92aceb2b62fa800ca99`;
  binds the complete diagnosis report and records mismatch details and HTML sections.
- `artifacts/sec-fy2025-ttm/duplicate-classification-20260912T092214554574Z.json`,
  SHA-256 `c79071dcbde1357b88139372aabace9108e298d90f9396a544140ef44dfca8e9`.

These are **as-reported cross-filing arithmetic diagnostics**. The declared
cohort remains `AS_REPORTED_CROSS_FILING_NOT_RESTATEMENT_QUALIFIED`; overlapping
revision/restatement bases have not been qualified. MSFT annual admission,
cross-filing comparability, full financial quality, MARKET_KNOWN evidence and
consumer shadow remain open. NVDA remains deferred.

## 6. Cross-filing comparability inventory (2026-09-12)

The next offline audit restored the same six productions and retained all 1,231
rows. Comparison keys include statement role, exact concept, unit/currency,
dimension, period type and both dates. Different periods are not matched, nulls
are not equal-value evidence, and multiple contexts are reported as ambiguous
rather than arbitrarily selected. Production/output hashes remain pinned to
Sections 4–5; the report also retains production and observation times.

| Issuer | Same-period overlapping keys | Equal values | Different values | Income-statement overlaps |
| --- | ---: | ---: | ---: | ---: |
| GOOG | 33 | 30 | 3 | 0 |
| TSLA | 33 | 33 | 0 | 0 |
| AAPL | 30 | 29 | 1 | 0 |

All 96 overlapping keys are balance-sheet or cash-flow instant facts; no
overlapping group has missing values or ambiguous contexts. The 92 equal-value
comparisons do not establish comparability of income-statement duration facts.
Rows present in only one filing remain explicitly inventoried; differences in
period coverage do not become missing-data errors or zero values.

### Four presentation bridges

All four differences have exact arithmetic bridges to lines separately presented
in the current filing's comparative balance sheet. Both original primary
statements were inspected for the date columns, labels, amounts and USD-million
scale. The following values are USD millions:

| Comparative date / issuer | Annual presentation | Current comparative presentation | Delta |
| --- | --- | --- | ---: |
| 2025-12-31 / GOOG | Other current assets 16,309 | Other current assets 13,870 + inventory 2,439 | 0 |
| 2025-12-31 / GOOG | Other non-current assets 16,245 | Other non-current assets 14,962 + intangible assets 1,283 | 0 |
| 2025-12-31 / GOOG | Other long-term liabilities 8,449 | Other long-term liabilities 7,530 + deferred income taxes 919 | 0 |
| 2025-09-27 / AAPL | Other non-current assets 83,727 | Other non-current assets 72,634 + intangible assets 11,093 | 0 |

The bridge evidence preserves exact native concepts, selected contexts, precision
and values. These are `ARITHMETIC_PRESENTATION_BRIDGE` results, not concept aliases
or proof of a particular accounting cause. The originally observed values remain
unchanged; no SDK correction, synthetic replacement fact or financial PASS was
introduced. In particular, the four unequal values must not be silently treated
as the same normalized field across filings.

### Disclosure evidence and limits

The retained GOOG and AAPL current filings explicitly discuss prior-period
reclassifications. GOOG's accounting-policy statement includes an exception for
the descriptions that follow; it must not be reduced to an unconditional
“no changes” assertion. TSLA's current Note 1 states that ASU 2025-05 was adopted
prospectively on January 1, 2026, without electing its practical expedient, and
reports no financial-statement impact from that adoption. This is evidence about
that specific adoption, not a blanket comparability qualification.

Keyword search records separate restatement, reclassification, discontinued
operations and accounting-policy categories, at most 20 snippets per category.
GOOG annual/current reclassification hits and TSLA annual restatement hits exceed
that limit and are explicitly marked truncated. References to restated bylaws or
stock plans are not financial-statement restatements; no keyword hit is not proof
that no restatement occurred.

Heading-validated policy excerpts retain up to 18,000 leading characters and,
when longer, a separately located 6,000-character tail. The current policy notes
for all three issuers are covered by these excerpts, including TSLA's adoption
paragraph near the end of Note 1. Annual GOOG Note 1 still has a 7,769-character
middle gap. TSLA annual Note 1 is Overview; its accounting policies are in Note 2,
whose excerpts still omit a 44,567-character middle portion. This inventory is
not a complete accounting-policy review.

### Qualification result and concrete missing inputs

All six revenue/net-income selectors remain `NO_OVERLAP`: annual FY2025 and
the current filing's FY2026/FY2025 YTD periods do not provide the same duration
in both productions. Therefore neither unchanged balance-sheet totals nor the
four presentation bridges qualify the FY+YTD inputs as a common restatement
basis. The cohort remains
`AS_REPORTED_CROSS_FILING_NOT_RESTATEMENT_QUALIFIED`.

The retained submissions identify the original FY2025 comparative-quarter
filings needed for the next direct comparison:

| Issuer | Original comparative filing | Period end | Primary document |
| --- | --- | --- | --- |
| GOOG | 0001652044-25-000062 | 2025-06-30 | goog-20250630.htm |
| TSLA | 0001628280-25-035806 | 2025-06-30 | tsla-20250630.htm |
| AAPL | 0000320193-25-000073 | 2025-06-28 | aapl-20250628.htm |

Those payloads were not acquired in this annual-vs-current inventory slice (which
evaluated annual FY2025 vs current FY2026 filings, where duration periods have
`NO_OVERLAP`). They have subsequently been retained and compared against current
filing comparative YTD values under bounded admission in [Section 7](#7-original-comparative-ytd-evidence-2026-09-12),
supplying the six required duration overlaps. Any differences would have required
an explicit revision/reclassification bridge; equality alone still does not replace
review of the annual-period basis and relevant accounting disclosures. Candidate
selection covers retained recent submissions only, not complete amendment history.

### Validation evidence

The inventory and bridge runs completed within 60 seconds and 512 MiB, with
network and snapshot writes denied. Peak RSS was respectively 288,047,104 and
283,721,728 bytes; each completed in about three seconds. Five synthetic inventory
checks, six independent selector/separation/snippet checks and two synthetic
bridge checks passed. Relevant Ruff checks passed. No consumer rerun or migration
is required for these evidence-only changes; consumer shadow remains a later gate.

Ignored local reports:

- `artifacts/sec-fy2025-ttm/comparability-20260912T100717468926Z.json`, SHA-256
  `0e274a7c84c70e40774f55ac6a137387fe6c629a6f15ab2c704e712732a06d6d`.
- `artifacts/sec-fy2025-ttm/comparability_bridges-20260912T101104189907Z.json`, SHA-256
  `aeb426d4896ad79fdff86baa67f2809c8b3c0cdd1891721ddd35dfebec7fd586`.
- `artifacts/sec-fy2025-ttm/prior-ytd-candidates-20260912T101233083993Z.json`, SHA-256
  `97eeea1da6ab61e382fa7dcc72cd9054d49487c51bd757edd81ed41dcc8f838b`.

## 7. Original comparative YTD evidence (2026-09-12)

The subsequent slice retained and restored the original FY2025 10-Q filings
identified above (GOOG/TSLA fiscal Q2 and AAPL fiscal Q3), and compared their YTD duration
facts directly against the comparative prior-YTD figures presented in the
current filings for GOOG, TSLA, and AAPL.

### Bounded original-quarter acquisition and restore

Three directory observations were discovered and retained in 3 GET requests
returning 22,996 bytes, verified in:

- `artifacts/sec-prior-ytd-pilot/20260912T114648812140Z/report.json`, SHA-256
  `e8e0b7f062115a7b5311352c0c744d190997bffe7110976b1e6fa17cc42fad51`.

All three original-quarter productions completed full source retention,
financial production, bundle creation, and read-only restore parity under
existing caps (primary and instance <= 4 MiB, other sources <= 2 MiB, aggregate
sources <= 16 MiB, 60s timeout, 512 MiB RSS budget):

| Issuer | 10-Q accession | Rows | Retained source bytes | Seconds | Peak RSS bytes |
| --- | --- | ---: | ---: | ---: | ---: |
| GOOG | 0001652044-25-000062 | 184 | 7,131,410 | 13.7082 | 290,291,712 |
| TSLA | 0001628280-25-035806 | 224 | 4,496,507 | 19.8741 | 199,262,208 |
| AAPL | 0000320193-25-000073 | 178 | 2,933,069 | 17.3210 | 185,303,040 |

Registry binding each original quarter production identity and output hash:

- `artifacts/sec-prior-ytd-pilot/productions.json`, SHA-256
  `dd66a72978d4753bc6c39f466ed794e60cde61c5d63cb47e08e8ebc860bd0dec`.

### Comparative YTD direct matching and verification

While Section 6 recorded `NO_OVERLAP` between annual FY2025 and current filings
due to differing duration spans, this direct comparison of original FY2025 YTD
filings against the current filing's comparative prior-YTD figures supplies all
six duration overlaps.

The comparison restored both the original quarter productions and pinned
current productions under strict denial of network access and SnapshotStore
writes/observes. In 2.3638 seconds and 278,822,912 bytes peak RSS, all six
revenue and net-income metric pairs matched with exact value equality and zero
delta.

Values below are USD millions, displayed from exact retained USD Decimals:

| Issuer / metric | Concept | Period | Original 10-Q YTD | Current comparative YTD | Delta | Raw XML check | Primary IS excerpt |
| --- | --- | --- | ---: | ---: | ---: | --- | --- |
| GOOG revenue | `us-gaap_Revenues` | 2025-01-01–2025-06-30 | 186,662 | 186,662 | 0 | VERIFIED | FOUND (16,000 chars) |
| GOOG net income | `us-gaap_NetIncomeLoss` | 2025-01-01–2025-06-30 | 62,736 | 62,736 | 0 | VERIFIED | FOUND (16,000 chars) |
| TSLA revenue | `us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax` | 2025-01-01–2025-06-30 | 41,831 | 41,831 | 0 | VERIFIED | FOUND (16,000 chars) |
| TSLA common net income | `us-gaap_NetIncomeLoss` | 2025-01-01–2025-06-30 | 1,581 | 1,581 | 0 | VERIFIED | FOUND (16,000 chars) |
| AAPL revenue | `us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax` | 2024-09-29–2025-06-28 | 313,695 | 313,695 | 0 | VERIFIED | FOUND (16,000 chars) |
| AAPL net income | `us-gaap_NetIncomeLoss` | 2024-09-29–2025-06-28 | 84,544 | 84,544 | 0 | VERIFIED | FOUND (16,000 chars) |

All selectors use exact concepts without aliases or fallbacks:

- GOOG revenue uses `us-gaap_Revenues`; AAPL and TSLA use
  `us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax`.
- All net income metrics use `us-gaap_NetIncomeLoss`. TSLA's net income is on
  the reported common-stockholders basis, while GOOG and AAPL reflect
  consolidated net income as reported.
- Exact durations and calendar boundaries are preserved without inference:
  GOOG and TSLA cover `2025-01-01..2025-06-30`; AAPL covers its fiscal Q3 YTD
  period `2024-09-29..2025-06-28`.
- For all six original rows, strict raw XML instance correspondence was verified
  via `check_raw_instance_correspondence` against the replayed instance XML
  payloads (matching QName, contextRef, unitRef, dates, native decimal precision
  `-6`, and Decimal values).
- Primary income statement sections were bounded to 16,000 characters and
  verified with `HEADING_IS` and scale headers in both original and current HTML.

### Limits and pending qualifications

- **Diagnostic-only scope**: These equality findings demonstrate numerical
  consistency between original 10-Q YTD disclosures and subsequent comparative
  disclosures. They do not constitute a financial quality `PASS`, full
  accounting-basis qualification, or `MARKET_KNOWN` status.
- **No arithmetic or SDK changes**: No concept aliasing, synthetic facts, or
  TTM arithmetic formula changes were introduced.
- **Pending qualifications**: Annual accounting policies, adoption notes, and
  complete amendment/revision histories remain open. MSFT annual admission
  remains blocked by payload bounds.

### Test and comparison evidence

- Adapter discovery tests: 15 passed (`artifacts/sec-prior-ytd-pilot/test_pilot_adapter.py`).
- Comparison harness tests: 10 passed (`artifacts/sec-prior-ytd-pilot/test_compare.py`).
- Real comparison report:
  `artifacts/sec-prior-ytd-pilot/comparison-20260912T115918523520Z.json`, SHA-256
  `6407f885100d6813d16afdb15ab2593edc87e8bb8382980264ebc163d563103d`
  (2.3638s, 278,822,912 bytes peak RSS, strict 6 raw facts verified).

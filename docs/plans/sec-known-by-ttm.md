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

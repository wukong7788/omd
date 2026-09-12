# SEC required-field acceptance matrix (v1)

Status: FINITE PRIMARY-FIELD SCOPE ACCEPTED WITH DECLARED EXCEPTIONS; see [final acceptance](sec-financial-acceptance.md). No whole-production SDK quality PASS, global aliases or missing-as-zero.
Binds retained 10-Q accessions: AAPL `0000320193-26-000020`, MSFT `0001193125-26-191507`, TSLA `0001628280-26-049270`, GOOG `0001652044-26-000071`.

## 1. Scope, missing statuses, and query modes

Derived strictly from explicit plan recipe needs in [pit-data-production-and-event-refresh.md](pit-data-production-and-event-refresh.md), [sec-quarter-ttm-recipe.md](sec-quarter-ttm-recipe.md), and [sec-neutral-metric-graph.md](sec-neutral-metric-graph.md). Audits required statement roles and semantic inputs without imposing full XBRL taxonomy audits.

Availability follows the canonical contracts in [plan Section 3](pit-data-production-and-event-refresh.md) and the applicable SDK query API. Record source/version-bound availability evidence and its declared mode; do not infer first publication from an observation date or impose consumer-commit requirements on local known-by queries. Production types differ: AAPL uses `SecObservedFinancialProduction`; MSFT, TSLA, GOOG use `SecDocumentFinancialProduction`.

Missing-status taxonomy:
- `raw_source_absent`: Proven absence in both primary statement presentation and raw instance (tag absence alone is insufficient).
- `parser_missing`: Proven raw fact within declared scope/role omitted by adapter/parser. Other discrepancies remain unresolved/incomparable.
- `supplemental_note_scope`: Source facts exist in a note role outside the primary statement; supplemental production remains separate.
- `not_required`: Concept/term inapplicable to issuer's specific presentation or consolidation structure.
- `derived`: Output of a named, versioned transform with recorded provenance. GOOG gross profit is derived potential, currently `NOT_RUN`.
- `unresolved`: Evidence does not yet distinguish disclosure absence, parser omission or incompatible scope; keep the affected requirement open.
- `period_input_incomplete`: Multi-period recipe requirements (e.g. 4 quarters, FY+YTD) exceed retained single-filing data.

Diagnostic rule: Historical equity-only contexts outside primary balance sheet dates (e.g. MSFT 2025-03-31, GOOG 2024-12-31, 2025-03-31, 2025-06-30 and 2026-03-31 in equity statements) are diagnostic gaps in statement union matching, not missing required balance sheet fields.

## 2. Finite required-field matrix

For this consolidated sample contract, numeric facts require no segment dimensions, exact native units (`iso4217:USD`, `shares`, or divide `USD/shares`), matching primary reported statement periods, and valid accession. Period columns vary per statement: balance sheets are instant; income statements report 3M and YTD; cash flows may be YTD-only in interim 10-Qs (3M not mandated). Existing checks cite diagnostic evidence, not candidate field PASS. Preserve issuer/CIK, accession, statement role, exact QName/context, native unit and unit reference, value/precision, instant or duration with start/end, dimensions, source observation/version and production identity. Verify all required primary columns, not merely the existence of one matching concept.

| Statement / Need | Plan recipe need | Scope & Issuer variation | Required concepts & Unit | Status / Qualification | Canonical evidence |
| --- | --- | --- | --- | --- | --- |
| **(a) Mandatory Native** | | | | | |
| BS: Assets | BS balance; asset base | Consolidated primary BS dates | `us-gaap:Assets` [`iso4217:USD`] | Native fact required | [sec-document-accounting.md](sec-document-accounting.md) |
| BS: Liabilities | BS balance; leverage | Consolidated primary BS dates | `us-gaap:Liabilities` [`iso4217:USD`] | Native fact required | [sec-document-accounting.md](sec-document-accounting.md) |
| BS: Equity Scope | BS balance; equity ownership scope | Filing-specific equity scope | MSFT/GOOG: `StockholdersEquity`; TSLA: `StockholdersEquity` + `MinorityInterest` + `RedeemableNoncontrollingInterest...`; AAPL: see completed inventory in Section 5 | Filing-specific equality; no global alias | [sec-document-accounting.md](sec-document-accounting.md) |
| Cash Balance | Rollforward anchor | Period start/end (BS or CF anchor) | MSFT/GOOG: `...RestrictedCash...`; TSLA: `...IncludingDisposalGroup...`; AAPL: anchor qualification remains open [`iso4217:USD`] | Native fact required; anchors may be CF or BS | [sec-document-accounting.md](sec-document-accounting.md) |
| IS: Revenue | TTM Rev, Margin, YoY, PS | Consolidated duration (3M/YTD) | GOOG: `Revenues`; MSFT/TSLA: `RevenueFromContractWithCustomerExcludingAssessedTax`; AAPL: see completed inventory in Section 5 | Native fact required; contract revenue only if consolidated | [sec-document-accounting.md](sec-document-accounting.md) |
| IS: Net Income Scope | TTM NI, PE, Margin, YoY | Total economic vs common attributable | `NetIncomeLoss` vs common attributable; verify if preferred dividends/NCI present [`iso4217:USD`] | Native fact required; PE denominator depends on equity scope | [sec-quarter-ttm-recipe.md](sec-quarter-ttm-recipe.md) |
| CF: Operating Cash | CFO-CapEx, FCF, YoY | Consolidated duration (YTD/3M) | `NetCashProvidedByUsedInOperatingActivities` [`iso4217:USD`] | Native fact required | [sec-document-accounting.md](sec-document-accounting.md) |
| CF: Cash Totals | Cash bridge reconciliation | Operating, Investing, Financing, FX, Net Movement (YTD) | Operating, Investing, Financing, Net Change; FX when separately disclosed | Filing-specific terms; AAPL has a three-activity equation (Section 6); prior 8 checks MATCH | [sec-document-accounting.md](sec-document-accounting.md) |
| **(b) Conditional / Scope** | | | | | |
| IS: Cost of Revenue | Gross profit reconciliation | Matching revenue period | MSFT: `CostOfGoodsAndServicesSold`; TSLA/GOOG: `CostOfRevenue`; AAPL: see completed inventory in Section 5 | Required gross profit evidence | [sec-document-accounting.md](sec-document-accounting.md) |
| IS: Gross Profit | Gross margin recipe | Required if disclosed; derivation separate if absent | MSFT/TSLA: native `GrossProfit`; GOOG: `raw_source_absent` (derived potential `NOT_RUN`); AAPL: see completed inventory in Section 5 | Conditional on disclosure; no synthetic native fact | [sec-document-accounting.md](sec-document-accounting.md) |
| CF: CapEx | CFO-CapEx recipe | Declared CapEx concept; explicit sign | Required input for CFO-CapEx; strict declared sign (`POSITIVE_OUTFLOW` or `NEGATIVE_OUTFLOW`) | Required for CFO-CapEx; inventory completed in Section 5 | [sec-neutral-metric-graph.md](sec-neutral-metric-graph.md) |
| IS: EPS & Shares | Supplemental per-share check | Consolidated duration (3M/YTD) | Basic/Diluted EPS [`USD/shares`], Basic/Diluted Shares [`shares`]; shares may reside only in a note (Section 6); no exact EPS*shares equality assumed | Conditional supplemental source; not FPE forecast | [sec-financial-source-qualification.md](sec-financial-source-qualification.md) |
| **(c) Downstream / Bridges** | | | | | |
| External inputs | PE, PS, FPE valuation | External caller attestation | `COMPANY_MARKET_CAP`, `SECURITY_PRICE`, `FORECAST_EPS` | Lacks real PIT pipeline; external attestation only | [sec-neutral-metric-graph.md](sec-neutral-metric-graph.md) |
| Period bridges | TTM 4-quarter / FY+YTD | Multi-accession consecutive | Consecutive 4 quarters or FY+YTD duration sets | Real bridge coverage pending; classify missing required periods explicitly | [sec-quarter-ttm-recipe.md](sec-quarter-ttm-recipe.md) |

## 3. Baseline before the consolidated audit

The pending column below records the pre-audit baseline; Section 5 supersedes it.

| Issuer & Accession | Production Type | Verified Known Evidence | Filing-Specific Scope | Pending Bounded Inventory Checks |
| --- | --- | --- | --- | --- |
| **AAPL** `0000320193-26-000020` | `SecObservedFinancialProduction` | 180 rows in 3 presentation roles (60 each); 46 HTML cells match; 8 EPS divide units repaired. | 20 MATCH / 2 MISSING accounting checks. | Primary period dates; exact concept inventories for Revenue, NI, CapEx, Diluted Shares. |
| **MSFT** `0001193125-26-191507` (period 2026-03-31) | `SecDocumentFinancialProduction` | 257 rows match raw facts; 684-unit audit passed. | `RevenueFromContract...`, `CostOfGoodsAndServicesSold`, `GrossProfit` (4 MATCH); Cash rollforward (4 MATCH); Components (4 MATCH); Equity (2 MATCH, 1 gap). | Exact CapEx concept row in CF; Net Income vs Common Attributable; diluted shares row. |
| **TSLA** `0001628280-26-049270` (latest 2026-06-30) | `SecDocumentFinancialProduction` | 220 rows match raw facts; 684-unit audit passed. | `RevenueFromContract...`, `CostOfRevenue`, `GrossProfit` (4 MATCH); Cash disposal group (2 MATCH); Components (2 MATCH); Equity + 2 NCI (2 MATCH). | Exact CapEx concept row in CF; Net Income vs Common Attributable; diluted shares row. |
| **GOOG** `0001652044-26-000071` (latest 2026-06-30) | `SecDocumentFinancialProduction` | 207 rows match raw facts; 684-unit audit passed. | Consolidated `Revenues` (4 contexts); `GrossProfit` raw_source_absent; Cash rollforward (2 MATCH); Components (2 MATCH); Equity (2 MATCH, 4 gaps). | Exact CapEx concept row in CF; Net Income vs Common Attributable; diluted shares row. |

## 4. Executed bounded local audit plan

Execute an offline, read-only diagnostic script verifying uncovered candidate fields for the four retained accessions without re-running passed suites:

1. **Restricted scope & periods**: Target primary statement columns only:
   - BS instant: MSFT `2026-03-31` and comparative prior; TSLA/GOOG latest `2026-06-30` and comparative prior; AAPL exact primary dates from inventory. Exclude historical equity-only dates.
   - IS durations: 3M and YTD; CF durations: reported periods (YTD or 3M per statement).
2. **Production restoration**: Restore existing productions from local store with network and snapshot writes disabled: AAPL via observed production; MSFT/TSLA/GOOG via document production.
3. **Exact concept inventory**:
   - Inventory CapEx concepts in cash flows and evaluate explicit sign (`POSITIVE_OUTFLOW` vs `NEGATIVE_OUTFLOW`).
   - Distinguish total consolidated `NetIncomeLoss` and net income attributable to common shareholders.
   - Extract supplemental diluted shares and EPS rows; verify native units without assuming `EPS * shares` equality.
4. **Primary source corroboration**: Corroborate extracted values against primary filing tables and raw instances; classify gaps using strict missing taxonomy.
5. **Output contract**: Write a new timestamped immutable artifact `artifacts/sec-required-field-audit/audit-<TIMESTAMP>.json` (<=60s, <=512MiB RSS). No SDK edits, no data PASS.

## 5. Consolidated local audit (2026-09-12)

Restored all four retained productions, 864 rows total, and located all 12 primary
statement sections. The field/period inventory has 218 present candidates, 10 initially missing
slots (subsequently classified in Section 6) and four previously established GOOG gross-profit disclosure absences. Candidate
presence is not a quality PASS. Existing unit, cash-component and equity evidence is
reused from [the accounting record](sec-document-accounting.md).

- AAPL BS dates: 2025-09-27 and 2026-06-27. IS columns are three and nine months
  ending 2025-06-28 and 2026-06-27; CF has the two nine-month columns only.
  Exact IS starts are 2024-09-29, 2025-03-30, 2025-09-28 and 2026-03-29.
  Revenue is `RevenueFromContractWithCustomerExcludingAssessedTax`, cost is
  `CostOfGoodsAndServicesSold`, monetary gross margin is `GrossProfit`, net income
  is `NetIncomeLoss`, and equity is `StockholdersEquity`.
- All 10 declared CF periods contain `PaymentsToAcquirePropertyPlantAndEquipment`
  in USD with positive native outflow magnitudes; the primary tables display negative
  cash flows. AAPL amounts are 9,473 / 6,799 million; MSFT prior YTD/quarter and
  current YTD/quarter are 47,472 / 16,745 / 80,146 / 30,876 million; TSLA amounts
  are 3,886 / 8,282 million; GOOG amounts are 39,643 / 80,598 million.
  TSLA explicitly excludes finance leases and nets sales; this is not a universal
  gross-CapEx alias. CFO minus this declared positive outflow is the supported input
  convention; this audit did not run a new FCF recipe.
- TSLA `ProfitLoss - NetIncomeLossAttributableToNoncontrollingInterest = NetIncomeLoss`
  and GOOG `NetIncomeLoss - PreferredStockDividendsIncomeStatementImpact =
  NetIncomeLossAvailableToCommonStockholdersBasic` each yield four exact SDK MATCH
  checks. All three terms are retained; GOOG preferred-dividend zeros are actual
  facts. AAPL/MSFT primary tables present `NetIncomeLoss`; this does not establish
  a universal equivalence between total and common-attributable income.
- Independent primary-table transcription matches all 32 basic/diluted EPS values
  and 24 AAPL/MSFT/TSLA basic/diluted weighted-average share values. EPS uses exact
  USD/shares units; AAPL displayed thousands and MSFT/TSLA millions are expanded to
  native shares. No exact EPS-times-shares equality is assumed.
- GOOG's eight required share slots remain absent from the income-statement output.
  Raw instance inspection finds 48 occurrences: 36 dimensional and 12 undimensioned,
  including repeated basic facts, covering all four required periods. Thus this is
  **not raw-source absence**. The follow-up in Section 6 establishes a note-only presentation scope;
  neither class aggregation nor cross-role injection is accepted.
- AAPL's two FX slots were initially unresolved; Section 6 establishes separate-line
  disclosure absence. No zero is inserted. GOOG's four
  gross-profit absences retain the prior source-qualified status; derivation is NOT_RUN.

The retained IS periods provide only two independent reported quarters per issuer,
not four consecutive quarters. All four also lack FY2025 input for FY + current YTD
minus prior YTD. Required FY intervals are AAPL 2024-09-29 through 2025-09-27,
MSFT 2024-07-01 through 2025-06-30, and TSLA/GOOG calendar 2025. Cash-flow quarterly
bridges require additional YTD boundaries except MSFT's already reported quarters.
These are `period_input_incomplete`, not failed arithmetic or synthesized values.

Local evidence (ignored provider artifacts, not distributed in Git):

| Artifact under `artifacts/sec-required-field-audit/` | SHA-256 |
| --- | --- |
| `audit-20260912T072214023267Z.json` | `56be7b06f8835e8672135e18bdebd0e418e8853a6be1bb2325f00dc35b1b7fe4` |
| `verification-20260912T072819584025Z.json` | `ebce42a8e1e02601ddfd7bc765f7ac5371ae49ff60b4f92c8b37a17e2c84d125` |
| `primary-supplement-20260912.json` | `9ccf7ca786809779971ff75d901c9e6af1a358e81b740c48ea2f65af4c81cb78` |

Final local harness regression checks: 14 passed. Verification ran offline in 1.63s,
224.3 MiB RSS, with snapshot writes disabled. No SDK change, consumer rerun, new
fetch or release was performed. NVDA remains deferred. The follow-up role/source classification is complete in Section 6. Historical
period fetching and consumer shadow evidence remain separate acceptance work.


## 6. GOOG share roles and AAPL FX classification (2026-09-12)

Both causes are resolved for these retained accessions. No SDK defect was established,
so the primary-statement parser and sealed outputs are unchanged.

**GOOG supplemental shares:** The exact presentation role
`http://www.google.com/role/CONSOLIDATEDSTATEMENTSOFINCOME` has 16 arcs and neither
weighted-average share concept. Both concepts occur exclusively in
`http://www.google.com/role/NetIncomePerCommonShareDetails`. The primary table displays
basic/diluted EPS referencing Note 12 and no share-count rows. The eight absent
primary-output slots therefore have `supplemental_note_scope`, not `parser_missing`
or `raw_source_absent`. This classification closes the primary-statement omission
investigation; it does not claim that a reusable supplemental-note API is implemented.

Raw facts cover all four required periods. There are 12 undimensioned occurrences:
for each period two identical basic facts and one diluted fact, plus 36 dimensional
occurrences across stock classes. Duplicate basic occurrences are not additional
shares. No summing across classes or inserting note facts into the primary statement
was performed. A future supplemental extraction must retain note role, exact context,
units, duplicate/conflict policy and production provenance separately.

**AAPL separate FX line:** The primary CF presentation role contains 34 unique concepts
and no separate FX term. Raw-instance inspection found none of the four examined
standard FX concepts or other exchange-rate-named fact candidates; primary-table review
also found no separate FX line. The calculation linkbase explicitly connects net cash
movement to operating, investing and financing totals, each with weight +1.
Thus `raw_source_absent` describes the separately disclosed FX input in this filing;
it is not required by this filing-specific three-activity equation. This makes no
claim that the company's economic FX effect is zero.

Independent checks matched the manually inspected primary amounts to all eight exact
USD output rows and verified both equations (USD millions):

- Prior nine months: `81,754 + 17,782 - 93,210 = 6,326`.
- Current nine months: `116,996 - 18,811 - 94,575 = 3,610`.

Both residuals are zero. These are local evidence checks, not new SDK quality PASS
records. The earlier generic five-term diagnostic remains historical evidence of a
missing term; it is not rewritten or filled with zero.

Local evidence under ignored `artifacts/sec-output-gap-audit/`:

| Artifact | SHA-256 |
| --- | --- |
| `audit-gap-20260912T074940036323Z.json` | `810f35e89bc8f087d423e9bd0ae12ad9cda5444bc52e3f883c5553872ab2fe8b` |
| `review-20260912T074951434413Z.json` | `81547584ed4c9b70fee69187828b73abf978ddae52d11e601cdbf3ba8236b40c` |

The diagnostic restores the existing sealed productions with network and snapshot
writes disabled, pins the prior batch SHA, and records source receipts and role
results. Final run: 1.56s / 258.5 MiB RSS. Independent receipt/role/period/value assertions
and two cash equations passed; focused Ruff F checks passed. No SDK change or consumer
migration requires a rerun in this step. Cross-filing periods, supplemental-note
production, consumer shadow and overall data-quality acceptance remain open.


## 7. Final finite-scope acceptance (2026-09-12)

All 232 declared field/period positions are classified: 218 verified primary facts,
eight GOOG note-only shares, four undisclosed GOOG native GrossProfit positions,
and two undisclosed AAPL separate FX positions. Fifty-eight selected equalities
were independently recomputed, including the two AAPL cash balance bridges.
The six selected TTM basis inputs and diagnostic anomaly disposition are also
reviewed. [The final acceptance](sec-financial-acceptance.md) owns the precise
scope, exclusions and immutable receipt. Historical missing records above remain
unchanged; the acceptance does not supply excluded values or qualify every row.
The Section 5 FY2025-source absence is historical: three issuers now have annual
sources; MSFT annual admission and other period histories remain separate.

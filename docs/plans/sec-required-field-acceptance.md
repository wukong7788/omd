# SEC required-field acceptance matrix (v1)

Status: INITIAL ACCEPTANCE CONTRACT (Scope frozen; no quality PASS; no global aliases; no missing-as-zero).
Binds retained 10-Q accessions: AAPL `0000320193-26-000020`, MSFT `0001193125-26-191507`, TSLA `0001628280-26-049270`, GOOG `0001652044-26-000071`.

## 1. Scope, missing statuses, and query modes

Derived strictly from explicit plan recipe needs in [pit-data-production-and-event-refresh.md](pit-data-production-and-event-refresh.md), [sec-quarter-ttm-recipe.md](sec-quarter-ttm-recipe.md), and [sec-neutral-metric-graph.md](sec-neutral-metric-graph.md). Audits required statement roles and semantic inputs without imposing full XBRL taxonomy audits.

Availability follows the canonical contracts in [plan Section 3](pit-data-production-and-event-refresh.md) and the applicable SDK query API. Record source/version-bound availability evidence and its declared mode; do not infer first publication from an observation date or impose consumer-commit requirements on local known-by queries. Production types differ: AAPL uses `SecObservedFinancialProduction`; MSFT, TSLA, GOOG use `SecDocumentFinancialProduction`.

Missing-status taxonomy:
- `raw_source_absent`: Proven absence in both primary statement presentation and raw instance (tag absence alone is insufficient).
- `parser_missing`: Proven raw fact within declared scope/role omitted by adapter/parser. Other discrepancies remain unresolved/incomparable.
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
| BS: Equity Scope | BS balance; equity ownership scope | Filing-specific equity scope | MSFT/GOOG: `StockholdersEquity`; TSLA: `StockholdersEquity` + `MinorityInterest` + `RedeemableNoncontrollingInterest...`; AAPL: pending inventory | Filing-specific equality; no global alias | [sec-document-accounting.md](sec-document-accounting.md) |
| Cash Balance | Rollforward anchor | Period start/end (BS or CF anchor) | MSFT/GOOG: `...RestrictedCash...`; TSLA: `...IncludingDisposalGroup...`; AAPL: pending inventory [`iso4217:USD`] | Native fact required; anchors may be CF or BS | [sec-document-accounting.md](sec-document-accounting.md) |
| IS: Revenue | TTM Rev, Margin, YoY, PS | Consolidated duration (3M/YTD) | GOOG: `Revenues`; MSFT/TSLA: `RevenueFromContractWithCustomerExcludingAssessedTax`; AAPL: pending inventory | Native fact required; contract revenue only if consolidated | [sec-document-accounting.md](sec-document-accounting.md) |
| IS: Net Income Scope | TTM NI, PE, Margin, YoY | Total economic vs common attributable | `NetIncomeLoss` vs common attributable; verify if preferred dividends/NCI present [`iso4217:USD`] | Native fact required; PE denominator depends on equity scope | [sec-quarter-ttm-recipe.md](sec-quarter-ttm-recipe.md) |
| CF: Operating Cash | CFO-CapEx, FCF, YoY | Consolidated duration (YTD/3M) | `NetCashProvidedByUsedInOperatingActivities` [`iso4217:USD`] | Native fact required | [sec-document-accounting.md](sec-document-accounting.md) |
| CF: Cash Totals | Cash bridge reconciliation | Operating, Investing, Financing, FX, Net Movement (YTD) | Disclosed 5 totals: Operating, Investing, Financing, FX, Net Change | Native facts required; 8 diagnostic checks MATCH | [sec-document-accounting.md](sec-document-accounting.md) |
| **(b) Conditional / Scope** | | | | | |
| IS: Cost of Revenue | Gross profit reconciliation | Matching revenue period | MSFT: `CostOfGoodsAndServicesSold`; TSLA/GOOG: `CostOfRevenue`; AAPL: pending inventory | Required gross profit evidence | [sec-document-accounting.md](sec-document-accounting.md) |
| IS: Gross Profit | Gross margin recipe | Required if disclosed; derivation separate if absent | MSFT/TSLA: native `GrossProfit`; GOOG: `raw_source_absent` (derived potential `NOT_RUN`); AAPL: pending inventory | Conditional on disclosure; no synthetic native fact | [sec-document-accounting.md](sec-document-accounting.md) |
| CF: CapEx | CFO-CapEx recipe | Declared CapEx concept; explicit sign | Required input for CFO-CapEx; strict declared sign (`POSITIVE_OUTFLOW` or `NEGATIVE_OUTFLOW`) | Required for CFO-CapEx; inventory pending | [sec-neutral-metric-graph.md](sec-neutral-metric-graph.md) |
| IS: EPS & Shares | Supplemental per-share check | Consolidated duration (3M/YTD) | Basic/Diluted EPS [`USD/shares`], Diluted Shares [`shares`]; no exact EPS*shares equality assumed | Conditional supplemental source; not FPE forecast | [sec-financial-source-qualification.md](sec-financial-source-qualification.md) |
| **(c) Downstream / Bridges** | | | | | |
| External inputs | PE, PS, FPE valuation | External caller attestation | `COMPANY_MARKET_CAP`, `SECURITY_PRICE`, `FORECAST_EPS` | Lacks real PIT pipeline; external attestation only | [sec-neutral-metric-graph.md](sec-neutral-metric-graph.md) |
| Period bridges | TTM 4-quarter / FY+YTD | Multi-accession consecutive | Consecutive 4 quarters or FY+YTD duration sets | Real bridge coverage pending; classify missing required periods explicitly | [sec-quarter-ttm-recipe.md](sec-quarter-ttm-recipe.md) |

## 3. Retained issuer evidence: verified knowns vs pending inventory checks

| Issuer & Accession | Production Type | Verified Known Evidence | Filing-Specific Scope | Pending Bounded Inventory Checks |
| --- | --- | --- | --- | --- |
| **AAPL** `0000320193-26-000020` | `SecObservedFinancialProduction` | 180 rows in 3 presentation roles (60 each); 46 HTML cells match; 8 EPS divide units repaired. | 20 MATCH / 2 MISSING accounting checks. | Primary period dates; exact concept inventories for Revenue, NI, CapEx, Diluted Shares. |
| **MSFT** `0001193125-26-191507` (period 2026-03-31) | `SecDocumentFinancialProduction` | 257 rows match raw facts; 684-unit audit passed. | `RevenueFromContract...`, `CostOfGoodsAndServicesSold`, `GrossProfit` (4 MATCH); Cash rollforward (4 MATCH); Components (4 MATCH); Equity (2 MATCH, 1 gap). | Exact CapEx concept row in CF; Net Income vs Common Attributable; diluted shares row. |
| **TSLA** `0001628280-26-049270` (latest 2026-06-30) | `SecDocumentFinancialProduction` | 220 rows match raw facts; 684-unit audit passed. | `RevenueFromContract...`, `CostOfRevenue`, `GrossProfit` (4 MATCH); Cash disposal group (2 MATCH); Components (2 MATCH); Equity + 2 NCI (2 MATCH). | Exact CapEx concept row in CF; Net Income vs Common Attributable; diluted shares row. |
| **GOOG** `0001652044-26-000071` (latest 2026-06-30) | `SecDocumentFinancialProduction` | 207 rows match raw facts; 684-unit audit passed. | Consolidated `Revenues` (4 contexts); `GrossProfit` raw_source_absent; Cash rollforward (2 MATCH); Components (2 MATCH); Equity (2 MATCH, 4 gaps). | Exact CapEx concept row in CF; Net Income vs Common Attributable; diluted shares row. |

## 4. Next executable bounded local audit plan

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

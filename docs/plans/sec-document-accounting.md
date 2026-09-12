# SEC document accounting diagnostics

`evaluate_sec_document_accounting(production, rules, *, detected_at, recorded_at,
max_rules=64, max_rows=10000)` evaluates declared accounting equalities against exactly
one sealed `SecDocumentFinancialProduction`. It performs no I/O and creates no quality
finding, PASS, availability assertion, normalized ID, consumer commit, or financial
approval. A `MATCH` remains only an arithmetic diagnostic.

The entry point accepts the document-production type exactly. Before rule evaluation it
limits accounting input to 10,000 rows, charges the retained production through the
document replay admission budget (32 MiB, bounded nodes and depth), runs the existing
structural row validator on every row, and copies then revalidates the document seal.
The recomputed production identity must equal the caller value. Tampered nested source,
vintage, output receipt, production identity, malformed row, or future detection fails;
the caller object is never repaired. `detected_at` is no earlier than both production and
output observation time, and `recorded_at` is no earlier than detection.

Rules, statuses, tolerance policies, row selection, duplicate handling, precision
allowances, SAME_CONTEXT and CASH_ROLLFORWARD checks, evidence limits, and report-wire
limits are the exact shared observed-accounting implementation. No new accounting rule
or availability interpretation is introduced here. Missing values and absent rows remain
`MISSING`; unit, context, period, duplicate, and precision conflicts remain
`INCOMPARABLE`; missing data is never converted to zero.

The result intentionally reuses `SecObservedAccountingReport`. Its identity binds only
the supplied document production identity, its exact output observation and fact-version
identities, parser/configuration identities, input row count, diagnostic times, and
checks. This reuse does not admit `SecObservedFinancialProduction`, invoke the old
producer factory, or claim that the two production domains are interchangeable.

Acceptance: independent Astra review accepted the implementation after 34 combined
accounting tests passed. A separately loaded pre-change HEAD implementation generated
the same legacy report bytes and identity as the refactored implementation; the local
evidence is `artifacts/sec-document-accounting-acceptance/report.json`. Actual parsed
synthetic document rows cover MATCH, MISMATCH, missing terms, and incompatible context,
unit, and precision diagnostics. Real-source and consumer acceptance remain separate.

## Selected retained-source diagnostic run

On 2026-09-12, an ignored offline harness restored the exact accepted MSFT,
TSLA and GOOG productions and evaluated three explicit SAME_CONTEXT rule
variants under EXACT tolerance and expected unit `iso4217:USD`:

- Assets minus Liabilities minus
  StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest.
- GrossProfit minus Revenues plus CostOfRevenue.
- GrossProfit minus RevenueFromContractWithCustomerExcludingAssessedTax plus
  CostOfRevenue, reported separately without an automatic fallback.

Every anchor context is retained, including contexts with absent terms; more
than 64 rules fails explicitly. Six local `uv` harness tests passed, including
rule overflow, missing anchors, mutation denial and diagnostic-time validation.
Network and snapshot writes were denied during restoration and evaluation.
Diagnostics use the actual run timestamp, not the earlier production timestamp.
Initial reports using production-time detection were retained as superseded
evidence and were not used for the final acceptance below.

| Production | MATCH | MISSING | MISMATCH | INCOMPARABLE |
| --- | ---: | ---: | ---: | ---: |
| MSFT | 0 | 10 | 0 | 0 |
| TSLA | 4 | 6 | 0 | 0 |
| GOOG | 0 | 10 | 0 | 0 |

All four TSLA matches are the alternate-revenue gross-profit rule, and their
signed arithmetic was separately recomputed from retained term evidence.
MSFT uses CostOfGoodsAndServicesSold rather than the selected CostOfRevenue
concept; GOOG's emitted rows lack GrossProfit. All three lack the selected
equity-including-noncontrolling-interest concept. These are rule-coverage gaps,
not proof that source statements omit amounts. No alias substitution, inferred
zero, financial-quality PASS, quality finding or consumer commit was created.
Existing AAPL evidence (20 MATCH, 2 MISSING across different selected rules)
was reused after checking its report/production identities; it was not rerun.

Each new process completed below 2 seconds and 245MiB sampled peak RSS, under
the 60-second/512MiB harness budget. Reports retain rules, row ordinals, term
values, residuals, missing reasons and available concept inventories. Local
directory: `artifacts/sec-real-accounting-checks/`.

| Final report | SHA-256 |
| --- | --- |
| `report-MSFT-20260912T051510067878Z.json` | `238a75e74a7c43e681ca2969922b2c472db9c8a6d242a0b919bb419f4c0dda91` |
| `report-TSLA-20260912T051511519453Z.json` | `1f0021111847a71f7977941a5a227f7c2959c41567b17f9db759e33481756e41` |
| `report-GOOG-20260912T051512336850Z.json` | `89a6340157994e66653136dc7c8484f6293d1004f692efc88d52a442e84cfbaa` |

Review accepts selected arithmetic diagnostics only. Complete financial quality,
explicit additional concept variants, raw normalized-unit verification and cash
rollforward completeness remain separate work.

## Filing-scoped revenue and gross-profit evidence

The 2026-09-12 follow-up compared retained raw instance QNames, emitted concepts,
context references and primary-statement text for the exact GOOG and MSFT
productions above. Network and snapshot writes were denied. No global concept
alias, normalized value, financial PASS or parser change was introduced.

For GOOG accession `0001652044-26-000071`, the original income statement lists
revenues, cost of revenues and operating expenses without a gross-profit
subtotal. The retained instance contains zero `us-gaap:GrossProfit` facts, as
does the output. Thus this missing selected check is not evidence of a parser
omission. `us-gaap:Revenues` supplies the four emitted consolidated revenue
facts. All 60 raw `RevenueFromContractWithCustomerExcludingAssessedTax` facts
carry explicit product/service or business-segment dimensions; none uses the
four consolidated revenue contexts. They must not substitute for consolidated
revenue. Revenue less cost may support a separately identified derived amount,
but is not a disclosed gross-profit fact or an independent equality check.

For MSFT accession `0001193125-26-191507`, primary-statement total revenue maps
to the retained `RevenueFromContractWithCustomerExcludingAssessedTax` facts,
total cost of revenue to `CostOfGoodsAndServicesSold`, and the monetary subtotal
labeled “Gross margin” to `GrossProfit`. Here “Gross margin” is an amount, not a
percentage. The four consolidated income contexts have matching periods and
USD units. A new rule scoped to this exact production and accession evaluates
GrossProfit minus RevenueFromContractWithCustomerExcludingAssessedTax plus
CostOfGoodsAndServicesSold. All four checks are MATCH with zero residual and
EXACT zero tolerance. The existing generic checks and their missing results
remain preserved; these are four additional checks, not replacements.

This establishes a filing-specific relationship, not universal equivalence of
`Revenues` with customer-contract revenue, or `CostOfRevenue` with
`CostOfGoodsAndServicesSold`. Any broader mapping requires evidence for period,
consolidation scope, recognition/tax basis and units. Numerical agreement alone
does not establish those semantics.

Local evidence directory: `artifacts/sec-concept-semantic-audit/`.

| Evidence | SHA-256 |
| --- | --- |
| `audit-report.json` | `2ee7fb859fb78aa538cbe3116713a9bc3ee04c2497c8ec670a57a1c793f41614` |
| `goog-revenue-contexts.json` | `c13627be96315e4598b3bc1a058b36d19eb762155b0bef1d84f40fa1fd8fdd8b` |
| `msft-accounting-check-20260912T055903220595Z.json` | `5880a2fd0ccdf1767bf09eb177244d12b53065b7a23382a823f7924a2cdf5eba` |

The extraction smoke check and static undefined-name checks passed locally;
the scoped SDK evaluation restored the exact production and retained full term
evidence with current diagnostic times. Review accepted these bounded findings.

## Retained-source unit and cash verification

On 2026-09-12 an independent offline unit parser resolved namespace-qualified
raw XBRL measures, including divide units, and compared them with every emitted
row from the exact MSFT, TSLA and GOOG productions above. Instance receipts
were checked against the sealed source manifest. All 684 rows matched:
MSFT 257, TSLA 220 and GOOG 207; no unit mismatch or missing unit metadata was
found. The emitted units cover USD, shares and USD per share. Other declared
but unused units are inventoried without claiming emitted-row coverage.

Eight synthetic harness tests passed, covering namespace aliases/rebinding,
foreign measures, divide units, duplicate IDs, missing metadata, DTD rejection,
resource limits and conservative cash inventory. The final unit report is
`artifacts/sec-unit-cash-audit/audit-summary-20260912T061706326033Z.json`,
SHA-256 `57a86121e4758cf4a00cba20ea977d2c5b2ff159a7eb8d4f00fb66f5a9a7a3de`.
This verifies unit representation for these emitted rows, not completeness of
all source facts or financial-quality approval.

The cash follow-up compared retained primary cash-flow statements with selected
instant balances and the disclosed net movement including exchange-rate effects.
Eight EXACT CASH_ROLLFORWARD checks matched with zero residual and zero
tolerance: MSFT four periods, TSLA two and GOOG two. Each rule selects the
period end and the day before the duration start, requires unique nondimensional
USD anchors, and binds its scope to the accession, production and primary-file
hash. All eight signed sums were independently recomputed from report evidence.

MSFT and GOOG use CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents
for balances. TSLA uses the IncludingDisposalGroupAndDiscontinuedOperations
balance concept: its primary cash-flow statement and restricted-cash note
explicitly identify the corresponding totals. This relationship is accepted
only for this retained TSLA filing; it is not a global concept alias. All three
use the disclosed CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents
PeriodIncreaseDecreaseIncludingExchangeRateEffect movement concept. No cash
component was inferred as zero or replaced with another cash definition.

Sixteen combined synthetic harness tests and Ruff undefined-name checks passed.
The final cash report is
`artifacts/sec-unit-cash-audit/cash-check-20260912T062149067966Z.json`,
SHA-256 `81e027ae67a7c2decb13cd329dd1be4c480bc54025d318161be279562d36fda1`.
It binds the final unit report above, primary receipts/snippets and full SDK
term evidence. The cash process used 2.26 seconds and 245.77 MiB peak RSS;
unit restoration stayed below 1.35 seconds per production and 273 MiB peak RSS.
Both ran offline under the 60-second/512-MiB budget with snapshot writes denied.

Review accepts these selected unit and cash-balance diagnostics. Operating,
investing, financing and FX component completeness, broader financial-quality
acceptance, and consumer acceptance remain separate. No SDK behavior, quality
PASS, consumer data or publication changed; consumer reruns and migration
documentation are not required for this evidence-only update.

## Retained cash component totals

The 2026-09-12 follow-up evaluated net movement minus operating, investing,
financing and exchange-rate effects against the same retained productions.
All eight SAME_CONTEXT EXACT USD checks matched with zero residual and
tolerance: MSFT four periods, TSLA two and GOOG two. Signed values were
preserved, including negative investing/financing and FX amounts. Each of the
eight sums was independently recomputed from the full retained term evidence.
Primary cash-flow statement snippets were reviewed for all five displayed
totals and their period columns; arithmetic agreement alone was not acceptance.

The three activity concepts are NetCashProvidedByUsedInOperatingActivities,
NetCashProvidedByUsedInInvestingActivities and
NetCashProvidedByUsedInFinancingActivities. GOOG selects
EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents;
MSFT and TSLA select that concept with
IncludingDisposalGroupAndDiscontinuedOperations appended. These are explicit
filing-scoped selections, not global aliases. The disclosed net-movement
concept is the same one used in the preceding cash-balance checks.

Rules retain all five required terms and anchor contexts from their union,
so missing net movement or FX is not silently skipped or treated as zero.
Units are left for SDK comparability checks rather than filtered away. Five
focused harness tests passed for signs, missing FX/net anchors, dimensions
and unknown symbols; Ruff undefined-name checks passed. This evidence-only
change does not alter the SDK or require consumer reruns or migration docs.

Final local evidence:
`artifacts/sec-cash-components-audit/cash-components-20260912T064452850547Z.json`,
SHA-256 `727bf2bf067fac6a160bcb75fa510ab5fd669042153fbea7ba93d18d52868317`.
It binds the preceding cash report by hash, exact production/output identities,
primary receipts and full SDK diagnostics. The offline process completed in
2.21 seconds with 244.83 MiB peak RSS, under its 60-second/512-MiB budget;
network and snapshot writes were denied.

Review accepts the selected top-level cash component reconciliation. It does
not establish completeness of the detailed rows within operating, investing
or financing sections, or overall financial-quality PASS. Earlier deferred
component-total work is resolved for these eight periods only.

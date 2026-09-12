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

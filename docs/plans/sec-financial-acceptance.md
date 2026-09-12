# SEC finite financial acceptance (2026-09-12)

Status: ACCEPTED for the frozen scope below, with explicit unavailable outputs.
This closes the selected basis review and required-field financial-quality work;
it does not grant whole-production quality, market-known eligibility or consumer use.

## Scope and decision

The six FY2025 + current FY2026 YTD - comparative FY2025 YTD revenue/net-income
inputs for GOOG, TSLA and AAPL are accepted on a scoped **as-reported basis**.
The acceptance records exact fiscal intervals, concepts, attribution, source and
production identities, review time, and each issuer's retained revision-inventory
cutoff. All six original-vs-comparative prior-YTD values match exactly.
This is a reviewed no-recast rationale for these inputs, not an assertion that
no later restatement can occur or a reconstruction of every period on a future
common-restated basis. Existing TTM values are unchanged.

- GOOG: current TPU sales recognition, return allowances and multiple performance
  obligations remain explicit; new transactions are not assumed to have zero
  revenue impact. Acquisition results, intangible amortization, held-for-sale
  depreciation changes, investment gains and taxes remain in reported income.
  The reviewed disclosures and matched comparative totals support retaining the
  selected FY2025 amounts without a retrospective adjustment.
- TSLA: crypto-asset recasting concerns 2024 comparisons; both selected 2025 inputs
  use the adopted basis. The 2026 credit-loss adoption states no statement impact.
  Tax-law, valuation-allowance and digital-asset effects remain in their reported
  periods. Net income retains the common-stockholders attribution, with the
  ProfitLoss/NCI reconciliation preserved. Revenue remains the disclosed total,
  including lease components, not a universal customer-contract-only alias.
- AAPL: retain its exact fiscal dates and 52-week years. Annual policy comparison,
  segment-disclosure adoption, reclassification review and matched comparative
  totals support the selected consolidated amounts. Tax effects stay in their
  reported periods; no recurring-earnings adjustment is introduced.

The two selected TSLA annual amendments were reviewed as Part III, cover-reference
and certification changes; the source details remain in
[the policy evidence](sec-known-by-ttm.md#9-referenced-annual-policies-and-selected-income-components-2026-09-12).
No Item 4.02 metadata match was found in the selected inventory. Its oldest recent
rows precede the target window, and all advertised older files end before that
window. This closes the **retained SEC-reported inventory** check for the selected
dates, not an independent completeness guarantee for every SEC archive.

| Issuer | Inventory window starts | Oldest retained recent filing | Inventory snapshot (UTC) |
| --- | --- | --- | --- |
| GOOG | 2025-02-05 | 2023-06-29 | 2026-09-11T16:34:11.709890+00:00 |
| TSLA | 2025-01-30 | 2018-05-07 | 2026-09-11T16:34:12.521858+00:00 |
| AAPL | 2024-11-01 | 2015-07-24 | 2026-09-11T16:34:07.197133+00:00 |

The acceptance was recorded on 2026-09-12 after reviewing the retained evidence.
The earlier inventory snapshot times are not the acceptance's knowledge time;
newly acquired policy evidence is never backdated.

## Required-field quality and anomaly disposition

The current AAPL/MSFT/TSLA/GOOG filings and exact field contracts are those in
[the required-field matrix](sec-required-field-acceptance.md). All 232 declared
field/period positions are accounted for:

| Classification | Positions | Consumption consequence |
| --- | ---: | --- |
| Verified primary facts | 218 | Accepted only for the declared primary periods, concepts and units |
| GOOG shares in supplemental note scope | 8 | Primary product does not supply them; supplemental extraction remains separate |
| GOOG native GrossProfit not disclosed | 4 | No native fact or automatic derived substitute supplied |
| AAPL separate FX not disclosed | 2 | Not a term of the reviewed three-activity equation; economic FX is not assumed zero |

There are no unclassified gaps within this frozen matrix. Independent checks tie
all 218 values/context references to the earlier verification and actual emitted
row ordinals. Missing required rows, wrong periods/units, nonfinite values and
conflicting duplicates fail; equivalent duplicates retain their ordinals.
The 14 exceptions are accepted classifications, not invented values or fulfilled
supplemental output requirements.

Fifty-eight selected signed accounting equalities were independently recomputed
with zero residual: 20 AAPL checks, eight other-issuer cash balance bridges,
eight cash component equations, six primary balance-sheet equity equations,
eight net-income attribution equations and eight MSFT/TSLA gross-profit equations.
AAPL's two beginning/ending cash checks are separate from its two three-activity
sums. Its two missing explicit-FX checks and five historical equity-only context
gaps in the other issuers remain preserved outside applicable primary checks.
Existing raw-instance, unit and primary-table evidence is reused; this is not an
audit of every detailed line, note, forecast or financial assertion.

The named local anomaly policy checks the six revenue/net-income YTD pairs for
sign change, nonpositive prior value, or absolute change above 50% of the prior
magnitude. The threshold is a review trigger, not an economic-normality standard.
One flag is triggered: GOOG net income. An append-only OPEN record and a separate
review disposition retain the evidence and original values. The investment note's
USD 135,946 million equity-securities gains support a major contributor to the
increase; they include measurement-alternative adjustments, are not all
mark-to-market gains, and are not the whole other-income total or an exact
attribution of the entire net-income change. No source/parser error or correction
was established by this flag. No original fact was overwritten or zero-filled.

## Validation and retained receipt

Eighteen focused checks passed, including independent wrong-divide-unit,
Infinity/NaN, zero-baseline, missing-period, conflicting-duplicate, deleted-check
and changed-term regressions. Twenty-four policy excerpts were independently
matched to source note hashes and character offsets. Ruff checks and formatting
passed. The final record pins inputs, helpers, classifications and review times.

Ignored local receipt:

- `artifacts/sec-final-financial-acceptance/20260912T135604661943Z/acceptance.json`
- SHA-256: `cdb53e653f332ece5a78d58f32af51f825b49ff4b2a523b12b7a61cb74b825ad`

The sibling `verification.json`, `anomaly-open.json` and `anomaly-resolution.json`
are hash-linked by that receipt. Draft Gemini narrative proposals are superseded
by its analyst-reviewed rationales. No provider payload or local helper is added
to Git. The harness is task-local; no new SDK quality-policy API is claimed.

Additional external AGY review of the final analyst record did not run because
its automatic approval review rejected transmission of those specific files.
Acceptance uses the documented local independent checks and Astra review; no
additional external-review acceptance is claimed.

## Separate gates

Whole-production SDK quality records were not created: the finite policy cannot
approve every row in a production. MARKET_KNOWN/publication evidence, consumer
shadow and commit, MSFT FY2025 source admission, NVDA, GOOG supplemental shares,
other recipes/period histories and broader multi-dataset quality remain separate.
No consumer rerun or migration is required for this evidence-only acceptance.

# SEC Known-By TTM Diagnostic

## 1. Goal & Boundaries
This module provides `diagnose_sec_fy_ytd_ttm`, an offline public diagnostic function for evaluating FY+YTD TTM arithmetic directly over sealed `SecObservedFinancialProduction` and `SecDocumentFinancialProduction` objects.

- **Diagnostic-Only**: Operates strictly on local observed known-by facts. It does not issue financial quality `PASS` marks, perform PIT quality/commit gating, or fabricate `SecNormalizedFinancialFactVersion` records.
- **No Publication Claim**: Evaluates inputs using observation timestamps (`max(vintage.known_by_at, produced_at, output_observation.snapshot_fetched_at)`) as availability bounds; does not assert SEC acceptance or market first publication.
- **Scope & Coverage**: Currently verified against synthetic fixtures; real-issuer production replay (e.g. AAPL, MSFT, GOOG, TSLA) remains gated on retaining required full-year productions (FY2025 prior FY for current FY2026 YTD targets).

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

Real FY2025 input is still absent for the four retained FY2026 targets. Next work
is bounded FY2025 source acquisition, source/period/comparability qualification,
and real diagnostic execution. Full financial quality, MARKET_KNOWN eligibility,
consumer shadow and publication are not granted by this slice.

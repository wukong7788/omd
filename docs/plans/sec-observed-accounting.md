# Observed SEC accounting diagnostics

Status: implemented; bounded offline acceptance and independent review passed.

`evaluate_sec_observed_accounting(production, rules, *, detected_at, recorded_at,
max_rules=64, max_rows=10000)` evaluates one existing sealed observed production.
It performs no I/O, creates no normalized IDs, quality findings, PASS or consumer
commits, and does not establish publication or financial correctness.

Frozen `SecAccountingTerm` selects exact statement_type/concept/context_ref with
coefficient +1 or -1. Frozen `SecAccountingRule` contains rule_id, applicability,
2..8 unique terms, expected_unit, tolerance_policy and scope_reference; its
identity binds every field. SAME_CONTEXT requires identical contexts, periods
and dimensions. CASH_ROLLFORWARD requires three ordered terms (ending cash,
beginning cash, reported change), signs +1/-1/-1, identical cash concepts,
instant/instant/duration periods, ending date equal to duration end and beginning
date plus one day equal to duration start. The caller explicitly declares the
cash-change definition and accounting completeness; FX is never assumed zero.

EXACT uses zero tolerance. ASSUME_NEAREST_REPORTED_DECIMALS explicitly assumes
nearest rounding and sums each term's half reported unit; native `INF` contributes
zero. This is a caller assumption, not a source-certified rounding convention.
Missing/malformed precision gives INCOMPARABLE. All signed sums use bounded
integer-scaled Decimal arithmetic independent of ambient precision, with at most
10,000 coefficient digits, exponent/adjusted exponent/common-scale span including
carry. Bounds are checked before exponentiation.
Admitted rows additionally retain the existing structural validator's stricter
1,024 coefficient-digit limit. Native nearest-rounding precision supports
integers -9999 through 9998 (without leading zeros or a plus sign), and `INF`;
other strings, including 9999, are INCOMPARABLE under that explicit policy.

The evaluator indexes rows once. Missing selectors or valid nulls yield MISSING;
conflicting duplicates, absent/mismatched units, dimensions or required period
metadata yield INCOMPARABLE. Identical relevant duplicates retain every ordinal.
Integrity-invalid productions, including forged nonfinite or reversed-date rows,
fail explicitly before diagnostics. All rows are from the same source production.

Frozen check results retain rules, exact selected row evidence and ordinals,
MATCH/MISMATCH/MISSING/INCOMPARABLE, residual/tolerance, and separate missing and
incomparable reasons. MISSING takes precedence if both occur, without discarding
either reason set. The frozen report binds source production/output observation/
fact identities, parser/configuration identity, every result and diagnostic
times. detected_at is no earlier than production/output observation; recorded_at
is no earlier than detection. MATCH never clears other findings or grants PASS.

Caps are positive non-bool integers, validated before consumption. At most 64
rules/8 terms and 10,000 rows are admitted, counting duplicates before dedup;
rules consume at most one overflow sentinel. Rule text is at most 1,024 UTF-8
bytes; input objects, evidence ordinals and report encoding have explicit bounds
before expensive serialization. Resource breaches raise ResourceLimitError;
invalid configuration/types/times fail, never become successful empty reports.
The production preflight caps 250,000 visited fields/nodes, depth 16, each input
text at 4,096 UTF-8 bytes, and aggregate text/Decimal representation at 8 MiB.
Selected evidence across all checks is capped at 100,000 row ordinals and 4 MiB
of encoded row content; final report encoding is capped at 8 MiB. Rule text has
the stricter 1,024-byte cap above.

Acceptance includes exact/tolerant arithmetic, missing FX/nulls, duplicates,
units/dimensions/periods/cash boundaries, missing/INF/extreme precision, hostile
Decimal context, resource bounds, identity determinism, existing integrity
protection and the retained AAPL 22-check comparison. The existing normalized
SecQualityFinding wrapper is explicitly deferred; observed rows cannot use fake
normalized version IDs.

## Acceptance

Thirty new tests and 106 affected regressions passed. They cover eight-term
exact/assumed-nearest arithmetic under hostile Decimal context, precision
boundaries, repeated evidence limits and preflight rejection without repairing
invalid caller objects. Independent Astra review accepted the implementation.
The retained AAPL production was rebuilt with network and snapshot writes
disabled: all 22 selected checks agree with the independent raw-instance oracle
(20 MATCH, 2 MISSING). The local report is
`artifacts/sec-pit-qualification-20260911/sdk-accounting/20260911T162410100708Z/report.json`.
It creates no findings, PASS records or consumer commits. This is selected
same-filing evidence, not complete financial acceptance or pilot expansion.

# SEC structural quality rules v1

This slice evaluates supplied `SecNormalizedFinancialFactVersion` rows offline
and produces existing `SecQualityFinding` annotations. The concrete callers are
the current SEC production pipeline (findings persisted in bundle v2) and its
offline replay/quality-history consumers. It does not fetch data, validate raw
SEC filings, change values, derive PASS/quarantine, or establish PIT availability.

## Public interface and coverage

`evaluate_sec_structural_quality(versions, *, detected_at, recorded_at,
max_records=10_000, max_findings=10_000)` returns a frozen
`SecStructuralQualityReport`. Its fields are `rule_version`, `rule_ids`,
`checked_normalized_version_ids`, `input_record_count`, and `findings` (tuples
where applicable). The fixed version is `sec-structural-v1`; callers cannot
relabel built-in behavior with an arbitrary rule version. IDs/findings are
canonically ordered; duplicate identical input versions count against the input
budget but are evaluated once. Empty input yields an empty report. An empty
finding tuple means only that these checks found no issue in the supplied rows,
not that a statement, filing, or financial dataset is complete or correct.

All generated findings are OPEN with issue class UNKNOWN, no adjudication or
predecessor, and exactly the target version's observation/fact evidence pair.
They inherit its adapter version and use fixed rule IDs, keys and reason text
that never interpolate source values. The evaluator validates internal input
bindings without silently repairing IDs or mutating caller objects. It does not
replay snapshots; bundle v2 verifies retained evidence when writing/loading.

Both timestamps are injected timezone-aware UTC-normalized datetimes.
`recorded_at >= detected_at >= version.recorded_at` for every supplied version,
including rows that produce no finding. Output is deterministic for identical
versions and timestamps. A rerun with different timestamps is a new assertion;
it is not an automatic history revision. Callers explicitly author superseding
adjudications using the existing finding API before combining revision histories.

## Rules

- `sec.structural.field_missing`: each absent/empty/whitespace `concept`,
  `context_ref`, `unit`, `unit_ref`, or None `value` produces a separate finding,
  with that field as the stable key/affected field and FIELD_MISSING. Zero and
  finite negative values are present. Absence does not prove non-disclosure or
  parser failure. `standard_concept`, label, dimensions and native display value
  are not mandatory fields in this slice.
- `sec.structural.period`: absent/blank `period_type` is FIELD_MISSING;
  unsupported nonblank types produce an OPEN finding with no missing reason.
  Supported types are exactly `instant` and `duration`. Both require period_end;
  duration also requires period_start. Each missing required date gets a finding
  with FIELD_MISSING. Unknown types are not guessed from other fields. Invalid
  date shapes, reversed periods and instant-with-start remain invalid typed input
  and fail explicitly, rather than being reinterpreted as valid source facts.
- `sec.structural.duplicate_value`: within a single observation/fact_version, accession,
  vintage, source artifact and production recipe (schema, adapter, normalization,
  configuration and recorded_at), group exact statement_type/concept/context_ref/unit/unit_ref,
  period_type/start/end/key/source, dimension, is_point_in_time, decimals and
  decimals_native. Only complete concept/context/unit/period identities with
  nonmissing finite Decimal values participate. A group with distinct numeric
  Decimal values yields one finding per member version on `value` only, key
  `duplicate_value`, with no missing reason. Numeric equals such as 1.0 and 1.00
  agree. No cross-observation, cross-recipe, cross-period, cross-dimension,
  cross-unit or cross-precision comparison occurs. This detects a candidate
  disagreement, not a proven accounting error; no tolerance or correction is
  inferred. Grouping is indexed, not pairwise, with linear-size output.

Units are checked only for presence. Currency compatibility, magnitude/scaling,
accounting equations, disclosure coverage, period bridges, precision-derived
tolerances, original XBRL comparison and quality policy execution are later work.

## Bounds and acceptance

Both count limits are positive exact integers, at most 10,000. Consume at most
max_records + 1 input entries, including duplicates. Exceeding an input or output
limit raises with no partial report. Before canonical serialization/identity
validation, reject row/metadata strings longer than 4,096 characters, recipe
names longer than 128 characters, Decimal coefficients over 1,024 digits or
absolute exponents over 10,000. These are evaluator admission limits, not changes
to provider ingestion or the normalized-row contract.

Use synthetic snapshot-backed normalized rows. Test missing versus zero/negative,
all period cases, exact-context conflict and equal values, excluded comparison
boundaries, duplicate input/order determinism, unchanged source rows and IDs,
malformed/tampered input and timestamp rejection, bounded generator/output/text,
and large same-group linear finding count. Integrate generated findings through
bundle v2 restart and as-of lookup without changing either PIT selector mode.
Run focused SEC/PIT/finding/bundle regressions and applicable static checks.
Independent Astra review must pass before commit. No consumer migration,
publication, live data validation or full production-panel acceptance is claimed.

# SEC normalized currency

Status: ACCEPTED locally (2026-09-10), not released. Primary owns acceptance; Luna implemented.
Baseline is clean OMD 0.2.4 with edgartools 5.56.0. Preserve any concurrent changes.

## Outcome and scope

Expose an independent `currency` field on SecStatementRow, its dictionary output,
and financial statement Parquet rows. Preserve `unit`, `unit_ref`, value and
value_native exactly. No conversion, FX, scaling, or default currency is allowed.
The concrete callers are typed SEC financial consumers and the Parquet/CLI data
consumer path. This is provider-specific, not a generic core schema change.

Authorized files: SEC financials.py, financials_dataset.py, a narrow private unit
helper module if useful, focused SEC model/dataset tests, financial-period-integrity
documentation and CHANGELOG. This plan remains primary-owned. No dependency,
package version, parser duplicate semantics, consumer, live, Git or publication work.

## Contract

- `currency` is derived and read-only (`field(init=False)` on frozen dataclass or
  equivalent); callers cannot supply a conflicting currency alongside native unit.
  Existing positional constructors remain compatible. dataclasses.replace(unit=...)
  recomputes currency. to_dict includes the new field.
- Recognize only a complete bare three-letter uppercase code or that code with
  the exact `iso4217:` prefix (e.g. USD and iso4217:USD both yield USD; EUR preserved).
  Outer whitespace may be ignored for recognition while original unit stays intact.
  This is syntactic normalization of provider currency codes, not validation against
  a current ISO currency registry. Do not claim arbitrary codes are currently active.
- Missing, empty, lower-case/non-code units, arbitrary QName prefixes, shares,
  pure, percentages, JSON compound definitions and currency-per-share units yield
  None. No inference from concept/issuer/unit_ref and no currency conversion.
- Add nullable string currency to statements Parquet. Move dataset schema to
  sec-company-financials-v3 and writer profile to sec-financials-parquet-v3;
  vintage identity version also v3 includes derived currency via row.to_dict.
  Use one canonical dataset-version constant where feasible without importing
  optional dataframe libraries into model metadata. Existing v1/v2 partitions
  remain untouched and must be rejected explicitly when reused by the v3 writer.
- Validate actual schemas (including currency) and metadata/hash as before.
  Rebuild into a new root; no in-place conversion or auto overwrite. Raw units
  remain distinguishable even when normalized currencies match, including in identity.

## Verification and migration

Focused offline regressions: USD/prefixed USD equivalence with identical Decimal
amounts, EUR preservation, missing/unknown/compound units, native raw text retained,
constructor and replace behavior, dictionary field, Parquet nullable currency and
roundtrip, actual missing-column rejection, v2 manifest rejection, immutable reuse,
and native currency equivalence does not collapse raw vintage identities.
Run full offline tests, Ruff/format/ty/build/diff checks; inspect wheel/sdist and use
existing Python 3.11/3.12 environments when available. No live calls are needed.
Normalization is O(length of unit), constant extra storage per row and no external
registry/network reads. No new dataset scans, fanout, or materialization.
Stop on inconsistent schema/identity or missing data semantics; do not guess.
Document new field usage and v2-to-v3 rebuild, alongside retained v1 migration history.
Only primary accepts after reviewing diff and evidence. No release is authorized.

## Acceptance receipt

Executed contract SHA256: e736c37bc90070616771073fd8567d4dd2c8f7202e5f30ff37dcfc62cd461f02.
This receipt was added after implementation and review; behavior requirements above
are unchanged. Python 3.11 and isolated candidate-wheel Python 3.12 full offline
suites each passed 953 tests. The additional legacy-schema parametrization was
then verified by 57 focused tests; no runtime code changed after the full gates.
Python 3.12 retained two existing multiprocessing fork deprecation warnings.
Ruff, format, ty, build, wheel/sdist source checks, and diff checks passed.
Independent review found no required changes and independently passed 57 tests.

The isolated wheel probe verified raw units and Decimal amounts unchanged, currency
USD/EUR and nullable noncurrency values, dictionary/Parquet outputs and validated
v3 partition schema. Candidate wheel SHA256:
ea19aca25e78a0f3a28c491b3d23ef5103466f94fc99468488ea9ddff9b25b2d.
This is a local unpublished 0.2.4 candidate. No dependency/package version change,
live request, commit, publication, consumer migration or existing data overwrite
was performed. Consumers must rebuild v1/v2 partitions into a new root and verify
their own same-source parity before adopting the new dataset.

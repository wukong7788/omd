# SEC dated target invalidation

This slice plans a caller-declared, exact set of derived-metric targets for
reconsideration.  It does not query metrics, prove source ownership or
availability, mutate outputs, select securities, or assert complete coverage.

## Declarations

- A change names the **old** typed input version being superseded.  A distinct,
  optional new version is retained only as caller context.  It also declares an
  issuer CIK, a half-open valuation interval `[valuation_from, valuation_to)`,
  optional market/system availability timestamps, an evidence reference, and
  `recorded_at`.
- A target names exactly one `DERIVED_METRIC` output version, CIK, instrument
  ID, instrument-binding identity, recipe identity, `valuation_at`, knowledge
  cutoff, explicit `SecPitMode`, and `recorded_at`.  Several declarations may
  name the same output; no declaration expands to other issuer securities.
- Edges remain the existing caller-declared `SecDependencyEdge` lineage.  A
  target is reachable only through same-CIK edges, and the final edge must have
  its declared recipe identity.  These checks prevent accidental cross-issuer
  or recipe matches; they do not prove underlying source ownership.
- Index construction requires every target output to have an existing declared
  edge output with the same CIK and recipe.  Cached declaration identities are
  recomputed and must match.  The index retains private validated query
  snapshots; public record access returns reconstructed copies.  Private
  introspection is not a security boundary.

## Eligibility and proof

The planner considers only changes, targets, and edges recorded on or before
`known_at`.  It first selects targets from the change CIK/date bucket using the
half-open valuation interval, then independently tests availability against the
target's own knowledge cutoff.  A market-known target requires a declared
`market_available_at`; a system-replay target requires a declared
`system_available_at`.  Missing required evidence fails explicitly; an
availability time after the target cutoff simply makes that target ineligible.

For each eligible target, the result carries one deterministic shortest
BFS proof path from the old input version to the exact target output, together
with the change identity and edge identities.  It never enumerates all paths.
Conflicting CIK/recipe declarations for one graph output, cycles, malformed
types, oversized text, and configured resource limits fail closed.  Target
buckets are indexed by issuer and valuation time, so one issuer change does
not scan targets for other issuers.

The traversal budget charges candidate rows, dequeued nodes, every examined
edge, and every reconstructed proof edge.  A separate bounded canonical-JSON
budget covers plan aggregation.  The coverage is only the injected declared
graph/targets; absence of a declaration never proves an issuer, instrument, or
date unaffected.

## Limits and acceptance

Index construction accepts at most 10,000 edges and 10,000 targets. A plan
accepts at most 10,000 change declarations and 20,000 aggregate visits;
canonical aggregation defaults to 8 MiB and may be explicitly set up to 16 MiB.
The planner checks the exact final canonical byte length as well as charging
entries during construction. Public index and plan targets are independent copies.

Independent Astra review accepted the corrected implementation; 13 combined
dated/dependency tests cover cutoffs, issuer isolation, deterministic paths,
identity conflicts, mutation isolation and resource rejection. A synthetic
10,000-edge/10,000-target long chain stopped at the traversal cap in 0.425 seconds
with 113,311,744 bytes peak RSS. Evidence is retained locally at
`artifacts/sec-dated-invalidation-acceptance/report.json`; this is a bounded
rejection probe, not whole-universe recomputation acceptance.

Whole-repository regression after integration: 1,780 tests passed (318 upstream
edgartools deprecation warnings), Ruff/type checks passed, and all formatted
files passed after correcting the README example layout. The 0.2.5 wheel and
sdist contain byte-identical versions of the four changed implementation
modules; local evidence is `artifacts/sec-dated-invalidation-acceptance/integration.json`.

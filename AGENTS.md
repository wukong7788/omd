# AGENTS.md

## Purpose

This repository provides reusable market-data ingestion infrastructure for
multiple applications. Tushare is the first provider. yfinance and FMP are
future integrations, not current implementation scope.

`PLAN.md` is the canonical architecture and migration plan until `v0.1.0`.

## Instruction Precedence

Follow instructions in this order:

1. platform and tool safety rules;
2. the user's explicit current-turn instruction;
3. the nearest directory-level `AGENTS.md` or `RULES.md`;
4. this file;
5. `PLAN.md`, README, and older design documents.

State any conflict and follow the higher-priority rule. Do not silently
reconcile incompatible requirements.

## Non-negotiable Invariants

1. Provider code is ingestion infrastructure, not strategy or portfolio logic.
2. Never silently change fields, units, adjustment policy, timezone, date
   boundaries, pagination, empty-result semantics, or missing-value policy.
3. Never convert missing market data into zero or another plausible value
   without an explicit named transform owned by the caller.
4. Never claim point-in-time availability from observation dates alone.
5. Never introduce look-ahead behavior through date alignment or revision
   handling.
6. Credentials are injected by callers. Library code must not read `.env`,
   environment variables, keychains, or consumer configuration files.
7. Never log, hash into public identities, snapshot, or serialize tokens and
   credentials.
8. Provider-specific assumptions stay under that provider. Tushare behavior
   must not become a generic core contract.
9. Raw/provider-native data and normalized/consumer data remain distinguishable
   and traceable.
10. Required symbol, field, page, or coverage failures fail explicitly; never
    skip them silently.
11. Tests are offline by default. Live provider calls require an explicit
    integration marker and user authorization.
12. Consumer repositories must pin immutable SDK versions, not a moving
    branch.
13. This repository is public. Treat every tracked file, commit, branch, tag,
    issue attachment, test fixture, log excerpt, and CI artifact as publicly
    readable.

## Scope Discipline

Before implementing a shared abstraction, identify at least two concrete
callers or one provider requirement plus a near-term migration need.

Do not build speculative yfinance/FMP modules while the active phase is
Tushare. Preserve future extensibility through narrow interfaces, not empty
providers or premature generalized schemas.

The SDK may own:

- provider clients and endpoint contracts;
- retry, rate limiting, error classification, request identity;
- provenance, snapshots, replay, and integrity validation;
- provider-semantic reusable recipes.

Consumers own:

- universe selection;
- business features and investment calculations;
- storage locations and publication workflows;
- strategy, backtest, signal, live execution, notifications, and UI;
- project-specific normalized schemas and operational schedules.

## API Design Rules

- Prefer typed requests, results, capabilities, and stable exception classes.
- Keep a provider escape hatch, but do not make arbitrary
  `fetch(endpoint, params)` the only public abstraction.
- Make effective parameters, fields, attempts, warnings, and provenance
  inspectable.
- Use endpoint-specific pagination/window rules.
- Require callers to choose ambiguous policies explicitly.
- Preserve provider-native values before normalization.
- Keep core metadata independent of Pandas and Polars.
- Tushare adapters may return Pandas because it is provider-native; Polars
  conversion must remain an explicit adapter.
- Public APIs require tests and documentation in the same change.

## Retry and Error Rules

- Retry only classified transient failures.
- Authentication, permission, invalid parameter, schema mismatch, and
  deterministic validation errors fail immediately.
- Retry counts are total attempts or retries consistently across the API;
  document the selected meaning and test it.
- Backoff, jitter, clock, and sleep are injectable for deterministic tests.
- Empty responses follow endpoint/request policy and are not globally treated
  as success or failure.
- Exhaustion errors preserve the original exception as their cause without
  leaking secrets.

## Snapshot and Data Safety

- Snapshot writes must be atomic and immutable.
- Validate request identity, response hash, endpoint, manifest schema, and
  serialization version on replay.
- Concurrent writes must not produce partial valid-looking snapshots.
- Never overwrite or delete material snapshots as part of routine fetching.
- Destructive cleanup requires explicit targets and user authorization.
- Git may contain only synthetic or irreversibly sanitized fixtures.
- Do not commit downloaded provider data, credentials, local caches, or
  consumer artifacts.
- `.env`, token files, real request headers, account identifiers, private
  consumer configuration, and raw provider responses are forbidden in Git.
- Documentation and tests use unmistakably fake credential placeholders.
- Never print a real secret merely to check whether it exists.
- Before pushing, inspect staged files and run the repository's secret scan.
- If a secret reaches Git history, stop publication, rotate/revoke the secret,
  and follow a history-remediation plan; a follow-up deletion commit does not
  remove public exposure.

## Development Baseline

Target Python versions:

- Python 3.11
- Python 3.12

Use `uv` for environments and dependency locking. Once the project scaffold
exists, canonical checks are:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv build
git diff --check
```

Do not use system Python or mix Conda with the project environment.

Runtime dependencies must remain minimal. Provider and dataframe integrations
belong in optional extras where practical. Dependency changes must update both
`pyproject.toml` and `uv.lock`.

## Testing Requirements

- Non-trivial behavior requires tests; bug fixes require regression tests.
- Mock at the provider-client boundary, not inside the behavior being tested.
- Cover success, empty, transient failure, permanent failure, malformed schema,
  pagination boundaries, duplicate pages, and partial coverage.
- Snapshot tests cover tampering, interruption, concurrency, idempotency, and
  same-request/different-response policy.
- Dataframe conversion tests cover dtype, null, date, timezone, ordering, unit,
  and float behavior.
- Consumer migration requires golden parity evidence before deleting legacy
  code.
- A successful command exit alone is not acceptance evidence; inspect the
  resulting contract or artifact.

## yfinance Provider Governance and Zero-Drift Auditing

### Baseline Governance and Invariants

1. **Immutable Pinning**:
   `yfinance` must always be pinned to an exact version (`yfinance==<version>`)
   in `pyproject.toml` and asserted at runtime via `EXPECTED_YFINANCE_VERSION`
   in `src/ohmydata/providers/yfinance/client.py`. Never use a loose range
   (e.g. `>=...`), as yfinance frequently alters parsing, column structures,
   and heuristic price repair algorithms across minor releases.

2. **Default `repair=False` Invariant**:
   Production ingestion and historical backtest pipelines must strictly default
   to `auto_adjust=False, repair=False, actions=True, keepna=True`.
   - Global `repair=True` is prohibited in canonical ingestion: it is an
     active heuristic mutator with known 100x unit scaling bugs and
     unflagged dividend modifications, and introduces an undeclared dependency
     on `scipy`.
   - OMD owns Quality Control (QC) anomaly detection (`check_ohlc_anomalies`,
     `check_price_jump_anomalies`, `check_volume_anomalies`). `repair=True` may
     only be evaluated as an isolated candidate repair engine for targeted
     anomalies, recording explicit `raw_value`, `canonical_value`,
     `repair_status`, and `repair_reason`.

3. **Predefined Benchmark Universe (`r10a0`)**:
   The `r10a0` multi-asset ETF universe serves as the canonical regression
   benchmark for US market data stability:
   - **Clusters (Cluster Variant v3, max 1 each)**:
     - `equity_risk`: `[SPY, QQQ, XLK, IWM, SMH]`
     - `sector_cyclicals`: `[XLF, XLE, XLV]`
     - `defensive`: `[TLT, GLD, USMV]`
   - **Regime Pools**:
     - `risk_on`: 11 symbols
     - `risk_off` (SPY < MA200): `[SHY, IEF, GLD]` (Top 2 selected)
   - **Unique Set (13 ETFs)**: `SPY`, `QQQ`, `XLK`, `IWM`, `SMH`, `XLF`, `XLE`,
     `XLV`, `TLT`, `GLD`, `USMV`, `SHY`, `IEF`.

4. **Zero-Drift Audit Tool (`omd audit-drift`)**:
   Before approving any future `yfinance` version upgrade, run the automated
   zero-drift audit tool across the full 10+ year history of the `r10a0`
   universe:
   ```bash
   # Strict unadjusted market bar zero-drift gate (must be bit-exact 0.0 error)
   omd audit-drift --universe r10a0 --baseline-dir <old_version_dir> --target-dir <new_version_dir> --raw-only

   # Full audit including adj_close with sub-cent rounding tolerance
   omd audit-drift --universe r10a0 --baseline-dir <old_version_dir> --target-dir <new_version_dir> --abs-tolerance 0.001
   ```
   **Acceptance Criteria for Upgrade**:
   - `raw-only` (Open, High, Low, Close, Volume) must achieve **0 row difference
     and 0 numeric difference** across all 13 ETFs over the full historical
     window.
   - Any divergence in `adj_close` must be bounded by sub-cent floating point
     rounding (<= 0.001) or accompanied by an explicit, auditable rationale.

## Repository and Git Hygiene

- Use a `src/` package layout.
- Keep modules focused; split files approaching 500 lines when responsibilities
  are separable.
- Use standard-library `logging` or injected observability hooks in the SDK;
  do not force a consumer logging framework.
- Temporary local scripts and generated artifacts belong under ignored
  directories.
- Preserve unrelated user changes in dirty worktrees.
- Never run destructive Git commands unless explicitly requested.
- Do not commit or push unless the user asks.
- Review `git diff --cached` before every commit and verify that ignored files
  have not been force-added.

Use semantic versioning. Each release-worthy behavior change updates
`CHANGELOG.md`. Breaking changes require a migration section and a major
version after `v1.0.0`.

### Release Commit and PyPI Publication

- A release commit must update every version-bearing or release-tracking file
  in the same commit: `pyproject.toml`, `src/ohmydata/__init__.py`,
  `uv.lock`, the exact version assertion in `tests/test_package.py`, and
  `CHANGELOG.md`. Documentation describing changed public behavior must be
  updated in that commit as well.
- Before committing, run the canonical test, Ruff, format, ty check, build, and
  `git diff --check` gates; inspect the built wheel/sdist version and the
  staged diff, and run the secret scan. Never publish a version that is
  inconsistent across those files or already exists on PyPI.
- After the release commit is committed and pushed to the intended public
  branch, the user may authorize execution of `.github/workflows/publish.yml`
  via GitHub Actions `workflow_dispatch`. Confirm the workflow uses the
  intended commit and version, then inspect the completed run and PyPI artifact
  before claiming publication succeeded.
- `publish.yml` is external publication. Do not dispatch it without explicit
  user authorization, and do not retry a failed run blindly: inspect the job
  logs, correct the release/workflow or trusted-publisher configuration, commit
  and push the fix when required, then dispatch a new run.

## Documentation Discipline

- `PLAN.md` owns pre-`v0.1.0` architecture, phases, and acceptance gates.
- `README.md` owns installation and user-facing examples.
- Provider endpoint semantics belong near the provider implementation or in
  dedicated provider documentation.
- Avoid duplicating mutable contracts across files; link to the canonical
  owner.
- When implemented behavior diverges from documentation, update the
  documentation in the same task or report the unresolved drift explicitly.
- Mark only verified plan checklist items complete.

## Workflow Triggers

Use the project skill `.agents/skills/sol-luna-workflow/SKILL.md` only when the
user explicitly asks for a Sol–Luna execution workflow. Do not trigger it
automatically based on task size, whether the task is multi-file, or whether it
comes from a plan/spec/ADR.

In that workflow:

- Sol owns requirement interpretation, contract-document review and freeze,
  architecture decisions, execution briefing, independent diff/evidence
  review, and final acceptance.
- Luna owns only the implementation, focused tests, authorized documentation
  updates, validation, and rework within Sol's frozen brief.
- Luna's results are provisional until Sol inspects the actual diff and
  evidence.

Do not invoke the workflow for explanation-only work, document-only review,
trivial isolated edits, external publishing, secret handling, or live provider
operations.

## Review Priorities

Review in this order:

1. secret exposure or unsafe file/network behavior;
2. silent missing/empty/schema/coverage handling;
3. unit, adjustment, timezone, and date semantic drift;
4. retry and error misclassification;
5. pagination loss, duplication, or unstable ordering;
6. snapshot identity and integrity;
7. consumer behavioral parity;
8. API maintainability and style.

Report findings by severity before style commentary. State whether consumer
rerun evidence or migration documentation is required.

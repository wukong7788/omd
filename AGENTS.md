# AGENTS.md

## Purpose

This repository provides reusable market-data ingestion infrastructure for
multiple applications, with Tushare, yfinance, and SEC providers. FMP remains
out of scope unless explicitly requested.

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

Extend providers only within the requested scope. Use narrow interfaces; do
not add speculative providers or premature generalized schemas.

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

Use `uv` for environments and dependency locking. Canonical checks are:

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

Choose checks for the changed behavior. Documentation and local harness edits
need syntax, link, and instruction-consistency checks, not the full SDK suite.
Code changes retain the canonical gates; releases require every release gate.
After checks pass, repeat or broaden them only for new changes, failures, or
unresolved risks. Do not add tests that merely assert instruction wording.

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

## yfinance Governance

Before changing yfinance ingestion, repair/QC, or its version, read
[the mandatory governance and zero-drift gates](docs/harness/yfinance-governance.md).
Exact version pinning and canonical defaults remain required:
`auto_adjust=False, repair=False, actions=True, keepna=True`.

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

## Harness and Workflow

Keep final responses concise: lead with the result, then relevant validation
and unresolved risks. Omit empty sections, repeated summaries, and checklist
matrices unless requested. Brevity must not hide failures or missing evidence.

Resolve routine choices within the user's authorized scope and continue work.
Ask only when missing information materially affects correctness or scope, or
an action requires authorization not already provided in the conversation.

Use `.agents/skills/sol-luna-workflow/SKILL.md` only for an explicit Sol–Luna
execution request. Task size or a plan/spec does not activate it. The skill
owns role, contract-freeze, delegation, and acceptance details. Explanation,
document-only work, trivial edits, publishing, secrets, and live provider
operations do not activate this workflow.

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

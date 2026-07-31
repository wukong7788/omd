# Phase 1 — Core Policies and Provenance

Status: `ACCEPTED`

## Objective

Implement the provider-independent Phase 1 core for deterministic request
identity, stable errors, bounded classified retry, instance-scoped rate
limiting, inspectable provenance, and atomic immutable snapshots.

The result is infrastructure only. It must not call Tushare, understand
endpoint pagination, normalize market data, discover credentials, or create a
generic provider client.

## Governing Documents

This increment is governed by:

- `PLAN.md`
- this contract
- `AGENTS.md`

The Luna execution brief must freeze `PLAN.md` and this contract by SHA-256.

## Current State and Callers

Phase 0 established an offline `ohmydata` package and recorded two concrete
consumer families in `docs/behavioral-inventory.md`.

The extracted `funmoney_backtest/data_pipeline/tushare_ext/` implementation is
evidence only. Its broad exception retry, universal `limit`/`offset`, mutable
request parameters, non-atomic response/manifest writes, and credential-adjacent
consumer behavior must not be copied.

Near-term callers are:

- the Phase 2 Tushare endpoint client, which needs request identity, retry,
  rate-limit, provenance, and snapshot primitives;
- both consumer shadow migrations, which need stable exception categories,
  inspectable attempts, immutable evidence, and secret-safe manifests.

## Authorized Files and Modules

Luna may create or edit only:

- `src/ohmydata/core/__init__.py`
- `src/ohmydata/core/errors.py`
- `src/ohmydata/core/specs.py`
- `src/ohmydata/core/policy.py`
- `src/ohmydata/core/rate_limit.py`
- `src/ohmydata/core/provenance.py`
- `src/ohmydata/core/snapshot.py`
- `tests/core/test_errors.py`
- `tests/core/test_specs.py`
- `tests/core/test_policy.py`
- `tests/core/test_rate_limit.py`
- `tests/core/test_provenance.py`
- `tests/core/test_snapshot.py`
- `README.md`
- `CHANGELOG.md`
- `pyproject.toml`
- `uv.lock`

Luna must not edit `PLAN.md`, either file under `docs/plans/`, the behavioral
inventory, Phase 0 characterization files, repository instructions, agent
configuration, or consumer repositories.

## Public Error Contract

Define these public classes and hierarchy in `ohmydata.core.errors`:

```text
OhMyDataError
├── ProviderError
│   ├── PermanentProviderError
│   │   ├── AuthenticationError
│   │   ├── PermissionDeniedError
│   │   ├── EmptyResponseError
│   │   ├── SchemaMismatchError
│   │   └── PaginationError
│   └── TransientProviderError
│       ├── RateLimitError
│       └── RetryExhaustedError
├── SnapshotIntegrityError
│   └── SnapshotConflictError
└── CoverageError
```

Requirements:

- classes are stable, importable from `ohmydata.core`, and contain no
  provider-specific parsing;
- `RetryExhaustedError` exposes immutable attempt history and preserves the
  final transient exception as `__cause__`;
- exception and attempt-history representations never embed original provider
  error messages or request parameter values;
- deterministic validation errors in core constructors may use `ValueError`
  or `TypeError`; they are not provider failures.

## Canonical Request Contract

Implement a frozen `RequestSpec` with:

```text
provider: str
endpoint: str
parameters: mapping
fields: tuple[str, ...]
```

It must expose:

- a defensive JSON-safe canonical payload;
- canonical UTF-8 JSON using sorted keys and compact separators;
- a full SHA-256 hexadecimal `request_identity`;
- defensive access to effective parameters.

Rules:

- provider and endpoint are non-empty safe identifiers, not filesystem paths;
- field order is preserved because response column order may be observable;
  duplicate or empty fields fail;
- mapping keys are strings and are sorted recursively;
- list/tuple order is preserved;
- supported values are `None`, booleans, integers, finite floats, strings,
  `date`, timezone-aware `datetime`, nested mappings, lists, and tuples;
- dates and datetimes use explicit tagged canonical representations so they do
  not collide with ordinary strings; datetimes normalize to UTC;
- naive datetimes, NaN/infinity, sets, bytes, arbitrary objects, non-string
  mapping keys, and unstable representations fail immediately;
- caller mutation of the original mapping/list after construction cannot
  change canonical payload, effective parameters, or identity;
- nested secret-bearing parameter keys are rejected case-insensitively.
  At minimum reject normalized forms of `token`, `api_token`, `access_token`,
  `api_key`, `secret`, `client_secret`, `password`, `authorization`,
  `proxy_authorization`, `cookie`, and `set_cookie`, including conventional
  suffix forms such as `_token`, `_secret`, `_password`, and `_api_key`;
- secret-key errors may identify the key path but never the associated value;
- request identities and manifests never contain rejected credentials.

Provider-specific parameter defaults and field meaning remain outside core.

## Retry Contract

Implement:

- frozen `RetryPolicy`;
- frozen `AttemptRecord`;
- generic frozen `RetryResult[T]`;
- `execute_with_retry`.

`RetryPolicy` uses:

```text
max_attempts
base_delay_seconds
backoff_multiplier
max_delay_seconds
jitter_ratio
```

Rules:

- `max_attempts` means total attempts, including the first;
- all numeric values are finite and validated; attempts are at least one,
  delays and jitter are non-negative, multiplier is at least one, and jitter
  is at most one;
- the default retry classifier retries only `TransientProviderError`
  subclasses;
- `PermanentProviderError`, unknown exceptions, schema, permission,
  authentication, pagination, and empty-result errors are not retried;
- callers may inject a narrower classifier but cannot make core validation
  errors retryable by accident;
- delay before the next attempt is bounded exponential backoff with symmetric
  proportional jitter:

```text
bounded = min(max_delay, base_delay * multiplier ** retry_index)
delay = bounded * (1 + jitter_ratio * (2 * random_value - 1))
```

  where `retry_index` starts at zero and injected `random_value` must be in
  `[0, 1]`;
- sleep and random functions are injectable and no hidden global RNG or sleep
  state is used;
- success returns the value and immutable attempt records, including the final
  successful attempt;
- attempt records contain attempt number, exception class name when relevant,
  and scheduled retry delay, but never exception messages;
- a non-retryable exception is re-raised unchanged on its first occurrence;
- exhaustion raises `RetryExhaustedError` from the last transient exception and
  exposes all attempt records.

## Rate-Limit Contract

Implement a fixed-interval, thread-safe, instance-scoped limiter:

```text
RateLimitPolicy(min_interval_seconds)
RateLimitDecision(waited_seconds, acquired_at)
RateLimiter(policy, clock, sleep).acquire()
```

Rules:

- interval is finite and non-negative;
- `clock` is an injected monotonic-seconds callable and `sleep` is injectable;
- callers sharing one limiter are serialized; separate limiter instances share
  no state;
- the first acquire does not sleep;
- later acquires wait only as needed and return inspectable timing;
- clock regression must not create a negative sleep;
- there is no module-level limiter, credential, filesystem path, endpoint
  quota, or provider assumption.

This is a primitive for Phase 2 endpoint policies, not a claim that all
providers use the same quota.

## Provenance Contract

Implement JSON-safe, dataframe-independent provenance types:

```text
EmptyDisposition = NOT_EMPTY | ALLOWED_EMPTY
FetchProvenance
```

`FetchProvenance` contains:

- provider, endpoint, request identity;
- defensive effective parameters and requested fields;
- timezone-aware retrieval timestamp normalized to UTC;
- immutable attempt records and derived attempt count;
- row count and ordered column summary;
- warnings;
- snapshot identities;
- explicit empty disposition.

Requirements:

- construction from a `RequestSpec` must not duplicate or drift from its
  canonical identity;
- row count is non-negative, column names are unique, and empty disposition
  agrees with row count;
- `to_dict()` returns a fresh JSON-safe object and does not leak later caller
  mutation;
- warnings are caller-supplied, already-sanitized text, are never derived from
  exception messages by core, and are not included in request identity;
- provenance contains no dataframe dependency; callers remain responsible for
  not placing credentials in free-form warnings.

## Snapshot Contract

Implement:

```text
SnapshotMode = APPEND | FROZEN
SnapshotRef
SnapshotReplay
SnapshotStore(root).write(...)
SnapshotStore(root).replay(...)
```

Write inputs:

- a `RequestSpec`;
- an already serialized immutable `bytes` payload;
- a required timezone-aware retrieval timestamp;
- a non-empty serialization identifier/version such as `json-rows-v1`;
- an explicit `SnapshotMode` (default `APPEND` is acceptable).

Path and identity:

```text
<root>/<provider>/<endpoint>/<request_identity>/
  append/<serialization_sha256>/<response_sha256>/response.bin + manifest.json
  frozen/response.bin + manifest.json
```

- response SHA-256 is over the exact supplied bytes;
- serialization SHA-256 is over the UTF-8 serialization identifier and avoids
  using caller text directly as a path component;
- snapshot identity deterministically includes the full request identity,
  response hash, serialization identifier, and mode;
- repeated same-request/same-bytes writes are idempotent and return the same
  reference without changing the original retrieval timestamp;
- `APPEND` permits same-request/different-response observations as distinct
  immutable assets;
- `FROZEN` permits only one response for a request; a later different response
  raises `SnapshotConflictError`.

Manifest schema version 1 contains only:

- manifest schema version;
- provider and endpoint;
- canonical request and request identity;
- response SHA-256 and byte size;
- serialization identifier;
- UTC retrieval timestamp;
- snapshot identity and mode.

Atomicity and concurrency:

- construct `response.bin` and `manifest.json` inside a unique temporary
  directory under the same request directory;
- publish the complete observation with one atomic directory rename;
- never overwrite an existing final observation directory or either final
  file;
- a losing concurrent writer validates the winner: identical content is
  idempotent, differing frozen content is a conflict, and malformed winner
  state is an integrity error;
- an exception before publish cannot leave a valid-looking final observation;
- cleanup may remove only the current call's uniquely named temporary
  directory;
- expose no routine delete or overwrite API.

Replay must fail closed with `SnapshotIntegrityError` for:

- missing response or manifest;
- malformed JSON or unsupported manifest schema;
- provider, endpoint, request identity, canonical request, snapshot identity,
  mode, serialization, response size, or response-hash mismatch;
- unexpected final path layout;
- mismatch with an optional expected `RequestSpec` supplied by the caller.

Replay returns exact bytes plus a validated defensive manifest. It must never
deserialize provider data or infer dataframe semantics.

Filesystem names derive only from validated safe provider/endpoint identifiers
and hexadecimal identities.

## Testing Contract

All tests are offline and synthetic.

Required coverage:

- error hierarchy and secret-safe exhaustion representation;
- canonical identity determinism, nested ordering, date/datetime tagging,
  caller-mutation isolation, invalid values, and nested secret-key rejection;
- retry success, bounded delays, deterministic jitter, total-attempt semantics,
  transient exhaustion/cause, and immediate permanent/unknown failure;
- limiter first/subsequent acquire, independent instances, clock regression,
  and multi-thread serialization;
- provenance validation, mutation isolation, UTC timestamp, empty disposition,
  and JSON serialization;
- snapshot idempotency, append drift, frozen drift rejection, concurrent same
  and different response writers, response and every manifest identity field
  tampering, truncated/missing files, unsupported schema/serialization,
  expected-request mismatch, and injected pre-publish interruption;
- a scan proving test fixtures/manifests contain no credential strings or real
  provider data.

Tests may use private monkeypatch points for interruption only; production
public APIs must not expose a general arbitrary callback.

## Documentation and Packaging

- `README.md` documents the Phase 1 public core with short offline examples,
  total-attempt retry meaning, secret-key rejection, explicit limiter
  instances, and append/frozen snapshot behavior.
- `CHANGELOG.md` records the implemented Phase 1 behavior without claiming a
  Tushare client.
- `ohmydata.core.__init__` exports the public types/functions in this contract.
- Keep `ohmydata.__init__` unchanged unless an import is strictly required;
  prefer the narrower `ohmydata.core` namespace.
- Runtime dependencies remain empty.
- Pyright remains strict and all public functions/classes are typed.

## Explicit Non-Goals

- No Tushare import, client, endpoint spec, field/unit definition, or error
  message parser.
- No Pandas, Polars, dataframe, Parquet, DuckDB, or consumer schema.
- No generic `fetch(endpoint, params)` client.
- No pagination or window policy.
- No credential loading, redaction-by-logging, environment lookup, `.env`
  access, or token storage.
- No JSON payload convenience serializer; callers supply declared bytes.
- No snapshot listing, pruning, deletion, overwrite, compression, migration,
  or remote object store.
- No consumer repository edits, live calls, commits, pushes, releases, or
  publication.

## Required Commands and Artifact Inspection

Luna must run:

```bash
uv lock --check
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
git diff --check
```

Luna must additionally inspect:

- wheel/sdist file lists and wheel metadata;
- runtime dependency metadata;
- source/test imports for provider/dataframe/environment/network libraries;
- final changed files against the authorized list;
- synthetic snapshot manifests and fixture text for credential leakage.

Sol will independently repeat checks on Python 3.11 and 3.12 as proportionate
to risk.

## Acceptance Conditions

Sol may declare this increment `ACCEPTED` only when:

1. every public contract above is implemented and documented;
2. permanent and unknown exceptions are never retried;
3. retry exhaustion preserves its cause without error-message leakage;
4. request mutation cannot change identity and secret-bearing keys fail before
   serialization;
5. limiter state is explicit, instance-scoped, injectable, and thread-safe;
6. snapshot publication is directory-atomic, immutable, concurrent-safe, and
   replay validates every declared identity/integrity field;
7. interruption and tampering cannot yield a valid replay;
8. no credential, real provider data, provider/dataframe dependency, hidden
   global state, or unrelated change exists;
9. documentation and package metadata match the actual public surface;
10. all required checks and both supported Python import/test probes pass.

## Stop Conditions

Return `EXECUTION_BLOCKED` without inventing scope if:

- either frozen document hash changes;
- atomic directory publication cannot be implemented on the supported local
  filesystem without overwrite risk;
- concurrency semantics require a destructive cleanup or global lock;
- a requested convenience would require accepting credentials in canonical
  parameters;
- implementation needs a runtime dependency or provider-specific assumption;
- a public API decision is missing or contradictory.

## Compatibility and Migration

This is a new pre-`v0.1.0` API. No compatibility shim for the consumer
`tushare_ext` code is authorized. Phase 2 must consume these primitives
directly and consumer migrations must pin an immutable SDK version.

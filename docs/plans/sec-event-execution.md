# Injected SEC event execution and recovery

Status: ACCEPTED (2026-09-12), independent Astra review complete.

## Responsibility and identity

A synchronous executor advances one existing `SecEventWorkLedger` using an
explicit discovery batch and work specification. It does not discover events,
schedule a service, publish to consumers or grant financial PASS. Callers inject
acquisition/production, recovery, validation, clock and deadline policy.

Public entry points are `SecEventExecutor`, the typed operation/recovery/validation
interfaces, and `retain_sec_execution_outputs` / `retain_sec_execution_validation`.
Construct the executor with a `SecEventWorkLedger`, the SnapshotStore containing
its execution outputs/evidence, and a UTC clock; call `run(batch, spec, handler,
validator, deadline=..., max_steps=...)` explicitly. Discovery sources may use the
ledger's separate store. No credentials or consumer configuration are read.

The operation key hashes the work identity and attempt number. Work identity
already includes event metadata, processing version, configuration and typed
input versions. A separate fixed POSIX execution lock in the work ledger root
covers replay, recovery, callbacks and journal advancement; concurrent execution
must fail explicitly rather than invoke duplicate handlers. Existing ledger
writer locking and transition validation remain authoritative.
The persistent lock and immutable receipt locators live in the fixed `.execution`
child of that work ledger. Only work-ledger scanning permits this reserved child;
discovery-ledger scanning remains strict. Unknown/unsafe execution entries fail.

## Retained results and recovery

Before `FETCHING` advances to `VALIDATING`, a fixed-schema acquisition receipt
must be durably retained. It binds the operation/work/specification and handler
version, exact input identities, and actual output snapshot observations. Receipt
and output bytes must be strictly replayed through SnapshotStore, including
request/response identity, schema, serialization and causal time checks.

Both handler and validator expose `recover(operation)` and return exactly
`COMPLETE`, `NOT_STARTED` or `UNKNOWN`. Only
`NOT_STARTED` permits a handler call. `COMPLETE` requires the same operation's
valid retained receipt; missing or inconsistent evidence does not trigger a
replacement fetch. `UNKNOWN` records an explicit `UNCONFIRMED_SIDE_EFFECT`
failure and never retries automatically. The caller must honor operation-key
idempotency: the SDK cannot promise exactly-once arbitrary external side effects.
`handler.version` must equal the work specification's `processing_version`;
the validator's explicit version is also bound in every receipt. The acquisition
callback retains its result with `retain_sec_execution_outputs` before returning
that observation reference. The validation callback similarly returns an already
retained validation receipt. Callers must recover that exact receipt after a
crash between callback completion and executor locator creation.

Output identities in work states are `RAW_FACT` wrappers of the actual output
snapshot `fact_version`. They identify bytes, not verified normalized or financial
production identities. The original payload remains traceable and unchanged.

The validation outcome and its evidence are also retained and bound to the
operation/output receipt before `READY`. A warm terminal lookup replays both
receipts and their retained output/evidence dependencies. This is
`OUTPUT_BYTES_REPLAYED`, not independent financial reconstruction: it verifies
retained bytes and causal identities without reparsing the financial source.
It does not establish the authenticity of a caller's quality judgment.

## States and budgets

The existing transition graph is unchanged. `SecExecutionRetry(retry_at)` declares
a transient acquisition failure; another recovery check must confirm
`NOT_STARTED` before scheduling `RETRY_WAIT`. If recovery reports COMPLETE,
the retained result is used instead. Work specifications permit at most three
total FETCHING entries and persist the retry time. A call
before that time returns waiting; the executor never sleeps internally.
This limit counts work attempts, not arbitrary network retries inside caller
callbacks. An interrupted operation may be called again only when the caller's
recovery explicitly confirms NOT_STARTED; its operation key remains stable.
`PermanentProviderError` records FAILED. Other callback exceptions propagate and
leave FETCHING/VALIDATING available for explicit subsequent recovery. Exception
messages are not serialized into the journal. Validation yields an explicit
ready or quarantined outcome. Dependencies remain
caller-declared exact versions and pass the existing binding/cycle checks.

Every state step checks an injected deadline and bounded step budget. Yielding
preserves recoverable state; it does not invent an illegal failed transition.
Deadline checks occur before and after callbacks, with cooperative cancellation
only. There is no claim that arbitrary Python/native callbacks can be forcibly
interrupted, or that sampled RSS is a hard memory ceiling.

Each operation permits 1–16 output observations and 1–16 validation evidence
observations, up to 128 exact dependency declarations, and at most 256 KiB per
fixed receipt/locator. Each artifact replay is at most 8 MiB, with a 32 MiB
aggregate receipt/output/evidence byte budget per invocation (a stricter
`max_replay_bytes` may be supplied). Duplicate observations share the checked
cache; role-specific limits still apply on cache hits. Steps are bounded to
1–32 per call. Existing discovery/work journal limits apply separately.
READY/QUARANTINED warm reuse revalidates receipt and evidence bytes; FAILED only
returns the durable failure state and grants no artifact qualification.

## Required acceptance

- Retained discovery through acquisition, production, validation and READY,
  plus quarantine and classified retry/exhaustion.
- Crash/restart before and after artifact retention and journal advancement;
  uncertainty, missing or tampered receipts, stale/concurrent entry.
- Bounded input/receipt/step handling and causal-time rejection.
- Cold/warm counters: valid terminal reuse performs zero acquisition/production
  callbacks and no financial parse, while replaying output and validation bytes.
- Independent review and an inspected offline integration artifact. Real-provider
  acquisition and consumer publication remain separately gated.

## Acceptance evidence

107 related event/package tests passed, including 44 new execution tests and
coverage of strict JSON types under valid outer hashes, callback deadline and directory
replacement guards, link-boundary crash recovery, process-lock release after
termination, output and aggregate byte limits, and cached receipt integrity.
Ruff, formatting, type checks and the 0.2.5 build passed; seven affected source
modules match the wheel and source distribution. No version bump or publication.

`artifacts/sec-event-execution-acceptance/20260911T172426679287Z/report.json`
records a synthetic single-cell filing parsed by the installed edgartools,
produced through the actual observed path and driven to READY. Both cold and
warm state/receipt identities agree, with one total production and validation
callback; all warm callbacks and network access were forbidden. Cold invocation
took approximately 0.066 seconds, warm 0.0038 seconds; the process including
imports took 0.935 seconds and reached a lifetime RSS of 177,422,336 bytes on
macOS. This one-cell probe is not the eight-security production performance gate
and does not establish real financial PASS or consumer publication.

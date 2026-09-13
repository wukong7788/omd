# Bounded idempotent SEC metric recomputation and recovery

Status: PARTIAL IMPLEMENTATION (2026-09-12), partial target-mapped single-work adapter; phases 8–10 not started.

## Responsibility and boundaries

The recompute adapter (`execute_sec_metric_recompute`) coordinates the recomputation
of exactly one caller-selected `SecDatedInvalidationTarget` against newly supplied
inputs (`SecTargetMetricInputs`).

It does not:
- Complete plan phases 6–7 or the full incremental recompute plan.
- Automatically discover SEC events or map filings to invalidation targets.
- Manage persistent scheduling daemons or worker pools.
- Claim full financial graph reconstruction or grant automatic quality PASS.
- Provide full financial or external lineage coverage (it registers dependency edges for normalized fact inputs only; no lineage completeness claim).
- Derive consumer quality records, consumer commits, or PIT eligibility automatically.
- Commit or publish data to downstream consumers.
- Maintain duplicate locator files or separate execution journals outside `SecEventExecutor`.

The caller retains full ownership of:
- Event-to-target mapping (identifying which invalidation targets to recompute for an event).
- Managing the per-work ledger directory (`SecEventWorkLedger`).
- Determining input facts, quality records, and external inputs for the target.
- Providing required explicit recovery callables (`handler_recovery` and `validator_recovery`).
- Consumer quality derivation, consumer commits, and PIT eligibility decisions.
- Consumer publication and notification.

## Architecture and reuse

The implementation strictly reuses the existing execution framework:
- **`SecEventExecutor`**: Synchronously advances the `SecEventWorkLedger` through
  `DISCOVERED -> QUEUED -> FETCHING -> VALIDATING -> READY`.
- **`SecExecutionHandler` (`SecMetricRecomputeHandler`)**: Recomputes the metric graph
  using `compute_sec_metric_graph`, validates finite Decimal values, and retains
  canonical serialized output bytes via `retain_sec_execution_outputs`.
- **`SecExecutionValidator` (`SecMetricRecomputeValidator`)**: Registers dependency edges
  for normalized fact inputs against the target recipe identity and output fact version,
  returning retained validation evidence.
- **`retain_sec_execution_outputs`**: Durably records output snapshot observations with
  the explicit claim `"claim": "OUTPUT_BYTES_REPLAYED"`.

## Target binding and identity

`validate_recompute_target_binding` validates the consistency of the target and input specifications:
- CIK match between target and filing discovery event.
- Knowledge cutoff match between target and input configuration.
- Valuation time match between target and input configuration.
- Mode consistency (e.g. `MARKET_KNOWN` vs `SYSTEM_REPLAY`).
- Cutoff later than valuation is strictly rejected in every mode, including `SYSTEM_REPLAY`.
- Binds old output version (`target.output_version`) to the new result manifest with normalized-input dependency edges only.

Target recipe identity (`target.recipe_identity`) is not blindly equated with the new graph
configuration identity (`inputs.config.configuration_identity`). Instead,
`build_recompute_work_spec` derives `spec.configuration_identity` via
`build_recompute_configuration_identity` using canonical hashing over:
- Schema label (`"sec-metric-recompute-work-config-v2"`)
- Target canonical payload (`target.canonical_payload()`)
- Graph configuration identity (`config.configuration_identity`) and serialized graph configuration
- Normalized fact version content identities
- Quality records (identity changes for any quality record modification)
- Consumer commits (identity changes for any commit modification)
- External input attestations (identity changes for any external input modification)

## Result fields

`execute_sec_metric_recompute` returns a frozen `SecMetricRecomputeExecutionResult`
containing the following actual result fields:
- `status`: `SecExecutionStatus` representing the executor outcome (`READY`, `QUARANTINED`,
  `FAILED`, `WAITING`, or `YIELDED`).
- `spec`: The constructed `SecEventWorkSpec` binding event metadata, target identity, and normalized inputs.
- `execution_result`: The underlying `SecExecutionResult` produced by `SecEventExecutor.run`.
- `target`: The caller-supplied `SecDatedInvalidationTarget`.
- `final_value`: `Decimal | None` extracted and parsed from the replayed output manifest.
- `result_identity`: `str | None` identifying the deterministic graph calculation result.
- `computed`: `bool` indicating whether actual graph evaluation occurred (`handler.executions > 0`).
- `replayed_payload`: `dict[str, Any] | None` containing the decoded and validated output manifest.

## Idempotence, warm replay, and zero computation

When an identical recompute work specification has already reached `READY` in the
ledger's execution store:
- The executor verifies the existing validation receipts and output observations.
- Re-execution replays the retained bytes directly (`OUTPUT_BYTES_REPLAYED`).
- Zero metric graph computations and zero acquisition/validation callbacks occur.

## Crash recovery protocol

Crash recovery requires explicit, caller-supplied recovery callables:
- Both `handler_recovery` and `validator_recovery` are required; passing `None` or non-callables raises `TypeError`.
- Explicit recovery returns: `COMPLETE`, `NOT_STARTED`, or `UNKNOWN`.
- `NOT_STARTED`: Allows the handler or validator to proceed with execution.
- `COMPLETE`: Reuses the existing valid retained receipt; missing, damaged, or schema-inconsistent evidence fails closed.
- `UNKNOWN`: Halts execution with `UNCONFIRMED_SIDE_EFFECT`, preventing unsafe duplicate execution.
- Absence of receipts is never silently inferred as safe restart.

## Bounded execution and limits

- All input collections (facts, qualities, commits, external inputs) are bounded before materialization (per-collection cap 1,024, aggregate cap 1,024 items).
- Maximum input versions in work spec capped at 128.
- Maximum dependency edges capped at 128.
- Maximum output payload capped at 8 MiB (`MAX_PAYLOAD`).
- Result payload bytes use canonical JSON serialization with exact finite `Decimal` representation.

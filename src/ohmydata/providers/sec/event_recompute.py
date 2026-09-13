"""Idempotent SEC metric recompute adapter built on existing SecEventExecutor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from ...core import (
    RequestSpec,
    SnapshotMode,
    SnapshotObservationRef,
    SnapshotStore,
)
from ...core.errors import (
    ResourceLimitError,
)
from ._event_discovery_models import (
    SecDiscoveryBatch,
    SecFilingDiscoveryEvent,
    _stamp,
    _utc,
)
from ._event_execution_models import (
    SecExecutionDependency,
    SecExecutionOperation,
    SecExecutionRecovery,
    SecExecutionRecoveryResult,
    SecExecutionResult,
    SecExecutionStatus,
    SecExecutionValidation,
)
from ._event_execution_receipts import (
    ReceiptReplay,
    retain_sec_execution_outputs,
    retain_sec_execution_validation,
)
from ._event_ledger_io import (
    canonical_bytes,
)
from ._event_recompute_codec import (
    MAX_DEPENDENCY_EDGES,
    SecTargetMetricInputs,
    build_recompute_configuration_identity,
    build_recompute_work_spec,
    decode_and_validate_recompute_observation,
    validate_recompute_target_binding,
)
from ._event_work_models import (
    SecEventWorkSpec,
)
from ._metric_arithmetic import (
    validate_decimal,
)
from .dated_invalidation import (
    SecDatedInvalidationTarget,
)
from .event_dependencies import (
    SecDataVersionId,
    SecDataVersionKind,
)
from .event_execution import (
    SecEventExecutor,
)
from .event_work import (
    SecEventWorkLedger,
)
from .metric_graph import (
    SecMetricGraphResult,
    compute_sec_metric_graph,
)


@dataclass(frozen=True)
class SecMetricRecomputeExecutionResult:
    """Outcome of running recomputation via SecEventExecutor."""

    status: SecExecutionStatus
    spec: SecEventWorkSpec
    execution_result: SecExecutionResult
    target: SecDatedInvalidationTarget
    final_value: Decimal | None
    result_identity: str | None
    computed: bool
    replayed_payload: dict[str, Any] | None = None


class SecMetricRecomputeHandler:
    """Bounded execution handler evaluating graph and retaining finite Decimal output bytes."""

    def __init__(
        self,
        store: SnapshotStore,
        target: SecDatedInvalidationTarget,
        inputs: SecTargetMetricInputs,
        spec: SecEventWorkSpec,
        version: str,
        *,
        clock: Callable[[], datetime],
        recovery: Callable[[SecExecutionOperation], SecExecutionRecoveryResult],
    ) -> None:
        if type(store) is not SnapshotStore:
            raise TypeError("store must be SnapshotStore")
        if type(target) is not SecDatedInvalidationTarget:
            raise TypeError("target must be SecDatedInvalidationTarget")
        if type(inputs) is not SecTargetMetricInputs:
            raise TypeError("inputs must be SecTargetMetricInputs")
        if type(spec) is not SecEventWorkSpec:
            raise TypeError("spec must be SecEventWorkSpec")
        if recovery is None or not callable(recovery):
            raise TypeError("handler requires an explicit recovery callback")
        if clock is None or not callable(clock):
            raise TypeError("clock must be callable")
        self.store = store
        self.target = target
        self.inputs = inputs
        self.spec = spec
        self.version = version
        self.clock = clock
        self.recovery = recovery
        self.executions: int = 0
        self.recoveries: int = 0
        self.last_graph_result: SecMetricGraphResult | None = None

    def recover(self, operation: SecExecutionOperation) -> SecExecutionRecoveryResult:
        self.recoveries += 1
        if operation.spec != self.spec:
            raise ValueError("operation spec does not match expected recompute work spec")
        res = self.recovery(operation)
        if type(res) is not SecExecutionRecoveryResult:
            raise TypeError("recovery callback must return SecExecutionRecoveryResult")
        if res.status is SecExecutionRecovery.COMPLETE:
            if res.receipt is None or type(res.receipt) is not SnapshotObservationRef:
                raise ValueError("COMPLETE recovery requires valid receipt observation")
            self._validate_recovered_receipt(operation, res.receipt)
        return res

    def _validate_recovered_receipt(
        self, operation: SecExecutionOperation, receipt: SnapshotObservationRef
    ) -> None:
        now = _utc(self.clock(), "clock")
        replay = ReceiptReplay(self.store)
        outputs = replay.acquire(receipt, operation, now)
        if len(outputs) != 1:
            raise ValueError("recovered receipt must contain exactly one output observation")
        decode_and_validate_recompute_observation(
            outputs[0],
            replay,
            expected_work_identity=operation.spec.work_identity,
            expected_target=self.target,
            expected_config=self.inputs.config,
        )

    def execute(self, operation: SecExecutionOperation) -> SnapshotObservationRef:
        self.executions += 1
        if operation.spec != self.spec:
            raise ValueError("operation spec does not match expected recompute work spec")
        if operation.spec.processing_version != self.version:
            raise ValueError("operation spec processing_version mismatch")
        expected_config_id = build_recompute_configuration_identity(
            target=self.target,
            config=self.inputs.config,
            versions=self.inputs.versions,
            quality_records=self.inputs.quality_records,
            consumer_commits=self.inputs.consumer_commits,
            external_inputs=self.inputs.external_inputs,
        )
        if operation.spec.configuration_identity != expected_config_id:
            raise ValueError("operation spec configuration_identity mismatch")
        expected_inputs = tuple(
            sorted(
                {
                    SecDataVersionId(SecDataVersionKind.NORMALIZED_FACT, v.normalized_version_id)
                    for v in self.inputs.versions
                }
            )
        )
        if operation.spec.input_version_ids != expected_inputs:
            raise ValueError("operation spec input_version_ids mismatch")

        graph_result = compute_sec_metric_graph(
            config=self.inputs.config,
            versions=self.inputs.versions,
            quality_records=self.inputs.quality_records,
            consumer_commits=self.inputs.consumer_commits,
            external_inputs=self.inputs.external_inputs,
        )
        self.last_graph_result = graph_result
        if graph_result.final.value is not None:
            validate_decimal(graph_result.final.value)

        now = _utc(self.clock(), "clock")
        manifest = {
            "schema": "sec-metric-recompute-fact-v1",
            "work_identity": operation.spec.work_identity,
            "target_identity": self.target.target_identity,
            "old_output_version": self.target.output_version.canonical_payload(),
            "target_recipe_identity": self.target.recipe_identity,
            "graph_configuration_identity": self.inputs.config.configuration_identity,
            "result_identity": graph_result.result_identity,
            "final_value_identity": graph_result.final.value_identity,
            "final_value": (
                None if graph_result.final.value is None else str(graph_result.final.value)
            ),
            "final_unit": graph_result.final.unit,
            "final_currency": graph_result.final.currency,
            "terminal_identities": [t.result.value_identity for t in graph_result.terminals],
            "step_identities": [s.value_identity for s in graph_result.steps],
            "claim": "OUTPUT_BYTES_REPLAYED",
            "recorded_at": _stamp(now),
        }
        spec = RequestSpec(
            "sec",
            "metric-recompute-fact",
            {
                "work_identity": operation.spec.work_identity,
                "target_identity": self.target.target_identity,
            },
        )
        output_obs = self.store.observe(
            spec,
            canonical_bytes(manifest),
            now,
            "sec-metric-recompute-fact-v1",
            SnapshotMode.FROZEN,
        )
        return retain_sec_execution_outputs(
            store=self.store,
            operation=operation,
            outputs=(output_obs,),
            recorded_at=now,
        )


class SecMetricRecomputeValidator:
    """Validates recompute output bytes and declares all input dependency edges."""

    def __init__(
        self,
        store: SnapshotStore,
        target: SecDatedInvalidationTarget,
        inputs: SecTargetMetricInputs,
        spec: SecEventWorkSpec,
        version: str,
        *,
        clock: Callable[[], datetime],
        recovery: Callable[[SecExecutionOperation], SecExecutionRecoveryResult],
    ) -> None:
        if type(store) is not SnapshotStore:
            raise TypeError("store must be SnapshotStore")
        if type(target) is not SecDatedInvalidationTarget:
            raise TypeError("target must be SecDatedInvalidationTarget")
        if type(inputs) is not SecTargetMetricInputs:
            raise TypeError("inputs must be SecTargetMetricInputs")
        if type(spec) is not SecEventWorkSpec:
            raise TypeError("spec must be SecEventWorkSpec")
        if recovery is None or not callable(recovery):
            raise TypeError("validator requires an explicit recovery callback")
        if clock is None or not callable(clock):
            raise TypeError("clock must be callable")
        self.store = store
        self.target = target
        self.inputs = inputs
        self.spec = spec
        self.version = version
        self.clock = clock
        self.recovery = recovery
        self.recoveries: int = 0

    def recover(self, operation: SecExecutionOperation) -> SecExecutionRecoveryResult:
        self.recoveries += 1
        if operation.spec != self.spec:
            raise ValueError("operation spec does not match expected recompute work spec")
        res = self.recovery(operation)
        if type(res) is not SecExecutionRecoveryResult:
            raise TypeError("recovery callback must return SecExecutionRecoveryResult")
        return res

    def validate(
        self,
        operation: SecExecutionOperation,
        acquisition: SnapshotObservationRef,
        outputs: tuple[SnapshotObservationRef, ...],
    ) -> SnapshotObservationRef:
        if operation.spec != self.spec:
            raise ValueError("operation spec does not match expected recompute work spec")
        if len(outputs) != 1:
            raise ValueError("recompute validation requires exactly one output observation")
        output_ref = outputs[0]
        replay = ReceiptReplay(self.store)
        decode_and_validate_recompute_observation(
            output_ref,
            replay,
            expected_work_identity=operation.spec.work_identity,
            expected_target=self.target,
            expected_config=self.inputs.config,
        )

        now = _utc(self.clock(), "clock")
        evidence_spec = RequestSpec(
            "sec",
            "metric-recompute-evidence",
            {"operation_key": operation.key},
        )
        evidence_obs = self.store.observe(
            evidence_spec,
            canonical_bytes(
                {
                    "schema": "sec-metric-recompute-evidence-v1",
                    "operation_key": operation.key,
                    "ready": True,
                    "claim": "BYTE_REPLAY_VALIDATED",
                    "recorded_at": _stamp(now),
                }
            ),
            now,
            "sec-metric-recompute-evidence-v1",
            SnapshotMode.FROZEN,
        )

        if len(operation.spec.input_version_ids) > MAX_DEPENDENCY_EDGES:
            raise ResourceLimitError(
                f"execution dependency limit exceeded: {len(operation.spec.input_version_ids)}"
            )

        deps = tuple(
            SecExecutionDependency(
                input_version=in_ver,
                output_version=SecDataVersionId(
                    SecDataVersionKind.RAW_FACT, output_ref.fact_version
                ),
                recipe_identity=self.target.recipe_identity,
            )
            for in_ver in operation.spec.input_version_ids
        )

        validation_res = SecExecutionValidation(
            ready=True,
            evidence=(evidence_obs,),
            dependencies=deps,
        )
        return retain_sec_execution_validation(
            store=self.store,
            operation=operation,
            acquisition=acquisition,
            validation=validation_res,
            recorded_at=now,
            canonical_cik=self.target.canonical_cik,
        )


def execute_sec_metric_recompute(
    *,
    ledger: SecEventWorkLedger,
    store: SnapshotStore,
    batch: SecDiscoveryBatch,
    event: SecFilingDiscoveryEvent,
    target: SecDatedInvalidationTarget,
    inputs: SecTargetMetricInputs,
    deadline: datetime,
    handler_recovery: Callable[[SecExecutionOperation], SecExecutionRecoveryResult],
    validator_recovery: Callable[[SecExecutionOperation], SecExecutionRecoveryResult],
    clock: Callable[[], datetime] | None = None,
    handler_version: str = "sec-metric-recompute-v1",
    validator_version: str = "sec-metric-recompute-validator-v1",
    max_steps: int = 16,
) -> SecMetricRecomputeExecutionResult:
    """Synchronously execute recomputation for one declared target via SecEventExecutor."""
    if handler_recovery is None or not callable(handler_recovery):
        raise TypeError("execute_sec_metric_recompute requires explicit handler_recovery callback")
    if validator_recovery is None or not callable(validator_recovery):
        raise TypeError(
            "execute_sec_metric_recompute requires explicit validator_recovery callback"
        )

    now_fn = clock or (lambda: datetime.now(UTC))
    spec = build_recompute_work_spec(
        event=event,
        target=target,
        inputs=inputs,
        processing_version=handler_version,
    )
    handler = SecMetricRecomputeHandler(
        store=store,
        target=target,
        inputs=inputs,
        spec=spec,
        version=handler_version,
        clock=now_fn,
        recovery=handler_recovery,
    )
    validator = SecMetricRecomputeValidator(
        store=store,
        target=target,
        inputs=inputs,
        spec=spec,
        version=validator_version,
        clock=now_fn,
        recovery=validator_recovery,
    )
    executor = SecEventExecutor(ledger, store=store, clock=now_fn)
    exec_result = executor.run(
        batch=batch,
        spec=spec,
        handler=handler,
        validator=validator,
        deadline=deadline,
        max_steps=max_steps,
    )

    final_val: Decimal | None = None
    res_identity: str | None = None
    replayed: dict[str, Any] | None = None

    if exec_result.status is SecExecutionStatus.READY and exec_result.output_receipt is not None:
        replay = ReceiptReplay(store)
        raw_output_ref = replay.acquire(
            exec_result.output_receipt,
            SecExecutionOperation(spec, 1, validator_version),
            _utc(now_fn(), "now"),
        )[0]
        replayed = decode_and_validate_recompute_observation(
            raw_output_ref,
            replay,
            expected_work_identity=spec.work_identity,
            expected_target=target,
            expected_config=inputs.config,
        )
        if replayed.get("final_value") is not None:
            final_val = Decimal(str(replayed["final_value"]))
        res_identity = cast(str, replayed.get("result_identity"))

    return SecMetricRecomputeExecutionResult(
        status=exec_result.status,
        spec=spec,
        execution_result=exec_result,
        target=target,
        final_value=final_val,
        result_identity=res_identity,
        computed=(handler.executions > 0),
        replayed_payload=replayed,
    )


__all__ = [
    "SecMetricRecomputeExecutionResult",
    "SecMetricRecomputeHandler",
    "SecMetricRecomputeValidator",
    "SecTargetMetricInputs",
    "build_recompute_work_spec",
    "execute_sec_metric_recompute",
    "validate_recompute_target_binding",
]

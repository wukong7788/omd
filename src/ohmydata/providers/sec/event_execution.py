"""Synchronous SEC work execution with retained receipts and explicit recovery."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

from ...core import SnapshotObservationRef, SnapshotStore
from ._event_discovery_models import _canonical, _utc
from ._event_execution_io import check_root, execution_lock, read_locator, write_locator
from ._event_execution_models import (
    SecExecutionDependency,
    SecExecutionHandler,
    SecExecutionOperation,
    SecExecutionRecovery,
    SecExecutionRecoveryResult,
    SecExecutionResult,
    SecExecutionRetry,
    SecExecutionStatus,
    SecExecutionValidation,
    SecExecutionValidator,
)
from ._event_execution_receipts import (
    MAX_TOTAL,
    ReceiptReplay,
    descriptor,
    operation_copy,
    output_ids,
    reference,
    retain_sec_execution_outputs,
    retain_sec_execution_validation,
)
from ._event_work_codec import decode_spec
from ._event_work_models import (
    SecEventWorkCommand,
    SecEventWorkErrorClass,
    SecEventWorkPhase,
    SecEventWorkSpec,
)
from .errors import PermanentProviderError, SnapshotIntegrityError
from .event_discovery import SecDiscoveryBatch
from .event_work import SecEventWorkLedger

_P = SecEventWorkPhase


def _clock() -> datetime:
    return datetime.now(UTC)


def _recovery(value: SecExecutionRecoveryResult) -> SecExecutionRecoveryResult:
    if type(value) is not SecExecutionRecoveryResult:
        raise TypeError("handler recovery must return a typed result")
    return SecExecutionRecoveryResult(value.status, value.receipt)


class SecEventExecutor:
    """Drive one caller-owned ledger; callbacks must honor operation-key recovery."""

    def __init__(
        self,
        ledger: SecEventWorkLedger,
        *,
        store: SnapshotStore,
        clock: Callable[[], datetime] = _clock,
    ) -> None:
        if type(ledger) is not SecEventWorkLedger or type(store) is not SnapshotStore:
            raise TypeError("executor requires a work ledger and snapshot store")
        self.ledger, self.store, self.clock = ledger, store, clock

    def run(
        self,
        batch: SecDiscoveryBatch,
        spec: SecEventWorkSpec,
        handler: SecExecutionHandler,
        validator: SecExecutionValidator,
        *,
        deadline: datetime,
        max_steps: int = 16,
        max_replay_bytes: int = MAX_TOTAL,
    ) -> SecExecutionResult:
        """No internal sleep, publication, financial PASS, or automatic uncertain retry."""
        deadline = _utc(deadline, "deadline")
        if type(max_steps) is not int or not 1 <= max_steps <= 32:
            raise ValueError("max_steps must be in 1..32")
        if type(batch) is not SecDiscoveryBatch or type(spec) is not SecEventWorkSpec:
            raise TypeError("invalid discovery or work input")
        spec = decode_spec(spec.canonical_payload())
        if handler.version != spec.processing_version:
            raise ValueError("handler version must match processing version")
        operation_copy(SecExecutionOperation(spec, 1, validator.version))
        replay = ReceiptReplay(self.store, maximum=max_replay_bytes)
        previous_clock: datetime | None = None

        def now() -> datetime:
            nonlocal previous_clock
            value = _utc(self.clock(), "clock")
            if previous_clock is not None and value < previous_clock:
                raise ValueError("execution clock moved backwards")
            previous_clock = value
            return value

        # The lock is derived solely from the ledger root; callers cannot split its scope.
        with execution_lock(self.ledger.root) as fd:
            check_root(fd, self.ledger.root)
            head, state, registered = self.ledger.load()
            if state is not None and (
                state.spec != spec or state.discovery_batch_identity != batch.batch_identity
            ):
                raise SnapshotIntegrityError("execution work/discovery identity mismatch")
            selected = next((event for event in batch.events if event.key == spec.event_key), None)
            if selected is None or selected.metadata_digest != spec.metadata_digest:
                raise SnapshotIntegrityError("execution event metadata mismatch")
            output_ref = validation_ref = None
            steps = 0

            def result(status: SecExecutionStatus) -> SecExecutionResult:
                check_root(fd, self.ledger.root)
                return SecExecutionResult(status, state, output_ref, validation_ref, steps)

            def append(
                target: SecEventWorkPhase,
                reason: str,
                *,
                recorded_at=None,
                error=None,
                retry_at=None,
                outputs=(),
                evidence=(),
                edges=(),
            ):
                nonlocal head, state, registered
                check_root(fd, self.ledger.root)
                parent = None if head is None else head.receipt_id
                identifier = hashlib.sha256(
                    _canonical(
                        {
                            "work": spec.work_identity,
                            "parent": parent,
                            "target": target.value,
                        }
                    )
                ).hexdigest()
                self.ledger.append(
                    batch,
                    spec,
                    SecEventWorkCommand(
                        identifier,
                        now() if recorded_at is None else recorded_at,
                        target,
                        error,
                        reason,
                        retry_at,
                        outputs,
                        evidence,
                        edges,
                    ),
                    expected_receipt_id=parent,
                )
                head, state, registered = self.ledger.load()

            def locate(operation: SecExecutionOperation, kind: str):
                claim = read_locator(fd, f"{operation.key}-{kind}.json")
                return None if claim is None else reference(claim, self.store)

            def retain_locator(operation: SecExecutionOperation, kind: str, ref):
                write_locator(fd, f"{operation.key}-{kind}.json", descriptor(ref))

            def uncertain():
                append(_P.FAILED, "UNCONFIRMED_SIDE_EFFECT", error=SecEventWorkErrorClass.PERMANENT)
                return result(SecExecutionStatus.FAILED)

            def invoke(callback, *args):
                check_root(fd, self.ledger.root)
                try:
                    return callback(*args)
                finally:
                    # A moved directory no longer shares this lock's exclusion scope.
                    check_root(fd, self.ledger.root)

            for _ in range(max_steps):
                if now() >= deadline:
                    return result(SecExecutionStatus.YIELDED)
                steps += 1
                if state is None:
                    append(_P.DISCOVERED, "EXECUTOR_DISCOVERED")
                    continue
                if state.state is _P.DISCOVERED:
                    append(_P.QUEUED, "EXECUTOR_QUEUED")
                    continue
                if state.state is _P.RETRY_WAIT:
                    if state.retry_at is None or now() < state.retry_at:
                        return result(SecExecutionStatus.WAITING)
                    append(_P.QUEUED, "RETRY_DUE")
                    continue
                if state.state is _P.QUEUED:
                    append(_P.FETCHING, "EXECUTION_STARTED")
                    continue
                if state.state is _P.FAILED:
                    return result(SecExecutionStatus.FAILED)
                operation = operation_copy(
                    SecExecutionOperation(spec, state.attempts, validator.version)
                )
                if state.state is _P.FETCHING:
                    output_ref = locate(operation, "acquire")
                    if output_ref is None:
                        if now() >= deadline:
                            return result(SecExecutionStatus.YIELDED)
                        recovery = _recovery(invoke(handler.recover, operation))
                        if now() >= deadline:
                            return result(SecExecutionStatus.YIELDED)
                        if recovery.status is SecExecutionRecovery.UNKNOWN:
                            return uncertain()
                        output_ref = recovery.receipt
                        if recovery.status is SecExecutionRecovery.NOT_STARTED:
                            try:
                                output_ref = invoke(handler.execute, operation)
                            except SecExecutionRetry as exc:
                                if now() >= deadline:
                                    return result(SecExecutionStatus.YIELDED)
                                recovery = _recovery(invoke(handler.recover, operation))
                                if now() >= deadline:
                                    return result(SecExecutionStatus.YIELDED)
                                if recovery.status is SecExecutionRecovery.UNKNOWN:
                                    return uncertain()
                                if recovery.status is SecExecutionRecovery.COMPLETE:
                                    output_ref = recovery.receipt
                                else:
                                    retry_now = now()
                                    if exc.retry_at < retry_now:
                                        raise ValueError(
                                            "retry time precedes failure report"
                                        ) from exc
                                    if state.attempts >= spec.max_attempts:
                                        append(
                                            _P.FAILED,
                                            "TRANSIENT_ATTEMPTS_EXHAUSTED",
                                            error=SecEventWorkErrorClass.TRANSIENT,
                                        )
                                        return result(SecExecutionStatus.FAILED)
                                    append(
                                        _P.RETRY_WAIT,
                                        "TRANSIENT_NOT_STARTED",
                                        error=SecEventWorkErrorClass.TRANSIENT,
                                        retry_at=exc.retry_at,
                                    )
                                    return result(SecExecutionStatus.WAITING)
                            except PermanentProviderError:
                                append(
                                    _P.FAILED,
                                    "PERMANENT_ACQUISITION_FAILURE",
                                    error=SecEventWorkErrorClass.PERMANENT,
                                )
                                return result(SecExecutionStatus.FAILED)
                    if type(output_ref) is not SnapshotObservationRef:
                        raise SnapshotIntegrityError(
                            "acquisition did not return a retained receipt"
                        )
                    outputs = replay.acquire(output_ref, operation, now())
                    if output_ref.snapshot_fetched_at < state.last_recorded_at:
                        raise SnapshotIntegrityError("acquisition receipt precedes FETCHING")
                    retain_locator(operation, "acquire", output_ref)
                    if now() >= deadline:
                        return result(SecExecutionStatus.YIELDED)
                    append(_P.VALIDATING, "OUTPUT_BYTES_REPLAYED", outputs=output_ids(outputs))
                    continue

                output_ref = locate(operation, "acquire")
                if output_ref is None:
                    raise SnapshotIntegrityError("work state is missing its acquisition receipt")
                outputs = replay.acquire(output_ref, operation, now())
                if (
                    state.output_version_ids != output_ids(outputs)
                    or output_ref.snapshot_fetched_at > state.last_recorded_at
                ):
                    raise SnapshotIntegrityError("work output receipt binding mismatch")
                validation_ref = locate(operation, "validate")
                if validation_ref is None:
                    if state.state is not _P.VALIDATING:
                        raise SnapshotIntegrityError("terminal work is missing validation receipt")
                    if now() >= deadline:
                        return result(SecExecutionStatus.YIELDED)
                    recovery = _recovery(invoke(validator.recover, operation))
                    if now() >= deadline:
                        return result(SecExecutionStatus.YIELDED)
                    if recovery.status is SecExecutionRecovery.UNKNOWN:
                        return uncertain()
                    validation_ref = recovery.receipt
                    if recovery.status is SecExecutionRecovery.NOT_STARTED:
                        try:
                            validation_ref = invoke(
                                validator.validate, operation, output_ref, outputs
                            )
                        except PermanentProviderError:
                            append(
                                _P.FAILED,
                                "PERMANENT_VALIDATION_FAILURE",
                                error=SecEventWorkErrorClass.PERMANENT,
                            )
                            return result(SecExecutionStatus.FAILED)
                    if type(validation_ref) is not SnapshotObservationRef:
                        raise SnapshotIntegrityError("validation did not return a retained receipt")
                ready, evidence, edges = replay.validation(
                    validation_ref, output_ref, operation, outputs, now(), selected.cik
                )
                retain_locator(operation, "validate", validation_ref)
                quality_ids = tuple(
                    sorted(
                        {
                            validation_ref.observation_identity,
                            *(ref.observation_identity for ref in evidence),
                        }
                    )
                )
                if state.state is _P.VALIDATING:
                    if validation_ref.snapshot_fetched_at < state.last_recorded_at:
                        raise SnapshotIntegrityError("validation receipt precedes VALIDATING")
                    if now() >= deadline:
                        return result(SecExecutionStatus.YIELDED)
                    append(
                        _P.READY if ready else _P.QUARANTINED,
                        "VALIDATION_BYTES_RETAINED",
                        recorded_at=validation_ref.snapshot_fetched_at,
                        outputs=output_ids(outputs) if ready else (),
                        evidence=quality_ids if ready else (),
                        edges=edges,
                    )
                elif (
                    state.state is not (_P.READY if ready else _P.QUARANTINED)
                    or state.last_recorded_at != validation_ref.snapshot_fetched_at
                    or state.quality_evidence_ids != (quality_ids if ready else ())
                    or registered != edges
                ):
                    raise SnapshotIntegrityError("terminal validation receipt binding mismatch")
                return result(SecExecutionStatus.READY if ready else SecExecutionStatus.QUARANTINED)
            return result(SecExecutionStatus.YIELDED)


__all__ = [
    "SecEventExecutor",
    "SecExecutionDependency",
    "SecExecutionHandler",
    "SecExecutionOperation",
    "SecExecutionRecovery",
    "SecExecutionRecoveryResult",
    "SecExecutionResult",
    "SecExecutionRetry",
    "SecExecutionStatus",
    "SecExecutionValidation",
    "SecExecutionValidator",
    "retain_sec_execution_outputs",
    "retain_sec_execution_validation",
]

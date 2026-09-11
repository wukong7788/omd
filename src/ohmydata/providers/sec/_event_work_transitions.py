"""Pure validation of explicit work reports; never performs the reported work."""

from __future__ import annotations

from ._event_work_models import (
    SecEventWorkCommand,
    SecEventWorkErrorClass,
    SecEventWorkPhase,
    SecEventWorkSpec,
    SecEventWorkState,
)
from .event_dependencies import SecDependencyEdge, SecDependencyIndex
from .event_discovery import SecDiscoveryBatch
from .event_ledger import SecEventLedgerConflictError

_P = SecEventWorkPhase
_ALLOWED = {
    _P.DISCOVERED: {_P.QUEUED},
    _P.QUEUED: {_P.FETCHING},
    _P.FETCHING: {_P.VALIDATING, _P.RETRY_WAIT, _P.FAILED},
    _P.VALIDATING: {_P.READY, _P.QUARANTINED, _P.FAILED},
    _P.RETRY_WAIT: {_P.QUEUED},
}


def advance(
    batch: SecDiscoveryBatch,
    spec: SecEventWorkSpec,
    command: SecEventWorkCommand,
    previous: SecEventWorkState | None,
    registered: tuple[SecDependencyEdge, ...],
) -> tuple[SecEventWorkState, tuple[SecDependencyEdge, ...]]:
    selected = next((event for event in batch.events if event.key == spec.event_key), None)
    if selected is None or selected.metadata_digest != spec.metadata_digest:
        raise SecEventLedgerConflictError("work event is absent or metadata disagrees")
    target = command.target_state
    if previous is None:
        if target is not _P.DISCOVERED:
            raise SecEventLedgerConflictError("first work command must be DISCOVERED")
        earliest = max(source.observation.snapshot_fetched_at for source in batch.sources)
        attempts = 0
        outputs = ()
        quality = ()
    else:
        if previous.spec != spec or previous.discovery_batch_identity != batch.batch_identity:
            raise SecEventLedgerConflictError("work identity or discovery batch changed")
        if target not in _ALLOWED.get(previous.state, set()):
            raise SecEventLedgerConflictError("invalid work state transition")
        earliest = previous.last_recorded_at
        attempts = previous.attempts
        outputs = previous.output_version_ids
        quality = previous.quality_evidence_ids
        if previous.state is _P.RETRY_WAIT and (
            previous.retry_at is None or command.recorded_at < previous.retry_at
        ):
            raise SecEventLedgerConflictError("retry is not due")
    if command.recorded_at < earliest:
        raise SecEventLedgerConflictError(
            "work command precedes retained evidence or previous report"
        )
    if target is _P.FETCHING:
        attempts += 1
        if attempts > spec.max_attempts:
            raise SecEventLedgerConflictError("work attempts exhausted")
    if target is _P.RETRY_WAIT:
        if (
            command.error_class is not SecEventWorkErrorClass.TRANSIENT
            or attempts == 0
            or attempts >= spec.max_attempts
        ):
            raise SecEventLedgerConflictError("work cannot retry this failure")
        if command.retry_at is None or command.retry_at < command.recorded_at:
            raise SecEventLedgerConflictError("retry requires a non-past explicit time")
    elif target is _P.FAILED:
        if command.error_class is None or command.retry_at is not None:
            raise SecEventLedgerConflictError("failed work requires an error class without retry")
    elif command.error_class is not None or command.retry_at is not None:
        raise SecEventLedgerConflictError("unexpected error or retry fields")
    if target is _P.VALIDATING:
        if (
            not command.output_version_ids
            or command.quality_evidence_ids
            or command.dependency_edges
        ):
            raise SecEventLedgerConflictError(
                "validation requires outputs without readiness evidence"
            )
        outputs = command.output_version_ids
    elif target is _P.READY:
        if (
            not command.output_version_ids
            or command.output_version_ids != outputs
            or not command.quality_evidence_ids
        ):
            raise SecEventLedgerConflictError(
                "readiness requires preserved outputs and quality references"
            )
        quality = command.quality_evidence_ids
        for edge in command.dependency_edges:
            if (
                edge.input_version not in spec.input_version_ids
                or edge.output_version not in outputs
                or edge.canonical_cik != selected.cik
                or edge.recorded_at != command.recorded_at
            ):
                raise SecEventLedgerConflictError("readiness dependency binding disagrees")
        index = SecDependencyIndex(registered + command.dependency_edges)
        registered = index.edges
    elif command.output_version_ids or command.quality_evidence_ids or command.dependency_edges:
        raise SecEventLedgerConflictError("unexpected work result evidence")
    return SecEventWorkState(
        spec,
        batch.batch_identity,
        target,
        attempts,
        command.command_id,
        command.recorded_at,
        command.retry_at,
        outputs,
        quality,
    ), registered

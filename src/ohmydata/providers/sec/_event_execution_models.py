"""Caller interfaces for synchronous, recoverable SEC work execution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from ...core import SnapshotObservationRef
from ._event_discovery_models import _canonical, _utc
from ._event_work_models import SecEventWorkSpec, SecEventWorkState
from .event_dependencies import SecDataVersionId


class SecExecutionStatus(str, Enum):
    READY = "READY"
    QUARANTINED = "QUARANTINED"
    FAILED = "FAILED"
    WAITING = "WAITING"
    YIELDED = "YIELDED"


class SecExecutionRecovery(str, Enum):
    COMPLETE = "COMPLETE"
    NOT_STARTED = "NOT_STARTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SecExecutionOperation:
    spec: SecEventWorkSpec
    attempt: int
    validator_version: str

    def __post_init__(self) -> None:
        if type(self.spec) is not SecEventWorkSpec:
            raise TypeError("execution requires a typed work specification")
        if type(self.attempt) is not int or not 1 <= self.attempt <= self.spec.max_attempts:
            raise ValueError("invalid execution attempt")
        if (
            type(self.validator_version) is not str
            or not self.validator_version
            or len(self.validator_version.encode()) > 128
        ):
            raise ValueError("validator version must be nonempty and bounded")

    @property
    def key(self) -> str:
        return hashlib.sha256(
            _canonical(
                {
                    "schema": "sec-event-operation-v1",
                    "work_identity": self.spec.work_identity,
                    "attempt": self.attempt,
                }
            )
        ).hexdigest()


@dataclass(frozen=True)
class SecExecutionRecoveryResult:
    status: SecExecutionRecovery
    receipt: SnapshotObservationRef | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not SecExecutionRecovery:
            raise TypeError("invalid execution recovery status")
        if self.status is SecExecutionRecovery.COMPLETE:
            if type(self.receipt) is not SnapshotObservationRef:
                raise TypeError("complete recovery requires a retained receipt")
        elif self.receipt is not None:
            raise ValueError("only complete recovery may carry a receipt")


@dataclass(frozen=True)
class SecExecutionDependency:
    input_version: SecDataVersionId
    output_version: SecDataVersionId
    recipe_identity: str


@dataclass(frozen=True)
class SecExecutionValidation:
    ready: bool
    evidence: tuple[SnapshotObservationRef, ...]
    dependencies: tuple[SecExecutionDependency, ...] = ()


@dataclass(frozen=True)
class SecExecutionResult:
    status: SecExecutionStatus
    state: SecEventWorkState | None
    output_receipt: SnapshotObservationRef | None
    validation_receipt: SnapshotObservationRef | None
    steps_completed: int


class SecExecutionRetry(Exception):
    """Declared transient failure; recovery must confirm NOT_STARTED before retry."""

    def __init__(self, retry_at: datetime) -> None:
        self.retry_at = _utc(retry_at, "retry_at")
        super().__init__("caller declared retryable acquisition failure")


class SecExecutionHandler(Protocol):
    version: str

    def recover(self, operation: SecExecutionOperation) -> SecExecutionRecoveryResult: ...

    def execute(self, operation: SecExecutionOperation) -> SnapshotObservationRef:
        """Retain outputs using retain_sec_execution_outputs; return that fixed receipt."""
        ...


class SecExecutionValidator(Protocol):
    version: str

    def recover(self, operation: SecExecutionOperation) -> SecExecutionRecoveryResult: ...

    def validate(
        self,
        operation: SecExecutionOperation,
        acquisition: SnapshotObservationRef,
        outputs: tuple[SnapshotObservationRef, ...],
    ) -> SnapshotObservationRef:
        """Retain a fixed validation receipt; never publish to consumers."""
        ...

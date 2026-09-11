"""Immutable caller-reported SEC work commands and state transitions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ._event_discovery_models import _canonical, _stamp, _utc
from .event_dependencies import SecDataVersionId, SecDependencyEdge, _identity


class SecEventWorkPhase(str, Enum):
    DISCOVERED = "DISCOVERED"
    QUEUED = "QUEUED"
    FETCHING = "FETCHING"
    VALIDATING = "VALIDATING"
    READY = "READY"
    QUARANTINED = "QUARANTINED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"


class SecEventWorkErrorClass(str, Enum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"


@dataclass(frozen=True)
class SecEventWorkSpec:
    event_key: str
    metadata_digest: str
    processing_version: str
    configuration_identity: str
    input_version_ids: tuple[SecDataVersionId, ...]
    max_attempts: int

    def __post_init__(self) -> None:
        for identity in (self.event_key, self.metadata_digest, self.configuration_identity):
            _identity(identity)
        if (
            type(self.processing_version) is not str
            or not self.processing_version
            or len(self.processing_version.encode()) > 1024
        ):
            raise ValueError("processing version must be nonempty and bounded")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3:
            raise ValueError("max_attempts must be in 1..3 total attempts")
        _versions(self.input_version_ids)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": "sec-event-work-spec-v1",
            "event_key": self.event_key,
            "metadata_digest": self.metadata_digest,
            "processing_version": self.processing_version,
            "configuration_identity": self.configuration_identity,
            "input_version_ids": [value.canonical_payload() for value in self.input_version_ids],
            "max_attempts": self.max_attempts,
        }

    @property
    def work_identity(self) -> str:
        return hashlib.sha256(_canonical(self.canonical_payload())).hexdigest()


def _versions(values: tuple[SecDataVersionId, ...]) -> None:
    if type(values) is not tuple or len(values) > 1_024:
        raise ValueError("version identities must be a bounded tuple")
    if any(type(value) is not SecDataVersionId for value in values):
        raise TypeError("version identities must be typed")
    if tuple(sorted(set(values))) != values:
        raise ValueError("version identities must be sorted and unique")


@dataclass(frozen=True)
class SecEventWorkCommand:
    command_id: str
    recorded_at: datetime
    target_state: SecEventWorkPhase
    error_class: SecEventWorkErrorClass | None
    reason_code: str
    retry_at: datetime | None
    output_version_ids: tuple[SecDataVersionId, ...]
    quality_evidence_ids: tuple[str, ...]
    dependency_edges: tuple[SecDependencyEdge, ...]

    def __post_init__(self) -> None:
        _identity(self.command_id)
        object.__setattr__(self, "recorded_at", _utc(self.recorded_at, "recorded_at"))
        if type(self.target_state) is not SecEventWorkPhase:
            raise TypeError("invalid work target state")
        if self.error_class is not None and type(self.error_class) is not SecEventWorkErrorClass:
            raise TypeError("invalid work error class")
        if (
            type(self.reason_code) is not str
            or not self.reason_code
            or len(self.reason_code.encode()) > 1024
        ):
            raise ValueError("reason_code must be nonempty and bounded")
        if self.retry_at is not None:
            object.__setattr__(self, "retry_at", _utc(self.retry_at, "retry_at"))
        _versions(self.output_version_ids)
        if type(self.quality_evidence_ids) is not tuple or len(self.quality_evidence_ids) > 1_024:
            raise ValueError("quality evidence must be a bounded tuple")
        for value in self.quality_evidence_ids:
            _identity(value)
        if tuple(sorted(set(self.quality_evidence_ids))) != self.quality_evidence_ids:
            raise ValueError("quality evidence must be sorted and unique")
        if type(self.dependency_edges) is not tuple or len(self.dependency_edges) > 10_000:
            raise ValueError("dependency edges must be a bounded tuple")
        if any(type(value) is not SecDependencyEdge for value in self.dependency_edges):
            raise TypeError("invalid dependency edges")
        ids = tuple(edge.edge_identity for edge in self.dependency_edges)
        if tuple(sorted(set(ids))) != ids:
            raise ValueError("dependency edges must be sorted and unique")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "recorded_at": _stamp(self.recorded_at),
            "schema": "sec-event-work-command-v1",
            "target_state": self.target_state.value,
            "error_class": None if self.error_class is None else self.error_class.value,
            "reason_code": self.reason_code,
            "retry_at": None if self.retry_at is None else _stamp(self.retry_at),
            "output_version_ids": [value.canonical_payload() for value in self.output_version_ids],
            "quality_evidence_ids": list(self.quality_evidence_ids),
            "dependency_edges": [edge.canonical_payload() for edge in self.dependency_edges],
        }

    @property
    def command_identity(self) -> str:
        return hashlib.sha256(_canonical(self.canonical_payload())).hexdigest()


@dataclass(frozen=True)
class SecEventWorkState:
    spec: SecEventWorkSpec
    discovery_batch_identity: str
    state: SecEventWorkPhase
    attempts: int
    last_command_id: str
    last_recorded_at: datetime
    retry_at: datetime | None
    output_version_ids: tuple[SecDataVersionId, ...]
    quality_evidence_ids: tuple[str, ...]

    def canonical_payload(self) -> dict[str, object]:
        return {
            "spec": self.spec.canonical_payload(),
            "schema": "sec-event-work-state-v1",
            "discovery_batch_identity": self.discovery_batch_identity,
            "state": self.state.value,
            "attempts": self.attempts,
            "last_command_id": self.last_command_id,
            "last_recorded_at": _stamp(self.last_recorded_at),
            "retry_at": None if self.retry_at is None else _stamp(self.retry_at),
            "output_version_ids": [value.canonical_payload() for value in self.output_version_ids],
            "quality_evidence_ids": list(self.quality_evidence_ids),
        }

    @property
    def state_identity(self) -> str:
        return hashlib.sha256(_canonical(self.canonical_payload())).hexdigest()


@dataclass(frozen=True)
class SecEventWorkReceipt:
    generation: int
    receipt_id: str
    parent_receipt_id: str | None
    work_identity: str
    command_id: str
    command_identity: str
    state_identity: str

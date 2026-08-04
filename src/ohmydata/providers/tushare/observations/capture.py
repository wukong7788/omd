# pyright: reportUnnecessaryIsInstance=false
"""Deterministic capture of validated Tushare fetch results into snapshots."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from ....core import (
    AvailabilityBasis,
    AvailabilityEvidence,
    AvailabilityPrecision,
    FetchProvenance,
    RequestSpec,
    SnapshotMode,
    SnapshotObservationRef,
    SnapshotStore,
)
from ....core.errors import SnapshotIntegrityError
from ..client import TushareFetchResult
from .serialization import (
    SERIALIZATION_IDENTIFIER,
    deserialize_tushare_frame,
    serialize_tushare_frame,
)


@runtime_checkable
class SupportedTushareRequest(Protocol):
    """Minimal request surface required by the capture path."""

    endpoint: str
    spec: RequestSpec


@dataclass(frozen=True, init=False)
class TushareObservedResult:
    """Immutable defensive view of one captured Tushare observation."""

    _frame: Any
    provenance: FetchProvenance
    observation: SnapshotObservationRef
    availability: AvailabilityEvidence
    serialization_identifier: str
    content_sha256: str
    row_count: int
    columns: tuple[str, ...]

    def __init__(
        self,
        frame: Any,
        provenance: FetchProvenance,
        observation: SnapshotObservationRef,
        availability: AvailabilityEvidence,
        serialization_identifier: str,
        content_sha256: str,
    ) -> None:
        if not isinstance(provenance, FetchProvenance):
            raise TypeError("provenance must be FetchProvenance")
        if not isinstance(observation, SnapshotObservationRef):
            raise TypeError("observation must be SnapshotObservationRef")
        if not isinstance(availability, AvailabilityEvidence):
            raise TypeError("availability must be AvailabilityEvidence")
        if type(serialization_identifier) is not str or not serialization_identifier:
            raise ValueError("serialization_identifier must be a non-empty string")
        if type(content_sha256) is not str or len(content_sha256) != 64:
            raise ValueError("content_sha256 must be a SHA-256 hex digest")
        if content_sha256 != observation.response_sha256:
            raise ValueError("content_sha256 mismatch against observation")
        if observation.request_identity != provenance.request_identity:
            raise ValueError("observation request identity mismatch")
        if availability.snapshot_fetched_at != observation.snapshot_fetched_at:
            raise ValueError("availability snapshot timestamp mismatch")
        columns = tuple(str(column) for column in frame.columns)
        row_count = len(frame)
        if row_count != provenance.row_count or columns != tuple(provenance.columns):
            raise ValueError("frame shape mismatch against provenance")
        object.__setattr__(self, "_frame", frame.copy(deep=True))
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "observation", observation)
        object.__setattr__(self, "availability", availability)
        object.__setattr__(self, "serialization_identifier", serialization_identifier)
        object.__setattr__(self, "content_sha256", content_sha256)
        object.__setattr__(self, "row_count", row_count)
        object.__setattr__(self, "columns", columns)

    @property
    def frame(self) -> Any:
        return self._frame.copy(deep=True)

    @property
    def request_identity(self) -> str:
        return self.observation.request_identity

    @property
    def response_sha256(self) -> str:
        return self.observation.response_sha256

    @property
    def snapshot_identity(self) -> str:
        return self.observation.snapshot_identity

    @property
    def observation_identity(self) -> str:
        return self.observation.observation_identity

    @property
    def fact_version(self) -> str:
        return self.observation.fact_version

    @property
    def snapshot_fetched_at(self) -> datetime:
        return self.observation.snapshot_fetched_at


def _validated_result(
    store: SnapshotStore,
    request: SupportedTushareRequest,
    result: TushareFetchResult,
    observed_at: datetime,
) -> tuple[bytes, SnapshotObservationRef, AvailabilityEvidence]:
    if not isinstance(store, SnapshotStore):
        raise TypeError("store must be SnapshotStore")
    if not isinstance(result, TushareFetchResult):
        raise TypeError("result must be TushareFetchResult")
    if not isinstance(request, SupportedTushareRequest):
        raise TypeError("request must be a SupportedTushareRequest")
    if type(request.endpoint) is not str or not request.endpoint:
        raise ValueError("request endpoint must be a non-empty string")
    if request.spec.provider != "tushare":
        raise ValueError("request must belong to the tushare provider")
    if (
        type(observed_at) is not datetime
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise TypeError("observed_at must be a timezone-aware datetime")
    if result.provenance.request_identity != request.spec.request_identity:
        raise ValueError("result provenance does not match the request")
    payload = serialize_tushare_frame(result.frame)
    observation = store.observe(
        request.spec,
        payload,
        observed_at,
        SERIALIZATION_IDENTIFIER,
        SnapshotMode.APPEND,
    )
    replay = store.replay_observation(observation, request.spec)
    rebuilt = deserialize_tushare_frame(replay.payload)
    if serialize_tushare_frame(rebuilt) != replay.payload:
        raise SnapshotIntegrityError("frame serialization is not deterministic")
    availability = AvailabilityEvidence.from_observation(
        store,
        observation,
        availability_basis=AvailabilityBasis.PROVIDER_FIRST_OBSERVED,
        availability_precision=AvailabilityPrecision.UNKNOWN,
    )
    return replay.payload, observation, availability


def capture_tushare_result(
    store: SnapshotStore,
    request: SupportedTushareRequest,
    result: TushareFetchResult,
    *,
    observed_at: datetime,
) -> TushareObservedResult:
    """Capture an already validated fetch result into an append-only snapshot.

    The provider-native frame is serialized deterministically, stored with
    ``SnapshotMode.APPEND``, replayed and round-trip verified before this
    function returns. The function never contacts a provider or reads
    credentials.
    """
    payload, observation, availability = _validated_result(store, request, result, observed_at)
    content_sha256 = hashlib.sha256(payload).hexdigest()
    if content_sha256 != observation.response_sha256:
        raise SnapshotIntegrityError("content hash mismatch against snapshot")
    return TushareObservedResult(
        result.frame,
        result.provenance,
        observation,
        availability,
        SERIALIZATION_IDENTIFIER,
        content_sha256,
    )


__all__ = ["SupportedTushareRequest", "TushareObservedResult", "capture_tushare_result"]

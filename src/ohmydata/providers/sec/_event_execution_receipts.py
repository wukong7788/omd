"""Fixed execution receipts bind retained bytes, not financial truth."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from ...core import RequestSpec, SnapshotMode, SnapshotObservationRef, SnapshotStore
from . import _event_ledger_io as io
from ._event_discovery_models import SecDiscoverySource, _stamp, _utc
from ._event_execution_io import MAX_RECEIPT_BYTES
from ._event_execution_models import (
    SecExecutionDependency,
    SecExecutionOperation,
    SecExecutionValidation,
)
from ._event_work_codec import _edge, decode_spec
from .errors import ResourceLimitError, SnapshotIntegrityError
from .event_dependencies import SecDataVersionId, SecDataVersionKind, SecDependencyEdge

MAX_PAYLOAD = 8 * 1024**2
MAX_TOTAL = 32 * 1024**2


def descriptor(ref: SnapshotObservationRef) -> dict[str, Any]:
    if type(ref) is not SnapshotObservationRef:
        raise TypeError("execution artifacts must be snapshot observations")
    return cast(
        dict[str, Any],
        SecDiscoverySource("execution-local", ref).canonical_payload()["observation"],
    )


def reference(value: object, store: SnapshotStore) -> SnapshotObservationRef:
    if type(value) is not dict:
        raise SnapshotIntegrityError("invalid execution observation descriptor")
    return SecDiscoverySource.from_canonical_payload(
        {"url": "execution-local", "observation": value},
        store,
    ).observation


def operation_copy(operation: SecExecutionOperation) -> SecExecutionOperation:
    if type(operation) is not SecExecutionOperation:
        raise TypeError("invalid execution operation")
    raw = io.canonical_bytes(operation.spec.canonical_payload())
    spec = decode_spec(io.decode_manifest(raw))
    return SecExecutionOperation(spec, operation.attempt, operation.validator_version)


def request(operation: SecExecutionOperation, kind: str) -> RequestSpec:
    return RequestSpec(
        "sec", "event-execution-receipt", {"operation_key": operation.key, "kind": kind}
    )


def base(operation: SecExecutionOperation, kind: str, recorded_at: datetime) -> dict[str, Any]:
    return {
        "schema": f"sec-event-execution-{kind}-v1",
        "operation_key": operation.key,
        "spec": operation.spec.canonical_payload(),
        "attempt": operation.attempt,
        "handler_version": operation.spec.processing_version,
        "validator_version": operation.validator_version,
        "recorded_at": _stamp(recorded_at),
    }


def output_ids(outputs: tuple[SnapshotObservationRef, ...]) -> tuple[SecDataVersionId, ...]:
    return tuple(
        sorted({SecDataVersionId(SecDataVersionKind.RAW_FACT, ref.fact_version) for ref in outputs})
    )


class ReceiptReplay:
    def __init__(self, store: SnapshotStore, *, maximum: int = MAX_TOTAL) -> None:
        if type(store) is not SnapshotStore:
            raise TypeError("execution requires SnapshotStore")
        if type(maximum) is not int or not 1 <= maximum <= MAX_TOTAL:
            raise ValueError("invalid execution replay budget")
        self.store, self.maximum, self.used = store, maximum, 0
        self.cache: dict[str, tuple[dict[str, Any], bytes]] = {}

    def replay(self, supplied: SnapshotObservationRef, *, limit: int = MAX_PAYLOAD) -> bytes:
        claim = descriptor(supplied)
        ref = reference(claim, self.store)
        # Reconstruct the path from validated store-relative identities.
        if ref.provider != "sec":
            raise SnapshotIntegrityError("execution artifact provider must be SEC")
        cached = self.cache.get(ref.observation_identity)
        if cached is not None:
            if cached[0] != claim or len(cached[1]) > limit:
                raise SnapshotIntegrityError(
                    "conflicting or oversized cached execution observation"
                )
            return cached[1]
        remaining = self.maximum - self.used
        if remaining <= 0:
            raise ResourceLimitError("execution aggregate replay budget exceeded")
        raw = self.store.replay_observation(ref, max_payload_bytes=min(limit, remaining)).payload
        self.used += len(raw)
        self.cache[ref.observation_identity] = (claim, raw)
        return raw

    def observations(
        self, value: object, recorded_at: datetime
    ) -> tuple[SnapshotObservationRef, ...]:
        if type(value) is not list or not 1 <= len(value) <= 16:
            raise ResourceLimitError("execution requires 1..16 retained artifacts")
        refs = tuple(reference(item, self.store) for item in value)
        ids = tuple(ref.observation_identity for ref in refs)
        if ids != tuple(sorted(set(ids))):
            raise SnapshotIntegrityError("execution artifact references must be sorted and unique")
        for ref in refs:
            if ref.snapshot_fetched_at > recorded_at:
                raise SnapshotIntegrityError("execution receipt precedes its evidence")
            self.replay(ref)
        return refs

    def receipt(
        self,
        ref: SnapshotObservationRef,
        operation: SecExecutionOperation,
        kind: str,
        now: datetime,
        extra_keys: set[str],
    ) -> dict[str, Any]:
        raw = self.replay(ref, limit=MAX_RECEIPT_BYTES)
        value = io.decode_manifest(raw)
        expected = base(operation, kind, ref.snapshot_fetched_at)
        if (
            ref.endpoint != "event-execution-receipt"
            or ref.mode is not SnapshotMode.FROZEN
            or ref.request_identity != request(operation, kind).request_identity
            or ref.serialization_identifier != expected["schema"]
            or ref.snapshot_fetched_at > now
            or set(value) != set(expected) | extra_keys
            or io.canonical_bytes({key: value[key] for key in expected})
            != io.canonical_bytes(expected)
        ):
            raise SnapshotIntegrityError("execution receipt operation or time mismatch")
        return value

    def acquire(
        self, ref: SnapshotObservationRef, operation: SecExecutionOperation, now: datetime
    ) -> tuple[SnapshotObservationRef, ...]:
        data = self.receipt(ref, operation, "acquire", now, {"outputs"})
        return self.observations(data["outputs"], ref.snapshot_fetched_at)

    def validation(
        self,
        ref: SnapshotObservationRef,
        acquired: SnapshotObservationRef,
        operation: SecExecutionOperation,
        outputs: tuple[SnapshotObservationRef, ...],
        now: datetime,
        cik: str,
    ) -> tuple[bool, tuple[SnapshotObservationRef, ...], tuple[SecDependencyEdge, ...]]:
        data = self.receipt(
            ref, operation, "validate", now, {"acquisition", "ready", "evidence", "dependencies"}
        )
        if (
            data["acquisition"] != descriptor(acquired)
            or type(data["ready"]) is not bool
            or ref.snapshot_fetched_at < acquired.snapshot_fetched_at
        ):
            raise SnapshotIntegrityError("validation acquisition binding mismatch")
        evidence = self.observations(data["evidence"], ref.snapshot_fetched_at)
        if type(data["dependencies"]) is not list or len(data["dependencies"]) > 128:
            raise ResourceLimitError("execution dependency limit exceeded")
        edges = tuple(_edge(item) for item in data["dependencies"])
        ids = tuple(edge.edge_identity for edge in edges)
        if ids != tuple(sorted(set(ids))):
            raise SnapshotIntegrityError("execution dependencies must be sorted and unique")
        for edge in edges:
            if (
                edge.input_version not in operation.spec.input_version_ids
                or edge.output_version not in output_ids(outputs)
                or edge.canonical_cik != cik
                or edge.recorded_at != ref.snapshot_fetched_at
            ):
                raise SnapshotIntegrityError("execution dependency binding mismatch")
        if not data["ready"] and edges:
            raise SnapshotIntegrityError("quarantined execution cannot register dependencies")
        return data["ready"], evidence, edges


def retain_sec_execution_outputs(
    *,
    store: SnapshotStore,
    operation: SecExecutionOperation,
    outputs: tuple[SnapshotObservationRef, ...],
    recorded_at: datetime,
) -> SnapshotObservationRef:
    """Retain a fixed receipt after replaying the caller's output snapshot bytes."""
    operation = operation_copy(operation)
    recorded_at = _utc(recorded_at, "recorded_at")
    if type(outputs) is not tuple or not 1 <= len(outputs) <= 16:
        raise ResourceLimitError("execution requires 1..16 outputs")
    claims = sorted(
        (descriptor(ref) for ref in outputs), key=lambda item: item["observation_identity"]
    )
    ReceiptReplay(store).observations(claims, recorded_at)
    data: dict[str, Any] = {**base(operation, "acquire", recorded_at), "outputs": claims}
    raw = io.canonical_bytes(data)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ResourceLimitError("execution receipt byte limit exceeded")
    return store.observe(
        request(operation, "acquire"), raw, recorded_at, data["schema"], SnapshotMode.FROZEN
    )


def retain_validation(
    replay: ReceiptReplay,
    operation: SecExecutionOperation,
    acquired: SnapshotObservationRef,
    outputs: tuple[SnapshotObservationRef, ...],
    result: SecExecutionValidation,
    recorded_at: datetime,
    cik: str,
) -> SnapshotObservationRef:
    if type(result) is not SecExecutionValidation or type(result.ready) is not bool:
        raise TypeError("invalid execution validation result")
    if type(result.evidence) is not tuple or not 1 <= len(result.evidence) <= 16:
        raise ResourceLimitError("execution requires 1..16 validation evidence observations")
    claims = sorted(
        (descriptor(ref) for ref in result.evidence), key=lambda item: item["observation_identity"]
    )
    replay.observations(claims, recorded_at)
    if type(result.dependencies) is not tuple or len(result.dependencies) > 128:
        raise ResourceLimitError("execution dependency limit exceeded")
    edges = []
    for item in result.dependencies:
        if type(item) is not SecExecutionDependency:
            raise TypeError("invalid execution dependency declaration")
        edges.append(
            SecDependencyEdge(
                item.input_version, item.output_version, cik, item.recipe_identity, recorded_at
            )
        )
    edges.sort(key=lambda edge: edge.edge_identity)
    data: dict[str, Any] = {
        **base(operation, "validate", recorded_at),
        "acquisition": descriptor(acquired),
        "ready": result.ready,
        "evidence": claims,
        "dependencies": [edge.canonical_payload() for edge in edges],
    }
    # Validate all semantic bindings before persisting a validation receipt.
    ids = output_ids(outputs)
    if (not result.ready and edges) or len({edge.edge_identity for edge in edges}) != len(edges):
        raise SnapshotIntegrityError("invalid validation dependency set")
    if any(
        edge.input_version not in operation.spec.input_version_ids or edge.output_version not in ids
        for edge in edges
    ):
        raise SnapshotIntegrityError("validation dependency binding mismatch")
    if recorded_at < acquired.snapshot_fetched_at:
        raise SnapshotIntegrityError("validation precedes acquisition")
    raw = io.canonical_bytes(data)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ResourceLimitError("execution receipt byte limit exceeded")
    return replay.store.observe(
        request(operation, "validate"), raw, recorded_at, data["schema"], SnapshotMode.FROZEN
    )


def retain_sec_execution_validation(
    *,
    store: SnapshotStore,
    operation: SecExecutionOperation,
    acquisition: SnapshotObservationRef,
    validation: SecExecutionValidation,
    recorded_at: datetime,
    canonical_cik: str,
) -> SnapshotObservationRef:
    """Retain an operation-bound validation decision and actual evidence bytes."""
    operation = operation_copy(operation)
    recorded_at = _utc(recorded_at, "recorded_at")
    replay = ReceiptReplay(store)
    outputs = replay.acquire(acquisition, operation, recorded_at)
    return retain_validation(
        replay, operation, acquisition, outputs, validation, recorded_at, canonical_cik
    )

"""Bounded collections, seal validations, and strict decoders for SEC metric recompute."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import islice
from typing import Any, TypeVar

from ...core import RequestSpec, SnapshotMode, SnapshotObservationRef
from ...core.errors import ResourceLimitError
from ._event_discovery_models import SecFilingDiscoveryEvent, _stamp, _utc
from ._event_discovery_models import _canonical as _canonical_discovery
from ._event_execution_receipts import MAX_PAYLOAD, ReceiptReplay
from ._event_ledger_io import decode_manifest
from ._event_work_models import SecEventWorkSpec
from ._metric_arithmetic import validate_decimal
from ._metric_common import encoded, identity
from ._metric_graph_models import SecMetricGraphConfig
from ._metric_inputs import SecMetricExternalInput
from .dated_invalidation import SecDatedInvalidationTarget
from .event_dependencies import SecDataVersionId, SecDataVersionKind
from .pit import (
    SecConsumerCommit,
    SecNormalizedFinancialFactVersion,
    SecQualityRecord,
    _hash,
    _normalized_content_payload,
)

T = TypeVar("T")

MAX_INPUT_COLLECTION_SIZE: int = 1024
MAX_AGGREGATE_INPUT_SIZE: int = 1024
MAX_RECOMPUTE_VERSIONS: int = 128
MAX_DEPENDENCY_EDGES: int = 128
MAX_STRING_FIELD_LENGTH: int = 1024
EXACT_MANIFEST_FIELDS: frozenset[str] = frozenset(
    (
        "schema",
        "work_identity",
        "target_identity",
        "old_output_version",
        "target_recipe_identity",
        "graph_configuration_identity",
        "result_identity",
        "final_value_identity",
        "final_value",
        "final_unit",
        "final_currency",
        "terminal_identities",
        "step_identities",
        "claim",
        "recorded_at",
    )
)


def _sha_check(value: object, name: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{name} must be a 64-character lowercase hex string")
    return value


def bounded_typed_collection(
    iterable: Iterable[Any],
    expected_type: type[T],
    name: str,
    *,
    remaining_budget: int,
    max_collection_size: int = MAX_INPUT_COLLECTION_SIZE,
) -> tuple[tuple[T, ...], int]:
    """Iterate up to remaining budget and max collection size before materialization.

    Bounds generator/iterator consumption using islice before list/tuple allocation.
    Enforces exact typed elements and raises ResourceLimitError if collection exceeds
    its cap or remaining aggregate budget.
    """
    if remaining_budget < 0:
        raise ResourceLimitError(
            f"aggregate input items exceed limit of {MAX_AGGREGATE_INPUT_SIZE}"
        )
    effective_limit = min(remaining_budget, max_collection_size)
    items: list[T] = []
    # Take effective_limit + 1 to detect overflow without unbounded consumption
    for item in islice(iterable, effective_limit + 1):
        if len(items) >= effective_limit:
            if remaining_budget <= len(items) and effective_limit == remaining_budget:
                raise ResourceLimitError(
                    f"aggregate input items exceed limit of {MAX_AGGREGATE_INPUT_SIZE}"
                )
            raise ResourceLimitError(
                f"{name} collection exceeds limit of {max_collection_size} items"
            )
        if type(item) is not expected_type:
            raise TypeError(
                f"{name} elements must be exact {expected_type.__name__}, got {type(item).__name__}"
            )
        items.append(item)
    return tuple(items), remaining_budget - len(items)


@dataclass(frozen=True)
class SecTargetMetricInputs:
    """Explicit, caller-injected inputs to compute a declared target metric graph.

    Input collections are bounded before materialization:
    - Maximum per-collection cap: 1,024 items.
    - Maximum aggregate cap across all 4 collections: 1,024 items.
    - Exact typed elements are strictly required.
    """

    config: SecMetricGraphConfig
    versions: tuple[SecNormalizedFinancialFactVersion, ...]
    quality_records: tuple[SecQualityRecord, ...]
    consumer_commits: tuple[SecConsumerCommit, ...] = ()
    external_inputs: tuple[SecMetricExternalInput, ...] = ()

    def __post_init__(self) -> None:
        if type(self.config) is not SecMetricGraphConfig:
            raise TypeError("config must be a SecMetricGraphConfig")
        budget = MAX_AGGREGATE_INPUT_SIZE
        for attr, coll, typ in (
            ("versions", self.versions, SecNormalizedFinancialFactVersion),
            ("quality_records", self.quality_records, SecQualityRecord),
            ("consumer_commits", self.consumer_commits, SecConsumerCommit),
            ("external_inputs", self.external_inputs, SecMetricExternalInput),
        ):
            items, budget = bounded_typed_collection(
                coll,
                typ,
                attr,
                remaining_budget=budget,
                max_collection_size=MAX_INPUT_COLLECTION_SIZE,
            )
            object.__setattr__(self, attr, items)


def validate_target_seal(target: SecDatedInvalidationTarget) -> None:
    """Validate that target has not been mutated and seals match canonical content."""
    if type(target) is not SecDatedInvalidationTarget:
        raise TypeError("target must be SecDatedInvalidationTarget")
    canonical_payload = target.canonical_payload()
    expected_id = hashlib.sha256(_canonical_discovery(canonical_payload)).hexdigest()
    if target.target_identity != expected_id:
        raise ValueError("target identity mismatch or mutated")
    if target.output_version.kind is not SecDataVersionKind.DERIVED_METRIC:
        raise ValueError("target output_version must be DERIVED_METRIC")
    _sha_check(target.output_version.identity, "target output_version identity")


def validate_config_seal(config: SecMetricGraphConfig) -> None:
    """Validate that graph config has not been mutated and identity matches canonical content."""
    if type(config) is not SecMetricGraphConfig:
        raise TypeError("config must be SecMetricGraphConfig")
    expected_id = identity("sec-metric-graph-config-v1", config)
    if config.configuration_identity != expected_id:
        raise ValueError("graph config identity mismatch or mutated")


def validate_version_seal(v: SecNormalizedFinancialFactVersion) -> None:
    """Reconstruct and validate seals for a normalized financial fact version."""
    if type(v) is not SecNormalizedFinancialFactVersion:
        raise TypeError("version must be SecNormalizedFinancialFactVersion")
    content = _normalized_content_payload(
        observation=v.observation,
        accession_number=v.accession_number,
        source_artifact_identity=v.source_artifact_identity,
        source_available_at=v.source_available_at,
        vintage_identity=v.vintage_identity,
        row_ordinal=v.row_ordinal,
        row=v.row,
        schema_version=v.schema_version,
        adapter_version=v.adapter_version,
        normalization_version=v.normalization_version,
        configuration_identity=v.configuration_identity,
    )
    content_id = _hash(content)
    expected_binding = _hash(
        {
            "content": content,
            "recorded_at": v.recorded_at,
            "projection_payload_sha256": v.observation.response_sha256,
        }
    )
    expected_v_id = _hash({"content_identity": content_id, "recorded_at": v.recorded_at})
    if (
        v.content_identity != content_id
        or v.normalized_version_id != expected_v_id
        or v._projection_binding_identity != expected_binding
    ):
        raise ValueError("normalized financial fact version mutated or invalid seal")


def validate_quality_record_seal(q: SecQualityRecord) -> None:
    """Validate quality record seal against its canonical content."""
    if type(q) is not SecQualityRecord:
        raise TypeError("quality record must be SecQualityRecord")
    expected_id = _hash(
        {
            "normalized_version_id": q.normalized_version_id,
            "quality_policy_version": q.quality_policy_version,
            "status": q.status.value,
            "recorded_at": q.recorded_at,
            "supersedes_quality_record_id": q.supersedes_quality_record_id,
        }
    )
    if q.quality_record_id != expected_id:
        raise ValueError("quality record mutated or invalid seal")


def validate_consumer_commit_seal(c: SecConsumerCommit) -> None:
    """Validate consumer commit seal against its canonical content."""
    if type(c) is not SecConsumerCommit:
        raise TypeError("consumer commit must be SecConsumerCommit")
    expected_id = _hash(
        {
            "normalized_version_id": c.normalized_version_id,
            "quality_record_id": c.quality_record_id,
            "consumer_dataset_identity": c.consumer_dataset_identity,
            "committed_at": c.committed_at,
        }
    )
    if c.commit_id != expected_id:
        raise ValueError("consumer commit mutated or invalid seal")


def validate_external_input_seal(ext: SecMetricExternalInput) -> None:
    """Validate external input seal against its canonical content."""
    if type(ext) is not SecMetricExternalInput:
        raise TypeError("external input must be SecMetricExternalInput")
    expected_id = identity("sec-metric-external-attestation-v1", ext)
    if ext.external_id != expected_id:
        raise ValueError("external input mutated or invalid seal")


def validate_recompute_target_binding(
    *,
    event: SecFilingDiscoveryEvent,
    target: SecDatedInvalidationTarget,
    inputs: SecTargetMetricInputs,
) -> None:
    """Validate strict consistency across event, target, and graph config."""
    if event.cik != target.canonical_cik:
        raise ValueError("event CIK disagrees with target canonical CIK")
    if inputs.config.canonical_cik != target.canonical_cik:
        raise ValueError("graph config CIK disagrees with target canonical CIK")
    if inputs.config.mode != target.mode:
        raise ValueError("graph config mode disagrees with target mode")
    if inputs.config.knowledge_cutoff != target.knowledge_cutoff:
        raise ValueError("graph config knowledge cutoff disagrees with target cutoff")
    if inputs.config.valuation_at is not None and inputs.config.valuation_at != target.valuation_at:
        raise ValueError("graph config valuation_at disagrees with target valuation_at")
    if target.knowledge_cutoff > target.valuation_at:
        raise ValueError("target knowledge cutoff cannot exceed valuation time")


def build_recompute_configuration_identity(
    *,
    target: SecDatedInvalidationTarget,
    config: SecMetricGraphConfig,
    versions: tuple[SecNormalizedFinancialFactVersion, ...],
    quality_records: tuple[SecQualityRecord, ...],
    consumer_commits: tuple[SecConsumerCommit, ...],
    external_inputs: tuple[SecMetricExternalInput, ...],
) -> str:
    """Derive deterministic configuration identity from canonical content of all inputs."""
    payload = {
        "schema": "sec-metric-recompute-work-config-v2",
        "target": target.canonical_payload(),
        "graph_config_identity": config.configuration_identity,
        "graph_config": json.loads(
            encoded({"schema": "sec-metric-graph-config-v1", "payload": config}).decode("utf-8")
        ),
        "fact_version_content_identities": [v.content_identity for v in versions],
        "quality_records": [
            {
                "normalized_version_id": q.normalized_version_id,
                "quality_policy_version": q.quality_policy_version,
                "status": q.status.value,
                "recorded_at": _stamp(q.recorded_at),
                "supersedes_quality_record_id": q.supersedes_quality_record_id,
                "quality_record_id": q.quality_record_id,
            }
            for q in quality_records
        ],
        "consumer_commits": [
            {
                "normalized_version_id": c.normalized_version_id,
                "quality_record_id": c.quality_record_id,
                "consumer_dataset_identity": c.consumer_dataset_identity,
                "committed_at": _stamp(c.committed_at),
                "commit_id": c.commit_id,
            }
            for c in consumer_commits
        ],
        "external_inputs": [
            json.loads(
                encoded({"schema": "sec-metric-external-attestation-v1", "payload": ext}).decode(
                    "utf-8"
                )
            )
            for ext in external_inputs
        ],
    }
    return hashlib.sha256(_canonical_discovery(payload)).hexdigest()


def build_recompute_work_spec(
    *,
    event: SecFilingDiscoveryEvent,
    target: SecDatedInvalidationTarget,
    inputs: SecTargetMetricInputs,
    processing_version: str,
    max_attempts: int = 1,
) -> SecEventWorkSpec:
    """Construct a stable SecEventWorkSpec binding event metadata, target, and all inputs."""
    validate_recompute_target_binding(event=event, target=target, inputs=inputs)
    validate_target_seal(target)
    validate_config_seal(inputs.config)
    for v in inputs.versions:
        validate_version_seal(v)
    for q in inputs.quality_records:
        validate_quality_record_seal(q)
    for c in inputs.consumer_commits:
        validate_consumer_commit_seal(c)
    for e in inputs.external_inputs:
        validate_external_input_seal(e)

    if len(inputs.versions) > MAX_RECOMPUTE_VERSIONS:
        raise ResourceLimitError(
            f"recompute input versions exceed limit of {MAX_RECOMPUTE_VERSIONS}"
        )
    if not inputs.versions:
        raise ValueError("recompute requires at least one normalized fact version")

    input_version_ids = tuple(
        sorted(
            {
                SecDataVersionId(SecDataVersionKind.NORMALIZED_FACT, v.normalized_version_id)
                for v in inputs.versions
            }
        )
    )
    config_identity = build_recompute_configuration_identity(
        target=target,
        config=inputs.config,
        versions=inputs.versions,
        quality_records=inputs.quality_records,
        consumer_commits=inputs.consumer_commits,
        external_inputs=inputs.external_inputs,
    )

    return SecEventWorkSpec(
        event_key=event.key,
        metadata_digest=event.metadata_digest,
        processing_version=processing_version,
        configuration_identity=config_identity,
        input_version_ids=input_version_ids,
        max_attempts=max_attempts,
    )


def decode_and_validate_recompute_result(
    raw_bytes: bytes,
    *,
    expected_work_identity: str,
    expected_target: SecDatedInvalidationTarget,
    expected_config: SecMetricGraphConfig | None = None,
) -> dict[str, Any]:
    """Strictly decode and validate fact manifest payload before READY or replay acceptance."""
    if type(raw_bytes) is not bytes or not raw_bytes:
        raise ValueError("recompute result payload must be nonempty bytes")
    if len(raw_bytes) > MAX_PAYLOAD:
        raise ResourceLimitError(f"recompute payload exceeds maximum limit of {MAX_PAYLOAD} bytes")
    manifest = decode_manifest(raw_bytes)
    if not isinstance(manifest, dict):
        raise TypeError("recompute result payload must be a JSON object")
    if set(manifest.keys()) != EXACT_MANIFEST_FIELDS:
        raise ValueError("manifest fields do not match exact expected set")

    if manifest["schema"] != "sec-metric-recompute-fact-v1":
        raise ValueError(f"unexpected recompute output schema: {manifest.get('schema')}")
    if manifest["claim"] != "OUTPUT_BYTES_REPLAYED":
        raise ValueError(f"unexpected recompute output claim: {manifest.get('claim')}")
    if manifest["work_identity"] != expected_work_identity:
        raise ValueError(
            f"output work identity mismatch: expected {expected_work_identity}, got {manifest.get('work_identity')}"
        )
    if manifest["target_identity"] != expected_target.target_identity:
        raise ValueError("output target identity mismatch")
    if manifest["target_recipe_identity"] != expected_target.recipe_identity:
        raise ValueError("output target recipe identity mismatch")
    if manifest["old_output_version"] != expected_target.output_version.canonical_payload():
        raise ValueError("output old version mismatch")
    terminals = manifest["terminal_identities"]
    if type(terminals) is not list or not (1 <= len(terminals) <= 64):
        raise ValueError("terminal_identities must be a list with count between 1 and 64")
    for t_id in terminals:
        _sha_check(t_id, "terminal_identity")
    if len(set(terminals)) != len(terminals):
        raise ValueError("terminal_identities must contain unique identities")

    steps = manifest["step_identities"]
    if type(steps) is not list or not (1 <= len(steps) <= 32):
        raise ValueError("step_identities must be a list with count between 1 and 32")
    for s_id in steps:
        _sha_check(s_id, "step_identity")
    if len(set(steps)) != len(steps):
        raise ValueError("step_identities must contain unique identities")

    final_val_id = _sha_check(manifest["final_value_identity"], "final_value_identity")
    if final_val_id != steps[-1]:
        raise ValueError("final_value_identity must equal the final step identity")

    if expected_config is not None:
        if manifest["graph_configuration_identity"] != expected_config.configuration_identity:
            raise ValueError("output graph configuration identity mismatch")
        if len(terminals) != len(expected_config.declarations):
            raise ValueError("terminal_identities count does not match expected declarations count")
        if len(steps) != len(expected_config.steps):
            raise ValueError("step_identities count does not match expected steps count")
    else:
        _sha_check(manifest["graph_configuration_identity"], "graph_configuration_identity")

    _sha_check(manifest["result_identity"], "result_identity")
    expected_result_id = identity(
        "sec-metric-graph-result-v1",
        {
            "configuration_identity": manifest["graph_configuration_identity"],
            "terminal_value_identities": tuple(terminals),
            "step_value_identities": tuple(steps),
            "final_id": final_val_id,
        },
    )
    if manifest["result_identity"] != expected_result_id:
        raise ValueError("result_identity does not match canonical graph result identity formula")

    final_val_raw = manifest["final_value"]
    if final_val_raw is not None:
        if type(final_val_raw) is not str:
            raise ValueError(
                f"final_value must be string or None, got {type(final_val_raw).__name__}"
            )
        try:
            dec = Decimal(final_val_raw)
        except Exception as exc:
            raise ValueError(f"invalid decimal string for final_value: {final_val_raw}") from exc
        validate_decimal(dec)

    unit = manifest["final_unit"]
    if type(unit) is not str or not (1 <= len(unit) <= MAX_STRING_FIELD_LENGTH):
        raise ValueError("final_unit must be a nonempty bounded string")

    currency = manifest["final_currency"]
    if type(currency) is not str or not (1 <= len(currency) <= MAX_STRING_FIELD_LENGTH):
        raise ValueError("final_currency must be a nonempty bounded string")

    rec_at = manifest["recorded_at"]
    if type(rec_at) is not str:
        raise ValueError("recorded_at must be an ISO timestamp string")
    try:
        dt = datetime.fromisoformat(rec_at)
        if dt.tzinfo is None:
            raise ValueError("recorded_at must be timezone-aware")
    except Exception as exc:
        raise ValueError(f"invalid recorded_at timestamp: {rec_at}") from exc

    return manifest


def decode_and_validate_recompute_observation(
    ref: SnapshotObservationRef,
    replay: ReceiptReplay,
    *,
    expected_work_identity: str,
    expected_target: SecDatedInvalidationTarget,
    expected_config: SecMetricGraphConfig | None = None,
) -> dict[str, Any]:
    """Validate observation metadata, replay bounded bytes, and decode recompute manifest."""
    if type(ref) is not SnapshotObservationRef:
        raise TypeError("observation must be SnapshotObservationRef")
    if ref.provider != "sec":
        raise ValueError("invalid output observation provider: expected 'sec'")
    if ref.endpoint != "metric-recompute-fact":
        raise ValueError("invalid output observation endpoint: expected 'metric-recompute-fact'")
    if ref.mode is not SnapshotMode.FROZEN:
        raise ValueError("invalid output observation mode: expected FROZEN")
    if ref.serialization_identifier != "sec-metric-recompute-fact-v1":
        raise ValueError("invalid output observation serialization")

    expected_spec = RequestSpec(
        "sec",
        "metric-recompute-fact",
        {
            "work_identity": expected_work_identity,
            "target_identity": expected_target.target_identity,
        },
    )
    if ref.request_identity != expected_spec.request_identity:
        raise ValueError("output request identity mismatch")

    raw_bytes = replay.replay(ref, limit=MAX_PAYLOAD)
    manifest = decode_and_validate_recompute_result(
        raw_bytes,
        expected_work_identity=expected_work_identity,
        expected_target=expected_target,
        expected_config=expected_config,
    )
    rec_dt = _utc(datetime.fromisoformat(manifest["recorded_at"]), "recorded_at")
    ref_dt = _utc(ref.snapshot_fetched_at, "snapshot_fetched_at")
    if rec_dt != ref_dt:
        raise ValueError("manifest recorded_at does not equal snapshot_fetched_at")
    return manifest

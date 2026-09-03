"""Bounded ETF benchmark/constituent producer and offline artifact assembler."""
# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportMissingParameterType=false, reportAttributeAccessIssue=false, reportArgumentType=false, reportUnknownMemberType=false, reportUnknownLambdaType=false, reportOptionalMemberAccess=false, reportUnusedImport=false, reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
import weakref
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, cast

from ...core import SnapshotStore, SourceFactObservation, SourceFactRegistry, SourceResolutionStatus
from .endpoints import EtfBasicRequest, IndexWeightRequest
from .observations import TushareObservedResult
from .recipes import audit_index_weight_vintage, build_etf_index_mapping_observations
from .vintage_artifacts import (
    PARQUET_COLUMNS,
    EtfBenchmarkConstituentBundle,
    compute_machine_gates,
    project_mapping_metadata,
    stable_frame,
)
from .vintage_producer import produce_etf_benchmark_constituent_vintages

_compute_machine_gates = compute_machine_gates


class _StageGuard:
    pass


@dataclass(frozen=True)
class EtfBenchmarkConstituentScope:
    etf_requests: tuple[EtfBasicRequest, ...] = field(default_factory=tuple)
    weight_requests: tuple[IndexWeightRequest, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        ids = [r.spec.request_identity for r in (*self.etf_requests, *self.weight_requests)]
        if len(ids) != len(set(ids)):
            raise ValueError("scope request identities must be unique")
        for req in self.weight_requests:
            if req.trade_date is None or req.start_date is not None or req.end_date is not None:
                raise ValueError("index weight scope requires exact trade_date requests")
        if any({r.ts_code, r.index_code} == {None} for r in self.etf_requests):
            raise ValueError("ETF scope must be bounded by ts_code or index_code")


class CoverageItemStatus(str, Enum):
    CAPTURED = "captured"
    MISSING = "missing"
    UNSUPPORTED = "unsupported"
    PARTIAL = "partial"
    FAILURE = "failure"


@dataclass(frozen=True)
class CoverageItemResult:
    request_identity: str
    endpoint: str
    status: CoverageItemStatus
    observation_identity: str | None = None
    reason: str | None = None


def derive_cutoff_status(
    item: CoverageItemResult,
    observation: TushareObservedResult | None,
    cutoff: datetime,
    *,
    incomplete: bool = False,
) -> SourceResolutionStatus:
    """Resolve source evidence using only validated provider observation anchors."""
    if item.status is not CoverageItemStatus.CAPTURED or observation is None:
        return SourceResolutionStatus.UNKNOWN_FAIL_CLOSED
    anchor = observation.availability.provider_first_observed_at
    if cutoff < anchor:
        return SourceResolutionStatus.NOT_YET_OBSERVED
    if incomplete:
        return SourceResolutionStatus.INCOMPLETE_SOURCE_OBSERVATION
    return SourceResolutionStatus.CURRENT_ONLY_HISTORICAL_UNPROVEN


def match_scope_observations(
    scope: EtfBenchmarkConstituentScope, observations: Sequence[TushareObservedResult]
) -> tuple[CoverageItemResult, ...]:
    by_request = {o.request_identity: o for o in observations}
    results: list[CoverageItemResult] = []
    for request in (*scope.etf_requests, *scope.weight_requests):
        observation = by_request.get(request.spec.request_identity)
        endpoint = request.endpoint
        if observation is None:
            results.append(
                CoverageItemResult(
                    request.spec.request_identity,
                    endpoint,
                    CoverageItemStatus.MISSING,
                    reason="no captured observation",
                )
            )
        elif observation.row_count == 0:
            results.append(
                CoverageItemResult(
                    request.spec.request_identity,
                    endpoint,
                    CoverageItemStatus.MISSING,
                    observation.observation_identity,
                    "empty response",
                )
            )
        elif endpoint == "index_weight" and observation.observation.endpoint != endpoint:
            results.append(
                CoverageItemResult(
                    request.spec.request_identity,
                    endpoint,
                    CoverageItemStatus.UNSUPPORTED,
                    reason="endpoint mismatch",
                )
            )
        else:
            results.append(
                CoverageItemResult(
                    request.spec.request_identity,
                    endpoint,
                    CoverageItemStatus.CAPTURED,
                    observation.observation_identity,
                )
            )
    return tuple(results)


def assemble_etf_benchmark_constituent_vintages(
    observations: Sequence[TushareObservedResult],
    *,
    store: SnapshotStore,
    registry: SourceFactRegistry,
    scope: EtfBenchmarkConstituentScope,
    cutoff: datetime,
    output_dir: Path,
    audit_policy: dict[str, Any] | None = None,
    coverage_outcomes: Sequence[CoverageItemResult] = (),
) -> EtfBenchmarkConstituentBundle:
    import pandas as pd

    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware")
    declared = {r.spec.request_identity for r in (*scope.etf_requests, *scope.weight_requests)}
    observation_ids = [o.request_identity for o in observations]
    if len(set(observation_ids)) != len(observation_ids):
        raise ValueError("duplicate captured observation")
    if set(observation_ids) - declared:
        raise ValueError("captured observation is outside declared scope")
    outcome_ids = [o.request_identity for o in coverage_outcomes]
    if len(set(outcome_ids)) != len(outcome_ids) or set(outcome_ids) - declared:
        raise ValueError("coverage outcome is outside declared scope or duplicated")
    target_dir = Path(output_dir)
    if target_dir.exists():
        raise FileExistsError("output directory must be new/empty")
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir = Path(tempfile.mkdtemp(prefix=f".{target_dir.name}-", dir=target_dir.parent))
    stage_guard = _StageGuard()
    cleanup = weakref.finalize(stage_guard, shutil.rmtree, output_dir, True)
    maps = [o for o in observations if o.observation.endpoint == "etf_basic"]
    weights = [o for o in observations if o.observation.endpoint == "index_weight"]
    mapping = (
        build_etf_index_mapping_observations(maps, include_vintage_fields=True)
        if maps
        else type("R", (), {"frame": pd.DataFrame()})()
    )
    for obs in maps:
        for _, row in obs.frame.iterrows():
            code = str(row["ts_code"])
            key = f"etf_mapping:{code}"
            version = obs.fact_version
            source_value_sha256 = hashlib.sha256(
                json.dumps(
                    [
                        code,
                        None if pd.isna(row.get("index_code")) else str(row.get("index_code")),
                        None if pd.isna(row.get("list_status")) else str(row.get("list_status")),
                    ],
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            registry.register(
                SourceFactObservation(
                    provider="tushare",
                    endpoint="etf_basic",
                    source_key=key,
                    provider_first_observed_at=obs.availability.provider_first_observed_at,
                    snapshot_fetched_at=obs.snapshot_fetched_at,
                    snapshot_identity=obs.snapshot_identity,
                    observation_identity=obs.observation_identity,
                    request_identity=obs.request_identity,
                    payload_sha256=obs.response_sha256,
                    source_value_sha256=source_value_sha256,
                    fact_version=version,
                    supersedes_fact_version=None,
                    revision_status="CURRENT",
                    quality_flags=("CURRENT_ONLY_HISTORICAL_UNPROVEN",),
                ),
                store=store,
                observation_ref=obs.observation,
            )
    for obs in weights:
        if len(obs.frame):
            index_code = str(obs.frame.iloc[0]["index_code"])
            trade_date = str(obs.frame.iloc[0]["trade_date"])
            key = f"index_weight:{index_code}:{trade_date}"
            version = obs.fact_version
            source_value_sha256 = hashlib.sha256(
                json.dumps(
                    sorted(
                        [
                            (
                                str(r.get("con_code")),
                                None if pd.isna(r.get("weight")) else str(r.get("weight")),
                            )
                            for _, r in obs.frame.iterrows()
                        ]
                    ),
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            registry.register(
                SourceFactObservation(
                    provider="tushare",
                    endpoint="index_weight",
                    source_key=key,
                    source_event_date=trade_date,
                    provider_first_observed_at=obs.availability.provider_first_observed_at,
                    snapshot_fetched_at=obs.snapshot_fetched_at,
                    snapshot_identity=obs.snapshot_identity,
                    observation_identity=obs.observation_identity,
                    request_identity=obs.request_identity,
                    payload_sha256=obs.response_sha256,
                    source_value_sha256=source_value_sha256,
                    fact_version=version,
                    supersedes_fact_version=None,
                    revision_status="CURRENT",
                    quality_flags=("RETRIEVAL_COMPLETENESS_UNPROVEN",),
                ),
                store=store,
                observation_ref=obs.observation,
            )
    etf_frame: Any = mapping.frame  # type: ignore[reportUnknownMemberType]
    etf_frame = project_mapping_metadata(etf_frame, registry.observations)
    vintage_rows: list[dict[str, Any]] = []
    constituent_rows: list[dict[str, Any]] = []
    for obs in weights:
        evidence = audit_index_weight_vintage(
            obs,
            total_weight_tolerance=float((audit_policy or {}).get("total_weight_tolerance", 0.01)),
        )
        recs = [
            r
            for r in registry.observations
            if r.source_key == f"index_weight:{evidence.index_code}:{evidence.trade_date}"
            and r.fact_version == obs.fact_version
        ]
        rec = recs[-1] if recs else None
        status = derive_cutoff_status(
            CoverageItemResult(obs.request_identity, "index_weight", CoverageItemStatus.CAPTURED),
            obs,
            cutoff,
            incomplete=True,
        )
        first_observed = (
            rec.provider_first_observed_at if rec else obs.availability.provider_first_observed_at
        )
        vintage_rows.append(
            {
                "index_code": evidence.index_code,
                "provider_trade_date": evidence.trade_date,
                "source_available_at": None,
                "availability_basis": "PROVIDER_FIRST_OBSERVED",
                "availability_precision": "UNKNOWN",
                "provider_first_observed_at": (
                    first_observed.isoformat() if first_observed else None
                ),
                "snapshot_fetched_at": obs.snapshot_fetched_at.isoformat(),
                "snapshot_identity": obs.snapshot_identity,
                "observation_identity": obs.observation_identity,
                "request_identity": obs.request_identity,
                "payload_sha256": obs.response_sha256,
                "fact_version": obs.fact_version,
                "revision_status": rec.revision_status if rec else "CURRENT",
                "supersedes_fact_version": rec.supersedes_fact_version if rec else None,
                "native_weight_unit": evidence.native_weight_unit,
                "conversion_identity": evidence.conversion_identity,
                "observed_component_count": evidence.observed_component_count,
                "expected_component_count": evidence.expected_component_count,
                "expected_count_basis": evidence.expected_count_basis,
                "coverage_weight": str(evidence.decimal_weight_sum),
                "retrieval_status": evidence.retrieval_status.value,
                "economic_completeness_status": evidence.economic_completeness_status.value,
                "duplicate_component_rows": evidence.duplicate_component_rows,
                "null_weight_count": evidence.null_weight_count,
                "non_finite_weight_count": evidence.non_finite_weight_count,
                "out_of_range_weight_count": evidence.out_of_range_weight_count,
                "weight_total_status": evidence.weight_total_status.value,
                "expected_count_status": evidence.expected_count_status.value,
                "quality_flags": [status.value],
            }
        )
        for _, row in obs.frame.iterrows():
            raw_weight = row["weight"]
            try:
                numeric = float(raw_weight)
                decimal_weight = (
                    numeric / 100 if pd.notna(raw_weight) and math.isfinite(numeric) else None
                )
            except (TypeError, ValueError, OverflowError):
                decimal_weight = None
            constituent_rows.append(
                {
                    "index_code": evidence.index_code,
                    "provider_trade_date": evidence.trade_date,
                    "con_code": str(row["con_code"]),
                    "weight_percent": row["weight"],
                    "weight_decimal": decimal_weight,
                    "native_weight_unit": evidence.native_weight_unit,
                    "conversion_identity": evidence.conversion_identity,
                    "fact_version": obs.fact_version,
                    "request_identity": obs.request_identity,
                    "snapshot_identity": obs.snapshot_identity,
                    "observation_identity": obs.observation_identity,
                    "payload_sha256": obs.response_sha256,
                }
            )
    stable_frame(etf_frame, "etf_benchmark_vintages.parquet").to_parquet(
        output_dir / "etf_benchmark_vintages.parquet", index=False
    )
    stable_frame(vintage_rows, "index_weight_vintages.parquet").to_parquet(
        output_dir / "index_weight_vintages.parquet", index=False
    )
    stable_frame(constituent_rows, "index_weight_constituents.parquet").to_parquet(
        output_dir / "index_weight_constituents.parquet", index=False
    )
    coverage = list(match_scope_observations(scope, observations))
    by_outcome = {item.request_identity: item for item in coverage_outcomes}
    coverage = [by_outcome.get(item.request_identity, item) for item in coverage]
    by_request = {o.request_identity: o for o in observations}
    resolutions: list[dict[str, Any]] = []
    for item in coverage:
        observation = by_request.get(item.request_identity)
        incomplete = bool(observation and observation.observation.endpoint == "index_weight")
        resolutions.append(
            {
                "endpoint": item.endpoint,
                "request_identity": item.request_identity,
                "observation_identity": item.observation_identity,
                "fact_version": observation.fact_version if observation else None,
                "snapshot_identity": observation.snapshot_identity if observation else None,
                "payload_sha256": observation.response_sha256 if observation else None,
                "resolution_status": derive_cutoff_status(
                    item, observation, cutoff, incomplete=incomplete
                ).value,
                "coverage_status": item.status.value,
                "provider_first_observed_at": (
                    observation.availability.provider_first_observed_at.isoformat()
                    if observation
                    else None
                ),
                "snapshot_fetched_at": observation.snapshot_fetched_at.isoformat()
                if observation
                else None,
                "cutoff": cutoff.isoformat(),
                "quality_flags": [
                    derive_cutoff_status(item, observation, cutoff, incomplete=incomplete).value
                ],
            }
        )
    stable_frame(resolutions, "source_resolution_manifest.parquet").to_parquet(
        output_dir / "source_resolution_manifest.parquet", index=False
    )
    (output_dir / "coverage_and_gap_report.json").write_text(
        json.dumps(
            {
                "observations": len(observations),
                "cutoff": cutoff.astimezone(UTC).isoformat(),
                "items": resolutions,
            },
            sort_keys=True,
        )
    )
    files = [
        "etf_benchmark_vintages.parquet",
        "index_weight_vintages.parquet",
        "index_weight_constituents.parquet",
        "source_resolution_manifest.parquet",
        "coverage_and_gap_report.json",
    ]
    hashes = {n: hashlib.sha256((output_dir / n).read_bytes()).hexdigest() for n in files}
    identity = hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    artifact_rows = {
        name.removesuffix(".parquet"): pd.read_parquet(output_dir / name).to_dict("records")
        for name in PARQUET_COLUMNS
    }
    gates = compute_machine_gates(
        registry,
        observations,
        cast(list[dict[str, Any]], artifact_rows["source_resolution_manifest"]),
        weights,
        cutoff,
        artifact_rows,
    )
    gate = {
        "schema_version": 1,
        "bundle_identity": None,
        "passes": all(value["passes"] for value in gates.values()),
        "gates": gates,
        "file_sha256": hashes,
    }
    identity = hashlib.sha256(
        json.dumps(
            {
                "file_sha256": hashes,
                "gate_payload": {k: v for k, v in gate.items() if k != "bundle_identity"},
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    gate["bundle_identity"] = identity
    (output_dir / "machine_gate.json").write_text(
        json.dumps(gate, sort_keys=True, separators=(",", ":"))
    )
    output_dir.rename(target_dir)
    cleanup.detach()
    return EtfBenchmarkConstituentBundle(target_dir, identity, gate)


__all__ = [
    "CoverageItemResult",
    "CoverageItemStatus",
    "EtfBenchmarkConstituentBundle",
    "EtfBenchmarkConstituentScope",
    "assemble_etf_benchmark_constituent_vintages",
    "derive_cutoff_status",
    "match_scope_observations",
    "produce_etf_benchmark_constituent_vintages",
]

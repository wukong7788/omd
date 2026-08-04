# pyright: reportUnnecessaryIsInstance=false
"""Immutable manifest-only look-through source-fact bundle builder."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from ..observations import TushareObservedResult
from .etf_index_mapping import EtfIndexMappingObservationResult, MappingObservationStatus
from .index_weight_vintage import IndexWeightVintageEvidence

_BUNDLE_SCHEMA_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_FORBIDDEN_KEYS = frozenset(
    {
        "first_usable_session",
        "style",
        "cluster",
        "target",
        "score",
        "rank",
        "order",
        "action",
        "live_signal",
    }
)


class LookthroughReadiness(str, Enum):
    SOURCE_FACTS_COMPLETE = "SOURCE_FACTS_COMPLETE"
    SOURCE_FACTS_PARTIAL = "SOURCE_FACTS_PARTIAL"
    CURRENT_OBSERVATION_ONLY = "CURRENT_OBSERVATION_ONLY"
    BLOCKED_UNPROVEN_COMPLETENESS = "BLOCKED_UNPROVEN_COMPLETENESS"


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _identity(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(document)).hexdigest()


def _readiness(
    mapping: EtfIndexMappingObservationResult,
    vintages: Sequence[IndexWeightVintageEvidence],
    industry_fact_count: int,
    missing_index_codes: Sequence[str],
    missing_industry_components: Sequence[str],
) -> LookthroughReadiness:
    has_mapping = mapping.row_count > 0
    has_weights = bool(vintages)
    if not has_mapping and not has_weights and industry_fact_count == 0:
        raise ValueError("bundle requires at least one captured source fact")
    weights_unproven = any(
        vintage.economic_completeness_status.value != "ECONOMIC_COMPLETENESS_PROVEN"
        for vintage in vintages
    )
    if weights_unproven:
        return LookthroughReadiness.BLOCKED_UNPROVEN_COMPLETENESS
    if not has_weights:
        # Only current-observation evidence was captured; no vintage audits.
        return LookthroughReadiness.CURRENT_OBSERVATION_ONLY
    if not has_mapping or missing_industry_components:
        return LookthroughReadiness.SOURCE_FACTS_PARTIAL
    if missing_index_codes:
        return LookthroughReadiness.SOURCE_FACTS_PARTIAL
    return LookthroughReadiness.SOURCE_FACTS_COMPLETE


def _mapping_rows(mapping: EtfIndexMappingObservationResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    frame = mapping.frame
    for _, row in frame.iterrows():
        rows.append(
            {
                "etf_symbol": row["etf_symbol"],
                "index_code": row["index_code"],
                "list_status": row["list_status"],
                "provider_first_observed_at": row["provider_first_observed_at"],
                "snapshot_fetched_at": row["snapshot_fetched_at"],
                "request_identity": row["request_identity"],
                "response_sha256": row["response_sha256"],
                "snapshot_identity": row["snapshot_identity"],
                "observation_identity": row["observation_identity"],
                "fact_version": row["fact_version"],
                "mapping_observation_status": row["mapping_observation_status"],
                "quality_flags": row["quality_flags"],
            }
        )
    return rows


def _vintage_summaries(
    vintages: Sequence[IndexWeightVintageEvidence],
) -> list[dict[str, Any]]:
    return [
        {
            "vintage_identity": vintage.vintage_identity,
            "index_code": vintage.index_code,
            "trade_date": vintage.trade_date,
            "native_weight_unit": vintage.native_weight_unit,
            "decimal_weight_unit": vintage.decimal_weight_unit,
            "conversion_identity": vintage.conversion_identity,
            "observed_component_count": vintage.observed_component_count,
            "component_codes": list(vintage.component_codes),
            "row_count": vintage.row_count,
            "expected_component_count": vintage.expected_component_count,
            "expected_count_basis": vintage.expected_count_basis,
            "duplicate_component_rows": vintage.duplicate_component_rows,
            "null_weight_count": vintage.null_weight_count,
            "non_finite_weight_count": vintage.non_finite_weight_count,
            "out_of_range_weight_count": vintage.out_of_range_weight_count,
            "native_weight_sum": str(vintage.native_weight_sum),
            "decimal_weight_sum": str(vintage.decimal_weight_sum),
            "total_weight_tolerance": str(vintage.total_weight_tolerance),
            "weight_total_status": vintage.weight_total_status.value,
            "expected_count_status": vintage.expected_count_status.value,
            "retrieval_status": vintage.retrieval_status.value,
            "economic_completeness_status": vintage.economic_completeness_status.value,
            "request_identity": vintage.request_identity,
            "response_sha256": vintage.response_sha256,
            "snapshot_identity": vintage.snapshot_identity,
            "observation_identity": vintage.observation_identity,
            "fact_version": vintage.fact_version,
            "snapshot_fetched_at": _utc_iso(vintage.snapshot_fetched_at),
        }
        for vintage in vintages
    ]


def _industry_summaries(
    industry_observations: Sequence[TushareObservedResult],
) -> list[dict[str, Any]]:
    return [
        {
            "endpoint": observation.observation.endpoint,
            "request_identity": observation.request_identity,
            "response_sha256": observation.response_sha256,
            "snapshot_identity": observation.snapshot_identity,
            "observation_identity": observation.observation_identity,
            "fact_version": observation.fact_version,
            "snapshot_fetched_at": _utc_iso(observation.snapshot_fetched_at),
            "row_count": observation.row_count,
            "columns": list(observation.columns),
            "content_sha256": observation.content_sha256,
        }
        for observation in industry_observations
    ]


def _component_evidence(
    vintages: Sequence[IndexWeightVintageEvidence],
    industry_observations: Sequence[TushareObservedResult],
) -> tuple[list[dict[str, str]], list[str], int]:
    industry_components: set[str] = set()
    for observation in industry_observations:
        frame = observation.frame
        if "ts_code" not in frame.columns:
            raise ValueError("industry observation is missing ts_code")
        industry_components.update(str(value) for value in frame["ts_code"].dropna().tolist())

    evidence: list[dict[str, str]] = []
    missing: set[str] = set()
    for vintage in vintages:
        for component_code in vintage.component_codes:
            observed = component_code in industry_components
            if not observed:
                missing.add(component_code)
            evidence.append(
                {
                    "vintage_identity": vintage.vintage_identity,
                    "index_code": vintage.index_code,
                    "trade_date": vintage.trade_date,
                    "component_code": component_code,
                    "industry_observation_status": "OBSERVED" if observed else "MISSING",
                }
            )
    return evidence, sorted(missing), len(industry_components)


@dataclass(frozen=True)
class LookthroughSourceBundle:
    """Immutable manifest-only look-through source-fact bundle."""

    output_dir: Path
    bundle_identity: str
    manifest: Mapping[str, Any]

    @classmethod
    def load(cls, output_dir: Path) -> LookthroughSourceBundle:
        path = Path(os.path.abspath(output_dir))
        manifest_path = path / _MANIFEST_NAME
        try:
            document = json.loads(
                manifest_path.read_text(encoding="utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except Exception as exc:
            raise ValueError("invalid bundle manifest") from exc
        if not isinstance(document, dict):
            raise TypeError("invalid bundle manifest")
        manifest = cast(dict[str, Any], document)
        if manifest.get("bundle_schema_version") != _BUNDLE_SCHEMA_VERSION:
            raise ValueError("bundle schema mismatch")
        identity = manifest.get("bundle_identity")
        if not isinstance(identity, str):
            raise TypeError("invalid bundle identity")
        return cls(path, identity, MappingProxyType(dict(manifest)))

    def verify(self) -> bool:
        try:
            loaded = LookthroughSourceBundle.load(self.output_dir)
            if loaded.bundle_identity != self.bundle_identity:
                return False
            manifest = dict(loaded.manifest)
            manifest.pop("bundle_identity", None)
            if _identity(manifest) != self.bundle_identity:
                return False
            readiness = loaded.manifest.get("readiness")
            if readiness not in {status.value for status in LookthroughReadiness}:
                return False
            return not _FORBIDDEN_KEYS.intersection(loaded.manifest)
        except (OSError, TypeError, ValueError):
            return False


def build_lookthrough_source_bundle(
    mapping_observations: EtfIndexMappingObservationResult,
    weight_vintages: Sequence[IndexWeightVintageEvidence],
    industry_observations: Sequence[TushareObservedResult],
    *,
    output_dir: Path,
) -> LookthroughSourceBundle:
    """Emit an immutable manifest-only source-fact bundle.

    The bundle binds every input identity and content hash and exposes per
    ETF/index/component evidence statuses. It never chooses a consumer
    decision-date vintage, derives ``first_usable_session``, fills missing
    industry, or generates style, cluster, overlap, or portfolio exposure.
    Output is new/empty-directory only; existing manifests are never
    overwritten.
    """
    if not isinstance(mapping_observations, EtfIndexMappingObservationResult):
        raise TypeError("mapping_observations must be EtfIndexMappingObservationResult")
    for vintage in weight_vintages:
        if not isinstance(vintage, IndexWeightVintageEvidence):
            raise TypeError("weight_vintages must contain IndexWeightVintageEvidence")
    for observation in industry_observations:
        if not isinstance(observation, TushareObservedResult):
            raise TypeError("industry_observations must contain TushareObservedResult")
        if observation.observation.endpoint != "index_member_all":
            raise ValueError("industry observations must come from index_member_all")

    target = Path(os.path.abspath(output_dir))
    if target.exists():
        if not target.is_dir():
            raise ValueError("output_dir must be a directory")
        if any(target.iterdir()):
            raise FileExistsError("output_dir must be new or empty")

    mapping_rows = _mapping_rows(mapping_observations)
    missing_index_codes = sorted(
        {
            row["etf_symbol"]
            for row in mapping_rows
            if MappingObservationStatus(row["mapping_observation_status"])
            is MappingObservationStatus.MISSING_INDEX_CODE
        }
    )
    observed_index_codes = {
        row["index_code"] for row in mapping_rows if row["index_code"] is not None
    }
    vintage_index_codes = {vintage.index_code for vintage in weight_vintages}
    unsupported_index_codes = sorted(observed_index_codes - vintage_index_codes)

    vintages_summaries = _vintage_summaries(weight_vintages)
    industry_summaries = _industry_summaries(industry_observations)
    component_evidence, missing_industry_components, industry_fact_count = _component_evidence(
        weight_vintages, industry_observations
    )
    readiness = _readiness(
        mapping_observations,
        weight_vintages,
        industry_fact_count,
        missing_index_codes,
        missing_industry_components,
    )

    document: dict[str, Any] = {
        "bundle_schema_version": _BUNDLE_SCHEMA_VERSION,
        "inputs": {
            "mapping_observations": {
                "observation_count": mapping_observations.observation_count,
                "row_count": mapping_observations.row_count,
                "content_sha256": mapping_observations.content_sha256,
            },
            "weight_vintages": [summary["vintage_identity"] for summary in vintages_summaries],
            "industry_observations": [
                summary["observation_identity"] for summary in industry_summaries
            ],
        },
        "mapping_observation_rows": mapping_rows,
        "weight_vintage_evidence": vintages_summaries,
        "industry_observation_evidence": industry_summaries,
        "component_evidence": component_evidence,
        "unsupported_index_codes": unsupported_index_codes,
        "missing_index_codes": missing_index_codes,
        "missing_industry_components": missing_industry_components,
        "readiness": readiness.value,
    }
    document["bundle_identity"] = _identity(document)

    target.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=target))
    try:
        (tmp / _MANIFEST_NAME).write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        try:
            os.rename(tmp / _MANIFEST_NAME, target / _MANIFEST_NAME)
        except OSError:
            if (target / _MANIFEST_NAME).exists():
                raise FileExistsError("output_dir must be new or empty")
            raise
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

    return LookthroughSourceBundle(
        target,
        document["bundle_identity"],
        MappingProxyType(document),
    )


__all__ = [
    "LookthroughReadiness",
    "LookthroughSourceBundle",
    "build_lookthrough_source_bundle",
]

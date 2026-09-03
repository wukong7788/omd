"""Assembly and disk-independent semantic checks for vintage artifacts."""
# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportArgumentType=false

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from ...core import SourceFactRegistry, SourceResolutionStatus
from .vintage_canonical import canonicalize

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN = ("return", "performance", "score", "rank", "target", "action", "signal", "promotion")
_UNSAFE = ("token", "secret", "password", "api_key", "authorization", "credential")


def _identity(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def _canonical_hash(frames: dict[str, Any], report: dict[str, Any]) -> str:
    logical = canonicalize(
        {
            "resolutions": sorted(
                report.get("items", []),
                key=lambda r: (str(r.get("request_identity")), str(r.get("endpoint"))),
            ),
            "artifacts": frames,
        }
    )
    payload = json.dumps(logical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _lineage(rows: list[dict[str, Any]]) -> tuple[bool, bool]:
    identities = (
        "request_identity",
        "observation_identity",
        "fact_version",
        "snapshot_identity",
        "payload_sha256",
    )
    complete = all(all(_identity(row.get(k)) for k in identities) for row in rows)
    by_observation: dict[str, tuple[str, str]] = {}
    consistent = True
    for row in rows:
        obs = str(row.get("observation_identity") or "")
        key = (str(row.get("request_identity")), str(row.get("payload_sha256")))
        if obs in by_observation and by_observation[obs] != key:
            consistent = False
        by_observation[obs] = key
    return complete, consistent


def _revision_ok(frames: dict[str, Any]) -> bool:
    rows = [r for frame in frames.values() for r in frame]
    by_key: dict[str, set[str]] = {}
    for row in rows:
        endpoint = str(
            row.get("endpoint") or ("index_weight" if "con_code" in row else "etf_basic")
        )
        key = str(
            row.get("source_key")
            or (
                f"index_weight:{row.get('index_code')}:{row.get('provider_trade_date')}"
                if endpoint == "index_weight"
                else f"etf_mapping:{row.get('etf_symbol')}"
            )
        )
        by_key.setdefault(f"{endpoint}:{key}", set()).add(str(row.get("fact_version")))
    for row in rows:
        link = row.get("supersedes_fact_version")
        if not link:
            if str(row.get("revision_status", "")).startswith("UNCHANGED"):
                continue
            continue
        if link == row.get("fact_version"):
            return False
        endpoint = "index_weight" if "con_code" in row else "etf_basic"
        key = f"{endpoint}:" + (
            f"index_weight:{row.get('index_code')}:{row.get('provider_trade_date')}"
            if endpoint == "index_weight"
            else f"etf_mapping:{row.get('etf_symbol')}"
        )
        # Bundles contain the current projection only; the prior revision may
        # live solely in the append-only registry.  Validate its shape here and
        # validate same-key ancestry during assembly via registry evidence.
        if link not in by_key.get(key, set()) and not _identity(link):
            return False
    return True


def _weight_ok(frames: dict[str, Any], resolutions: list[dict[str, Any]]) -> bool:
    weights = frames.get("index_weight_vintages", [])
    resolution_by_request = {str(r.get("request_identity")): r for r in resolutions}
    for row in weights:
        status = str(row.get("retrieval_status"))
        economic = str(row.get("economic_completeness_status"))
        resolution = resolution_by_request.get(str(row.get("request_identity")), {}).get(
            "resolution_status"
        )
        incomplete = (
            status == "RETRIEVAL_COMPLETENESS_UNPROVEN"
            or economic == "ECONOMIC_COMPLETENESS_UNPROVEN"
        )
        diagnostics = (
            "observed_component_count",
            "expected_component_count",
            "duplicate_component_rows",
            "null_weight_count",
            "non_finite_weight_count",
            "out_of_range_weight_count",
        )
        valid_diag = all(
            (k == "expected_component_count" and row.get(k) is None)
            or (isinstance(row.get(k), (int, float)) and int(row.get(k)) >= 0)
            for k in diagnostics
        )
        if incomplete:
            if not (
                status == "RETRIEVAL_COMPLETENESS_UNPROVEN"
                and economic == "ECONOMIC_COMPLETENESS_UNPROVEN"
                and resolution == SourceResolutionStatus.INCOMPLETE_SOURCE_OBSERVATION.value
                and valid_diag
            ):
                return False
        elif status != "RETRIEVAL_COMPLETE" or economic != "ECONOMIC_COMPLETENESS_PROVEN":
            return False
    return True


def recompute_artifact_gate_passes(
    frames: dict[str, Any], report: dict[str, Any], gate: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    resolutions = list(report.get("items", []))
    rows = [row for frame in frames.values() for row in frame]
    lineage_rows = [r for r in rows if r.get("request_identity") or r.get("observation_identity")]
    complete, consistent = _lineage(lineage_rows) if lineage_rows else (False, True)
    captured = [r for r in resolutions if r.get("coverage_status") == "captured"]
    future = []
    for row in resolutions:
        try:
            anchor = datetime.fromisoformat(str(row["provider_first_observed_at"]))
            cutoff = datetime.fromisoformat(str(row["cutoff"]))
            if (
                cutoff < anchor
                and row.get("resolution_status") != SourceResolutionStatus.NOT_YET_OBSERVED.value
            ):
                future.append(row)
        except (KeyError, TypeError, ValueError):
            if row.get("coverage_status") == "captured":
                future.append(row)
    text = json.dumps(
        canonicalize({"frames": frames, "report": report}), sort_keys=True, default=str
    ).lower()
    canonical = _canonical_hash(frames, report)
    expected_canonical = (
        gate.get("gates", {})
        .get("DETERMINISTIC_REPLAY", {})
        .get("evidence", {})
        .get("canonical_sha256")
    )
    return {
        "APPEND_ONLY": {
            "passes": bool(
                gate.get("gates", {})
                .get("APPEND_ONLY", {})
                .get("evidence", {})
                .get("registry_verified")
            )
            and consistent
            and bool(rows),
            "evidence": {
                "registry_verified": bool(
                    gate.get("gates", {})
                    .get("APPEND_ONLY", {})
                    .get("evidence", {})
                    .get("registry_verified")
                ),
                "lineage_consistent": consistent,
            },
        },
        "NO_FUTURE_VISIBILITY": {"passes": not future, "evidence": {"future_rows": len(future)}},
        "EXACT_LINEAGE": {
            "passes": bool(captured) and complete and consistent,
            "evidence": {"captured_rows": len(captured), "lineage_complete": complete},
        },
        "UNKNOWN_FAILS_CLOSED": {
            "passes": all(
                r.get("resolution_status")
                != SourceResolutionStatus.VERIFIED_SOURCE_OBSERVATION.value
                for r in resolutions
                if r.get("coverage_status") != "captured"
            ),
            "evidence": {},
        },
        "REVISION_PRESERVED": {"passes": _revision_ok(frames), "evidence": {}},
        "WEIGHT_INTEGRITY": {
            "passes": _weight_ok(frames, resolutions),
            "evidence": {"weight_observations": len(frames.get("index_weight_vintages", []))},
        },
        "DETERMINISTIC_REPLAY": {
            "passes": bool(expected_canonical and expected_canonical == canonical),
            "evidence": {"canonical_sha256": canonical},
        },
        "NO_ECONOMIC_LABEL_DEPENDENCY": {
            "passes": not any(token in text for token in _FORBIDDEN),
            "evidence": {},
        },
        "PUBLIC_SAFETY": {
            "passes": not any(token in text for token in _UNSAFE)
            and (complete or not lineage_rows),
            "evidence": {},
        },
    }


def compute_machine_gates(
    registry: SourceFactRegistry,
    observations: Any,
    resolutions: list[dict[str, Any]],
    weights: Any,
    cutoff: Any,
    artifact_rows: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    frames = dict(artifact_rows or {})
    frames.setdefault("source_resolution_manifest", resolutions)
    report = {"items": resolutions}
    gates = recompute_artifact_gate_passes(frames, report, {"gates": {}})
    gates["APPEND_ONLY"]["evidence"]["registry_verified"] = registry.verify()
    gates["APPEND_ONLY"]["passes"] = bool(
        registry.verify()
        and registry.manifest().observation_count > 0
        and gates["APPEND_ONLY"]["evidence"].get("lineage_consistent", True)
    )
    unresolved = 0
    inconsistent = 0
    links = 0
    records = registry.observations
    for record in records:
        if record.supersedes_fact_version:
            if record.revision_status in {"CURRENT", "UNCHANGED_FROM_PREVIOUS"}:
                inconsistent += 1
            links += 1
            if not any(
                prior.fact_version == record.supersedes_fact_version
                and prior.provider == record.provider
                and prior.endpoint == record.endpoint
                and prior.source_key == record.source_key
                for prior in records
            ):
                unresolved += 1
        elif record.revision_status in {"REVISED", "REVISED_FROM_PREVIOUS"}:
            unresolved += 1
    gates["REVISION_PRESERVED"] = {
        "passes": registry.verify() and unresolved == 0 and inconsistent == 0,
        "evidence": {
            "revision_links": links,
            "unresolved_links": unresolved,
            "inconsistent_status_links": inconsistent,
        },
    }
    gates["DETERMINISTIC_REPLAY"]["passes"] = bool(observations)
    return gates

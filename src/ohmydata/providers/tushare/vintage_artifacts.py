# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportMissingParameterType=false, reportAttributeAccessIssue=false, reportArgumentType=false, reportUnknownMemberType=false, reportUnknownLambdaType=false, reportOptionalMemberAccess=false

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .vintage_canonical import canonicalize
from .vintage_gates import compute_machine_gates, recompute_artifact_gate_passes

PARQUET_COLUMNS: dict[str, tuple[str, ...]] = {
    "etf_benchmark_vintages.parquet": (
        "etf_symbol",
        "index_code",
        "etf_code",
        "benchmark_index_code",
        "list_status",
        "mapping_source_type",
        "source_effective_from",
        "source_effective_to",
        "source_available_at",
        "availability_basis",
        "availability_precision",
        "provider_first_observed_at",
        "snapshot_fetched_at",
        "fact_version",
        "request_identity",
        "snapshot_identity",
        "observation_identity",
        "payload_sha256",
        "response_sha256",
        "mapping_observation_status",
        "revision_status",
        "supersedes_fact_version",
        "quality_flags",
    ),
    "index_weight_vintages.parquet": (
        "index_code",
        "provider_trade_date",
        "source_available_at",
        "availability_basis",
        "availability_precision",
        "provider_first_observed_at",
        "snapshot_fetched_at",
        "fact_version",
        "request_identity",
        "snapshot_identity",
        "observation_identity",
        "payload_sha256",
        "revision_status",
        "supersedes_fact_version",
        "native_weight_unit",
        "conversion_identity",
        "observed_component_count",
        "expected_component_count",
        "expected_count_basis",
        "coverage_weight",
        "retrieval_status",
        "economic_completeness_status",
        "duplicate_component_rows",
        "null_weight_count",
        "non_finite_weight_count",
        "out_of_range_weight_count",
        "weight_total_status",
        "expected_count_status",
        "quality_flags",
    ),
    "index_weight_constituents.parquet": (
        "index_code",
        "provider_trade_date",
        "con_code",
        "weight_percent",
        "weight_decimal",
        "native_weight_unit",
        "conversion_identity",
        "fact_version",
        "request_identity",
        "snapshot_identity",
        "observation_identity",
        "payload_sha256",
        "source_available_at",
        "availability_basis",
        "availability_precision",
        "provider_first_observed_at",
        "snapshot_fetched_at",
        "revision_status",
        "supersedes_fact_version",
        "quality_flags",
    ),
    "source_resolution_manifest.parquet": (
        "endpoint",
        "request_identity",
        "observation_identity",
        "fact_version",
        "snapshot_identity",
        "payload_sha256",
        "resolution_status",
        "coverage_status",
        "provider_first_observed_at",
        "snapshot_fetched_at",
        "cutoff",
        "quality_flags",
    ),
}


def project_mapping_metadata(frame: Any, records: Sequence[Any]) -> Any:
    """Attach row-specific registry lineage to mapping artifact rows."""
    if not len(frame):
        return frame
    mapping_records = {
        (r.source_key, r.observation_identity, r.fact_version): r
        for r in records
        if r.endpoint == "etf_basic"
    }

    def record(row: Any) -> Any:
        return mapping_records.get(
            (
                f"etf_mapping:{row['etf_symbol']}",
                str(row["observation_identity"]),
                str(row["fact_version"]),
            )
        )

    frame["provider_first_observed_at"] = frame.apply(
        lambda row: record(row).provider_first_observed_at.isoformat() if record(row) else None,
        axis=1,
    )
    frame["payload_sha256"] = frame.apply(
        lambda row: record(row).payload_sha256 if record(row) else row.get("response_sha256"),
        axis=1,
    )
    frame["response_sha256"] = frame["payload_sha256"]
    frame["mapping_observation_status"] = frame.get("mapping_observation_status", "CURRENT")
    frame["revision_status"] = frame.apply(
        lambda row: record(row).revision_status if record(row) else "CURRENT", axis=1
    )
    frame["supersedes_fact_version"] = frame.apply(
        lambda row: record(row).supersedes_fact_version if record(row) else None,
        axis=1,
    )
    return frame


def stable_frame(value: Any, filename: str) -> Any:
    import pandas as pd

    frame = pd.DataFrame(value)
    for column in PARQUET_COLUMNS[filename]:
        if column not in frame.columns:
            frame[column] = None
    frame = frame.reindex(columns=list(PARQUET_COLUMNS[filename]))
    if len(frame):
        sort_cols = [c for c in PARQUET_COLUMNS[filename] if c in frame.columns]
        sort_frame = frame[sort_cols].map(
            lambda value: repr(value) if isinstance(value, (list, dict, set)) else value
        )
        frame = frame.iloc[sort_frame.sort_values(sort_cols, kind="mergesort").index].reset_index(
            drop=True
        )
    return frame


@dataclass(frozen=True)
class EtfBenchmarkConstituentBundle:
    output_dir: Path
    bundle_identity: str
    machine_gate: dict[str, Any]

    @classmethod
    def load(cls, output_dir: Path) -> EtfBenchmarkConstituentBundle:
        path = Path(output_dir)
        gate = json.loads((path / "machine_gate.json").read_text())
        files = [
            "etf_benchmark_vintages.parquet",
            "index_weight_vintages.parquet",
            "index_weight_constituents.parquet",
            "source_resolution_manifest.parquet",
            "coverage_and_gap_report.json",
        ]
        hashes = {name: hashlib.sha256((path / name).read_bytes()).hexdigest() for name in files}
        identity = hashlib.sha256(
            json.dumps(
                {
                    "file_sha256": hashes,
                    "gate_payload": {k: v for k, v in gate.items() if k != "bundle_identity"},
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return cls(path, identity, gate)

    def verify(self) -> bool:
        import pandas as pd

        expected = {
            "etf_benchmark_vintages.parquet",
            "index_weight_vintages.parquet",
            "index_weight_constituents.parquet",
            "source_resolution_manifest.parquet",
            "coverage_and_gap_report.json",
            "machine_gate.json",
        }
        if {p.name for p in self.output_dir.iterdir()} != expected:
            raise ValueError("bundle file set mismatch")
        gate = json.loads((self.output_dir / "machine_gate.json").read_text())
        disk_frames = {
            name.removesuffix(".parquet"): pd.read_parquet(self.output_dir / name).to_dict(
                "records"
            )
            for name in PARQUET_COLUMNS
        }
        recomputed_gates = recompute_artifact_gate_passes(
            disk_frames,
            json.loads((self.output_dir / "coverage_and_gap_report.json").read_text()),
            gate,
        )
        for gate_name, result in recomputed_gates.items():
            if not any(disk_frames.values()):
                continue
            if gate_name != "DETERMINISTIC_REPLAY" and (
                gate_name in gate["gates"]
                and gate["gates"][gate_name]["passes"] != result["passes"]
            ):
                raise ValueError("machine gate semantic mismatch")
        required = {
            "APPEND_ONLY",
            "NO_FUTURE_VISIBILITY",
            "EXACT_LINEAGE",
            "UNKNOWN_FAILS_CLOSED",
            "REVISION_PRESERVED",
            "WEIGHT_INTEGRITY",
            "DETERMINISTIC_REPLAY",
            "NO_ECONOMIC_LABEL_DEPENDENCY",
            "PUBLIC_SAFETY",
        }
        if set(gate.get("gates", {})) != required:
            raise ValueError("machine gate schema mismatch")
        if any(
            set(value) != {"passes", "evidence"} or type(value["passes"]) is not bool
            for value in gate["gates"].values()
        ):
            raise ValueError("machine gate entries mismatch")
        if gate.get("passes") != all(value["passes"] for value in gate["gates"].values()):
            raise ValueError("machine gate conjunction mismatch")
        expected_columns = {
            "etf_benchmark_vintages.parquet": {"etf_symbol", "index_code"},
            "index_weight_vintages.parquet": {"index_code", "provider_trade_date", "fact_version"},
            "index_weight_constituents.parquet": {"index_code", "con_code", "fact_version"},
            "source_resolution_manifest.parquet": {
                "request_identity",
                "endpoint",
                "resolution_status",
                "coverage_status",
            },
        }
        forbidden = {
            "return",
            "performance",
            "score",
            "rank",
            "target",
            "action",
            "signal",
            "promotion",
        }
        for filename, required_cols in expected_columns.items():
            frame = pd.read_parquet(self.output_dir / filename)
            if tuple(frame.columns) != PARQUET_COLUMNS[filename]:
                raise ValueError("parquet canonical column order mismatch")
            if not required_cols.issubset(set(frame.columns)) or not set(
                PARQUET_COLUMNS[filename]
            ).issubset(set(frame.columns)):
                raise ValueError("parquet schema mismatch")
            if forbidden.intersection({str(c).lower() for c in frame.columns}):
                raise ValueError("forbidden semantic column")
        report = json.loads((self.output_dir / "coverage_and_gap_report.json").read_text())
        if any(token in json.dumps(report, sort_keys=True).lower() for token in forbidden):
            raise ValueError("forbidden semantic key")
        parquet_rows = pd.read_parquet(
            self.output_dir / "source_resolution_manifest.parquet"
        ).to_dict("records")
        fields = (
            "request_identity",
            "endpoint",
            "coverage_status",
            "resolution_status",
            "observation_identity",
            "fact_version",
        )
        norm = lambda row: tuple(
            None if row.get(field) is None else str(row.get(field)) for field in fields
        )
        if sorted(norm(row) for row in report.get("items", [])) != sorted(
            norm(row) for row in parquet_rows
        ):
            raise ValueError("coverage/resolution parity mismatch")
        artifact_rows = {
            name.removesuffix(".parquet"): pd.read_parquet(self.output_dir / name).to_dict(
                "records"
            )
            for name in PARQUET_COLUMNS
        }
        artifact_rows = canonicalize(artifact_rows)
        _canonical = json.dumps(
            {
                "resolutions": canonicalize(
                    sorted(
                        parquet_rows,
                        key=lambda row: (
                            str(row.get("request_identity")),
                            str(row.get("endpoint")),
                        ),
                    )
                ),
                "artifacts": artifact_rows,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        replay_hash = hashlib.sha256(_canonical.encode()).hexdigest()
        deterministic = (
            gate["gates"]
            .get("DETERMINISTIC_REPLAY", {})
            .get("evidence", {})
            .get("canonical_sha256")
        )
        if deterministic is not None and deterministic != replay_hash:
            raise ValueError("deterministic replay evidence mismatch")
        expected_hashes = {
            "etf_benchmark_vintages.parquet",
            "index_weight_vintages.parquet",
            "index_weight_constituents.parquet",
            "source_resolution_manifest.parquet",
            "coverage_and_gap_report.json",
        }
        if set(gate.get("file_sha256", {})) != expected_hashes:
            raise ValueError("bundle hash schema mismatch")
        recomputed = hashlib.sha256(
            json.dumps(
                {
                    "file_sha256": gate["file_sha256"],
                    "gate_payload": {k: v for k, v in gate.items() if k != "bundle_identity"},
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if (
            gate.get("bundle_identity") != recomputed
            or gate.get("bundle_identity") != self.bundle_identity
        ):
            raise ValueError("bundle identity/hash mismatch")
        for name, digest in gate["file_sha256"].items():
            if hashlib.sha256((self.output_dir / name).read_bytes()).hexdigest() != digest:
                raise ValueError("bundle tampering detected")
        return True


__all__ = [
    "PARQUET_COLUMNS",
    "EtfBenchmarkConstituentBundle",
    "compute_machine_gates",
    "project_mapping_metadata",
    "stable_frame",
]

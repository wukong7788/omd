"""Shared Arrow schema, serialization, and verification helpers for SEC financials datasets."""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from .adr_metadata import SecAdrShareMetadata
from .financials import (
    FINANCIALS_DATASET_SCHEMA,
    FINANCIALS_DATASET_SCHEMA_V4,
    FINANCIALS_DATASET_SCHEMA_V5,
    SecCompanyFinancialVintage,
    SecStatementRow,
)
from .unit_evidence import SecFinancialUnitEvidence

SchemaVersion = Literal["v3", "v4", "v5"]

WRITER_PROFILE = "sec-financials-parquet-v3"
WRITER_PROFILE_V4 = "sec-financials-parquet-v4"
WRITER_PROFILE_V5 = "sec-financials-parquet-v5"

SCHEMA_METADATA: dict[SchemaVersion, tuple[str, str]] = {
    "v3": (FINANCIALS_DATASET_SCHEMA, WRITER_PROFILE),
    "v4": (FINANCIALS_DATASET_SCHEMA_V4, WRITER_PROFILE_V4),
    "v5": (FINANCIALS_DATASET_SCHEMA_V5, WRITER_PROFILE_V5),
}


def load_pyarrow() -> Any:
    try:
        return importlib.import_module("pyarrow")
    except ImportError as exc:
        raise RuntimeError("install the optional 'sec-financials' extra to write Parquet") from exc


def load_parquet() -> Any:
    return importlib.import_module("pyarrow.parquet")


def detect_schema_version(manifest: Mapping[str, Any]) -> SchemaVersion:
    ds = manifest.get("dataset_schema")
    wp = manifest.get("writer_profile")
    for version, (expected_ds, expected_wp) in SCHEMA_METADATA.items():
        if ds == expected_ds and wp == expected_wp:
            return version
    raise ValueError("incompatible financials dataset schema or writer profile")


def _json_compact(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def vintage_schema(pa: Any, *, version: SchemaVersion = "v3") -> Any:
    fields = [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("cik", pa.string(), nullable=False),
        pa.field("company_name", pa.string(), nullable=False),
        pa.field("form", pa.string(), nullable=False),
        pa.field("accession_number", pa.string(), nullable=False),
        pa.field("filing_date", pa.date32(), nullable=False),
        pa.field("fiscal_year", pa.int32(), nullable=True),
        pa.field("fiscal_period", pa.string(), nullable=True),
        pa.field("period_end", pa.date32(), nullable=True),
        pa.field("accepted_at", pa.timestamp("us", tz="UTC"), nullable=True),
        pa.field("availability_anchor", pa.timestamp("us", tz="UTC"), nullable=True),
        pa.field("availability_basis", pa.string(), nullable=False),
        pa.field("availability_precision", pa.string(), nullable=False),
        pa.field("availability_policy", pa.string(), nullable=False),
        pa.field("availability_lag_days", pa.int32(), nullable=False),
        pa.field("is_amendment", pa.bool_(), nullable=False),
        pa.field("quality_flags", pa.list_(pa.field("element", pa.string())), nullable=False),
        pa.field("row_count", pa.int64(), nullable=False),
        pa.field("vintage_identity", pa.string(), nullable=False),
    ]
    if version in {"v4", "v5"}:
        fields += [
            pa.field("unit_evidence_payload", pa.string(), nullable=True),
            pa.field("unit_evidence_identity", pa.string(), nullable=True),
        ]
    if version == "v5":
        fields += [
            pa.field("adr_metadata_payload", pa.string(), nullable=True),
            pa.field("adr_metadata_identity", pa.string(), nullable=True),
        ]
    ds, wp = SCHEMA_METADATA[version]
    return pa.schema(
        fields,
        metadata={b"omd.dataset_schema": ds.encode(), b"omd.writer_profile": wp.encode()},
    )


def statement_schema(pa: Any, *, version: SchemaVersion = "v3") -> Any:
    fields = [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("accession_number", pa.string(), nullable=False),
        pa.field("statement_type", pa.string(), nullable=False),
        pa.field("standard_concept", pa.string(), nullable=False),
        pa.field("concept", pa.string(), nullable=False),
        pa.field("label", pa.string(), nullable=False),
        pa.field("value", pa.string(), nullable=True),
        pa.field("value_native", pa.string(), nullable=True),
        pa.field("unit", pa.string(), nullable=True),
        pa.field("currency", pa.string(), nullable=True),
        pa.field("decimals", pa.int32(), nullable=True),
        pa.field("period_start", pa.date32(), nullable=True),
        pa.field("period_end", pa.date32(), nullable=True),
        pa.field("period_type", pa.string(), nullable=True),
        pa.field("dimension", pa.string(), nullable=True),
        pa.field("period_key", pa.string(), nullable=True),
        pa.field("context_ref", pa.string(), nullable=True),
        pa.field("unit_ref", pa.string(), nullable=True),
        pa.field("decimals_native", pa.string(), nullable=True),
        pa.field("period_source", pa.string(), nullable=True),
        pa.field("is_point_in_time", pa.bool_(), nullable=False),
        pa.field("availability_anchor", pa.timestamp("us", tz="UTC"), nullable=True),
    ]
    if version in {"v4", "v5"}:
        fields.append(pa.field("vintage_identity", pa.string(), nullable=False))
    if version == "v5":
        fields.append(pa.field("cik", pa.string(), nullable=False))
    ds, wp = SCHEMA_METADATA[version]
    return pa.schema(
        fields,
        metadata={b"omd.dataset_schema": ds.encode(), b"omd.writer_profile": wp.encode()},
    )


def serialize_tables(
    sorted_vintages: Iterable[SecCompanyFinancialVintage],
    *,
    version: SchemaVersion,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    vintage_rows: list[dict[str, Any]] = []
    statement_rows: list[dict[str, Any]] = []
    preserve_order = version in {"v4", "v5"}

    for v in sorted_vintages:
        vintage_row: dict[str, Any] = {
            "symbol": v.symbol,
            "cik": v.cik,
            "company_name": v.company_name,
            "form": v.form,
            "accession_number": v.accession_number,
            "filing_date": v.filing_date,
            "fiscal_year": v.fiscal_year,
            "fiscal_period": v.fiscal_period,
            "period_end": v.period_end,
            "accepted_at": v.accepted_at,
            "availability_anchor": v.availability_anchor,
            "availability_basis": v.availability_basis,
            "availability_precision": v.availability_precision,
            "availability_policy": v.availability_policy,
            "availability_lag_days": v.availability_lag_days,
            "is_amendment": v.is_amendment,
            "quality_flags": list(v.quality_flags),
            "row_count": len(v.rows),
            "vintage_identity": v.vintage_identity,
        }
        if version in {"v4", "v5"}:
            ev = v.unit_evidence
            vintage_row["unit_evidence_payload"] = (
                _json_compact(ev.canonical_payload) if ev else None
            )
            vintage_row["unit_evidence_identity"] = ev.evidence_identity if ev else None
        if version == "v5":
            am = v.adr_metadata
            vintage_row["adr_metadata_payload"] = (
                _json_compact(am.canonical_payload) if am else None
            )
            vintage_row["adr_metadata_identity"] = am.metadata_identity if am else None
        vintage_rows.append(vintage_row)

        ordered_rows = (
            list(v.rows)
            if preserve_order
            else sorted(
                v.rows,
                key=lambda r: (
                    r.statement_type,
                    r.standard_concept,
                    r.concept,
                    r.period_end or date.min,
                ),
            )
        )
        for r in ordered_rows:
            statement_row: dict[str, Any] = {
                **r.to_dict(),
                "symbol": v.symbol,
                "accession_number": v.accession_number,
                "value": str(r.value) if r.value is not None else None,
                "availability_anchor": v.availability_anchor,
            }
            if version in {"v4", "v5"}:
                statement_row["vintage_identity"] = v.vintage_identity
            if version == "v5":
                statement_row["cik"] = v.cik
            statement_rows.append(statement_row)

    return vintage_rows, statement_rows


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_parquet_tables_and_manifest(
    dest: Path,
    *,
    vintage_rows: list[dict[str, Any]],
    statement_rows: list[dict[str, Any]],
    version: SchemaVersion,
    manifest_fields: Mapping[str, Any],
) -> dict[str, Any]:
    pa = load_pyarrow()
    pq = load_parquet()

    v_table = pa.Table.from_pylist(vintage_rows, schema=vintage_schema(pa, version=version))
    v_path = dest / "company_financial_vintages.parquet"
    v_tmp = v_path.with_suffix(".tmp")
    pq.write_table(v_table, v_tmp, compression="zstd")
    v_tmp.replace(v_path)

    s_table = pa.Table.from_pylist(statement_rows, schema=statement_schema(pa, version=version))
    s_path = dest / "financial_statements.parquet"
    s_tmp = s_path.with_suffix(".tmp")
    pq.write_table(s_table, s_tmp, compression="zstd")
    s_tmp.replace(s_path)

    ds, wp = SCHEMA_METADATA[version]
    manifest: dict[str, Any] = {
        **manifest_fields,
        "vintage_count": len(vintage_rows),
        "statement_row_count": len(statement_rows),
        "files": {
            "company_financial_vintages.parquet": {
                "sha256": sha256_file(v_path),
                "bytes": v_path.stat().st_size,
            },
            "financial_statements.parquet": {
                "sha256": sha256_file(s_path),
                "bytes": s_path.stat().st_size,
            },
        },
        "dataset_schema": ds,
        "writer_profile": wp,
    }
    m_path = dest / "manifest.json"
    m_tmp = m_path.with_suffix(".tmp")
    m_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    m_tmp.replace(m_path)
    return manifest


def _evidence_from_record(record: Mapping[str, Any]) -> SecFinancialUnitEvidence | None:
    payload_text = record["unit_evidence_payload"]
    identity = record["unit_evidence_identity"]
    if payload_text is None and identity is None:
        return None
    if not isinstance(payload_text, str) or not isinstance(identity, str):
        raise TypeError("v4 unit evidence payload and identity must be paired")
    try:
        payload = json.loads(payload_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid v4 unit evidence payload") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "parser_version",
        "cik",
        "accession_number",
        "instance_document",
        "instance_sha256",
        "schema_version",
    }:
        raise ValueError("invalid v4 unit evidence payload")
    if payload.pop("schema_version") != "sec-financial-unit-evidence-v1":
        raise ValueError("unknown v4 unit evidence schema")
    try:
        evidence = SecFinancialUnitEvidence(**payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid v4 unit evidence payload") from exc
    if evidence.evidence_identity != identity:
        raise ValueError("v4 unit evidence identity mismatch")
    return evidence


def _adr_metadata_from_record(record: Mapping[str, Any]) -> SecAdrShareMetadata | None:
    payload_text = record["adr_metadata_payload"]
    identity = record["adr_metadata_identity"]
    if payload_text is None and identity is None:
        return None
    if not isinstance(payload_text, str) or not isinstance(identity, str):
        raise TypeError("v5 ADR metadata payload and identity must be paired")
    try:
        payload = json.loads(payload_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid v5 ADR metadata payload") from exc
    if not isinstance(payload, dict) or not payload:
        raise ValueError("invalid v5 ADR metadata payload")
    adr_meta = SecAdrShareMetadata.from_payload(payload)
    if adr_meta.metadata_identity != identity:
        raise ValueError("v5 ADR metadata identity mismatch")
    return adr_meta


def validate_and_reconstruct_linked_vintages(
    dest: Path,
    pq: Any,
    *,
    version: Literal["v4", "v5"],
) -> tuple[SecCompanyFinancialVintage, ...]:
    """Verify full v4/v5 table links, evidence, ADR metadata, and reconstructed identities."""
    vintage_records = pq.ParquetFile(dest / "company_financial_vintages.parquet").read().to_pylist()
    statement_records = pq.ParquetFile(dest / "financial_statements.parquet").read().to_pylist()
    vintages_by_identity: dict[str, dict[str, Any]] = {}
    ordered_identities: list[str] = []
    for record in vintage_records:
        identity = record["vintage_identity"]
        if not isinstance(identity, str) or identity in vintages_by_identity:
            raise ValueError(f"duplicate or invalid {version} vintage_identity")
        vintages_by_identity[identity] = record
        ordered_identities.append(identity)

    rows_by_identity: dict[str, list[dict[str, Any]]] = {
        identity: [] for identity in ordered_identities
    }
    for row in statement_records:
        identity = row["vintage_identity"]
        if identity not in rows_by_identity:
            raise ValueError(f"orphan {version} statement vintage_identity")
        vintage = vintages_by_identity[identity]
        if (
            row["symbol"] != vintage["symbol"]
            or row["accession_number"] != vintage["accession_number"]
            or row["availability_anchor"] != vintage["availability_anchor"]
            or (version == "v5" and row["cik"] != vintage["cik"])
        ):
            raise ValueError(f"{version} statement does not match linked vintage")
        rows_by_identity[identity].append(row)

    reconstructed: list[SecCompanyFinancialVintage] = []
    for identity in ordered_identities:
        record = vintages_by_identity[identity]
        linked_rows = rows_by_identity[identity]
        if record["row_count"] != len(linked_rows):
            raise ValueError(f"{version} vintage row_count does not match linked statements")
        evidence = _evidence_from_record(record)
        adr_meta = _adr_metadata_from_record(record) if version == "v5" else None
        try:
            rows = tuple(
                SecStatementRow(
                    statement_type=row["statement_type"],
                    standard_concept=row["standard_concept"],
                    concept=row["concept"],
                    label=row["label"],
                    value=Decimal(row["value"]) if row["value"] is not None else None,
                    value_native=row["value_native"],
                    unit=row["unit"],
                    decimals=row["decimals"],
                    period_start=row["period_start"],
                    period_end=row["period_end"],
                    period_type=row["period_type"],
                    dimension=row["dimension"],
                    period_key=row["period_key"],
                    context_ref=row["context_ref"],
                    unit_ref=row["unit_ref"],
                    decimals_native=row["decimals_native"],
                    period_source=row["period_source"],
                    is_point_in_time=row["is_point_in_time"],
                )
                for row in linked_rows
            )
            if any(row.currency != stored["currency"] for row, stored in zip(rows, linked_rows)):
                raise ValueError(f"{version} statement currency does not match unit")
            vintage = SecCompanyFinancialVintage(
                symbol=record["symbol"],
                cik=record["cik"],
                company_name=record["company_name"],
                form=record["form"],
                accession_number=record["accession_number"],
                filing_date=record["filing_date"],
                fiscal_year=record["fiscal_year"],
                fiscal_period=record["fiscal_period"],
                period_end=record["period_end"],
                accepted_at=record["accepted_at"],
                availability_anchor=record["availability_anchor"],
                availability_basis=record["availability_basis"],
                availability_precision=record["availability_precision"],
                availability_policy=record["availability_policy"],
                availability_lag_days=record["availability_lag_days"],
                is_amendment=record["is_amendment"],
                quality_flags=tuple(record["quality_flags"]),
                rows=rows,
                unit_evidence=evidence,
                adr_metadata=adr_meta,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid {version} financial vintage record") from exc
        if vintage.vintage_identity != identity:
            raise ValueError(f"{version} reconstructed vintage_identity mismatch")
        reconstructed.append(vintage)
    return tuple(reconstructed)


def validate_manifest_files_and_schemas(
    dest: Path,
    manifest: Mapping[str, Any],
    *,
    version: SchemaVersion,
) -> Any:
    """Verify required files, sizes, SHA-256 digests, Arrow schemas, and row counts."""
    expected = {"company_financial_vintages.parquet", "financial_statements.parquet"}
    if not isinstance(manifest.get("files"), Mapping) or set(manifest["files"]) != expected:
        raise ValueError("financials manifest must contain both required tables")
    pa = load_pyarrow()
    pq = load_parquet()
    for filename, schema in (
        ("company_financial_vintages.parquet", vintage_schema(pa, version=version)),
        ("financial_statements.parquet", statement_schema(pa, version=version)),
    ):
        path = dest / filename
        if not path.is_file():
            raise ValueError("required financials table missing")
        meta = manifest["files"][filename]
        if (
            not isinstance(meta, Mapping)
            or path.stat().st_size != meta.get("bytes")
            or sha256_file(path) != meta.get("sha256")
        ):
            raise ValueError("financials table hash or size mismatch")
        table = pq.ParquetFile(path)
        if not table.schema_arrow.equals(schema, check_metadata=True):
            raise ValueError("incompatible financials table schema")
        count_key = (
            "vintage_count"
            if filename == "company_financial_vintages.parquet"
            else "statement_row_count"
        )
        if (
            type(manifest.get(count_key)) is not int
            or manifest[count_key] != table.metadata.num_rows
        ):
            raise ValueError("financials manifest row count mismatch")
    return pq


def vintage_sort_key(
    vintage: SecCompanyFinancialVintage, *, include_cik: bool = False
) -> tuple[Any, ...]:
    base: tuple[Any, ...] = (
        vintage.filing_date,
        vintage.accepted_at or datetime.min.replace(tzinfo=UTC),
        vintage.accession_number,
    )
    if include_cik:
        return (vintage.cik, *base, vintage.vintage_identity)
    return base


__all__ = [
    "SCHEMA_METADATA",
    "WRITER_PROFILE",
    "WRITER_PROFILE_V4",
    "WRITER_PROFILE_V5",
    "SchemaVersion",
    "detect_schema_version",
    "load_parquet",
    "load_pyarrow",
    "serialize_tables",
    "sha256_file",
    "statement_schema",
    "validate_and_reconstruct_linked_vintages",
    "validate_manifest_files_and_schemas",
    "vintage_schema",
    "vintage_sort_key",
    "write_parquet_tables_and_manifest",
]

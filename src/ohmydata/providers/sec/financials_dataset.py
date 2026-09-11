"""Deterministic Parquet writer for company financial statement vintages and line items."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .financials import (
    FINANCIALS_DATASET_SCHEMA,
    FINANCIALS_DATASET_SCHEMA_V4,
    SecCompanyFinancialVintage,
    SecStatementRow,
)
from .unit_evidence import SecFinancialUnitEvidence

DATASET_SCHEMA = FINANCIALS_DATASET_SCHEMA
WRITER_PROFILE = "sec-financials-parquet-v3"
WRITER_PROFILE_V4 = "sec-financials-parquet-v4"


import importlib


def _pa() -> Any:
    try:
        return importlib.import_module("pyarrow")
    except ImportError as exc:
        raise RuntimeError("install the optional 'sec-financials' extra to write Parquet") from exc


def _vintage_schema(pa: Any, *, v4: bool = False) -> Any:
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
    if v4:
        fields.extend(
            [
                pa.field("unit_evidence_payload", pa.string(), nullable=True),
                pa.field("unit_evidence_identity", pa.string(), nullable=True),
            ]
        )
    return pa.schema(
        fields,
        metadata={
            b"omd.dataset_schema": (
                FINANCIALS_DATASET_SCHEMA_V4 if v4 else DATASET_SCHEMA
            ).encode(),
            b"omd.writer_profile": (WRITER_PROFILE_V4 if v4 else WRITER_PROFILE).encode(),
        },
    )


def _statement_schema(pa: Any, *, v4: bool = False) -> Any:
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
    if v4:
        fields.append(pa.field("vintage_identity", pa.string(), nullable=False))
    return pa.schema(
        fields,
        metadata={
            b"omd.dataset_schema": (
                FINANCIALS_DATASET_SCHEMA_V4 if v4 else DATASET_SCHEMA
            ).encode(),
            b"omd.writer_profile": (WRITER_PROFILE_V4 if v4 else WRITER_PROFILE).encode(),
        },
    )


def validate_financials_partition(partition: str | Path) -> dict[str, Any]:
    """Reject stale schemas, partial partitions and mismatched table identities."""
    dest = Path(partition)
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    is_v4 = (
        manifest.get("dataset_schema") == FINANCIALS_DATASET_SCHEMA_V4
        and manifest.get("writer_profile") == WRITER_PROFILE_V4
    )
    is_v3 = (
        manifest.get("dataset_schema") == DATASET_SCHEMA
        and manifest.get("writer_profile") == WRITER_PROFILE
    )
    if not (is_v3 or is_v4):
        raise ValueError("incompatible financials dataset schema or writer profile")
    expected = {"company_financial_vintages.parquet", "financial_statements.parquet"}
    if set(manifest.get("files", {})) != expected:
        raise ValueError("financials manifest must contain both required tables")
    pa = _pa()
    pq = importlib.import_module("pyarrow.parquet")
    symbol = dest.name.removeprefix("symbol=")
    if not dest.name.startswith("symbol=") or manifest.get("symbol") != symbol:
        raise ValueError("financials manifest symbol does not match partition identity")
    for filename, schema in (
        ("company_financial_vintages.parquet", _vintage_schema(pa, v4=is_v4)),
        ("financial_statements.parquet", _statement_schema(pa, v4=is_v4)),
    ):
        path = dest / filename
        if not path.is_file():
            raise ValueError("required financials table missing")
        meta = manifest["files"][filename]
        if path.stat().st_size != meta.get("bytes") or hashlib.sha256(
            path.read_bytes()
        ).hexdigest() != meta.get("sha256"):
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
        symbols = table.read(columns=["symbol"]).column("symbol").to_pylist()
        if any(value != symbol for value in symbols):
            raise ValueError("financials table symbol does not match partition identity")
    if is_v4:
        _validate_v4_links(dest, pq)
    return manifest


def _evidence_from_record(record: dict[str, Any]) -> SecFinancialUnitEvidence | None:
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


def _validate_v4_links(dest: Path, pq: Any) -> None:
    vintage_records = pq.ParquetFile(dest / "company_financial_vintages.parquet").read().to_pylist()
    statement_records = pq.ParquetFile(dest / "financial_statements.parquet").read().to_pylist()
    vintages_by_identity: dict[str, dict[str, Any]] = {}
    for record in vintage_records:
        identity = record["vintage_identity"]
        if not isinstance(identity, str) or identity in vintages_by_identity:
            raise ValueError("duplicate or invalid v4 vintage_identity")
        vintages_by_identity[identity] = record
    rows_by_identity: dict[str, list[dict[str, Any]]] = {
        identity: [] for identity in vintages_by_identity
    }
    for row in statement_records:
        identity = row["vintage_identity"]
        if identity not in rows_by_identity:
            raise ValueError("orphan v4 statement vintage_identity")
        vintage = vintages_by_identity[identity]
        if (
            row["symbol"] != vintage["symbol"]
            or row["accession_number"] != vintage["accession_number"]
            or row["availability_anchor"] != vintage["availability_anchor"]
        ):
            raise ValueError("v4 statement does not match linked vintage")
        rows_by_identity[identity].append(row)
    for identity, record in vintages_by_identity.items():
        linked_rows = rows_by_identity[identity]
        if record["row_count"] != len(linked_rows):
            raise ValueError("v4 vintage row_count does not match linked statements")
        evidence = _evidence_from_record(record)
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
                raise ValueError("v4 statement currency does not match unit")
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
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid v4 financial vintage record") from exc
        if vintage.vintage_identity != identity:
            raise ValueError("v4 reconstructed vintage_identity mismatch")


def write_financials_partition(
    root: str | Path,
    symbol: str,
    vintages: Iterable[SecCompanyFinancialVintage],
) -> dict[str, Any]:
    """Write company financial statement vintages and rows into a partitioned directory."""
    pa = _pa()
    pq: Any = importlib.import_module("pyarrow.parquet")

    final_dest = Path(root) / f"symbol={symbol.upper()}"

    sorted_vintages = sorted(
        vintages,
        key=lambda v: (
            v.filing_date,
            v.accepted_at or datetime.min.replace(tzinfo=UTC),
            v.accession_number,
        ),
    )
    is_v4 = any(v.unit_evidence is not None for v in sorted_vintages)
    dataset_schema = FINANCIALS_DATASET_SCHEMA_V4 if is_v4 else DATASET_SCHEMA
    writer_profile = WRITER_PROFILE_V4 if is_v4 else WRITER_PROFILE
    if any(v.symbol != symbol.upper() for v in sorted_vintages):
        raise ValueError("vintage symbol must exactly match the uppercase partition symbol")
    if final_dest.exists():
        existing = validate_financials_partition(final_dest)
        if (
            existing["dataset_schema"] != dataset_schema
            or existing["writer_profile"] != writer_profile
        ):
            raise ValueError("immutable financials partition differs; write to a new output root")
        table = pq.ParquetFile(final_dest / "company_financial_vintages.parquet").read()
        if table.column("vintage_identity").to_pylist() == [
            v.vintage_identity for v in sorted_vintages
        ]:
            return existing
        raise ValueError("immutable financials partition differs; write to a new output root")
    Path(root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".financials-", dir=root) as stage:
        dest = Path(stage)
        vintage_rows: list[dict[str, Any]] = []
        statement_rows: list[dict[str, Any]] = []

        for v in sorted_vintages:
            vintage_row = {
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
            if is_v4:
                vintage_row["unit_evidence_payload"] = (
                    json.dumps(
                        v.unit_evidence.canonical_payload, sort_keys=True, separators=(",", ":")
                    )
                    if v.unit_evidence is not None
                    else None
                )
                vintage_row["unit_evidence_identity"] = (
                    v.unit_evidence.evidence_identity if v.unit_evidence is not None else None
                )
            vintage_rows.append(vintage_row)
            sorted_rows = (
                list(v.rows)
                if is_v4
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
            for r in sorted_rows:
                statement_row = {
                    "symbol": v.symbol,
                    "accession_number": v.accession_number,
                    "statement_type": r.statement_type,
                    "standard_concept": r.standard_concept,
                    "concept": r.concept,
                    "label": r.label,
                    "value": str(r.value) if r.value is not None else None,
                    "value_native": r.value_native,
                    "unit": r.unit,
                    "currency": r.currency,
                    "decimals": r.decimals,
                    "period_start": r.period_start,
                    "period_end": r.period_end,
                    "period_type": r.period_type,
                    "dimension": r.dimension,
                    "period_key": r.period_key,
                    "context_ref": r.context_ref,
                    "unit_ref": r.unit_ref,
                    "decimals_native": r.decimals_native,
                    "period_source": r.period_source,
                    "is_point_in_time": r.is_point_in_time,
                    "availability_anchor": v.availability_anchor,
                }
                if is_v4:
                    statement_row["vintage_identity"] = v.vintage_identity
                statement_rows.append(statement_row)

        # 1. Write company_financial_vintages.parquet atomically
        v_schema = _vintage_schema(pa, v4=is_v4)
        v_table = pa.Table.from_pylist(vintage_rows, schema=v_schema)
        v_path = dest / "company_financial_vintages.parquet"
        v_tmp = v_path.with_suffix(".tmp")
        pq.write_table(v_table, v_tmp, compression="zstd")
        v_tmp.replace(v_path)

        # 2. Write financial_statements.parquet atomically
        s_schema = _statement_schema(pa, v4=is_v4)
        s_table = pa.Table.from_pylist(statement_rows, schema=s_schema)
        s_path = dest / "financial_statements.parquet"
        s_tmp = s_path.with_suffix(".tmp")
        pq.write_table(s_table, s_tmp, compression="zstd")
        s_tmp.replace(s_path)

        # 3. Write manifest.json atomically
        def sha256_file(p: Path) -> str:
            h = hashlib.sha256()
            h.update(p.read_bytes())
            return h.hexdigest()

        manifest = {
            "symbol": symbol.upper(),
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
            "dataset_schema": dataset_schema,
            "writer_profile": writer_profile,
        }
        m_path = dest / "manifest.json"
        m_tmp = m_path.with_suffix(".tmp")
        m_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        m_tmp.replace(m_path)

        # Publish the complete partition in one directory rename. A concurrent winner
        # leaves a nonempty target, causing rename to fail instead of overwriting data.
        dest.rename(final_dest)

    return manifest

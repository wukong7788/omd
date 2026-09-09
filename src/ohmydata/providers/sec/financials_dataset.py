"""Deterministic Parquet writer for company financial statement vintages and line items."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .financials import SecCompanyFinancialVintage

DATASET_SCHEMA = "sec-company-financials-v2"
WRITER_PROFILE = "sec-financials-parquet-v2"


import importlib


def _pa() -> Any:
    try:
        return importlib.import_module("pyarrow")
    except ImportError as exc:
        raise RuntimeError("install the optional 'sec-financials' extra to write Parquet") from exc


def _vintage_schema(pa: Any) -> Any:
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
    return pa.schema(
        fields,
        metadata={
            b"omd.dataset_schema": DATASET_SCHEMA.encode(),
            b"omd.writer_profile": WRITER_PROFILE.encode(),
        },
    )


def _statement_schema(pa: Any) -> Any:
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
    return pa.schema(
        fields,
        metadata={
            b"omd.dataset_schema": DATASET_SCHEMA.encode(),
            b"omd.writer_profile": WRITER_PROFILE.encode(),
        },
    )


def validate_financials_partition(partition: str | Path) -> dict[str, Any]:
    """Reject stale schemas, partial partitions and mismatched table identities."""
    dest = Path(partition)
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    if (
        manifest.get("dataset_schema") != DATASET_SCHEMA
        or manifest.get("writer_profile") != WRITER_PROFILE
    ):
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
        ("company_financial_vintages.parquet", _vintage_schema(pa)),
        ("financial_statements.parquet", _statement_schema(pa)),
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
    return manifest


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
    if any(v.symbol != symbol.upper() for v in sorted_vintages):
        raise ValueError("vintage symbol must exactly match the uppercase partition symbol")
    if final_dest.exists():
        existing = validate_financials_partition(final_dest)
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
            vintage_rows.append(
                {
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
            )
            sorted_rows = sorted(
                v.rows,
                key=lambda r: (
                    r.statement_type,
                    r.standard_concept,
                    r.concept,
                    r.period_end or date.min,
                ),
            )
            for r in sorted_rows:
                statement_rows.append(
                    {
                        "symbol": v.symbol,
                        "accession_number": v.accession_number,
                        "statement_type": r.statement_type,
                        "standard_concept": r.standard_concept,
                        "concept": r.concept,
                        "label": r.label,
                        "value": str(r.value) if r.value is not None else None,
                        "value_native": r.value_native,
                        "unit": r.unit,
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
                )

        # 1. Write company_financial_vintages.parquet atomically
        v_schema = _vintage_schema(pa)
        v_table = pa.Table.from_pylist(vintage_rows, schema=v_schema)
        v_path = dest / "company_financial_vintages.parquet"
        v_tmp = v_path.with_suffix(".tmp")
        pq.write_table(v_table, v_tmp, compression="zstd")
        v_tmp.replace(v_path)

        # 2. Write financial_statements.parquet atomically
        s_schema = _statement_schema(pa)
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
            "dataset_schema": DATASET_SCHEMA,
            "writer_profile": WRITER_PROFILE,
        }
        m_path = dest / "manifest.json"
        m_tmp = m_path.with_suffix(".tmp")
        m_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        m_tmp.replace(m_path)

        # Publish the complete partition in one directory rename. A concurrent winner
        # leaves a nonempty target, causing rename to fail instead of overwriting data.
        dest.rename(final_dest)

    return manifest

"""Deterministic Parquet writer for company financial statement vintages and line items."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .financials import SecCompanyFinancialVintage

DATASET_SCHEMA = "sec-company-financials-v1"
WRITER_PROFILE = "sec-financials-parquet-v1"


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
        pa.field("value", pa.decimal128(28, 4), nullable=True),
        pa.field("value_native", pa.string(), nullable=True),
        pa.field("unit", pa.string(), nullable=True),
        pa.field("decimals", pa.int32(), nullable=True),
        pa.field("period_start", pa.date32(), nullable=True),
        pa.field("period_end", pa.date32(), nullable=True),
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


def write_financials_partition(
    root: str | Path,
    symbol: str,
    vintages: Iterable[SecCompanyFinancialVintage],
) -> dict[str, Any]:
    """Write company financial statement vintages and rows into a partitioned directory."""
    pa = _pa()
    pq: Any = importlib.import_module("pyarrow.parquet")

    dest = Path(root) / f"symbol={symbol.upper()}"
    dest.mkdir(parents=True, exist_ok=True)

    sorted_vintages = sorted(
        vintages,
        key=lambda v: (
            v.filing_date,
            v.accepted_at or datetime.min.replace(tzinfo=UTC),
            v.accession_number,
        ),
    )
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
                    "value": r.value,
                    "value_native": r.value_native,
                    "unit": r.unit,
                    "decimals": r.decimals,
                    "period_start": r.period_start,
                    "period_end": r.period_end,
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

    return manifest

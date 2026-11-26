"""Deterministic immutable Parquet writer and validator for USD-translated SEC bulk fundamentals."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from ._fx_translation_models import (
    BULK_FUNDAMENTALS_USD_DATASET_SCHEMA,
    BULK_FUNDAMENTALS_USD_WRITER_PROFILE,
    SecFxTranslationStatus,
    SecUsdTranslationResult,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_USD_TABLE_FILENAME = "fundamentals_usd.parquet"


def _pa() -> Any:
    try:
        return importlib.import_module("pyarrow")
    except ImportError as exc:
        raise RuntimeError(
            "install an optional pyarrow extra (e.g. 'sec-cli') to write USD Fundamentals Parquet"
        ) from exc


def _usd_fundamentals_schema(pa: Any) -> Any:
    return pa.schema(
        [
            pa.field("cik", pa.string(), nullable=False),
            pa.field("entity_name", pa.string(), nullable=True),
            pa.field("symbols", pa.list_(pa.field("element", pa.string())), nullable=False),
            pa.field("accession_number", pa.string(), nullable=False),
            pa.field("form", pa.string(), nullable=False),
            pa.field("is_amendment", pa.bool_(), nullable=False),
            pa.field("filing_date", pa.date32(), nullable=False),
            pa.field("report_date", pa.date32(), nullable=False),
            pa.field("accepted_at", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("fiscal_year", pa.int32(), nullable=False),
            pa.field("fiscal_period", pa.string(), nullable=False),
            pa.field("period_kind", pa.string(), nullable=False),
            pa.field("period_start", pa.date32(), nullable=True),
            pa.field("period_end", pa.date32(), nullable=False),
            pa.field("taxonomy", pa.string(), nullable=False),
            pa.field("original_currency", pa.string(), nullable=True),
            pa.field("fx_status", pa.string(), nullable=False),
            pa.field("fx_flow_rate_to_usd", pa.string(), nullable=True),
            pa.field("fx_flow_source", pa.string(), nullable=True),
            pa.field("fx_flow_date", pa.date32(), nullable=True),
            pa.field("fx_bs_rate_to_usd", pa.string(), nullable=True),
            pa.field("fx_bs_source", pa.string(), nullable=True),
            pa.field("fx_bs_date", pa.date32(), nullable=True),
            pa.field("fx_provenance_json", pa.string(), nullable=False),
            pa.field("revenue_usd", pa.string(), nullable=True),
            pa.field("gross_profit_usd", pa.string(), nullable=True),
            pa.field("operating_income_usd", pa.string(), nullable=True),
            pa.field("net_income_usd", pa.string(), nullable=True),
            pa.field("ocf_usd", pa.string(), nullable=True),
            pa.field("capex_usd", pa.string(), nullable=True),
            pa.field("total_assets_usd", pa.string(), nullable=True),
            pa.field("total_debt_usd", pa.string(), nullable=True),
            pa.field("eps_diluted_usd", pa.string(), nullable=True),
            pa.field("diluted_shares", pa.string(), nullable=True),
            pa.field("dei_shares_outstanding", pa.string(), nullable=True),
            pa.field("dei_shares_outstanding_end", pa.date32(), nullable=True),
        ],
        metadata={
            b"omd.dataset_schema": BULK_FUNDAMENTALS_USD_DATASET_SCHEMA.encode(),
            b"omd.writer_profile": BULK_FUNDAMENTALS_USD_WRITER_PROFILE.encode(),
        },
    )


def _dec_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _sha256_bytes(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def write_usd_translated_fundamentals_dataset(
    output_dir: str | Path,
    result: SecUsdTranslationResult,
) -> dict[str, Any]:
    """Atomically write immutable USD-translated fundamentals Parquet dataset + manifest.json."""
    if not isinstance(result, SecUsdTranslationResult):
        raise TypeError("result must be a SecUsdTranslationResult")
    pa = _pa()
    pq: Any = importlib.import_module("pyarrow.parquet")
    final_dest = Path(output_dir)
    final_dest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=".bulk-fundamentals-usd-", dir=final_dest.parent
    ) as stage:
        dest = Path(stage)
        records = [
            {
                "cik": r.cik,
                "entity_name": r.entity_name,
                "symbols": list(r.symbols),
                "accession_number": r.accession_number,
                "form": r.form,
                "is_amendment": r.is_amendment,
                "filing_date": r.filing_date,
                "report_date": r.report_date,
                "accepted_at": r.accepted_at,
                "fiscal_year": r.fiscal_year,
                "fiscal_period": r.fiscal_period,
                "period_kind": r.period_kind,
                "period_start": r.period_start,
                "period_end": r.period_end,
                "taxonomy": r.taxonomy,
                "original_currency": r.original_currency,
                "fx_status": r.fx_status.value,
                "fx_flow_rate_to_usd": _dec_str(r.fx_flow_rate_to_usd),
                "fx_flow_source": r.fx_flow_source,
                "fx_flow_date": r.fx_flow_date,
                "fx_bs_rate_to_usd": _dec_str(r.fx_bs_rate_to_usd),
                "fx_bs_source": r.fx_bs_source,
                "fx_bs_date": r.fx_bs_date,
                "fx_provenance_json": r.fx_provenance_json,
                "revenue_usd": _dec_str(r.revenue_usd),
                "gross_profit_usd": _dec_str(r.gross_profit_usd),
                "operating_income_usd": _dec_str(r.operating_income_usd),
                "net_income_usd": _dec_str(r.net_income_usd),
                "ocf_usd": _dec_str(r.ocf_usd),
                "capex_usd": _dec_str(r.capex_usd),
                "total_assets_usd": _dec_str(r.total_assets_usd),
                "total_debt_usd": _dec_str(r.total_debt_usd),
                "eps_diluted_usd": _dec_str(r.eps_diluted_usd),
                "diluted_shares": _dec_str(r.diluted_shares),
                "dei_shares_outstanding": _dec_str(r.dei_shares_outstanding),
                "dei_shares_outstanding_end": r.dei_shares_outstanding_end,
            }
            for r in result.rows
        ]
        table_path = dest / _USD_TABLE_FILENAME
        table_tmp = table_path.with_suffix(".tmp")
        pq.write_table(
            pa.Table.from_pylist(records, schema=_usd_fundamentals_schema(pa)),
            table_tmp,
            compression="zstd",
        )
        table_tmp.replace(table_path)

        status_counts = {status.value: 0 for status in SecFxTranslationStatus}
        for row in result.rows:
            status_counts[row.fx_status.value] += 1

        manifest: dict[str, Any] = {
            "dataset_schema": BULK_FUNDAMENTALS_USD_DATASET_SCHEMA,
            "writer_profile": BULK_FUNDAMENTALS_USD_WRITER_PROFILE,
            "policy": result.policy,
            "counts": {
                "total_rows": len(records),
                **{k.lower(): v for k, v in status_counts.items()},
            },
            "status_counts": status_counts,
            "files": {
                _USD_TABLE_FILENAME: {
                    "sha256": _sha256_bytes(table_path),
                    "bytes": table_path.stat().st_size,
                    "rows": len(records),
                }
            },
        }
        m_path = dest / "manifest.json"
        m_tmp = m_path.with_suffix(".tmp")
        m_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        m_tmp.replace(m_path)

        if final_dest.exists():
            existing = validate_usd_translated_fundamentals_dataset(final_dest)
            if existing == manifest:
                return existing
            raise ValueError(
                "immutable USD fundamentals dataset differs; write to a new output directory"
            )
        dest.rename(final_dest)
    return manifest


def validate_usd_translated_fundamentals_dataset(dataset_dir: str | Path) -> dict[str, Any]:
    """Validate schema, SHA-256 hash, status counts, and PIT FX date bounds."""
    dest = Path(dataset_dir)
    manifest_path = dest / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("USD fundamentals manifest.json missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if type(manifest) is not dict:
        raise ValueError("USD fundamentals manifest.json must be an object")
    if (
        manifest.get("dataset_schema") != BULK_FUNDAMENTALS_USD_DATASET_SCHEMA
        or manifest.get("writer_profile") != BULK_FUNDAMENTALS_USD_WRITER_PROFILE
    ):
        raise ValueError("incompatible USD fundamentals dataset schema or writer profile")
    if type(manifest.get("policy")) is not dict:
        raise ValueError("USD fundamentals manifest policy must be a mapping")
    files_meta = manifest.get("files")
    if type(files_meta) is not dict or set(files_meta) != {_USD_TABLE_FILENAME}:
        raise ValueError("USD fundamentals manifest must list fundamentals_usd.parquet")
    meta = files_meta[_USD_TABLE_FILENAME]
    if (
        type(meta) is not dict
        or type(meta.get("sha256")) is not str
        or _SHA256_RE.fullmatch(meta["sha256"]) is None
        or type(meta.get("bytes")) is not int
        or meta["bytes"] < 0
        or type(meta.get("rows")) is not int
        or meta["rows"] < 0
    ):
        raise ValueError("USD fundamentals manifest file metadata invalid")

    counts = manifest.get("counts")
    status_counts = manifest.get("status_counts")
    if type(counts) is not dict or type(status_counts) is not dict:
        raise ValueError("USD fundamentals manifest counts or status_counts missing")

    table_path = dest / _USD_TABLE_FILENAME
    if not table_path.is_file():
        raise ValueError(f"required USD fundamentals table missing: {_USD_TABLE_FILENAME}")
    if table_path.stat().st_size != meta["bytes"] or _sha256_bytes(table_path) != meta["sha256"]:
        raise ValueError(f"USD fundamentals hash or size mismatch for {_USD_TABLE_FILENAME}")

    pa = _pa()
    pq: Any = importlib.import_module("pyarrow.parquet")
    pf = pq.ParquetFile(table_path)
    if not pf.schema_arrow.equals(_usd_fundamentals_schema(pa), check_metadata=True):
        raise ValueError(f"incompatible parquet schema for {_USD_TABLE_FILENAME}")
    if (
        pf.metadata.num_rows != meta["rows"]
        or type(counts.get("total_rows")) is not int
        or pf.metadata.num_rows != counts["total_rows"]
    ):
        raise ValueError(f"row count mismatch for {_USD_TABLE_FILENAME}")

    valid_statuses = {status.value for status in SecFxTranslationStatus}
    observed_counts = {status.value: 0 for status in SecFxTranslationStatus}
    table = pf.read(columns=["fx_status", "accepted_at", "fx_flow_date", "fx_bs_date"])
    statuses = table.column("fx_status").to_pylist()
    accepted_col = table.column("accepted_at").to_pylist()
    flow_dates = table.column("fx_flow_date").to_pylist()
    bs_dates = table.column("fx_bs_date").to_pylist()
    for status_val, accepted_at, flow_dt, bs_dt in zip(
        statuses, accepted_col, flow_dates, bs_dates, strict=True
    ):
        if status_val not in valid_statuses:
            raise ValueError(f"invalid fx_status in {_USD_TABLE_FILENAME}: {status_val!r}")
        observed_counts[status_val] += 1
        if accepted_at is None:
            raise ValueError("accepted_at cannot be null in USD fundamentals table")
        accepted_day = accepted_at.date()
        if flow_dt is not None and flow_dt > accepted_day:
            raise ValueError("fx_flow_date violates PIT accepted_at bound")
        if bs_dt is not None and bs_dt > accepted_day:
            raise ValueError("fx_bs_date violates PIT accepted_at bound")

    if any(type(v) is not int for v in status_counts.values()) or status_counts != observed_counts:
        raise ValueError("USD fundamentals manifest status_counts mismatch")
    for status_key, expected_val in observed_counts.items():
        if (
            type(counts.get(status_key.lower())) is not int
            or counts[status_key.lower()] != expected_val
        ):
            raise ValueError(f"USD fundamentals manifest count mismatch for {status_key.lower()}")
    return manifest


__all__ = [
    "BULK_FUNDAMENTALS_USD_DATASET_SCHEMA",
    "BULK_FUNDAMENTALS_USD_WRITER_PROFILE",
    "validate_usd_translated_fundamentals_dataset",
    "write_usd_translated_fundamentals_dataset",
]

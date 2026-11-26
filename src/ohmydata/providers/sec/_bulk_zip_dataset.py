"""Deterministic immutable Parquet writer and validator for SEC bulk companyfacts datasets."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._bulk_zip_models import (
    SEC_MULTI_TAXONOMY_CONCEPTS_VERSION,
    SecBulkCikOutcomeStatus,
    SecBulkProjectionResult,
)

BULK_COMPANYFACTS_DATASET_SCHEMA = "sec-bulk-companyfacts-v1"
BULK_COMPANYFACTS_WRITER_PROFILE = "sec-bulk-companyfacts-parquet-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _pa() -> Any:
    try:
        return importlib.import_module("pyarrow")
    except ImportError as exc:
        raise RuntimeError(
            "install an optional pyarrow extra (e.g. 'sec-cli') to write Bulk ZIP Parquet"
        ) from exc


def _fundamentals_schema(pa: Any) -> Any:
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
            pa.field("reporting_currency", pa.string(), nullable=True),
            pa.field("revenue", pa.string(), nullable=True),
            pa.field("gross_profit", pa.string(), nullable=True),
            pa.field("operating_income", pa.string(), nullable=True),
            pa.field("net_income", pa.string(), nullable=True),
            pa.field("ocf", pa.string(), nullable=True),
            pa.field("capex", pa.string(), nullable=True),
            pa.field("total_assets", pa.string(), nullable=True),
            pa.field("total_debt", pa.string(), nullable=True),
            pa.field("diluted_shares", pa.string(), nullable=True),
            pa.field("eps_diluted", pa.string(), nullable=True),
            pa.field("dei_shares_outstanding", pa.string(), nullable=True),
            pa.field("dei_shares_outstanding_end", pa.date32(), nullable=True),
            pa.field("source_tags_json", pa.string(), nullable=False),
        ],
        metadata={
            b"omd.dataset_schema": BULK_COMPANYFACTS_DATASET_SCHEMA.encode(),
            b"omd.writer_profile": BULK_COMPANYFACTS_WRITER_PROFILE.encode(),
        },
    )


def _facts_schema(pa: Any) -> Any:
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
            pa.field("namespace", pa.string(), nullable=False),
            pa.field("tag", pa.string(), nullable=False),
            pa.field("unit", pa.string(), nullable=False),
            pa.field("currency", pa.string(), nullable=True),
            pa.field("value", pa.string(), nullable=False),
            pa.field("period_start", pa.date32(), nullable=True),
            pa.field("period_end", pa.date32(), nullable=False),
            pa.field("fiscal_year", pa.int32(), nullable=True),
            pa.field("fiscal_period", pa.string(), nullable=True),
            pa.field("frame", pa.string(), nullable=True),
            pa.field("taxonomy", pa.string(), nullable=True),
        ],
        metadata={
            b"omd.dataset_schema": BULK_COMPANYFACTS_DATASET_SCHEMA.encode(),
            b"omd.writer_profile": BULK_COMPANYFACTS_WRITER_PROFILE.encode(),
        },
    )


def _dec_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _sha256_bytes(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def write_bulk_companyfacts_dataset(
    output_dir: str | Path,
    projection: SecBulkProjectionResult,
) -> dict[str, Any]:
    """Atomically write CIK-keyed immutable Parquet dataset + manifest.json."""
    pa = _pa()
    pq: Any = importlib.import_module("pyarrow.parquet")
    final_dest = Path(output_dir)
    final_dest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".bulk-companyfacts-", dir=final_dest.parent) as stage:
        dest = Path(stage)
        fund_records = [
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
                "reporting_currency": r.reporting_currency,
                "revenue": _dec_str(r.revenue),
                "gross_profit": _dec_str(r.gross_profit),
                "operating_income": _dec_str(r.operating_income),
                "net_income": _dec_str(r.net_income),
                "ocf": _dec_str(r.ocf),
                "capex": _dec_str(r.capex),
                "total_assets": _dec_str(r.total_assets),
                "total_debt": _dec_str(r.total_debt),
                "diluted_shares": _dec_str(r.diluted_shares),
                "eps_diluted": _dec_str(r.eps_diluted),
                "dei_shares_outstanding": _dec_str(r.dei_shares_outstanding),
                "dei_shares_outstanding_end": r.dei_shares_outstanding_end,
                "source_tags_json": r.source_tags_json,
            }
            for r in projection.fundamental_rows
        ]
        fact_records = [
            {
                "cik": f.cik,
                "entity_name": f.entity_name,
                "symbols": list(f.symbols),
                "accession_number": f.accession_number,
                "form": f.form,
                "is_amendment": f.is_amendment,
                "filing_date": f.filing_date,
                "report_date": f.report_date,
                "accepted_at": f.accepted_at,
                "namespace": f.namespace,
                "tag": f.tag,
                "unit": f.unit,
                "currency": f.currency,
                "value": str(f.value),
                "period_start": f.period_start,
                "period_end": f.period_end,
                "fiscal_year": f.fiscal_year,
                "fiscal_period": f.fiscal_period,
                "frame": f.frame,
                "taxonomy": f.taxonomy,
            }
            for f in projection.joined_facts
        ]

        f_path = dest / "fundamentals.parquet"
        f_tmp = f_path.with_suffix(".tmp")
        pq.write_table(
            pa.Table.from_pylist(fund_records, schema=_fundamentals_schema(pa)),
            f_tmp,
            compression="zstd",
        )
        f_tmp.replace(f_path)

        j_path = dest / "facts.parquet"
        j_tmp = j_path.with_suffix(".tmp")
        pq.write_table(
            pa.Table.from_pylist(fact_records, schema=_facts_schema(pa)),
            j_tmp,
            compression="zstd",
        )
        j_tmp.replace(j_path)

        cik_outcomes = {
            item.cik: {
                "status": item.status.value,
                "entity_name": item.entity_name,
                "symbols": list(item.symbols),
                "taxonomy": item.taxonomy,
                "reporting_currency": item.reporting_currency,
                "filing_count": item.filing_count,
                "history_pages_read": item.history_pages_read,
                "fact_count": item.fact_count,
                "fundamental_row_count": item.fundamental_row_count,
                "error_type": item.error_type,
                "error_message": item.error_message,
            }
            for item in projection.outcomes
        }
        status_counts = {status.value.lower(): 0 for status in SecBulkCikOutcomeStatus}
        for item in projection.outcomes:
            status_counts[item.status.value.lower()] += 1
        manifest: dict[str, Any] = {
            "dataset_schema": BULK_COMPANYFACTS_DATASET_SCHEMA,
            "writer_profile": BULK_COMPANYFACTS_WRITER_PROFILE,
            "concept_mapping_version": SEC_MULTI_TAXONOMY_CONCEPTS_VERSION,
            "source_archives": {
                "companyfacts_zip": {
                    "filename": projection.companyfacts_archive.filename,
                    "sha256": projection.companyfacts_archive.sha256,
                    "bytes": projection.companyfacts_archive.bytes,
                },
                "submissions_zip": {
                    "filename": projection.submissions_archive.filename,
                    "sha256": projection.submissions_archive.sha256,
                    "bytes": projection.submissions_archive.bytes,
                },
            },
            "filters": projection.filters,
            "counts": {
                "total_ciks": len(projection.outcomes),
                **status_counts,
                "fundamental_row_count": len(fund_records),
                "fact_row_count": len(fact_records),
            },
            "cik_outcomes": cik_outcomes,
            "files": {
                "fundamentals.parquet": {
                    "sha256": _sha256_bytes(f_path),
                    "bytes": f_path.stat().st_size,
                    "rows": len(fund_records),
                },
                "facts.parquet": {
                    "sha256": _sha256_bytes(j_path),
                    "bytes": j_path.stat().st_size,
                    "rows": len(fact_records),
                },
            },
        }
        m_path = dest / "manifest.json"
        m_tmp = m_path.with_suffix(".tmp")
        m_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        m_tmp.replace(m_path)

        if final_dest.exists():
            existing = validate_bulk_companyfacts_dataset(final_dest)
            if existing == manifest:
                return existing
            raise ValueError(
                "immutable bulk companyfacts dataset differs; write to a new output directory"
            )
        dest.rename(final_dest)
    return manifest


def _validate_manifest_sections(
    manifest: dict[str, Any],
) -> tuple[set[str], datetime | None, dict[str, Any]]:
    source_archives = manifest.get("source_archives")
    if type(source_archives) is not dict or set(source_archives) != {
        "companyfacts_zip",
        "submissions_zip",
    }:
        raise ValueError("bulk companyfacts manifest source_archives missing or invalid")
    for archive_key in ("companyfacts_zip", "submissions_zip"):
        entry = source_archives[archive_key]
        if (
            type(entry) is not dict
            or type(entry.get("filename")) is not str
            or not entry["filename"].strip()
            or type(entry.get("sha256")) is not str
            or _SHA256_RE.fullmatch(entry["sha256"]) is None
            or type(entry.get("bytes")) is not int
            or entry["bytes"] < 0
        ):
            raise ValueError(f"bulk companyfacts manifest source archive invalid: {archive_key}")

    filters = manifest.get("filters")
    if type(filters) is not dict:
        raise ValueError("bulk companyfacts manifest filters must be a mapping")
    raw_upper = filters.get("acceptance_upper")
    upper_bound: datetime | None = None
    if raw_upper is not None:
        if type(raw_upper) is not str:
            raise ValueError("bulk companyfacts manifest acceptance_upper must be an ISO string")
        try:
            parsed_upper = datetime.fromisoformat(raw_upper)
        except ValueError as exc:
            raise ValueError("bulk companyfacts manifest acceptance_upper is invalid") from exc
        if parsed_upper.tzinfo is None or parsed_upper.utcoffset() is None:
            raise ValueError("bulk companyfacts manifest acceptance_upper must be timezone-aware")
        upper_bound = parsed_upper.astimezone(UTC)

    outcomes = manifest.get("cik_outcomes")
    if type(outcomes) is not dict:
        raise ValueError("bulk companyfacts manifest cik_outcomes missing")
    counts = manifest.get("counts")
    if type(counts) is not dict:
        raise ValueError("bulk companyfacts manifest counts missing")
    if type(counts.get("total_ciks")) is not int or counts["total_ciks"] != len(outcomes):
        raise ValueError("bulk companyfacts manifest total_ciks mismatch")

    valid_statuses = {status.value for status in SecBulkCikOutcomeStatus}
    expected_counts = {status.value.lower(): 0 for status in SecBulkCikOutcomeStatus}
    ok_ciks: set[str] = set()
    for cik, info in outcomes.items():
        if type(cik) is not str or type(info) is not dict:
            raise ValueError("bulk companyfacts manifest cik_outcomes entry invalid")
        status = info.get("status")
        if type(status) is not str or status not in valid_statuses:
            raise ValueError(f"bulk companyfacts manifest invalid CIK status: {status!r}")
        expected_counts[status.lower()] += 1
        if status == SecBulkCikOutcomeStatus.OK.value:
            ok_ciks.add(cik)
    for status_key, expected_val in expected_counts.items():
        if type(counts.get(status_key)) is not int or counts[status_key] != expected_val:
            raise ValueError(f"bulk companyfacts manifest count mismatch for {status_key}")
    return ok_ciks, upper_bound, counts


def validate_bulk_companyfacts_dataset(dataset_dir: str | Path) -> dict[str, Any]:
    """Validate schema, SHA-256 hashes, row counts, CIK outcomes, and PIT cutoffs."""
    dest = Path(dataset_dir)
    manifest_path = dest / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("bulk companyfacts manifest.json missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if type(manifest) is not dict:
        raise ValueError("bulk companyfacts manifest.json must be an object")
    if (
        manifest.get("dataset_schema") != BULK_COMPANYFACTS_DATASET_SCHEMA
        or manifest.get("writer_profile") != BULK_COMPANYFACTS_WRITER_PROFILE
    ):
        raise ValueError("incompatible bulk companyfacts dataset schema or writer profile")
    files_meta = manifest.get("files")
    if type(files_meta) is not dict or set(files_meta) != {
        "fundamentals.parquet",
        "facts.parquet",
    }:
        raise ValueError("bulk companyfacts manifest must list both required parquet files")
    ok_ciks, upper_bound, counts = _validate_manifest_sections(manifest)
    pa = _pa()
    pq: Any = importlib.import_module("pyarrow.parquet")

    for filename, expected_schema, count_key in (
        ("fundamentals.parquet", _fundamentals_schema(pa), "fundamental_row_count"),
        ("facts.parquet", _facts_schema(pa), "fact_row_count"),
    ):
        path = dest / filename
        if not path.is_file():
            raise ValueError(f"required bulk companyfacts table missing: {filename}")
        meta = files_meta[filename]
        if path.stat().st_size != meta.get("bytes") or _sha256_bytes(path) != meta.get("sha256"):
            raise ValueError(f"bulk companyfacts hash or size mismatch for {filename}")
        pf = pq.ParquetFile(path)
        if not pf.schema_arrow.equals(expected_schema, check_metadata=True):
            raise ValueError(f"incompatible parquet schema for {filename}")
        if (
            pf.metadata.num_rows != meta.get("rows")
            or type(counts.get(count_key)) is not int
            or pf.metadata.num_rows != counts[count_key]
        ):
            raise ValueError(f"row count mismatch for {filename}")
        table = pf.read(columns=["cik", "accepted_at"])
        for cik_val in table.column("cik").to_pylist():
            if cik_val not in ok_ciks:
                raise ValueError(f"table {filename} references non-OK CIK {cik_val!r}")
        if upper_bound is not None:
            for accepted in table.column("accepted_at").to_pylist():
                if accepted is None or accepted > upper_bound:
                    raise ValueError(f"table {filename} violates acceptance_upper PIT bound")
    return manifest


__all__ = [
    "BULK_COMPANYFACTS_DATASET_SCHEMA",
    "BULK_COMPANYFACTS_WRITER_PROFILE",
    "validate_bulk_companyfacts_dataset",
    "write_bulk_companyfacts_dataset",
]

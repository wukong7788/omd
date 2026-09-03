"""PyArrow schemas and atomic publication for SEC N-PORT structural qualification."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from .batch import canonical_hash

QUALIFICATION_DATASET_SCHEMA = "sec-nport-qualification-v1"
QUALIFICATION_RECEIPT_SCHEMA = "sec-nport-qualification-receipt-v1"

COVERAGE_COLUMNS = (
    "source_quarter",
    "fund_symbol",
    "cik",
    "series_id",
    "selection_mode",
    "expected",
    "vintage_count",
    "original_count",
    "amendment_count",
    "earliest_report_date",
    "latest_report_date",
    "earliest_availability_anchor",
    "latest_availability_anchor",
    "unknown_availability_count",
    "partition_identity",
)

VINTAGE_QUALITY_COLUMNS = (
    "source_quarter",
    "fund_symbol",
    "cik",
    "series_id",
    "accession_number",
    "report_date",
    "filing_date",
    "accepted_at",
    "observed_at",
    "availability_anchor",
    "availability_basis",
    "availability_precision",
    "availability_policy",
    "availability_lag_days",
    "submission_type",
    "vintage_identity",
    "holding_row_count",
    "unique_holding_id_count",
    "nonnull_percentage_count",
    "null_percentage_count",
    "zero_percentage_count",
    "positive_percentage_count",
    "negative_percentage_count",
    "percentage_sum",
    "derivative_rows_count",
    "derivative_weight",
    "restricted_rows_count",
    "restricted_weight",
    "debt_like_rows_count",
    "debt_like_weight",
    "cash_like_rows_count",
    "cash_like_weight",
    "unknown_cat_rows_count",
    "unknown_cat_weight",
    "missing_currency_code_count",
    "missing_exchange_rate_count",
    "missing_balance_count",
    "missing_currency_value_count",
    "total_assets",
    "total_liabilities",
    "net_assets",
    "quality_flags",
)

AMENDMENT_FACTS_COLUMNS = (
    "cik",
    "series_id",
    "report_date",
    "accession_number",
    "submission_type",
    "vintage_identity",
    "family_size",
    "family_order",
    "predecessor_accession_number",
    "relation_basis",
    "availability_anchor",
    "payload_hash",
)

IDENTIFIER_QUALITY_COLUMNS = (
    "source_quarter",
    "fund_symbol",
    "cik",
    "series_id",
    "accession_number",
    "vintage_identity",
    "total_holding_count",
    "zero_identifier_holding_count",
    "single_identifier_holding_count",
    "multi_identifier_holding_count",
    "any_identifier_holding_count",
    "any_identifier_percentage_sum",
    "cusip_present_count",
    "isin_present_count",
    "ticker_present_count",
    "other_present_count",
    "duplicate_identifier_value_count",
    "multi_value_same_type_holding_count",
    "multi_issuer_name_identifier_count",
    "null_or_empty_identifier_count",
)


def _pa() -> Any:
    try:
        return importlib.import_module("pyarrow")
    except ImportError as exc:
        raise RuntimeError("install the optional 'sec-cli' extra to run qualification") from exc


def get_coverage_schema(pa: Any) -> Any:
    return pa.schema(
        [
            pa.field("source_quarter", pa.string(), nullable=False),
            pa.field("fund_symbol", pa.string(), nullable=False),
            pa.field("cik", pa.string(), nullable=False),
            pa.field("series_id", pa.string(), nullable=True),
            pa.field("selection_mode", pa.string(), nullable=False),
            pa.field("expected", pa.bool_(), nullable=False),
            pa.field("vintage_count", pa.int64(), nullable=False),
            pa.field("original_count", pa.int64(), nullable=False),
            pa.field("amendment_count", pa.int64(), nullable=False),
            pa.field("earliest_report_date", pa.date32(), nullable=True),
            pa.field("latest_report_date", pa.date32(), nullable=True),
            pa.field("earliest_availability_anchor", pa.timestamp("us", tz="UTC"), nullable=True),
            pa.field("latest_availability_anchor", pa.timestamp("us", tz="UTC"), nullable=True),
            pa.field("unknown_availability_count", pa.int64(), nullable=False),
            pa.field("partition_identity", pa.string(), nullable=False),
        ]
    )


def get_vintage_quality_schema(pa: Any) -> Any:
    return pa.schema(
        [
            pa.field("source_quarter", pa.string(), nullable=False),
            pa.field("fund_symbol", pa.string(), nullable=False),
            pa.field("cik", pa.string(), nullable=False),
            pa.field("series_id", pa.string(), nullable=True),
            pa.field("accession_number", pa.string(), nullable=False),
            pa.field("report_date", pa.date32(), nullable=False),
            pa.field("filing_date", pa.date32(), nullable=False),
            pa.field("accepted_at", pa.timestamp("us", tz="UTC"), nullable=True),
            pa.field("observed_at", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("availability_anchor", pa.timestamp("us", tz="UTC"), nullable=True),
            pa.field("availability_basis", pa.string(), nullable=False),
            pa.field("availability_precision", pa.string(), nullable=False),
            pa.field("availability_policy", pa.string(), nullable=False),
            pa.field("availability_lag_days", pa.int16(), nullable=False),
            pa.field("submission_type", pa.string(), nullable=False),
            pa.field("vintage_identity", pa.string(), nullable=False),
            pa.field("holding_row_count", pa.int64(), nullable=False),
            pa.field("unique_holding_id_count", pa.int64(), nullable=False),
            pa.field("nonnull_percentage_count", pa.int64(), nullable=False),
            pa.field("null_percentage_count", pa.int64(), nullable=False),
            pa.field("zero_percentage_count", pa.int64(), nullable=False),
            pa.field("positive_percentage_count", pa.int64(), nullable=False),
            pa.field("negative_percentage_count", pa.int64(), nullable=False),
            pa.field("percentage_sum", pa.decimal128(38, 18), nullable=False),
            pa.field("derivative_rows_count", pa.int64(), nullable=False),
            pa.field("derivative_weight", pa.decimal128(38, 18), nullable=False),
            pa.field("restricted_rows_count", pa.int64(), nullable=False),
            pa.field("restricted_weight", pa.decimal128(38, 18), nullable=False),
            pa.field("debt_like_rows_count", pa.int64(), nullable=False),
            pa.field("debt_like_weight", pa.decimal128(38, 18), nullable=False),
            pa.field("cash_like_rows_count", pa.int64(), nullable=False),
            pa.field("cash_like_weight", pa.decimal128(38, 18), nullable=False),
            pa.field("unknown_cat_rows_count", pa.int64(), nullable=False),
            pa.field("unknown_cat_weight", pa.decimal128(38, 18), nullable=False),
            pa.field("missing_currency_code_count", pa.int64(), nullable=False),
            pa.field("missing_exchange_rate_count", pa.int64(), nullable=False),
            pa.field("missing_balance_count", pa.int64(), nullable=False),
            pa.field("missing_currency_value_count", pa.int64(), nullable=False),
            pa.field("total_assets", pa.decimal128(38, 18), nullable=True),
            pa.field("total_liabilities", pa.decimal128(38, 18), nullable=True),
            pa.field("net_assets", pa.decimal128(38, 18), nullable=True),
            pa.field("quality_flags", pa.list_(pa.field("element", pa.string())), nullable=False),
        ]
    )


def get_amendment_facts_schema(pa: Any) -> Any:
    return pa.schema(
        [
            pa.field("cik", pa.string(), nullable=False),
            pa.field("series_id", pa.string(), nullable=True),
            pa.field("report_date", pa.date32(), nullable=False),
            pa.field("accession_number", pa.string(), nullable=False),
            pa.field("submission_type", pa.string(), nullable=False),
            pa.field("vintage_identity", pa.string(), nullable=False),
            pa.field("family_size", pa.int64(), nullable=False),
            pa.field("family_order", pa.int64(), nullable=False),
            pa.field("predecessor_accession_number", pa.string(), nullable=True),
            pa.field("relation_basis", pa.string(), nullable=False),
            pa.field("availability_anchor", pa.timestamp("us", tz="UTC"), nullable=True),
            pa.field("payload_hash", pa.string(), nullable=False),
        ]
    )


def get_identifier_quality_schema(pa: Any) -> Any:
    return pa.schema(
        [
            pa.field("source_quarter", pa.string(), nullable=False),
            pa.field("fund_symbol", pa.string(), nullable=False),
            pa.field("cik", pa.string(), nullable=False),
            pa.field("series_id", pa.string(), nullable=True),
            pa.field("accession_number", pa.string(), nullable=False),
            pa.field("vintage_identity", pa.string(), nullable=False),
            pa.field("total_holding_count", pa.int64(), nullable=False),
            pa.field("zero_identifier_holding_count", pa.int64(), nullable=False),
            pa.field("single_identifier_holding_count", pa.int64(), nullable=False),
            pa.field("multi_identifier_holding_count", pa.int64(), nullable=False),
            pa.field("any_identifier_holding_count", pa.int64(), nullable=False),
            pa.field("any_identifier_percentage_sum", pa.decimal128(38, 18), nullable=False),
            pa.field("cusip_present_count", pa.int64(), nullable=False),
            pa.field("isin_present_count", pa.int64(), nullable=False),
            pa.field("ticker_present_count", pa.int64(), nullable=False),
            pa.field("other_present_count", pa.int64(), nullable=False),
            pa.field("duplicate_identifier_value_count", pa.int64(), nullable=False),
            pa.field("multi_value_same_type_holding_count", pa.int64(), nullable=False),
            pa.field("multi_issuer_name_identifier_count", pa.int64(), nullable=False),
            pa.field("null_or_empty_identifier_count", pa.int64(), nullable=False),
        ]
    )


def schema_fingerprint(schema: Any) -> str:
    descriptors = [{"name": f.name, "type": str(f.type), "nullable": f.nullable} for f in schema]
    return canonical_hash(descriptors)


def logical_table_hash(table_name: str, rows: list[dict[str, Any]]) -> str:
    return canonical_hash({"table": table_name, "rows": rows})


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def write_candidate_qualification_tables(
    *,
    stage_dir: Path,
    request_data: dict[str, Any],
    coverage_rows: list[dict[str, Any]],
    vintage_quality_rows: list[dict[str, Any]],
    amendment_facts_rows: list[dict[str, Any]],
    identifier_quality_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Writes the 4 candidate fact tables and qualification_request.json to staging."""
    pa = _pa()
    pq: Any = importlib.import_module("pyarrow.parquet")

    # 1. Deterministic in-memory sorting
    sorted_coverage = sorted(
        coverage_rows,
        key=lambda r: (str(r.get("source_quarter")), str(r.get("fund_symbol"))),
    )
    sorted_vintage = sorted(
        vintage_quality_rows,
        key=lambda r: (
            str(r.get("source_quarter")),
            str(r.get("fund_symbol")),
            str(r.get("report_date")),
            str(r.get("accession_number")),
            str(r.get("vintage_identity")),
        ),
    )
    sorted_amendment = sorted(
        amendment_facts_rows,
        key=lambda r: (
            str(r.get("cik")),
            str(r.get("series_id") or ""),
            str(r.get("report_date")),
            int(r.get("family_order", 0)),
            str(r.get("accession_number")),
        ),
    )
    sorted_identifier = sorted(
        identifier_quality_rows,
        key=lambda r: (
            str(r.get("source_quarter")),
            str(r.get("fund_symbol")),
            str(r.get("accession_number")),
            str(r.get("vintage_identity")),
        ),
    )

    tables_to_write = (
        (
            "quarter_fund_coverage.parquet",
            sorted_coverage,
            get_coverage_schema(pa),
            "quarter_fund_coverage",
        ),
        (
            "vintage_quality.parquet",
            sorted_vintage,
            get_vintage_quality_schema(pa),
            "vintage_quality",
        ),
        (
            "amendment_facts.parquet",
            sorted_amendment,
            get_amendment_facts_schema(pa),
            "amendment_facts",
        ),
        (
            "identifier_quality.parquet",
            sorted_identifier,
            get_identifier_quality_schema(pa),
            "identifier_quality",
        ),
    )

    output_artifacts: dict[str, Any] = {}
    schemas_desc: dict[str, Any] = {}

    for file_name, rows, schema, table_key in tables_to_write:
        file_path = stage_dir / file_name
        arrow_table = pa.Table.from_pylist(rows, schema=schema)
        pq.write_table(arrow_table, file_path, compression="zstd")

        # fsync file
        fd = os.open(file_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

        size_bytes = file_path.stat().st_size
        file_hash = _sha256_file(file_path)
        fp = schema_fingerprint(schema)
        log_hash = logical_table_hash(table_key, arrow_table.to_pylist())

        output_artifacts[file_name] = {
            "path": file_name,
            "bytes": size_bytes,
            "sha256": file_hash,
            "row_count": len(rows),
            "schema_fingerprint": fp,
            "logical_hash": log_hash,
        }
        schemas_desc[table_key] = {
            "fingerprint": fp,
            "fields": [
                {"name": f.name, "type": str(f.type), "nullable": f.nullable} for f in schema
            ],
        }

    # Write qualification_request.json
    req_path = stage_dir / "qualification_request.json"
    req_content = json.dumps(request_data, indent=2, sort_keys=True)
    req_path.write_text(req_content, encoding="utf-8")
    fd = os.open(req_path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

    output_artifacts["qualification_request.json"] = {
        "path": "qualification_request.json",
        "bytes": req_path.stat().st_size,
        "sha256": _sha256_file(req_path),
        "row_count": 1,
        "schema_fingerprint": canonical_hash(list(request_data.keys())),
        "logical_hash": canonical_hash(request_data),
    }

    _fsync_dir(stage_dir)
    return output_artifacts, schemas_desc


def publish_qualification_receipt_and_rename(
    *,
    stage_dir: Path,
    output_dir: Path,
    receipt_data: dict[str, Any],
) -> str:
    """Writes receipt in staging, fsyncs, and atomically renames to output_dir."""
    from ...core.errors import SnapshotIntegrityError

    parent = output_dir.parent
    rcp_path = stage_dir / "qualification_receipt.json"
    rcp_content = json.dumps(receipt_data, indent=2, sort_keys=True)
    rcp_path.write_text(rcp_content, encoding="utf-8")
    fd = os.open(rcp_path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

    _fsync_dir(stage_dir)

    try:
        stage_dir.replace(output_dir)
    except OSError as exc:
        raise SnapshotIntegrityError(f"atomic publication failed: {exc}") from exc

    _fsync_dir(parent)
    return canonical_hash(receipt_data)


def write_qualification_bundle(
    *,
    output_dir: Path,
    request_data: dict[str, Any],
    coverage_rows: list[dict[str, Any]],
    vintage_quality_rows: list[dict[str, Any]],
    amendment_facts_rows: list[dict[str, Any]],
    identifier_quality_rows: list[dict[str, Any]],
    receipt_data: dict[str, Any],
) -> dict[str, Any]:
    """Deterministically and atomically writes the complete qualification bundle."""
    from ...core.errors import SnapshotIntegrityError

    resolved_output = output_dir.resolve()
    if resolved_output.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    if output_dir.is_symlink():
        raise SnapshotIntegrityError(f"output path is a symlink: {output_dir}")

    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)

    stage_dir = parent / f".tmp-qualify-{uuid.uuid4().hex}"
    stage_dir.mkdir(parents=True, exist_ok=False)

    try:
        artifacts, schemas = write_candidate_qualification_tables(
            stage_dir=stage_dir,
            request_data=request_data,
            coverage_rows=coverage_rows,
            vintage_quality_rows=vintage_quality_rows,
            amendment_facts_rows=amendment_facts_rows,
            identifier_quality_rows=identifier_quality_rows,
        )
        receipt_data["output_artifacts"] = artifacts
        receipt_data["schemas"] = schemas

        publish_qualification_receipt_and_rename(
            stage_dir=stage_dir,
            output_dir=output_dir,
            receipt_data=receipt_data,
        )
        return receipt_data
    except Exception:
        if stage_dir.exists():
            import shutil

            shutil.rmtree(stage_dir, ignore_errors=True)
        raise

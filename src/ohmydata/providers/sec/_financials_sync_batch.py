"""Built-in atomic immutable incremental batch writer and validator for SEC financials."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from ._financials_sync_state import normalize_cik
from .batch import canonical_json
from .financials import (
    FINANCIALS_DATASET_SCHEMA,
    FINANCIALS_DATASET_SCHEMA_V4,
    SecCompanyFinancialVintage,
)


def _parquet_helpers() -> tuple[Any, Any, Any]:
    """Resolve pyarrow + table schema helpers across both pre- and post-Phase-2 layouts."""
    try:
        mod: Any = importlib.import_module("._financials_dataset_io", package=__package__)
    except ImportError:
        mod = importlib.import_module(".financials_dataset", package=__package__)
    if hasattr(mod, "load_pyarrow") and hasattr(mod, "vintage_schema"):
        return (
            mod.load_pyarrow,
            lambda pa, *, v4=False: mod.vintage_schema(pa, version="v4" if v4 else "v3"),
            lambda pa, *, v4=False: mod.statement_schema(pa, version="v4" if v4 else "v3"),
        )
    return mod._pa, mod._vintage_schema, mod._statement_schema


_SAFE_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BATCH_WRITER_PROFILE_V3 = "sec-financials-parquet-v3"
_BATCH_WRITER_PROFILE_V4 = "sec-financials-parquet-v4"


class SecIncrementalBatchWriter(Protocol):
    """Protocol matching ``write_financials_incremental_batch``."""

    def __call__(
        self,
        root: str | Path,
        batch_id: str,
        vintages: Iterable[SecCompanyFinancialVintage],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


def validate_batch_id(batch_id: str) -> str:
    """Validate a safe single-segment batch identifier."""
    if (
        not isinstance(batch_id, str)
        or _SAFE_BATCH_ID_RE.fullmatch(batch_id) is None
        or batch_id in {".", ".."}
    ):
        raise ValueError(f"invalid batch_id: {batch_id!r}")
    return batch_id


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise TypeError("batch metadata must be a mapping")
    return json.loads(canonical_json(dict(metadata)).decode("utf-8"))


def default_write_financials_incremental_batch(
    root: str | Path,
    batch_id: str,
    vintages: Iterable[SecCompanyFinancialVintage],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write an atomic immutable incremental batch under ``<root>/batches/<batch_id>/``.

    Contract-compatible with ``financials_dataset.write_financials_incremental_batch``:
    - Never overwrites an existing differing batch (idempotent on identical vintages+metadata).
    - Supports single- or multi-CIK vintages and empty symbols (delisted companies).
    - Persists Parquet tables plus SHA-256 verified ``manifest.json`` atomically via rename.
    """
    safe_id = validate_batch_id(batch_id)
    meta_clean = _normalize_metadata(metadata)
    pa_fn, vintage_schema_fn, statement_schema_fn = _parquet_helpers()
    pa = pa_fn()
    pq: Any = importlib.import_module("pyarrow.parquet")

    sorted_vintages = sorted(
        vintages,
        key=lambda v: (
            normalize_cik(v.cik),
            v.filing_date,
            v.accepted_at or datetime.min.replace(tzinfo=UTC),
            v.accession_number,
        ),
    )
    if not sorted_vintages:
        raise ValueError("incremental batch requires at least one vintage")

    is_v4 = any(v.unit_evidence is not None for v in sorted_vintages)
    dataset_schema = FINANCIALS_DATASET_SCHEMA_V4 if is_v4 else FINANCIALS_DATASET_SCHEMA
    writer_profile = _BATCH_WRITER_PROFILE_V4 if is_v4 else _BATCH_WRITER_PROFILE_V3

    batches_root = Path(root) / "batches"
    final_dest = batches_root / safe_id
    if final_dest.exists():
        existing = validate_default_financials_incremental_batch(final_dest)
        table = pq.ParquetFile(final_dest / "company_financial_vintages.parquet").read()
        if (
            existing.get("dataset_schema") == dataset_schema
            and existing.get("writer_profile") == writer_profile
            and existing.get("metadata", {}) == meta_clean
            and table.column("vintage_identity").to_pylist()
            == [v.vintage_identity for v in sorted_vintages]
        ):
            return existing
        raise ValueError(f"immutable financials batch collision for batch_id={safe_id!r}")

    batches_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".batch-{safe_id}-", dir=batches_root) as stage:
        dest = Path(stage)
        vintage_rows: list[dict[str, Any]] = []
        statement_rows: list[dict[str, Any]] = []
        ciks_set: set[str] = set()
        symbols_set: set[str] = set()
        accessions_set: set[str] = set()

        for v in sorted_vintages:
            norm_cik = normalize_cik(v.cik)
            ciks_set.add(norm_cik)
            if v.symbol:
                symbols_set.add(v.symbol)
            accessions_set.add(v.accession_number)

            vintage_row: dict[str, Any] = {
                "symbol": v.symbol,
                "cik": norm_cik,
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

            ordered_rows = (
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
            for r in ordered_rows:
                statement_row: dict[str, Any] = {
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

        v_schema = vintage_schema_fn(pa, v4=is_v4)
        v_table = pa.Table.from_pylist(vintage_rows, schema=v_schema)
        v_path = dest / "company_financial_vintages.parquet"
        v_tmp = v_path.with_suffix(".tmp")
        pq.write_table(v_table, v_tmp, compression="zstd")
        v_tmp.replace(v_path)

        s_schema = statement_schema_fn(pa, v4=is_v4)
        s_table = pa.Table.from_pylist(statement_rows, schema=s_schema)
        s_path = dest / "financial_statements.parquet"
        s_tmp = s_path.with_suffix(".tmp")
        pq.write_table(s_table, s_tmp, compression="zstd")
        s_tmp.replace(s_path)

        manifest: dict[str, Any] = {
            "partition_key": "batch",
            "batch_id": safe_id,
            "ciks": sorted(ciks_set),
            "symbols": sorted(symbols_set),
            "accession_numbers": sorted(accessions_set),
            "vintage_count": len(vintage_rows),
            "statement_row_count": len(statement_rows),
            "files": {
                "company_financial_vintages.parquet": {
                    "sha256": _sha256_file(v_path),
                    "bytes": v_path.stat().st_size,
                },
                "financial_statements.parquet": {
                    "sha256": _sha256_file(s_path),
                    "bytes": s_path.stat().st_size,
                },
            },
            "dataset_schema": dataset_schema,
            "writer_profile": writer_profile,
            "metadata": meta_clean,
        }
        m_path = dest / "manifest.json"
        m_tmp = m_path.with_suffix(".tmp")
        m_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        m_tmp.replace(m_path)

        dest.rename(final_dest)

    return manifest


def validate_default_financials_incremental_batch(batch_dir: str | Path) -> dict[str, Any]:
    """Validate an immutable incremental batch directory (schema, hashes, counts, identity)."""
    dest = Path(batch_dir)
    if not dest.is_dir():
        raise ValueError(f"batch directory does not exist: {dest}")
    manifest_path = dest / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"batch manifest missing: {manifest_path}")
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("partition_key") != "batch" or manifest.get("batch_id") != dest.name:
        raise ValueError("batch manifest identity does not match directory name")

    is_v4 = (
        manifest.get("dataset_schema") == FINANCIALS_DATASET_SCHEMA_V4
        and manifest.get("writer_profile") == _BATCH_WRITER_PROFILE_V4
    )
    is_v3 = (
        manifest.get("dataset_schema") == FINANCIALS_DATASET_SCHEMA
        and manifest.get("writer_profile") == _BATCH_WRITER_PROFILE_V3
    )
    if not (is_v3 or is_v4):
        raise ValueError("incompatible incremental batch dataset schema or writer profile")

    expected_files = {"company_financial_vintages.parquet", "financial_statements.parquet"}
    if set(manifest.get("files", {})) != expected_files:
        raise ValueError("incremental batch manifest must list both required parquet files")

    pa_fn, vintage_schema_fn, statement_schema_fn = _parquet_helpers()
    pa = pa_fn()
    pq: Any = importlib.import_module("pyarrow.parquet")
    for filename, schema in (
        ("company_financial_vintages.parquet", vintage_schema_fn(pa, v4=is_v4)),
        ("financial_statements.parquet", statement_schema_fn(pa, v4=is_v4)),
    ):
        path = dest / filename
        if not path.is_file():
            raise ValueError(f"required batch file missing: {filename}")
        meta = manifest["files"][filename]
        if path.stat().st_size != meta.get("bytes") or _sha256_file(path) != meta.get("sha256"):
            raise ValueError(f"batch file integrity mismatch: {filename}")
        pf = pq.ParquetFile(path)
        if not pf.schema_arrow.equals(schema, check_metadata=True):
            raise ValueError(f"batch table schema mismatch: {filename}")
        count_key = (
            "vintage_count"
            if filename == "company_financial_vintages.parquet"
            else "statement_row_count"
        )
        if type(manifest.get(count_key)) is not int or manifest[count_key] != pf.metadata.num_rows:
            raise ValueError(f"batch manifest {count_key} mismatch")

    v_table = pq.ParquetFile(dest / "company_financial_vintages.parquet").read()
    observed_ciks = sorted({normalize_cik(c) for c in v_table.column("cik").to_pylist()})
    observed_symbols = sorted({s for s in v_table.column("symbol").to_pylist() if s})
    observed_accessions = sorted(set(v_table.column("accession_number").to_pylist()))
    if manifest.get("ciks") != observed_ciks:
        raise ValueError("batch manifest ciks do not match parquet contents")
    if manifest.get("symbols") != observed_symbols:
        raise ValueError("batch manifest symbols do not match parquet contents")
    if manifest.get("accession_numbers") != observed_accessions:
        raise ValueError("batch manifest accession_numbers do not match parquet contents")

    return manifest


def resolve_default_batch_hooks() -> tuple[
    SecIncrementalBatchWriter, Callable[[str | Path], Mapping[str, Any]]
]:
    """Use ``financials_dataset.write_financials_incremental_batch`` when present, else fallback."""
    from . import financials_dataset

    writer = getattr(financials_dataset, "write_financials_incremental_batch", None)
    validator = getattr(financials_dataset, "validate_financials_incremental_batch", None)
    if callable(writer) and callable(validator):
        return writer, validator
    return default_write_financials_incremental_batch, validate_default_financials_incremental_batch


__all__ = [
    "SecIncrementalBatchWriter",
    "default_write_financials_incremental_batch",
    "resolve_default_batch_hooks",
    "validate_batch_id",
    "validate_default_financials_incremental_batch",
]

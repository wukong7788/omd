"""Deterministic Parquet writer for SEC company financial vintages, CIK partitions, and batches."""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ._financials_dataset_io import (
    WRITER_PROFILE,
    WRITER_PROFILE_V4,
    WRITER_PROFILE_V5,
    SchemaVersion,
    detect_schema_version,
    load_parquet,
    load_pyarrow,
    serialize_tables,
    statement_schema,
    validate_and_reconstruct_linked_vintages,
    validate_manifest_files_and_schemas,
    vintage_schema,
    vintage_sort_key,
    write_parquet_tables_and_manifest,
)
from .financials import (
    FINANCIALS_DATASET_SCHEMA,
    FINANCIALS_DATASET_SCHEMA_V4,
    FINANCIALS_DATASET_SCHEMA_V5,
    SecCompanyFinancialVintage,
    normalize_sec_cik,
)

DATASET_SCHEMA = FINANCIALS_DATASET_SCHEMA
_SAFE_BATCH_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_pa = load_pyarrow


def _vintage_schema(pa: Any, *, v4: bool = False) -> Any:
    return vintage_schema(pa, version="v4" if v4 else "v3")


def _statement_schema(pa: Any, *, v4: bool = False) -> Any:
    return statement_schema(pa, version="v4" if v4 else "v3")


def _require_unique_vintage_identities(vintages: Iterable[SecCompanyFinancialVintage]) -> None:
    seen: set[str] = set()
    for vintage in vintages:
        identity = vintage.vintage_identity
        if identity in seen:
            raise ValueError(f"duplicate vintage_identity in financials write: {identity}")
        seen.add(identity)


def _validate_batch_id(batch_id: str) -> str:
    if (
        not isinstance(batch_id, str)
        or batch_id in {".", ".."}
        or not _SAFE_BATCH_ID_RE.fullmatch(batch_id)
    ):
        raise ValueError(f"unsafe incremental batch_id: {batch_id!r}")
    return batch_id


def _canonical_symbols(
    vintages: Iterable[SecCompanyFinancialVintage],
    extra_symbols: Iterable[str] | None = None,
) -> list[str]:
    symbols: set[str] = set()
    for vintage in vintages:
        cleaned = vintage.symbol.strip().upper()
        if cleaned:
            symbols.add(cleaned)
    if extra_symbols is not None:
        for sym in extra_symbols:
            if not isinstance(sym, str) or not sym.strip():
                raise ValueError(f"invalid symbol in CIK partition metadata: {sym!r}")
            symbols.add(sym.strip().upper())
    return sorted(symbols)


def _canonical_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise TypeError("batch metadata must be a mapping or None")
    try:
        serialized = json.dumps(dict(metadata), sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("batch metadata must be strictly JSON-serializable") from exc
    decoded = json.loads(serialized)
    if type(decoded) is not dict:
        raise ValueError("batch metadata must serialize to a JSON object")
    return decoded


def validate_financials_partition(partition: str | Path) -> dict[str, Any]:
    """Reject stale schemas, partial partitions and mismatched table identities."""
    dest = Path(partition)
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    version = detect_schema_version(manifest)
    symbol = dest.name.removeprefix("symbol=")
    if not dest.name.startswith("symbol=") or manifest.get("symbol") != symbol:
        raise ValueError("financials manifest symbol does not match partition identity")
    pq = validate_manifest_files_and_schemas(dest, manifest, version=version)
    for filename in ("company_financial_vintages.parquet", "financial_statements.parquet"):
        table = pq.ParquetFile(dest / filename)
        symbols = table.read(columns=["symbol"]).column("symbol").to_pylist()
        if any(value != symbol for value in symbols):
            raise ValueError("financials table symbol does not match partition identity")
    if version in {"v4", "v5"}:
        validate_and_reconstruct_linked_vintages(dest, pq, version=version)
    return manifest


def write_financials_partition(
    root: str | Path,
    symbol: str,
    vintages: Iterable[SecCompanyFinancialVintage],
) -> dict[str, Any]:
    """Write company financial statement vintages and rows into a symbol= partitioned directory."""
    pq = load_parquet()
    upper_symbol = symbol.upper()
    final_dest = Path(root) / f"symbol={upper_symbol}"
    sorted_vintages = sorted(vintages, key=vintage_sort_key)

    has_adr = any(v.adr_metadata is not None for v in sorted_vintages)
    has_evidence = any(v.unit_evidence is not None for v in sorted_vintages)
    version: SchemaVersion = "v5" if has_adr else "v4" if has_evidence else "v3"
    expected_schema = (
        FINANCIALS_DATASET_SCHEMA_V5
        if version == "v5"
        else FINANCIALS_DATASET_SCHEMA_V4
        if version == "v4"
        else DATASET_SCHEMA
    )
    expected_profile = (
        WRITER_PROFILE_V5
        if version == "v5"
        else WRITER_PROFILE_V4
        if version == "v4"
        else WRITER_PROFILE
    )

    if any(v.symbol != upper_symbol for v in sorted_vintages):
        raise ValueError("vintage symbol must exactly match the uppercase partition symbol")
    if version in {"v4", "v5"}:
        _require_unique_vintage_identities(sorted_vintages)
    if final_dest.exists():
        existing = validate_financials_partition(final_dest)
        if (
            existing["dataset_schema"] != expected_schema
            or existing["writer_profile"] != expected_profile
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
        vintage_rows, statement_rows = serialize_tables(sorted_vintages, version=version)
        manifest = write_parquet_tables_and_manifest(
            dest,
            vintage_rows=vintage_rows,
            statement_rows=statement_rows,
            version=version,
            manifest_fields={"symbol": upper_symbol},
        )
        dest.rename(final_dest)
    return manifest


def validate_cik_financials_partition(partition: str | Path) -> dict[str, Any]:
    """Validate an immutable ``cik=<10-digit>`` financials partition fail-closed."""
    dest = Path(partition)
    if not dest.name.startswith("cik="):
        raise ValueError("CIK partition directory must start with 'cik='")
    raw_cik = dest.name.removeprefix("cik=")
    normalized_cik = normalize_sec_cik(raw_cik)
    if raw_cik != normalized_cik:
        raise ValueError("CIK partition directory must use canonical 10-digit zero-padded CIK")

    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    version = detect_schema_version(manifest)
    if version != "v5":
        raise ValueError("CIK financials partition requires v5 dataset schema")
    if manifest.get("partition_key") != "cik" or manifest.get("cik") != normalized_cik:
        raise ValueError("financials manifest CIK does not match partition identity")

    manifest_symbols = manifest.get("symbols")
    if (
        not isinstance(manifest_symbols, list)
        or any(
            not isinstance(sym, str) or not sym or sym != sym.strip().upper()
            for sym in manifest_symbols
        )
        or manifest_symbols != sorted(set(manifest_symbols))
    ):
        raise ValueError("financials manifest symbols must be sorted unique uppercase strings")

    pq = validate_manifest_files_and_schemas(dest, manifest, version=version)
    for filename in ("company_financial_vintages.parquet", "financial_statements.parquet"):
        table = pq.ParquetFile(dest / filename)
        ciks = table.read(columns=["cik"]).column("cik").to_pylist()
        if any(value != normalized_cik for value in ciks):
            raise ValueError("financials table CIK does not match partition identity")

    vintages = validate_and_reconstruct_linked_vintages(dest, pq, version="v5")
    observed_symbols = _canonical_symbols(vintages)
    if not set(observed_symbols).issubset(set(manifest_symbols)):
        raise ValueError("financials manifest symbols omits observed vintage symbols")
    return manifest


def read_cik_financials_partition(
    partition: str | Path,
) -> tuple[dict[str, Any], tuple[SecCompanyFinancialVintage, ...]]:
    """Validate and read back an immutable ``cik=`` financials partition."""
    dest = Path(partition)
    manifest = validate_cik_financials_partition(dest)
    pq = load_parquet()
    vintages = validate_and_reconstruct_linked_vintages(dest, pq, version="v5")
    return manifest, vintages


def write_cik_financials_partition(
    root: str | Path,
    cik: str,
    vintages: Iterable[SecCompanyFinancialVintage],
    *,
    symbols: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Write company financial vintages into an immutable ``cik=<10-digit>`` directory."""
    pq = load_parquet()
    normalized_cik = normalize_sec_cik(cik)
    final_dest = Path(root) / f"cik={normalized_cik}"
    sorted_vintages = sorted(vintages, key=vintage_sort_key)

    for vintage in sorted_vintages:
        if vintage.cik != normalized_cik:
            raise ValueError("vintage cik must exactly match the canonical 10-digit partition CIK")
    _require_unique_vintage_identities(sorted_vintages)

    expected_symbols = _canonical_symbols(sorted_vintages, symbols)
    if final_dest.exists():
        existing = validate_cik_financials_partition(final_dest)
        table = pq.ParquetFile(final_dest / "company_financial_vintages.parquet").read()
        if existing["symbols"] == expected_symbols and table.column(
            "vintage_identity"
        ).to_pylist() == [v.vintage_identity for v in sorted_vintages]:
            return existing
        raise ValueError("immutable financials cik partition differs; write to a new output root")

    Path(root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".financials-cik-", dir=root) as stage:
        dest = Path(stage)
        vintage_rows, statement_rows = serialize_tables(sorted_vintages, version="v5")
        manifest = write_parquet_tables_and_manifest(
            dest,
            vintage_rows=vintage_rows,
            statement_rows=statement_rows,
            version="v5",
            manifest_fields={
                "partition_key": "cik",
                "cik": normalized_cik,
                "symbols": expected_symbols,
            },
        )
        dest.rename(final_dest)
    return manifest


def validate_financials_incremental_batch(batch_dir: str | Path) -> dict[str, Any]:
    """Validate an immutable incremental financials batch directory fail-closed."""
    dest = Path(batch_dir)
    batch_id = _validate_batch_id(dest.name)
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    version = detect_schema_version(manifest)
    if version != "v5":
        raise ValueError("incremental financials batch requires v5 dataset schema")
    if manifest.get("partition_key") != "batch" or manifest.get("batch_id") != batch_id:
        raise ValueError("incremental batch manifest identity mismatch")
    if type(manifest.get("metadata")) is not dict:
        raise ValueError("incremental batch manifest metadata must be an object")

    pq = validate_manifest_files_and_schemas(dest, manifest, version="v5")
    vintages = validate_and_reconstruct_linked_vintages(dest, pq, version="v5")

    observed_ciks = sorted({normalize_sec_cik(v.cik) for v in vintages})
    if any(v.cik != normalize_sec_cik(v.cik) for v in vintages):
        raise ValueError("incremental batch vintage cik must be canonical 10-digit string")
    observed_symbols = _canonical_symbols(vintages)
    observed_accessions = sorted({v.accession_number for v in vintages})

    if manifest.get("ciks") != observed_ciks:
        raise ValueError("incremental batch manifest ciks mismatch")
    if manifest.get("symbols") != observed_symbols:
        raise ValueError("incremental batch manifest symbols mismatch")
    if manifest.get("accession_numbers") != observed_accessions:
        raise ValueError("incremental batch manifest accession_numbers mismatch")
    return manifest


def write_financials_incremental_batch(
    root: str | Path,
    batch_id: str,
    vintages: Iterable[SecCompanyFinancialVintage],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write multi-CIK vintages atomically into an immutable ``<root>/batches/<batch_id>/``."""
    pq = load_parquet()
    safe_batch_id = _validate_batch_id(batch_id)
    canonical_meta = _canonical_metadata(metadata)

    sorted_vintages = sorted(vintages, key=lambda v: vintage_sort_key(v, include_cik=True))
    for vintage in sorted_vintages:
        if vintage.cik != normalize_sec_cik(vintage.cik):
            raise ValueError("vintage cik must be a canonical 10-digit zero-padded string")
    _require_unique_vintage_identities(sorted_vintages)

    batches_root = Path(root) / "batches"
    final_dest = batches_root / safe_batch_id
    if final_dest.exists():
        existing = validate_financials_incremental_batch(final_dest)
        table = pq.ParquetFile(final_dest / "company_financial_vintages.parquet").read()
        if existing["metadata"] == canonical_meta and table.column(
            "vintage_identity"
        ).to_pylist() == [v.vintage_identity for v in sorted_vintages]:
            return existing
        raise ValueError("immutable financials incremental batch differs; use a new batch_id")

    batches_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".financials-batch-", dir=batches_root) as stage:
        dest = Path(stage)
        vintage_rows, statement_rows = serialize_tables(sorted_vintages, version="v5")
        manifest = write_parquet_tables_and_manifest(
            dest,
            vintage_rows=vintage_rows,
            statement_rows=statement_rows,
            version="v5",
            manifest_fields={
                "partition_key": "batch",
                "batch_id": safe_batch_id,
                "ciks": sorted({v.cik for v in sorted_vintages}),
                "symbols": _canonical_symbols(sorted_vintages),
                "accession_numbers": sorted({v.accession_number for v in sorted_vintages}),
                "metadata": canonical_meta,
            },
        )
        dest.rename(final_dest)
    return manifest


def list_financials_incremental_batches(root: str | Path) -> tuple[Path, ...]:
    """Return sorted batch directory paths under ``<root>/batches``."""
    batches_root = Path(root) / "batches"
    if not batches_root.is_dir():
        return ()
    return tuple(
        sorted(
            entry
            for entry in batches_root.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )
    )


__all__ = [
    "DATASET_SCHEMA",
    "WRITER_PROFILE",
    "WRITER_PROFILE_V4",
    "WRITER_PROFILE_V5",
    "list_financials_incremental_batches",
    "read_cik_financials_partition",
    "validate_cik_financials_partition",
    "validate_financials_incremental_batch",
    "validate_financials_partition",
    "write_cik_financials_partition",
    "write_financials_incremental_batch",
    "write_financials_partition",
]

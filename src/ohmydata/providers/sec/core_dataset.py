"""Deterministic PyArrow writer for the compact SEC research input tables."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from decimal import Decimal
from itertools import islice
from pathlib import Path
from typing import Any

from .batch import (
    AppendOnlyIndex,
    atomic_json,
    canonical_hash,
    partition_identity,
    publish_directory,
)
from .nport import SecNportQuarterResult

WRITER_PROFILE = "sec-core-parquet-v1"
DATASET_SCHEMA = "sec-fund-holdings-pit-v1"
FUND_COLUMNS = (
    "provider",
    "fund_symbol",
    "cik",
    "registrant_name",
    "registrant_file_number",
    "registrant_lei",
    "series_id",
    "series_name",
    "series_lei",
    "accession_number",
    "submission_type",
    "report_ending_period",
    "report_date",
    "filing_date",
    "accepted_at",
    "observed_at",
    "availability_anchor",
    "availability_basis",
    "availability_precision",
    "availability_policy",
    "availability_lag_days",
    "total_assets",
    "total_assets_native",
    "total_liabilities",
    "total_liabilities_native",
    "net_assets",
    "net_assets_native",
    "artifact_sha256",
    "artifact_manifest_sha256",
    "payload_hash",
    "vintage_identity",
    "universe_hash",
    "quality_flags",
)
HOLDING_COLUMNS = (
    "fund_symbol",
    "cik",
    "series_id",
    "accession_number",
    "report_date",
    "holding_id",
    "issuer_name",
    "issuer_lei",
    "issuer_title",
    "issuer_cusip",
    "balance",
    "balance_native",
    "unit",
    "other_unit_desc",
    "currency_code",
    "currency_value",
    "currency_value_native",
    "exchange_rate",
    "exchange_rate_native",
    "percentage",
    "percentage_native",
    "payoff_profile",
    "asset_cat",
    "other_asset",
    "issuer_type",
    "other_issuer",
    "investment_country",
    "is_restricted_security",
    "fair_value_level",
    "derivative_cat",
    "artifact_sha256",
    "payload_hash",
    "vintage_identity",
)
IDENTIFIER_COLUMNS = (
    "fund_symbol",
    "cik",
    "series_id",
    "accession_number",
    "report_date",
    "holding_id",
    "identifiers_id",
    "identifier_isin",
    "identifier_ticker",
    "other_identifier",
    "other_identifier_desc",
    "artifact_sha256",
    "vintage_identity",
)

PRIMARY_KEYS = {
    "fund_vintages": (
        "provider",
        "cik",
        "series_id",
        "report_date",
        "accession_number",
        "artifact_sha256",
    ),
    "holdings": ("cik", "accession_number", "holding_id", "artifact_sha256"),
    "identifiers": ("cik", "accession_number", "holding_id", "identifiers_id", "artifact_sha256"),
}


def _pk_key(row: dict[str, Any], columns: tuple[str, ...]) -> tuple[tuple[bool, str], ...]:
    """Comparable lexical primary-key tuple, with nulls first."""

    def scalar(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (str, int, float, bool, Decimal)):
            return str(value)
        return str(value)

    return tuple((row.get(column) is not None, scalar(row.get(column))) for column in columns)


def _pa() -> Any:
    try:
        pa = importlib.import_module("pyarrow")
    except ImportError as exc:
        raise RuntimeError("install the optional 'sec-cli' extra to write Parquet") from exc
    return pa


def _native(row: Mapping[str, Any], name: str) -> str | None:
    native = row.get("__native__")
    value = native.get(name) if native is not None else row.get(name)
    return value if value not in (None, "") else None


def _decimal(value: Any) -> Decimal | None:
    return value if isinstance(value, Decimal) else None


def _schema(pa: Any, columns: list[str]) -> Any:
    decimals = {
        "total_assets",
        "total_liabilities",
        "net_assets",
        "balance",
        "currency_value",
        "exchange_rate",
        "percentage",
    }
    dates = {"report_ending_period", "report_date", "filing_date"}
    timestamps = {"accepted_at", "observed_at", "availability_anchor"}
    fields: list[Any] = []
    for column in columns:
        if column in decimals:
            typ = pa.decimal128(38, 18)
        elif column in dates:
            typ = pa.date32()
        elif column in timestamps:
            typ = pa.timestamp("us", tz="UTC")
        elif column == "availability_lag_days":
            typ = pa.int16()
        elif column == "quality_flags":
            typ = pa.list_(pa.string())
        else:
            typ = pa.string()
        nonnull = column in {
            "provider",
            "fund_symbol",
            "cik",
            "accession_number",
            "report_date",
            "holding_id",
            "identifiers_id",
            "artifact_sha256",
            "payload_hash",
            "vintage_identity",
            "universe_hash",
            "observed_at",
            "quality_flags",
        }
        fields.append(pa.field(column, typ, nullable=not nonnull))
    return pa.schema(
        fields,
        metadata={
            b"omd.dataset_schema": DATASET_SCHEMA.encode(),
            b"omd.writer_profile": WRITER_PROFILE.encode(),
        },
    )


def rows_from_result(
    result: SecNportQuarterResult,
    fund_symbol: str,
    universe_hash: str,
    artifact_manifest_sha256: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    funds: list[dict[str, Any]] = []
    holdings: list[dict[str, Any]] = []
    identifiers: list[dict[str, Any]] = []
    for v in result.vintages:
        ident = v.vintage_identity
        payload_hash = v.payload_hash
        funds.append(
            {
                "provider": "sec",
                "fund_symbol": fund_symbol,
                "cik": v.cik,
                "registrant_name": v.registrant_name,
                "registrant_file_number": v.registrant_file_number,
                "registrant_lei": v.registrant_lei,
                "series_id": v.series_id or None,
                "series_name": v.series_name,
                "series_lei": v.series_lei,
                "accession_number": v.accession_number,
                "submission_type": v.submission_type,
                "report_ending_period": v.report_ending_period or v.report_date,
                "report_date": v.report_date,
                "filing_date": v.filing_date,
                "accepted_at": v.accepted_at,
                "observed_at": v.observed_at,
                "availability_anchor": v.availability_anchor,
                "availability_basis": v.availability_basis,
                "availability_precision": v.availability_precision,
                "availability_policy": v.availability_policy,
                "availability_lag_days": v.availability_lag_days,
                "total_assets": v.total_assets,
                "total_assets_native": v.native_assets.get("TOTAL_ASSETS")
                if v.native_assets
                else (str(v.total_assets) if v.total_assets is not None else None),
                "total_liabilities": v.total_liabilities,
                "total_liabilities_native": v.native_assets.get("TOTAL_LIABILITIES")
                if v.native_assets
                else (str(v.total_liabilities) if v.total_liabilities is not None else None),
                "net_assets": v.net_assets,
                "net_assets_native": v.native_assets.get("NET_ASSETS")
                if v.native_assets
                else (str(v.net_assets) if v.net_assets is not None else None),
                "artifact_sha256": v.artifact_sha256,
                "artifact_manifest_sha256": artifact_manifest_sha256,
                "payload_hash": payload_hash,
                "vintage_identity": ident,
                "universe_hash": universe_hash,
                "quality_flags": sorted(set(v.quality_flags)),
            }
        )
        for row in v.holdings:
            d = {
                "fund_symbol": fund_symbol,
                "cik": v.cik,
                "series_id": v.series_id or None,
                "accession_number": v.accession_number,
                "report_date": v.report_date,
                "holding_id": str(row.get("HOLDING_ID") or ""),
                "issuer_name": row.get("ISSUER_NAME"),
                "issuer_lei": row.get("ISSUER_LEI"),
                "issuer_title": row.get("ISSUER_TITLE"),
                "issuer_cusip": row.get("ISSUER_CUSIP"),
                "balance": _decimal(row.get("BALANCE")),
                "balance_native": _native(row, "BALANCE"),
                "unit": row.get("UNIT"),
                "other_unit_desc": row.get("OTHER_UNIT_DESC"),
                "currency_code": row.get("CURRENCY_CODE"),
                "currency_value": _decimal(row.get("CURRENCY_VALUE")),
                "currency_value_native": _native(row, "CURRENCY_VALUE"),
                "exchange_rate": _decimal(row.get("EXCHANGE_RATE")),
                "exchange_rate_native": _native(row, "EXCHANGE_RATE"),
                "percentage": _decimal(row.get("PERCENTAGE")),
                "percentage_native": _native(row, "PERCENTAGE"),
                "payoff_profile": row.get("PAYOFF_PROFILE"),
                "asset_cat": row.get("ASSET_CAT"),
                "other_asset": row.get("OTHER_ASSET"),
                "issuer_type": row.get("ISSUER_TYPE"),
                "other_issuer": row.get("OTHER_ISSUER"),
                "investment_country": row.get("INVESTMENT_COUNTRY"),
                "is_restricted_security": row.get("IS_RESTRICTED_SECURITY"),
                "fair_value_level": row.get("FAIR_VALUE_LEVEL"),
                "derivative_cat": row.get("DERIVATIVE_CAT"),
                "artifact_sha256": v.artifact_sha256,
                "payload_hash": payload_hash,
                "vintage_identity": ident,
            }
            holdings.append(d)
        for row in v.identifiers:
            identifiers.append(
                {
                    "fund_symbol": fund_symbol,
                    "cik": v.cik,
                    "series_id": v.series_id or None,
                    "accession_number": v.accession_number,
                    "report_date": v.report_date,
                    "holding_id": str(row.get("HOLDING_ID") or ""),
                    "identifiers_id": str(row.get("IDENTIFIERS_ID") or ""),
                    "identifier_isin": row.get("IDENTIFIER_ISIN"),
                    "identifier_ticker": row.get("IDENTIFIER_TICKER"),
                    "other_identifier": row.get("OTHER_IDENTIFIER"),
                    "other_identifier_desc": row.get("OTHER_IDENTIFIER_DESC"),
                    "artifact_sha256": v.artifact_sha256,
                    "vintage_identity": ident,
                }
            )
    return funds, holdings, identifiers


def logical_table_hash(table_name: str, table_or_rows: Any) -> str:
    rows = table_or_rows.to_pylist() if hasattr(table_or_rows, "to_pylist") else list(table_or_rows)
    return canonical_hash({"table": table_name, "rows": rows})


def validate_tables(
    directory: str | Path, expected_manifest: dict[str, Any] | None = None
) -> dict[str, Any]:
    pa = _pa()
    pq = __import__("pyarrow.parquet", fromlist=["read_table"])
    directory = Path(directory)
    result: dict[str, Any] = {"files": {}, "logical_hashes": {}}
    for name, columns in zip(
        ("fund_vintages", "holdings", "identifiers"),
        (FUND_COLUMNS, HOLDING_COLUMNS, IDENTIFIER_COLUMNS),
    ):
        path = directory / f"{name}.parquet"
        if not path.is_file():
            raise ValueError("missing parquet table")
        # ParquetFile reads one exact file; read_table would infer Hive
        # partition columns from source_quarter/artifact/partition parents.
        table = pq.ParquetFile(path).read()
        if list(table.column_names) != list(columns):
            raise ValueError("schema column order mismatch")
        if table.schema != _schema(pa, list(columns)):
            raise ValueError("schema mismatch")
        rows = table.to_pylist()
        keys = PRIMARY_KEYS[name]
        key_values = [tuple(row.get(column) for column in keys) for row in rows]
        if len(key_values) != len({repr(value) for value in key_values}):
            raise ValueError("duplicate primary key")
        if [_pk_key(row, keys) for row in rows] != sorted(_pk_key(row, keys) for row in rows):
            raise ValueError("primary key ordering mismatch")
        result["files"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        result["logical_hashes"][name] = logical_table_hash(name, table)
    manifest_path = directory / "manifest.json"
    if manifest_path.is_file():
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
        if stored.get("file_hashes") and stored["file_hashes"] != result["files"]:
            raise ValueError("parquet file hash mismatch")
        if stored.get("logical_hashes") and stored["logical_hashes"] != result["logical_hashes"]:
            raise ValueError("logical table hash mismatch")
        quality_path = directory / "quality.json"
        if stored.get("quality_hash") is not None and (
            not quality_path.is_file()
            or stored["quality_hash"] != hashlib.sha256(quality_path.read_bytes()).hexdigest()
        ):
            raise ValueError("quality hash mismatch")
        if canonical_hash(
            {k: v for k, v in stored.items() if k != "manifest_sha256"}
        ) != stored.get("manifest_sha256", canonical_hash(stored)):
            raise ValueError("manifest hash mismatch")
    if expected_manifest is not None and (
        not manifest_path.is_file() or json.loads(manifest_path.read_text()) != expected_manifest
    ):
        raise ValueError("manifest mismatch")
    return result


def write_partition(
    root: str | Path,
    *,
    source_quarter: str,
    artifact_sha256: str,
    artifact_manifest_sha256: str,
    universe_hash: str,
    edgar_closure_hash: str,
    parser_version: str,
    availability_policy: str,
    lag_days: int | None,
    tables: tuple[Iterable[dict[str, Any]], Iterable[dict[str, Any]], Iterable[dict[str, Any]]],
    quality: dict[str, Any],
    max_selected_rows: int = 5_000_000,
    max_output_bytes: int = 2 * 1024**3,
) -> dict[str, Any]:
    if max_selected_rows <= 0 or max_output_bytes <= 0:
        raise ValueError("output limits must be positive")
    pa = _pa()
    pq = __import__("pyarrow.parquet", fromlist=["ParquetFile"])
    version = pa.__version__
    identity = partition_identity(
        source_quarter=source_quarter,
        artifact_sha256=artifact_sha256,
        artifact_manifest_sha256=artifact_manifest_sha256,
        universe_hash=universe_hash,
        edgar_closure_hash=edgar_closure_hash,
        parser_version=parser_version,
        dataset_schema_version=DATASET_SCHEMA,
        availability_policy=availability_policy,
        lag_days=lag_days,
        writer_profile_version=WRITER_PROFILE,
        pyarrow_version=version,
    )
    base = (
        Path(root)
        / "core"
        / DATASET_SCHEMA
        / f"source_quarter={source_quarter}"
        / f"artifact={artifact_sha256}"
        / f"partition={identity}"
    )
    base.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(base.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    temp = Path(tempfile.mkdtemp(prefix=f".{identity}-", dir=base.parent))
    try:
        write_tables(tables, temp, max_rows=max_selected_rows)
        reread = validate_tables(temp)
        quality_value = {"schema_version": "sec-quality-v1", **quality}
        atomic_json(temp / "quality.json", quality_value)
        manifest = {
            "schema_version": "sec-partition-manifest-v1",
            "partition_identity": identity,
            "dataset_schema": DATASET_SCHEMA,
            "writer_profile": WRITER_PROFILE,
            "pyarrow_version": version,
            "source_quarter": source_quarter,
            "artifact_sha256": artifact_sha256,
            "artifact_manifest_sha256": artifact_manifest_sha256,
            "universe_hash": universe_hash,
            "edgar_closure_hash": edgar_closure_hash,
            "parser_version": parser_version,
            "availability_policy": availability_policy,
            "lag_days": lag_days,
            "row_counts": {
                n: pq.ParquetFile(temp / f"{n}.parquet").metadata.num_rows
                for n in ("fund_vintages", "holdings", "identifiers")
            },
            "logical_hashes": reread["logical_hashes"],
            "file_hashes": reread["files"],
            "quality_hash": hashlib.sha256((temp / "quality.json").read_bytes()).hexdigest(),
        }
        atomic_json(temp / "manifest.json", manifest)
        validate_tables(temp)
        staged_bytes = sum(
            (temp / name).stat().st_size
            for name in (
                "fund_vintages.parquet",
                "holdings.parquet",
                "identifiers.parquet",
                "quality.json",
                "manifest.json",
            )
        )
        if staged_bytes > max_output_bytes:
            raise ValueError("staged partition exceeds max-output-bytes")
        publish_directory(temp, base)
        key = (
            "source_quarter",
            "artifact_sha256",
            "artifact_manifest_sha256",
            "universe_hash",
            "edgar_closure_hash",
            "parser_version",
            "dataset_schema_version",
            "availability_policy",
            "lag_days",
            "writer_profile_version",
            "pyarrow_version",
        )
        entry = {
            k: manifest.get(
                k,
                DATASET_SCHEMA
                if k == "dataset_schema_version"
                else WRITER_PROFILE
                if k == "writer_profile_version"
                else None,
            )
            for k in key
        }
        entry.update(
            {
                "partition_identity": identity,
                "partition_path": str(base.relative_to(Path(root))),
                "manifest_hash": canonical_hash(manifest),
                "file_hashes": manifest["file_hashes"],
            }
        )
        AppendOnlyIndex(
            Path(root) / "core" / DATASET_SCHEMA / "catalog.json", "sec-core-catalog-v1"
        ).append(entry, key)
        return manifest
    finally:
        if temp.exists():
            import shutil

            shutil.rmtree(temp)


def write_tables(
    tables: tuple[Iterable[dict[str, Any]], Iterable[dict[str, Any]], Iterable[dict[str, Any]]],
    output_dir: str | Path,
    *,
    max_rows: int = 5_000_000,
) -> dict[str, str]:
    if max_rows <= 0:
        raise ValueError("max_rows must be positive")
    pa = _pa()
    pq = __import__("pyarrow.parquet", fromlist=["write_table"])
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    total_rows = 0
    for name, rows, columns in zip(
        ("fund_vintages", "holdings", "identifiers"),
        tables,
        (FUND_COLUMNS, HOLDING_COLUMNS, IDENTIFIER_COLUMNS),
    ):
        remaining = max_rows - total_rows
        data = list(islice(iter(rows), remaining + 1))
        if len(data) > remaining:
            raise ValueError("selected output row limit exceeded")
        total_rows += len(data)
        for row in data:
            for field in (
                "total_assets",
                "total_liabilities",
                "net_assets",
                "balance",
                "currency_value",
                "exchange_rate",
                "percentage",
            ):
                value = row.get(field)
                if isinstance(value, Decimal):
                    exponent = value.as_tuple().exponent
                    if not isinstance(exponent, int):
                        raise TypeError("invalid decimal exponent")
                    if len(value.as_tuple().digits) > 38 or -exponent > 18:
                        raise ValueError("decimal exceeds decimal128(38,18)")
        keys = PRIMARY_KEYS[name]
        data.sort(key=lambda row: _pk_key(row, keys))
        table = pa.Table.from_pylist(data, schema=_schema(pa, list(columns)))
        # Explicitly reject duplicate primary keys after null normalization.
        pyrows = table.to_pylist()
        tuples = [tuple(row[k] for k in keys) for row in pyrows]
        if len(tuples) != len(set(map(repr, tuples))):
            raise ValueError("duplicate primary key")
        path = out / f"{name}.parquet"
        fd, tmp_name = tempfile.mkstemp(prefix=f".{name}-", dir=out)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            pq.write_table(
                table,
                tmp,
                version="2.6",
                data_page_version="2.0",
                compression="zstd",
                compression_level=9,
                use_dictionary=False,
                write_statistics=True,
                row_group_size=65536,
                coerce_timestamps="us",
                allow_truncated_timestamps=False,
            )
            tmp.replace(path)
        finally:
            if tmp.exists():
                tmp.unlink()
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes

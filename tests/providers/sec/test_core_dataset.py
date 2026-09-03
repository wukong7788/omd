from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from typing import Any, cast

import pytest

from ohmydata.providers.sec.core_dataset import (
    FUND_COLUMNS,
    HOLDING_COLUMNS,
    IDENTIFIER_COLUMNS,
    validate_tables,
    write_partition,
    write_tables,
)


def test_empty_tables_have_fixed_schema_and_order(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    pq = cast(Any, importlib.import_module("pyarrow.parquet"))

    empty_tables: tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]] = (
        [],
        [],
        [],
    )
    write_tables(empty_tables, tmp_path)
    for name, columns in zip(
        ("fund_vintages", "holdings", "identifiers"),
        (FUND_COLUMNS, HOLDING_COLUMNS, IDENTIFIER_COLUMNS),
    ):
        assert pq.read_table(tmp_path / f"{name}.parquet").column_names == list(columns)


def test_decimal_overflow_fails_closed(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    row: dict[str, Any] = {
        "provider": "sec",
        "fund_symbol": "X",
        "cik": "0000000001",
        "accession_number": "0000000001-24-000001",
        "report_date": None,
        "observed_at": None,
        "artifact_sha256": "a",
        "payload_hash": "b",
        "vintage_identity": "c",
        "universe_hash": "d",
        "quality_flags": [],
        "total_assets": __import__("decimal").Decimal("1e-19"),
    }
    with pytest.raises(ValueError):
        write_tables(([row], [], []), tmp_path)


def test_write_tables_row_limit_is_checked_before_publication(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    with pytest.raises(ValueError, match="max_rows"):
        write_tables(([{"provider": "sec"}], [], []), tmp_path, max_rows=0)


def test_write_tables_consumes_only_one_row_past_limit(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    consumed = 0

    def rows() -> Any:
        nonlocal consumed
        for _ in range(4):
            consumed += 1
            if consumed > 3:
                raise AssertionError("iterator consumed beyond limit plus one")
            yield {"provider": "sec"}

    with pytest.raises(ValueError, match="row limit"):
        write_tables((rows(), [], []), tmp_path, max_rows=2)
    assert consumed == 3
    assert not (tmp_path / "fund_vintages.parquet").exists()


def test_file_hash_is_stable_for_empty_tables(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    empty_tables: tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]] = (
        [],
        [],
        [],
    )
    first = write_tables(empty_tables, tmp_path)
    second = write_tables(empty_tables, tmp_path)
    assert first == second
    assert (
        hashlib.sha256((tmp_path / "holdings.parquet").read_bytes()).hexdigest()
        == second["holdings"]
    )


def test_partition_publication_catalog_and_tamper(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    empty_tables: tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]] = (
        [],
        [],
        [],
    )
    quality: dict[str, Any] = {"quality_flags": []}
    kwargs: dict[str, Any] = {
        "source_quarter": "2024q2",
        "artifact_sha256": "a" * 64,
        "artifact_manifest_sha256": "b" * 64,
        "universe_hash": "c" * 64,
        "edgar_closure_hash": "d" * 64,
        "parser_version": "1",
        "availability_policy": "observation-only",
        "lag_days": None,
        "tables": empty_tables,
        "quality": quality,
    }
    manifest = write_partition(tmp_path, **kwargs)
    target = (
        tmp_path
        / "core"
        / "sec-fund-holdings-pit-v1"
        / "source_quarter=2024q2"
        / ("artifact=" + "a" * 64)
        / ("partition=" + manifest["partition_identity"])
    )
    assert target.is_dir() and (target / "manifest.json").is_file()
    assert validate_tables(target)["files"]
    assert (
        write_partition(tmp_path, **kwargs)["partition_identity"] == manifest["partition_identity"]
    )
    parquet = target / "holdings.parquet"
    original = parquet.read_bytes()
    parquet.write_bytes(original + b"tamper")
    with pytest.raises(ValueError):
        validate_tables(target)


def test_partition_byte_limit_leaves_no_catalog_or_partition(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    kwargs: dict[str, Any] = {
        "source_quarter": "2024q2",
        "artifact_sha256": "a" * 64,
        "artifact_manifest_sha256": "b" * 64,
        "universe_hash": "c" * 64,
        "edgar_closure_hash": "d" * 64,
        "parser_version": "1",
        "availability_policy": "observation-only",
        "lag_days": None,
        "tables": ([], [], []),
        "quality": {},
        "max_output_bytes": 1,
    }
    with pytest.raises(ValueError, match="max-output-bytes"):
        write_partition(tmp_path, **kwargs)
    assert not list((tmp_path / "core").glob("**/partition=*"))
    assert not (tmp_path / "core" / "sec-fund-holdings-pit-v1" / "catalog.json").exists()


def test_nonempty_partition_decimal_datetime_roundtrip(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    from datetime import UTC, date, datetime
    from decimal import Decimal

    fund: dict[str, Any] = {k: None for k in FUND_COLUMNS}
    fund.update(
        {
            "provider": "sec",
            "fund_symbol": "X",
            "cik": "0000000001",
            "accession_number": "0000000001-24-000001",
            "report_ending_period": date(2024, 3, 31),
            "report_date": date(2024, 3, 31),
            "observed_at": datetime(2024, 5, 1, tzinfo=UTC),
            "artifact_sha256": "a" * 64,
            "artifact_manifest_sha256": "b" * 64,
            "payload_hash": "c" * 64,
            "vintage_identity": "d" * 64,
            "universe_hash": "e" * 64,
            "quality_flags": [],
            "total_assets": Decimal("1.2300"),
            "total_assets_native": "1.2300",
        }
    )
    holding: dict[str, Any] = {k: None for k in HOLDING_COLUMNS}
    holding.update(
        {
            "fund_symbol": "X",
            "cik": "0000000001",
            "accession_number": "0000000001-24-000001",
            "report_date": date(2024, 3, 31),
            "holding_id": "H1",
            "artifact_sha256": "a" * 64,
            "payload_hash": "c" * 64,
            "vintage_identity": "d" * 64,
            "balance": Decimal("1.2300"),
            "balance_native": "1.2300",
        }
    )
    ident: dict[str, Any] = {k: None for k in IDENTIFIER_COLUMNS}
    ident.update(
        {
            "fund_symbol": "X",
            "cik": "0000000001",
            "accession_number": "0000000001-24-000001",
            "report_date": date(2024, 3, 31),
            "holding_id": "H1",
            "identifiers_id": "I1",
            "artifact_sha256": "a" * 64,
            "vintage_identity": "d" * 64,
        }
    )
    args: dict[str, Any] = {
        "source_quarter": "2024q2",
        "artifact_sha256": "a" * 64,
        "artifact_manifest_sha256": "b" * 64,
        "universe_hash": "e" * 64,
        "edgar_closure_hash": "f" * 64,
        "parser_version": "1",
        "availability_policy": "observation-only",
        "lag_days": None,
        "tables": ([fund], [holding], [ident]),
        "quality": {},
    }
    first = write_partition(tmp_path, **args)
    second = write_partition(tmp_path, **args)
    assert first["partition_identity"] == second["partition_identity"]


def test_primary_key_sort_null_first_and_validator_rejects_duplicate(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pyarrow")
    pa = cast(Any, importlib.import_module("pyarrow"))
    pq = cast(Any, importlib.import_module("pyarrow.parquet"))

    def fund(series: str | None, suffix: str) -> dict[str, Any]:
        row: dict[str, Any] = {k: None for k in FUND_COLUMNS}
        row.update(
            {
                "provider": "sec",
                "fund_symbol": "X",
                "cik": "0000000001",
                "series_id": series,
                "accession_number": f"0000000001-24-00000{suffix}",
                "report_date": __import__("datetime").date(2024, 3, 31),
                "observed_at": __import__("datetime").datetime(
                    2024, 5, 1, tzinfo=__import__("datetime").UTC
                ),
                "artifact_sha256": "a" * 64,
                "payload_hash": "b" * 64,
                "vintage_identity": suffix * 64,
                "universe_hash": "c" * 64,
                "quality_flags": [],
            }
        )
        return row

    rows = [fund("S1", "1"), fund(None, "2")]
    write_tables((rows, [], []), tmp_path)
    table = pq.ParquetFile(tmp_path / "fund_vintages.parquet").read()
    assert table.column("series_id").to_pylist() == [None, "S1"]
    validate_tables(tmp_path)

    duplicate = pa.Table.from_pylist([rows[1], rows[1]], schema=table.schema)
    pq.write_table(duplicate, tmp_path / "fund_vintages.parquet")
    with pytest.raises(ValueError, match="duplicate primary key"):
        validate_tables(tmp_path)


def test_primary_key_lexical_order_a_b_c_d_rejected_when_rewritten(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pyarrow")
    pa = cast(Any, importlib.import_module("pyarrow"))
    pq = cast(Any, importlib.import_module("pyarrow.parquet"))

    def holding(a: str, b: str, c: str, d: str) -> dict[str, Any]:
        row: dict[str, Any] = {k: None for k in HOLDING_COLUMNS}
        row.update(
            {
                "fund_symbol": "X",
                "cik": a,
                "accession_number": b,
                "report_date": __import__("datetime").date(2024, 3, 31),
                "holding_id": c,
                "artifact_sha256": d,
                "payload_hash": "p" * 64,
                "vintage_identity": "v" * 64,
            }
        )
        return row

    rows = [holding("0000000001", "A", "H1", "a" * 64), holding("0000000002", "A", "H1", "a" * 64)]
    write_tables(([], rows, []), tmp_path)
    validate_tables(tmp_path)
    table = pq.ParquetFile(tmp_path / "holdings.parquet").read()
    pq.write_table(
        pa.Table.from_pylist(list(reversed(rows)), schema=table.schema),
        tmp_path / "holdings.parquet",
    )
    with pytest.raises(ValueError, match="primary key ordering"):
        validate_tables(tmp_path)

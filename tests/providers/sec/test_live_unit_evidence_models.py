"""Offline persistence contracts for SEC live unit evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ohmydata.providers.sec.financials import (
    SecCompanyFinancialVintage,
    SecStatementRow,
)
from ohmydata.providers.sec.financials_dataset import (
    validate_financials_partition,
    write_financials_partition,
)
from ohmydata.providers.sec.unit_evidence import (
    SEC_LIVE_FINANCIAL_PARSER_V2,
    SecFinancialUnitEvidence,
)


def _evidence() -> SecFinancialUnitEvidence:
    return SecFinancialUnitEvidence(
        parser_version=SEC_LIVE_FINANCIAL_PARSER_V2,
        cik="320193",
        accession_number="0000320193-24-000006",
        instance_document="aapl-20231230_htm.xml",
        instance_sha256="a" * 64,
    )


def _vintage(
    *, evidence: SecFinancialUnitEvidence | None = None, value: str = "1"
) -> SecCompanyFinancialVintage:
    return SecCompanyFinancialVintage(
        symbol="AAPL",
        cik="0000320193",
        company_name="Apple Inc.",
        form="10-Q",
        accession_number="0000320193-24-000006",
        filing_date=date(2024, 2, 2),
        rows=(
            SecStatementRow(
                "income_statement",
                "Revenue",
                "us-gaap_Revenue",
                "Revenue",
                Decimal(value),
                value,
                unit="iso4217:USD",
                context_ref="c1",
                unit_ref="u1",
            ),
        ),
        unit_evidence=evidence,
    )


def _rehash_manifest(partition, filename: str) -> None:
    manifest_path = partition / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    path = partition / filename
    manifest["files"][filename] = {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
    }
    manifest_path.write_text(json.dumps(manifest))


def test_unit_evidence_is_canonical_and_v2_only() -> None:
    evidence = _evidence()
    assert evidence.canonical_payload == {
        "parser_version": SEC_LIVE_FINANCIAL_PARSER_V2,
        "cik": "320193",
        "accession_number": "0000320193-24-000006",
        "instance_document": "aapl-20231230_htm.xml",
        "instance_sha256": "a" * 64,
        "schema_version": "sec-financial-unit-evidence-v1",
    }
    assert evidence.evidence_identity == _evidence().evidence_identity
    with pytest.raises(ValueError, match="zero-stripped"):
        replace(evidence, cik="0000320193")
    with pytest.raises(ValueError, match="parser v2"):
        replace(evidence, parser_version="sec-live-financial-parser-v1-edgartools-5.56.0")


@pytest.mark.parametrize(
    "field,value",
    [
        ("cik", " 320193"),
        ("cik", "٣٢٠١٩٣"),
        ("accession_number", "000032019324000006"),
        ("instance_document", "../fake.xml"),
        ("instance_document", "https://example.invalid/fake.xml"),
        ("instance_document", "fake.xml?query=1"),
        ("instance_sha256", "A" * 64),
        ("instance_sha256", "a" * 63),
    ],
)
def test_unit_evidence_rejects_noncanonical_identity_fields(field, value) -> None:
    with pytest.raises(ValueError):
        replace(_evidence(), **{field: value})


def test_vintage_v3_identity_is_preserved_and_evidence_is_bound() -> None:
    legacy = _vintage()
    evidenced = _vintage(evidence=_evidence())
    assert legacy.vintage_identity != evidenced.vintage_identity
    with pytest.raises(ValueError, match="cik"):
        replace(evidenced, cik="0000000001")
    with pytest.raises(ValueError, match="accession_number"):
        replace(evidenced, accession_number="0000320193-24-000007")


def test_v4_dataset_links_same_accession_mixed_vintages_and_validates(tmp_path) -> None:
    legacy = _vintage(value="1")
    evidenced = _vintage(evidence=_evidence(), value="2")
    manifest = write_financials_partition(tmp_path, "AAPL", [legacy, evidenced])
    assert manifest["dataset_schema"] == "sec-company-financials-v4"
    partition = tmp_path / "symbol=AAPL"
    assert validate_financials_partition(partition) == manifest
    rows = pq.ParquetFile(partition / "financial_statements.parquet").read().to_pylist()
    assert {row["vintage_identity"] for row in rows} == {
        legacy.vintage_identity,
        evidenced.vintage_identity,
    }
    assert {row["accession_number"] for row in rows} == {legacy.accession_number}


def test_v4_validator_rejects_hash_correct_link_and_evidence_tampering(tmp_path) -> None:
    vintage = _vintage(evidence=_evidence())
    write_financials_partition(tmp_path, "AAPL", [vintage])
    partition = tmp_path / "symbol=AAPL"
    statement_path = partition / "financial_statements.parquet"
    statement_table = pq.ParquetFile(statement_path).read()
    statements = statement_table.to_pylist()
    statements[0]["vintage_identity"] = "b" * 64
    pq.write_table(pa.Table.from_pylist(statements, schema=statement_table.schema), statement_path)
    _rehash_manifest(partition, statement_path.name)
    with pytest.raises(ValueError, match="orphan"):
        validate_financials_partition(partition)

    # Start a fresh immutable partition for evidence tampering.
    other_root = tmp_path / "other"
    write_financials_partition(other_root, "AAPL", [vintage])
    other = other_root / "symbol=AAPL"
    vintages_path = other / "company_financial_vintages.parquet"
    vintage_table = pq.ParquetFile(vintages_path).read()
    vintages = vintage_table.to_pylist()
    payload = json.loads(vintages[0]["unit_evidence_payload"])
    payload["instance_sha256"] = "b" * 64
    vintages[0]["unit_evidence_payload"] = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )
    pq.write_table(pa.Table.from_pylist(vintages, schema=vintage_table.schema), vintages_path)
    _rehash_manifest(other, vintages_path.name)
    with pytest.raises(ValueError, match="identity"):
        validate_financials_partition(other)


@pytest.mark.parametrize(
    "field,value", [("currency", "EUR"), ("availability_anchor", datetime(2024, 2, 3, tzinfo=UTC))]
)
def test_v4_rejects_hash_correct_derived_statement_tampering(tmp_path, field, value) -> None:
    write_financials_partition(tmp_path, "AAPL", [_vintage(evidence=_evidence())])
    partition = tmp_path / "symbol=AAPL"
    path = partition / "financial_statements.parquet"
    table = pq.ParquetFile(path).read()
    rows = table.to_pylist()
    rows[0][field] = value
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    _rehash_manifest(partition, path.name)
    with pytest.raises(ValueError):
        validate_financials_partition(partition)


def test_v4_preserves_native_row_order_for_mixed_vintages(tmp_path) -> None:
    legacy = _vintage()
    rows = (replace(legacy.rows[0], standard_concept="Z", concept="fake_Z"), legacy.rows[0])
    legacy = replace(legacy, rows=rows)
    evidenced = replace(legacy, unit_evidence=_evidence())
    manifest = write_financials_partition(tmp_path, "AAPL", [legacy, evidenced])
    assert validate_financials_partition(tmp_path / "symbol=AAPL") == manifest
    assert write_financials_partition(tmp_path, "AAPL", [legacy, evidenced]) == manifest
    stored = (
        pq.ParquetFile(tmp_path / "symbol=AAPL" / "financial_statements.parquet").read().to_pylist()
    )
    assert [row["standard_concept"] for row in stored] == ["Z", "Revenue", "Z", "Revenue"]


@pytest.mark.parametrize("field,value", [("row_count", 2), ("vintage_identity", "b" * 64)])
def test_v4_rejects_hash_correct_vintage_tampering(tmp_path, field, value) -> None:
    write_financials_partition(tmp_path, "AAPL", [_vintage(evidence=_evidence())])
    partition = tmp_path / "symbol=AAPL"
    path = partition / "company_financial_vintages.parquet"
    table = pq.ParquetFile(path).read()
    rows = table.to_pylist()
    rows[0][field] = value
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    _rehash_manifest(partition, path.name)
    with pytest.raises(ValueError):
        validate_financials_partition(partition)


def test_v4_immutable_collision_preserves_existing_files(tmp_path) -> None:
    vintage = _vintage(evidence=_evidence())
    write_financials_partition(tmp_path, "AAPL", [vintage])
    partition = tmp_path / "symbol=AAPL"
    before = {path.name: path.read_bytes() for path in partition.iterdir()}
    with pytest.raises(ValueError, match="immutable"):
        write_financials_partition(tmp_path, "AAPL", [_vintage(evidence=_evidence(), value="2")])
    assert {path.name: path.read_bytes() for path in partition.iterdir()} == before


def test_v3_pre_evidence_golden_bytes_are_unchanged(tmp_path) -> None:
    row = SecStatementRow(
        "income_statement",
        "SyntheticRevenue",
        "fake_Revenue",
        "Synthetic revenue",
        Decimal("123.45"),
        "123.45",
        unit="iso4217:USD",
        decimals=2,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 3, 31),
        period_type="duration",
        context_ref="fake-context",
        unit_ref="fake-unit",
        decimals_native="2",
    )
    vintage = SecCompanyFinancialVintage(
        "FAKE",
        "0000000001",
        "Synthetic Filing Co.",
        "10-Q",
        "0000000001-24-000001",
        date(2024, 5, 1),
        period_end=date(2024, 3, 31),
        accepted_at=datetime(2024, 5, 1, 21, tzinfo=UTC),
        rows=(row,),
    )
    assert (
        vintage.vintage_identity
        == "fb52bda733f30c5c14d9066681d1ef96b584dbadd4e58356d14a79dc92f71145"
    )
    manifest = write_financials_partition(tmp_path, "FAKE", [vintage])
    partition = tmp_path / "symbol=FAKE"
    assert validate_financials_partition(partition) == manifest
    # Captured before live-v2/v4 implementation with the locked writer runtime.
    expected = {
        "financial_statements.parquet": "167adc7781d5d0780e2ce1e858847417129c903064106563589248381a59bf99",
        "company_financial_vintages.parquet": "d4fd658372b741c66b8958ef5cb812fcc6b7060c45873f137ea0f301ead79f88",
        "manifest.json": "1b741983bd7b3461b9c7d6872dfd85131c3c50d2cf394345e3bc7cae6ee75a03",
    }
    assert {
        name: hashlib.sha256((partition / name).read_bytes()).hexdigest() for name in expected
    } == expected

"""Offline unit and integration tests for SEC N-PORT structural qualification."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ohmydata.core.errors import (
    AmbiguousPartitionError,
    CoverageError,
    ResourceLimitError,
    SnapshotIntegrityError,
)
from ohmydata.providers.sec.artifacts import SecArtifactStore
from ohmydata.providers.sec.batch import (
    Quarter,
    SecEquityEtfUniverse,
    SecNportBatch,
    atomic_json,
    receipt_hashes,
)
from ohmydata.providers.sec.core_dataset import (
    FUND_COLUMNS,
    HOLDING_COLUMNS,
    IDENTIFIER_COLUMNS,
    write_partition,
)
from ohmydata.providers.sec.qualification import (
    Deadline,
    SecNportPartitionSet,
    SecNportPartitionSetEntry,
    qualify_sec_nport,
)


class _DummyClient:
    def __init__(self, body: bytes = b"offline") -> None:
        self.body = body


def make_synthetic_env(
    root: Path,
    quarter: Quarter | None = None,
    symbol: str = "TST",
    cik: str = "0000000001",
    series_id: str | None = None,
    availability_policy: str = "observation-only",
    lag_days: int | None = None,
    universe_funds: list[dict[str, Any]] | None = None,
    vintages_rows: list[dict[str, Any]] | None = None,
    holdings_rows: list[dict[str, Any]] | None = None,
    identifiers_rows: list[dict[str, Any]] | None = None,
) -> tuple[SecEquityEtfUniverse, Path]:
    target_quarter = quarter or Quarter(2024, 2)
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as archive:
        for name in (
            "SUBMISSION",
            "REGISTRANT",
            "FUND_REPORTED_INFO",
            "FUND_REPORTED_HOLDING",
            "IDENTIFIERS",
        ):
            info = zipfile.ZipInfo(f"{name}.tsv", (2024, 1, 1, 0, 0, 0))
            archive.writestr(info, "X\n")

    store = SecArtifactStore(root / "raw")
    ref = store.publish(
        source=io.BytesIO(source.getvalue()),
        year=target_quarter.year,
        quarter=target_quarter.number,
        source_url="https://www.sec.gov/files/test.zip",
        retrieved_at=datetime(2024, 8, 1, 0, 0, 0, tzinfo=UTC),
    )

    batch = SecNportBatch(root, _DummyClient())
    batch._quarter_index().append(  # pyright: ignore[reportPrivateUsage]
        {
            "year": target_quarter.year,
            "quarter": target_quarter.number,
            "artifact_sha256": ref.sha256,
            "manifest_sha256": ref.manifest_sha256,
            "source_url": "https://www.sec.gov/files/test.zip",
            "retrieved_at": "2024-08-01T00:00:00Z",
        },
        ("year", "quarter", "artifact_sha256", "manifest_sha256", "source_url", "retrieved_at"),
    )

    u_funds = universe_funds or [
        {
            "symbol": symbol,
            "cik": cik,
            "selection_mode": "single_series_cik" if series_id is None else "series",
            "series_id": series_id,
            "valid_from_quarter": None,
            "valid_to_quarter": None,
        }
    ]
    u_dict = {
        "schema_version": "sec-equity-etf-universe-v1",
        "funds": u_funds,
    }
    u_path = root / "universe.json"
    u_path.write_text(json.dumps(u_dict), encoding="utf-8")
    universe = SecEquityEtfUniverse.load(u_path)

    receipt: dict[str, Any] = {
        "schema_version": "sec-fetch-receipt-v1",
        "source_quarter": str(target_quarter),
        "nport_artifact_sha256": ref.sha256,
        "nport_manifest_sha256": ref.manifest_sha256,
        "universe_hash": universe.universe_hash,
        "payloads": [],
        "accessions": {},
    }
    receipt["edgar_closure_hash"], receipt["receipt_sha256"] = receipt_hashes(receipt)
    atomic_json(
        root
        / "state"
        / "fetch-receipts"
        / str(target_quarter)
        / f"{receipt['receipt_sha256']}.json",
        receipt,
    )

    default_vintages = [
        {
            **{key: None for key in FUND_COLUMNS},
            "provider": "sec",
            "fund_symbol": symbol,
            "cik": cik,
            "series_id": series_id,
            "accession_number": f"{cik}-24-000001",
            "submission_type": "NPORT-P",
            "report_date": date(2024, 3, 31),
            "filing_date": date(2024, 4, 30),
            "accepted_at": datetime(2024, 4, 30, 18, 0, 0, tzinfo=UTC),
            "observed_at": datetime(2024, 8, 1, 0, 0, 0, tzinfo=UTC),
            "availability_anchor": (
                datetime(2024, 8, 1, 0, 0, 0, tzinfo=UTC)
                if availability_policy == "observation-only"
                else datetime(2024, 4, 30, 18, 0, 0, tzinfo=UTC)
            ),
            "availability_basis": (
                "OBSERVATION_DATE_ONLY"
                if availability_policy == "observation-only"
                else "ACCEPTED_AT_PLUS_LAG_V1"
            ),
            "availability_precision": "DAY",
            "availability_policy": availability_policy,
            "availability_lag_days": lag_days or 0,
            "artifact_sha256": ref.sha256,
            "artifact_manifest_sha256": ref.manifest_sha256,
            "payload_hash": "p1",
            "vintage_identity": f"v-{cik}-{target_quarter}",
            "universe_hash": universe.universe_hash,
            "quality_flags": [],
        }
    ]

    default_holdings = [
        {
            **{key: None for key in HOLDING_COLUMNS},
            "fund_symbol": symbol,
            "cik": cik,
            "series_id": series_id,
            "accession_number": f"{cik}-24-000001",
            "report_date": date(2024, 3, 31),
            "holding_id": "1",
            "issuer_name": "CORP A",
            "issuer_cusip": "000000101",
            "currency_code": "USD",
            "balance": Decimal(100),
            "currency_value": Decimal(1000),
            "exchange_rate": Decimal(1),
            "percentage": Decimal("60.5"),
            "asset_cat": "EQ",
            "is_restricted_security": "N",
            "artifact_sha256": ref.sha256,
            "payload_hash": "p1",
            "vintage_identity": f"v-{cik}-{target_quarter}",
        },
        {
            **{key: None for key in HOLDING_COLUMNS},
            "fund_symbol": symbol,
            "cik": cik,
            "series_id": series_id,
            "accession_number": f"{cik}-24-000001",
            "report_date": date(2024, 3, 31),
            "holding_id": "2",
            "issuer_name": "CORP B",
            "issuer_cusip": "000000102",
            "currency_code": "USD",
            "balance": Decimal(50),
            "currency_value": Decimal(500),
            "exchange_rate": Decimal(1),
            "percentage": Decimal("39.5"),
            "asset_cat": "CSH",
            "is_restricted_security": "N",
            "artifact_sha256": ref.sha256,
            "payload_hash": "p1",
            "vintage_identity": f"v-{cik}-{target_quarter}",
        },
    ]

    default_identifiers = [
        {
            **{key: None for key in IDENTIFIER_COLUMNS},
            "fund_symbol": symbol,
            "cik": cik,
            "series_id": series_id,
            "accession_number": f"{cik}-24-000001",
            "report_date": date(2024, 3, 31),
            "holding_id": "1",
            "identifiers_id": "1",
            "identifier_isin": "US0000001010",
            "identifier_ticker": "CRPA",
            "artifact_sha256": ref.sha256,
            "vintage_identity": f"v-{cik}-{target_quarter}",
        }
    ]

    v_to_write = vintages_rows if vintages_rows is not None else default_vintages
    for v in v_to_write:
        if not v.get("universe_hash"):
            v["universe_hash"] = universe.universe_hash
        if not v.get("artifact_sha256"):
            v["artifact_sha256"] = ref.sha256
        if not v.get("artifact_manifest_sha256"):
            v["artifact_manifest_sha256"] = ref.manifest_sha256

    write_partition(
        root,
        source_quarter=str(target_quarter),
        artifact_sha256=ref.sha256,
        artifact_manifest_sha256=ref.manifest_sha256,
        universe_hash=universe.universe_hash,
        edgar_closure_hash=receipt["edgar_closure_hash"],
        parser_version="1",
        availability_policy=availability_policy,
        lag_days=lag_days,
        tables=(
            v_to_write,
            holdings_rows if holdings_rows is not None else default_holdings,
            identifiers_rows if identifiers_rows is not None else default_identifiers,
        ),
        quality={"scan_counts": {}, "qa": {}, "quality_flags": []},
    )

    return universe, u_path


# --- Test 1: Single Quarter End-to-End Success ---
def test_single_quarter_qualification_success(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    root = tmp_path / "dataset"
    out = tmp_path / "qualified"
    universe, _ = make_synthetic_env(root)

    ref = qualify_sec_nport(
        root=root,
        quarters=(Quarter(2024, 2),),
        universe=universe,
        availability_policy="observation-only",
        lag_days=None,
        output=out,
    )

    assert ref.status == "STRUCTURALLY_COMPLETE"
    assert (out / "qualification_receipt.json").is_file()
    assert (out / "qualification_request.json").is_file()
    assert (out / "quarter_fund_coverage.parquet").is_file()
    assert (out / "vintage_quality.parquet").is_file()
    assert (out / "amendment_facts.parquet").is_file()
    assert (out / "identifier_quality.parquet").is_file()

    rcp = ref.receipt
    assert rcp["schema_version"] == "sec-nport-qualification-receipt-v1"
    assert rcp["coverage_summary"]["requested_quarters"] == 1
    assert rcp["coverage_summary"]["expected_funds"] == 1
    assert rcp["coverage_summary"]["total_vintages"] == 1
    assert all(rcp["gates"].values())


# --- Test 2: Multi-Quarter Qualification ---
def test_multi_quarter_qualification_stitches_canonical_order(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    root = tmp_path / "dataset"
    out = tmp_path / "qualified"
    universe, _ = make_synthetic_env(root, quarter=Quarter(2024, 1))
    make_synthetic_env(root, quarter=Quarter(2024, 2))

    ref = qualify_sec_nport(
        root=root,
        quarters=(Quarter(2024, 1), Quarter(2024, 2)),
        universe=universe,
        availability_policy="observation-only",
        lag_days=None,
        output=out,
    )

    assert ref.status == "STRUCTURALLY_COMPLETE"
    assert ref.receipt["coverage_summary"]["requested_quarters"] == 2
    assert ref.receipt["coverage_summary"]["total_vintages"] == 2


# --- Test 3: Deterministic Byte & Table Identity ---
def test_qualification_determinism_across_separate_runs(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    root1 = tmp_path / "ds1"
    root2 = tmp_path / "ds2"
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"

    u1, _ = make_synthetic_env(root1)
    u2, _ = make_synthetic_env(root2)

    ref1 = qualify_sec_nport(
        root=root1,
        quarters=(Quarter(2024, 2),),
        universe=u1,
        availability_policy="observation-only",
        lag_days=None,
        output=out1,
    )
    ref2 = qualify_sec_nport(
        root=root2,
        quarters=(Quarter(2024, 2),),
        universe=u2,
        availability_policy="observation-only",
        lag_days=None,
        output=out2,
    )

    # Parquet outputs must have identical logical hashes and row counts
    for name in (
        "quarter_fund_coverage.parquet",
        "vintage_quality.parquet",
        "amendment_facts.parquet",
        "identifier_quality.parquet",
    ):
        art1 = ref1.receipt["output_artifacts"][name]
        art2 = ref2.receipt["output_artifacts"][name]
        assert art1["logical_hash"] == art2["logical_hash"]
        assert art1["row_count"] == art2["row_count"]
        assert art1["schema_fingerprint"] == art2["schema_fingerprint"]


# --- Test 4: Missing Partition -> CoverageError ---
def test_missing_partition_raises_coverage_error(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    root = tmp_path / "dataset"
    out = tmp_path / "qualified"
    universe, _ = make_synthetic_env(root, quarter=Quarter(2024, 2))

    with pytest.raises(CoverageError, match="quarter artifact missing"):
        qualify_sec_nport(
            root=root,
            quarters=(Quarter(2024, 1), Quarter(2024, 2)),
            universe=universe,
            availability_policy="observation-only",
            lag_days=None,
            output=out,
        )


# --- Test 5: Ambiguous Partitions & Partition Set Resolution ---
def test_ambiguous_partitions_and_partition_set_resolution(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    root = tmp_path / "dataset"
    out = tmp_path / "qualified"
    universe, _ = make_synthetic_env(root, quarter=Quarter(2024, 2))

    pq = pytest.importorskip("pyarrow.parquet")
    receipt_file = next((root / "state" / "fetch-receipts" / "2024q2").glob("*.json"))
    rcp_obj = json.loads(receipt_file.read_text())
    first_part = next(
        (root / "core" / "sec-fund-holdings-pit-v1" / "source_quarter=2024q2").glob(
            "artifact=*/partition=*"
        )
    )
    v_rows = pq.read_table(first_part / "fund_vintages.parquet").to_pylist()
    h_rows = pq.read_table(first_part / "holdings.parquet").to_pylist()
    id_rows = pq.read_table(first_part / "identifiers.parquet").to_pylist()

    raw_ref = SecNportBatch(root)._quarter_index().read()["entries"][0]
    write_partition(
        root,
        source_quarter="2024q2",
        artifact_sha256=raw_ref["artifact_sha256"],
        artifact_manifest_sha256=raw_ref["manifest_sha256"],
        universe_hash=universe.universe_hash,
        edgar_closure_hash=rcp_obj["edgar_closure_hash"],
        parser_version="2",
        availability_policy="observation-only",
        lag_days=None,
        tables=(v_rows, h_rows, id_rows),
        quality={"scan_counts": {}, "qa": {}, "quality_flags": []},
    )

    # Implicit selection must fail with AmbiguousPartitionError
    with pytest.raises(AmbiguousPartitionError, match="ambiguous"):
        qualify_sec_nport(
            root=root,
            quarters=(Quarter(2024, 2),),
            universe=universe,
            availability_policy="observation-only",
            lag_days=None,
            output=out,
        )

    # Providing canonical partition set resolves ambiguity
    catalog = root / "core" / "sec-fund-holdings-pit-v1" / "catalog.json"
    entries = json.loads(catalog.read_text())["entries"]
    first_entry = entries[0]
    pset = SecNportPartitionSet(
        (
            SecNportPartitionSetEntry(
                source_quarter="2024q2",
                partition_identity=first_entry["partition_identity"],
                manifest_hash=first_entry["manifest_hash"],
            ),
        )
    )

    ref = qualify_sec_nport(
        root=root,
        quarters=(Quarter(2024, 2),),
        universe=universe,
        availability_policy="observation-only",
        lag_days=None,
        output=out,
        partition_set=pset,
    )
    assert ref.status == "STRUCTURALLY_COMPLETE"


# --- Test 6: Missing Expected Fund -> STRUCTURALLY_PARTIAL ---
def test_missing_expected_fund_yields_structurally_partial(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    root = tmp_path / "dataset"
    out = tmp_path / "qualified"
    # Universe expects both TST and MISSING
    universe_funds = [
        {
            "symbol": "TST",
            "cik": "0000000001",
            "selection_mode": "single_series_cik",
            "series_id": None,
            "valid_from_quarter": None,
            "valid_to_quarter": None,
        },
        {
            "symbol": "MISSING",
            "cik": "0000000002",
            "selection_mode": "single_series_cik",
            "series_id": None,
            "valid_from_quarter": None,
            "valid_to_quarter": None,
        },
    ]
    universe, _ = make_synthetic_env(root, universe_funds=universe_funds)

    ref = qualify_sec_nport(
        root=root,
        quarters=(Quarter(2024, 2),),
        universe=universe,
        availability_policy="observation-only",
        lag_days=None,
        output=out,
    )
    assert ref.status == "STRUCTURALLY_PARTIAL"
    assert ref.receipt["gates"]["EXPECTED_FUND_QUARTERS_ACCOUNTED"] is False


# --- Test 7: Unexpected Fund Symbol -> SnapshotIntegrityError ---
def test_unexpected_fund_symbol_fails_fast(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    root = tmp_path / "dataset"
    out = tmp_path / "qualified"
    vintages = [
        {
            **{key: None for key in FUND_COLUMNS},
            "provider": "sec",
            "fund_symbol": "UNEXPECTED",
            "cik": "0000000099",
            "accession_number": "0000000099-24-000001",
            "submission_type": "NPORT-P",
            "report_date": date(2024, 3, 31),
            "filing_date": date(2024, 4, 30),
            "observed_at": datetime(2024, 8, 1, tzinfo=UTC),
            "availability_basis": "OBSERVATION_DATE_ONLY",
            "availability_precision": "DAY",
            "availability_policy": "observation-only",
            "availability_lag_days": 0,
            "artifact_sha256": "sha",
            "payload_hash": "p",
            "vintage_identity": "v-unexp",
            "universe_hash": "u",
            "quality_flags": [],
        }
    ]
    universe, _ = make_synthetic_env(root, vintages_rows=vintages)

    with pytest.raises(SnapshotIntegrityError, match="outside reviewed active universe"):
        qualify_sec_nport(
            root=root,
            quarters=(Quarter(2024, 2),),
            universe=universe,
            availability_policy="observation-only",
            lag_days=None,
            output=out,
        )


# --- Test 8: Amendment Families Ordering & Relations ---
def test_amendment_families_relations(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    root = tmp_path / "dataset"
    out = tmp_path / "qualified"

    raw_ref_sha = "sha_test"
    vintages = [
        # Original filing
        {
            **{key: None for key in FUND_COLUMNS},
            "provider": "sec",
            "fund_symbol": "TST",
            "cik": "0000000001",
            "accession_number": "0000000001-24-000001",
            "submission_type": "NPORT-P",
            "report_date": date(2024, 3, 31),
            "filing_date": date(2024, 4, 30),
            "accepted_at": datetime(2024, 4, 30, 17, 0, 0, tzinfo=UTC),
            "observed_at": datetime(2024, 8, 1, tzinfo=UTC),
            "availability_anchor": datetime(2024, 4, 30, 17, 0, 0, tzinfo=UTC),
            "availability_basis": "ACCEPTED_AT_PLUS_LAG_V1",
            "availability_precision": "SECOND",
            "availability_policy": "accepted-at-plus-lag",
            "availability_lag_days": 1,
            "artifact_sha256": raw_ref_sha,
            "payload_hash": "p1",
            "vintage_identity": "v1",
            "quality_flags": [],
        },
        # Amendment filing
        {
            **{key: None for key in FUND_COLUMNS},
            "provider": "sec",
            "fund_symbol": "TST",
            "cik": "0000000001",
            "accession_number": "0000000001-24-000002",
            "submission_type": "NPORT-P/A",
            "report_date": date(2024, 3, 31),
            "filing_date": date(2024, 5, 15),
            "accepted_at": datetime(2024, 5, 15, 18, 0, 0, tzinfo=UTC),
            "observed_at": datetime(2024, 8, 1, tzinfo=UTC),
            "availability_anchor": datetime(2024, 5, 15, 18, 0, 0, tzinfo=UTC),
            "availability_basis": "ACCEPTED_AT_PLUS_LAG_V1",
            "availability_precision": "SECOND",
            "availability_policy": "accepted-at-plus-lag",
            "availability_lag_days": 1,
            "artifact_sha256": raw_ref_sha,
            "payload_hash": "p2",
            "vintage_identity": "v2",
            "quality_flags": [],
        },
    ]
    universe, _ = make_synthetic_env(
        root,
        availability_policy="accepted-at-plus-lag",
        lag_days=1,
        vintages_rows=vintages,
    )

    ref = qualify_sec_nport(
        root=root,
        quarters=(Quarter(2024, 2),),
        universe=universe,
        availability_policy="accepted-at-plus-lag",
        lag_days=1,
        output=out,
    )
    assert ref.status == "STRUCTURALLY_COMPLETE"

    tbl = pq.read_table(out / "amendment_facts.parquet").to_pylist()
    assert len(tbl) == 2
    assert tbl[0]["relation_basis"] == "ORIGINAL_IN_FAMILY"
    assert tbl[0]["family_order"] == 0
    assert tbl[0]["predecessor_accession_number"] is None

    assert tbl[1]["relation_basis"] == "FORM_AND_ORDER_INFERRED"
    assert tbl[1]["family_order"] == 1
    assert tbl[1]["predecessor_accession_number"] == "0000000001-24-000001"


# --- Test 9: Weight Facts & Decimal Arithmetic ---
def test_weight_facts_precision_and_categories(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    root = tmp_path / "dataset"
    out = tmp_path / "qualified"

    holdings = [
        {
            **{key: None for key in HOLDING_COLUMNS},
            "fund_symbol": "TST",
            "cik": "0000000001",
            "accession_number": "0000000001-24-000001",
            "report_date": date(2024, 3, 31),
            "holding_id": "1",
            "percentage": Decimal("40.123456789012345678"),
            "asset_cat": "DBT",
            "is_restricted_security": "Y",
            "derivative_cat": "FWD",
            "artifact_sha256": "sha",
            "payload_hash": "p",
            "vintage_identity": "v-0000000001-2024q2",
        },
        {
            **{key: None for key in HOLDING_COLUMNS},
            "fund_symbol": "TST",
            "cik": "0000000001",
            "accession_number": "0000000001-24-000001",
            "report_date": date(2024, 3, 31),
            "holding_id": "2",
            "percentage": Decimal("59.876543210987654322"),
            "asset_cat": "CSH",
            "artifact_sha256": "sha",
            "payload_hash": "p",
            "vintage_identity": "v-0000000001-2024q2",
        },
    ]

    universe, _ = make_synthetic_env(root, holdings_rows=holdings)

    ref = qualify_sec_nport(
        root=root,
        quarters=(Quarter(2024, 2),),
        universe=universe,
        availability_policy="observation-only",
        lag_days=None,
        output=out,
    )
    assert ref.status == "STRUCTURALLY_COMPLETE"

    vq = pq.read_table(out / "vintage_quality.parquet").to_pylist()[0]
    assert vq["percentage_sum"] == Decimal(100)
    assert vq["debt_like_rows_count"] == 1
    assert vq["debt_like_weight"] == Decimal("40.123456789012345678")
    assert vq["cash_like_rows_count"] == 1
    assert vq["cash_like_weight"] == Decimal("59.876543210987654322")
    assert vq["restricted_rows_count"] == 1
    assert vq["derivative_rows_count"] == 1


# --- Test 10: Identifier Facts & Multiplicity ---
def test_identifier_quality_multiplicity_and_conflicts(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    root = tmp_path / "dataset"
    out = tmp_path / "qualified"

    holdings = [
        {
            **{key: None for key in HOLDING_COLUMNS},
            "fund_symbol": "TST",
            "cik": "0000000001",
            "accession_number": "0000000001-24-000001",
            "report_date": date(2024, 3, 31),
            "holding_id": "1",
            "issuer_name": "CORP A",
            "issuer_cusip": "SHARED_CUSIP",
            "percentage": Decimal(50),
            "artifact_sha256": "sha",
            "payload_hash": "p",
            "vintage_identity": "v-0000000001-2024q2",
        },
        {
            **{key: None for key in HOLDING_COLUMNS},
            "fund_symbol": "TST",
            "cik": "0000000001",
            "accession_number": "0000000001-24-000001",
            "report_date": date(2024, 3, 31),
            "holding_id": "2",
            "issuer_name": "CORP B",
            "issuer_cusip": "SHARED_CUSIP",  # Shared CUSIP with different holding and issuer
            "percentage": Decimal(50),
            "artifact_sha256": "sha",
            "payload_hash": "p",
            "vintage_identity": "v-0000000001-2024q2",
        },
    ]

    identifiers = [
        {
            **{key: None for key in IDENTIFIER_COLUMNS},
            "fund_symbol": "TST",
            "cik": "0000000001",
            "accession_number": "0000000001-24-000001",
            "report_date": date(2024, 3, 31),
            "holding_id": "1",
            "identifiers_id": "1",
            "identifier_ticker": "TICK1",
            "artifact_sha256": "sha",
            "vintage_identity": "v-0000000001-2024q2",
        },
        {
            **{key: None for key in IDENTIFIER_COLUMNS},
            "fund_symbol": "TST",
            "cik": "0000000001",
            "accession_number": "0000000001-24-000001",
            "report_date": date(2024, 3, 31),
            "holding_id": "1",
            "identifiers_id": "2",
            "identifier_ticker": "TICK2",  # Multiple tickers on holding 1
            "artifact_sha256": "sha",
            "vintage_identity": "v-0000000001-2024q2",
        },
    ]

    universe, _ = make_synthetic_env(root, holdings_rows=holdings, identifiers_rows=identifiers)

    ref = qualify_sec_nport(
        root=root,
        quarters=(Quarter(2024, 2),),
        universe=universe,
        availability_policy="observation-only",
        lag_days=None,
        output=out,
    )
    assert ref.status == "STRUCTURALLY_COMPLETE"

    iq = pq.read_table(out / "identifier_quality.parquet").to_pylist()[0]
    assert iq["total_holding_count"] == 2
    assert iq["single_identifier_holding_count"] == 0
    assert iq["multi_identifier_holding_count"] == 1  # holding 1 has 2 identifiers
    assert iq["zero_identifier_holding_count"] == 1  # holding 2 has 0 identifiers
    assert iq["multi_value_same_type_holding_count"] == 1  # holding 1 has 2 tickers
    assert iq["duplicate_identifier_value_count"] == 1  # SHARED_CUSIP on 2 holdings
    assert iq["multi_issuer_name_identifier_count"] == 1  # SHARED_CUSIP on CORP A and CORP B


# --- Test 11: Output Directory Already Exists -> Fails Fast ---
def test_output_directory_already_exists_fails_fast(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    root = tmp_path / "dataset"
    out = tmp_path / "qualified"
    out.mkdir()
    universe, _ = make_synthetic_env(root)

    with pytest.raises(FileExistsError):
        qualify_sec_nport(
            root=root,
            quarters=(Quarter(2024, 2),),
            universe=universe,
            availability_policy="observation-only",
            lag_days=None,
            output=out,
        )


# --- Test 12: Post-Persist Replay Catches Tampered File ---
def test_post_persist_replay_catches_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("pyarrow")
    root = tmp_path / "dataset"
    out = tmp_path / "qualified"
    universe, _ = make_synthetic_env(root)

    # Monkeypatch write_qualification_bundle to simulate disk tampering before verification
    import ohmydata.providers.sec.qualification as q_mod

    original_write = q_mod.write_qualification_bundle

    def tampered_write(**kwargs: Any) -> dict[str, Any]:
        res = original_write(**kwargs)
        # Corrupt one file after write
        (kwargs["output_dir"] / "quarter_fund_coverage.parquet").write_bytes(b"corrupted")
        return res

    monkeypatch.setattr(q_mod, "write_qualification_bundle", tampered_write)

    with pytest.raises(SnapshotIntegrityError, match="hash or byte mismatch"):
        qualify_sec_nport(
            root=root,
            quarters=(Quarter(2024, 2),),
            universe=universe,
            availability_policy="observation-only",
            lag_days=None,
            output=out,
        )


# --- Test 13: Deadline Exceeded -> ResourceLimitError ---
def test_deadline_exceeded_raises_resource_limit_error(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    root = tmp_path / "dataset"
    out = tmp_path / "qualified"
    universe, _ = make_synthetic_env(root)

    deadline = Deadline(timeout_seconds=-1.0)
    with pytest.raises(ResourceLimitError, match="deadline exceeded"):
        qualify_sec_nport(
            root=root,
            quarters=(Quarter(2024, 2),),
            universe=universe,
            availability_policy="observation-only",
            lag_days=None,
            output=out,
            deadline=deadline,
        )


# --- Test 14: Structural Counters ---
def test_structural_counters_recorded(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    root = tmp_path / "dataset"
    out = tmp_path / "qualified"
    universe, _ = make_synthetic_env(root)

    ref = qualify_sec_nport(
        root=root,
        quarters=(Quarter(2024, 2),),
        universe=universe,
        availability_policy="observation-only",
        lag_days=None,
        output=out,
    )
    c = ref.counters
    assert c["partitions_selected"] == 1
    assert c["fund_vintage_rows_read"] == 1
    assert c["holding_rows_read"] == 2
    assert c["identifier_rows_read"] == 1
    assert c["table_scans"] == 3
    assert c["replay_table_scans"] == 4
    assert c["replay_rows_read"] == c["qualification_rows_written"]


# --- Test 15: CLI Integration ---
def test_cli_qualify_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("pyarrow")
    from ohmydata.cli import main

    root = tmp_path / "dataset"
    out = tmp_path / "qualified"
    _universe, u_path = make_synthetic_env(root)

    rc = main(
        [
            "sec",
            "nport",
            "qualify",
            "--root",
            str(root),
            "--quarters",
            "2024q2",
            "--universe",
            str(u_path),
            "--availability-policy",
            "observation-only",
            "--output",
            str(out),
            "--json",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["qualification_status"] == "STRUCTURALLY_COMPLETE"

"""Offline tests for CIK fetching/partitioning, incremental batches, FPI/IFRS, and ADR metadata."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ohmydata.providers.sec._statement_parser import SecUnitEvidenceError, parse_statement_rows
from ohmydata.providers.sec.adr_metadata import (
    SEC_ADR_METADATA_SCHEMA,
    SecAdrShareMetadata,
    SecDeiSharesMetadata,
    SecShareBasis,
    convert_ordinary_shares_to_adr_equivalent,
    extract_dei_shares_metadata,
)
from ohmydata.providers.sec.edgartools_adapter import SecFinancialsClient
from ohmydata.providers.sec.financials import (
    FINANCIALS_DATASET_SCHEMA_V5,
    SecCompanyFinancialVintage,
    SecFinancialsRequest,
    SecStatementRow,
    normalize_sec_cik,
)
from ohmydata.providers.sec.financials_dataset import (
    list_financials_incremental_batches,
    read_cik_financials_partition,
    validate_cik_financials_partition,
    validate_financials_incremental_batch,
    validate_financials_partition,
    write_cik_financials_partition,
    write_financials_incremental_batch,
    write_financials_partition,
)
from ohmydata.providers.sec.unit_evidence import (
    SEC_LIVE_FINANCIAL_PARSER_V2,
    SecFinancialUnitEvidence,
)

V1 = "sec-live-financial-parser-v1-edgartools-6.4.0"
V2 = SEC_LIVE_FINANCIAL_PARSER_V2


def _rehash_manifest(directory: Path, filename: str) -> None:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target = directory / filename
    manifest["files"][filename] = {
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "bytes": target.stat().st_size,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _sample_row(
    *,
    concept: str = "ifrs-full:Revenue",
    standard_concept: str = "Revenues",
    unit: str = "iso4217:TWD",
    value: str = "650000000000",
) -> SecStatementRow:
    return SecStatementRow(
        statement_type="income_statement",
        standard_concept=standard_concept,
        concept=concept,
        label="Revenue",
        value=Decimal(value),
        value_native=value,
        unit=unit,
        decimals=-6,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_type="duration",
        period_key="duration_2024-01-01_2024-12-31",
        context_ref="c1",
        unit_ref="u1",
        decimals_native="-6",
        period_source="xbrl-context",
    )


def _sample_adr_metadata(*, currency: str = "TWD") -> SecAdrShareMetadata:
    return SecAdrShareMetadata(
        reporting_currency=currency,
        share_basis=SecShareBasis.ORDINARY_SHARES,
        ordinary_shares_per_adr=Decimal(5),
        ratio_source="20-f-cover-page",
        dei_shares=SecDeiSharesMetadata(
            shares_outstanding=Decimal(25932071000),
            as_of_date=date(2024, 12, 31),
            unit="shares",
            native_tag="dei:EntityCommonStockSharesOutstanding",
            context_ref="c-dei",
        ),
    )


def _sample_vintage(
    *,
    symbol: str = "TSM",
    cik: str = "0001046179",
    form: str = "20-F",
    accession: str = "0001046179-25-000010",
    filing_date: date = date(2025, 4, 17),
    rows: tuple[SecStatementRow, ...] | None = None,
    unit_evidence: SecFinancialUnitEvidence | None = None,
    adr_metadata: SecAdrShareMetadata | None = None,
) -> SecCompanyFinancialVintage:
    return SecCompanyFinancialVintage(
        symbol=symbol,
        cik=cik,
        company_name="Taiwan Semiconductor Manufacturing Co Ltd",
        form=form,
        accession_number=accession,
        filing_date=filing_date,
        period_end=date(2024, 12, 31),
        accepted_at=datetime(2025, 4, 17, 10, 30, 0, tzinfo=UTC),
        rows=(_sample_row(),) if rows is None else rows,
        unit_evidence=unit_evidence,
        adr_metadata=adr_metadata,
    )


# ---------------------------------------------------------------------------
# Requirement #1: SecFinancialsRequest (symbols + ciks) & CIK normalization
# ---------------------------------------------------------------------------


def test_request_accepts_ciks_only_symbols_only_or_both_and_canonicalizes() -> None:
    req_pos = SecFinancialsRequest(("AAPL",))
    assert req_pos.symbols == ("AAPL",)
    assert req_pos.ciks == ()

    req_cik = SecFinancialsRequest(ciks=("320193", "0001046179"))
    assert req_cik.symbols == ()
    assert req_cik.ciks == ("0000320193", "0001046179")

    req_both = SecFinancialsRequest(symbols=("AAPL",), ciks=("789019",))
    assert req_both.symbols == ("AAPL",)
    assert req_both.ciks == ("0000789019",)

    assert normalize_sec_cik(" 1046179 ") == "0001046179"


@pytest.mark.parametrize(
    "bad_cik",
    ["", "   ", "0", "0000000000", "abc", "-1", "12345678901", "٣٢٠١٩٣", "320193.0"],
)
def test_request_rejects_invalid_ciks(bad_cik: str) -> None:
    with pytest.raises(ValueError, match="invalid CIK"):
        SecFinancialsRequest(ciks=(bad_cik,))


def test_request_rejects_empty_symbols_and_empty_ciks() -> None:
    with pytest.raises(ValueError, match="symbols cannot be empty"):
        SecFinancialsRequest()


@pytest.mark.parametrize(
    "form",
    ["10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A", "6-K"],
)
def test_request_accepts_domestic_and_fpi_forms(form: str) -> None:
    req = SecFinancialsRequest(ciks=("1046179",), forms=(form,))
    assert req.forms == (form,)


@pytest.mark.parametrize("bad_form", ["8-K", "6-K/A", "S-1", "DEF 14A"])
def test_request_rejects_unsupported_forms(bad_form: str) -> None:
    with pytest.raises(ValueError, match="supported SEC financial forms"):
        SecFinancialsRequest(ciks=("1046179",), forms=(bad_form,))


# ---------------------------------------------------------------------------
# Requirement #1: edgartools_adapter CIK fetching & delisted tickerless issuer
# ---------------------------------------------------------------------------


def test_client_fetches_by_cik_including_delisted_company_without_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called_targets: list[str] = []

    statement = SimpleNamespace(
        get_raw_data=lambda: [
            {
                "concept": "us-gaap_Revenues",
                "label": "Revenue",
                "values": {"duration_2022-01-01_2022-12-31": 900000},
                "unit": "USD",
            }
        ]
    )
    fin = SimpleNamespace(
        balance_sheet=lambda: None,
        income_statement=lambda: statement,
        cash_flow_statement=lambda: None,
    )
    filing = SimpleNamespace(
        form="10-K",
        filing_date="2023-03-01",
        cik="999999",
        company="Delisted Synthetic Corp",
        accession_number="0000999999-23-000001",
        period_of_report="2022-12-31",
        header=SimpleNamespace(acceptance_datetime=datetime(2023, 3, 1, 21, 0, tzinfo=UTC)),
        obj=lambda: SimpleNamespace(financials=fin),
    )

    class FakeCompany:
        def __init__(self, target: str) -> None:
            called_targets.append(target)
            self.ticker = None
            self.tickers: list[str] = []
            self.name = "Delisted Synthetic Corp"

        def get_ticker(self) -> str | None:
            return None

        def get_filings(self, **kwargs: Any) -> list[Any]:
            return [filing]

    monkeypatch.setattr("edgar.Company", FakeCompany)
    monkeypatch.setattr("edgar.set_identity", lambda _: None)

    client = SecFinancialsClient("Synthetic test@example.invalid")
    vintages = client.fetch_company_financials(
        SecFinancialsRequest(ciks=("999999",), forms=("10-K",), parser_version=V1)
    )

    assert called_targets == ["0000999999"]
    assert len(vintages) == 1
    assert vintages[0].cik == "0000999999"
    assert vintages[0].symbol == ""
    assert vintages[0].company_name == "Delisted Synthetic Corp"
    assert len(vintages[0].rows) == 1


# ---------------------------------------------------------------------------
# Requirement #1: CIK-partitioned Parquet write / validate / read & tamper tests
# ---------------------------------------------------------------------------


def test_cik_partition_write_validate_read_and_immutability(tmp_path: Path) -> None:
    adr_meta = _sample_adr_metadata(currency="TWD")
    ev = SecFinancialUnitEvidence(
        parser_version=V2,
        cik="1046179",
        accession_number="0001046179-25-000010",
        instance_document="tsm-20241231_htm.xml",
        instance_sha256="c" * 64,
    )
    vintage = _sample_vintage(unit_evidence=ev, adr_metadata=adr_meta)

    manifest = write_cik_financials_partition(
        tmp_path,
        "1046179",
        [vintage],
        symbols=["TSM", "2330"],
    )
    partition = tmp_path / "cik=0001046179"
    assert partition.is_dir()
    assert manifest["partition_key"] == "cik"
    assert manifest["cik"] == "0001046179"
    assert manifest["symbols"] == ["2330", "TSM"]
    assert manifest["dataset_schema"] == FINANCIALS_DATASET_SCHEMA_V5
    assert manifest["vintage_count"] == 1
    assert manifest["statement_row_count"] == 1

    # Idempotent rewrite returns exact same manifest
    assert (
        write_cik_financials_partition(tmp_path, "0001046179", [vintage], symbols=["2330", "TSM"])
        == manifest
    )

    # Validate & round-trip read back
    assert validate_cik_financials_partition(partition) == manifest
    read_manifest, read_vintages = read_cik_financials_partition(partition)
    assert read_manifest == manifest
    assert len(read_vintages) == 1
    restored = read_vintages[0]
    assert restored == vintage
    assert restored.vintage_identity == vintage.vintage_identity
    assert restored.adr_metadata == adr_meta
    assert restored.unit_evidence == ev

    # Collision with differing vintages fails closed without mutating files
    before = {p.name: p.read_bytes() for p in partition.iterdir()}
    changed = _sample_vintage(
        rows=(_sample_row(value="650000000001"),),
        unit_evidence=ev,
        adr_metadata=adr_meta,
    )
    with pytest.raises(ValueError, match="immutable"):
        write_cik_financials_partition(tmp_path, "0001046179", [changed], symbols=["2330", "TSM"])
    assert {p.name: p.read_bytes() for p in partition.iterdir()} == before


def test_cik_partition_delisted_empty_symbol_and_tamper_detection(tmp_path: Path) -> None:
    delisted = _sample_vintage(symbol="", cik="0000999999", accession="0000999999-23-000001")
    manifest = write_cik_financials_partition(tmp_path, "999999", [delisted])
    assert manifest["symbols"] == []
    partition = tmp_path / "cik=0000999999"
    _, restored = read_cik_financials_partition(partition)
    assert restored[0].symbol == ""

    # Hash-correct tampering of adr_metadata_payload fails closed
    adr_root = tmp_path / "adr_tamper"
    write_cik_financials_partition(
        adr_root,
        "1046179",
        [_sample_vintage(adr_metadata=_sample_adr_metadata(currency="TWD"))],
    )
    adr_part = adr_root / "cik=0001046179"
    v_path = adr_part / "company_financial_vintages.parquet"
    v_table = pq.ParquetFile(v_path).read()
    rows = v_table.to_pylist()
    payload = json.loads(rows[0]["adr_metadata_payload"])
    payload["reporting_currency"] = "USD"
    rows[0]["adr_metadata_payload"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    pq.write_table(pa.Table.from_pylist(rows, schema=v_table.schema), v_path)
    _rehash_manifest(adr_part, v_path.name)
    with pytest.raises(ValueError, match="ADR metadata identity mismatch"):
        validate_cik_financials_partition(adr_part)


# ---------------------------------------------------------------------------
# Requirement #1 / P0 #4: Immutable incremental batch writer & validator
# ---------------------------------------------------------------------------


def test_incremental_batch_multi_cik_immutability_and_listing(tmp_path: Path) -> None:
    v1 = _sample_vintage(
        symbol="TSM",
        cik="0001046179",
        accession="0001046179-25-000010",
        adr_metadata=_sample_adr_metadata(currency="TWD"),
    )
    v2 = _sample_vintage(
        symbol="ASML",
        cik="0000937966",
        accession="0000937966-25-000005",
        rows=(_sample_row(unit="iso4217:EUR", value="28263000000"),),
        adr_metadata=SecAdrShareMetadata(
            reporting_currency="EUR",
            share_basis=SecShareBasis.ORDINARY_SHARES,
        ),
    )

    m1 = write_financials_incremental_batch(
        tmp_path,
        "sync-20260401T120000Z-001",
        [v1, v2],
        metadata={"run_id": "run-1", "source": "incremental"},
    )
    batch_dir = tmp_path / "batches" / "sync-20260401T120000Z-001"
    assert batch_dir.is_dir()
    assert m1["partition_key"] == "batch"
    assert m1["batch_id"] == "sync-20260401T120000Z-001"
    assert m1["ciks"] == ["0000937966", "0001046179"]
    assert m1["symbols"] == ["ASML", "TSM"]
    assert m1["accession_numbers"] == ["0000937966-25-000005", "0001046179-25-000010"]
    assert m1["vintage_count"] == 2
    assert m1["statement_row_count"] == 2
    assert m1["metadata"] == {"run_id": "run-1", "source": "incremental"}

    assert validate_financials_incremental_batch(batch_dir) == m1
    # Idempotent rewrite
    assert (
        write_financials_incremental_batch(
            tmp_path,
            "sync-20260401T120000Z-001",
            [v2, v1],  # Different input order still sorts deterministically!
            metadata={"source": "incremental", "run_id": "run-1"},
        )
        == m1
    )
    # Differing content on same batch_id fails closed
    with pytest.raises(ValueError, match="immutable"):
        write_financials_incremental_batch(tmp_path, "sync-20260401T120000Z-001", [v1])

    # Second batch + listing
    write_financials_incremental_batch(tmp_path, "sync-20260402T120000Z-002", [v1])
    listed = list_financials_incremental_batches(tmp_path)
    assert [p.name for p in listed] == [
        "sync-20260401T120000Z-001",
        "sync-20260402T120000Z-002",
    ]


@pytest.mark.parametrize("bad_batch_id", ["", ".", "..", "../escape", "a/b", ".hidden", "bad id"])
def test_incremental_batch_rejects_unsafe_batch_id(tmp_path: Path, bad_batch_id: str) -> None:
    with pytest.raises(ValueError, match="unsafe incremental batch_id"):
        write_financials_incremental_batch(tmp_path, bad_batch_id, [])


# ---------------------------------------------------------------------------
# Requirement #2: Foreign ADR forms (20-F/40-F/6-K) + amendment linking + IFRS
# ---------------------------------------------------------------------------


def test_fpi_amendment_linking_20f_and_40f(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [
        SimpleNamespace(
            form="20-F/A",
            filing_date="2025-05-01",
            cik="1046179",
            company="TSMC",
            accession_number="0001046179-25-000020",
            period_of_report="2024-12-31",
            header=SimpleNamespace(acceptance_datetime=datetime(2025, 5, 1, 12, 0, tzinfo=UTC)),
            obj=lambda: None,
        ),
        SimpleNamespace(
            form="20-F",
            filing_date="2025-04-17",
            cik="1046179",
            company="TSMC",
            accession_number="0001046179-25-000010",
            period_of_report="2024-12-31",
            header=SimpleNamespace(acceptance_datetime=datetime(2025, 4, 17, 12, 0, tzinfo=UTC)),
            obj=lambda: None,
        ),
        SimpleNamespace(
            form="40-F/A",
            filing_date="2025-05-02",
            cik="1046179",
            company="TSMC",
            accession_number="0001046179-25-000021",
            period_of_report="2024-12-31",
            header=SimpleNamespace(acceptance_datetime=datetime(2025, 5, 2, 12, 0, tzinfo=UTC)),
            obj=lambda: None,
        ),
        SimpleNamespace(
            form="40-F",
            filing_date="2025-04-18",
            cik="1046179",
            company="TSMC",
            accession_number="0001046179-25-000011",
            period_of_report="2024-12-31",
            header=SimpleNamespace(acceptance_datetime=datetime(2025, 4, 18, 12, 0, tzinfo=UTC)),
            obj=lambda: None,
        ),
    ]
    monkeypatch.setattr(
        "edgar.Company",
        lambda _: SimpleNamespace(get_filings=lambda **kw: entries, ticker="TSM"),
    )
    monkeypatch.setattr("edgar.set_identity", lambda _: None)

    client = SecFinancialsClient("Synthetic test@example.invalid")
    vintages = client.fetch_company_financials(
        SecFinancialsRequest(ciks=("1046179",), forms=("20-F", "40-F"), parser_version=V1)
    )
    by_acc = {v.accession_number: v for v in vintages}
    assert by_acc["0001046179-25-000020"].base_filing_accession_number == "0001046179-25-000010"
    assert by_acc["0001046179-25-000021"].base_filing_accession_number == "0001046179-25-000011"


def test_non_xbrl_6k_emits_explicit_flags_without_crashing_in_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    press_release_6k = SimpleNamespace(
        form="6-K",
        filing_date="2025-07-17",
        cik="1046179",
        company="TSMC",
        accession_number="0001046179-25-000050",
        period_of_report="2025-06-30",
        header=SimpleNamespace(acceptance_datetime=datetime(2025, 7, 17, 11, 0, tzinfo=UTC)),
        attachments=SimpleNamespace(data_files=[]),
        homepage=SimpleNamespace(data_files=[]),
        obj=lambda: (_ for _ in ()).throw(AssertionError("non-XBRL 6-K should not parse obj")),
    )
    monkeypatch.setattr(
        "edgar.Company",
        lambda _: SimpleNamespace(get_filings=lambda **kw: [press_release_6k], ticker="TSM"),
    )
    monkeypatch.setattr("edgar.set_identity", lambda _: None)

    client = SecFinancialsClient("Synthetic test@example.invalid")
    vintages = client.fetch_company_financials(
        SecFinancialsRequest(ciks=("1046179",), forms=("6-K",), parser_version=V2)
    )
    assert len(vintages) == 1
    v = vintages[0]
    assert v.form == "6-K"
    assert v.rows == ()
    assert v.unit_evidence is None
    assert "NON_XBRL_6K" in v.quality_flags
    assert "NO_XBRL_ATTACHMENT" in v.quality_flags
    assert "NO_FINANCIAL_STATEMENTS" in v.quality_flags


@pytest.mark.parametrize("currency", ["EUR", "TWD", "CNY", "DKK", "GBP", "JPY"])
def test_statement_parser_supports_ifrs_full_and_preserves_native_currency(currency: str) -> None:
    statement = SimpleNamespace(
        get_raw_data=lambda: [
            {
                "concept": "ifrs-full:Revenue",
                "label": "Revenue",
                "values": {"duration_2024-01-01_2024-12-31": 12345000000},
                "unit": f"iso4217:{currency}",
            },
            {
                "concept": "ifrs-full_ProfitLossFromOperatingActivities",
                "label": "Operating profit",
                "values": {"duration_2024-01-01_2024-12-31": 4560000000},
                "unit": f"iso4217:{currency}",
            },
            {
                "concept": "ifrs-full:ProfitLoss",
                "label": "Net profit",
                "values": {"duration_2024-01-01_2024-12-31": 3890000000},
                "unit": f"iso4217:{currency}",
            },
            {
                "concept": "ifrs-full:CashFlowsFromUsedInOperatingActivities",
                "label": "Operating cash flows",
                "values": {"duration_2024-01-01_2024-12-31": 5100000000},
                "unit": f"iso4217:{currency}",
            },
            {
                "concept": "ifrs-full:CustomForeignMetric",
                "label": "Custom IFRS item",
                "values": {"duration_2024-01-01_2024-12-31": 42},
                "unit": f"iso4217:{currency}",
            },
        ]
    )
    rows = parse_statement_rows(statement, "income_statement")
    by_concept = {r.concept: r for r in rows}

    assert by_concept["ifrs-full:Revenue"].standard_concept == "Revenues"
    assert by_concept["ifrs-full_ProfitLossFromOperatingActivities"].standard_concept == (
        "OperatingIncomeLoss"
    )
    assert by_concept["ifrs-full:ProfitLoss"].standard_concept == "NetIncomeLoss"
    assert by_concept["ifrs-full:CashFlowsFromUsedInOperatingActivities"].standard_concept == (
        "OperatingCashFlow"
    )
    assert by_concept["ifrs-full:CustomForeignMetric"].standard_concept == "CustomForeignMetric"
    assert all(r.unit == f"iso4217:{currency}" for r in rows)
    assert all(r.currency == currency for r in rows)


def test_statement_parser_ifrs_display_dataframe_preserves_native_concept_and_currency() -> None:
    frame = pd.DataFrame(
        {
            "concept": ["ifrs-full:Assets", "ifrs-full:Borrowings"],
            "label": ["Total assets", "Borrowings"],
            "unit": ["DKK", "DKK"],
            "point_in_time": [True, True],
            "2024-12-31": [450000000000, 32000000000],
        }
    )
    rows = parse_statement_rows(SimpleNamespace(to_dataframe=lambda **kw: frame), "balance_sheet")
    assert [(r.concept, r.standard_concept, r.unit, r.currency) for r in rows] == [
        ("ifrs-full:Assets", "Assets", "DKK", "DKK"),
        ("ifrs-full:Borrowings", "TotalDebt", "DKK", "DKK"),
    ]


# ---------------------------------------------------------------------------
# Requirement #3 / P1 #5: ADR metadata, DEI cover-page shares & opt-in helper
# ---------------------------------------------------------------------------


def test_adr_metadata_models_identity_and_opt_in_conversion() -> None:
    adr = _sample_adr_metadata(currency="TWD")
    assert adr.canonical_payload["schema_version"] == SEC_ADR_METADATA_SCHEMA
    assert SecAdrShareMetadata.from_payload(adr.canonical_payload) == adr
    assert (
        adr.metadata_identity
        == SecAdrShareMetadata.from_payload(adr.canonical_payload).metadata_identity
    )

    # Opt-in conversion: 25,932,071,000 ordinary shares / 5 = 5,186,414,200 ADR-equivalent shares
    assert convert_ordinary_shares_to_adr_equivalent(Decimal(25932071000), Decimal(5)) == Decimal(
        5186414200
    )

    with pytest.raises(ValueError, match="ordinary_shares_per_adr"):
        convert_ordinary_shares_to_adr_equivalent(Decimal(100), Decimal(0))
    with pytest.raises(ValueError, match="shares"):
        convert_ordinary_shares_to_adr_equivalent(Decimal(-1), Decimal(5))


def test_vintage_identity_unchanged_without_adr_metadata_and_changes_with_adr_metadata() -> None:
    base = _sample_vintage(adr_metadata=None)
    with_adr = replace(base, adr_metadata=_sample_adr_metadata(currency="TWD"))
    assert base.vintage_identity != with_adr.vintage_identity


def test_symbol_partition_upgrades_to_v5_when_adr_metadata_present(tmp_path: Path) -> None:
    vintage = _sample_vintage(adr_metadata=_sample_adr_metadata(currency="TWD"))
    manifest = write_financials_partition(tmp_path, "TSM", [vintage])
    assert manifest["dataset_schema"] == FINANCIALS_DATASET_SCHEMA_V5
    assert validate_financials_partition(tmp_path / "symbol=TSM") == manifest


def test_extract_dei_shares_and_reporting_currency_in_v2_fpi_20f(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    XBRL = pytest.importorskip("edgar.xbrl").XBRL
    Statement = pytest.importorskip("edgar.xbrl.statements").Statement
    PresentationNode = pytest.importorskip("edgar.xbrl.models").PresentationNode

    raw = b"""<xbrl xmlns="http://www.xbrl.org/2003/instance"
      xmlns:ifrs-full="http://xbrl.ifrs.org/taxonomy/2023-03-23/ifrs-full"
      xmlns:dei="http://xbrl.sec.gov/dei/2023"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
      <context id="fy24">
        <entity><identifier scheme="http://www.sec.gov/CIK">0001046179</identifier></entity>
        <period><startDate>2024-01-01</startDate><endDate>2024-12-31</endDate></period>
      </context>
      <context id="cover">
        <entity><identifier scheme="http://www.sec.gov/CIK">0001046179</identifier></entity>
        <period><instant>2024-12-31</instant></period>
      </context>
      <unit id="twd"><measure>iso4217:TWD</measure></unit>
      <unit id="sh"><measure>shares</measure></unit>
      <ifrs-full:Revenue contextRef="fy24" unitRef="twd" decimals="-6">2894308000000</ifrs-full:Revenue>
      <dei:EntityCommonStockSharesOutstanding contextRef="cover" unitRef="sh" decimals="0">25932071000</dei:EntityCommonStockSharesOutstanding>
    </xbrl>"""

    # Standalone raw extraction check
    dei_only = extract_dei_shares_metadata(raw_instance=raw)
    assert dei_only == SecDeiSharesMetadata(
        shares_outstanding=Decimal(25932071000),
        as_of_date=date(2024, 12, 31),
        unit="shares",
        native_tag="dei:EntityCommonStockSharesOutstanding",
        context_ref="cover",
    )

    xbrl = XBRL()
    xbrl.parser.parse_instance_content(raw.decode("utf-8"))
    role = "https://example.invalid/role/IncomeStatement"
    xbrl.parser.presentation_trees = {
        role: SimpleNamespace(
            all_nodes={
                "ifrs-full_Revenue": PresentationNode(
                    element_id="ifrs-full_Revenue", standard_label="Revenue"
                )
            }
        )
    }
    xbrl.find_statement = lambda *args: ([], role, "IncomeStatement")
    income_stmt = Statement(xbrl, "IncomeStatement")

    attachment = SimpleNamespace(
        document="tsm-20241231_htm.xml",
        document_type="EX-101.INS",
        extension=".xml",
        url="https://www.sec.gov/Archives/edgar/data/1046179/000104617925000010/tsm-20241231_htm.xml",
        sgml_document=SimpleNamespace(content=raw.decode("utf-8")),
    )
    fin = SimpleNamespace(
        balance_sheet=lambda: None,
        income_statement=lambda: income_stmt,
        cash_flow_statement=lambda: None,
    )
    filing = SimpleNamespace(
        cik="1046179",
        accession_number="0001046179-25-000010",
        form="20-F",
        filing_date="2025-04-17",
        period_of_report="2024-12-31",
        company="Taiwan Semiconductor Manufacturing Co Ltd",
        header=SimpleNamespace(acceptance_datetime=datetime(2025, 4, 17, 10, 0, tzinfo=UTC)),
        attachments=SimpleNamespace(data_files=[attachment]),
        obj=lambda: SimpleNamespace(financials=fin),
    )

    monkeypatch.setattr("edgar.set_identity", lambda _: None)
    monkeypatch.setattr(
        "edgar.Company",
        lambda _: SimpleNamespace(get_filings=lambda **kw: [filing], ticker="TSM"),
    )

    client = SecFinancialsClient("Synthetic test@example.invalid")
    vintages = client.fetch_company_financials(
        SecFinancialsRequest(ciks=("1046179",), forms=("20-F",), parser_version=V2)
    )
    assert len(vintages) == 1
    v = vintages[0]
    assert v.cik == "0001046179"
    assert v.symbol == "TSM"
    assert v.unit_evidence is not None
    assert len(v.rows) == 1
    assert v.rows[0].concept == "ifrs-full_Revenue"
    assert v.rows[0].standard_concept == "Revenues"
    assert v.rows[0].unit == "iso4217:TWD"
    assert v.rows[0].currency == "TWD"
    assert v.rows[0].value == Decimal(2894308000000)

    # ADR metadata captures reporting_currency + DEI cover-page shares without guessing ADR ratio!
    assert v.adr_metadata is not None
    assert v.adr_metadata.reporting_currency == "TWD"
    assert v.adr_metadata.share_basis == SecShareBasis.UNKNOWN
    assert v.adr_metadata.ordinary_shares_per_adr is None
    assert v.adr_metadata.ratio_source is None
    assert v.adr_metadata.dei_shares is not None
    assert v.adr_metadata.dei_shares.shares_outstanding == Decimal(25932071000)
    assert v.adr_metadata.dei_shares.as_of_date == date(2024, 12, 31)
    assert v.adr_metadata.dei_shares.unit == "shares"


def test_v2_custom_runner_still_rejects_missing_evidence_except_non_xbrl_6k() -> None:
    non_xbrl_6k = SecCompanyFinancialVintage(
        symbol="TSM",
        cik="0001046179",
        company_name="TSMC",
        form="6-K",
        accession_number="0001046179-25-000050",
        filing_date=date(2025, 7, 17),
        quality_flags=("NON_XBRL_6K", "NO_XBRL_ATTACHMENT", "NO_FINANCIAL_STATEMENTS"),
        rows=(),
    )
    client = SecFinancialsClient("Synthetic test@example.invalid", runner=lambda req: [non_xbrl_6k])
    res = client.fetch_company_financials(
        SecFinancialsRequest(ciks=("1046179",), forms=("6-K",), parser_version=V2)
    )
    assert res == [non_xbrl_6k]

    # A 6-K claiming NON_XBRL_6K WITH statement rows must still be rejected in v2!
    forged = replace(non_xbrl_6k, rows=(_sample_row(),))
    bad_client = SecFinancialsClient("Synthetic test@example.invalid", runner=lambda req: [forged])
    with pytest.raises(SecUnitEvidenceError, match="without unit evidence"):
        bad_client.fetch_company_financials(
            SecFinancialsRequest(ciks=("1046179",), forms=("6-K",), parser_version=V2)
        )


def test_dei_raw_xml_rejects_doctype_entity_and_non_share_units() -> None:
    xxe_xml = b"""<!DOCTYPE xbrl [<!ENTITY lol "lol">]>
    <xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:dei="http://xbrl.sec.gov/dei/2023">
      <context id="c"><entity><identifier scheme="http://www.sec.gov/CIK">1</identifier></entity>
        <period><instant>2024-12-31</instant></period></context>
      <unit id="sh"><measure>shares</measure></unit>
      <dei:EntityCommonStockSharesOutstanding contextRef="c" unitRef="sh">100</dei:EntityCommonStockSharesOutstanding>
    </xbrl>"""
    assert extract_dei_shares_metadata(raw_instance=xxe_xml) is None

    monetary_xml = b"""<xbrl xmlns="http://www.xbrl.org/2003/instance"
      xmlns:dei="http://xbrl.sec.gov/dei/2023"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
      <context id="c"><entity><identifier scheme="http://www.sec.gov/CIK">1</identifier></entity>
        <period><instant>2024-12-31</instant></period></context>
      <unit id="usd"><measure>iso4217:USD</measure></unit>
      <dei:EntityCommonStockSharesOutstanding contextRef="c" unitRef="usd">100</dei:EntityCommonStockSharesOutstanding>
    </xbrl>"""
    assert extract_dei_shares_metadata(raw_instance=monetary_xml) is None

    divide_xml = b"""<xbrl xmlns="http://www.xbrl.org/2003/instance"
      xmlns:dei="http://xbrl.sec.gov/dei/2023"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
      <context id="c"><entity><identifier scheme="http://www.sec.gov/CIK">1</identifier></entity>
        <period><instant>2024-12-31</instant></period></context>
      <unit id="per_share"><divide><unitNumerator><measure>iso4217:USD</measure></unitNumerator>
        <unitDenominator><measure>shares</measure></unitDenominator></divide></unit>
      <dei:EntityCommonStockSharesOutstanding contextRef="c" unitRef="per_share">100</dei:EntityCommonStockSharesOutstanding>
    </xbrl>"""
    assert extract_dei_shares_metadata(raw_instance=divide_xml) is None

    # XBRL object with monetary unit must also be rejected, while xbrli:shares is accepted
    bad_fact = SimpleNamespace(
        element_id="dei_EntityCommonStockSharesOutstanding",
        context_ref="c",
        value="100",
        unit_ref="u_usd",
    )
    good_fact = SimpleNamespace(
        element_id="dei_EntityCommonStockSharesOutstanding",
        context_ref="c",
        value="250000",
        unit_ref="u_sh",
    )
    ctx = SimpleNamespace(dimensions={}, period={"type": "instant", "instant": "2024-12-31"})
    bad_xbrl = SimpleNamespace(
        parser=SimpleNamespace(facts={"f1": bad_fact}),
        contexts={"c": ctx},
        units={"u_usd": {"measure": "iso4217:USD"}},
    )
    assert extract_dei_shares_metadata(bad_xbrl) is None

    good_xbrl = SimpleNamespace(
        parser=SimpleNamespace(facts={"f1": good_fact}),
        contexts={"c": ctx},
        units={"u_sh": {"measure": "xbrli:shares"}},
    )
    extracted = extract_dei_shares_metadata(good_xbrl)
    assert extracted is not None
    assert extracted.shares_outstanding == Decimal(250000)
    assert extracted.unit == "xbrli:shares"


def test_cik_fetch_fails_closed_when_provider_returns_mismatched_cik(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_filing = SimpleNamespace(
        form="10-K",
        filing_date="2024-03-01",
        cik="320193",
        company="Apple Inc.",
        accession_number="0000320193-24-000001",
        period_of_report="2023-12-31",
        header=SimpleNamespace(acceptance_datetime=datetime(2024, 3, 1, 21, 0, tzinfo=UTC)),
        obj=lambda: None,
    )
    monkeypatch.setattr(
        "edgar.Company",
        lambda _: SimpleNamespace(get_filings=lambda **kw: [wrong_filing], ticker="TSM"),
    )
    monkeypatch.setattr("edgar.set_identity", lambda _: None)

    client = SecFinancialsClient("Synthetic test@example.invalid")
    with pytest.raises(ValueError, match="does not match requested CIK"):
        client.fetch_company_financials(
            SecFinancialsRequest(ciks=("1046179",), forms=("10-K",), parser_version=V1)
        )


def test_writers_reject_duplicate_vintage_identity_before_creating_files(tmp_path: Path) -> None:
    vintage = _sample_vintage(adr_metadata=_sample_adr_metadata(currency="TWD"))
    with pytest.raises(ValueError, match="duplicate vintage_identity"):
        write_cik_financials_partition(tmp_path, "1046179", [vintage, vintage])
    assert not (tmp_path / "cik=0001046179").exists()

    with pytest.raises(ValueError, match="duplicate vintage_identity"):
        write_financials_incremental_batch(tmp_path, "batch-dup-1", [vintage, vintage])
    assert not (tmp_path / "batches" / "batch-dup-1").exists()

    with pytest.raises(ValueError, match="duplicate vintage_identity"):
        write_financials_partition(tmp_path, "TSM", [vintage, vintage])
    assert not (tmp_path / "symbol=TSM").exists()

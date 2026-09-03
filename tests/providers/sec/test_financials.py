"""Offline tests for SEC company financial statements (PIT) models and adapter."""

from __future__ import annotations

import importlib
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pandas as pd
import pytest

from ohmydata.providers.sec.edgartools_adapter import (
    SecFinancialsClient,
    ensure_edgar_available,
    parse_statement_rows,
    validate_user_agent,
)
from ohmydata.providers.sec.financials import (
    SecCompanyFinancialVintage,
    SecFinancialsRequest,
    SecStatementRow,
)
from ohmydata.providers.sec.financials_dataset import write_financials_partition


def test_ensure_edgar_available() -> None:
    # edgartools is installed in test environment
    ensure_edgar_available()


def test_validate_user_agent() -> None:
    with pytest.raises(ValueError, match="User-Agent cannot be empty"):
        validate_user_agent("")

    with pytest.raises(ValueError, match="User-Agent cannot be whitespace only"):
        validate_user_agent("   ")

    with pytest.raises(ValueError, match="User-Agent should include an email or domain"):
        validate_user_agent("JustAName")

    valid = validate_user_agent("ResearchBot test@example.com")
    assert valid == "ResearchBot test@example.com"


def test_request_validation() -> None:
    with pytest.raises(ValueError, match="symbols cannot be empty"):
        SecFinancialsRequest(symbols=())

    with pytest.raises(ValueError, match="invalid symbol"):
        SecFinancialsRequest(symbols=("",))

    with pytest.raises(ValueError, match="unsupported availability_policy"):
        SecFinancialsRequest(symbols=("AAPL",), availability_policy="invalid-policy")

    with pytest.raises(ValueError, match="lag_days must be between 0 and 30"):
        SecFinancialsRequest(symbols=("AAPL",), lag_days=35)

    req = SecFinancialsRequest(symbols=("AAPL", "MSFT"), lag_days=1)
    assert req.symbols == ("AAPL", "MSFT")
    assert req.lag_days == 1
    assert req.forms == ("10-K", "10-Q")


def test_vintage_point_in_time_anchor_and_identity() -> None:
    accepted = datetime(2024, 2, 1, 21, 30, 0, tzinfo=UTC)
    row = SecStatementRow(
        statement_type="income_statement",
        standard_concept="Revenues",
        concept="us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
        label="Total net sales",
        value=Decimal(119575000000),
        value_native="119575000000",
        unit="USD",
        period_end=date(2023, 12, 30),
        is_point_in_time=False,
    )
    vintage = SecCompanyFinancialVintage(
        symbol="AAPL",
        cik="0000320193",
        company_name="Apple Inc.",
        form="10-Q",
        accession_number="0000320193-24-000006",
        filing_date=date(2024, 2, 2),
        period_end=date(2023, 12, 30),
        accepted_at=accepted,
        availability_lag_days=1,
        rows=(row,),
    )

    # Availability anchor computed from accepted_at + 1 day lag
    assert vintage.availability_anchor == datetime(2024, 2, 2, 21, 30, 0, tzinfo=UTC)
    # Accounting period end is preserved but distinct from availability anchor
    assert vintage.period_end == date(2023, 12, 30)

    # Deterministic vintage identity hash
    ident1 = vintage.vintage_identity
    ident2 = vintage.vintage_identity
    assert ident1 == ident2
    assert len(ident1) == 64

    # Filter by statement
    assert len(vintage.filter_statement("income_statement")) == 1
    assert len(vintage.filter_statement("balance_sheet")) == 0


def test_vintage_requires_timezone_aware_accepted_at() -> None:
    naive_dt = datetime(2024, 2, 1, 21, 30, 0)  # noqa: DTZ001
    with pytest.raises(ValueError, match="accepted_at must be timezone-aware"):
        SecCompanyFinancialVintage(
            symbol="AAPL",
            cik="0000320193",
            company_name="Apple Inc.",
            form="10-Q",
            accession_number="0000320193-24-000006",
            filing_date=date(2024, 2, 2),
            accepted_at=naive_dt,
        )


def test_parse_statement_rows_from_mock_statement() -> None:
    mock_statement = MagicMock()
    df_data = {
        "label": ["Operating expenses:", "Research and development", "Total operating expenses"],
        "concept": [
            "us-gaap_OperatingExpensesAbstract",
            "us-gaap_ResearchAndDevelopmentExpense",
            "us-gaap_OperatingExpenses",
        ],
        "standard_concept": ["", "ResearchAndDevelopmentExpense", "OperatingExpenses"],
        "unit": ["", "USD", "USD"],
        "point_in_time": [False, False, False],
        "abstract": [True, False, False],
        "dimension": [False, False, False],
        "2023-12-30": [None, 7696000000, 14482000000],
        "2022-12-31": [None, 7709000000, 14316000000],
    }
    mock_statement.to_dataframe.return_value = pd.DataFrame(df_data)

    rows = parse_statement_rows(mock_statement, "income_statement")
    # Abstract header is skipped; 2 non-abstract items x 2 periods = 4 rows
    assert len(rows) == 4

    rd_rows = [r for r in rows if r.standard_concept == "ResearchAndDevelopmentExpense"]
    assert len(rd_rows) == 2
    rd_2023 = next(r for r in rd_rows if r.period_end == date(2023, 12, 30))
    assert rd_2023.value == Decimal(7696000000)
    assert rd_2023.statement_type == "income_statement"
    assert rd_2023.unit == "USD"
    assert rd_2023.label == "Research and development"


def test_client_from_config_and_runner_offline() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        ua_file = tmp_path / "contact.txt"
        ua_file.write_text("TestUser (test@example.invalid)\n", encoding="utf-8")

        config_file = tmp_path / "financials-config.json"
        config_file.write_text(
            '{"user_agent_file": "contact.txt", "symbols": ["AAPL"]}', encoding="utf-8"
        )

        client = SecFinancialsClient.from_config(config_file)
        assert client.user_agent == "TestUser (test@example.invalid)"

        # Injected offline runner test
        mock_vintage = SecCompanyFinancialVintage(
            symbol="AAPL",
            cik="0000320193",
            company_name="Apple Inc.",
            form="10-K",
            accession_number="0000320193-23-000106",
            filing_date=date(2023, 11, 3),
            accepted_at=datetime(2023, 11, 2, 22, 0, 0, tzinfo=UTC),
        )

        def offline_runner(req: SecFinancialsRequest) -> list[SecCompanyFinancialVintage]:
            return [mock_vintage] if "AAPL" in req.symbols else []

        mock_client = SecFinancialsClient("Test (test@example.com)", runner=offline_runner)
        res = mock_client.fetch_company_financials(SecFinancialsRequest(symbols=("AAPL",)))
        assert len(res) == 1
        assert res[0].accession_number == "0000320193-23-000106"


def test_write_financials_partition_and_manifest() -> None:
    row1 = SecStatementRow(
        statement_type="balance_sheet",
        standard_concept="CashAndCashEquivalents",
        concept="us-gaap_CashAndCashEquivalentsAtCarryingValue",
        label="Cash and cash equivalents",
        value=Decimal(29965000000),
        value_native="29965000000",
        unit="USD",
        period_end=date(2023, 9, 30),
        is_point_in_time=True,
    )
    row2 = SecStatementRow(
        statement_type="income_statement",
        standard_concept="Revenues",
        concept="us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
        label="Total net sales",
        value=Decimal(383285000000),
        value_native="383285000000",
        unit="USD",
        period_end=date(2023, 9, 30),
        is_point_in_time=False,
    )
    vintage = SecCompanyFinancialVintage(
        symbol="AAPL",
        cik="0000320193",
        company_name="Apple Inc.",
        form="10-K",
        accession_number="0000320193-23-000106",
        filing_date=date(2023, 11, 3),
        period_end=date(2023, 9, 30),
        accepted_at=datetime(2023, 11, 2, 22, 8, 4, tzinfo=UTC),
        availability_lag_days=0,
        rows=(row1, row2),
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        manifest = write_financials_partition(tmpdir, "AAPL", [vintage])
        assert manifest["symbol"] == "AAPL"
        assert manifest["vintage_count"] == 1
        assert manifest["statement_row_count"] == 2
        assert "company_financial_vintages.parquet" in manifest["files"]
        assert "financial_statements.parquet" in manifest["files"]

        # Read back tables with PyArrow
        pq = cast(Any, importlib.import_module("pyarrow.parquet"))

        v_path = Path(tmpdir) / "symbol=AAPL" / "company_financial_vintages.parquet"
        v_tbl = pq.ParquetFile(v_path).read()
        assert v_tbl.num_rows == 1
        assert str(v_tbl.column("symbol")[0].as_py()) == "AAPL"
        assert int(v_tbl.column("row_count")[0].as_py()) == 2

        s_path = Path(tmpdir) / "symbol=AAPL" / "financial_statements.parquet"
        s_tbl = pq.ParquetFile(s_path).read()
        assert s_tbl.num_rows == 2
        concepts: list[str] = [str(s_tbl.column("standard_concept")[i].as_py()) for i in range(2)]
        assert "CashAndCashEquivalents" in concepts
        assert "Revenues" in concepts


def test_parse_statement_rows_dimensional_filtering() -> None:
    mock_statement = MagicMock()
    df_data = {
        "label": ["iPhone", "Mac", "Total net sales"],
        "concept": [
            "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
        ],
        "standard_concept": ["Revenues", "Revenues", "Revenues"],
        "unit": ["USD", "USD", "USD"],
        "point_in_time": [False, False, False],
        "abstract": [False, False, False],
        "dimension": [True, True, False],
        "2023-09-30": [200610000000, 29357000000, 383285000000],
    }
    mock_statement.to_dataframe.return_value = pd.DataFrame(df_data)

    # By default, dimensional rows are excluded
    face_rows = parse_statement_rows(mock_statement, "income_statement", include_dimensions=False)
    assert len(face_rows) == 1
    assert face_rows[0].label == "Total net sales"
    assert face_rows[0].value == Decimal(383285000000)

    # When include_dimensions=True, all rows are included
    all_rows = parse_statement_rows(mock_statement, "income_statement", include_dimensions=True)
    assert len(all_rows) == 3


def test_client_fetch_company_financials_with_mocked_edgar_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_company_cls = MagicMock()
    mock_company_instance = MagicMock()
    mock_company_cls.return_value = mock_company_instance

    mock_filing = MagicMock()
    mock_filing.form = "10-K"
    mock_filing.filing_date = "2023-11-03"
    mock_filing.cik = 320193
    mock_filing.company = "Apple Inc."
    mock_filing.accession_number = "0000320193-23-000106"
    mock_filing.period_of_report = "2023-09-30"

    mock_header = MagicMock()
    mock_header.acceptance_datetime = datetime(2023, 11, 2, 17, 8, 4)  # noqa: DTZ001
    mock_filing.header = mock_header

    mock_report = MagicMock()
    mock_fin = MagicMock()
    mock_report.financials = mock_fin
    mock_filing.obj.return_value = mock_report

    mock_bs = MagicMock()
    mock_bs.to_dataframe.return_value = pd.DataFrame(
        {
            "label": ["Cash"],
            "concept": ["us-gaap_Cash"],
            "standard_concept": ["CashAndCashEquivalents"],
            "unit": ["USD"],
            "point_in_time": [True],
            "abstract": [False],
            "dimension": [False],
            "2023-09-30": [29965000000],
        }
    )
    mock_fin.balance_sheet.return_value = mock_bs
    mock_fin.income_statement.return_value = None
    mock_fin.cash_flow_statement.return_value = None

    mock_company_instance.get_filings.return_value = [mock_filing]

    # Monkeypatch edgar.Company
    monkeypatch.setattr("edgar.Company", mock_company_cls)

    client = SecFinancialsClient("TestAgent test@example.com")
    req = SecFinancialsRequest(symbols=("AAPL",), forms=("10-K",))
    vintages = client.fetch_company_financials(req)

    assert len(vintages) == 1
    v = vintages[0]
    assert v.symbol == "AAPL"
    assert v.cik == "0000320193"
    assert v.form == "10-K"
    assert v.accession_number == "0000320193-23-000106"
    assert v.accepted_at == datetime(
        2023, 11, 2, 21, 8, 4, tzinfo=UTC
    )  # Eastern 17:08:04 -> UTC 21:08:04 (EDT is UTC-4 in Nov before DST end)
    assert len(v.rows) == 1
    assert v.rows[0].value == Decimal(29965000000)


def test_cli_sec_financials_commands(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ohmydata.cli import main

    row = SecStatementRow(
        statement_type="balance_sheet",
        standard_concept="CashAndCashEquivalents",
        concept="us-gaap_Cash",
        label="Cash",
        value=Decimal(1000000),
        value_native="1000000",
        unit="USD",
        period_end=date(2023, 9, 30),
        is_point_in_time=True,
    )
    vintage = SecCompanyFinancialVintage(
        symbol="MSFT",
        cik="0000789019",
        company_name="Microsoft Corp",
        form="10-K",
        accession_number="0000789019-23-000010",
        filing_date=date(2023, 10, 24),
        period_end=date(2023, 9, 30),
        accepted_at=datetime(2023, 10, 24, 20, 0, 0, tzinfo=UTC),
        rows=(row,),
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        # Pre-populate one partition
        write_financials_partition(tmpdir, "MSFT", [vintage])

        # 1. Test inspect
        code = main(
            ["sec", "financials", "inspect", "--root", tmpdir, "--symbol", "MSFT", "--json"]
        )
        assert code == 0
        out = capsys.readouterr().out
        assert '"symbol":"MSFT"' in out or '"symbol": "MSFT"' in out
        assert '"vintage_count":1' in out or '"vintage_count": 1' in out

        # 2. Test inspect with --rows
        code = main(
            [
                "sec",
                "financials",
                "inspect",
                "--root",
                tmpdir,
                "--symbol",
                "MSFT",
                "--rows",
                "--json",
            ]
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "CashAndCashEquivalents" in out

        # 3. Test validate
        code = main(["sec", "financials", "validate", "--root", tmpdir, "--json"])
        assert code == 0
        out = capsys.readouterr().out
        assert '"partitions_verified":1' in out or '"partitions_verified": 1' in out

        # 4. Test sync via mock
        def mock_fetch(self: Any, req: Any) -> list[SecCompanyFinancialVintage]:
            return [vintage]

        monkeypatch.setattr(SecFinancialsClient, "fetch_company_financials", mock_fetch)
        code = main(
            [
                "sec",
                "financials",
                "sync",
                "--root",
                tmpdir,
                "--symbols",
                "MSFT",
                "--latest",
                "--user-agent",
                "Test <test@example.com>",
                "--json",
            ]
        )
        assert code == 0
        out = capsys.readouterr().out
        assert '"status":"completed"' in out or '"status": "completed"' in out
        assert '"symbols_processed":1' in out or '"symbols_processed": 1' in out

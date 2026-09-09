"""Offline selection regressions using edgartools' real Filings container."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pyarrow as pa
import pytest

from ohmydata.providers.sec.edgartools_adapter import SecFinancialsClient
from ohmydata.providers.sec.financials import SecFinancialsRequest

Filings = pytest.importorskip("edgar._filings").Filings
Filing = pytest.importorskip("edgar._filings").Filing


def _filings(count: int) -> Filings:
    return Filings(
        pa.table(
            {
                "cik": [1] * count,
                "company": ["Synthetic"] * count,
                "form": ["10-K"] * count,
                "filing_date": [f"2024-0{i + 1}-01" for i in range(count)],
                "accession_number": [f"0000000001-24-00000{i + 1}" for i in range(count)],
            }
        )
    )


def test_real_edgartools_filings_latest_shape_zero_one_many() -> None:
    assert len(_filings(0)) == 0
    one = _filings(1).latest(1)
    assert one is not None and one.form == "10-K"
    many = _filings(3).latest(2)
    assert isinstance(many, Filings)
    assert len(many) == 2


def install_company(monkeypatch, entries, reports=None):
    table = pa.table(
        {
            "cik": [1] * len(entries),
            "company": ["Synthetic"] * len(entries),
            "form": [e[0] for e in entries],
            "filing_date": [e[1] for e in entries],
            "accession_number": [e[2] for e in entries],
        }
    )
    calls = []

    def get_filings(**kwargs):
        calls.append(kwargs)
        return Filings(table)

    monkeypatch.setattr("edgar.Company", lambda symbol: SimpleNamespace(get_filings=get_filings))
    monkeypatch.setattr("edgar.set_identity", lambda identity: None)
    monkeypatch.setattr(
        Filing,
        "header",
        property(
            lambda self: SimpleNamespace(acceptance_datetime=datetime(2024, 8, 1, tzinfo=UTC))
        ),
    )
    monkeypatch.setattr(Filing, "period_of_report", property(lambda self: "2024-06-30"))
    monkeypatch.setattr(Filing, "obj", lambda self: (reports or {}).get(self.accession_number))
    return SecFinancialsClient("Synthetic test@example.invalid"), calls


@pytest.mark.parametrize("count", [0, 1, 3])
def test_client_enumerates_real_filings_without_treating_collection_as_filing(monkeypatch, count):
    entries = [("10-K", f"2024-0{i + 1}-01", f"fake-{i}") for i in range(count)]
    client, _ = install_company(monkeypatch, entries)
    result = client.fetch_company_financials(SecFinancialsRequest(("FAKE",), limit=2))
    assert len(result) == min(count, 2)
    assert all("NO_FINANCIAL_STATEMENTS" in v.quality_flags for v in result)
    assert all(not any("PARSE_FAILED" in flag for flag in v.quality_flags) for v in result)


@pytest.mark.parametrize(
    "amendments,forms,expected",
    [
        (False, ("10-K",), "original"),
        (True, ("10-K",), "governance"),
        (True, ("10-K/A",), "governance"),
        (False, ("10-K/A",), None),
    ],
)
def test_governance_amendment_never_hides_or_substitutes_original(
    monkeypatch, amendments, forms, expected
):
    entries = [("10-K/A", "2024-08-02", "governance"), ("10-K", "2024-08-01", "original")]
    client, calls = install_company(monkeypatch, entries)
    result = client.fetch_company_financials(
        SecFinancialsRequest(("FAKE",), forms=forms, include_amendments=amendments, limit=1)
    )
    assert [v.accession_number for v in result] == ([expected] if expected else [])
    assert calls[0]["amendments"] is amendments
    if expected == "governance":
        assert result[0].is_amendment
        assert "NO_FINANCIALS_OBJECT" in result[0].quality_flags


@pytest.mark.parametrize(
    "start,end,expected,date_filter",
    [
        (2024, None, "future", "2024-01-01:"),
        (None, 2024, "quarter", ":2024-12-31"),
        (2024, 2024, "quarter", "2024-01-01:2024-12-31"),
    ],
)
def test_year_bounds_and_multiple_forms_before_limit(
    monkeypatch, start, end, expected, date_filter
):
    entries = [
        ("10-K", "2025-01-01", "future"),
        ("10-Q", "2024-08-01", "quarter"),
        ("10-K", "2023-01-01", "old"),
    ]
    client, calls = install_company(monkeypatch, entries)
    result = client.fetch_company_financials(
        SecFinancialsRequest(("FAKE",), start_year=start, end_year=end, limit=1)
    )
    assert [v.accession_number for v in result] == [expected]
    assert calls[0]["filing_date"] == date_filter


def test_missing_empty_and_parse_failed_statements_are_distinguishable(monkeypatch):
    fin = SimpleNamespace(
        balance_sheet=lambda: None,
        income_statement=lambda: SimpleNamespace(get_raw_data=list),
        cash_flow_statement=lambda: SimpleNamespace(
            get_raw_data=lambda: [{"concept": "fake:Cash", "values": {"unknown": 1}}]
        ),
    )
    client, _ = install_company(
        monkeypatch,
        [("10-Q", "2024-08-01", "quarter")],
        {"quarter": SimpleNamespace(financials=fin)},
    )
    result = client.fetch_company_financials(SecFinancialsRequest(("FAKE",)))[0]
    assert set(result.quality_flags) == {
        "BALANCE_SHEET_MISSING",
        "INCOME_STATEMENT_EMPTY",
        "CASH_FLOW_PARSE_FAILED",
        "NO_FINANCIAL_STATEMENTS",
    }
    assert result.accepted_at == datetime(2024, 8, 1, tzinfo=UTC)
    assert result.availability_anchor == result.accepted_at


@pytest.mark.parametrize("include_dimensions", [True, False])
def test_client_dimension_policy_is_explicit(monkeypatch, include_dimensions):
    statement = SimpleNamespace(
        get_raw_data=lambda: [
            {
                "concept": "fake:Revenue",
                "is_dimension": True,
                "dimension_metadata": [{"dimension": "fake:Axis", "member": "fake:Member"}],
                "values": {"duration_2024-04-01_2024-06-30": 10},
            }
        ]
    )
    fin = SimpleNamespace(
        balance_sheet=lambda: None,
        income_statement=lambda: statement,
        cash_flow_statement=lambda: None,
    )
    client, _ = install_company(
        monkeypatch,
        [("10-Q", "2024-08-01", "quarter")],
        {"quarter": SimpleNamespace(financials=fin)},
    )
    result = client.fetch_company_financials(
        SecFinancialsRequest(("FAKE",), include_dimensions=include_dimensions)
    )[0]
    assert len(result.rows) == int(include_dimensions)
    assert ("DIMENSIONS_EXCLUDED_BY_REQUEST" in result.quality_flags) is not include_dimensions


def test_lowercase_client_symbol_returns_canonical_vintage(monkeypatch):
    client, _ = install_company(monkeypatch, [("10-Q", "2024-08-01", "quarter")])
    result = client.fetch_company_financials(SecFinancialsRequest(("fake",)))[0]
    assert result.symbol == "FAKE"

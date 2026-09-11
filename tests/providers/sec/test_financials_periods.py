"""Synthetic native Edgar 5.56 boundaries and period/vintage roundtrip regressions."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from ohmydata.providers.sec.edgartools_adapter import (
    SecFinancialsClient,
    SecStatementParseError,
    parse_statement_rows,
)
from ohmydata.providers.sec.financials import (
    SecCompanyFinancialVintage,
    SecFinancialsRequest,
    SecStatementRow,
)
from ohmydata.providers.sec.financials_dataset import (
    validate_financials_partition,
    write_financials_partition,
)

Context = pytest.importorskip("edgar.xbrl.models").Context
Fact = pytest.importorskip("edgar.xbrl.models").Fact
PresentationNode = pytest.importorskip("edgar.xbrl.models").PresentationNode
Statement = pytest.importorskip("edgar.xbrl.statements").Statement
XBRL = pytest.importorskip("edgar.xbrl.xbrl").XBRL
FactsView = pytest.importorskip("edgar.xbrl.facts").FactsView


def native_statement():
    quarter = "duration_2024-04-01_2024-06-30"
    ytd = "duration_2024-01-01_2024-06-30"
    concept = "us-gaap_OperatingIncomeLoss"
    contexts = {
        "q": Context(
            context_id="q",
            period={"type": "duration", "startDate": "2024-04-01", "endDate": "2024-06-30"},
        ),
        "y": Context(
            context_id="y",
            period={"type": "duration", "startDate": "2024-01-01", "endDate": "2024-06-30"},
        ),
        "d": Context(
            context_id="d",
            period={"type": "duration", "startDate": "2024-04-01", "endDate": "2024-06-30"},
            dimensions={"fake:Axis": "fake:Member"},
        ),
    }
    facts = {
        "q": Fact(
            element_id=concept,
            context_ref="q",
            value="1234567890123456.1234",
            numeric_value=1234567890123456.0,
            unit_ref="u1",
            decimals=4,
        ),
        "y": Fact(
            element_id=concept,
            context_ref="y",
            value="2000000000000000.5678",
            numeric_value=2000000000000000.5,
            unit_ref="u2",
            decimals="INF",
        ),
        "d": Fact(
            element_id=concept,
            context_ref="d",
            value="7.0000",
            numeric_value=7,
            unit_ref="u1",
            decimals=4,
        ),
    }
    xbrl = XBRL()
    xbrl.parser.facts = facts
    xbrl.parser.contexts = contexts
    xbrl.parser.context_period_map = {"q": quarter, "y": ytd, "d": quarter}
    xbrl.parser.units = {"u1": {"measure": "iso4217:USD"}, "u2": {"measure": "iso4217:EUR"}}
    xbrl.parser.presentation_trees = {
        "fake:IncomeRole": SimpleNamespace(
            all_nodes={
                concept: PresentationNode(
                    element_id=concept, standard_label="GAAP operating income"
                )
            }
        )
    }
    xbrl.find_statement = lambda *args: ([], "fake:IncomeRole", "IncomeStatement")
    assert isinstance(xbrl.facts, FactsView)
    assert not hasattr(xbrl.facts, "values")
    return Statement(xbrl, "IncomeStatement"), xbrl


def test_actual_statement_shape_preserves_native_precision_and_periods():
    statement, _ = native_statement()
    rows = parse_statement_rows(statement, "income_statement")
    assert len(rows) == 2
    quarter = next(r for r in rows if r.context_ref == "q")
    ytd = next(r for r in rows if r.context_ref == "y")
    assert quarter.value == Decimal("1234567890123456.1234")
    assert ytd.value == Decimal("2000000000000000.5678")
    assert quarter.period_end == ytd.period_end == date(2024, 6, 30)
    assert quarter.period_start == date(2024, 4, 1)
    assert ytd.period_start == date(2024, 1, 1)
    assert quarter.period_key != ytd.period_key
    assert quarter.context_ref == "q"
    assert quarter.unit == "iso4217:USD" and ytd.unit == "iso4217:EUR"
    assert quarter.unit_ref == "u1"
    assert ytd.decimals is None and ytd.decimals_native == "INF"
    assert quarter.period_source == "xbrl-context"


def test_factsview_enriched_api_is_not_used_for_native_facts(monkeypatch):
    statement, xbrl = native_statement()
    assert isinstance(xbrl, XBRL)
    assert isinstance(xbrl.facts, FactsView)
    assert xbrl._facts is xbrl.parser.facts  # Upstream raw alias, not the query view.

    def forbidden(*args, **kwargs):
        raise AssertionError("enriched facts would lose the native boundary")

    monkeypatch.setattr(FactsView, "get_facts", forbidden)
    rows = parse_statement_rows(statement, "income_statement", include_dimensions=True)
    assert {r.context_ref for r in rows} == {"q", "y", "d"}
    assert {r.concept for r in rows} == {"us-gaap_OperatingIncomeLoss"}


@pytest.mark.parametrize("bad_mapping", [None, [], "invalid"])
def test_unavailable_native_fact_mapping_fails_without_display_fallback(bad_mapping):
    statement, xbrl = native_statement()
    xbrl.parser.facts = bad_mapping
    with pytest.raises(SecStatementParseError, match="native parser facts must be a mapping"):
        parse_statement_rows(statement, "income_statement")


def test_client_returns_all_three_statements_from_real_xbrl_factsview(monkeypatch):
    income, xbrl = native_statement()
    xbrl.parser.contexts["instant"] = Context(
        context_id="instant", period={"type": "instant", "instant": "2024-06-30"}
    )
    xbrl.parser.context_period_map["instant"] = "instant_2024-06-30"
    for kind, concept, context in (
        ("BalanceSheet", "us-gaap_Assets", "instant"),
        ("CashFlowStatement", "us-gaap_NetCashProvidedByUsedInOperatingActivities", "y"),
    ):
        xbrl.parser.facts[concept] = Fact(
            element_id=concept, context_ref=context, value="42.123456", unit_ref="u1"
        )
        xbrl.parser.presentation_trees[kind] = SimpleNamespace(
            all_nodes={concept: PresentationNode(element_id=concept, standard_label=concept)}
        )
    xbrl.find_statement = lambda kind: (
        [],
        "fake:IncomeRole" if kind == "IncomeStatement" else kind,
        kind,
    )
    financials = SimpleNamespace(
        balance_sheet=lambda: Statement(xbrl, "BalanceSheet"),
        income_statement=lambda: income,
        cash_flow_statement=lambda: Statement(xbrl, "CashFlowStatement"),
    )
    filing = SimpleNamespace(
        form="10-Q",
        filing_date="2024-08-01",
        cik=1,
        company="Synthetic",
        accession_number="fake-fixed",
        period_of_report="2024-06-30",
        header=SimpleNamespace(acceptance_datetime=datetime(2024, 8, 1, tzinfo=UTC)),
        obj=lambda: SimpleNamespace(financials=financials),
    )
    monkeypatch.setattr(
        "edgar.Company", lambda symbol: SimpleNamespace(get_filings=lambda **kw: [filing])
    )
    monkeypatch.setattr("edgar.set_identity", lambda value: None)
    result = SecFinancialsClient("Synthetic test@example.invalid").fetch_company_financials(
        SecFinancialsRequest(("FAKE",), limit=1, include_amendments=False, include_dimensions=False)
    )[0]
    assert {row.statement_type for row in result.rows} == {
        "balance_sheet",
        "income_statement",
        "cash_flow",
    }
    assert not any("PARSE_FAILED" in flag for flag in result.quality_flags)
    assert len(result.filter_statement("income_statement")) == 2
    assert result.filter_statement("cash_flow")[0].value == Decimal("42.123456")


def test_native_dimensions_preserved_and_unknown_units_stay_unknown():
    statement, xbrl = native_statement()
    del xbrl.units["u1"]
    rows = parse_statement_rows(statement, "income_statement", include_dimensions=True)
    assert len(rows) == 3
    segment = next(r for r in rows if r.context_ref == "d")
    assert segment.dimension == '{"fake:Axis":"fake:Member"}'
    assert segment.value_native == "7.0000"
    assert segment.unit is None


def test_conflicting_native_context_values_rejected():
    statement, xbrl = native_statement()
    xbrl.parser.facts["conflict"] = Fact(
        element_id="us-gaap_OperatingIncomeLoss", context_ref="q", value="8", unit_ref="u1"
    )
    with pytest.raises(SecStatementParseError, match="conflicting native"):
        parse_statement_rows(statement, "income_statement")


def test_display_suffixes_retain_identity_and_unknown_starts():
    frame = pd.DataFrame(
        {
            "concept": ["fake:Revenue"],
            "label": ["Revenue"],
            "unit": [pd.NA],
            "2024-06-30 (Q2)": [10],
            "2024-06-30 (YTD)": [20],
            "2023-12-31 (FY)": [30],
        }
    )
    statement = SimpleNamespace(to_dataframe=lambda **kw: frame)
    rows = parse_statement_rows(statement, "income_statement")
    assert len(rows) == 3
    assert len({r.period_key for r in rows}) == 3
    assert all(
        r.period_start is None and r.unit is None and r.period_type == "duration" for r in rows
    )
    assert rows[0].period_source == "display-column-unknown-start"


def test_instant_has_no_duration_start_and_bool_na_rows_are_filtered():
    frame = pd.DataFrame(
        {
            "concept": ["fake:Header", "fake:Bool", "fake:NA", "fake:Assets"],
            "abstract": [np.bool_(True), False, pd.NA, False],
            "2024-06-30": [100, np.bool_(True), pd.NA, 40],
        }
    )
    rows = parse_statement_rows(SimpleNamespace(to_dataframe=lambda **kw: frame), "balance_sheet")
    assert len(rows) == 1
    assert rows[0].value == Decimal(40)
    assert rows[0].is_point_in_time and rows[0].period_start is None


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), Decimal("Infinity"), Decimal("NaN")])
def test_nonfinite_rejected(bad):
    statement = SimpleNamespace(
        get_raw_data=lambda: [
            {"concept": "fake:Income", "values": {"duration_2024-01-01_2024-06-30": bad}}
        ]
    )
    with pytest.raises(SecStatementParseError, match="non-finite"):
        parse_statement_rows(statement, "income_statement")


def test_unknown_and_mixed_unknown_columns_are_errors():
    for cols in ({"bad-quarter": [1]}, {"2024-06-30": [1], "bad-quarter": [2]}):
        frame = pd.DataFrame({"concept": ["fake:Cash"], **cols})
        with pytest.raises(SecStatementParseError, match="unrecognized"):
            parse_statement_rows(
                SimpleNamespace(to_dataframe=lambda frame=frame, **kw: frame), "balance_sheet"
            )


@pytest.mark.parametrize("legacy_version", ["v1", "v2"])
def test_v3_parquet_roundtrip_identity_and_old_schema_rejection(tmp_path, legacy_version):
    statement, _ = native_statement()
    rows = tuple(parse_statement_rows(statement, "income_statement", include_dimensions=True))
    vintage = SecCompanyFinancialVintage(
        "FAKE",
        "0000000001",
        "Synthetic",
        "10-Q",
        "fake-2024-q2",
        date(2024, 8, 1),
        accepted_at=datetime(2024, 8, 1, tzinfo=UTC),
        rows=rows,
        quality_flags=("BALANCE_SHEET_MISSING",),
    )
    manifest = write_financials_partition(tmp_path, "FAKE", [vintage])
    assert manifest["dataset_schema"] == "sec-company-financials-v3"
    partition = tmp_path / "symbol=FAKE"
    data = pq.ParquetFile(partition / "financial_statements.parquet").read().to_pylist()
    by_context = {r["context_ref"]: r for r in data}
    for record in by_context.values():
        record["value"] = Decimal(record["value"]) if record["value"] is not None else None
    for row in rows:
        assert all(
            by_context[row.context_ref][key] == value for key, value in row.to_dict().items()
        )
    stored = pq.ParquetFile(partition / "company_financial_vintages.parquet").read().to_pylist()[0]
    assert stored["quality_flags"] == ["BALANCE_SHEET_MISSING"]
    assert stored["vintage_identity"] == vintage.vintage_identity
    assert (
        replace(vintage, rows=(replace(rows[0], period_start=date(2024, 1, 1)),)).vintage_identity
        != vintage.vintage_identity
    )
    manifest["dataset_schema"] = f"sec-company-financials-{legacy_version}"
    manifest["writer_profile"] = f"sec-financials-parquet-{legacy_version}"
    (partition / "manifest.json").write_text(json.dumps(manifest))
    before = {path.name: path.read_bytes() for path in partition.iterdir()}
    with pytest.raises(ValueError, match="incompatible"):
        write_financials_partition(tmp_path, "FAKE", [vintage])
    assert {path.name: path.read_bytes() for path in partition.iterdir()} == before


def test_currency_column_is_required_by_actual_parquet_schema(tmp_path):
    row = SecStatementRow(
        "income_statement", "Revenue", "fake:Revenue", "Revenue", Decimal(1), "1", "USD"
    )
    vintage = SecCompanyFinancialVintage(
        "FAKE", "0000000001", "Synthetic", "10-Q", "fake-quarter", date(2024, 8, 1), rows=(row,)
    )
    write_financials_partition(tmp_path, "FAKE", [vintage])
    partition = tmp_path / "symbol=FAKE"
    table_path = partition / "financial_statements.parquet"
    table = pq.ParquetFile(table_path).read().drop(["currency"])
    pq.write_table(table, table_path)
    manifest_path = partition / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["financial_statements.parquet"] = {
        "sha256": hashlib.sha256(table_path.read_bytes()).hexdigest(),
        "bytes": table_path.stat().st_size,
    }
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="table schema"):
        validate_financials_partition(partition)


def test_old_positional_row_constructor_remains_compatible():
    row = SecStatementRow(
        "balance_sheet",
        "Assets",
        "fake:Assets",
        "Assets",
        Decimal(1),
        "1",
        None,
        None,
        None,
        date(2024, 6, 30),
        True,
    )
    assert row.is_point_in_time is True


def test_equal_revenue_concepts_survive_provider_presentation_deduplication():
    statement, xbrl = native_statement()
    tree = xbrl.presentation_trees["fake:IncomeRole"]
    for concept in (
        "us-gaap_Revenues",
        "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
    ):
        tree.all_nodes[concept] = PresentationNode(element_id=concept, standard_label="Revenue")
        xbrl.parser.facts[concept] = Fact(
            element_id=concept, context_ref="q", value="12.12345", unit_ref="u1"
        )

    def display_must_not_be_used(*args):
        raise AssertionError("deduplicated provider display must not be consulted")

    xbrl.get_statement = display_must_not_be_used
    rows = parse_statement_rows(statement, "income_statement")
    revenue = [r for r in rows if r.label == "Revenue"]
    assert len(revenue) == 2
    assert len({r.concept for r in revenue}) == 2
    assert all(r.value == Decimal("12.12345") for r in revenue)


def test_arbitrary_precision_roundtrip_and_failed_write_leave_existing_untouched(tmp_path):
    row = SecStatementRow(
        "income_statement",
        "Revenue",
        "fake:Revenue",
        "Revenue",
        Decimal("1.123456789012345678901234567890123456789"),
        "1.123456789012345678901234567890123456789",
    )
    vintage = SecCompanyFinancialVintage(
        "FAKE", "0000000001", "Synthetic", "10-Q", "fake-quarter", date(2024, 8, 1), rows=(row,)
    )
    manifest = write_financials_partition(tmp_path, "FAKE", [vintage])
    partition = tmp_path / "symbol=FAKE"
    record = pq.ParquetFile(partition / "financial_statements.parquet").read().to_pylist()[0]
    assert Decimal(record["value"]) == row.value
    before = {p.name: p.read_bytes() for p in partition.iterdir()}
    assert write_financials_partition(tmp_path, "FAKE", [vintage]) == manifest
    with pytest.raises(ValueError, match="immutable"):
        write_financials_partition(
            tmp_path, "FAKE", [replace(vintage, rows=(replace(row, value=Decimal(2)),))]
        )
    assert {p.name: p.read_bytes() for p in partition.iterdir()} == before


def test_interrupted_table_write_never_publishes_partial_partition(tmp_path, monkeypatch):
    original = pq.write_table
    calls = 0

    def fail_second(table, path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic disk failure")
        return original(table, path, **kwargs)

    monkeypatch.setattr(pq, "write_table", fail_second)
    with pytest.raises(OSError, match="synthetic"):
        write_financials_partition(tmp_path, "FAKE", [])
    assert not (tmp_path / "symbol=FAKE").exists()
    assert not list(tmp_path.glob(".financials-*"))


@pytest.mark.parametrize("command", ["inspect", "validate"])
def test_cli_rejects_old_manifest_and_old_table_schema(tmp_path, command, capsys):
    from ohmydata.cli import main

    manifest = write_financials_partition(tmp_path, "FAKE", [])
    manifest["dataset_schema"] = "sec-company-financials-v1"
    path = tmp_path / "symbol=FAKE" / "manifest.json"
    path.write_text(json.dumps(manifest))
    args = ["sec", "financials", command, "--root", str(tmp_path), "--json"]
    if command == "inspect":
        args.extend(["--symbol", "FAKE"])
    assert main(args) == 2
    assert "incompatible" in capsys.readouterr().err


def test_manifest_cannot_omit_tables_or_relabel_legacy_table(tmp_path):
    import hashlib

    manifest = write_financials_partition(tmp_path, "FAKE", [])
    partition = tmp_path / "symbol=FAKE"
    path = partition / "manifest.json"
    path.write_text(json.dumps({**manifest, "files": {}}))
    with pytest.raises(ValueError, match="required tables"):
        validate_financials_partition(partition)
    table_path = partition / "financial_statements.parquet"
    table = (
        pq.ParquetFile(table_path)
        .read()
        .replace_schema_metadata({b"omd.dataset_schema": b"sec-company-financials-v1"})
    )
    pq.write_table(table, table_path)
    manifest["files"][table_path.name] = {
        "sha256": hashlib.sha256(table_path.read_bytes()).hexdigest(),
        "bytes": table_path.stat().st_size,
    }
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="table schema"):
        validate_financials_partition(partition)


def test_concurrent_partition_publish_remains_complete(tmp_path):
    def write():
        try:
            return write_financials_partition(tmp_path, "FAKE", [])
        except OSError:
            return None  # A concurrent publisher won the atomic rename.

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: write(), range(2)))
    assert any(result is not None for result in results)
    manifest = validate_financials_partition(tmp_path / "symbol=FAKE")
    assert manifest["vintage_count"] == 0
    assert not list(tmp_path.glob(".financials-*"))


def test_native_fact_index_scans_source_once_for_many_unrelated_facts():
    statement, xbrl = native_statement()

    class CountingFacts(dict):
        calls = 0
        visited = 0

        def values(self):
            self.calls += 1
            for value in super().values():
                self.visited += 1
                yield value

    facts = CountingFacts(xbrl.parser.facts)
    for i in range(2000):
        facts[f"irrelevant-{i}"] = Fact(
            element_id="fake:Unrelated", context_ref="unused", value="1"
        )
    xbrl.parser.facts = facts
    rows = parse_statement_rows(statement, "income_statement")
    assert facts.calls == 1
    assert facts.visited == 2003
    assert len(rows) == 2


def test_identical_facts_with_distinct_context_ids_are_both_retained():
    statement, xbrl = native_statement()
    xbrl.contexts["q2"] = xbrl.contexts["q"].model_copy(update={"context_id": "q2"})
    xbrl.context_period_map["q2"] = xbrl.context_period_map["q"]
    xbrl.parser.facts["q2"] = xbrl.parser.facts["q"].model_copy(update={"context_ref": "q2"})
    rows = parse_statement_rows(statement, "income_statement")
    assert {r.context_ref for r in rows} == {"q", "q2", "y"}
    assert len(rows) == 3


@pytest.mark.parametrize(
    "field,value", [("symbol", "OTHER"), ("vintage_count", 99), ("statement_row_count", 99)]
)
def test_relabelled_manifest_and_corrupt_counts_are_rejected(tmp_path, field, value):
    manifest = write_financials_partition(tmp_path, "FAKE", [])
    partition = tmp_path / "symbol=FAKE"
    manifest[field] = value
    (partition / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="identity|count"):
        validate_financials_partition(partition)


def test_writer_rejects_noncanonical_symbol_before_creating_partition(tmp_path):
    vintage = SecCompanyFinancialVintage(
        "fake", "0000000001", "Synthetic", "10-Q", "fake-quarter", date(2024, 8, 1)
    )
    with pytest.raises(ValueError, match="uppercase"):
        write_financials_partition(tmp_path, "FAKE", [vintage])
    assert not (tmp_path / "symbol=FAKE").exists()

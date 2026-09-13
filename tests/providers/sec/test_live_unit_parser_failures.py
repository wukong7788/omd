"""Actual pinned-parser failures must reject the live v2 result as a whole."""

from __future__ import annotations

import socket
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ohmydata.providers.sec import SecFinancialsClient, SecFinancialsRequest, SecUnitEvidenceError
from ohmydata.providers.sec._statement_parser import parse_statement_rows

XBRL = pytest.importorskip("edgar.xbrl").XBRL
Statement = pytest.importorskip("edgar.xbrl.statements").Statement
PresentationNode = pytest.importorskip("edgar.xbrl.models").PresentationNode
V2 = "sec-live-financial-parser-v2-edgartools-5.56.0"


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("network access")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


def raw_instance(*, revenue="7", extra="", unit_ref='unitRef="usd"'):
    return f"""<xbrl xmlns="http://www.xbrl.org/2003/instance"
      xmlns:fake="https://example.invalid/taxonomy"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
      <context id="q"><entity><identifier scheme="http://www.sec.gov/CIK">1</identifier></entity>
        <period><startDate>2024-01-01</startDate><endDate>2024-03-31</endDate></period></context>
      <context id="i"><entity><identifier scheme="http://www.sec.gov/CIK">1</identifier></entity>
        <period><instant>2024-03-31</instant></period></context>
      <unit id="usd"><measure>iso4217:USD</measure></unit>
      <fake:EntityName contextRef="q">Synthetic Filing Company</fake:EntityName>
      <fake:Revenue contextRef="q" {unit_ref} decimals="0">{revenue}</fake:Revenue>
      <fake:Assets contextRef="i" unitRef="usd" decimals="0">20</fake:Assets>{extra}
    </xbrl>""".encode()


def native(raw, concept="fake_Revenue", kind="IncomeStatement"):
    xbrl = XBRL()
    xbrl.parser.parse_instance_content(raw.decode())
    role = "https://example.invalid/role/Synthetic"
    xbrl.parser.presentation_trees = {
        role: SimpleNamespace(
            all_nodes={concept: PresentationNode(element_id=concept, standard_label="Synthetic")}
        )
    }
    xbrl.find_statement = lambda *args: ([], role, kind)
    return Statement(xbrl, kind)


@pytest.mark.parametrize("statement", [None, SimpleNamespace(get_raw_data=list), []])
def test_explicit_v2_rejects_non_native_statement(statement):
    with pytest.raises(SecUnitEvidenceError):
        parse_statement_rows(
            statement, "income_statement", parser_version=V2, raw_instance=raw_instance()
        )


def test_v2_raw_native_value_mismatch_fails():
    with pytest.raises(SecUnitEvidenceError, match="does not match"):
        parse_statement_rows(
            native(raw_instance(revenue="8")),
            "income_statement",
            parser_version=V2,
            raw_instance=raw_instance(),
        )


@pytest.mark.parametrize("unit_ref", ["", 'unitRef="missing"'])
def test_v2_missing_native_unit_reference_fails(unit_ref):
    raw = raw_instance(unit_ref=unit_ref)
    with pytest.raises(SecUnitEvidenceError):
        parse_statement_rows(native(raw), "income_statement", parser_version=V2, raw_instance=raw)


def test_conflicting_duplicate_raw_fact_is_not_hidden_by_parser_mapping():
    raw = raw_instance(
        extra='<fake:Revenue contextRef="q" unitRef="usd" decimals="0">8</fake:Revenue>'
    )
    with pytest.raises(SecUnitEvidenceError, match="conflict"):
        parse_statement_rows(
            native(raw_instance()), "income_statement", parser_version=V2, raw_instance=raw
        )


def test_identical_duplicate_and_non_numeric_raw_facts_are_permitted():
    raw = raw_instance(
        extra='<fake:Revenue contextRef="q" unitRef="usd" decimals="0">7</fake:Revenue>'
    )
    rows = parse_statement_rows(
        native(raw_instance()), "income_statement", parser_version=V2, raw_instance=raw
    )
    assert len(rows) == 1 and rows[0].value == 7


def test_nvda_like_compatible_pair_is_permitted():
    # NVDA IncomeTaxExpenseBenefit: USD 11,800,000,000 decimals=-8 and USD 11,819,000,000 decimals=-6
    # Strictly positive overlap exists -> compatible
    raw = raw_instance(
        revenue="11800000000",
        extra='<fake:Revenue contextRef="q" unitRef="usd" decimals="-6">11819000000</fake:Revenue>',
    ).replace(b'decimals="0"', b'decimals="-8"')
    stmt = native(raw_instance(revenue="11819000000").replace(b'decimals="0"', b'decimals="-6"'))
    rows = parse_statement_rows(stmt, "income_statement", parser_version=V2, raw_instance=raw)
    assert len(rows) == 1
    assert rows[0].value == 11819000000
    assert rows[0].decimals == -6


def test_equal_value_with_different_decimals_is_compatible():
    raw = raw_instance(
        extra='<fake:Revenue contextRef="q" unitRef="usd" decimals="2">7</fake:Revenue>'
    )
    rows = parse_statement_rows(
        native(raw_instance()), "income_statement", parser_version=V2, raw_instance=raw
    )
    assert len(rows) == 1 and rows[0].value == 7


def test_differing_unit_ref_is_conflict():
    raw = (
        raw_instance()
        .replace(
            b"</unit>",
            b'</unit><unit id="eur"><measure>iso4217:EUR</measure></unit>',
        )
        .replace(
            b"</xbrl>",
            b'<fake:Revenue contextRef="q" unitRef="eur" decimals="0">7</fake:Revenue></xbrl>',
        )
    )
    with pytest.raises(SecUnitEvidenceError, match="conflict"):
        parse_statement_rows(
            native(raw_instance()), "income_statement", parser_version=V2, raw_instance=raw
        )


def test_inf_decimals_behavior():
    # 1. INF vs INF, same value -> compatible
    raw_inf_same = raw_instance(
        extra='<fake:Revenue contextRef="q" unitRef="usd" decimals="INF">7</fake:Revenue>'
    ).replace(b'decimals="0"', b'decimals="INF"')
    rows = parse_statement_rows(
        native(raw_instance().replace(b'decimals="0"', b'decimals="INF"')),
        "income_statement",
        parser_version=V2,
        raw_instance=raw_inf_same,
    )
    assert len(rows) == 1 and rows[0].value == 7

    # 2. INF vs finite decimals, same value -> compatible
    raw_inf_finite = raw_instance(
        extra='<fake:Revenue contextRef="q" unitRef="usd" decimals="INF">7</fake:Revenue>'
    )
    rows = parse_statement_rows(
        native(raw_instance()), "income_statement", parser_version=V2, raw_instance=raw_inf_finite
    )
    assert len(rows) == 1 and rows[0].value == 7

    # 3. INF vs INF, different value -> conflict
    raw_inf_diff = raw_instance(
        extra='<fake:Revenue contextRef="q" unitRef="usd" decimals="INF">8</fake:Revenue>'
    ).replace(b'decimals="0"', b'decimals="INF"')
    with pytest.raises(SecUnitEvidenceError, match="conflict"):
        parse_statement_rows(
            native(raw_instance().replace(b'decimals="0"', b'decimals="INF"')),
            "income_statement",
            parser_version=V2,
            raw_instance=raw_inf_diff,
        )

    # 4. INF vs finite decimals, different value -> conflict
    raw_inf_finite_diff = raw_instance(
        extra='<fake:Revenue contextRef="q" unitRef="usd" decimals="INF">8</fake:Revenue>'
    )
    with pytest.raises(SecUnitEvidenceError, match="conflict"):
        parse_statement_rows(
            native(raw_instance()),
            "income_statement",
            parser_version=V2,
            raw_instance=raw_inf_finite_diff,
        )


def test_boundary_touch_conflict_and_strictly_positive_overlap():
    # Boundary touch: 100 with decimals=-1 (H1=5) and 105.5 with decimals=0 (H2=0.5)
    # diff is 5.5 == H1+H2 -> mere boundary-touch is not enough -> conflict
    raw_touch = raw_instance(
        revenue="100",
        extra='<fake:Revenue contextRef="q" unitRef="usd" decimals="0">105.5</fake:Revenue>',
    ).replace(b'decimals="0"', b'decimals="-1"', 1)
    with pytest.raises(SecUnitEvidenceError, match="conflict"):
        parse_statement_rows(
            native(raw_instance(revenue="100").replace(b'decimals="0"', b'decimals="-1"')),
            "income_statement",
            parser_version=V2,
            raw_instance=raw_touch,
        )

    # Strictly positive overlap: 100 with decimals=-1 (H1=5) and 105.4 with decimals=0 (H2=0.5)
    # diff is 5.4 < H1+H2=5.5 -> compatible
    raw_overlap = raw_instance(
        revenue="100",
        extra='<fake:Revenue contextRef="q" unitRef="usd" decimals="0">105.4</fake:Revenue>',
    ).replace(b'decimals="0"', b'decimals="-1"', 1)
    stmt = native(raw_instance(revenue="105.4"))
    rows = parse_statement_rows(
        stmt, "income_statement", parser_version=V2, raw_instance=raw_overlap
    )
    assert len(rows) == 1 and rows[0].value == Decimal("105.4")


@pytest.mark.parametrize("bad_decimals", ['decimals="bad"', 'decimals="1.5"'])
def test_malformed_decimals_fail_closed_on_duplicate(bad_decimals):
    raw = raw_instance(
        extra=f'<fake:Revenue contextRef="q" unitRef="usd" {bad_decimals}>7</fake:Revenue>'
    )
    with pytest.raises(SecUnitEvidenceError, match="conflict"):
        parse_statement_rows(
            native(raw_instance()), "income_statement", parser_version=V2, raw_instance=raw
        )


@pytest.mark.parametrize("bad_val", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numeric_value_fails_closed(bad_val):
    raw = raw_instance(
        extra=f'<fake:Revenue contextRef="q" unitRef="usd" decimals="0">{bad_val}</fake:Revenue>'
    )
    with pytest.raises(SecUnitEvidenceError):
        parse_statement_rows(
            native(raw_instance()), "income_statement", parser_version=V2, raw_instance=raw
        )


@pytest.mark.parametrize(
    "definition,expected,currency",
    [
        ("<measure>iso4217:USD</measure>", "iso4217:USD", "USD"),
        (
            (
                "<divide><unitNumerator><measure>iso4217:USD</measure></unitNumerator>"
                "<unitDenominator><measure>shares</measure></unitDenominator></divide>"
            ),
            '{"denominator":["shares"],"numerator":["iso4217:USD"],"type":"divide"}',
            None,
        ),
        (
            "<measure>iso4217:USD</measure><measure>shares</measure>",
            '{"measures":["iso4217:USD","shares"],"type":"product"}',
            None,
        ),
    ],
)
def test_actual_v2_units_preserve_every_other_native_field(definition, expected, currency):
    raw = raw_instance().replace(b"<measure>iso4217:USD</measure>", definition.encode())
    statement = native(raw)
    legacy = parse_statement_rows(statement, "income_statement")
    rows = parse_statement_rows(statement, "income_statement", parser_version=V2, raw_instance=raw)
    assert len(rows) == len(legacy) == 1
    assert rows[0].unit == expected and rows[0].currency == currency
    assert replace(rows[0], unit=None) == replace(legacy[0], unit=None)


def test_actual_v2_entity_segment_dimension_is_corroborated():
    raw = (
        raw_instance()
        .replace(
            b'xmlns:fake="https://example.invalid/taxonomy"',
            b'xmlns:fake="https://example.invalid/taxonomy" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"',
        )
        .replace(
            b"</identifier></entity>",
            b'</identifier><segment><xbrldi:explicitMember dimension="fake:Axis">fake:Member</xbrldi:explicitMember></segment></entity>',
        )
    )
    statement = native(raw)
    rows = parse_statement_rows(
        statement, "income_statement", include_dimensions=True, parser_version=V2, raw_instance=raw
    )
    assert len(rows) == 1 and rows[0].dimension == '{"fake:Axis":"fake:Member"}'
    assert rows == parse_statement_rows(statement, "income_statement", include_dimensions=True)
    with pytest.raises(SecUnitEvidenceError, match="does not match"):
        parse_statement_rows(
            statement,
            "income_statement",
            include_dimensions=True,
            parser_version=V2,
            raw_instance=raw.replace(b"fake:Member", b"fake:OtherMember"),
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"<xbrl",
        b"\xff",
        raw_instance(extra='<unit id="unused"/>'),
        raw_instance(extra='<unit id="usd"><measure>iso4217:USD</measure></unit>'),
    ],
)
def test_malformed_evidence_raises_public_unit_error(raw):
    with pytest.raises(SecUnitEvidenceError):
        parse_statement_rows(
            native(raw_instance()), "income_statement", parser_version=V2, raw_instance=raw
        )


@pytest.mark.parametrize("mismatch", [False, True])
def test_client_prepares_once_and_never_returns_partial_evidence_failure(monkeypatch, mismatch):
    import edgar

    import ohmydata.providers.sec._live_unit_corroboration as corroboration

    raw = raw_instance()
    first = native(raw, "fake_Assets", "BalanceSheet")
    second = native(raw_instance(revenue="8" if mismatch else "7"))
    calls = []
    prepared = []
    prepare = corroboration.prepare_raw_instance

    def counted_prepare(*args, **kwargs):
        prepared.append(args[0])
        return prepare(*args, **kwargs)

    monkeypatch.setattr(corroboration, "prepare_raw_instance", counted_prepare)

    def statement(name, value):
        calls.append(name)
        return value

    fin = SimpleNamespace(
        balance_sheet=lambda: statement("balance_sheet", first),
        income_statement=lambda: statement("income_statement", second),
        cash_flow_statement=lambda: statement("cash_flow", None),
    )
    attachment = SimpleNamespace(
        document="fake.xml",
        document_type="EX-101.INS",
        extension=".xml",
        url="https://www.sec.gov/Archives/edgar/data/1/000000000124000001/fake.xml",
        sgml_document=SimpleNamespace(content=raw.decode()),
    )
    filing = SimpleNamespace(
        cik=1,
        accession_number="0000000001-24-000001",
        form="10-Q",
        filing_date="2024-05-01",
        company="Synthetic Filing Company",
        attachments=SimpleNamespace(data_files=[attachment]),
        obj=lambda: SimpleNamespace(financials=fin),
    )
    monkeypatch.setattr(edgar, "set_identity", lambda _: None)
    monkeypatch.setattr(
        edgar, "Company", lambda _: SimpleNamespace(get_filings=lambda **kwargs: [filing])
    )
    client = SecFinancialsClient("Synthetic contact@example.invalid")
    request = SecFinancialsRequest(symbols=("FAKE",))
    if mismatch:
        with pytest.raises(SecUnitEvidenceError, match="does not match"):
            client.fetch_company_financials(request)
        assert calls == ["balance_sheet", "income_statement"]
    else:
        vintages = client.fetch_company_financials(request)
        assert len(vintages) == 1 and len(vintages[0].rows) == 2
        assert vintages[0].unit_evidence is not None
        assert vintages[0].unit_evidence.parser_version == V2
        assert calls == ["balance_sheet", "income_statement", "cash_flow"]
    assert prepared == [raw]

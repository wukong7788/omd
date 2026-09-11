"""Offline full-SGML financial production tests using the installed parser."""

import socket
from datetime import UTC, datetime

import pytest

from ohmydata.core import RequestSpec, SnapshotStore
from ohmydata.providers.sec import SecSgmlFinancialsRequest, produce_sec_financials_from_sgml
from ohmydata.providers.sec.sgml_financials import _documents, _header

pytest.importorskip("edgar")


def _raw(
    *,
    acceptance: str = "20240501170000",
    components: bool = True,
    dimension: bool = False,
    wrapped_components: bool = False,
    xml_declaration: bool = False,
) -> bytes:
    documents = (
        ""
        if not components
        else """
<DOCUMENT>
<TYPE>EX-101.SCH
<FILENAME>fake.xsd
<TEXT><xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:link="http://www.xbrl.org/2003/linkbase" targetNamespace="http://fasb.org/us-gaap/2024" xmlns:us-gaap="http://fasb.org/us-gaap/2024"><xs:element name="IncomeStatementAbstract" id="IncomeStatementAbstract" abstract="true"/><xs:element name="Revenues" id="Revenues" type="xs:decimal" xbrli:periodType="duration"/><link:roleType id="ConsolidatedStatementsOfIncome" roleURI="http://example.invalid/role/ConsolidatedStatementsOfIncome"><link:definition>Consolidated Statements of Income</link:definition></link:roleType></xs:schema></TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>EX-101.PRE
<FILENAME>fake_pre.xml
<TEXT><link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink"><link:presentationLink xlink:role="http://example.invalid/role/ConsolidatedStatementsOfIncome"><link:loc xlink:label="root" xlink:href="fake.xsd#us-gaap_IncomeStatementAbstract"/><link:loc xlink:label="revenue" xlink:href="fake.xsd#us-gaap_Revenues"/><link:presentationArc xlink:from="root" xlink:to="revenue" order="1"/></link:presentationLink></link:linkbase></TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>EX-101.LAB
<FILENAME>fake_lab.xml
<TEXT><link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink"><link:labelLink><link:loc xlink:label="revenue" xlink:href="fake.xsd#us-gaap_Revenues"/><link:label xlink:label="label" xlink:role="http://www.xbrl.org/2003/role/label">Revenue</link:label><link:labelArc xlink:from="revenue" xlink:to="label"/></link:labelLink></link:linkbase></TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>EX-101.INS
<FILENAME>fake.xml
<TEXT><xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:us-gaap="http://fasb.org/us-gaap/2024" xmlns:dei="http://xbrl.sec.gov/dei/2024" xmlns:iso4217="http://www.xbrl.org/2003/iso4217"><context id="c1"><entity><identifier scheme="http://www.sec.gov/CIK">0000000001</identifier></entity><period><startDate>2024-01-01</startDate><endDate>2024-03-31</endDate></period></context><unit id="usd"><measure>iso4217:USD</measure></unit><us-gaap:Revenues contextRef="c1" unitRef="usd" decimals="0">123</us-gaap:Revenues><dei:EntityCentralIndexKey contextRef="c1">0000000001</dei:EntityCentralIndexKey></xbrl></TEXT>
</DOCUMENT>"""
    )
    if dimension:
        documents = documents.replace(
            'xmlns:iso4217="http://www.xbrl.org/2003/iso4217">',
            'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
            'xmlns:xbrldi="http://xbrl.org/2006/xbrldi">',
        ).replace(
            "</identifier></entity>",
            "</identifier><segment><xbrldi:explicitMember "
            'dimension="us-gaap:ProductOrServiceAxis">us-gaap:ProductMember'
            "</xbrldi:explicitMember></segment></entity>",
        )
    if wrapped_components:
        declaration = '<?xml version="1.0"?>\n' if xml_declaration else ""
        documents = documents.replace("<TEXT>", f"<TEXT><XBRL>\n{declaration}").replace(
            "</TEXT>", "\n</XBRL></TEXT>"
        )
    return f"""<SEC-DOCUMENT>0000000001-24-000001.txt\n<SEC-HEADER>\nACCESSION NUMBER: 0000000001-24-000001\nCONFORMED SUBMISSION TYPE: 10-Q\nFILED AS OF DATE: 20240501\nDATE AS OF CHANGE: 20240501\n<ACCEPTANCE-DATETIME>{acceptance}\nFILER:\n\tCOMPANY DATA:\n\t\tCONFORMED NAME: Synthetic Filing Co.\n\t\tCENTRAL INDEX KEY: 0000000001\nCONFORMED PERIOD OF REPORT: 20240331\n</SEC-HEADER>\n{documents}\n""".encode()


def _request() -> SecSgmlFinancialsRequest:
    return SecSgmlFinancialsRequest(
        "FAKE", "0000000001", "0000000001-24-000001", "10-Q", ("income_statement",), False
    )


def test_modern_company_conformed_name_ignores_former_name_history():
    raw = _raw().replace(
        b"CONFORMED NAME: Synthetic Filing Co.",
        b"COMPANY CONFORMED NAME: Synthetic Filing Co.\n"
        b"FORMER COMPANY: Example Former Issuer\n"
        b"FORMER CONFORMED NAME: Former Filing Co.",
    )

    _, _, _, company_name = _header(raw.decode("utf-8"), _request())

    assert company_name == "Synthetic Filing Co."


def test_modern_company_conformed_name_preserves_legacy_vintage_rows(tmp_path, monkeypatch):
    legacy_source, legacy_observation = _observation(tmp_path / "legacy", _raw())
    modern_raw = _raw().replace(
        b"CONFORMED NAME: Synthetic Filing Co.", b"COMPANY CONFORMED NAME: Synthetic Filing Co."
    )
    modern_source, modern_observation = _observation(tmp_path / "modern", modern_raw)
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))

    legacy = produce_sec_financials_from_sgml(
        source_store=legacy_source,
        source_observation=legacy_observation,
        projection_store=SnapshotStore(tmp_path / "legacy-projection"),
        request=_request(),
        produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
    )
    modern = produce_sec_financials_from_sgml(
        source_store=modern_source,
        source_observation=modern_observation,
        projection_store=SnapshotStore(tmp_path / "modern-projection"),
        request=_request(),
        produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
    )

    assert modern.vintage.vintage_identity == legacy.vintage.vintage_identity
    assert modern.vintage.rows == legacy.vintage.rows


@pytest.mark.parametrize("xml_declaration", [False, True])
def test_wrapped_modern_components_preserve_rows_and_raw_bytes(
    tmp_path, monkeypatch, xml_declaration
):
    bare_raw = _raw().replace(
        b"CONFORMED NAME: Synthetic Filing Co.", b"COMPANY CONFORMED NAME: Synthetic Filing Co."
    )
    wrapped_raw = _raw(wrapped_components=True, xml_declaration=xml_declaration).replace(
        b"CONFORMED NAME: Synthetic Filing Co.", b"COMPANY CONFORMED NAME: Synthetic Filing Co."
    )
    bare_source, bare_observation = _observation(tmp_path / "bare", bare_raw)
    wrapped_source, wrapped_observation = _observation(tmp_path / "wrapped", wrapped_raw)
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))

    bare = produce_sec_financials_from_sgml(
        source_store=bare_source,
        source_observation=bare_observation,
        projection_store=SnapshotStore(tmp_path / "bare-projection"),
        request=_request(),
        produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
    )
    wrapped = produce_sec_financials_from_sgml(
        source_store=wrapped_source,
        source_observation=wrapped_observation,
        projection_store=SnapshotStore(tmp_path / "wrapped-projection"),
        request=_request(),
        produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
    )

    assert wrapped.vintage.vintage_identity == bare.vintage.vintage_identity
    assert wrapped.vintage.rows == bare.vintage.rows
    assert wrapped_source.replay_observation(wrapped_observation).payload == wrapped_raw


@pytest.mark.parametrize(
    "raw",
    [
        _raw(wrapped_components=True).replace(b"</XBRL>", b"", 1),
        _raw(wrapped_components=True).replace(b"<XBRL>", b"<XBRL><XBRL>", 1),
        _raw(wrapped_components=True).replace(b"</XBRL>", b"</XBRL><XBRL>duplicate</XBRL>", 1),
        _raw(wrapped_components=True).replace(b"<XBRL>", b"outside<XBRL>", 1),
        _raw(wrapped_components=True).replace(b"</XBRL>", b"</XBRL>outside", 1),
    ],
    ids=["missing-close", "nested-wrapper", "duplicate-paired", "nonempty-prefix", "suffix"],
)
def test_malformed_component_wrapper_fails_closed(raw):
    with pytest.raises(ValueError, match="XBRL wrapper"):
        _documents(raw.decode("utf-8"))


def test_wrapped_component_doctype_fails_before_projection_write(tmp_path):
    raw = _raw(wrapped_components=True).replace(
        b"<XBRL>\n", b"<XBRL>\n<!DOCTYPE x [<!ENTITY e 'x'>]>\n", 1
    )
    source, observation = _observation(tmp_path, raw)
    projection = SnapshotStore(tmp_path / "projection")

    with pytest.raises(ValueError, match="unsafe XML"):
        produce_sec_financials_from_sgml(
            source_store=source,
            source_observation=observation,
            projection_store=projection,
            request=_request(),
            produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
        )

    assert not list(projection.root.rglob("response.bin"))


@pytest.mark.parametrize(
    ("replacement", "case"),
    [
        (
            b"COMPANY CONFORMED NAME: Current Filing Co.\nCONFORMED NAME: Legacy Filing Co.",
            "modern-and-legacy",
        ),
        (
            b"COMPANY CONFORMED NAME: Current Filing Co.\nCOMPANY CONFORMED NAME: Duplicate Filing Co.",
            "duplicate-modern",
        ),
        (
            b"COMPANY CONFORMED NAME: Current Filing Co.\nCONFORMED NAME missing-colon",
            "malformed-duplicate",
        ),
        (b"COMPANY CONFORMED NAME: \t", "empty-modern"),
        (b"COMPANY CONFORMED NAME:\nCurrent Filing Co.", "cross-line-modern"),
        (b"FORMER COMPANY CONFORMED NAME: Former Filing Co.", "former-only"),
    ],
)
def test_company_conformed_name_variants_fail_closed(replacement, case):
    raw = _raw().replace(b"CONFORMED NAME: Synthetic Filing Co.", replacement)

    with pytest.raises(ValueError, match="COMPANY/CONFORMED NAME"):
        _header(raw.decode("utf-8"), _request())


def _observation(
    tmp_path, raw: bytes, observed_at: datetime = datetime(2024, 5, 1, 22, tzinfo=UTC)
):
    store = SnapshotStore(tmp_path / "raw")
    observation = store.observe(
        RequestSpec(
            "sec",
            "company-filing-sgml",
            {"cik": "0000000001", "accession_number": "0000000001-24-000001", "form": "10-Q"},
        ),
        raw,
        observed_at,
        "sec-filing-sgml-v1",
    )
    return store, observation


def test_real_sgml_xbrl_parser_produces_replay_bound_native_row(tmp_path, monkeypatch):
    source, observation = _observation(tmp_path, _raw())
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    production = produce_sec_financials_from_sgml(
        source_store=source,
        source_observation=observation,
        projection_store=SnapshotStore(tmp_path / "projection"),
        request=_request(),
        produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
    )
    assert production.vintage.accepted_at == datetime(2024, 5, 1, 21, tzinfo=UTC)
    assert production.vintage.company_name == "Synthetic Filing Co."
    assert len(production.vintage.rows) == len(production.versions) == 1
    assert production.vintage.rows[0].value_native == "123"
    assert production.versions[0].source_artifact_identity == observation.fact_version


def test_missing_xbrl_component_fails_before_projection_write(tmp_path):
    source, observation = _observation(
        tmp_path, _raw().replace(b"<TYPE>EX-101.LAB", b"<TYPE>EX-101.OTHER")
    )
    projection = SnapshotStore(tmp_path / "projection")
    with pytest.raises(ValueError, match="missing required"):
        produce_sec_financials_from_sgml(
            source_store=source,
            source_observation=observation,
            projection_store=projection,
            request=_request(),
            produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (_raw(acceptance="20241103013000"), "ambiguous"),
        (_raw().replace(b"0000000001</identifier>", b"0000000002</identifier>"), "context CIK"),
        (_raw().replace(b"<TYPE>EX-101.LAB", b"<TYPE>EX-101.OTHER"), "missing required"),
    ],
)
def test_unsafe_or_unbound_source_fails_before_projection(tmp_path, raw, message):
    source, observation = _observation(tmp_path, raw)
    with pytest.raises(ValueError, match=message):
        produce_sec_financials_from_sgml(
            source_store=source,
            source_observation=observation,
            projection_store=SnapshotStore(tmp_path / "projection"),
            request=_request(),
            produced_at=datetime(2024, 11, 4, tzinfo=UTC),
        )


def test_source_request_and_replay_limits_fail_closed(tmp_path):
    source, observation = _observation(tmp_path, _raw())
    with pytest.raises(Exception, match="payload exceeds limit"):
        produce_sec_financials_from_sgml(
            source_store=source,
            source_observation=observation,
            projection_store=SnapshotStore(tmp_path / "projection"),
            request=_request(),
            produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
            max_raw_bytes=1,
        )
    wrong = SecSgmlFinancialsRequest(
        "FAKE", "0000000001", "0000000001-24-000001", "10-K", ("income_statement",), False
    )
    with pytest.raises(Exception, match="request mismatch"):
        produce_sec_financials_from_sgml(
            source_store=source,
            source_observation=observation,
            projection_store=SnapshotStore(tmp_path / "projection2"),
            request=wrong,
            produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
        )


def test_xbrl_component_elements_share_one_aggregate_budget(tmp_path):
    padding = b"<x/>" * 50_000
    raw = _raw()
    for closing in (
        b"</xs:schema>",
        b"</link:presentationLink>",
        b"</link:labelLink>",
        b"</xbrl>",
    ):
        raw = raw.replace(closing, padding + closing, 1)
    source, observation = _observation(tmp_path, raw)
    with pytest.raises(ValueError, match="aggregate element limit"):
        produce_sec_financials_from_sgml(
            source_store=source,
            source_observation=observation,
            projection_store=SnapshotStore(tmp_path / "projection"),
            request=_request(),
            produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (_raw().replace(b"CONFORMED NAME: Synthetic Filing Co.\n", b""), "CONFORMED NAME"),
        (
            _raw().replace(
                b"CONFORMED NAME: Synthetic Filing Co.", b"CONFORMED NAME: A\nCONFORMED NAME: B"
            ),
            "CONFORMED NAME",
        ),
        (
            _raw().replace(b"<ACCEPTANCE-DATETIME>20240501170000", b"<ACCEPTANCE-DATETIME>bad"),
            "malformed",
        ),
        (_raw().replace(b"<TYPE>EX-101.LAB", b"<TYPE>EX-101.PRE", 1), "duplicate"),
        (_raw().replace(b"<xbrl ", b"<!DOCTYPE x [<!ENTITY e 'x'>]><xbrl "), "unsafe XML"),
        (_raw().replace(b"<TYPE>EX-101.INS", b"<TYPE>EX-101.OTHER"), "missing required"),
    ],
)
def test_header_and_component_variants_fail_closed(tmp_path, raw, message):
    source, observation = _observation(tmp_path, raw)
    with pytest.raises(ValueError, match=message):
        produce_sec_financials_from_sgml(
            source_store=source,
            source_observation=observation,
            projection_store=SnapshotStore(tmp_path / "projection"),
            request=_request(),
            produced_at=datetime(2024, 5, 2, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "raw",
    [
        _raw().replace(
            b"<ACCEPTANCE-DATETIME>20240501170000",
            b"<ACCEPTANCE-DATETIME>20240501170000\n<ACCEPTANCE-DATETIME>bad",
        ),
        _raw().replace(
            b"CONFORMED NAME: Synthetic Filing Co.", b"CONFORMED NAME:\nSynthetic Filing Co."
        ),
        _raw().replace(b"</TEXT>\n</DOCUMENT>", b"</TEXT><TEXT>x</TEXT>\n</DOCUMENT>", 1),
        _raw().replace(b"</DOCUMENT>", b"<DOCUMENT></DOCUMENT></DOCUMENT>", 1),
    ],
)
def test_malformed_header_and_sgml_tag_boundaries_fail_closed(tmp_path, raw):
    source, observation = _observation(tmp_path, raw)
    with pytest.raises(ValueError):
        produce_sec_financials_from_sgml(
            source_store=source,
            source_observation=observation,
            projection_store=SnapshotStore(tmp_path / "projection"),
            request=_request(),
            produced_at=datetime(2024, 5, 2, tzinfo=UTC),
        )


@pytest.mark.parametrize("acceptance", ["20240102170000", "20240701170000"])
def test_winter_and_summer_acceptance_times_are_utc_bound(tmp_path, acceptance):
    source, observation = _observation(
        tmp_path, _raw(acceptance=acceptance), datetime(2024, 8, 1, tzinfo=UTC)
    )
    production = produce_sec_financials_from_sgml(
        source_store=source,
        source_observation=observation,
        projection_store=SnapshotStore(tmp_path / "projection"),
        request=_request(),
        produced_at=datetime(2024, 8, 1, tzinfo=UTC),
    )
    assert production.vintage.accepted_at is not None
    assert production.vintage.accepted_at.tzinfo is UTC


def test_missing_requested_statement_and_utf8_fail_closed(tmp_path):
    source, observation = _observation(tmp_path, _raw())
    with pytest.raises(ValueError, match="statement is missing"):
        produce_sec_financials_from_sgml(
            source_store=source,
            source_observation=observation,
            projection_store=SnapshotStore(tmp_path / "projection"),
            request=SecSgmlFinancialsRequest(
                "FAKE", "0000000001", "0000000001-24-000001", "10-Q", ("cash_flow",), False
            ),
            produced_at=datetime(2024, 5, 2, tzinfo=UTC),
        )
    source, observation = _observation(tmp_path / "bytes", _raw() + b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        produce_sec_financials_from_sgml(
            source_store=source,
            source_observation=observation,
            projection_store=SnapshotStore(tmp_path / "projection2"),
            request=_request(),
            produced_at=datetime(2024, 5, 2, tzinfo=UTC),
        )

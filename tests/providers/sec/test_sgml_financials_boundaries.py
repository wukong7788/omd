"""Reject malformed retained SGML before invoking third-party parsers or publishing."""

from datetime import UTC, datetime

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.providers.sec import SecSgmlFinancialsRequest, produce_sec_financials_from_sgml
from tests.providers.sec.test_sgml_financials import _observation, _raw, _request


@pytest.mark.parametrize(
    "raw",
    [
        _raw().replace(b"</SEC-HEADER>", b""),
        _raw().replace(b"</SEC-HEADER>", b"</SEC-HEADER></SEC-HEADER>"),
        _raw()
        .replace(b"<ACCEPTANCE-DATETIME>20240501170000\n", b"")
        .replace(b"</SEC-HEADER>", b"</SEC-HEADER>\n<ACCEPTANCE-DATETIME>20240501170000"),
        _raw(acceptance="2024050117000"),
        _raw(acceptance="20240310023000"),
        _raw().replace(
            b"ACCESSION NUMBER: 0000000001-24-000001", b"ACCESSION NUMBER: 0000000001-24-000002"
        ),
        _raw().replace(b"CENTRAL INDEX KEY: 0000000001", b"CENTRAL INDEX KEY: 0000000002"),
        _raw().replace(b"CONFORMED SUBMISSION TYPE: 10-Q", b"CONFORMED SUBMISSION TYPE: 10-K"),
        _raw().replace(b"FILED AS OF DATE: 20240501", b"FILED AS OF DATE: 2024-05-01"),
        _raw().replace(b"</DOCUMENT>", b"", 1),
        _raw().replace(b"</TEXT>", b"", 1),
        _raw().replace(b"<TYPE>EX-101.SCH", b"<TYPE>EX-101.SCH\n<TYPE>EX-101.SCH", 1),
        _raw().replace(b"</xs:schema>", b"</broken>"),
        _raw().replace(b"</xs:schema>", b"<x>" * 129 + b"</x>" * 129 + b"</xs:schema>"),
        _raw().replace(b"http://www.sec.gov/CIK", b"http://example.invalid/other"),
        _raw().replace(
            b">0000000001</dei:EntityCentralIndexKey>", b">0000000002</dei:EntityCentralIndexKey>"
        ),
    ],
    ids=[
        "missing-header-end",
        "duplicate-header-end",
        "outside-header-acceptance",
        "short-acceptance",
        "nonexistent-dst",
        "accession-mismatch",
        "cik-mismatch",
        "form-mismatch",
        "noncompact-date",
        "missing-document-end",
        "missing-text-end",
        "duplicate-type",
        "malformed-xml",
        "xml-depth",
        "unknown-entity-scheme",
        "dei-mismatch",
    ],
)
def test_invalid_input_never_reaches_permissive_parser(tmp_path, monkeypatch, raw):
    from edgar.sgml import FilingSGML

    monkeypatch.setattr(
        FilingSGML, "from_text", lambda *_: pytest.fail("unvalidated SGML reached parser")
    )
    source, observation = _observation(tmp_path, raw)
    projection = SnapshotStore(tmp_path / "projection")
    with pytest.raises(ValueError):
        produce_sec_financials_from_sgml(
            source_store=source,
            source_observation=observation,
            projection_store=projection,
            request=_request(),
            produced_at=datetime(2024, 5, 2, tzinfo=UTC),
        )
    assert not list(projection.root.rglob("response.bin"))


@pytest.mark.parametrize(
    "limits", [{"max_raw_bytes": 8 * 1024 * 1024 + 1}, {"max_rows": 10_001}, {"max_rows": True}]
)
def test_limits_cannot_be_relaxed(tmp_path, limits):
    source, observation = _observation(tmp_path, _raw())
    with pytest.raises(ValueError):
        produce_sec_financials_from_sgml(
            source_store=source,
            source_observation=observation,
            projection_store=SnapshotStore(tmp_path / "projection"),
            request=_request(),
            produced_at=datetime(2024, 5, 2, tzinfo=UTC),
            **limits,
        )


def test_request_requires_immutable_statement_selection_and_dimension_choice():
    args = ("FAKE", "0000000001", "0000000001-24-000001", "10-Q")
    with pytest.raises(TypeError):
        SecSgmlFinancialsRequest(*args, ("income_statement",))
    with pytest.raises((ValueError, TypeError)):
        SecSgmlFinancialsRequest(*args, ["income_statement"], False)


def test_incremental_row_budget_fails_before_writing(tmp_path):
    context = b'<context id="c2"><entity><identifier scheme="http://www.sec.gov/CIK">0000000001</identifier></entity><period><startDate>2023-01-01</startDate><endDate>2023-03-31</endDate></period></context>'
    fact = b'<us-gaap:Revenues contextRef="c2" unitRef="usd" decimals="0">456</us-gaap:Revenues>'
    raw = _raw().replace(b"</xbrl>", context + fact + b"</xbrl>")
    source, observation = _observation(tmp_path, raw)
    projection = SnapshotStore(tmp_path / "projection")
    with pytest.raises(ValueError, match="row"):
        produce_sec_financials_from_sgml(
            source_store=source,
            source_observation=observation,
            projection_store=projection,
            request=_request(),
            produced_at=datetime(2024, 5, 2, tzinfo=UTC),
            max_rows=1,
        )
    assert not list(projection.root.rglob("response.bin"))


@pytest.mark.parametrize(
    "acceptance,expected",
    [
        ("20240102170000", datetime(2024, 1, 2, 22, tzinfo=UTC)),
        ("20240701170000", datetime(2024, 7, 1, 21, tzinfo=UTC)),
    ],
)
def test_exact_seasonal_offset(tmp_path, acceptance, expected):
    source, observation = _observation(
        tmp_path, _raw(acceptance=acceptance), datetime(2024, 8, 1, tzinfo=UTC)
    )
    production = produce_sec_financials_from_sgml(
        source_store=source,
        source_observation=observation,
        projection_store=SnapshotStore(tmp_path / "projection"),
        request=_request(),
        produced_at=datetime(2024, 8, 2, tzinfo=UTC),
    )
    assert production.vintage.accepted_at == expected
    assert production.versions[0].source_available_at == expected


@pytest.mark.parametrize("observed_hour,produced_hour", [(20, 23), (22, 21)])
def test_causal_times_fail_before_any_projection(tmp_path, observed_hour, produced_hour):
    source, observation = _observation(
        tmp_path, _raw(), datetime(2024, 5, 1, observed_hour, tzinfo=UTC)
    )
    projection = SnapshotStore(tmp_path / "projection")
    with pytest.raises(ValueError, match="causal"):
        produce_sec_financials_from_sgml(
            source_store=source,
            source_observation=observation,
            projection_store=projection,
            request=_request(),
            produced_at=datetime(2024, 5, 1, produced_hour, tzinfo=UTC),
        )
    assert not list(projection.root.rglob("response.bin"))

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.providers.sec._companyfacts_models import SecCanonicalMetric, SecCompanyFactsFiling
from ohmydata.providers.sec._companyfacts_projection import project_sec_companyfacts_quarters
from ohmydata.providers.sec._filing_xbrl import (
    fetch_sec_filing_xbrl_source,
    parse_sec_filing_xbrl_instance,
)
from ohmydata.providers.sec.errors import ResourceLimitError, SchemaMismatchError

_CIK = "0000000123"
_ACC = f"{_CIK}-26-000001"
_ACCEPTED = datetime(2026, 8, 1, 16, tzinfo=UTC)


def _instance(
    *,
    cik: str = _CIK,
    end: str = "2026-06-30",
    period: str = "Q2",
    include_fact: bool = True,
    start: str = "2026-04-01",
    unit: str = "USD",
    tag: str = "RevenueFromContractWithCustomerExcludingAssessedTax",
    fact_prefix: str = "us-gaap",
    dei_prefix: str = "dei",
    identifier_scheme: str = "http://www.sec.gov/CIK",
    unit_measure: str = "iso4217:USD",
    dimensional: bool = False,
) -> bytes:
    segment = (
        '<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:ProductAxis">us-gaap:ProductMember</xbrldi:explicitMember></xbrli:segment>'
        if dimensional
        else ""
    )
    fact_context = "Qdim" if dimensional else "Q"
    fact = (
        f'<{fact_prefix}:{tag} contextRef="{fact_context}" unitRef="{unit}" decimals="-3">123</{fact_prefix}:{tag}>'
        if include_fact
        else ""
    )
    return f"""<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:dei="http://xbrl.sec.gov/dei/2025"
      xmlns:us-gaap="http://fasb.org/us-gaap/2026" xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
      xmlns:custom="http://example.test/taxonomy">
      <{dei_prefix}:DocumentFiscalYearFocus contextRef="Q">2026</{dei_prefix}:DocumentFiscalYearFocus>
      <{dei_prefix}:DocumentFiscalPeriodFocus contextRef="Q">{period}</{dei_prefix}:DocumentFiscalPeriodFocus>
      <{dei_prefix}:DocumentPeriodEndDate contextRef="Q">{end}</{dei_prefix}:DocumentPeriodEndDate>
      <xbrli:context id="Q"><xbrli:entity><xbrli:identifier scheme="{identifier_scheme}">{cik}</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>
      {f'<xbrli:context id="Qdim"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}</xbrli:identifier>{segment}</xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>' if dimensional else ""}
      <xbrli:unit id="USD"><xbrli:measure>{unit_measure}</xbrli:measure></xbrli:unit>
      <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
      <xbrli:unit id="usdPerShare"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>
      {fact}
    </xbrli:xbrl>""".encode()


def _parse(body: bytes, *, accession: str = _ACC, report_date: date = date(2026, 6, 30)):
    return parse_sec_filing_xbrl_instance(
        body,
        cik=_CIK,
        accession_number=accession,
        form="10-Q",
        report_date=report_date,
        filing_date=date(2026, 8, 1),
        accepted_at=_ACCEPTED,
    )


def test_parse_exact_undimensioned_quarter_fact() -> None:
    parsed = _parse(_instance())
    assert (parsed.fiscal_year_focus, parsed.fiscal_period_focus, parsed.period_end) == (
        2026,
        "Q2",
        date(2026, 6, 30),
    )
    assert len(parsed.facts) == 1
    fact = parsed.facts[0]
    assert (fact.tag, fact.unit, fact.value, fact.start, fact.end) == (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "USD",
        Decimal(123),
        date(2026, 4, 1),
        date(2026, 6, 30),
    )
    assert fact.accession_number == _ACC
    assert fact.accepted_at == _ACCEPTED


def test_valid_dei_with_no_canonical_facts_is_empty_not_synthetic_zero() -> None:
    assert _parse(_instance(include_fact=False)).facts == ()


def test_canonical_metrics_reject_each_others_units() -> None:
    # A revenue tag expressed in USD/share is not a flow fact.
    assert _parse(_instance(unit="usdPerShare")).facts == ()
    # Diluted EPS expressed in plain USD is not a per-share fact.
    assert _parse(_instance(tag="EarningsPerShareDiluted")).facts == ()


def test_custom_taxonomy_cannot_spoof_canonical_fact_or_dei_focus() -> None:
    assert (
        _parse(
            _instance(fact_prefix="custom", tag="EarningsPerShareDiluted", unit="usdPerShare")
        ).facts
        == ()
    )
    with pytest.raises(SchemaMismatchError, match="DEI focus is missing"):
        _parse(_instance(dei_prefix="custom"))


def test_context_identifier_and_unit_qnames_require_official_schemes() -> None:
    with pytest.raises(SchemaMismatchError, match="entity mismatch"):
        _parse(_instance(identifier_scheme="http://example.test/CIK"))
    assert _parse(_instance(unit_measure="custom:USD")).facts == ()


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (b"<xbrli:xbrl>", "malformed"),
        (_instance(cik="0000000999"), "entity mismatch"),
        (_instance(end="2026-06-29"), "period end conflicts"),
        (_instance(period="FY"), "fiscal focus is invalid"),
        (_instance(unit="shares"), None),
        (_instance(dimensional=True), None),
        (_instance(start="2026-01-01"), None),
    ],
)
def test_reject_or_exclude_unsafe_period_sources(body: bytes, match: str | None) -> None:
    if match is None:
        assert _parse(body).facts == ()
    else:
        with pytest.raises(SchemaMismatchError, match=match):
            _parse(body)


def test_wrong_requested_report_date_is_rejected() -> None:
    with pytest.raises(SchemaMismatchError, match="period end conflicts"):
        _parse(_instance(), report_date=date(2026, 9, 30))


def test_bounded_and_xml_entity_inputs_fail_closed() -> None:
    with pytest.raises(ResourceLimitError):
        _parse(b" " * (8 * 1024**2 + 1))
    with pytest.raises(SchemaMismatchError, match="declarations"):
        _parse(b"<!DOCTYPE x [<!ENTITY y 'boom'>]><x/>")


def test_fetch_retains_exact_index_and_unique_instance(tmp_path) -> None:
    cik = _CIK
    accession = _ACC
    directory = f"/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
    index = json.dumps(
        {
            "directory": {
                "name": directory,
                "item": [
                    {"name": "report.htm", "type": "10-Q"},
                    {"name": "report.xsd", "type": "text.gif"},
                    {"name": "report_cal.xml", "type": "text.gif"},
                    {"name": "report_def.xml", "type": "text.gif"},
                    {"name": "report_lab.xml", "type": "text.gif"},
                    {"name": "report_pre.xml", "type": "text.gif"},
                    {"name": "FilingSummary.xml", "type": "text.gif"},
                    # SEC's directory JSON may expose opaque content types such
                    # as text.gif for every text document. The filename and
                    # parsed DEI identity establish the instance instead.
                    {"name": "report_htm.xml", "type": "text.gif"},
                ],
            }
        }
    ).encode()
    instance = _instance()
    base = f"https://www.sec.gov{directory}"

    class Response:
        def __init__(self, url: str, body: bytes):
            self.url = url
            self.body = io.BytesIO(body)

    class Client:
        def open(self, url: str, **kwargs):
            return Response(url, index if url.endswith("index.json") else instance)

    store = SnapshotStore(tmp_path)
    captured = datetime(2026, 9, 1, tzinfo=UTC)
    source = fetch_sec_filing_xbrl_source(
        client=Client(),
        store=store,
        cik=cik,
        accession_number=accession,
        form="10-Q",
        report_date=date(2026, 6, 30),
        filing_date=date(2026, 8, 1),
        accepted_at=_ACCEPTED,
        primary_document="report.htm",
        utc_now=lambda: captured,
    )
    assert len(source.facts) == 1
    assert source.filing_url == f"{base}/report_htm.xml"
    assert source.index_observation.response_sha256
    assert source.instance_observation.response_sha256


def test_fetch_rejects_missing_or_ambiguous_instance(tmp_path) -> None:
    directory = f"/Archives/edgar/data/{int(_CIK)}/{_ACC.replace('-', '')}"
    index = json.dumps(
        {
            "directory": {
                "name": directory,
                "item": [
                    {"name": "report.htm", "type": "10-Q"},
                    {"name": "one_htm.xml", "type": "text.gif"},
                    {"name": "two_htm.xml", "type": "text.gif"},
                ],
            }
        }
    ).encode()

    class Response:
        url = f"https://www.sec.gov{directory}/index.json"

        def __init__(self):
            self.body = io.BytesIO(index)

    class Client:
        def open(self, url: str, **kwargs):
            return Response()

    with pytest.raises(SchemaMismatchError, match="missing or ambiguous"):
        fetch_sec_filing_xbrl_source(
            client=Client(),
            store=SnapshotStore(tmp_path),
            cik=_CIK,
            accession_number=_ACC,
            form="10-Q",
            report_date=date(2026, 6, 30),
            filing_date=date(2026, 8, 1),
            accepted_at=_ACCEPTED,
            primary_document="report.htm",
            utc_now=lambda: datetime(2026, 9, 1, tzinfo=UTC),
        )


def _fact(value: str, accession: str, form: str, accepted: datetime):
    from ohmydata.providers.sec._filing_xbrl import SecFilingXbrlFact

    return SecFilingXbrlFact(
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "USD",
        Decimal(value),
        date(2026, 4, 1),
        date(2026, 6, 30),
        2026,
        "Q2",
        accession,
        form,
        date(2026, 8, 1),
        accepted,
    )


def test_amendment_precedence_and_conflict_remain_explicit() -> None:
    amendment = f"{_CIK}-26-000002"
    filings = {
        _ACC: SecCompanyFactsFiling(_ACC, "10-Q", date(2026, 6, 30), _ACCEPTED),
        amendment: SecCompanyFactsFiling(
            amendment, "10-Q/A", date(2026, 6, 30), datetime(2026, 8, 3, 16, tzinfo=UTC)
        ),
    }
    equal = project_sec_companyfacts_quarters(
        (
            _fact("123", _ACC, "10-Q", _ACCEPTED),
            _fact("123", amendment, "10-Q/A", filings[amendment].accepted_at),
        ),
        filings,
        "a" * 64,
        requested_periods=((2026, 2),),
        filing_xbrl_observations={_ACC: ("i" * 64, "x" * 64), amendment: ("j" * 64, "y" * 64)},
    )
    revenue = equal[0].field(SecCanonicalMetric.REVENUE)
    assert revenue.status.value == "PRESENT"
    assert revenue.value == Decimal(123)
    assert revenue.evidence[0].accession_number == amendment
    assert not hasattr(revenue.evidence[0], "companyfacts_observation_id")

    conflict = project_sec_companyfacts_quarters(
        (
            _fact("123", _ACC, "10-Q", _ACCEPTED),
            _fact("124", amendment, "10-Q/A", filings[amendment].accepted_at),
        ),
        filings,
        "a" * 64,
        requested_periods=((2026, 2),),
        filing_xbrl_observations={_ACC: ("i" * 64, "x" * 64), amendment: ("j" * 64, "y" * 64)},
    )
    revenue = conflict[0].field(SecCanonicalMetric.REVENUE)
    assert revenue.status.value == "AMBIGUOUS"
    assert len(revenue.evidence) == 2


def test_xbrl_evidence_requires_distinct_observation_ids() -> None:
    fact = _fact("123", _ACC, "10-Q", _ACCEPTED)
    filing = SecCompanyFactsFiling(_ACC, "10-Q", date(2026, 6, 30), _ACCEPTED)
    with pytest.raises(SchemaMismatchError, match="identity is missing"):
        project_sec_companyfacts_quarters(
            (fact,), {_ACC: filing}, "a" * 64, requested_periods=((2026, 2),)
        )

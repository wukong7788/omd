from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from types import MappingProxyType

import pytest

from ohmydata.core import RequestSpec, SnapshotMode, SnapshotStore
from ohmydata.providers.sec import companyfacts
from ohmydata.providers.sec._event_discovery_models import SecDiscoverySource
from ohmydata.providers.sec._filing_xbrl import SecFilingXbrlFact, SecFilingXbrlSource
from ohmydata.providers.sec.companyfacts import _incomplete_xbrl_amendments
from ohmydata.providers.sec.errors import (
    CoverageError,
    ResourceLimitError,
    SchemaMismatchError,
    TransientProviderError,
)
from ohmydata.providers.sec.http import SecHttpClient
from ohmydata.providers.sec.quarterly import SecCompanyEligibilityStatus
from ohmydata.providers.sec.ticker_cik import SecTickerCikMapping

_CIK = "0000000123"
_AT = datetime(2026, 3, 1, tzinfo=UTC)


def _fact(
    tag: str,
    value: str,
    *,
    accn: str,
    form: str,
    fy: int,
    fp: str,
    start: str,
    end: str,
    filed: str = "2026-02-15",
    unit: str = "USD",
    frame: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "val": Decimal(value),
        "accn": accn,
        "form": form,
        "fy": fy,
        "fp": fp,
        "start": start,
        "end": end,
        "filed": filed,
    }
    if frame is not None:
        result["frame"] = frame
    return result


def _payload(rows: dict[str, list[dict[str, object]]]) -> bytes:
    gaap = {
        tag: {"label": tag, "units": {unit: values}}
        for tag, unit, values in (
            ("RevenueFromContractWithCustomerExcludingAssessedTax", "USD", rows.get("revenue", [])),
            ("GrossProfit", "USD", rows.get("gross", [])),
            ("OperatingIncomeLoss", "USD", rows.get("operating", [])),
            ("EarningsPerShareDiluted", "USD/shares", rows.get("eps", [])),
        )
    }
    return json.dumps(
        {"cik": 123, "entityName": "Synthetic Corp", "facts": {"us-gaap": gaap}},
        default=lambda value: json.loads(str(value)) if isinstance(value, Decimal) else value,
    ).encode()


def _filing(
    accn: str,
    form: str,
    report_date: str,
    accepted: str,
    doc: str = "report.htm",
) -> dict[str, object]:
    return {
        "accessionNumber": accn,
        "form": form,
        "filingDate": accepted[:10],
        "reportDate": report_date,
        "primaryDocument": doc,
        "acceptanceDateTime": accepted,
    }


def _root_payload(rows: list[dict[str, object]], *, entity_type: str = "operating") -> bytes:
    columns = (
        "accessionNumber",
        "form",
        "filingDate",
        "reportDate",
        "primaryDocument",
        "acceptanceDateTime",
    )
    return json.dumps(
        {
            "cik": 123,
            "entityType": entity_type,
            "filings": {
                "recent": {key: [row[key] for row in rows] for key in columns},
                "files": [],
            },
        }
    ).encode()


def _setup_api(monkeypatch, tmp_path, payload: bytes, root_body: bytes, *, ticker="SYN"):
    store = SnapshotStore(tmp_path)
    map_ref = store.observe(
        RequestSpec("sec", "ticker_cik", {}), b"{}", _AT, "map-v1", SnapshotMode.APPEND
    )
    mapping = SecTickerCikMapping(
        MappingProxyType({ticker: (_CIK, "Synthetic Corp")}),
        "https://www.sec.gov/files/company_tickers.json",
        map_ref,
    )
    root_ref = store.observe(
        RequestSpec("sec", "edgar_submissions", {"cik": _CIK}),
        root_body,
        _AT,
        "sec-submissions-json-v1",
        SnapshotMode.APPEND,
    )
    root = SecDiscoverySource(f"https://data.sec.gov/submissions/CIK{_CIK}.json", root_ref)
    monkeypatch.setattr(
        companyfacts, "fetch_sec_ticker_cik_mapping", lambda *args, **kwargs: mapping
    )
    monkeypatch.setattr(companyfacts, "fetch_sec_submissions_root", lambda *args, **kwargs: root)
    client = SecHttpClient("Synthetic Test Contact test@example.invalid", opener=object())

    class Response:
        url = companyfacts.SEC_COMPANYFACTS_URL.format(cik=_CIK)

        def __init__(self):
            self.body = io.BytesIO(payload)

    client.open = lambda *args, **kwargs: Response()  # type: ignore[method-assign]
    return store, mapping, root, client


def test_companyfacts_parser_rejects_duplicate_keys_and_bad_cik() -> None:
    with pytest.raises(SchemaMismatchError):
        companyfacts.parse_sec_companyfacts_payload(b'{"cik":123,"cik":123}', _CIK)
    with pytest.raises(SchemaMismatchError):
        companyfacts.parse_sec_companyfacts_payload(b'{"cik":999,"facts":{}}', _CIK)


def test_companyfacts_accepts_exact_string_cik_and_ignores_instant_flow_rows() -> None:
    accession = f"{_CIK}-25-000001"
    duration = _fact(
        "EPS",
        "1.25",
        accn=accession,
        form="10-Q",
        fy=2025,
        fp="Q1",
        start="2025-01-01",
        end="2025-03-31",
        unit="USD/shares",
    )
    instant = {**duration, "val": 2, "end": "2025-03-31"}
    del instant["start"]
    payload = json.loads(_payload({"eps": [instant, duration]}))
    payload["cik"] = _CIK
    parsed = companyfacts.parse_sec_companyfacts_payload(json.dumps(payload).encode(), _CIK)
    assert len(parsed) == 1
    assert parsed[0].value == Decimal("1.25")
    payload["cik"] = "0000000999"
    with pytest.raises(SchemaMismatchError, match="CIK mismatch"):
        companyfacts.parse_sec_companyfacts_payload(json.dumps(payload).encode(), _CIK)


def test_companyfacts_missing_duration_end_still_fails() -> None:
    row = _fact(
        "Revenue",
        "10",
        accn=f"{_CIK}-25-000001",
        form="10-Q",
        fy=2025,
        fp="Q1",
        start="2025-01-01",
        end="2025-03-31",
    )
    del row["end"]
    with pytest.raises(SchemaMismatchError, match="duration metadata"):
        companyfacts.parse_sec_companyfacts_payload(_payload({"revenue": [row]}), _CIK)


def test_supported_filing_with_broken_fiscal_metadata_fails_explicitly() -> None:
    row = _fact(
        "Revenue",
        "10",
        accn=f"{_CIK}-25-000001",
        form="10-Q",
        fy=2025,
        fp="Q1",
        start="2025-01-01",
        end="2025-03-31",
    )
    del row["fp"]
    with pytest.raises(SchemaMismatchError, match="fiscal or accession"):
        companyfacts.parse_sec_companyfacts_payload(_payload({"revenue": [row]}), _CIK)


def test_q2_ytd_is_not_mistaken_for_independent_quarter() -> None:
    accn = f"{_CIK}-25-000002"
    raw = companyfacts.parse_sec_companyfacts_payload(
        json.dumps(
            {
                "cik": 123,
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "val": 30,
                                        "accn": accn,
                                        "form": "10-Q",
                                        "fy": 2025,
                                        "fp": "Q2",
                                        "start": "2025-01-01",
                                        "end": "2025-06-30",
                                        "filed": "2025-08-01",
                                    }
                                ]
                            }
                        }
                    }
                },
            }
        ).encode(),
        _CIK,
    )
    filing = companyfacts.SecCompanyFactsFiling(
        accn, "10-Q", date(2025, 6, 30), datetime(2025, 8, 1, tzinfo=UTC)
    )
    slots = companyfacts.project_sec_companyfacts_quarters(raw, {accn: filing}, "a" * 64)
    assert slots[-1].fiscal_quarter == 2
    revenue = slots[-1].field(companyfacts.SecCanonicalMetric.REVENUE)
    assert revenue.status is companyfacts.SecCanonicalFieldStatus.MISSING


def test_revenue_alias_projects_and_conflicting_aliases_are_ambiguous() -> None:
    accession = f"{_CIK}-25-000001"
    filing = companyfacts.SecCompanyFactsFiling(
        accession, "10-Q", date(2025, 3, 31), datetime(2025, 5, 1, tzinfo=UTC)
    )
    primary = companyfacts.SecCompanyFactsFact(
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "USD",
        Decimal(100),
        date(2025, 1, 1),
        date(2025, 3, 31),
        2025,
        "Q1",
        "10-Q",
        date(2025, 5, 1),
        accession,
        "CY2025Q1",
    )
    projected = companyfacts.project_sec_companyfacts_quarters(
        (primary,), {accession: filing}, "d" * 64, requested_periods=((2025, 1),)
    )
    revenue = projected[0].field(companyfacts.SecCanonicalMetric.REVENUE)
    assert revenue.status is companyfacts.SecCanonicalFieldStatus.PRESENT
    assert revenue.evidence[0].native_tag == "RevenueFromContractWithCustomerIncludingAssessedTax"

    conflicting = companyfacts.SecCompanyFactsFact(
        "Revenues",
        "USD",
        Decimal(99),
        date(2025, 1, 1),
        date(2025, 3, 31),
        2025,
        "Q1",
        "10-Q",
        date(2025, 5, 1),
        accession,
        "CY2025Q1",
    )
    prioritized = companyfacts.project_sec_companyfacts_quarters(
        (primary, conflicting),
        {accession: filing},
        "d" * 64,
        requested_periods=((2025, 1),),
    )
    revenue = prioritized[0].field(companyfacts.SecCanonicalMetric.REVENUE)
    assert revenue.status is companyfacts.SecCanonicalFieldStatus.PRESENT
    assert revenue.value == Decimal(99)
    assert [item.native_tag for item in revenue.evidence] == ["Revenues", primary.tag]

    other_accession = f"{_CIK}-25-000099"
    cross_filing = replace(conflicting, accn=other_accession)
    cross_filing_ambiguous = companyfacts.project_sec_companyfacts_quarters(
        (primary, cross_filing),
        {
            accession: filing,
            other_accession: companyfacts.SecCompanyFactsFiling(
                other_accession,
                "10-Q",
                date(2025, 3, 31),
                datetime(2025, 5, 2, tzinfo=UTC),
            ),
        },
        "d" * 64,
        requested_periods=((2025, 1),),
    )[0].field(companyfacts.SecCanonicalMetric.REVENUE)
    assert cross_filing_ambiguous.status is companyfacts.SecCanonicalFieldStatus.AMBIGUOUS
    assert {item.accession_number for item in cross_filing_ambiguous.evidence} == {
        accession,
        other_accession,
    }


def test_be_total_revenue_priority_keeps_contract_subset_evidence() -> None:
    accession = f"{_CIK}-26-000001"
    filing = companyfacts.SecCompanyFactsFiling(
        accession,
        "10-Q",
        date(2026, 6, 30),
        datetime(2026, 7, 28, 17, 27, 3, tzinfo=UTC),
    )
    end = date(2026, 6, 30)
    start = date(2026, 4, 1)
    total = companyfacts.SecCompanyFactsFact(
        "Revenues",
        "USD",
        Decimal(1_065_365_000),
        start,
        end,
        2026,
        "Q2",
        "10-Q",
        date(2026, 7, 28),
        accession,
        "CY2026Q2",
    )
    contract_subset = replace(
        total,
        tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        value=Decimal(1_060_746_000),
    )
    field = companyfacts.project_sec_companyfacts_quarters(
        (total, contract_subset),
        {accession: filing},
        "d" * 64,
        requested_periods=((2026, 2),),
    )[0].field(companyfacts.SecCanonicalMetric.REVENUE)

    assert field.status is companyfacts.SecCanonicalFieldStatus.PRESENT
    assert field.value == Decimal(1_065_365_000)
    assert [item.native_tag for item in field.evidence] == [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
    ]


def test_be_total_revenue_priority_spans_proven_amendment_chain() -> None:
    base_accession = f"{_CIK}-26-000001"
    amendment_accession = f"{_CIK}-26-000002"
    report_date = date(2026, 6, 30)
    filings = {
        base_accession: companyfacts.SecCompanyFactsFiling(
            base_accession,
            "10-Q",
            report_date,
            datetime(2026, 7, 28, 17, tzinfo=UTC),
        ),
        amendment_accession: companyfacts.SecCompanyFactsFiling(
            amendment_accession,
            "10-Q/A",
            report_date,
            datetime(2026, 7, 29, 17, tzinfo=UTC),
        ),
    }
    common = {
        "unit": "USD",
        "start": date(2026, 4, 1),
        "end": report_date,
        "fy": 2026,
        "fp": "Q2",
        "filed": date(2026, 7, 28),
        "frame": "CY2026Q2",
    }
    base_contract = companyfacts.SecCompanyFactsFact(
        tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        value=Decimal(1_060_746_000),
        form="10-Q",
        accn=base_accession,
        **common,
    )
    amended_total = replace(
        base_contract,
        tag="Revenues",
        value=Decimal(1_065_365_000),
        form="10-Q/A",
        accn=amendment_accession,
    )
    amended_contract = replace(
        base_contract,
        form="10-Q/A",
        accn=amendment_accession,
    )

    field = companyfacts.project_sec_companyfacts_quarters(
        (base_contract, amended_total, amended_contract),
        filings,
        "d" * 64,
        requested_periods=((2026, 2),),
    )[0].field(companyfacts.SecCanonicalMetric.REVENUE)

    assert field.status is companyfacts.SecCanonicalFieldStatus.PRESENT
    assert field.value == Decimal(1_065_365_000)
    assert field.evidence[0].accession_number == amendment_accession
    assert {item.accession_number for item in field.evidence[1:]} == {
        base_accession,
        amendment_accession,
    }


def test_q4_exact_subtraction_evidence_and_q4_eps_direct_frame() -> None:
    q3 = f"{_CIK}-25-000003"
    fy = f"{_CIK}-26-000001"
    facts = (
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(650),
            date(2025, 1, 1),
            date(2025, 9, 30),
            2025,
            "Q3",
            "10-Q",
            date(2025, 11, 10),
            q3,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(1000),
            date(2025, 1, 1),
            date(2025, 12, 31),
            2025,
            "FY",
            "10-K",
            date(2026, 2, 15),
            fy,
            "CY2025",
        ),
        companyfacts.SecCompanyFactsFact(
            "EarningsPerShareDiluted",
            "USD/shares",
            Decimal("0.75"),
            date(2025, 10, 1),
            date(2025, 12, 31),
            2025,
            "FY",
            "10-K",
            date(2026, 2, 15),
            fy,
            "CY2025Q4",
        ),
    )
    filings = {
        q3: companyfacts.SecCompanyFactsFiling(
            q3, "10-Q", date(2025, 9, 30), datetime(2025, 11, 10, tzinfo=UTC)
        ),
        fy: companyfacts.SecCompanyFactsFiling(
            fy,
            "10-K",
            date(2025, 12, 31),
            datetime(2026, 2, 15, tzinfo=UTC),
            "report.htm",
            f"https://www.sec.gov/Archives/edgar/data/123/{fy.replace('-', '')}/report.htm",
        ),
    }
    with localcontext() as context:
        context.prec = 2
        slots = companyfacts.project_sec_companyfacts_quarters(
            facts, filings, "b" * 64, requested_periods=((2025, 4),)
        )
    revenue = slots[0].field(companyfacts.SecCanonicalMetric.REVENUE)
    assert revenue.status is companyfacts.SecCanonicalFieldStatus.PRESENT
    assert revenue.value == Decimal(350)
    assert len(revenue.evidence[0].source_evidence) == 2
    assert {item.accession_number for item in revenue.evidence[0].source_evidence} == {q3, fy}
    eps = slots[0].field(companyfacts.SecCanonicalMetric.GAAP_DILUTED_EPS)
    assert eps.status is companyfacts.SecCanonicalFieldStatus.PRESENT
    assert eps.value == Decimal("0.75")


def test_equal_q4_results_from_distinct_q3_filings_remain_ambiguous() -> None:
    q3_a = f"{_CIK}-25-000003"
    q3_b = f"{_CIK}-25-000004"
    fy = f"{_CIK}-26-000001"
    facts = (
        companyfacts.SecCompanyFactsFact(
            "GrossProfit",
            "USD",
            Decimal(65),
            date(2025, 1, 1),
            date(2025, 9, 30),
            2025,
            "Q3",
            "10-Q",
            date(2025, 11, 10),
            q3_a,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "GrossProfit",
            "USD",
            Decimal(65),
            date(2025, 1, 1),
            date(2025, 9, 30),
            2025,
            "Q3",
            "10-Q",
            date(2025, 11, 11),
            q3_b,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "GrossProfit",
            "USD",
            Decimal(100),
            date(2025, 1, 1),
            date(2025, 12, 31),
            2025,
            "FY",
            "10-K",
            date(2026, 2, 15),
            fy,
            "CY2025",
        ),
    )
    filings = {
        q3_a: companyfacts.SecCompanyFactsFiling(
            q3_a, "10-Q", date(2025, 9, 30), datetime(2025, 11, 10, tzinfo=UTC)
        ),
        q3_b: companyfacts.SecCompanyFactsFiling(
            q3_b, "10-Q", date(2025, 9, 30), datetime(2025, 11, 11, tzinfo=UTC)
        ),
        fy: companyfacts.SecCompanyFactsFiling(
            fy, "10-K", date(2025, 12, 31), datetime(2026, 2, 15, tzinfo=UTC)
        ),
    }
    field = companyfacts.project_sec_companyfacts_quarters(
        facts, filings, "d" * 64, requested_periods=((2025, 4),)
    )[0].field(companyfacts.SecCanonicalMetric.GROSS_PROFIT)
    assert field.status is companyfacts.SecCanonicalFieldStatus.AMBIGUOUS
    assert len(field.evidence) == 2
    assert {item.source_evidence[1].accession_number for item in field.evidence} == {q3_a, q3_b}


def test_q4_pair_generation_is_bounded(monkeypatch) -> None:
    from ohmydata.providers.sec import _companyfacts_projection

    q3_a = f"{_CIK}-25-000003"
    q3_b = f"{_CIK}-25-000004"
    fy = f"{_CIK}-26-000001"
    facts = tuple(
        companyfacts.SecCompanyFactsFact(
            "GrossProfit",
            "USD",
            Decimal(65),
            date(2025, 1, 1),
            date(2025, 9, 30),
            2025,
            "Q3",
            "10-Q",
            date(2025, 11, 10),
            accession,
            None,
        )
        for accession in (q3_a, q3_b)
    ) + (
        companyfacts.SecCompanyFactsFact(
            "GrossProfit",
            "USD",
            Decimal(100),
            date(2025, 1, 1),
            date(2025, 12, 31),
            2025,
            "FY",
            "10-K",
            date(2026, 2, 15),
            fy,
            "CY2025",
        ),
    )
    filings = {
        accession: companyfacts.SecCompanyFactsFiling(
            accession, "10-Q", date(2025, 9, 30), datetime(2025, 11, 10, tzinfo=UTC)
        )
        for accession in (q3_a, q3_b)
    }
    filings[fy] = companyfacts.SecCompanyFactsFiling(
        fy, "10-K", date(2025, 12, 31), datetime(2026, 2, 15, tzinfo=UTC)
    )
    monkeypatch.setattr(_companyfacts_projection, "_MAX_PAIR_ATTEMPTS", 1)
    with pytest.raises(ResourceLimitError, match="pair limit"):
        companyfacts.project_sec_companyfacts_quarters(
            facts, filings, "d" * 64, requested_periods=((2025, 4),)
        )


def test_q4_eps_rejects_mismatched_filing_accession() -> None:
    accn = f"{_CIK}-26-000001"
    other = f"{_CIK}-26-000002"
    fact = companyfacts.SecCompanyFactsFact(
        "EarningsPerShareDiluted",
        "USD/shares",
        Decimal("0.75"),
        date(2025, 10, 1),
        date(2025, 12, 31),
        2025,
        "FY",
        "10-K",
        date(2026, 2, 15),
        accn,
        "CY2025Q4",
    )
    filings = {
        accn: companyfacts.SecCompanyFactsFiling(
            other, "10-K", date(2025, 12, 31), datetime(2026, 2, 15, tzinfo=UTC)
        )
    }
    field = companyfacts.project_sec_companyfacts_quarters(
        (fact,), filings, "e" * 64, requested_periods=((2025, 4),)
    )[0].field(companyfacts.SecCanonicalMetric.GAAP_DILUTED_EPS)
    assert field.status is companyfacts.SecCanonicalFieldStatus.MISSING


def test_q4_amendment_cohort_is_ambiguous_and_coverage_is_explicit() -> None:
    q3 = f"{_CIK}-25-000003"
    fy = f"{_CIK}-26-000001"
    amended = f"{_CIK}-26-000002"
    facts = (
        companyfacts.SecCompanyFactsFact(
            "GrossProfit",
            "USD",
            Decimal(65),
            date(2025, 1, 1),
            date(2025, 9, 30),
            2025,
            "Q3",
            "10-Q",
            date(2025, 11, 10),
            q3,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "GrossProfit",
            "USD",
            Decimal(100),
            date(2025, 1, 1),
            date(2025, 12, 31),
            2025,
            "FY",
            "10-K",
            date(2026, 2, 15),
            fy,
            "CY2025",
        ),
        companyfacts.SecCompanyFactsFact(
            "GrossProfit",
            "USD",
            Decimal(101),
            date(2025, 1, 1),
            date(2025, 12, 31),
            2025,
            "FY",
            "10-K/A",
            date(2026, 3, 1),
            amended,
            "CY2025",
        ),
    )
    filings = {
        q3: companyfacts.SecCompanyFactsFiling(
            q3, "10-Q", date(2025, 9, 30), datetime(2025, 11, 10, tzinfo=UTC)
        ),
        fy: companyfacts.SecCompanyFactsFiling(
            fy, "10-K", date(2025, 12, 31), datetime(2026, 2, 15, tzinfo=UTC)
        ),
        amended: companyfacts.SecCompanyFactsFiling(
            amended, "10-K/A", date(2025, 12, 31), datetime(2026, 3, 1, tzinfo=UTC)
        ),
    }
    slots = companyfacts.project_sec_companyfacts_quarters(
        facts, filings, "c" * 64, requested_periods=((2025, 4),)
    )
    gross = slots[0].field(companyfacts.SecCanonicalMetric.GROSS_PROFIT)
    assert gross.status is companyfacts.SecCanonicalFieldStatus.AMBIGUOUS
    assert gross.value is None
    incomplete = companyfacts.project_sec_companyfacts_quarters(
        facts,
        filings,
        "c" * 64,
        requested_periods=((2025, 4),),
        incomplete_periods=frozenset({(2025, 4)}),
    )
    assert incomplete[0].field(companyfacts.SecCanonicalMetric.GROSS_PROFIT).status is (
        companyfacts.SecCanonicalFieldStatus.COVERAGE_INCOMPLETE
    )


def test_high_level_fetch_retains_companyfacts_and_filing_url(monkeypatch, tmp_path) -> None:
    q3 = f"{_CIK}-25-000003"
    fy = f"{_CIK}-26-000001"
    root_rows = [
        _filing(q3, "10-Q", "2025-09-30", "2025-11-10T16:00:00.000Z"),
        _filing(fy, "10-K", "2025-12-31", "2026-02-15T16:00:00.000Z"),
    ]
    payload = _payload(
        {
            "revenue": [
                _fact(
                    "Revenue",
                    "650",
                    accn=q3,
                    form="10-Q",
                    fy=2025,
                    fp="Q3",
                    start="2025-01-01",
                    end="2025-09-30",
                    filed="2025-11-10",
                ),
                _fact(
                    "Revenue",
                    "1000",
                    accn=fy,
                    form="10-K",
                    fy=2025,
                    fp="FY",
                    start="2025-01-01",
                    end="2025-12-31",
                    filed="2026-02-15",
                    frame="CY2025",
                ),
            ],
            "eps": [
                _fact(
                    "EPS",
                    "0.75",
                    accn=fy,
                    form="10-K",
                    fy=2025,
                    fp="FY",
                    start="2025-10-01",
                    end="2025-12-31",
                    filed="2026-02-15",
                    unit="USD/shares",
                    frame="CY2025Q4",
                )
            ],
        }
    )
    store, _, _, client = _setup_api(monkeypatch, tmp_path, payload, _root_payload(root_rows))
    result = companyfacts.fetch_sec_canonical_quarters("SYN", client, store, utc_now=lambda: _AT)
    assert result.eligibility.status is SecCompanyEligibilityStatus.SEC_COMPANY
    assert result.coverage_complete and result.periods_resolved
    assert len(result.slots) == 8
    q4 = next(slot for slot in result.slots if (slot.fiscal_year, slot.fiscal_quarter) == (2025, 4))
    revenue = q4.field(companyfacts.SecCanonicalMetric.REVENUE)
    assert revenue.status is companyfacts.SecCanonicalFieldStatus.PRESENT
    assert revenue.value == Decimal(350)
    assert {source.filing_url for source in revenue.evidence[0].source_evidence} == {
        f"https://www.sec.gov/Archives/edgar/data/123/{q3.replace('-', '')}/report.htm",
        f"https://www.sec.gov/Archives/edgar/data/123/{fy.replace('-', '')}/report.htm",
    }
    assert q4.field(companyfacts.SecCanonicalMetric.GROSS_PROFIT).status is (
        companyfacts.SecCanonicalFieldStatus.MISSING
    )
    assert result.companyfacts_observation_id is not None
    four = companyfacts.fetch_sec_canonical_quarters(
        "SYN", client, store, utc_now=lambda: _AT, count=4
    )
    assert four.coverage_complete and four.periods_resolved
    assert len(four.slots) == 4
    assert (four.slots[-1].fiscal_year, four.slots[-1].fiscal_quarter) == (2025, 4)


def test_unmatched_historical_accession_is_coverage_incomplete(monkeypatch, tmp_path) -> None:
    q3 = f"{_CIK}-25-000003"
    fy = f"{_CIK}-26-000001"
    root_rows = [_filing(fy, "10-K", "2025-12-31", "2026-02-15T16:00:00.000Z")]
    payload = _payload(
        {
            "revenue": [
                _fact(
                    "Revenue",
                    "650",
                    accn=q3,
                    form="10-Q",
                    fy=2025,
                    fp="Q3",
                    start="2025-01-01",
                    end="2025-09-30",
                    filed="2025-11-10",
                ),
                _fact(
                    "Revenue",
                    "1000",
                    accn=fy,
                    form="10-K",
                    fy=2025,
                    fp="FY",
                    start="2025-01-01",
                    end="2025-12-31",
                    filed="2026-02-15",
                ),
            ]
        }
    )
    store, _, _, client = _setup_api(monkeypatch, tmp_path, payload, _root_payload(root_rows))
    result = companyfacts.fetch_sec_canonical_quarters("SYN", client, store, utc_now=lambda: _AT)
    assert result.periods_resolved
    assert not result.coverage_complete
    assert result.uncovered_accessions == (q3,)
    q4 = next(slot for slot in result.slots if (slot.fiscal_year, slot.fiscal_quarter) == (2025, 4))
    assert q4.field(companyfacts.SecCanonicalMetric.REVENUE).status is (
        companyfacts.SecCanonicalFieldStatus.COVERAGE_INCOMPLETE
    )


def test_newer_sec_filing_without_fiscal_facts_returns_unresolved(monkeypatch, tmp_path) -> None:
    fy = f"{_CIK}-26-000001"
    newer = f"{_CIK}-26-000004"
    root_rows = [
        _filing(fy, "10-K", "2025-12-31", "2026-02-15T16:00:00.000Z"),
        _filing(newer, "10-Q", "2026-03-31", "2026-05-10T16:00:00.000Z"),
    ]
    payload = _payload(
        {
            "revenue": [
                _fact(
                    "Revenue",
                    "1000",
                    accn=fy,
                    form="10-K",
                    fy=2025,
                    fp="FY",
                    start="2025-01-01",
                    end="2025-12-31",
                    filed="2026-02-15",
                )
            ]
        }
    )
    store, _, _, client = _setup_api(monkeypatch, tmp_path, payload, _root_payload(root_rows))
    monkeypatch.setattr(
        companyfacts,
        "fetch_sec_filing_xbrl_source",
        lambda **kwargs: (_ for _ in ()).throw(CoverageError("missing source")),
    )
    result = companyfacts.fetch_sec_canonical_quarters("SYN", client, store, utc_now=lambda: _AT)
    assert not result.periods_resolved
    assert result.slots == ()
    assert result.unresolved_period_accessions == (newer,)


def test_companyfacts_lag_uses_distinct_exact_filing_xbrl_evidence(monkeypatch, tmp_path) -> None:
    q1 = f"{_CIK}-26-000001"
    q2 = f"{_CIK}-26-000002"
    root_rows = [
        _filing(q1, "10-Q", "2026-03-31", "2026-05-10T16:00:00.000Z"),
        _filing(q2, "10-Q", "2026-06-30", "2026-08-01T16:00:00.000Z"),
    ]
    payload = _payload(
        {
            "revenue": [
                _fact(
                    "Revenue",
                    "100",
                    accn=q1,
                    form="10-Q",
                    fy=2026,
                    fp="Q1",
                    start="2026-01-01",
                    end="2026-03-31",
                    filed="2026-05-10",
                )
            ]
        }
    )
    store, _, _, client = _setup_api(monkeypatch, tmp_path, payload, _root_payload(root_rows))
    captured = datetime(2026, 9, 1, tzinfo=UTC)
    xbrl_fact = SecFilingXbrlFact(
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "USD",
        Decimal(120),
        date(2026, 4, 1),
        date(2026, 6, 30),
        2026,
        "Q2",
        q2,
        "10-Q",
        date(2026, 8, 1),
        datetime(2026, 8, 1, 16, tzinfo=UTC),
    )

    def load_xbrl(**kwargs):
        index = store.observe(
            RequestSpec("sec", "company-filing-directory", {"cik": _CIK, "accession_number": q2}),
            b'{"directory":{"name":"synthetic"}}',
            captured,
            "sec-filing-directory-json-v1",
            SnapshotMode.APPEND,
        )
        instance = store.observe(
            RequestSpec(
                "sec",
                "company-filing-document",
                {"cik": _CIK, "accession_number": q2, "filename": "instance.xml"},
            ),
            b"<synthetic />",
            captured,
            "sec-filing-document-bytes-v1",
            SnapshotMode.APPEND,
        )
        return SecFilingXbrlSource(
            (xbrl_fact,),
            index,
            instance,
            f"https://www.sec.gov/Archives/edgar/data/123/{q2.replace('-', '')}/instance.xml",
        )

    monkeypatch.setattr(companyfacts, "fetch_sec_filing_xbrl_source", load_xbrl)
    result = companyfacts.fetch_sec_canonical_quarters(
        "SYN", client, store, utc_now=lambda: captured, count=1
    )
    assert result.coverage_complete and result.periods_resolved
    assert result.companyfacts_observation_id is not None
    assert len(result.filing_xbrl_observation_ids) == 2
    slot = result.slots[0]
    assert (slot.fiscal_year, slot.fiscal_quarter) == (2026, 2)
    revenue = slot.field(companyfacts.SecCanonicalMetric.REVENUE)
    assert revenue.status is companyfacts.SecCanonicalFieldStatus.PRESENT
    assert revenue.value == Decimal(120)
    evidence = revenue.evidence[0]
    from ohmydata.providers.sec import SecFilingXbrlFactEvidence

    assert isinstance(evidence, SecFilingXbrlFactEvidence)
    assert evidence.accession_number == q2
    assert evidence.accepted_at == datetime(2026, 8, 1, 16, tzinfo=UTC)
    assert evidence.index_observation_id in result.filing_xbrl_observation_ids
    assert evidence.instance_observation_id in result.filing_xbrl_observation_ids
    assert not hasattr(evidence, "companyfacts_observation_id")
    assert slot.field(companyfacts.SecCanonicalMetric.GROSS_PROFIT).status is (
        companyfacts.SecCanonicalFieldStatus.MISSING
    )


def test_partial_xbrl_amendment_marks_period_coverage_incomplete() -> None:
    base = f"{_CIK}-26-000010"
    amended = f"{_CIK}-26-000011"
    amended_at = datetime(2026, 8, 3, 16, tzinfo=UTC)
    filings = {
        base: companyfacts.SecCompanyFactsFiling(
            base, "10-Q", date(2026, 6, 30), datetime(2026, 8, 1, 16, tzinfo=UTC)
        ),
        amended: companyfacts.SecCompanyFactsFiling(
            amended, "10-Q/A", date(2026, 6, 30), amended_at
        ),
    }
    revenue = SecFilingXbrlFact(
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "USD",
        Decimal(120),
        date(2026, 4, 1),
        date(2026, 6, 30),
        2026,
        "Q2",
        base,
        "10-Q",
        date(2026, 8, 1),
        filings[base].accepted_at,
    )
    eps = SecFilingXbrlFact(
        "EarningsPerShareDiluted",
        "USD/shares",
        Decimal("0.5"),
        date(2026, 4, 1),
        date(2026, 6, 30),
        2026,
        "Q2",
        base,
        "10-Q",
        date(2026, 8, 1),
        filings[base].accepted_at,
    )
    amended_revenue = SecFilingXbrlFact(
        revenue.tag,
        revenue.unit,
        revenue.value,
        revenue.start,
        revenue.end,
        revenue.fiscal_year_focus,
        revenue.fiscal_period_focus,
        amended,
        "10-Q/A",
        date(2026, 8, 3),
        amended_at,
    )
    periods, accessions = _incomplete_xbrl_amendments(
        (revenue, eps, amended_revenue), filings, None, {amended}
    )
    assert periods == frozenset({(2026, 2)})
    assert accessions == (amended,)


def test_companyfacts_filed_date_joins_to_sec_filing_date_not_utc_acceptance_day(
    monkeypatch, tmp_path
) -> None:
    accession = f"{_CIK}-25-000002"
    row = _filing(accession, "10-Q", "2025-06-30", "2025-07-30T22:11:13.000Z")
    row["filingDate"] = "2025-07-31"
    payload = _payload(
        {
            "revenue": [
                _fact(
                    "Revenue",
                    "100",
                    accn=accession,
                    form="10-Q",
                    fy=2025,
                    fp="Q2",
                    start="2025-04-01",
                    end="2025-06-30",
                    filed="2025-07-31",
                )
            ]
        }
    )
    store, _, _, client = _setup_api(monkeypatch, tmp_path, payload, _root_payload([row]))
    result = companyfacts.fetch_sec_canonical_quarters("SYN", client, store, utc_now=lambda: _AT)
    assert result.coverage_complete and result.periods_resolved
    q2 = next(slot for slot in result.slots if (slot.fiscal_year, slot.fiscal_quarter) == (2025, 2))
    assert q2.field(companyfacts.SecCanonicalMetric.REVENUE).value == Decimal(100)

    accepted_cutoff = companyfacts.fetch_sec_canonical_quarters(
        "SYN",
        client,
        store,
        utc_now=lambda: _AT,
        acceptance_upper=datetime(2025, 7, 30, 22, 30, tzinfo=UTC),
    )
    assert accepted_cutoff.coverage_complete and accepted_cutoff.periods_resolved
    assert any(
        slot.field(companyfacts.SecCanonicalMetric.REVENUE).value == Decimal(100)
        for slot in accepted_cutoff.slots
    )

    row["filingDate"] = "2025-07-30"
    store, _, _, client = _setup_api(
        monkeypatch, tmp_path / "mismatch", payload, _root_payload([row])
    )
    monkeypatch.setattr(
        companyfacts,
        "fetch_sec_filing_xbrl_source",
        lambda **kwargs: (_ for _ in ()).throw(CoverageError("missing source")),
    )
    unresolved = companyfacts.fetch_sec_canonical_quarters(
        "SYN", client, store, utc_now=lambda: _AT
    )
    assert not unresolved.periods_resolved
    assert unresolved.unresolved_period_accessions == (accession,)


def test_same_day_acceptance_cutoff_targets_alternate_older_window() -> None:
    newest = companyfacts.SecCompanyFactsFact(
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "USD",
        Decimal(100),
        date(2025, 1, 1),
        date(2025, 12, 31),
        2025,
        "FY",
        "10-K",
        date(2026, 2, 15),
        f"{_CIK}-26-000001",
        None,
    )
    q3 = companyfacts.SecCompanyFactsFact(
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "USD",
        Decimal(75),
        date(2025, 1, 1),
        date(2025, 9, 30),
        2025,
        "Q3",
        "10-Q",
        date(2025, 11, 10),
        f"{_CIK}-25-000003",
        None,
    )
    cutoff = datetime(2026, 2, 15, 15, tzinfo=UTC)
    targets = companyfacts._periods_from_companyfacts((newest, q3), cutoff, 8)
    assert (2023, 4) in targets
    assert (2024, 4) in targets


def test_high_level_historical_cutoff_uses_the_older_eight_quarter_window(
    monkeypatch, tmp_path
) -> None:
    old = f"{_CIK}-22-000003"
    newer = f"{_CIK}-26-000001"
    rows = [
        _filing(old, "10-Q", "2022-09-30", "2022-11-10T16:00:00.000Z"),
        _filing(newer, "10-K", "2025-12-31", "2026-02-15T16:00:00.000Z"),
    ]
    payload = _payload(
        {
            "revenue": [
                _fact(
                    "Revenue",
                    "75",
                    accn=old,
                    form="10-Q",
                    fy=2022,
                    fp="Q3",
                    start="2022-07-01",
                    end="2022-09-30",
                    filed="2022-11-10",
                ),
                _fact(
                    "Revenue",
                    "1000",
                    accn=newer,
                    form="10-K",
                    fy=2025,
                    fp="FY",
                    start="2025-01-01",
                    end="2025-12-31",
                    filed="2026-02-15",
                    frame="CY2025",
                ),
            ]
        }
    )
    store, _, _, client = _setup_api(monkeypatch, tmp_path, payload, _root_payload(rows))
    result = companyfacts.fetch_sec_canonical_quarters(
        "SYN",
        client,
        store,
        utc_now=lambda: _AT,
        acceptance_upper=datetime(2022, 12, 1, tzinfo=UTC),
    )
    assert result.coverage_complete and result.periods_resolved
    assert (result.slots[-1].fiscal_year, result.slots[-1].fiscal_quarter) == (2022, 3)
    revenue = result.slots[-1].field(companyfacts.SecCanonicalMetric.REVENUE)
    assert revenue.status is companyfacts.SecCanonicalFieldStatus.PRESENT
    assert revenue.value == Decimal(75)


def test_non_sec_non_company_and_transient_eligibility_return_no_fields(
    monkeypatch, tmp_path
) -> None:
    payload = _payload({})
    store, mapping, _, client = _setup_api(monkeypatch, tmp_path, payload, _root_payload([]))
    missing_mapping = SecTickerCikMapping(
        MappingProxyType({}), mapping.source_url, mapping.observation
    )
    monkeypatch.setattr(
        companyfacts, "fetch_sec_ticker_cik_mapping", lambda *a, **k: missing_mapping
    )
    absent = companyfacts.fetch_sec_canonical_quarters("NOPE", client, store, utc_now=lambda: _AT)
    assert absent.eligibility.status is SecCompanyEligibilityStatus.NON_SEC
    assert absent.slots == ()

    investment_rows = [
        _filing(f"{_CIK}-26-000001", "10-K", "2025-12-31", "2026-02-15T16:00:00.000Z")
    ]
    investment_store, _, _, investment_client = _setup_api(
        monkeypatch,
        tmp_path / "investment",
        payload,
        _root_payload(investment_rows, entity_type="investment"),
    )
    investment = companyfacts.fetch_sec_canonical_quarters(
        "SYN", investment_client, investment_store, utc_now=lambda: _AT
    )
    assert investment.eligibility.status is SecCompanyEligibilityStatus.NON_COMPANY
    assert investment.slots == ()

    monkeypatch.setattr(
        companyfacts,
        "fetch_sec_ticker_cik_mapping",
        lambda *a, **k: (_ for _ in ()).throw(TransientProviderError("synthetic transient")),
    )
    failed = companyfacts.fetch_sec_canonical_quarters("SYN", client, store, utc_now=lambda: _AT)
    assert failed.eligibility.status is SecCompanyEligibilityStatus.TRANSIENT_FAILURE
    assert failed.slots == ()


def test_companyfacts_and_history_body_read_errors_are_transient(monkeypatch, tmp_path) -> None:
    accession = f"{_CIK}-26-000001"
    root = _root_payload([_filing(accession, "10-K", "2025-12-31", "2026-02-15T16:00:00.000Z")])
    store, _, _, client = _setup_api(monkeypatch, tmp_path, _payload({}), root)

    class BrokenBody:
        def read(self) -> bytes:
            raise OSError("synthetic body failure")

        def close(self) -> None:
            pass

    class BrokenResponse:
        def __init__(self, url: str) -> None:
            self.url = url
            self.body = BrokenBody()

    client.open = lambda url, **kwargs: BrokenResponse(url)  # type: ignore[method-assign]
    with pytest.raises(TransientProviderError, match="companyfacts response read"):
        companyfacts.fetch_sec_canonical_quarters("SYN", client, store, utc_now=lambda: _AT)
    with pytest.raises(TransientProviderError, match="history response read"):
        companyfacts._fetch_history_page(
            client, store, _CIK, f"CIK{_CIK}-submissions-001.json", lambda: _AT
        )


def test_chronological_period_resolution_fixes_crm_10k_year_tagging() -> None:
    accn_q3 = f"{_CIK}-25-000238"
    accn_10k = f"{_CIK}-26-000060"
    accn_q1 = f"{_CIK}-26-000127"
    facts = (
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(30_000),
            date(2025, 2, 1),
            date(2025, 10, 31),
            2026,
            "Q3",
            "10-Q",
            date(2025, 12, 4),
            accn_q3,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(40_000),
            date(2025, 2, 1),
            date(2026, 1, 31),
            2025,  # CRM anomaly: DocumentFiscalYearFocus 2025 on 10-K ending Jan 2026
            "FY",
            "10-K",
            date(2026, 3, 2),
            accn_10k,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(11_000),
            date(2026, 2, 1),
            date(2026, 4, 30),
            2027,
            "Q1",
            "10-Q",
            date(2026, 5, 28),
            accn_q1,
            None,
        ),
    )
    filings = {
        accn_q3: companyfacts.SecCompanyFactsFiling(
            accn_q3, "10-Q", date(2025, 10, 31), datetime(2025, 12, 4, 2, tzinfo=UTC)
        ),
        accn_10k: companyfacts.SecCompanyFactsFiling(
            accn_10k, "10-K", date(2026, 1, 31), datetime(2026, 3, 2, 21, tzinfo=UTC)
        ),
        accn_q1: companyfacts.SecCompanyFactsFiling(
            accn_q1, "10-Q", date(2026, 4, 30), datetime(2026, 5, 28, 22, tzinfo=UTC)
        ),
    }
    slots = companyfacts.project_sec_companyfacts_quarters(
        facts, filings, "obs_crm", requested_periods=((2026, 3), (2026, 4), (2027, 1))
    )
    assert len(slots) == 3

    slot_q4 = slots[1]
    assert slot_q4.fiscal_year == 2026
    assert slot_q4.fiscal_quarter == 4

    rev = slot_q4.field(companyfacts.SecCanonicalMetric.REVENUE)
    assert rev.status is companyfacts.SecCanonicalFieldStatus.PRESENT
    assert rev.value == Decimal(10_000)
    assert len(rev.evidence) == 1
    # Parent evidence has canonical period
    assert rev.evidence[0].fiscal_year_focus == 2026
    assert rev.evidence[0].fiscal_period_focus == "Q4"
    # Source annual evidence preserves native fact.fy == 2025
    annual_source = rev.evidence[0].source_evidence[0]
    assert annual_source.accession_number == accn_10k
    assert annual_source.fiscal_year_focus == 2025

    # EPS stays non-derived
    eps = slot_q4.field(companyfacts.SecCanonicalMetric.GAAP_DILUTED_EPS)
    assert eps.status is companyfacts.SecCanonicalFieldStatus.MISSING
    assert eps.value is None


def test_chronological_period_resolution_fixes_crwd_discontinuous_year_sequence() -> None:
    accn_q3 = f"{_CIK}-24-000026"
    accn_10k = f"{_CIK}-25-000009"
    accn_q1 = f"{_CIK}-25-000019"
    accn_q2 = f"{_CIK}-25-000025"
    accn_q3_next = f"{_CIK}-25-000033"
    facts = (
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(2500),
            date(2024, 2, 1),
            date(2024, 10, 31),
            2025,
            "Q3",
            "10-Q",
            date(2024, 11, 27),
            accn_q3,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(3500),
            date(2024, 2, 1),
            date(2025, 1, 31),
            2024,  # CRWD anomaly: tagged fy=2024 on 10-K ending Jan 2025
            "FY",
            "10-K",
            date(2025, 3, 10),
            accn_10k,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(1000),
            date(2025, 2, 1),
            date(2025, 4, 30),
            2025,  # CRWD anomaly: tagged fy=2025 on Q1 ending Apr 2025
            "Q1",
            "10-Q",
            date(2025, 6, 4),
            accn_q1,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(1100),
            date(2025, 5, 1),
            date(2025, 7, 31),
            2026,
            "Q2",
            "10-Q",
            date(2025, 8, 28),
            accn_q2,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(1200),
            date(2025, 8, 1),
            date(2025, 10, 31),
            2026,
            "Q3",
            "10-Q",
            date(2025, 12, 3),
            accn_q3_next,
            None,
        ),
    )
    filings = {
        accn_q3: companyfacts.SecCompanyFactsFiling(
            accn_q3, "10-Q", date(2024, 10, 31), datetime(2024, 11, 27, 2, tzinfo=UTC)
        ),
        accn_10k: companyfacts.SecCompanyFactsFiling(
            accn_10k, "10-K", date(2025, 1, 31), datetime(2025, 3, 10, 2, tzinfo=UTC)
        ),
        accn_q1: companyfacts.SecCompanyFactsFiling(
            accn_q1, "10-Q", date(2025, 4, 30), datetime(2025, 6, 4, 2, tzinfo=UTC)
        ),
        accn_q2: companyfacts.SecCompanyFactsFiling(
            accn_q2, "10-Q", date(2025, 7, 31), datetime(2025, 8, 28, 2, tzinfo=UTC)
        ),
        accn_q3_next: companyfacts.SecCompanyFactsFiling(
            accn_q3_next, "10-Q", date(2025, 10, 31), datetime(2025, 12, 3, 2, tzinfo=UTC)
        ),
    }
    slots = companyfacts.project_sec_companyfacts_quarters(
        facts,
        filings,
        "obs_crwd",
        requested_periods=((2025, 3), (2025, 4), (2026, 1), (2026, 2)),
    )
    assert len(slots) == 4

    # FY2025Q4 flow derived (3500 - 2500)
    slot_q4 = slots[1]
    assert (slot_q4.fiscal_year, slot_q4.fiscal_quarter) == (2025, 4)
    rev_q4 = slot_q4.field(companyfacts.SecCanonicalMetric.REVENUE)
    assert rev_q4.status is companyfacts.SecCanonicalFieldStatus.PRESENT
    assert rev_q4.value == Decimal(1000)

    # FY2026Q1 discrete 10-Q fact
    slot_q1 = slots[2]
    assert (slot_q1.fiscal_year, slot_q1.fiscal_quarter) == (2026, 1)
    rev_q1 = slot_q1.field(companyfacts.SecCanonicalMetric.REVENUE)
    assert rev_q1.status is companyfacts.SecCanonicalFieldStatus.PRESENT
    assert rev_q1.value == Decimal(1000)
    # Native fact.fy preserved
    assert rev_q1.evidence[0].fiscal_year_focus == 2025

    # FY2026Q2 discrete 10-Q fact
    slot_q2 = slots[3]
    assert (slot_q2.fiscal_year, slot_q2.fiscal_quarter) == (2026, 2)
    rev_q2 = slot_q2.field(companyfacts.SecCanonicalMetric.REVENUE)
    assert rev_q2.status is companyfacts.SecCanonicalFieldStatus.PRESENT
    assert rev_q2.value == Decimal(1100)


def test_q4_flow_derivation_incompatible_cohort_boundaries() -> None:
    accn_q3 = f"{_CIK}-25-000003"
    accn_fy = f"{_CIK}-26-000001"
    filings = {
        accn_q3: companyfacts.SecCompanyFactsFiling(
            accn_q3, "10-Q", date(2025, 9, 30), datetime(2025, 11, 10, tzinfo=UTC)
        ),
        accn_fy: companyfacts.SecCompanyFactsFiling(
            accn_fy, "10-K", date(2025, 12, 31), datetime(2026, 2, 15, tzinfo=UTC)
        ),
    }

    # Incompatible start date: annual starts 2025-01-01, Q3 YTD starts 2025-02-01
    bad_start_facts = (
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(650),
            date(2025, 2, 1),
            date(2025, 9, 30),
            2025,
            "Q3",
            "10-Q",
            date(2025, 11, 10),
            accn_q3,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(1000),
            date(2025, 1, 1),
            date(2025, 12, 31),
            2025,
            "FY",
            "10-K",
            date(2026, 2, 15),
            accn_fy,
            None,
        ),
    )
    slots = companyfacts.project_sec_companyfacts_quarters(
        bad_start_facts, filings, "obs_start", requested_periods=((2025, 4),)
    )
    assert (
        slots[0].field(companyfacts.SecCanonicalMetric.REVENUE).status
        is companyfacts.SecCanonicalFieldStatus.MISSING
    )

    # Incompatible unit: annual USD, Q3 YTD EUR
    bad_unit_facts = (
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "EUR",
            Decimal(650),
            date(2025, 1, 1),
            date(2025, 9, 30),
            2025,
            "Q3",
            "10-Q",
            date(2025, 11, 10),
            accn_q3,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(1000),
            date(2025, 1, 1),
            date(2025, 12, 31),
            2025,
            "FY",
            "10-K",
            date(2026, 2, 15),
            accn_fy,
            None,
        ),
    )
    slots = companyfacts.project_sec_companyfacts_quarters(
        bad_unit_facts, filings, "obs_unit", requested_periods=((2025, 4),)
    )
    assert (
        slots[0].field(companyfacts.SecCanonicalMetric.REVENUE).status
        is companyfacts.SecCanonicalFieldStatus.MISSING
    )

    # Never derive annual EPS from 12M minus 9M: remains MISSING
    eps_facts = (
        companyfacts.SecCompanyFactsFact(
            "EarningsPerShareDiluted",
            "USD/shares",
            Decimal("1.80"),
            date(2025, 1, 1),
            date(2025, 9, 30),
            2025,
            "Q3",
            "10-Q",
            date(2025, 11, 10),
            accn_q3,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "EarningsPerShareDiluted",
            "USD/shares",
            Decimal("2.50"),
            date(2025, 1, 1),
            date(2025, 12, 31),
            2025,
            "FY",
            "10-K",
            date(2026, 2, 15),
            accn_fy,
            None,
        ),
    )
    slots = companyfacts.project_sec_companyfacts_quarters(
        eps_facts, filings, "obs_eps", requested_periods=((2025, 4),)
    )
    assert (
        slots[0].field(companyfacts.SecCanonicalMetric.GAAP_DILUTED_EPS).status
        is companyfacts.SecCanonicalFieldStatus.MISSING
    )


def test_coverage_complete_distinction_from_field_completeness(monkeypatch, tmp_path) -> None:
    accession = f"{_CIK}-26-000001"
    root = _root_payload([_filing(accession, "10-K", "2025-12-31", "2026-02-15T16:00:00.000Z")])
    revenue_fact = _fact(
        "Revenue",
        "1000",
        accn=accession,
        form="10-K",
        fy=2025,
        fp="FY",
        start="2025-01-01",
        end="2025-12-31",
    )
    store, _, _, client = _setup_api(
        monkeypatch,
        tmp_path,
        _payload({"revenue": [revenue_fact]}),
        root,
    )
    result = companyfacts.fetch_sec_canonical_quarters("SYN", client, store, utc_now=lambda: _AT)
    # Transport coverage is complete (all target accessions found in submissions)
    assert result.coverage_complete is True
    assert result.periods_resolved is True
    # But individual fields lacking source facts remain MISSING, not COVERAGE_INCOMPLETE
    slot = result.slots[-1]
    assert (
        slot.field(companyfacts.SecCanonicalMetric.GAAP_DILUTED_EPS).status
        is companyfacts.SecCanonicalFieldStatus.MISSING
    )


def test_chronology_fails_closed_on_multi_year_contradiction() -> None:
    accn_q1 = f"{_CIK}-25-000001"
    accn_q2 = f"{_CIK}-25-000002"
    accn_q3 = f"{_CIK}-25-000003"
    facts = (
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(100),
            date(2025, 1, 1),
            date(2025, 3, 31),
            2025,
            "Q1",
            "10-Q",
            date(2025, 5, 1),
            accn_q1,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(110),
            date(2025, 4, 1),
            date(2025, 6, 30),
            2030,  # Contradiction: jumps to 2030
            "Q2",
            "10-Q",
            date(2025, 8, 1),
            accn_q2,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(120),
            date(2025, 7, 1),
            date(2025, 9, 30),
            2030,  # Majority would be 2030 (2 vs 1), but gap is 5 years (> 1)
            "Q3",
            "10-Q",
            date(2025, 11, 1),
            accn_q3,
            None,
        ),
    )
    filings = {
        accn_q1: companyfacts.SecCompanyFactsFiling(
            accn_q1, "10-Q", date(2025, 3, 31), datetime(2025, 5, 1, 16, tzinfo=UTC)
        ),
        accn_q2: companyfacts.SecCompanyFactsFiling(
            accn_q2, "10-Q", date(2025, 6, 30), datetime(2025, 8, 1, 16, tzinfo=UTC)
        ),
        accn_q3: companyfacts.SecCompanyFactsFiling(
            accn_q3, "10-Q", date(2025, 9, 30), datetime(2025, 11, 1, 16, tzinfo=UTC)
        ),
    }
    # Resolver must fail closed and NOT override Q1 to FY2030Q1
    slots = companyfacts.project_sec_companyfacts_quarters(
        facts, filings, "obs_multi_year", requested_periods=((2030, 1),)
    )
    # Slot (2030, 1) has no fact because Q1 is FY2025, not FY2030
    assert (
        slots[0].field(companyfacts.SecCanonicalMetric.REVENUE).status
        is companyfacts.SecCanonicalFieldStatus.MISSING
    )


def test_chronology_respects_acceptance_upper_cutoff() -> None:
    accn_q2 = f"{_CIK}-25-000100"
    accn_q3 = f"{_CIK}-25-000238"
    accn_10k = f"{_CIK}-26-000060"
    facts = (
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(20_000),
            date(2025, 2, 1),
            date(2025, 7, 31),
            2026,
            "Q2",
            "10-Q",
            date(2025, 9, 4),
            accn_q2,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(30_000),
            date(2025, 2, 1),
            date(2025, 10, 31),
            2026,
            "Q3",
            "10-Q",
            date(2025, 12, 4),
            accn_q3,
            None,
        ),
        companyfacts.SecCompanyFactsFact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
            Decimal(40_000),
            date(2025, 2, 1),
            date(2026, 1, 31),
            2025,  # 10-K with fy=2025 accepted later
            "FY",
            "10-K",
            date(2026, 3, 2),
            accn_10k,
            None,
        ),
    )
    filings = {
        accn_q2: companyfacts.SecCompanyFactsFiling(
            accn_q2, "10-Q", date(2025, 7, 31), datetime(2025, 9, 4, 2, tzinfo=UTC)
        ),
        accn_q3: companyfacts.SecCompanyFactsFiling(
            accn_q3, "10-Q", date(2025, 10, 31), datetime(2025, 12, 4, 2, tzinfo=UTC)
        ),
        accn_10k: companyfacts.SecCompanyFactsFiling(
            accn_10k, "10-K", date(2026, 1, 31), datetime(2026, 3, 2, 21, tzinfo=UTC)
        ),
    }

    # As of 2025-12-31, the 10-K is not yet accepted. It must be excluded from chronology and projection.
    cutoff = datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)
    slots_before = companyfacts.project_sec_companyfacts_quarters(
        facts, filings, "obs_pit", acceptance_upper=cutoff, requested_periods=((2026, 4),)
    )
    assert (
        slots_before[0].field(companyfacts.SecCanonicalMetric.REVENUE).status
        is companyfacts.SecCanonicalFieldStatus.MISSING
    )

    # After acceptance, the 10-K is included in chronology and resolves to (2026, 4)
    slots_after = companyfacts.project_sec_companyfacts_quarters(
        facts, filings, "obs_pit", requested_periods=((2026, 4),)
    )
    assert (
        slots_after[0].field(companyfacts.SecCanonicalMetric.REVENUE).status
        is companyfacts.SecCanonicalFieldStatus.PRESENT
    )
    assert slots_after[0].field(companyfacts.SecCanonicalMetric.REVENUE).value == Decimal(10_000)

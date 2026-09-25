from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from types import MappingProxyType

import pytest

from ohmydata.core import RequestSpec, SnapshotMode, SnapshotStore
from ohmydata.providers.sec import companyfacts
from ohmydata.providers.sec._event_discovery_models import SecDiscoverySource
from ohmydata.providers.sec.errors import (
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
    ambiguous = companyfacts.project_sec_companyfacts_quarters(
        (primary, conflicting),
        {accession: filing},
        "d" * 64,
        requested_periods=((2025, 1),),
    )
    revenue = ambiguous[0].field(companyfacts.SecCanonicalMetric.REVENUE)
    assert revenue.status is companyfacts.SecCanonicalFieldStatus.AMBIGUOUS
    assert {item.native_tag for item in revenue.evidence} == {primary.tag, conflicting.tag}


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
    result = companyfacts.fetch_sec_canonical_quarters("SYN", client, store, utc_now=lambda: _AT)
    assert not result.periods_resolved
    assert result.slots == ()
    assert result.unresolved_period_accessions == (newer,)


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

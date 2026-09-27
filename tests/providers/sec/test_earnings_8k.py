"""Synthetic SEC 8-K Item 2.02 earnings release contracts; no downloaded fixtures."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.providers.sec import companyfacts, earnings_8k
from ohmydata.providers.sec._companyfacts_models import (
    Sec8KReleaseEvidence,
    SecCanonicalFieldStatus,
    SecCanonicalMetric,
    SecCanonicalQuarterField,
    SecCanonicalQuarterlyResult,
    SecCanonicalQuarterSlot,
    SecQuarterlyEligibility,
)
from ohmydata.providers.sec.earnings_8k import (
    Sec8KEpsAmbiguousError,
    Sec8KEpsStatus,
    Sec8KQuarterlyEpsItem,
    Sec8KQuarterlyEpsResult,
    enrich_sec_canonical_quarters_with_8k_eps,
    fetch_sec_8k_quarterly_eps,
)
from ohmydata.providers.sec.errors import (
    CoverageError,
    ResourceLimitError,
    SchemaMismatchError,
)
from ohmydata.providers.sec.http import SecHttpClient
from ohmydata.providers.sec.quarterly import SecCompanyEligibilityStatus

CIK = "0001108524"
AT = datetime(2026, 3, 15, tzinfo=UTC)


def _index_html(accession: str, document: str = "ex-991.htm", doc_type: str = "EX-99.1") -> bytes:
    path = f"/Archives/edgar/data/{int(CIK)}/{accession.replace('-', '')}/{document}"
    return f"""
    <html><body>
    <h3>SEC Accession No. {accession}</h3>
    <table>
      <tr><td>1</td><td>8-K</td><td><a href="{path}">crm.htm</a></td><td>8-K</td></tr>
      <tr><td>2</td><td>{doc_type}</td><td><a href="{path}">{document}</a></td><td>{doc_type}</td></tr>
    </table>
    </body></html>
    """.encode()


def _exhibit_html(
    period_str: str = "January 31, 2026",
    diluted_eps: str = "2.07",
    *,
    include_non_gaap: bool = True,
    year_ended_only: bool = False,
    include_guidance: bool = True,
) -> bytes:
    guidance_section = (
        """
        <table>
          <tr><th>Full Year Guidance</th></tr>
          <tr><td>Diluted net income per share</td><td>$7.85 - $7.93</td></tr>
        </table>
        """
        if include_guidance
        else ""
    )
    non_gaap_section = (
        """
        <tr><td>Non-GAAP diluted net income per share</td><td>$3.81</td></tr>
        """
        if include_non_gaap
        else ""
    )
    header = f"Year Ended {period_str}" if year_ended_only else f"Three Months Ended {period_str}"
    return f"""
    <html><body>
    <h1>Financial Results for {header}</h1>
    {guidance_section}
    <table>
      <tr>
        <th colspan="2">{header}</th>
      </tr>
      <tr>
        <td>Revenues</td>
        <td>$11,201</td>
      </tr>
      <tr>
        <td>Basic net income per share</td>
        <td>$2.08</td>
      </tr>
      <tr>
        <td>Diluted net income per share</td>
        <td>${diluted_eps}</td>
      </tr>
      {non_gaap_section}
      <tr>
        <td>Shares used in computing diluted net income per share</td>
        <td>940</td>
      </tr>
    </table>
    </body></html>
    """.encode()


def _setup_8k_api(
    monkeypatch,
    tmp_path,
    rows: list[dict[str, object]],
    bodies_extra: dict[str, bytes] | None = None,
    files: list[dict[str, object]] | None = None,
):
    store = SnapshotStore(tmp_path)
    columns = (
        "accessionNumber",
        "form",
        "filingDate",
        "reportDate",
        "acceptanceDateTime",
        "primaryDocument",
        "items",
    )
    payload = {
        "cik": CIK,
        "entityType": "operating",
        "filings": {
            "recent": {name: [row.get(name) for row in rows] for name in columns},
            "files": files or [],
        },
    }
    client = SecHttpClient("Synthetic Test Contact test@example.invalid", opener=object())
    bodies: dict[str, bytes] = {
        "https://www.sec.gov/files/company_tickers.json": json.dumps(
            {
                "0": {"cik_str": int(CIK), "ticker": "CRM", "title": "Salesforce"},
                "1": {"cik_str": int(CIK), "ticker": "CRWD", "title": "CrowdStrike"},
            }
        ).encode(),
        f"https://data.sec.gov/submissions/CIK{CIK}.json": json.dumps(payload).encode(),
    }
    if bodies_extra:
        bodies.update(bodies_extra)

    class MockResponse:
        def __init__(self, url: str, body: bytes) -> None:
            self.url = url
            self.body = io.BytesIO(body)
            self.status = 200
            self.headers = {
                "Content-Type": "application/json" if url.endswith(".json") else "text/html",
                "Content-Length": str(len(body)),
            }

    def mock_open(url: str, **kwargs):
        body = bodies.get(url)
        if body is None:
            raise RuntimeError(f"Unexpected URL fetched in mock: {url}")
        return MockResponse(url, body)

    monkeypatch.setattr(client, "open", mock_open)
    return store, client


def test_8k_direct_gaap_diluted_eps_success(monkeypatch, tmp_path) -> None:
    accn = f"{CIK}-26-000056"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn.replace('-', '')}"
    rows = [
        {
            "accessionNumber": accn,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-02-25",
            "acceptanceDateTime": "2026-02-25T21:03:12.000Z",
            "primaryDocument": "crm-20260225.htm",
            "items": "2.02,9.01",
        }
    ]
    bodies = {
        f"{base}/{accn}-index.htm": _index_html(accn),
        f"{base}/ex-991.htm": _exhibit_html(period_str="January 31, 2026", diluted_eps="2.07"),
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows, bodies)

    res = earnings_8k.fetch_sec_8k_quarterly_eps(
        "CRM",
        client,
        store,
        target_quarters=((2026, 4, date(2026, 1, 31)),),
        utc_now=lambda: AT,
    )
    assert res.ticker == "CRM"
    assert res.coverage_complete is True
    assert len(res.items) == 1

    item = res.items[0]
    assert item.fiscal_year == 2026
    assert item.fiscal_quarter == 4
    assert item.period_end == date(2026, 1, 31)
    assert item.status is earnings_8k.Sec8KEpsStatus.PRESENT
    assert item.value == Decimal("2.07")
    assert item.unit == "USD/shares"
    assert item.native_label == "Diluted net income per share"
    assert item.accession_number == accn
    assert item.form == "8-K"
    assert item.filing_date == date(2026, 2, 25)
    assert item.accepted_at == datetime(2026, 2, 25, 21, 3, 12, tzinfo=UTC)
    assert item.filing_url == f"{base}/{accn}-index.htm"
    assert item.exhibit_url == f"{base}/ex-991.htm"
    assert item.index_observation_id is not None
    assert item.exhibit_observation_id is not None


def test_8k_direct_gaap_diluted_eps_parenthesized_negative(monkeypatch, tmp_path) -> None:
    accn = f"{CIK}-25-000005"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn.replace('-', '')}"
    rows = [
        {
            "accessionNumber": accn,
            "form": "8-K",
            "filingDate": "2025-03-04",
            "reportDate": "2025-03-04",
            "acceptanceDateTime": "2025-03-04T21:11:41.000Z",
            "primaryDocument": "crwd-20250304.htm",
            "items": "2.02,9.01",
        }
    ]
    bodies = {
        f"{base}/{accn}-index.htm": _index_html(accn),
        f"{base}/ex-991.htm": _exhibit_html(period_str="January 31, 2025", diluted_eps="(0.37)"),
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows, bodies)

    res = earnings_8k.fetch_sec_8k_quarterly_eps(
        "CRM",
        client,
        store,
        target_quarters=((2025, 4, date(2025, 1, 31)),),
        utc_now=lambda: AT,
    )
    assert res.coverage_complete is True
    assert len(res.items) == 1
    assert res.items[0].status is earnings_8k.Sec8KEpsStatus.PRESENT
    assert res.items[0].value == Decimal("-0.37")


def test_8k_parser_does_not_shift_blank_target_into_comparative_column() -> None:
    html = b"""
    <table>
      <tr><th></th><th>Three Months Ended January 31, 2026</th>
          <th>Three Months Ended October 31, 2025</th></tr>
      <tr><td>Diluted net income per share</td><td></td><td>1.23</td></tr>
    </table>
    """

    result = earnings_8k.parse_sec_8k_earnings_release(html, expected_period_end=date(2026, 1, 31))

    assert result == {}


def test_8k_parser_keeps_currency_and_parenthesized_values_within_period() -> None:
    html = b"""
    <table>
      <tr><th></th><th colspan="2">Three Months Ended January 31, 2026</th>
          <th>Three Months Ended October 31, 2025</th></tr>
      <tr><td>Diluted net income per share</td><td>$</td><td>(0.37)</td><td>$0.25</td></tr>
    </table>
    """

    result = earnings_8k.parse_sec_8k_earnings_release(html, expected_period_end=date(2026, 1, 31))

    assert result == {date(2026, 1, 31): (Decimal("-0.37"), "Diluted net income per share")}


def test_8k_non_gaap_and_guidance_rejection(monkeypatch, tmp_path) -> None:
    accn = f"{CIK}-26-000056"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn.replace('-', '')}"
    rows = [
        {
            "accessionNumber": accn,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-02-25",
            "acceptanceDateTime": "2026-02-25T21:03:12.000Z",
            "primaryDocument": "crm-20260225.htm",
            "items": "2.02,9.01",
        }
    ]
    # Exhibit with ONLY guidance and non-GAAP rows; no GAAP row
    exhibit_only_non_gaap = b"""
    <html><body>
    <table>
      <tr><th>Full Year Guidance</th></tr>
      <tr><td>Diluted net income per share range</td><td>$1.77 - $1.79</td></tr>
    </table>
    <table>
      <tr><th>Three Months Ended January 31, 2026</th></tr>
      <tr><td>Non-GAAP diluted net income per share</td><td>$3.81</td></tr>
      <tr><td>Adjusted diluted earnings per share</td><td>$3.50</td></tr>
    </table>
    </body></html>
    """
    bodies = {
        f"{base}/{accn}-index.htm": _index_html(accn),
        f"{base}/ex-991.htm": exhibit_only_non_gaap,
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows, bodies)

    res = earnings_8k.fetch_sec_8k_quarterly_eps(
        "CRM",
        client,
        store,
        target_quarters=((2026, 4, date(2026, 1, 31)),),
        utc_now=lambda: AT,
    )
    assert res.coverage_complete is True
    assert len(res.items) == 1
    # Non-GAAP and guidance rejected -> clean search remains MISSING
    assert res.items[0].status is earnings_8k.Sec8KEpsStatus.MISSING
    assert res.items[0].value is None
    assert res.items[0].reason is not None


def test_8k_missing_period_rejection(monkeypatch, tmp_path) -> None:
    accn = f"{CIK}-26-000056"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn.replace('-', '')}"
    rows = [
        {
            "accessionNumber": accn,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-02-25",
            "acceptanceDateTime": "2026-02-25T21:03:12.000Z",
            "primaryDocument": "crm-20260225.htm",
            "items": "2.02,9.01",
        }
    ]
    # Exhibit only has Year Ended (12M), no Three Months Ended
    bodies = {
        f"{base}/{accn}-index.htm": _index_html(accn),
        f"{base}/ex-991.htm": _exhibit_html(
            period_str="January 31, 2026", diluted_eps="7.80", year_ended_only=True
        ),
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows, bodies)

    res = earnings_8k.fetch_sec_8k_quarterly_eps(
        "CRM",
        client,
        store,
        target_quarters=((2026, 4, date(2026, 1, 31)),),
        utc_now=lambda: AT,
    )
    assert res.coverage_complete is True
    assert len(res.items) == 1
    assert res.items[0].status is earnings_8k.Sec8KEpsStatus.MISSING
    assert res.items[0].value is None


def test_8k_ambiguous_multiple_candidates_in_single_exhibit(monkeypatch, tmp_path) -> None:
    accn = f"{CIK}-26-000056"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn.replace('-', '')}"
    rows = [
        {
            "accessionNumber": accn,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-02-25",
            "acceptanceDateTime": "2026-02-25T21:03:12.000Z",
            "primaryDocument": "crm-20260225.htm",
            "items": "2.02,9.01",
        }
    ]
    # Two conflicting tables in the same exhibit reporting different GAAP diluted EPS
    conflicting_exhibit = b"""
    <html><body>
    <table>
      <tr><th>Three Months Ended January 31, 2026</th></tr>
      <tr><td>Diluted net income per share</td><td>$2.07</td></tr>
    </table>
    <table>
      <tr><th>Three Months Ended January 31, 2026</th></tr>
      <tr><td>GAAP diluted net income per share</td><td>$2.15</td></tr>
    </table>
    </body></html>
    """
    bodies = {
        f"{base}/{accn}-index.htm": _index_html(accn),
        f"{base}/ex-991.htm": conflicting_exhibit,
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows, bodies)

    res = earnings_8k.fetch_sec_8k_quarterly_eps(
        "CRM",
        client,
        store,
        target_quarters=((2026, 4, date(2026, 1, 31)),),
        utc_now=lambda: AT,
    )
    assert res.coverage_complete is True
    assert len(res.items) == 1
    assert res.items[0].status is earnings_8k.Sec8KEpsStatus.AMBIGUOUS
    assert res.items[0].value is None
    assert "conflicting" in (res.items[0].reason or "").lower()


def test_wrong_non_202_8k_rejection(monkeypatch, tmp_path) -> None:
    accn = f"{CIK}-26-000099"
    rows = [
        {
            "accessionNumber": accn,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-02-25",
            "acceptanceDateTime": "2026-02-25T21:03:12.000Z",
            "primaryDocument": "crm-non202.htm",
            "items": "5.02,9.01",  # No Item 2.02!
        }
    ]
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows, {})

    res = earnings_8k.fetch_sec_8k_quarterly_eps(
        "CRM",
        client,
        store,
        target_quarters=((2026, 4, date(2026, 1, 31)),),
        utc_now=lambda: AT,
    )
    assert res.coverage_complete is True
    # 8-K without Item 2.02 is rejected/skipped -> MISSING
    assert res.items[0].status is earnings_8k.Sec8KEpsStatus.MISSING


def test_8k_acceptance_upper_cutoff(monkeypatch, tmp_path) -> None:
    accn = f"{CIK}-26-000056"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn.replace('-', '')}"
    rows = [
        {
            "accessionNumber": accn,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-02-25",
            "acceptanceDateTime": "2026-02-25T21:03:12.000Z",
            "primaryDocument": "crm-20260225.htm",
            "items": "2.02,9.01",
        }
    ]
    bodies = {
        f"{base}/{accn}-index.htm": _index_html(accn),
        f"{base}/ex-991.htm": _exhibit_html(period_str="January 31, 2026", diluted_eps="2.07"),
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows, bodies)

    # Acceptance upper set BEFORE the 8-K acceptance time
    cutoff = datetime(2026, 2, 20, 23, 59, 59, tzinfo=UTC)
    res = earnings_8k.fetch_sec_8k_quarterly_eps(
        "CRM",
        client,
        store,
        target_quarters=((2026, 4, date(2026, 1, 31)),),
        acceptance_upper=cutoff,
        utc_now=lambda: AT,
    )
    assert res.coverage_complete is True
    # Excluded by PIT cutoff -> MISSING
    assert res.items[0].status is earnings_8k.Sec8KEpsStatus.MISSING


def test_8k_amendment_conflicting_value_ambiguous(monkeypatch, tmp_path) -> None:
    accn_orig = f"{CIK}-26-000056"
    accn_amend = f"{CIK}-26-000057"
    base_orig = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn_orig.replace('-', '')}"
    base_amend = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn_amend.replace('-', '')}"

    rows = [
        {
            "accessionNumber": accn_orig,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-02-25",
            "acceptanceDateTime": "2026-02-25T21:03:12.000Z",
            "primaryDocument": "crm-20260225.htm",
            "items": "2.02,9.01",
        },
        {
            "accessionNumber": accn_amend,
            "form": "8-K/A",
            "filingDate": "2026-02-26",
            "reportDate": "2026-02-25",
            "acceptanceDateTime": "2026-02-26T14:00:00.000Z",
            "primaryDocument": "crm-20260226.htm",
            "items": "2.02,9.01",
        },
    ]
    bodies = {
        f"{base_orig}/{accn_orig}-index.htm": _index_html(accn_orig),
        f"{base_orig}/ex-991.htm": _exhibit_html(period_str="January 31, 2026", diluted_eps="2.07"),
        f"{base_amend}/{accn_amend}-index.htm": _index_html(accn_amend),
        # Amendment reports a different EPS value: 2.10
        f"{base_amend}/ex-991.htm": _exhibit_html(
            period_str="January 31, 2026", diluted_eps="2.10"
        ),
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows, bodies)

    res = earnings_8k.fetch_sec_8k_quarterly_eps(
        "CRM",
        client,
        store,
        target_quarters=((2026, 4, date(2026, 1, 31)),),
        utc_now=lambda: AT,
    )
    assert res.coverage_complete is True
    assert len(res.items) == 1
    # Conflicting amendment value -> AMBIGUOUS with alternatives
    assert res.items[0].status is earnings_8k.Sec8KEpsStatus.AMBIGUOUS
    assert len(res.items[0].alternatives) == 2


def test_8k_amendment_identical_value_present(monkeypatch, tmp_path) -> None:
    accn_orig = f"{CIK}-26-000056"
    accn_amend = f"{CIK}-26-000057"
    base_orig = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn_orig.replace('-', '')}"
    base_amend = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn_amend.replace('-', '')}"

    rows = [
        {
            "accessionNumber": accn_orig,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-02-25",
            "acceptanceDateTime": "2026-02-25T21:03:12.000Z",
            "primaryDocument": "crm-20260225.htm",
            "items": "2.02,9.01",
        },
        {
            "accessionNumber": accn_amend,
            "form": "8-K/A",
            "filingDate": "2026-02-26",
            "reportDate": "2026-02-25",
            "acceptanceDateTime": "2026-02-26T14:00:00.000Z",
            "primaryDocument": "crm-20260226.htm",
            "items": "2.02,9.01",
        },
    ]
    bodies = {
        f"{base_orig}/{accn_orig}-index.htm": _index_html(accn_orig),
        f"{base_orig}/ex-991.htm": _exhibit_html(period_str="January 31, 2026", diluted_eps="2.07"),
        f"{base_amend}/{accn_amend}-index.htm": _index_html(accn_amend),
        # Amendment reports identical EPS value: 2.07
        f"{base_amend}/ex-991.htm": _exhibit_html(
            period_str="January 31, 2026", diluted_eps="2.07"
        ),
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows, bodies)

    res = earnings_8k.fetch_sec_8k_quarterly_eps(
        "CRM",
        client,
        store,
        target_quarters=((2026, 4, date(2026, 1, 31)),),
        utc_now=lambda: AT,
    )
    assert res.coverage_complete is True
    assert len(res.items) == 1
    assert res.items[0].status is earnings_8k.Sec8KEpsStatus.PRESENT
    assert res.items[0].value == Decimal("2.07")
    assert res.items[0].accession_number == accn_amend
    assert len(res.items[0].alternatives) == 1
    assert res.items[0].alternatives[0].accession_number == accn_orig


def test_8k_resource_limits() -> None:
    # Exceeding byte limit raises ResourceLimitError
    huge_bytes = b"<html>" + b"A" * (2 * 1024 * 1024 + 10) + b"</html>"
    with pytest.raises(ResourceLimitError, match="byte limit"):
        earnings_8k.parse_sec_8k_earnings_release(huge_bytes)

    # Exceeding HTML tag limit raises ResourceLimitError
    many_tags = b"<html><body>" + b"<div></div>" * 30_001 + b"</body></html>"
    with pytest.raises(ResourceLimitError, match="HTML structure limit"):
        earnings_8k.parse_sec_8k_earnings_release(many_tags)


def test_8k_invalid_utf8_raises_schema_mismatch(monkeypatch, tmp_path) -> None:
    accn = f"{CIK}-26-000056"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn.replace('-', '')}"
    rows = [
        {
            "accessionNumber": accn,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-02-25",
            "acceptanceDateTime": "2026-02-25T21:03:12.000Z",
            "primaryDocument": "crm-20260225.htm",
            "items": "2.02,9.01",
        }
    ]
    # Malformed UTF-8 in index
    bodies_bad_index = {
        f"{base}/{accn}-index.htm": b"<html><body>\xff\xfe\x00\x00</body></html>",
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows, bodies_bad_index)
    with pytest.raises(SchemaMismatchError, match="UTF-8"):
        earnings_8k.fetch_sec_8k_quarterly_eps(
            "CRM",
            client,
            store,
            target_quarters=((2026, 4, date(2026, 1, 31)),),
            utc_now=lambda: AT,
        )

    # Malformed UTF-8 in exhibit
    bodies_bad_exhibit = {
        f"{base}/{accn}-index.htm": _index_html(accn),
        f"{base}/ex-991.htm": b"<html><body>\x80\x81\x82</body></html>",
    }
    store2, client2 = _setup_8k_api(monkeypatch, tmp_path / "exhibit", rows, bodies_bad_exhibit)
    with pytest.raises(SchemaMismatchError, match="UTF-8"):
        earnings_8k.fetch_sec_8k_quarterly_eps(
            "CRM",
            client2,
            store2,
            target_quarters=((2026, 4, date(2026, 1, 31)),),
            utc_now=lambda: AT,
        )


def test_8k_history_page_closure(monkeypatch, tmp_path) -> None:
    accn = f"{CIK}-26-000056"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn.replace('-', '')}"
    # The qualifying 8-K is in a historical page, NOT in recent filings
    columns = (
        "accessionNumber",
        "form",
        "filingDate",
        "reportDate",
        "acceptanceDateTime",
        "primaryDocument",
        "items",
    )
    hist_row = {
        "accessionNumber": accn,
        "form": "8-K",
        "filingDate": "2026-02-25",
        "reportDate": "2026-02-25",
        "acceptanceDateTime": "2026-02-25T21:03:12.000Z",
        "primaryDocument": "crm-20260225.htm",
        "items": "2.02,9.01",
    }
    hist_payload = {
        "cik": CIK,
        "filings": {
            "recent": {name: [hist_row[name]] for name in columns},
            "files": [],
        },
    }
    hist_name = f"CIK{CIK}-submissions-001.json"
    files = [
        {
            "name": hist_name,
            "filingFrom": "2026-02-01",
            "filingTo": "2026-03-01",
        }
    ]
    bodies = {
        f"https://data.sec.gov/submissions/{hist_name}": json.dumps(hist_payload).encode(),
        f"{base}/{accn}-index.htm": _index_html(accn),
        f"{base}/ex-991.htm": _exhibit_html(period_str="January 31, 2026", diluted_eps="2.07"),
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows=[], bodies_extra=bodies, files=files)

    res = earnings_8k.fetch_sec_8k_quarterly_eps(
        "CRM",
        client,
        store,
        target_quarters=((2026, 4, date(2026, 1, 31)),),
        utc_now=lambda: AT,
    )
    assert res.coverage_complete is True
    assert len(res.items) == 1
    assert res.items[0].status is earnings_8k.Sec8KEpsStatus.PRESENT
    assert res.items[0].value == Decimal("2.07")


def test_validate_target_quarters(monkeypatch, tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    client = SecHttpClient("Synthetic Test Contact test@example.invalid", opener=object())

    # Empty tuple
    with pytest.raises(ValueError, match="non-empty tuple of at most 8"):
        earnings_8k.fetch_sec_8k_quarterly_eps("CRM", client, store, target_quarters=())

    # >8 items
    too_many = tuple((2020 + i, 1, date(2020 + i, 3, 31)) for i in range(9))
    with pytest.raises(ValueError, match="non-empty tuple of at most 8"):
        earnings_8k.fetch_sec_8k_quarterly_eps("CRM", client, store, target_quarters=too_many)

    # Duplicate fiscal quarter key
    dupe_key = ((2026, 4, date(2026, 1, 31)), (2026, 4, date(2026, 2, 28)))
    with pytest.raises(ValueError, match="duplicate fiscal quarter key"):
        earnings_8k.fetch_sec_8k_quarterly_eps("CRM", client, store, target_quarters=dupe_key)

    # Duplicate period_end date
    dupe_date = ((2026, 4, date(2026, 1, 31)), (2025, 4, date(2026, 1, 31)))
    with pytest.raises(ValueError, match="duplicate period_end date"):
        earnings_8k.fetch_sec_8k_quarterly_eps("CRM", client, store, target_quarters=dupe_date)

    # Invalid fiscal quarter number (5)
    invalid_q = ((2026, 5, date(2026, 1, 31)),)
    with pytest.raises(ValueError, match="fiscal_quarter must be an integer in 1..4"):
        earnings_8k.fetch_sec_8k_quarterly_eps("CRM", client, store, target_quarters=invalid_q)

    # Invalid period_end (not a date)
    invalid_date = ((2026, 4, "2026-01-31"),)
    with pytest.raises(TypeError, match="period_end must be a date instance"):
        earnings_8k.fetch_sec_8k_quarterly_eps("CRM", client, store, target_quarters=invalid_date)


def test_enrich_rejects_identity_mismatch() -> None:
    eligibility = SecQuarterlyEligibility(
        SecCompanyEligibilityStatus.SEC_COMPANY,
        "CRM",
        CIK,
        "operating",
        "map_obs",
        "sub_obs",
        ("0001108524-26-000060",),
        "operating entity matches",
    )
    canonical = SecCanonicalQuarterlyResult(
        "CRM", CIK, eligibility, (), "obs_cf", ("obs_sub",), True, True
    )

    # Ticker mismatch
    eps_wrong_ticker = earnings_8k.Sec8KQuarterlyEpsResult(
        "AAPL", CIK, (), "map_obs", "sub_obs", True
    )
    with pytest.raises(ValueError, match="ticker mismatch"):
        earnings_8k.enrich_sec_canonical_quarters_with_8k_eps(canonical, eps_wrong_ticker)

    # CIK mismatch
    eps_wrong_cik = earnings_8k.Sec8KQuarterlyEpsResult(
        "CRM", "0000320193", (), "map_obs", "sub_obs", True
    )
    with pytest.raises(ValueError, match="CIK mismatch"):
        earnings_8k.enrich_sec_canonical_quarters_with_8k_eps(canonical, eps_wrong_cik)


def test_enrich_rejects_missing_provenance() -> None:
    eligibility = SecQuarterlyEligibility(
        SecCompanyEligibilityStatus.SEC_COMPANY,
        "CRM",
        CIK,
        "operating",
        "map_obs",
        "sub_obs",
        ("0001108524-26-000060",),
        "operating entity matches",
    )
    canonical = SecCanonicalQuarterlyResult(
        "CRM", CIK, eligibility, (), "obs_cf", ("obs_sub",), True, True
    )

    # Item claims PRESENT but has None for required provenance fields
    bad_item = earnings_8k.Sec8KQuarterlyEpsItem(
        2026,
        4,
        date(2026, 1, 31),
        earnings_8k.Sec8KEpsStatus.PRESENT,
        Decimal("2.07"),
        "USD/shares",
        "Diluted net income per share",
        None,  # missing accession!
        "8-K",
        date(2026, 2, 25),
        datetime(2026, 2, 25, 21, tzinfo=UTC),
        "https://example.invalid",
        "https://example.invalid",
        "https://example.invalid",
        "obs1",
        "obs2",
    )
    bad_res = earnings_8k.Sec8KQuarterlyEpsResult(
        "CRM", CIK, (bad_item,), "map_obs", "sub_obs", True
    )
    with pytest.raises(ValueError, match="valid accession_number"):
        earnings_8k.enrich_sec_canonical_quarters_with_8k_eps(canonical, bad_res)


def test_existing_10k_explicit_q4_eps_still_wins() -> None:
    accn_10k = f"{CIK}-26-000060"
    facts = (
        companyfacts.SecCompanyFactsFact(
            "EarningsPerShareDiluted",
            "USD/shares",
            Decimal("1.95"),
            date(2025, 11, 1),
            date(2026, 1, 31),
            2026,
            "FY",
            "10-K",
            date(2026, 3, 2),
            accn_10k,
            None,
        ),
    )
    filings = {
        accn_10k: companyfacts.SecCompanyFactsFiling(
            accn_10k, "10-K", date(2026, 1, 31), datetime(2026, 3, 2, 21, tzinfo=UTC)
        ),
    }
    slots = companyfacts.project_sec_companyfacts_quarters(
        facts, filings, "obs_10k", requested_periods=((2026, 4),)
    )
    eligibility = SecQuarterlyEligibility(
        SecCompanyEligibilityStatus.SEC_COMPANY,
        "CRM",
        CIK,
        "operating",
        "map_obs",
        "sub_obs",
        (accn_10k,),
        "operating entity matches",
    )
    canonical = companyfacts.SecCanonicalQuarterlyResult(
        "CRM",
        CIK,
        eligibility,
        slots,
        "obs_companyfacts",
        ("obs_submissions",),
        True,
        True,
    )
    assert canonical.slots[0].field(
        companyfacts.SecCanonicalMetric.GAAP_DILUTED_EPS
    ).value == Decimal("1.95")

    # 8-K reports 2.07
    eps_item = earnings_8k.Sec8KQuarterlyEpsItem(
        2026,
        4,
        date(2026, 1, 31),
        earnings_8k.Sec8KEpsStatus.PRESENT,
        Decimal("2.07"),
        "USD/shares",
        "Diluted net income per share",
        "0001108524-26-000056",
        "8-K",
        date(2026, 2, 25),
        datetime(2026, 2, 25, 21, tzinfo=UTC),
        "https://www.sec.gov/Archives/edgar/data/1108524/000110852426000056/0001108524-26-000056-index.htm",
        "https://www.sec.gov/Archives/edgar/data/1108524/000110852426000056/0001108524-26-000056-index.htm",
        "https://www.sec.gov/Archives/edgar/data/1108524/000110852426000056/ex-991.htm",
        "obs_index",
        "obs_exhibit",
    )
    eps_res = earnings_8k.Sec8KQuarterlyEpsResult(
        "CRM", CIK, (eps_item,), "map_obs", "sub_obs", True
    )

    enriched = earnings_8k.enrich_sec_canonical_quarters_with_8k_eps(canonical, eps_res)
    # Explicit 10-K fact was already PRESENT -> 8-K did NOT overwrite it! 10-K still wins!
    assert enriched.slots[0].field(
        companyfacts.SecCanonicalMetric.GAAP_DILUTED_EPS
    ).value == Decimal("1.95")


def test_annual_minus_9m_eps_remains_prohibited() -> None:
    accn_q3 = f"{CIK}-25-000238"
    accn_10k = f"{CIK}-26-000060"
    facts = (
        companyfacts.SecCompanyFactsFact(
            "EarningsPerShareDiluted",
            "USD/shares",
            Decimal("5.73"),
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
            "EarningsPerShareDiluted",
            "USD/shares",
            Decimal("7.80"),
            date(2025, 2, 1),
            date(2026, 1, 31),
            2026,
            "FY",
            "10-K",
            date(2026, 3, 2),
            accn_10k,
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
    }
    slots = companyfacts.project_sec_companyfacts_quarters(
        facts, filings, "obs_subtraction_prohibited", requested_periods=((2026, 4),)
    )
    # Subtraction 7.80 - 5.73 = 2.07 is STRICTLY PROHIBITED; remains MISSING
    field = slots[0].field(companyfacts.SecCanonicalMetric.GAAP_DILUTED_EPS)
    assert field.status is companyfacts.SecCanonicalFieldStatus.MISSING
    assert field.value is None


def test_fetch_sec_canonical_quarters_with_enrich_8k_q4_eps(monkeypatch, tmp_path) -> None:
    accn_q3 = f"{CIK}-25-000238"
    accn_10k = f"{CIK}-26-000060"
    accn_8k = f"{CIK}-26-000056"
    base_8k = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn_8k.replace('-', '')}"

    root_rows = [
        {
            "accessionNumber": accn_8k,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-02-25",
            "acceptanceDateTime": "2026-02-25T21:03:12.000Z",
            "primaryDocument": "crm-20260225.htm",
            "items": "2.02,9.01",
        },
        {
            "accessionNumber": accn_10k,
            "form": "10-K",
            "filingDate": "2026-03-02",
            "reportDate": "2026-01-31",
            "acceptanceDateTime": "2026-03-02T21:00:00.000Z",
            "primaryDocument": "crm-10k.htm",
            "items": "",
        },
        {
            "accessionNumber": accn_q3,
            "form": "10-Q",
            "filingDate": "2025-12-04",
            "reportDate": "2025-10-31",
            "acceptanceDateTime": "2025-12-04T21:00:00.000Z",
            "primaryDocument": "crm-10q.htm",
            "items": "",
        },
    ]

    companyfacts_payload = {
        "cik": int(CIK),
        "entityName": "Salesforce, Inc.",
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "label": "Revenue",
                    "units": {
                        "USD": [
                            {
                                "val": 30324000000,
                                "accn": accn_q3,
                                "form": "10-Q",
                                "fy": 2026,
                                "fp": "Q3",
                                "start": "2025-02-01",
                                "end": "2025-10-31",
                                "filed": "2025-12-04",
                            },
                            {
                                "val": 41525000000,
                                "accn": accn_10k,
                                "form": "10-K",
                                "fy": 2026,
                                "fp": "FY",
                                "start": "2025-02-01",
                                "end": "2026-01-31",
                                "filed": "2026-03-02",
                            },
                        ]
                    },
                },
                "EarningsPerShareDiluted": {
                    "label": "Earnings Per Share, Diluted",
                    "units": {
                        "USD/shares": [
                            {
                                "val": Decimal("5.73"),
                                "accn": accn_q3,
                                "form": "10-Q",
                                "fy": 2026,
                                "fp": "Q3",
                                "start": "2025-02-01",
                                "end": "2025-10-31",
                                "filed": "2025-12-04",
                            },
                            {
                                "val": Decimal("7.80"),
                                "accn": accn_10k,
                                "form": "10-K",
                                "fy": 2026,
                                "fp": "FY",
                                "start": "2025-02-01",
                                "end": "2026-01-31",
                                "filed": "2026-03-02",
                            },
                        ]
                    },
                },
            }
        },
    }

    bodies = {
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json": json.dumps(
            companyfacts_payload,
            default=lambda value: json.loads(str(value)) if isinstance(value, Decimal) else value,
        ).encode(),
        f"{base_8k}/{accn_8k}-index.htm": _index_html(accn_8k),
        f"{base_8k}/ex-991.htm": _exhibit_html(period_str="January 31, 2026", diluted_eps="2.07"),
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, root_rows, bodies)

    # 1. Default (enrich_8k_q4_eps=False): Q4 EPS remains MISSING
    res_default = companyfacts.fetch_sec_canonical_quarters(
        "CRM", client, store, utc_now=lambda: AT, count=1
    )
    assert res_default.coverage_complete is True
    slot_default = res_default.slots[0]
    assert (slot_default.fiscal_year, slot_default.fiscal_quarter) == (2026, 4)
    assert slot_default.field(SecCanonicalMetric.REVENUE).status is SecCanonicalFieldStatus.PRESENT
    assert (
        slot_default.field(SecCanonicalMetric.GAAP_DILUTED_EPS).status
        is SecCanonicalFieldStatus.MISSING
    )

    # 2. Opt-in (enrich_8k_q4_eps=True): Q4 EPS is enriched with direct 8-K value 2.07
    res_enriched = companyfacts.fetch_sec_canonical_quarters(
        "CRM", client, store, utc_now=lambda: AT, count=1, enrich_8k_q4_eps=True
    )
    assert res_enriched.coverage_complete is True
    slot_enriched = res_enriched.slots[0]
    eps_field = slot_enriched.field(SecCanonicalMetric.GAAP_DILUTED_EPS)
    assert eps_field.status is SecCanonicalFieldStatus.PRESENT
    assert eps_field.value == Decimal("2.07")
    assert eps_field.unit == "USD/shares"
    assert len(eps_field.evidence) == 1
    assert isinstance(eps_field.evidence[0], Sec8KReleaseEvidence)
    evidence: Sec8KReleaseEvidence = eps_field.evidence[0]
    assert evidence.value == Decimal("2.07")
    assert evidence.period_end == date(2026, 1, 31)
    assert evidence.accession_number == accn_8k
    assert evidence.form == "8-K"
    assert evidence.exhibit_url == f"{base_8k}/ex-991.htm"


def test_8k_submissions_observation_ids(monkeypatch, tmp_path) -> None:
    accn = f"{CIK}-26-000056"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn.replace('-', '')}"
    hist_name = f"CIK{CIK}-submissions-001.json"
    rows_hist = [
        {
            "accessionNumber": accn,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-01-31",
            "acceptanceDateTime": "2026-02-25T21:00:00.000Z",
            "primaryDocument": "crm-8k.htm",
            "items": "2.02",
        }
    ]
    columns = (
        "accessionNumber",
        "form",
        "filingDate",
        "reportDate",
        "acceptanceDateTime",
        "primaryDocument",
        "items",
    )
    hist_payload = {name: [r.get(name) for r in rows_hist] for name in columns}
    files = [{"name": hist_name, "filingFrom": "2026-01-01", "filingTo": "2026-02-28"}]
    bodies = {
        f"https://data.sec.gov/submissions/{hist_name}": json.dumps(hist_payload).encode(),
        f"{base}/{accn}-index.htm": _index_html(accn),
        f"{base}/ex-991.htm": _exhibit_html(period_str="January 31, 2026", diluted_eps="2.07"),
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, [], bodies, files=files)
    res = fetch_sec_8k_quarterly_eps(
        "CRM", client, store, target_quarters=((2026, 4, date(2026, 1, 31)),), utc_now=lambda: AT
    )
    assert len(res.submissions_observation_ids) == 2
    assert isinstance(res.submissions_observation_ids, tuple)
    assert res.submissions_observation_ids[0] != res.submissions_observation_ids[1]

    slot = SecCanonicalQuarterSlot(
        fiscal_year=2026,
        fiscal_quarter=4,
        fields=(
            SecCanonicalQuarterField(
                metric=SecCanonicalMetric.REVENUE,
                status=SecCanonicalFieldStatus.PRESENT,
                value=Decimal(11000),
                unit="USD",
                evidence=(
                    companyfacts.SecCanonicalFactEvidence(
                        native_tag="Revenue",
                        unit="USD",
                        value=Decimal(11000),
                        period_start=date(2025, 2, 1),
                        period_end=date(2026, 1, 31),
                        fiscal_year_focus=2026,
                        fiscal_period_focus="FY",
                        frame=None,
                        accession_number=accn,
                        form="10-K",
                        accepted_at=AT,
                        companyfacts_observation_id="orig_canonical_obs",
                    ),
                ),
            ),
            SecCanonicalQuarterField(
                metric=SecCanonicalMetric.GAAP_DILUTED_EPS,
                status=SecCanonicalFieldStatus.MISSING,
                value=None,
                unit=None,
                evidence=(),
            ),
        ),
    )
    eligibility = SecQuarterlyEligibility(
        SecCompanyEligibilityStatus.SEC_COMPANY,
        "CRM",
        CIK,
        "operating",
        "map_obs",
        "sub_obs",
        ("0001108524-26-000060",),
        "operating entity matches",
    )
    can_res = SecCanonicalQuarterlyResult(
        "CRM",
        CIK,
        eligibility,
        (slot,),
        "cf_id",
        ("canonical_sub_1",),
        True,
        True,
    )
    enriched = enrich_sec_canonical_quarters_with_8k_eps(can_res, res)
    assert enriched.submissions_observation_ids == (
        "canonical_sub_1",
        res.submissions_observation_ids[0],
        res.submissions_observation_ids[1],
    )


def test_8k_target_window_filtering_prevents_unneeded_fetch(monkeypatch, tmp_path) -> None:
    accn_in = f"{CIK}-26-000056"
    accn_out = f"{CIK}-25-000099"
    base_in = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn_in.replace('-', '')}"
    rows = [
        {
            "accessionNumber": accn_in,
            "form": "8-K",
            "filingDate": "2026-02-15",
            "reportDate": "2026-01-31",
            "acceptanceDateTime": "2026-02-15T21:00:00.000Z",
            "primaryDocument": "crm-8k.htm",
            "items": "2.02",
        },
        {
            "accessionNumber": accn_out,
            "form": "8-K",
            "filingDate": "2025-06-15",
            "reportDate": "2025-04-30",
            "acceptanceDateTime": "2025-06-15T21:00:00.000Z",
            "primaryDocument": "crm-8k-old.htm",
            "items": "2.02",
        },
    ]
    bodies = {
        f"{base_in}/{accn_in}-index.htm": _index_html(accn_in),
        f"{base_in}/ex-991.htm": _exhibit_html(period_str="January 31, 2026", diluted_eps="2.07"),
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows, bodies)
    res = fetch_sec_8k_quarterly_eps(
        "CRM", client, store, target_quarters=((2026, 4, date(2026, 1, 31)),), utc_now=lambda: AT
    )
    assert res.items[0].status is Sec8KEpsStatus.PRESENT
    assert res.items[0].value == Decimal("2.07")


def test_8k_missing_history_filing_range_raises_coverage_error(monkeypatch, tmp_path) -> None:
    hist_name = f"CIK{CIK}-submissions-001.json"
    files = [{"name": hist_name}]
    store, client = _setup_8k_api(monkeypatch, tmp_path, [], files=files)
    with pytest.raises(CoverageError, match="cannot be range-qualified"):
        fetch_sec_8k_quarterly_eps(
            "CRM",
            client,
            store,
            target_quarters=((2026, 4, date(2026, 1, 31)),),
            utc_now=lambda: AT,
        )


def test_8k_within_exhibit_ambiguity_preserves_candidate_provenance(monkeypatch, tmp_path) -> None:
    assert issubclass(Sec8KEpsAmbiguousError, SchemaMismatchError)

    accn = f"{CIK}-26-000056"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn.replace('-', '')}"
    rows = [
        {
            "accessionNumber": accn,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-01-31",
            "acceptanceDateTime": "2026-02-25T21:00:00.000Z",
            "primaryDocument": "crm-8k.htm",
            "items": "2.02",
        }
    ]
    ambiguous_exhibit = b"""
    <html><body>
    <table>
      <tr><th>Three Months Ended January 31, 2026</th></tr>
      <tr><td>Diluted net income per share</td><td>$1.50</td></tr>
    </table>
    <table>
      <tr><th>Three Months Ended January 31, 2026</th></tr>
      <tr><td>Diluted earnings per share</td><td>$1.60</td></tr>
    </table>
    </body></html>
    """

    bodies = {
        f"{base}/{accn}-index.htm": _index_html(accn),
        f"{base}/ex-991.htm": ambiguous_exhibit,
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows, bodies)
    res = fetch_sec_8k_quarterly_eps(
        "CRM", client, store, target_quarters=((2026, 4, date(2026, 1, 31)),), utc_now=lambda: AT
    )
    assert len(res.items) == 1
    item = res.items[0]
    assert item.status is Sec8KEpsStatus.AMBIGUOUS
    assert item.accession_number == accn
    assert len(item.alternatives) == 2
    for alt in item.alternatives:
        assert alt.status is Sec8KEpsStatus.PRESENT
        assert alt.accession_number == accn
        assert alt.form == "8-K"
        assert alt.exhibit_url == f"{base}/ex-991.htm"
        assert alt.index_observation_id is not None
        assert alt.exhibit_observation_id is not None
    assert {alt.value for alt in item.alternatives} == {Decimal("1.50"), Decimal("1.60")}


def test_enrich_8k_preserves_existing_ambiguous_and_incomplete_fields() -> None:
    slot_ambig = SecCanonicalQuarterSlot(
        fiscal_year=2026,
        fiscal_quarter=4,
        fields=(
            SecCanonicalQuarterField(
                metric=SecCanonicalMetric.REVENUE,
                status=SecCanonicalFieldStatus.PRESENT,
                value=Decimal(11000),
                unit="USD",
                evidence=(),
            ),
            SecCanonicalQuarterField(
                metric=SecCanonicalMetric.GAAP_DILUTED_EPS,
                status=SecCanonicalFieldStatus.AMBIGUOUS,
                value=None,
                unit=None,
                evidence=(),
            ),
        ),
    )
    eligibility = SecQuarterlyEligibility(
        SecCompanyEligibilityStatus.SEC_COMPANY,
        "CRM",
        CIK,
        "operating",
        "map_obs",
        "sub_obs",
        ("0001108524-26-000060",),
        "operating entity matches",
    )
    can_res = SecCanonicalQuarterlyResult(
        "CRM",
        CIK,
        eligibility,
        (slot_ambig,),
        "cf_id",
        ("sub_1",),
        True,
        True,
    )
    item_present = Sec8KQuarterlyEpsItem(
        fiscal_year=2026,
        fiscal_quarter=4,
        period_end=date(2026, 1, 31),
        status=Sec8KEpsStatus.PRESENT,
        value=Decimal("2.07"),
        unit="USD/shares",
        native_label="Diluted net income per share",
        accession_number="0001108524-26-000056",
        form="8-K",
        filing_date=date(2026, 2, 25),
        accepted_at=AT,
        filing_url=f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/000110852426000056/0001108524-26-000056-index.htm",
        index_url=f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/000110852426000056/0001108524-26-000056-index.htm",
        exhibit_url=f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/000110852426000056/ex-991.htm",
        index_observation_id="idx_obs",
        exhibit_observation_id="ex_obs",
    )
    eps_res = Sec8KQuarterlyEpsResult(
        ticker="CRM",
        cik=CIK,
        items=(item_present,),
        mapping_observation_id="map_id",
        submissions_observation_ids=("sub_2",),
        coverage_complete=True,
    )
    enriched = enrich_sec_canonical_quarters_with_8k_eps(can_res, eps_res)
    assert (
        enriched.slots[0].field(SecCanonicalMetric.GAAP_DILUTED_EPS).status
        is SecCanonicalFieldStatus.AMBIGUOUS
    )

    slot_incomplete = replace(
        slot_ambig,
        fields=(
            slot_ambig.fields[0],
            replace(slot_ambig.fields[1], status=SecCanonicalFieldStatus.COVERAGE_INCOMPLETE),
        ),
    )
    can_res_inc = replace(can_res, slots=(slot_incomplete,))
    enriched_inc = enrich_sec_canonical_quarters_with_8k_eps(can_res_inc, eps_res)
    assert (
        enriched_inc.slots[0].field(SecCanonicalMetric.GAAP_DILUTED_EPS).status
        is SecCanonicalFieldStatus.COVERAGE_INCOMPLETE
    )


def test_enrich_8k_coverage_incomplete_raises_coverage_error() -> None:
    slot = SecCanonicalQuarterSlot(
        fiscal_year=2026,
        fiscal_quarter=4,
        fields=(
            SecCanonicalQuarterField(
                metric=SecCanonicalMetric.GAAP_DILUTED_EPS,
                status=SecCanonicalFieldStatus.MISSING,
                value=None,
                unit=None,
                evidence=(),
            ),
        ),
    )
    eligibility = SecQuarterlyEligibility(
        SecCompanyEligibilityStatus.SEC_COMPANY,
        "CRM",
        CIK,
        "operating",
        "map_obs",
        "sub_obs",
        ("0001108524-26-000060",),
        "operating entity matches",
    )
    can_res = SecCanonicalQuarterlyResult(
        "CRM",
        CIK,
        eligibility,
        (slot,),
        "cf_id",
        ("sub_1",),
        True,
        True,
    )
    eps_res = Sec8KQuarterlyEpsResult(
        ticker="CRM",
        cik=CIK,
        items=(),
        mapping_observation_id="map_id",
        submissions_observation_ids=("sub_2",),
        coverage_complete=False,
    )
    with pytest.raises(CoverageError, match="incomplete coverage"):
        enrich_sec_canonical_quarters_with_8k_eps(can_res, eps_res)


def test_enrich_8k_ambiguous_result_populates_alternative_release_evidence() -> None:
    sec_base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/000110852426000056"
    alt1 = Sec8KQuarterlyEpsItem(
        fiscal_year=2026,
        fiscal_quarter=4,
        period_end=date(2026, 1, 31),
        status=Sec8KEpsStatus.PRESENT,
        value=Decimal("1.50"),
        unit="USD/shares",
        native_label="Diluted EPS table 1",
        accession_number="0001108524-26-000056",
        form="8-K",
        filing_date=date(2026, 2, 25),
        accepted_at=AT,
        filing_url=f"{sec_base}/0001108524-26-000056-index.htm",
        index_url=f"{sec_base}/0001108524-26-000056-index.htm",
        exhibit_url=f"{sec_base}/ex-991.htm",
        index_observation_id="idx_obs",
        exhibit_observation_id="ex_obs_1",
    )
    alt2 = Sec8KQuarterlyEpsItem(
        fiscal_year=2026,
        fiscal_quarter=4,
        period_end=date(2026, 1, 31),
        status=Sec8KEpsStatus.PRESENT,
        value=Decimal("1.60"),
        unit="USD/shares",
        native_label="Diluted EPS table 2",
        accession_number="0001108524-26-000056",
        form="8-K",
        filing_date=date(2026, 2, 25),
        accepted_at=AT,
        filing_url=f"{sec_base}/0001108524-26-000056-index.htm",
        index_url=f"{sec_base}/0001108524-26-000056-index.htm",
        exhibit_url=f"{sec_base}/ex-991.htm",
        index_observation_id="idx_obs",
        exhibit_observation_id="ex_obs_2",
    )
    item_ambig = Sec8KQuarterlyEpsItem(
        fiscal_year=2026,
        fiscal_quarter=4,
        period_end=date(2026, 1, 31),
        status=Sec8KEpsStatus.AMBIGUOUS,
        accession_number="0001108524-26-000056",
        form="8-K",
        filing_date=date(2026, 2, 25),
        accepted_at=AT,
        filing_url=f"{sec_base}/0001108524-26-000056-index.htm",
        index_url=f"{sec_base}/0001108524-26-000056-index.htm",
        exhibit_url=f"{sec_base}/ex-991.htm",
        index_observation_id="idx_obs",
        exhibit_observation_id="ex_obs_1",
        alternatives=(alt1, alt2),
        reason="conflicting candidates",
    )
    eps_res = Sec8KQuarterlyEpsResult(
        ticker="CRM",
        cik=CIK,
        items=(item_ambig,),
        mapping_observation_id="map_id",
        submissions_observation_ids=("sub_2",),
        coverage_complete=True,
    )
    slot_missing = SecCanonicalQuarterSlot(
        fiscal_year=2026,
        fiscal_quarter=4,
        fields=(
            SecCanonicalQuarterField(
                metric=SecCanonicalMetric.REVENUE,
                status=SecCanonicalFieldStatus.PRESENT,
                value=Decimal(11000),
                unit="USD",
                evidence=(
                    companyfacts.SecCanonicalFactEvidence(
                        native_tag="Revenue",
                        unit="USD",
                        value=Decimal(11000),
                        period_start=date(2025, 2, 1),
                        period_end=date(2026, 1, 31),
                        fiscal_year_focus=2026,
                        fiscal_period_focus="FY",
                        frame=None,
                        accession_number="0001108524-26-000060",
                        form="10-K",
                        accepted_at=AT,
                        companyfacts_observation_id="cf_obs",
                    ),
                ),
            ),
            SecCanonicalQuarterField(
                metric=SecCanonicalMetric.GAAP_DILUTED_EPS,
                status=SecCanonicalFieldStatus.MISSING,
                value=None,
                unit=None,
                evidence=(),
            ),
        ),
    )
    eligibility = SecQuarterlyEligibility(
        SecCompanyEligibilityStatus.SEC_COMPANY,
        "CRM",
        CIK,
        "operating",
        "map_obs",
        "sub_obs",
        ("0001108524-26-000060",),
        "operating entity matches",
    )
    can_res = SecCanonicalQuarterlyResult(
        "CRM",
        CIK,
        eligibility,
        (slot_missing,),
        "cf_id",
        ("sub_1",),
        True,
        True,
    )
    enriched = enrich_sec_canonical_quarters_with_8k_eps(can_res, eps_res)
    eps_field = enriched.slots[0].field(SecCanonicalMetric.GAAP_DILUTED_EPS)
    assert eps_field.status is SecCanonicalFieldStatus.AMBIGUOUS
    assert len(eps_field.evidence) == 2
    assert isinstance(eps_field.evidence[0], Sec8KReleaseEvidence)
    assert isinstance(eps_field.evidence[1], Sec8KReleaseEvidence)
    assert eps_field.evidence[0].value == Decimal("1.50")
    assert eps_field.evidence[1].value == Decimal("1.60")
    assert eps_field.evidence[0].fiscal_year_focus == 2026
    assert eps_field.evidence[0].fiscal_period_focus == "Q4"


def test_8k_index_observation_retained_even_without_exhibit(monkeypatch, tmp_path) -> None:
    accn = f"{CIK}-26-000056"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn.replace('-', '')}"
    rows = [
        {
            "accessionNumber": accn,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-01-31",
            "acceptanceDateTime": "2026-02-25T21:00:00.000Z",
            "primaryDocument": "crm-8k.htm",
            "items": "2.02",
        }
    ]
    index_no_ex99 = f"""
    <html><body>
    <h3>SEC Accession No. {accn}</h3>
    <table>
      <tr><td>1</td><td>8-K</td><td><a href="/Archives/edgar/data/{int(CIK)}/{accn.replace("-", "")}/crm.htm">crm.htm</a></td><td>8-K</td></tr>
      <tr><td>2</td><td>EX-10.1</td><td><a href="/Archives/edgar/data/{int(CIK)}/{accn.replace("-", "")}/ex-101.htm">ex-101.htm</a></td><td>EX-10.1</td></tr>
    </table>
    </body></html>
    """.encode()
    bodies = {
        f"{base}/{accn}-index.htm": index_no_ex99,
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows, bodies)
    res = fetch_sec_8k_quarterly_eps(
        "CRM", client, store, target_quarters=((2026, 4, date(2026, 1, 31)),), utc_now=lambda: AT
    )
    assert res.items[0].status is Sec8KEpsStatus.MISSING

    # Verify that the index was observed and retained in SnapshotStore
    index_manifests = list((store.root / "sec" / "edgar_8k_index").glob("**/manifest.json"))
    assert len(index_manifests) == 1
    manifest_data = json.loads(index_manifests[0].read_text(encoding="utf-8"))
    assert manifest_data["endpoint"] == "edgar_8k_index"
    assert manifest_data["canonical_request"]["parameters"] == {"cik": CIK, "accession": accn}


def test_enrich_revenue_evidence_absent_does_not_enrich_eps_even_if_other_metric_has_evidence() -> (
    None
):
    sec_base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/000110852426000056"
    item_present = Sec8KQuarterlyEpsItem(
        fiscal_year=2026,
        fiscal_quarter=4,
        period_end=date(2026, 1, 31),
        status=Sec8KEpsStatus.PRESENT,
        value=Decimal("2.07"),
        unit="USD/shares",
        native_label="Diluted net income per share",
        accession_number="0001108524-26-000056",
        form="8-K",
        filing_date=date(2026, 2, 25),
        accepted_at=AT,
        filing_url=f"{sec_base}/0001108524-26-000056-index.htm",
        index_url=f"{sec_base}/0001108524-26-000056-index.htm",
        exhibit_url=f"{sec_base}/ex-991.htm",
        index_observation_id="idx_obs",
        exhibit_observation_id="ex_obs",
    )
    eps_res = Sec8KQuarterlyEpsResult(
        ticker="CRM",
        cik=CIK,
        items=(item_present,),
        mapping_observation_id="map_id",
        submissions_observation_ids=("sub_2",),
        coverage_complete=True,
    )
    slot = SecCanonicalQuarterSlot(
        fiscal_year=2026,
        fiscal_quarter=4,
        fields=(
            SecCanonicalQuarterField(
                SecCanonicalMetric.REVENUE,
                SecCanonicalFieldStatus.MISSING,
                None,
                None,
                (),
            ),
            SecCanonicalQuarterField(
                SecCanonicalMetric.GROSS_PROFIT,
                SecCanonicalFieldStatus.PRESENT,
                Decimal(8000),
                "USD",
                (
                    companyfacts.SecCanonicalFactEvidence(
                        native_tag="GrossProfit",
                        unit="USD",
                        value=Decimal(8000),
                        period_start=date(2025, 2, 1),
                        period_end=date(2026, 1, 31),
                        fiscal_year_focus=2026,
                        fiscal_period_focus="FY",
                        frame=None,
                        accession_number="0001108524-26-000060",
                        form="10-K",
                        accepted_at=AT,
                        companyfacts_observation_id="cf_obs",
                    ),
                ),
            ),
            SecCanonicalQuarterField(
                SecCanonicalMetric.GAAP_DILUTED_EPS,
                SecCanonicalFieldStatus.MISSING,
                None,
                None,
                (),
            ),
        ),
    )
    eligibility = SecQuarterlyEligibility(
        SecCompanyEligibilityStatus.SEC_COMPANY,
        "CRM",
        CIK,
        "operating",
        "map_obs",
        "sub_obs",
        ("0001108524-26-000060",),
        "operating entity matches",
    )
    can_res = SecCanonicalQuarterlyResult(
        "CRM",
        CIK,
        eligibility,
        (slot,),
        "cf_id",
        ("sub_1",),
        True,
        True,
    )
    enriched = enrich_sec_canonical_quarters_with_8k_eps(can_res, eps_res)
    eps_field = enriched.slots[0].field(SecCanonicalMetric.GAAP_DILUTED_EPS)
    assert eps_field.status is SecCanonicalFieldStatus.MISSING
    assert eps_field.value is None
    assert eps_field.evidence == ()


def test_enrich_ambiguous_item_validation_rejects_invalid_alternatives() -> None:
    sec_base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/000110852426000056"
    alt_valid = Sec8KQuarterlyEpsItem(
        fiscal_year=2026,
        fiscal_quarter=4,
        period_end=date(2026, 1, 31),
        status=Sec8KEpsStatus.PRESENT,
        value=Decimal("1.50"),
        unit="USD/shares",
        native_label="Diluted EPS table 1",
        accession_number="0001108524-26-000056",
        form="8-K",
        filing_date=date(2026, 2, 25),
        accepted_at=AT,
        filing_url=f"{sec_base}/0001108524-26-000056-index.htm",
        index_url=f"{sec_base}/0001108524-26-000056-index.htm",
        exhibit_url=f"{sec_base}/ex-991.htm",
        index_observation_id="idx_obs",
        exhibit_observation_id="ex_obs_1",
    )
    base_ambig = Sec8KQuarterlyEpsItem(
        fiscal_year=2026,
        fiscal_quarter=4,
        period_end=date(2026, 1, 31),
        status=Sec8KEpsStatus.AMBIGUOUS,
        alternatives=(alt_valid,),
        reason="ambiguity",
    )
    eligibility = SecQuarterlyEligibility(
        SecCompanyEligibilityStatus.SEC_COMPANY,
        "CRM",
        CIK,
        "operating",
        "map_obs",
        "sub_obs",
        ("0001108524-26-000060",),
        "operating entity matches",
    )
    can_res = SecCanonicalQuarterlyResult(
        "CRM",
        CIK,
        eligibility,
        (),
        "cf_id",
        ("sub_1",),
        True,
        True,
    )

    item_empty = replace(base_ambig, alternatives=())
    eps_res_empty = Sec8KQuarterlyEpsResult("CRM", CIK, (item_empty,), "m", ("s",), True)
    with pytest.raises(ValueError, match="AMBIGUOUS item must have non-empty alternatives"):
        enrich_sec_canonical_quarters_with_8k_eps(can_res, eps_res_empty)

    alt_missing = replace(alt_valid, status=Sec8KEpsStatus.MISSING)
    item_non_present = replace(base_ambig, alternatives=(alt_missing,))
    eps_res_non_present = Sec8KQuarterlyEpsResult(
        "CRM", CIK, (item_non_present,), "m", ("s",), True
    )
    with pytest.raises(ValueError, match="AMBIGUOUS alternative must have status PRESENT"):
        enrich_sec_canonical_quarters_with_8k_eps(can_res, eps_res_non_present)

    alt_mismatched = replace(alt_valid, fiscal_year=2025)
    item_mismatched = replace(base_ambig, alternatives=(alt_mismatched,))
    eps_res_mismatched = Sec8KQuarterlyEpsResult("CRM", CIK, (item_mismatched,), "m", ("s",), True)
    with pytest.raises(ValueError, match="AMBIGUOUS alternative period mismatch"):
        enrich_sec_canonical_quarters_with_8k_eps(can_res, eps_res_mismatched)


def test_enrich_rejects_duplicate_8k_result_item_keys() -> None:
    sec_base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/000110852426000056"
    item1 = Sec8KQuarterlyEpsItem(
        fiscal_year=2026,
        fiscal_quarter=4,
        period_end=date(2026, 1, 31),
        status=Sec8KEpsStatus.PRESENT,
        value=Decimal("2.07"),
        unit="USD/shares",
        native_label="Diluted net income per share",
        accession_number="0001108524-26-000056",
        form="8-K",
        filing_date=date(2026, 2, 25),
        accepted_at=AT,
        filing_url=f"{sec_base}/0001108524-26-000056-index.htm",
        index_url=f"{sec_base}/0001108524-26-000056-index.htm",
        exhibit_url=f"{sec_base}/ex-991.htm",
        index_observation_id="idx_obs",
        exhibit_observation_id="ex_obs",
    )
    item2 = replace(item1, value=Decimal("2.08"))
    eps_res = Sec8KQuarterlyEpsResult("CRM", CIK, (item1, item2), "m", ("s",), True)
    can_res = SecCanonicalQuarterlyResult(
        "CRM",
        CIK,
        SecQuarterlyEligibility(
            SecCompanyEligibilityStatus.SEC_COMPANY, "CRM", CIK, "operating", "m", "s", (), "ok"
        ),
        (),
        "cf_id",
        ("sub_1",),
        True,
        True,
    )
    with pytest.raises(ValueError, match="duplicate 8-K EPS item for period"):
        enrich_sec_canonical_quarters_with_8k_eps(can_res, eps_res)


def test_present_provenance_validation_tightened() -> None:
    sec_base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/000110852426000056"
    item_base = Sec8KQuarterlyEpsItem(
        fiscal_year=2026,
        fiscal_quarter=4,
        period_end=date(2026, 1, 31),
        status=Sec8KEpsStatus.PRESENT,
        value=Decimal("2.07"),
        unit="USD/shares",
        native_label="Diluted net income per share",
        accession_number="0001108524-26-000056",
        form="8-K",
        filing_date=date(2026, 2, 25),
        accepted_at=AT,
        filing_url=f"{sec_base}/0001108524-26-000056-index.htm",
        index_url=f"{sec_base}/0001108524-26-000056-index.htm",
        exhibit_url=f"{sec_base}/ex-991.htm",
        index_observation_id="idx_obs",
        exhibit_observation_id="ex_obs",
    )
    can_res = SecCanonicalQuarterlyResult(
        "CRM",
        CIK,
        SecQuarterlyEligibility(
            SecCompanyEligibilityStatus.SEC_COMPANY, "CRM", CIK, "operating", "m", "s", (), "ok"
        ),
        (),
        "cf_id",
        ("sub_1",),
        True,
        True,
    )

    naive_dt = datetime(2026, 2, 25, 21, 0, 0)  # noqa: DTZ001
    eps_naive = Sec8KQuarterlyEpsResult(
        "CRM", CIK, (replace(item_base, accepted_at=naive_dt),), "m", ("s",), True
    )
    with pytest.raises(ValueError, match="timezone-aware accepted_at"):
        enrich_sec_canonical_quarters_with_8k_eps(can_res, eps_naive)

    eps_bad_url = Sec8KQuarterlyEpsResult(
        "CRM",
        CIK,
        (replace(item_base, exhibit_url="https://bad-site.org/ex-99.htm"),),
        "m",
        ("s",),
        True,
    )
    with pytest.raises(ValueError, match="SEC HTTPS allowlist"):
        enrich_sec_canonical_quarters_with_8k_eps(can_res, eps_bad_url)


def test_enrich_sec_canonical_quarters_with_zero_eps_succeeds() -> None:
    sec_base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/000110852426000056"
    item_zero = Sec8KQuarterlyEpsItem(
        fiscal_year=2026,
        fiscal_quarter=4,
        period_end=date(2026, 1, 31),
        status=Sec8KEpsStatus.PRESENT,
        value=Decimal(0),
        unit="USD/shares",
        native_label="Diluted net income per share",
        accession_number="0001108524-26-000056",
        form="8-K",
        filing_date=date(2026, 2, 25),
        accepted_at=AT,
        filing_url=f"{sec_base}/0001108524-26-000056-index.htm",
        index_url=f"{sec_base}/0001108524-26-000056-index.htm",
        exhibit_url=f"{sec_base}/ex-991.htm",
        index_observation_id="idx_obs",
        exhibit_observation_id="ex_obs",
    )
    eps_res = Sec8KQuarterlyEpsResult("CRM", CIK, (item_zero,), "m", ("s",), True)
    slot_missing = SecCanonicalQuarterSlot(
        fiscal_year=2026,
        fiscal_quarter=4,
        fields=(
            SecCanonicalQuarterField(
                SecCanonicalMetric.REVENUE,
                SecCanonicalFieldStatus.PRESENT,
                Decimal(11000),
                "USD",
                (
                    companyfacts.SecCanonicalFactEvidence(
                        native_tag="Revenue",
                        unit="USD",
                        value=Decimal(11000),
                        period_start=date(2025, 2, 1),
                        period_end=date(2026, 1, 31),
                        fiscal_year_focus=2026,
                        fiscal_period_focus="FY",
                        frame=None,
                        accession_number="0001108524-26-000060",
                        form="10-K",
                        accepted_at=AT,
                        companyfacts_observation_id="cf_obs",
                    ),
                ),
            ),
            SecCanonicalQuarterField(
                SecCanonicalMetric.GAAP_DILUTED_EPS,
                SecCanonicalFieldStatus.MISSING,
                None,
                None,
                (),
            ),
        ),
    )
    can_res = SecCanonicalQuarterlyResult(
        "CRM",
        CIK,
        SecQuarterlyEligibility(
            SecCompanyEligibilityStatus.SEC_COMPANY, "CRM", CIK, "operating", "m", "s", (), "ok"
        ),
        (slot_missing,),
        "cf_id",
        ("sub_1",),
        True,
        True,
    )
    enriched = enrich_sec_canonical_quarters_with_8k_eps(can_res, eps_res)
    eps_field = enriched.slots[0].field(SecCanonicalMetric.GAAP_DILUTED_EPS)
    assert eps_field.status is SecCanonicalFieldStatus.PRESENT
    assert eps_field.value == Decimal(0)
    assert len(eps_field.evidence) == 1
    assert eps_field.evidence[0].value == Decimal(0)


@pytest.mark.parametrize(
    "bad_value",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_enrich_rejects_non_finite_decimal_values(bad_value: Decimal) -> None:
    sec_base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/000110852426000056"
    item = Sec8KQuarterlyEpsItem(
        fiscal_year=2026,
        fiscal_quarter=4,
        period_end=date(2026, 1, 31),
        status=Sec8KEpsStatus.PRESENT,
        value=bad_value,
        unit="USD/shares",
        native_label="Diluted net income per share",
        accession_number="0001108524-26-000056",
        form="8-K",
        filing_date=date(2026, 2, 25),
        accepted_at=AT,
        filing_url=f"{sec_base}/0001108524-26-000056-index.htm",
        index_url=f"{sec_base}/0001108524-26-000056-index.htm",
        exhibit_url=f"{sec_base}/ex-991.htm",
        index_observation_id="idx_obs",
        exhibit_observation_id="ex_obs",
    )
    eps_res = Sec8KQuarterlyEpsResult("CRM", CIK, (item,), "m", ("s",), True)
    can_res = SecCanonicalQuarterlyResult(
        "CRM",
        CIK,
        SecQuarterlyEligibility(
            SecCompanyEligibilityStatus.SEC_COMPANY, "CRM", CIK, "operating", "m", "s", (), "ok"
        ),
        (),
        "cf_id",
        ("sub_1",),
        True,
        True,
    )
    with pytest.raises(ValueError, match="finite Decimal"):
        enrich_sec_canonical_quarters_with_8k_eps(can_res, eps_res)


def test_8k_multi_target_does_not_contaminate_prior_period_with_comparative_table(
    monkeypatch, tmp_path
) -> None:
    accn_2025 = f"{CIK}-25-000001"
    accn_2026 = f"{CIK}-26-000002"
    base_2025 = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn_2025.replace('-', '')}"
    base_2026 = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn_2026.replace('-', '')}"

    rows = [
        {
            "accessionNumber": accn_2025,
            "form": "8-K",
            "filingDate": "2025-03-04",
            "reportDate": "2025-01-31",
            "acceptanceDateTime": "2025-03-04T21:00:00.000Z",
            "primaryDocument": "crwd-20250304.htm",
            "items": "2.02",
        },
        {
            "accessionNumber": accn_2026,
            "form": "8-K",
            "filingDate": "2026-03-03",
            "reportDate": "2026-01-31",
            "acceptanceDateTime": "2026-03-03T21:00:00.000Z",
            "primaryDocument": "crwd-20260303.htm",
            "items": "2.02",
        },
    ]

    exhibit_2025 = _exhibit_html(period_str="January 31, 2025", diluted_eps="-0.37")
    exhibit_2026_comparative = b"""
    <html><body>
    <table>
      <tr>
        <th></th>
        <th>Three Months Ended January 31, 2026</th>
        <th>Three Months Ended January 31, 2025</th>
      </tr>
      <tr>
        <td>Diluted net income (loss) per share</td>
        <td>$0.25</td>
        <td>$(0.35)</td>
      </tr>
    </table>
    </body></html>
    """

    bodies = {
        f"{base_2025}/{accn_2025}-index.htm": _index_html(accn_2025),
        f"{base_2025}/ex-991.htm": exhibit_2025,
        f"{base_2026}/{accn_2026}-index.htm": _index_html(accn_2026),
        f"{base_2026}/ex-991.htm": exhibit_2026_comparative,
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows, bodies)

    res = earnings_8k.fetch_sec_8k_quarterly_eps(
        "CRWD",
        client,
        store,
        target_quarters=(
            (2025, 4, date(2025, 1, 31)),
            (2026, 4, date(2026, 1, 31)),
        ),
        utc_now=lambda: AT,
    )
    assert res.coverage_complete is True
    assert len(res.items) == 2

    item_2025 = res.items[0]
    assert item_2025.fiscal_year == 2025
    assert item_2025.status is earnings_8k.Sec8KEpsStatus.PRESENT
    assert item_2025.value == Decimal("-0.37")
    assert item_2025.accession_number == accn_2025

    item_2026 = res.items[1]
    assert item_2026.fiscal_year == 2026
    assert item_2026.status is earnings_8k.Sec8KEpsStatus.PRESENT
    assert item_2026.value == Decimal("0.25")
    assert item_2026.accession_number == accn_2026


def test_8k_multi_target_within_exhibit_ambiguity_in_later_filing_does_not_contaminate_prior_period(
    monkeypatch, tmp_path
) -> None:
    accn_2025 = f"{CIK}-25-000001"
    accn_2026 = f"{CIK}-26-000002"
    base_2025 = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn_2025.replace('-', '')}"
    base_2026 = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn_2026.replace('-', '')}"

    rows = [
        {
            "accessionNumber": accn_2025,
            "form": "8-K",
            "filingDate": "2025-03-04",
            "reportDate": "2025-01-31",
            "acceptanceDateTime": "2025-03-04T21:00:00.000Z",
            "primaryDocument": "crwd-20250304.htm",
            "items": "2.02",
        },
        {
            "accessionNumber": accn_2026,
            "form": "8-K",
            "filingDate": "2026-03-03",
            "reportDate": "2026-01-31",
            "acceptanceDateTime": "2026-03-03T21:00:00.000Z",
            "primaryDocument": "crwd-20260303.htm",
            "items": "2.02",
        },
    ]

    exhibit_2025 = _exhibit_html(period_str="January 31, 2025", diluted_eps="-0.37")
    exhibit_2026_ambig_2025 = b"""
    <html><body>
    <table>
      <tr>
        <th>Three Months Ended January 31, 2026</th>
      </tr>
      <tr>
        <td>Diluted net income per share</td>
        <td>$0.25</td>
      </tr>
    </table>
    <table>
      <tr>
        <th>Three Months Ended January 31, 2025</th>
      </tr>
      <tr>
        <td>Diluted net income per share</td>
        <td>$(0.35)</td>
      </tr>
      <tr>
        <td>Diluted net income per share</td>
        <td>$(0.36)</td>
      </tr>
    </table>
    </body></html>
    """

    bodies = {
        f"{base_2025}/{accn_2025}-index.htm": _index_html(accn_2025),
        f"{base_2025}/ex-991.htm": exhibit_2025,
        f"{base_2026}/{accn_2026}-index.htm": _index_html(accn_2026),
        f"{base_2026}/ex-991.htm": exhibit_2026_ambig_2025,
    }
    store, client = _setup_8k_api(monkeypatch, tmp_path, rows, bodies)

    res = earnings_8k.fetch_sec_8k_quarterly_eps(
        "CRWD",
        client,
        store,
        target_quarters=(
            (2025, 4, date(2025, 1, 31)),
            (2026, 4, date(2026, 1, 31)),
        ),
        utc_now=lambda: AT,
    )
    assert res.coverage_complete is True
    assert res.items[0].status is earnings_8k.Sec8KEpsStatus.PRESENT
    assert res.items[0].value == Decimal("-0.37")
    assert res.items[1].status is earnings_8k.Sec8KEpsStatus.PRESENT
    assert res.items[1].value == Decimal("0.25")

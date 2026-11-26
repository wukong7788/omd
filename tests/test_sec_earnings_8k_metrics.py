"""Offline synthetic tests for SEC Form 8-K Revenue, Gross Profit, EPS, batch, and event streams."""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.providers.sec._event_discovery_models import (
    SecFilingDiscoveryEvent,
    SecFilingEventKind,
)
from ohmydata.providers.sec.earnings_8k import (
    Sec8KBatchOutcomeStatus,
    Sec8KBatchTarget,
    Sec8KMetric,
    Sec8KMetricStatus,
    fetch_sec_8k_metrics_for_discovery_events,
    fetch_sec_8k_quarterly_batch,
    fetch_sec_8k_quarterly_metrics,
    parse_sec_8k_earnings_metrics,
)
from ohmydata.providers.sec.errors import PermanentProviderError, TransientProviderError
from ohmydata.providers.sec.http import SecHttpClient

CIK_A = "0001108524"
CIK_B = "0001535527"
CIK_C = "0001045810"
CIK_D = "0001652044"
AT = datetime(2026, 3, 20, tzinfo=UTC)


def _index_html(cik: str, accession: str, docs: list[tuple[str, str]] | None = None) -> bytes:
    compact = accession.replace("-", "")
    items = docs if docs is not None else [("ex-991.htm", "EX-99.1")]
    trs = []
    for idx, (doc, doc_type) in enumerate(items, start=1):
        href = f"/Archives/edgar/data/{int(cik)}/{compact}/{doc}"
        trs.append(
            f"<tr><td>{idx}</td><td>{doc_type}</td>"
            f'<td><a href="{href}">{doc}</a></td><td>{doc_type}</td></tr>'
        )
    return (
        f"<html><body><h3>SEC Accession No. {accession}</h3>"
        f"<table>{''.join(trs)}</table></body></html>"
    ).encode()


def _earnings_exhibit(
    *,
    period_header: str = "Three Months Ended January 31, 2026",
    scale_header: str = "(in millions, except per share data)",
    revenue: str = "11,201",
    gross_profit: str = "8,410",
    diluted_eps: str = "2.07",
    extra_tables: str = "",
    pre_table_context: str = "",
) -> bytes:
    return f"""
    <html><body>
      <h2>Consolidated Statements of Operations</h2>
      <p>{pre_table_context}</p>
      <table>
        <caption>{scale_header}</caption>
        <tr>
          <th></th>
          <th>{period_header}</th>
          <th>Year Ended January 31, 2026</th>
          <th>Nine Months Ended October 31, 2025</th>
        </tr>
        <tr><td>Subscription and support revenue</td><td>$10,500</td><td>$38,000</td><td>$27,500</td></tr>
        <tr><td>Professional services revenue</td><td>701</td><td>2,400</td><td>1,699</td></tr>
        <tr><td>Total revenues</td><td>${revenue}</td><td>$40,400</td><td>$29,199</td></tr>
        <tr><td>Cost of revenues</td><td>2,791</td><td>10,000</td><td>7,209</td></tr>
        <tr><td>Gross profit</td><td>${gross_profit}</td><td>$30,400</td><td>$21,990</td></tr>
        <tr><td>Gross margin %</td><td>75.1%</td><td>75.2%</td><td>75.3%</td></tr>
        <tr><td>Non-GAAP gross profit</td><td>$8,950</td><td>$32,100</td><td>$23,150</td></tr>
        <tr><td>Adjusted revenue</td><td>$11,450</td><td>$41,000</td><td>$29,550</td></tr>
        <tr><td>Diluted net income per share</td><td>${diluted_eps}</td><td>$7.40</td><td>$5.33</td></tr>
        <tr><td>Non-GAAP diluted net income per share</td><td>$3.81</td><td>$11.50</td><td>$7.69</td></tr>
      </table>
      {extra_tables}
    </body></html>
    """.encode()


def _make_submissions(cik: str, rows: list[dict[str, object]]) -> bytes:
    cols = (
        "accessionNumber",
        "form",
        "filingDate",
        "reportDate",
        "acceptanceDateTime",
        "primaryDocument",
        "items",
    )
    payload = {
        "cik": cik,
        "entityType": "operating",
        "filings": {
            "recent": {c: [r.get(c) for r in rows] for c in cols},
            "files": [],
        },
    }
    return json.dumps(payload).encode()


def _setup_client(monkeypatch, tmp_path, bodies: dict[str, bytes | Exception]):
    store = SnapshotStore(tmp_path)
    client = SecHttpClient("Synthetic Test Contact test@example.invalid", opener=object())

    class Resp:
        def __init__(self, url: str, body: bytes) -> None:
            self.url = url
            self.body = io.BytesIO(body)
            self.headers: dict[str, str] = {}

    calls: list[str] = []

    def mock_open(url: str, **kwargs):
        calls.append(url)
        val = bodies.get(url)
        if val is None:
            raise AssertionError(f"Unexpected URL: {url}")
        if isinstance(val, Exception):
            raise val
        return Resp(url, val)

    monkeypatch.setattr(client, "open", mock_open)
    return store, client, calls


@pytest.mark.parametrize(
    "scale_header,rev_raw,gp_raw,expected_rev,expected_gp,expected_scale",
    [
        (
            "(in millions, except per share data)",
            "11,201",
            "8,410",
            Decimal(11201000000),
            Decimal(8410000000),
            "in millions",
        ),
        (
            "(Dollars in thousands, except shares in thousands and per share amounts)",
            "945,250",
            "710,125",
            Decimal(945250000),
            Decimal(710125000),
            "in thousands",
        ),
        (
            "($ in billions, except per share data)",
            "11.20",
            "8.41",
            Decimal("11200000000.00"),
            Decimal("8410000000.00"),
            "in billions",
        ),
        (
            "(in exact dollars, except per share data)",
            "125,400,000",
            "90,100,000",
            Decimal(125400000),
            Decimal(90100000),
            "in dollars",
        ),
    ],
)
def test_8k_metrics_exact_decimal_scale_and_full_evidence(
    monkeypatch,
    tmp_path,
    scale_header: str,
    rev_raw: str,
    gp_raw: str,
    expected_rev: Decimal,
    expected_gp: Decimal,
    expected_scale: str,
) -> None:
    accn = f"{CIK_A}-26-000010"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK_A)}/{accn.replace('-', '')}"
    rows = [
        {
            "accessionNumber": accn,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-02-25",
            "acceptanceDateTime": "2026-02-25T21:03:12.000Z",
            "primaryDocument": "crm.htm",
            "items": "2.02,9.01",
        }
    ]
    bodies: dict[str, bytes | Exception] = {
        f"https://data.sec.gov/submissions/CIK{CIK_A}.json": _make_submissions(CIK_A, rows),
        f"{base}/{accn}-index.htm": _index_html(CIK_A, accn),
        f"{base}/ex-991.htm": _earnings_exhibit(
            scale_header=scale_header, revenue=rev_raw, gross_profit=gp_raw, diluted_eps="2.07"
        ),
    }
    store, client, calls = _setup_client(monkeypatch, tmp_path, bodies)

    # CIK-first resolution: works for delisted issuers without company_tickers.json
    res = fetch_sec_8k_quarterly_metrics(
        client,
        store,
        cik="1108524",
        target_quarters=((2026, 4, date(2026, 1, 31)),),
        utc_now=lambda: AT,
    )
    assert not any("company_tickers.json" in u for u in calls)
    assert res.cik == CIK_A
    assert res.mapping_observation_id is None
    assert len(res.items) == 1

    item = res.items[0]
    rev = item.field(Sec8KMetric.REVENUE)
    gp = item.field(Sec8KMetric.GROSS_PROFIT)
    eps = item.field(Sec8KMetric.GAAP_DILUTED_EPS)

    assert rev.status is Sec8KMetricStatus.PRESENT
    assert rev.value == expected_rev
    assert rev.unit == "USD"
    assert rev.declared_scale == expected_scale
    assert rev.native_label == "Total revenues"
    assert len(rev.evidence) == 1
    ev = rev.evidence[0]
    assert ev.accession_number == accn
    assert ev.accepted_at == datetime(2026, 2, 25, 21, 3, 12, tzinfo=UTC)
    assert ev.filing_url == f"{base}/{accn}-index.htm"
    assert ev.exhibit_url == f"{base}/ex-991.htm"
    assert len(ev.index_observation_id) == 64
    assert len(ev.exhibit_observation_id) == 64

    assert gp.status is Sec8KMetricStatus.PRESENT
    assert gp.value == expected_gp
    assert gp.unit == "USD"
    assert gp.native_label == "Gross profit"

    assert eps.status is Sec8KMetricStatus.PRESENT
    assert eps.value == Decimal("2.07")
    assert eps.unit == "USD/shares"


def test_8k_metrics_fail_closed_on_missing_or_contradictory_scale_and_non_usd() -> None:
    # 1. Missing scale header -> Revenue and Gross Profit MUST fail closed as MISSING
    no_scale = _earnings_exhibit(scale_header="", pre_table_context="")
    parsed_missing = parse_sec_8k_earnings_metrics(no_scale, expected_period_end=date(2026, 1, 31))
    assert (Sec8KMetric.REVENUE, date(2026, 1, 31)) not in parsed_missing.values
    assert (Sec8KMetric.GROSS_PROFIT, date(2026, 1, 31)) not in parsed_missing.values
    # EPS is unaffected because it is USD/shares
    assert parsed_missing.values[
        (Sec8KMetric.GAAP_DILUTED_EPS, date(2026, 1, 31))
    ].value == Decimal("2.07")

    # 2. Contradictory scale headers -> AMBIGUOUS (fail closed!)
    contradictory = _earnings_exhibit(
        pre_table_context="Amounts in millions",
        scale_header="(Dollars in thousands)",
    )
    parsed_ambig = parse_sec_8k_earnings_metrics(
        contradictory, expected_period_end=date(2026, 1, 31)
    )
    assert (Sec8KMetric.REVENUE, date(2026, 1, 31)) in parsed_ambig.ambiguities
    assert (
        "contradictory monetary scale"
        in parsed_ambig.ambiguities[(Sec8KMetric.REVENUE, date(2026, 1, 31))].reason
    )
    assert (Sec8KMetric.GROSS_PROFIT, date(2026, 1, 31)) in parsed_ambig.ambiguities

    # 3. Non-USD currency -> rejected (never mislabeled as USD!)
    eur_html = _earnings_exhibit(scale_header="(in millions of euros)")
    parsed_eur = parse_sec_8k_earnings_metrics(eur_html, expected_period_end=date(2026, 1, 31))
    assert (Sec8KMetric.REVENUE, date(2026, 1, 31)) not in parsed_eur.values
    assert (Sec8KMetric.GROSS_PROFIT, date(2026, 1, 31)) not in parsed_eur.values


def test_8k_metrics_rejects_guidance_nongaap_segments_margins_and_ytd() -> None:
    exhibit = b"""
    <html><body>
      <p>Q1 Fiscal 2027 Guidance (in millions)</p>
      <table>
        <tr><th></th><th>Three Months Ended January 31, 2026</th></tr>
        <tr><td>Total revenues</td><td>$12,000</td></tr>
        <tr><td>Gross profit</td><td>$9,000</td></tr>
      </table>
      <p>Non-GAAP Financial Measures (in millions)</p>
      <table>
        <tr><th></th><th>Three Months Ended January 31, 2026</th></tr>
        <tr><td>Total revenues</td><td>$11,500</td></tr>
        <tr><td>Gross profit</td><td>$8,800</td></tr>
      </table>
      <p>Segment Revenue Disaggregation (in millions)</p>
      <table>
        <tr><th></th><th>Three Months Ended January 31, 2026</th></tr>
        <tr><td>Total revenues</td><td>$5,000</td></tr>
      </table>
      <p>Annual and Nine-Month Summary (in millions)</p>
      <table>
        <tr>
          <th></th>
          <th>Year Ended January 31, 2026</th>
          <th>Nine Months Ended October 31, 2025</th>
          <th>Constant Currency Three Months Ended January 31, 2026</th>
        </tr>
        <tr><td>Total revenues</td><td>$40,400</td><td>$29,199</td><td>$11,100</td></tr>
        <tr><td>Gross profit</td><td>$30,400</td><td>$21,990</td><td>$8,300</td></tr>
        <tr><td>Gross margin %</td><td>75.2%</td><td>75.3%</td><td>74.8%</td></tr>
      </table>
    </body></html>
    """
    parsed = parse_sec_8k_earnings_metrics(exhibit, expected_period_end=date(2026, 1, 31))
    assert parsed.values == {}
    assert parsed.ambiguities == {}


def test_8k_metrics_conflicting_values_across_tables_marked_ambiguous(
    monkeypatch, tmp_path
) -> None:
    accn = f"{CIK_A}-26-000011"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK_A)}/{accn.replace('-', '')}"
    conflict_table = """
      <p>Summary Results (in millions)</p>
      <table>
        <tr><th></th><th>Three Months Ended January 31, 2026</th></tr>
        <tr><td>Total revenues</td><td>$11,999</td></tr>
        <tr><td>Gross profit</td><td>$8,410</td></tr>
      </table>
    """
    rows = [
        {
            "accessionNumber": accn,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-02-25",
            "acceptanceDateTime": "2026-02-25T21:03:12.000Z",
            "primaryDocument": "crm.htm",
            "items": "2.02",
        }
    ]
    bodies: dict[str, bytes | Exception] = {
        f"https://data.sec.gov/submissions/CIK{CIK_A}.json": _make_submissions(CIK_A, rows),
        f"{base}/{accn}-index.htm": _index_html(CIK_A, accn),
        f"{base}/ex-991.htm": _earnings_exhibit(extra_tables=conflict_table),
    }
    store, client, _ = _setup_client(monkeypatch, tmp_path, bodies)
    res = fetch_sec_8k_quarterly_metrics(
        client,
        store,
        cik=CIK_A,
        target_quarters=((2026, 4, date(2026, 1, 31)),),
        utc_now=lambda: AT,
    )
    item = res.items[0]
    rev = item.field(Sec8KMetric.REVENUE)
    gp = item.field(Sec8KMetric.GROSS_PROFIT)
    assert rev.status is Sec8KMetricStatus.AMBIGUOUS
    assert rev.value is None
    assert len(rev.evidence) == 2
    # Identical Gross Profit across both tables collapses cleanly to PRESENT
    assert gp.status is Sec8KMetricStatus.PRESENT
    assert gp.value == Decimal(8410000000)


def test_8k_batch_isolates_ok_empty_transient_and_permanent_failures(monkeypatch, tmp_path) -> None:
    accn_a = f"{CIK_A}-26-000020"
    base_a = f"https://www.sec.gov/Archives/edgar/data/{int(CIK_A)}/{accn_a.replace('-', '')}"
    bodies: dict[str, bytes | Exception] = {
        f"https://data.sec.gov/submissions/CIK{CIK_A}.json": _make_submissions(
            CIK_A,
            [
                {
                    "accessionNumber": accn_a,
                    "form": "8-K",
                    "filingDate": "2026-02-25",
                    "reportDate": "2026-02-25",
                    "acceptanceDateTime": "2026-02-25T21:00:00.000Z",
                    "primaryDocument": "a.htm",
                    "items": "2.02",
                }
            ],
        ),
        f"{base_a}/{accn_a}-index.htm": _index_html(CIK_A, accn_a),
        f"{base_a}/ex-991.htm": _earnings_exhibit(),
        f"https://data.sec.gov/submissions/CIK{CIK_B}.json": _make_submissions(CIK_B, []),
        f"https://data.sec.gov/submissions/CIK{CIK_C}.json": TransientProviderError(
            "SEC HTTP status 503 User-Agent=secret@example.invalid"
        ),
        f"https://data.sec.gov/submissions/CIK{CIK_D}.json": PermanentProviderError(
            "SEC HTTP status 404"
        ),
    }
    store, client, _ = _setup_client(monkeypatch, tmp_path, bodies)

    targets = (
        Sec8KBatchTarget(cik=CIK_A, target_quarters=((2026, 4, date(2026, 1, 31)),)),
        Sec8KBatchTarget(cik=CIK_B, target_quarters=((2026, 4, date(2026, 1, 31)),)),
        Sec8KBatchTarget(cik=CIK_C, target_quarters=((2026, 4, date(2026, 1, 31)),)),
        Sec8KBatchTarget(cik=CIK_D, target_quarters=((2026, 4, date(2026, 1, 31)),)),
    )
    batch = fetch_sec_8k_quarterly_batch(targets, client, store, utc_now=lambda: AT)

    assert batch.ok_count == 1
    assert batch.empty_count == 1
    assert batch.transient_failure_count == 1
    assert batch.permanent_failure_count == 1
    assert [r.status for r in batch.receipts] == [
        Sec8KBatchOutcomeStatus.OK,
        Sec8KBatchOutcomeStatus.EMPTY,
        Sec8KBatchOutcomeStatus.TRANSIENT_FAILURE,
        Sec8KBatchOutcomeStatus.PERMANENT_FAILURE,
    ]
    assert batch.receipts[0].result is not None
    assert batch.receipts[0].result.items[0].field(Sec8KMetric.REVENUE).value == Decimal(
        11201000000
    )
    assert batch.receipts[2].error_type == "TransientProviderError"
    assert "secret@example.invalid" not in (batch.receipts[2].error_message or "")
    assert "[REDACTED]" in (batch.receipts[2].error_message or "")


def test_8k_discovery_event_stream_integration(monkeypatch, tmp_path) -> None:
    accn_ok = f"{CIK_A}-26-000030"
    accn_err = f"{CIK_B}-26-000031"
    accn_non202 = f"{CIK_A}-26-000032"
    accn_future = f"{CIK_A}-26-000033"
    base_ok = f"https://www.sec.gov/Archives/edgar/data/{int(CIK_A)}/{accn_ok.replace('-', '')}"
    base_err = f"https://www.sec.gov/Archives/edgar/data/{int(CIK_B)}/{accn_err.replace('-', '')}"

    # Exhibit includes both current Q4 (Jan 31, 2026) and prior-year comparative (Jan 31, 2025)
    comparative_exhibit = b"""
    <html><body>
      <table>
        <caption>(in millions, except per share data)</caption>
        <tr>
          <th></th>
          <th>Fourth Quarter Ended January 31, 2026</th>
          <th>Fourth Quarter Ended January 31, 2025</th>
        </tr>
        <tr><td>Total revenues</td><td>$11,201</td><td>$9,993</td></tr>
        <tr><td>Gross profit</td><td>$8,410</td><td>$7,450</td></tr>
        <tr><td>Diluted net income per share</td><td>$2.07</td><td>$1.75</td></tr>
      </table>
    </body></html>
    """

    bodies: dict[str, bytes | Exception] = {
        f"{base_ok}/{accn_ok}-index.htm": _index_html(CIK_A, accn_ok),
        f"{base_ok}/ex-991.htm": comparative_exhibit,
        f"{base_err}/{accn_err}-index.htm": TransientProviderError("SEC HTTP status 503"),
    }
    store, client, calls = _setup_client(monkeypatch, tmp_path, bodies)

    def _evt(
        key: str,
        cik: str,
        accn: str,
        form: str,
        kind: SecFilingEventKind,
        accepted: datetime,
        items: str | None,
    ) -> SecFilingDiscoveryEvent:
        return SecFilingDiscoveryEvent(
            key=key,
            cik=str(int(cik)),
            accession=accn,
            kind=kind,
            form=form,
            filing_date="2026-02-25",
            report_date="2026-02-25",
            acceptance_native=accepted.isoformat().replace("+00:00", "Z"),
            acceptance_at=accepted,
            primary_document="doc.htm",
            items=items,
            metadata_digest="0" * 64,
            source_observation_identities=("a" * 64,),
        )

    events = (
        _evt(
            "evt-10q",
            CIK_A,
            f"{CIK_A}-26-000001",
            "10-Q",
            SecFilingEventKind.SEC_FILING_ACCEPTED,
            datetime(2026, 2, 20, tzinfo=UTC),
            None,
        ),
        _evt(
            "evt-8k-ok",
            CIK_A,
            accn_ok,
            "8-K",
            SecFilingEventKind.SEC_8K_ITEM_2_02,
            datetime(2026, 2, 25, 21, 0, tzinfo=UTC),
            "2.02,9.01",
        ),
        _evt(
            "evt-8ka-transient",
            CIK_B,
            accn_err,
            "8-K/A",
            SecFilingEventKind.SEC_8K_ITEM_2_02,
            datetime(2026, 2, 26, 21, 0, tzinfo=UTC),
            "2.02",
        ),
        _evt(
            "evt-8k-502",
            CIK_A,
            accn_non202,
            "8-K",
            SecFilingEventKind.SEC_FILING_ACCEPTED,
            datetime(2026, 2, 27, 21, 0, tzinfo=UTC),
            "5.02",
        ),
        _evt(
            "evt-8k-future",
            CIK_A,
            accn_future,
            "8-K",
            SecFilingEventKind.SEC_8K_ITEM_2_02,
            datetime(2026, 3, 5, 21, 0, tzinfo=UTC),
            "2.02",
        ),
    )

    result = fetch_sec_8k_metrics_for_discovery_events(
        events,
        client,
        store,
        acceptance_upper=datetime(2026, 3, 1, tzinfo=UTC),
        utc_now=lambda: AT,
    )
    # 10-Q and post-cutoff 8-K are skipped; 5.02 makes zero HTTP calls
    assert len(result.receipts) == 3
    assert result.ok_count == 1
    assert result.transient_failure_count == 1
    assert result.empty_count == 1
    assert not any("submissions" in u for u in calls)

    ok_rec = result.receipts[0]
    assert ok_rec.status is Sec8KBatchOutcomeStatus.OK
    assert ok_rec.event_key == "evt-8k-ok"
    assert ok_rec.accession_number == accn_ok
    assert ok_rec.result is not None
    assert ok_rec.result.submissions_observation_ids == ("a" * 64,)
    # Prior-year comparative column (2025-01-31) is excluded; only current release quarter kept
    assert [it.period_end for it in ok_rec.result.items] == [date(2026, 1, 31)]
    q4_item = ok_rec.result.items[0]
    assert q4_item.fiscal_quarter == 4
    assert q4_item.field(Sec8KMetric.REVENUE).value == Decimal(11201000000)
    assert q4_item.field(Sec8KMetric.GROSS_PROFIT).value == Decimal(8410000000)
    assert q4_item.field(Sec8KMetric.GAAP_DILUTED_EPS).value == Decimal("2.07")

    assert result.receipts[1].status is Sec8KBatchOutcomeStatus.TRANSIENT_FAILURE
    assert result.receipts[1].event_key == "evt-8ka-transient"
    assert result.receipts[2].status is Sec8KBatchOutcomeStatus.EMPTY
    assert result.receipts[2].event_key == "evt-8k-502"


def test_8k_metrics_split_currency_cell_negative_gross_profit_and_ticker_resolution(
    monkeypatch, tmp_path
) -> None:
    accn = f"{CIK_A}-26-000040"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK_A)}/{accn.replace('-', '')}"
    split_exhibit = b"""
    <html><body>
      <p>Consolidated Statements of Operations (in thousands, except per share amounts)</p>
      <table>
        <tr>
          <th></th>
          <th colspan="2">Three Months Ended January 31, 2026</th>
        </tr>
        <tr><td>Net revenue</td><td>$</td><td>850,000</td></tr>
        <tr><td>Gross profit</td><td>$</td><td>(12,500)</td></tr>
        <tr><td>Diluted net income per share</td><td>$</td><td>(0.45)</td></tr>
      </table>
    </body></html>
    """
    rows = [
        {
            "accessionNumber": accn,
            "form": "8-K",
            "filingDate": "2026-02-25",
            "reportDate": "2026-02-25",
            "acceptanceDateTime": "2026-02-25T21:00:00.000Z",
            "primaryDocument": "crm.htm",
            "items": "2.02",
        }
    ]
    bodies: dict[str, bytes | Exception] = {
        "https://www.sec.gov/files/company_tickers.json": json.dumps(
            {"0": {"cik_str": int(CIK_A), "ticker": "CRM", "title": "Synthetic CRM"}}
        ).encode(),
        f"https://data.sec.gov/submissions/CIK{CIK_A}.json": _make_submissions(CIK_A, rows),
        f"{base}/{accn}-index.htm": _index_html(CIK_A, accn),
        f"{base}/ex-991.htm": split_exhibit,
    }
    store, client, _ = _setup_client(monkeypatch, tmp_path, bodies)

    res = fetch_sec_8k_quarterly_metrics(
        client,
        store,
        ticker="crm",
        target_quarters=((2026, 4, date(2026, 1, 31)),),
        utc_now=lambda: AT,
    )
    assert res.ticker == "CRM"
    assert res.cik == CIK_A
    assert res.mapping_observation_id is not None
    item = res.items[0]
    assert item.field(Sec8KMetric.REVENUE).value == Decimal(850_000_000)
    assert item.field(Sec8KMetric.GROSS_PROFIT).value == Decimal(-12_500_000)
    assert item.field(Sec8KMetric.GAAP_DILUTED_EPS).value == Decimal("-0.45")


def test_8k_metrics_english_prose_europe_decade_not_dropped_as_non_usd() -> None:
    exhibit = _earnings_exhibit(
        pre_table_context=(
            "Strong demand across Europe and European enterprise customers "
            "marked our best decade of growth."
        ),
        scale_header="(in millions, except per share data)",
        revenue="11,201",
        gross_profit="8,410",
    )
    parsed = parse_sec_8k_earnings_metrics(exhibit, expected_period_end=date(2026, 1, 31))
    assert parsed.values[(Sec8KMetric.REVENUE, date(2026, 1, 31))].value == Decimal(11_201_000_000)
    assert parsed.values[(Sec8KMetric.GROSS_PROFIT, date(2026, 1, 31))].value == Decimal(
        8_410_000_000
    )


def test_8k_batch_redacts_multiword_user_agent_and_raw_emails(monkeypatch, tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    ua = "MyOrg Research Desk analyst@example.invalid"
    client = SecHttpClient(ua, opener=object())

    def mock_open(url: str, **kwargs):
        if CIK_A in url:
            raise TransientProviderError(f"SEC HTTP status 503 with User-Agent: {ua}; retry later")
        raise PermanentProviderError(f"rejected raw identity {ua} or contact ops@corp.invalid")

    monkeypatch.setattr(client, "open", mock_open)
    batch = fetch_sec_8k_quarterly_batch(
        (
            Sec8KBatchTarget(cik=CIK_A, target_quarters=((2026, 4, date(2026, 1, 31)),)),
            Sec8KBatchTarget(cik=CIK_B, target_quarters=((2026, 4, date(2026, 1, 31)),)),
        ),
        client,
        store,
        utc_now=lambda: AT,
    )
    for receipt in batch.receipts:
        msg = receipt.error_message or ""
        assert "MyOrg" not in msg
        assert "Research" not in msg
        assert "analyst@example.invalid" not in msg
        assert "ops@corp.invalid" not in msg
        assert "[REDACTED]" in msg


def test_8k_discovery_event_stream_deduplicates_paired_item_202_events(
    monkeypatch, tmp_path
) -> None:
    accn = f"{CIK_A}-26-000050"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK_A)}/{accn.replace('-', '')}"
    bodies: dict[str, bytes | Exception] = {
        f"{base}/{accn}-index.htm": _index_html(CIK_A, accn),
        f"{base}/ex-991.htm": _earnings_exhibit(),
    }
    store, client, calls = _setup_client(monkeypatch, tmp_path, bodies)
    accepted = datetime(2026, 2, 25, 21, 0, tzinfo=UTC)
    paired_events = (
        SecFilingDiscoveryEvent(
            key="evt-accepted-dup",
            cik=str(int(CIK_A)),
            accession=accn,
            kind=SecFilingEventKind.SEC_FILING_ACCEPTED,
            form="8-K",
            filing_date="2026-02-25",
            report_date="2026-02-25",
            acceptance_native="2026-02-25T21:00:00Z",
            acceptance_at=accepted,
            primary_document="doc.htm",
            items="2.02,9.01",
            metadata_digest="0" * 64,
            source_observation_identities=("a" * 64,),
        ),
        SecFilingDiscoveryEvent(
            key="evt-item202-preferred",
            cik=str(int(CIK_A)),
            accession=accn,
            kind=SecFilingEventKind.SEC_8K_ITEM_2_02,
            form="8-K",
            filing_date="2026-02-25",
            report_date="2026-02-25",
            acceptance_native="2026-02-25T21:00:00Z",
            acceptance_at=accepted,
            primary_document="doc.htm",
            items="2.02,9.01",
            metadata_digest="0" * 64,
            source_observation_identities=("a" * 64,),
        ),
    )
    res = fetch_sec_8k_metrics_for_discovery_events(
        paired_events, client, store, utc_now=lambda: AT
    )
    assert len(res.receipts) == 1
    assert res.ok_count == 1
    assert res.receipts[0].event_key == "evt-item202-preferred"
    assert len(calls) == 2

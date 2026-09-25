"""Synthetic SEC 6-K earnings-release contracts; no downloaded filing fixtures."""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.providers.sec import tsm_6k
from ohmydata.providers.sec.errors import CoverageError, ResourceLimitError, SchemaMismatchError
from ohmydata.providers.sec.http import SecHttpClient

CIK = "0001046179"
AT = datetime(2026, 8, 1, tzinfo=UTC)


def _release(year: int, quarter: int) -> bytes:
    ordinal = ("first", "second", "third", "fourth")[quarter - 1]
    day = (31, 30, 30, 31)[quarter - 1]
    month_name = ("March", "June", "September", "December")[quarter - 1]
    return f"""
    <html><body><h1>TSMC Reports {ordinal.title()} Quarter EPS of NT$10.00</h1>
    <p>TSMC today announced consolidated revenue of NT$1,000.00 billion,
    net income of NT$450.00 billion, and diluted earnings per share of NT$10.00
    (US$1.50 per ADR unit) for the {ordinal} quarter ended {month_name} {day}, {year}.</p>
    <p>All figures were prepared in accordance with TIFRS on a consolidated basis.</p>
    <p>In US dollars, {ordinal} quarter revenue was $30.00 billion.</p>
    <p>Gross margin for the quarter was 60.0%, operating margin was 50.0%.</p>
    <p>TSMC's {year} {ordinal} quarter consolidated results:</p>
    <p>(Unit: NT$ million, except for EPS)</p>
    <table>
      <tr><td></td><td>{quarter}Q{year % 100:02d} Amount a</td><td>prior period</td></tr>
      <tr><td>Net sales</td><td>1,000,000</td><td>900,000</td></tr>
      <tr><td>Gross profit</td><td>600,000</td><td>500,000</td></tr>
      <tr><td>Income from operations</td><td>500,000</td><td>400,000</td></tr>
      <tr><td>Income before tax</td><td>550,000</td><td>450,000</td></tr>
      <tr><td>Net income</td><td>450,000</td><td>350,000</td></tr>
      <tr><td>EPS (NT$)</td><td>10.00 b</td><td>9.00</td></tr>
    </table></body></html>
    """.encode()


def _index(accession: str, *, href: str | None = None) -> bytes:
    basename = "quarter-release.htm"
    path = f"/Archives/edgar/data/{int(CIK)}/{accession.replace('-', '')}/{basename}"
    return f"""
    <html><body><h3>SEC Accession No. {accession}</h3>
    <table class="tableFile"><tr><td>2</td><td>EX-99.1</td>
    <td><a href="{href or path}">{basename}</a></td><td>EX-99.1</td></tr></table>
    </body></html>
    """.encode()


def _row(accession: str, period: str, filing: str) -> dict[str, str]:
    return {
        "accessionNumber": accession,
        "form": "6-K",
        "filingDate": filing,
        "reportDate": period,
        "primaryDocument": f"tsm-{filing.replace('-', '')}x6k.htm",
        "acceptanceDateTime": f"{filing}T10:00:00.000Z",
    }


def _setup(monkeypatch, tmp_path, rows: list[dict[str, str]]):
    store = SnapshotStore(tmp_path)
    columns = (
        "accessionNumber",
        "form",
        "filingDate",
        "reportDate",
        "primaryDocument",
        "acceptanceDateTime",
    )
    payload = {
        "cik": CIK,
        "filings": {"recent": {name: [row[name] for row in rows] for name in columns}, "files": []},
    }
    client = SecHttpClient("Synthetic Test Contact test@example.invalid", opener=object())
    bodies: dict[str, bytes] = {
        "https://www.sec.gov/files/company_tickers.json": json.dumps(
            {"0": {"cik_str": int(CIK), "ticker": "TSM", "title": "Synthetic Semiconductor"}}
        ).encode(),
        f"https://data.sec.gov/submissions/CIK{CIK}.json": json.dumps(payload).encode(),
    }
    for row in rows:
        accession = row["accessionNumber"]
        base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accession.replace('-', '')}"
        bodies[f"{base}/{accession}-index.htm"] = _index(accession)
        period = date.fromisoformat(row["reportDate"])
        bodies[f"{base}/quarter-release.htm"] = _release(period.year, period.month // 3)

    class Response:
        def __init__(self, url: str, body: bytes) -> None:
            self.url = url
            self.body = io.BytesIO(body)
            self.headers: dict[str, str] = {}

    calls: list[str] = []

    def open_url(url: str, **kwargs):
        calls.append(url)
        return Response(url, bodies[url])

    monkeypatch.setattr(client, "open", open_url)
    return store, client, bodies, calls


def test_release_parser_keeps_twd_usd_and_adr_units_distinct() -> None:
    values = tsm_6k.parse_sec_tsm_6k_release(
        _release(2026, 2), expected_period_end=date(2026, 6, 30)
    )
    assert values.revenue_twd_million == Decimal(1_000_000)
    assert values.revenue_usd_billion == Decimal("30.00")
    assert values.gross_profit_twd_million == Decimal(600_000)
    assert values.operating_income_twd_million == Decimal(500_000)
    assert values.income_before_tax_twd_million == Decimal(550_000)
    assert values.net_income_twd_million == Decimal(450_000)
    assert values.diluted_eps_twd_per_ordinary_share == Decimal("10.00")
    assert values.diluted_eps_usd_per_adr == Decimal("1.50")
    assert values.gross_margin_pct == Decimal("60.0")


def test_explicit_usd_estimate_keeps_reported_and_derived_values_distinct() -> None:
    values = tsm_6k.parse_sec_tsm_6k_release(
        _release(2026, 2), expected_period_end=date(2026, 6, 30)
    )
    converted = tsm_6k.estimate_sec_tsm_6k_usd_from_revenue(values)
    assert converted.implied_twd_per_usd.quantize(Decimal("0.0001")) == Decimal("33.3333")
    assert converted.reported_revenue_usd_million == Decimal(30_000)
    assert converted.estimated_gross_profit_usd_million == Decimal(18_000)
    assert converted.estimated_operating_income_usd_million == Decimal(15_000)
    assert converted.estimated_income_before_tax_usd_million == Decimal(16_500)
    assert converted.estimated_net_income_usd_million == Decimal(13_500)
    assert converted.reported_diluted_eps_usd_per_adr == Decimal("1.50")


@pytest.mark.parametrize(
    "original,replacement",
    [
        (b"Gross profit</td><td>600,000", b"Gross profit</td><td>500,000"),
        (b"In US dollars, second quarter revenue", b"USD revenue"),
        (b"TIFRS on a consolidated basis", b"unknown basis"),
        (b"2Q26 Amount a", b"2Q25 Amount a"),
        (b"net income of NT$450.00 billion", b"net income of NT$440.00 billion"),
    ],
)
def test_release_parser_fails_closed_on_conflicting_or_missing_evidence(
    original: bytes, replacement: bytes
) -> None:
    body = _release(2026, 2).replace(original, replacement)
    with pytest.raises(SchemaMismatchError):
        tsm_6k.parse_sec_tsm_6k_release(body, expected_period_end=date(2026, 6, 30))


def test_release_parser_rejects_wrong_quarter_and_unbounded_source() -> None:
    with pytest.raises(SchemaMismatchError, match="quarter"):
        tsm_6k.parse_sec_tsm_6k_release(_release(2026, 2), expected_period_end=date(2026, 3, 31))
    with pytest.raises(ResourceLimitError):
        tsm_6k.parse_sec_tsm_6k_release(
            _release(2026, 2) + b" " * (128 * 1024),
            expected_period_end=date(2026, 6, 30),
        )


def test_fetch_tsm_6k_quarters_retains_exact_sources_and_respects_cutoff(
    monkeypatch, tmp_path
) -> None:
    q1 = _row(f"{CIK}-26-000001", "2026-03-31", "2026-04-16")
    q2 = _row(f"{CIK}-26-000002", "2026-06-30", "2026-07-16")
    store, client, _, calls = _setup(monkeypatch, tmp_path, [q2, q1])
    result = tsm_6k.fetch_sec_tsm_6k_quarters(client, store, count=2, utc_now=lambda: AT)
    assert result.coverage_complete and result.missing_period_ends == ()
    assert [q.values.period_end for q in result.quarters] == [date(2026, 3, 31), date(2026, 6, 30)]
    assert len(calls) == 6
    assert result.quarters[1].values.diluted_eps_usd_per_adr == Decimal("1.50")
    assert (
        result.quarters[1].values.revenue_twd_million
        != result.quarters[1].values.revenue_usd_billion
    )
    assert all(len(q.exhibit_observation_id) == 64 for q in result.quarters)

    cutoff = tsm_6k.fetch_sec_tsm_6k_quarters(
        client,
        store,
        count=1,
        utc_now=lambda: AT,
        acceptance_upper=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert cutoff.coverage_complete
    assert [q.values.period_end for q in cutoff.quarters] == [date(2026, 3, 31)]


def test_fetch_tsm_6k_quarters_reports_gap_and_rejects_unsafe_exhibit(
    monkeypatch, tmp_path
) -> None:
    q2 = _row(f"{CIK}-26-000002", "2026-06-30", "2026-07-16")
    store, client, bodies, _ = _setup(monkeypatch, tmp_path, [q2])
    result = tsm_6k.fetch_sec_tsm_6k_quarters(client, store, count=2, utc_now=lambda: AT)
    assert not result.coverage_complete
    assert result.missing_period_ends == (date(2026, 3, 31),)
    index_url = next(url for url in bodies if url.endswith("-index.htm"))
    bodies[index_url] = _index(q2["accessionNumber"], href="https://evil.invalid/x")
    with pytest.raises(SchemaMismatchError, match="unsafe or mismatched"):
        tsm_6k.fetch_sec_tsm_6k_quarters(client, store, count=1, utc_now=lambda: AT)


def test_fetch_tsm_6k_quarters_requires_validated_issuer_identity(monkeypatch, tmp_path) -> None:
    row = _row(f"{CIK}-26-000002", "2026-06-30", "2026-07-16")
    store, client, bodies, _ = _setup(monkeypatch, tmp_path, [row])
    bodies["https://www.sec.gov/files/company_tickers.json"] = json.dumps(
        {"0": {"cik_str": 1, "ticker": "TSM", "title": "Synthetic Semiconductor"}}
    ).encode()
    with pytest.raises(CoverageError, match="validated issuer CIK"):
        tsm_6k.fetch_sec_tsm_6k_quarters(client, store, count=1, utc_now=lambda: AT)

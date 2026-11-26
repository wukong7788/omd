"""Offline synthetic tests for plugin-based foreign private issuer Form 6-K extraction."""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.providers.sec import fpi_6k
from ohmydata.providers.sec._fpi_6k_xbrl import parse_sec_fpi_6k_xbrl
from ohmydata.providers.sec.errors import CoverageError, ResourceLimitError, SchemaMismatchError
from ohmydata.providers.sec.fpi_6k import (
    SecFpi6KExhibitContext,
    SecFpi6KMetricRow,
    SecFpi6KParserRegistry,
    SecFpi6KPluginParseOutput,
    SecFpi6KShareBasis,
    SecFpi6KSourceKind,
    default_fpi_6k_registry,
    fetch_sec_fpi_6k_quarters,
)
from ohmydata.providers.sec.http import SecHttpClient

TSM_CIK = "0001046179"
EUR_FPI_CIK = "0000937966"
CNY_FPI_CIK = "0001577552"
CUSTOM_CIK = "0001999999"
AT = datetime(2026, 8, 15, tzinfo=UTC)


def _tsm_release(year: int, quarter: int) -> bytes:
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


def _index_html(
    cik: str,
    accession: str,
    rows: list[tuple[str, str, str | None]],
) -> bytes:
    """Build synthetic SEC -index.htm; rows are (doc_name, doc_type, custom_href_or_None)."""
    compact = accession.replace("-", "")
    trs: list[str] = []
    for idx, (doc_name, doc_type, custom_href) in enumerate(rows, start=1):
        href = custom_href or f"/Archives/edgar/data/{int(cik)}/{compact}/{doc_name}"
        trs.append(
            f"<tr><td>{idx}</td><td>{doc_type}</td>"
            f'<td><a href="{href}">{doc_name}</a></td><td>{doc_type}</td></tr>'
        )
    return (
        f"<html><body><h3>SEC Accession No. {accession}</h3>"
        f"<table>{''.join(trs)}</table></body></html>"
    ).encode()


def _ifrs_eur_xbrl(cik: str, *, include_facts: bool = True) -> bytes:
    facts = (
        """
        <ifrs-full:Revenue contextRef="q2_2026" unitRef="eur">7500000000</ifrs-full:Revenue>
        <ifrs-full:Revenue contextRef="ytd_2026" unitRef="eur">14500000000</ifrs-full:Revenue>
        <ifrs-full:Revenue contextRef="dim_seg" unitRef="eur">3000000000</ifrs-full:Revenue>
        <ifrs-full:GrossProfit contextRef="q2_2026" unitRef="eur">3900000000</ifrs-full:GrossProfit>
        <ifrs-full:ProfitLossFromOperatingActivities contextRef="q2_2026" unitRef="eur">2400000000</ifrs-full:ProfitLossFromOperatingActivities>
        <ifrs-full:ProfitLoss contextRef="q2_2026" unitRef="eur">2050000000</ifrs-full:ProfitLoss>
        <ifrs-full:DilutedEarningsLossPerShare contextRef="q2_2026" unitRef="eur_per_share">5.25</ifrs-full:DilutedEarningsLossPerShare>
        <ifrs-full:Assets contextRef="inst_q2" unitRef="eur">42000000000</ifrs-full:Assets>
        <dei:EntityCommonStockSharesOutstanding contextRef="inst_q2" unitRef="shares">390000000</dei:EntityCommonStockSharesOutstanding>
        """
        if include_facts
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <xbrli:xbrl
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
      xmlns:ifrs-full="https://xbrl.ifrs.org/taxonomy/2025-03-27/ifrs-full"
      xmlns:dei="http://xbrl.sec.gov/dei/2025"
      xmlns:ex="http://example.invalid/custom">
      <xbrli:context id="q2_2026">
        <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <xbrli:context id="ytd_2026">
        <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <xbrli:context id="dim_seg">
        <xbrli:entity>
          <xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}</xbrli:identifier>
          <xbrli:segment><xbrldi:explicitMember dimension="ex:SegAxis">ex:EuvMember</xbrldi:explicitMember></xbrli:segment>
        </xbrli:entity>
        <xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <xbrli:context id="inst_q2">
        <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period>
      </xbrli:context>
      <xbrli:unit id="eur"><xbrli:measure>iso4217:EUR</xbrli:measure></xbrli:unit>
      <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
      <xbrli:unit id="eur_per_share">
        <xbrli:divide>
          <xbrli:unitNumerator><xbrli:measure>iso4217:EUR</xbrli:measure></xbrli:unitNumerator>
          <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator>
        </xbrli:divide>
      </xbrli:unit>
      <dei:DocumentPeriodEndDate contextRef="q2_2026">2026-06-30</dei:DocumentPeriodEndDate>
      <dei:DocumentFiscalYearFocus contextRef="q2_2026">2026</dei:DocumentFiscalYearFocus>
      <dei:DocumentFiscalPeriodFocus contextRef="q2_2026">Q2</dei:DocumentFiscalPeriodFocus>
      {facts}
    </xbrli:xbrl>
    """.encode()


def _inline_xbrl_cny(cik: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <html xmlns="http://www.w3.org/1999/xhtml"
          xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
          xmlns:xbrli="http://www.xbrl.org/2003/instance"
          xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
          xmlns:us-gaap="http://fasb.org/us-gaap/2025"
          xmlns:dei="http://xbrl.sec.gov/dei/2025">
      <body>
        <ix:header>
          <ix:resources>
            <xbrli:context id="c_q1">
              <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}</xbrli:identifier></xbrli:entity>
              <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
            </xbrli:context>
            <xbrli:unit id="u_cny"><xbrli:measure>iso4217:CNY</xbrli:measure></xbrli:unit>
            <xbrli:unit id="u_cny_eps">
              <xbrli:divide>
                <xbrli:unitNumerator><xbrli:measure>iso4217:CNY</xbrli:measure></xbrli:unitNumerator>
                <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator>
              </xbrli:divide>
            </xbrli:unit>
          </ix:resources>
        </ix:header>
        <p><ix:nonNumeric name="dei:DocumentPeriodEndDate" contextRef="c_q1">2026-03-31</ix:nonNumeric></p>
        <p><ix:nonNumeric name="dei:DocumentFiscalYearFocus" contextRef="c_q1">2026</ix:nonNumeric></p>
        <p><ix:nonNumeric name="dei:DocumentFiscalPeriodFocus" contextRef="c_q1">Q1</ix:nonNumeric></p>
        <p><ix:nonFraction name="us-gaap:Revenues" contextRef="c_q1" unitRef="u_cny" scale="6">240,500</ix:nonFraction></p>
        <p><ix:nonFraction name="us-gaap:EarningsPerShareDiluted" contextRef="c_q1" unitRef="u_cny_eps" sign="-">1.25</ix:nonFraction></p>
      </body>
    </html>
    """.encode()


def _setup_fpi_client(
    monkeypatch,
    tmp_path,
    cik: str,
    recent_rows: list[dict[str, object]],
    bodies_extra: dict[str, bytes],
    *,
    files: list[dict[str, object]] | None = None,
):
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
        "cik": cik,
        "filings": {
            "recent": {col: [row.get(col) for row in recent_rows] for col in columns},
            "files": files or [],
        },
    }
    client = SecHttpClient("Synthetic FPI Test Contact test@example.invalid", opener=object())
    bodies: dict[str, bytes] = {
        f"https://data.sec.gov/submissions/CIK{cik}.json": json.dumps(payload).encode(),
    }
    bodies.update(bodies_extra)

    class Response:
        def __init__(self, url: str, body: bytes) -> None:
            self.url = url
            self.body = io.BytesIO(body)
            self.headers: dict[str, str] = {}

    calls: list[str] = []

    def mock_open(url: str, **kwargs):
        calls.append(url)
        if url not in bodies:
            raise AssertionError(f"Unexpected URL requested: {url}")
        return Response(url, bodies[url])

    monkeypatch.setattr(client, "open", mock_open)
    return store, client, calls


def test_fpi_6k_registry_normalization_and_default_tsmc() -> None:
    registry = default_fpi_6k_registry()
    assert registry.has("1046179")
    assert registry.has(TSM_CIK)
    assert registry.registered_ciks() == (TSM_CIK,)
    parser = registry.get("1046179")
    assert parser is not None
    assert parser.parser_id == "tsmc-6k-ex99-v1"

    with pytest.raises(ValueError, match="already registered"):
        registry.register("1046179", parser)
    with pytest.raises(ValueError, match="ASCII digits"):
        registry.get("ABC")
    with pytest.raises(TypeError, match="SecFpi6KExhibitParser"):
        registry.register(CUSTOM_CIK, object())  # type: ignore[arg-type]


def test_fpi_6k_priority1_xbrl_ifrs_eur_and_dei_shares_cik_first(monkeypatch, tmp_path) -> None:
    accn = f"{EUR_FPI_CIK}-26-000010"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(EUR_FPI_CIK)}/{accn.replace('-', '')}"
    rows = [
        {
            "accessionNumber": accn,
            "form": "6-K",
            "filingDate": "2026-07-15",
            "reportDate": "2026-06-30",
            "primaryDocument": "asml-6k.htm",
            "acceptanceDateTime": "2026-07-15T11:30:00.000Z",
        }
    ]
    extra = {
        f"{base}/{accn}-index.htm": _index_html(
            EUR_FPI_CIK,
            accn,
            [
                ("asml-20260630.xml", "EX-101.INS", None),
                ("asml-20260630_cal.xml", "EX-101.CAL", None),
                ("ex99-1.htm", "EX-99.1", None),
            ],
        ),
        f"{base}/asml-20260630.xml": _ifrs_eur_xbrl(EUR_FPI_CIK),
    }
    store, client, calls = _setup_fpi_client(monkeypatch, tmp_path, EUR_FPI_CIK, rows, extra)

    # CIK-first resolution: company_tickers.json is NEVER requested (works for delisted ADRs)
    result = fetch_sec_fpi_6k_quarters("937966", client, store, utc_now=lambda: AT)
    assert not any("company_tickers.json" in url for url in calls)
    assert result.cik == EUR_FPI_CIK
    assert result.coverage_complete is True
    assert len(result.records) == 1

    rec = result.records[0]
    assert rec.source_kind is SecFpi6KSourceKind.XBRL_ATTACHMENT
    assert rec.parser_id == "sec-fpi-6k-xbrl-v1"
    assert rec.reporting_currency == "EUR"
    assert rec.period_end == date(2026, 6, 30)
    assert rec.fiscal_year == 2026
    assert rec.fiscal_quarter == 2
    assert len(rec.index_observation_id) == 64
    assert rec.source_observation_id is not None and len(rec.source_observation_id) == 64

    by_metric = {m.metric: m for m in rec.metrics}
    # YTD (14.5B) and dimensional segment (3B) are strictly excluded!
    assert by_metric["revenue"].value == Decimal(7500000000)
    assert by_metric["revenue"].unit == "EUR"
    assert by_metric["revenue"].reporting_currency == "EUR"
    assert by_metric["revenue"].taxonomy == "ifrs-full"
    assert by_metric["gross_profit"].value == Decimal(3900000000)
    assert by_metric["operating_income"].value == Decimal(2400000000)
    assert by_metric["net_income"].value == Decimal(2050000000)
    assert by_metric["diluted_eps"].value == Decimal("5.25")
    assert by_metric["diluted_eps"].unit == "EUR/shares"
    assert by_metric["total_assets"].value == Decimal(42000000000)
    assert by_metric["total_assets"].period_type == "instant"
    assert by_metric["dei_common_shares_outstanding"].value == Decimal(390000000)
    assert by_metric["dei_common_shares_outstanding"].unit == "shares"
    assert by_metric["dei_common_shares_outstanding"].reporting_currency is None


def test_fpi_6k_priority1_inline_xbrl_cny_and_beats_plugin(monkeypatch, tmp_path) -> None:
    accn = f"{CNY_FPI_CIK}-26-000003"
    compact = accn.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CNY_FPI_CIK)}/{compact}"
    ix_href = f"/ix?doc=/Archives/edgar/data/{int(CNY_FPI_CIK)}/{compact}/baba-6k.htm"

    class ShouldNotRunPlugin:
        parser_id = "should-not-run-v1"

        def parse(self, body: bytes, *, context: SecFpi6KExhibitContext):
            raise AssertionError("EX-99.1 plugin must not run when Priority 1 XBRL succeeds")

    registry = SecFpi6KParserRegistry()
    registry.register(CNY_FPI_CIK, ShouldNotRunPlugin())

    rows = [
        {
            "accessionNumber": accn,
            "form": "6-K",
            "filingDate": "2026-05-14",
            "reportDate": "2026-03-31",
            "primaryDocument": "baba-6k.htm",
            "acceptanceDateTime": "2026-05-14T10:00:00.000Z",
        }
    ]
    extra = {
        f"{base}/{accn}-index.htm": _index_html(
            CNY_FPI_CIK,
            accn,
            [
                ("baba-6k.htm", "6-K", ix_href),
                ("ex99-1.htm", "EX-99.1", None),
            ],
        ),
        f"{base}/baba-6k.htm": _inline_xbrl_cny(CNY_FPI_CIK),
    }
    store, client, calls = _setup_fpi_client(monkeypatch, tmp_path, CNY_FPI_CIK, rows, extra)
    result = fetch_sec_fpi_6k_quarters(
        CNY_FPI_CIK, client, store, registry=registry, utc_now=lambda: AT
    )
    assert not any("ex99-1.htm" in url for url in calls)
    rec = result.records[0]
    assert rec.source_kind is SecFpi6KSourceKind.XBRL_ATTACHMENT
    assert rec.reporting_currency == "CNY"
    by_metric = {m.metric: m for m in rec.metrics}
    assert by_metric["revenue"].value == Decimal(240500000000)
    assert by_metric["revenue"].unit == "CNY"
    assert by_metric["diluted_eps"].value == Decimal("-1.25")
    assert by_metric["diluted_eps"].unit == "CNY/shares"


def test_fpi_6k_priority2_default_tsmc_plugin_and_non_earnings_6k(monkeypatch, tmp_path) -> None:
    accn_q2 = f"{TSM_CIK}-26-000020"
    accn_monthly = f"{TSM_CIK}-26-000021"
    base_q2 = f"https://www.sec.gov/Archives/edgar/data/{int(TSM_CIK)}/{accn_q2.replace('-', '')}"
    base_m = (
        f"https://www.sec.gov/Archives/edgar/data/{int(TSM_CIK)}/{accn_monthly.replace('-', '')}"
    )
    rows = [
        {
            "accessionNumber": accn_q2,
            "form": "6-K",
            "filingDate": "2026-07-16",
            "reportDate": "2026-06-30",
            "primaryDocument": "tsm-20260716x6k.htm",
            "acceptanceDateTime": "2026-07-16T10:00:00.000Z",
        },
        {
            "accessionNumber": accn_monthly,
            "form": "6-K",
            "filingDate": "2026-08-10",
            "reportDate": "2026-07-31",
            "primaryDocument": "tsm-20260810x6k.htm",
            "acceptanceDateTime": "2026-08-10T10:00:00.000Z",
        },
    ]
    extra = {
        f"{base_q2}/{accn_q2}-index.htm": _index_html(
            TSM_CIK, accn_q2, [("quarter-release.htm", "EX-99.1", None)]
        ),
        f"{base_q2}/quarter-release.htm": _tsm_release(2026, 2),
        f"{base_m}/{accn_monthly}-index.htm": _index_html(
            TSM_CIK, accn_monthly, [("monthly-sales.htm", "EX-99.1", None)]
        ),
        f"{base_m}/monthly-sales.htm": b"<html><body><p>TSMC July 2026 Revenue Report</p></body></html>",
    }
    store, client, _ = _setup_fpi_client(monkeypatch, tmp_path, TSM_CIK, rows, extra)
    result = fetch_sec_fpi_6k_quarters(TSM_CIK, client, store, utc_now=lambda: AT)

    assert len(result.records) == 2
    rec_q2, rec_monthly = result.records
    assert rec_q2.source_kind is SecFpi6KSourceKind.EX99_PLUGIN
    assert rec_q2.parser_id == "tsmc-6k-ex99-v1"
    assert rec_q2.reporting_currency == "TWD"
    assert rec_q2.share_basis is SecFpi6KShareBasis.ORDINARY_SHARES
    assert rec_q2.fiscal_year == 2026 and rec_q2.fiscal_quarter == 2

    by_metric = {m.metric: m for m in rec_q2.metrics}
    assert by_metric["revenue"].value == Decimal(1000000000000)
    assert by_metric["revenue"].unit == "TWD"
    assert by_metric["revenue"].reporting_currency == "TWD"
    assert by_metric["diluted_eps"].value == Decimal("10.00")
    assert by_metric["diluted_eps"].unit == "TWD/shares"
    assert by_metric["diluted_eps"].share_basis is SecFpi6KShareBasis.ORDINARY_SHARES
    assert by_metric["diluted_eps_adr"].value == Decimal("1.50")
    assert by_metric["diluted_eps_adr"].unit == "USD/ADR"
    assert by_metric["diluted_eps_adr"].share_basis is SecFpi6KShareBasis.ADR_EQUIVALENT_SHARES
    assert by_metric["revenue_usd_reported"].value == Decimal(30000000000)

    # Non-earnings 6-K cleanly produces explicit UNSUPPORTED without crashing the batch
    assert rec_monthly.source_kind is SecFpi6KSourceKind.UNSUPPORTED
    assert rec_monthly.metrics == ()
    assert rec_monthly.index_observation_id is not None
    assert rec_monthly.source_observation_id is not None
    assert result.unsupported_accessions == (accn_monthly,)
    assert result.coverage_complete is False


def test_fpi_6k_priority2_custom_plugin_and_priority3_unsupported_and_empty(
    monkeypatch, tmp_path
) -> None:
    accn_custom = f"{CUSTOM_CIK}-26-000001"
    accn_empty_xbrl = f"{CUSTOM_CIK}-26-000002"
    base1 = (
        f"https://www.sec.gov/Archives/edgar/data/{int(CUSTOM_CIK)}/{accn_custom.replace('-', '')}"
    )
    base2 = f"https://www.sec.gov/Archives/edgar/data/{int(CUSTOM_CIK)}/{accn_empty_xbrl.replace('-', '')}"

    class CustomJpyPlugin:
        parser_id = "custom-jpy-6k-v1"

        def parse(
            self, body: bytes, *, context: SecFpi6KExhibitContext
        ) -> SecFpi6KPluginParseOutput | None:
            if b"JPY Earnings" not in body:
                return None
            return SecFpi6KPluginParseOutput(
                period_end=date(2026, 3, 31),
                reporting_currency="JPY",
                share_basis=SecFpi6KShareBasis.ADR_EQUIVALENT_SHARES,
                fiscal_year=2026,
                fiscal_quarter=4,
                metrics=(
                    SecFpi6KMetricRow(
                        metric="revenue",
                        native_tag="Sales",
                        value=Decimal(500000000000),
                        unit="JPY",
                        reporting_currency="JPY",
                        share_basis=SecFpi6KShareBasis.UNKNOWN,
                        taxonomy="ex99_plugin",
                        period_end=date(2026, 3, 31),
                    ),
                ),
            )

    rows = [
        {
            "accessionNumber": accn_custom,
            "form": "6-K",
            "filingDate": "2026-05-01",
            "reportDate": "2026-03-31",
            "primaryDocument": "doc1.htm",
            "acceptanceDateTime": "2026-05-01T08:00:00.000Z",
        },
        {
            "accessionNumber": accn_empty_xbrl,
            "form": "6-K",
            "filingDate": "2026-06-01",
            "reportDate": "2026-06-30",
            "primaryDocument": "doc2.htm",
            "acceptanceDateTime": "2026-06-01T08:00:00.000Z",
        },
    ]
    extra = {
        f"{base1}/{accn_custom}-index.htm": _index_html(
            CUSTOM_CIK, accn_custom, [("release.htm", "EX-99.1", None)]
        ),
        f"{base1}/release.htm": b"<html><body>JPY Earnings Release</body></html>",
        f"{base2}/{accn_empty_xbrl}-index.htm": _index_html(
            CUSTOM_CIK, accn_empty_xbrl, [("cover.xml", "EX-101.INS", None)]
        ),
        f"{base2}/cover.xml": _ifrs_eur_xbrl(CUSTOM_CIK, include_facts=False),
    }
    store, client, _ = _setup_fpi_client(monkeypatch, tmp_path, CUSTOM_CIK, rows, extra)

    # Without custom plugin: first filing is UNSUPPORTED, second is EMPTY
    res_unregistered = fetch_sec_fpi_6k_quarters(CUSTOM_CIK, client, store, utc_now=lambda: AT)
    assert res_unregistered.records[0].source_kind is SecFpi6KSourceKind.UNSUPPORTED
    assert res_unregistered.records[1].source_kind is SecFpi6KSourceKind.EMPTY
    assert res_unregistered.unsupported_accessions == (accn_custom,)
    assert res_unregistered.empty_accessions == (accn_empty_xbrl,)

    # With custom plugin registered: first filing becomes EX99_PLUGIN with JPY preserved
    reg = default_fpi_6k_registry()
    reg.register(CUSTOM_CIK, CustomJpyPlugin())
    res_registered = fetch_sec_fpi_6k_quarters(
        CUSTOM_CIK, client, store, registry=reg, utc_now=lambda: AT
    )
    assert res_registered.records[0].source_kind is SecFpi6KSourceKind.EX99_PLUGIN
    assert res_registered.records[0].parser_id == "custom-jpy-6k-v1"
    assert res_registered.records[0].reporting_currency == "JPY"
    assert res_registered.records[0].share_basis is SecFpi6KShareBasis.ADR_EQUIVALENT_SHARES
    assert res_registered.records[0].metrics[0].value == Decimal(500000000000)


def test_fpi_6k_pit_cutoff_year_window_and_history_page(monkeypatch, tmp_path) -> None:
    accn_2024 = f"{EUR_FPI_CIK}-24-000001"
    accn_2026_early = f"{EUR_FPI_CIK}-26-000001"
    accn_2026_late = f"{EUR_FPI_CIK}-26-000002"
    b24 = f"https://www.sec.gov/Archives/edgar/data/{int(EUR_FPI_CIK)}/{accn_2024.replace('-', '')}"
    b26 = f"https://www.sec.gov/Archives/edgar/data/{int(EUR_FPI_CIK)}/{accn_2026_early.replace('-', '')}"

    recent_rows = [
        {
            "accessionNumber": accn_2026_early,
            "form": "6-K",
            "filingDate": "2026-07-15",
            "reportDate": "2026-06-30",
            "primaryDocument": "q2.htm",
            "acceptanceDateTime": "2026-07-15T10:00:00.000Z",
        },
        {
            "accessionNumber": accn_2026_late,
            "form": "6-K",
            "filingDate": "2026-08-05",
            "reportDate": "2026-06-30",
            "primaryDocument": "late.htm",
            "acceptanceDateTime": "2026-08-05T10:00:00.000Z",
        },
    ]
    hist_name = f"CIK{EUR_FPI_CIK}-submissions-001.json"
    hist_payload = {
        "accessionNumber": [accn_2024],
        "form": ["6-K"],
        "filingDate": ["2024-07-17"],
        "reportDate": ["2024-06-30"],
        "primaryDocument": ["q2-2024.htm"],
        "acceptanceDateTime": ["2024-07-17T10:00:00.000Z"],
    }
    extra = {
        f"https://data.sec.gov/submissions/{hist_name}": json.dumps(hist_payload).encode(),
        f"{b24}/{accn_2024}-index.htm": _index_html(EUR_FPI_CIK, accn_2024, []),
        f"{b26}/{accn_2026_early}-index.htm": _index_html(
            EUR_FPI_CIK, accn_2026_early, [("asml.xml", "EX-101.INS", None)]
        ),
        f"{b26}/asml.xml": _ifrs_eur_xbrl(EUR_FPI_CIK),
    }
    store, client, _ = _setup_fpi_client(
        monkeypatch,
        tmp_path,
        EUR_FPI_CIK,
        recent_rows,
        extra,
        files=[{"name": hist_name, "filingFrom": "2024-01-01", "filingTo": "2024-12-31"}],
    )

    result = fetch_sec_fpi_6k_quarters(
        EUR_FPI_CIK,
        client,
        store,
        start_year=2024,
        end_year=2026,
        acceptance_upper=datetime(2026, 8, 1, tzinfo=UTC),
        utc_now=lambda: AT,
    )
    assert [r.accession_number for r in result.records] == [accn_2024, accn_2026_early]
    assert len(result.submissions_observation_ids) == 2


def test_fpi_6k_security_xxe_entity_mismatch_and_unsafe_href(monkeypatch, tmp_path) -> None:
    # XXE / DTD declarations must fail closed
    xxe = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "1">]><xbrli:xbrl/>'
    with pytest.raises(SchemaMismatchError, match="declarations"):
        parse_sec_fpi_6k_xbrl(xxe, cik=EUR_FPI_CIK)

    # Wrong entity CIK in XBRL context must fail closed
    with pytest.raises(SchemaMismatchError, match="entity mismatch"):
        parse_sec_fpi_6k_xbrl(_ifrs_eur_xbrl(TSM_CIK), cik=EUR_FPI_CIK)

    # Unsafe external href in filing index must fail closed
    accn = f"{EUR_FPI_CIK}-26-000099"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(EUR_FPI_CIK)}/{accn.replace('-', '')}"
    rows = [
        {
            "accessionNumber": accn,
            "form": "6-K",
            "filingDate": "2026-07-15",
            "reportDate": "2026-06-30",
            "primaryDocument": "x.htm",
            "acceptanceDateTime": "2026-07-15T10:00:00.000Z",
        }
    ]
    extra = {
        f"{base}/{accn}-index.htm": _index_html(
            EUR_FPI_CIK, accn, [("evil.xml", "EX-101.INS", "https://evil.invalid/evil.xml")]
        )
    }
    store, client, _ = _setup_fpi_client(monkeypatch, tmp_path, EUR_FPI_CIK, rows, extra)
    with pytest.raises(SchemaMismatchError, match="unsafe or mismatched"):
        fetch_sec_fpi_6k_quarters(EUR_FPI_CIK, client, store, utc_now=lambda: AT)

    # Oversized XBRL payload fails with ResourceLimitError
    with pytest.raises(ResourceLimitError):
        parse_sec_fpi_6k_xbrl(b" " * (8 * 1024**2 + 1), cik=EUR_FPI_CIK)

    # Missing filingFrom/filingTo on historical page fails explicitly with CoverageError
    hist_name = f"CIK{EUR_FPI_CIK}-submissions-001.json"
    store_cov, client_cov, _ = _setup_fpi_client(
        monkeypatch,
        tmp_path / "cov",
        EUR_FPI_CIK,
        [],
        {},
        files=[{"name": hist_name}],
    )
    with pytest.raises(CoverageError, match="cannot be range-qualified"):
        fetch_sec_fpi_6k_quarters(
            EUR_FPI_CIK, client_cov, store_cov, start_year=2024, utc_now=lambda: AT
        )
    assert fpi_6k.__all__


def test_fpi_6k_xbrl_index_skips_r_xml_and_metalinks_and_handles_dei_and_comparatives(
    monkeypatch, tmp_path
) -> None:
    # 1. Index contains R1.xml and MetaLinks.xml BEFORE actual *_htm.xml instance
    accn = f"{EUR_FPI_CIK}-26-000055"
    base = f"https://www.sec.gov/Archives/edgar/data/{int(EUR_FPI_CIK)}/{accn.replace('-', '')}"
    xbrl_cover_and_comparative = f"""<?xml version="1.0" encoding="UTF-8"?>
    <xbrli:xbrl
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
      xmlns:ifrs-full="https://xbrl.ifrs.org/taxonomy/2025-03-27/ifrs-full"
      xmlns:dei="http://xbrl.sec.gov/dei/2025">
      <xbrli:context id="q1_2025">
        <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{EUR_FPI_CIK}</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <xbrli:context id="q1_2024">
        <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{EUR_FPI_CIK}</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-03-31</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <xbrli:context id="cover_shares">
        <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{EUR_FPI_CIK}</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:instant>2025-04-12</xbrli:instant></xbrli:period>
      </xbrli:context>
      <xbrli:unit id="eur"><xbrli:measure>iso4217:EUR</xbrli:measure></xbrli:unit>
      <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
      <ifrs-full:Revenue contextRef="q1_2025" unitRef="eur">7700000000</ifrs-full:Revenue>
      <ifrs-full:Revenue contextRef="q1_2024" unitRef="eur">5290000000</ifrs-full:Revenue>
      <dei:EntityCommonStockSharesOutstanding contextRef="cover_shares" unitRef="shares">393200000</dei:EntityCommonStockSharesOutstanding>
    </xbrli:xbrl>
    """.encode()

    rows = [
        {
            "accessionNumber": accn,
            "form": "6-K",
            "filingDate": "2025-04-16",
            "reportDate": "",
            "primaryDocument": "asml-6k.htm",
            "acceptanceDateTime": "2025-04-16T11:00:00.000Z",
        }
    ]
    extra = {
        f"{base}/{accn}-index.htm": _index_html(
            EUR_FPI_CIK,
            accn,
            [
                ("R1.xml", "XML", None),
                ("MetaLinks.xml", "XML", None),
                ("asml-20250331_htm.xml", "XML", None),
            ],
        ),
        f"{base}/asml-20250331_htm.xml": xbrl_cover_and_comparative,
    }
    store, client, calls = _setup_fpi_client(monkeypatch, tmp_path, EUR_FPI_CIK, rows, extra)
    res = fetch_sec_fpi_6k_quarters(EUR_FPI_CIK, client, store, utc_now=lambda: AT)
    assert not any(u.endswith(("R1.xml", "MetaLinks.xml")) for u in calls)
    assert len(res.records) == 1
    rec = res.records[0]
    assert rec.source_kind is SecFpi6KSourceKind.XBRL_ATTACHMENT
    assert rec.period_end == date(2025, 3, 31)
    # Prior-year comparative 2024-03-31 revenue is excluded; cover-page DEI shares (2025-04-12) retained
    rev_rows = [m for m in rec.metrics if m.metric == "revenue"]
    assert len(rev_rows) == 1
    assert rev_rows[0].value == Decimal(7_700_000_000)
    assert rev_rows[0].period_end == date(2025, 3, 31)
    dei_rows = [m for m in rec.metrics if m.metric == "dei_common_shares_outstanding"]
    assert len(dei_rows) == 1
    assert dei_rows[0].value == Decimal(393_200_000)
    assert dei_rows[0].period_end == date(2025, 4, 12)

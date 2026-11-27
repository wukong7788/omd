from __future__ import annotations

import json
import zipfile
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ohmydata.cli import main as cli_main
from ohmydata.providers.sec import (
    BULK_FUNDAMENTALS_USD_DATASET_SCHEMA,
    BULK_FUNDAMENTALS_USD_WRITER_PROFILE,
    FX_FILING_IMPLIED_SOURCE,
    SecBulkFundamentalRow,
    SecBulkJoinedFact,
    SecFxDatePolicy,
    SecFxFlowMethod,
    SecFxRateObservation,
    SecFxRateTable,
    SecFxTranslationStatus,
    build_bulk_companyfacts_dataset,
    build_usd_translated_fundamentals_dataset,
    extract_filing_implied_fx_rates,
    load_fx_rate_table,
    translate_bulk_dataset_to_usd,
    translate_bulk_fundamentals_to_usd,
    validate_usd_translated_fundamentals_dataset,
    write_usd_translated_fundamentals_dataset,
)


def _row(
    *,
    cik: str = "0001046179",
    symbols: tuple[str, ...] = ("TSM",),
    accession_number: str = "0001046179-25-000001",
    form: str = "20-F",
    filing_date: date = date(2025, 4, 15),
    report_date: date = date(2024, 12, 31),
    accepted_at: datetime = datetime(2025, 4, 15, 16, 0, tzinfo=UTC),
    fiscal_year: int = 2024,
    fiscal_period: str = "FY",
    period_kind: str = "FY",
    period_start: date | None = date(2024, 1, 1),
    period_end: date = date(2024, 12, 31),
    taxonomy: str = "ifrs-full",
    reporting_currency: str | None = "TWD",
    revenue: Decimal | None = Decimal(1000000),
    gross_profit: Decimal | None = Decimal(550000),
    operating_income: Decimal | None = Decimal(450000),
    net_income: Decimal | None = Decimal(400000),
    ocf: Decimal | None = Decimal(500000),
    capex: Decimal | None = Decimal(200000),
    total_assets: Decimal | None = Decimal(3000000),
    total_debt: Decimal | None = Decimal(600000),
    diluted_shares: Decimal | None = Decimal(5000000),
    eps_diluted: Decimal | None = Decimal("20.00"),
    dei_shares_outstanding: Decimal | None = Decimal(5100000),
    dei_shares_outstanding_end: date | None = date(2025, 3, 31),
) -> SecBulkFundamentalRow:
    return SecBulkFundamentalRow(
        cik=cik,
        entity_name="Synthetic Issuer",
        symbols=symbols,
        accession_number=accession_number,
        form=form,
        is_amendment=form.endswith("/A"),
        filing_date=filing_date,
        report_date=report_date,
        accepted_at=accepted_at,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        period_kind=period_kind,
        period_start=period_start,
        period_end=period_end,
        taxonomy=taxonomy,
        reporting_currency=reporting_currency,
        revenue=revenue,
        gross_profit=gross_profit,
        operating_income=operating_income,
        net_income=net_income,
        ocf=ocf,
        capex=capex,
        total_assets=total_assets,
        total_debt=total_debt,
        diluted_shares=diluted_shares,
        eps_diluted=eps_diluted,
        dei_shares_outstanding=dei_shares_outstanding,
        dei_shares_outstanding_end=dei_shares_outstanding_end,
    )


def _fact(
    *,
    cik: str = "0001046179",
    accession_number: str = "0001046179-25-000001",
    namespace: str = "ifrs-full",
    tag: str = "Revenue",
    unit: str = "TWD",
    currency: str | None = "TWD",
    value: Decimal = Decimal(1000000),
    period_start: date | None = date(2024, 1, 1),
    period_end: date = date(2024, 12, 31),
    filing_date: date = date(2025, 4, 15),
    accepted_at: datetime = datetime(2025, 4, 15, 16, 0, tzinfo=UTC),
) -> SecBulkJoinedFact:
    return SecBulkJoinedFact(
        cik=cik,
        entity_name="Synthetic Issuer",
        symbols=("TSM",),
        accession_number=accession_number,
        form="20-F",
        is_amendment=False,
        filing_date=filing_date,
        report_date=period_end,
        accepted_at=accepted_at,
        namespace=namespace,
        tag=tag,
        unit=unit,
        currency=currency,
        value=value,
        period_start=period_start,
        period_end=period_end,
        fiscal_year=2024,
        fiscal_period="FY",
        frame=None,
        taxonomy=namespace,
    )


def test_native_usd_passthrough_exact_zero_rounding_drift() -> None:
    src = _row(
        cik="0000320193",
        symbols=("AAPL",),
        form="10-K",
        taxonomy="us-gaap",
        reporting_currency="USD",
        revenue=Decimal("391035000000.123456789"),
        eps_diluted=Decimal("6.0812345"),
        diluted_shares=Decimal(15408095000),
        dei_shares_outstanding=Decimal(15115823000),
    )
    res = translate_bulk_fundamentals_to_usd([src])
    assert res.counts[SecFxTranslationStatus.NATIVE_USD.value] == 1
    out = res.rows[0]
    assert out.fx_status is SecFxTranslationStatus.NATIVE_USD
    assert out.original_currency == "USD"
    assert out.fx_flow_rate_to_usd == Decimal(1)
    assert out.fx_bs_rate_to_usd == Decimal(1)
    assert out.fx_flow_source == "NATIVE_USD"
    assert out.fx_bs_source == "NATIVE_USD"
    assert out.fx_flow_date is None
    assert out.fx_bs_date is None
    assert out.revenue_usd == Decimal("391035000000.123456789")
    assert out.eps_diluted_usd == Decimal("6.0812345")
    assert out.total_assets_usd == src.total_assets
    assert out.diluted_shares == Decimal(15408095000)
    assert out.dei_shares_outstanding == Decimal(15115823000)


def test_filing_implied_dual_currency_separate_flow_and_bs_rates() -> None:
    # CNY 20-F where flow implied rate = 0.140 and BS implied rate = 0.138
    cik = "0001577552"
    accn = "0001577552-25-000001"
    row = _row(
        cik=cik,
        symbols=("BABA",),
        accession_number=accn,
        taxonomy="us-gaap",
        reporting_currency="CNY",
        revenue=Decimal(1000000),
        net_income=Decimal(200000),
        total_assets=Decimal(5000000),
        total_debt=Decimal(1000000),
        eps_diluted=Decimal("10.00"),
        diluted_shares=Decimal(20000),
        dei_shares_outstanding=Decimal(21000),
    )
    facts = [
        # Duration flow pairs -> 0.140
        _fact(
            cik=cik,
            accession_number=accn,
            namespace="us-gaap",
            tag="Revenues",
            unit="CNY",
            currency="CNY",
            value=Decimal(1000000),
        ),
        _fact(
            cik=cik,
            accession_number=accn,
            namespace="us-gaap",
            tag="Revenues",
            unit="USD",
            currency="USD",
            value=Decimal(140000),
        ),
        _fact(
            cik=cik,
            accession_number=accn,
            namespace="us-gaap",
            tag="NetIncomeLoss",
            unit="CNY",
            currency="CNY",
            value=Decimal(200000),
        ),
        _fact(
            cik=cik,
            accession_number=accn,
            namespace="us-gaap",
            tag="NetIncomeLoss",
            unit="USD",
            currency="USD",
            value=Decimal(28000),
        ),
        # Instant BS pairs -> 0.138
        _fact(
            cik=cik,
            accession_number=accn,
            namespace="us-gaap",
            tag="Assets",
            unit="CNY",
            currency="CNY",
            value=Decimal(5000000),
            period_start=None,
        ),
        _fact(
            cik=cik,
            accession_number=accn,
            namespace="us-gaap",
            tag="Assets",
            unit="USD",
            currency="USD",
            value=Decimal(690000),
            period_start=None,
        ),
        # Per-share EPS fact with rounded/ADR-skewed ratio must be ignored for implied FX extraction
        _fact(
            cik=cik,
            accession_number=accn,
            namespace="us-gaap",
            tag="EarningsPerShareDiluted",
            unit="CNY/shares",
            currency="CNY",
            value=Decimal("10.00"),
        ),
        _fact(
            cik=cik,
            accession_number=accn,
            namespace="us-gaap",
            tag="EarningsPerShareDiluted",
            unit="USD/shares",
            currency="USD",
            value=Decimal("11.20"),
        ),
    ]
    res = translate_bulk_fundamentals_to_usd([row], joined_facts=facts)
    assert res.counts[SecFxTranslationStatus.TRANSLATED_FILING_IMPLIED.value] == 1
    out = res.rows[0]
    assert out.fx_status is SecFxTranslationStatus.TRANSLATED_FILING_IMPLIED
    assert out.fx_flow_rate_to_usd == Decimal("0.14")
    assert out.fx_bs_rate_to_usd == Decimal("0.138")
    assert out.fx_flow_source == FX_FILING_IMPLIED_SOURCE
    assert out.fx_bs_source == FX_FILING_IMPLIED_SOURCE
    assert out.revenue_usd == Decimal(140000)
    assert out.net_income_usd == Decimal(28000)
    assert out.total_assets_usd == Decimal(690000)
    assert out.total_debt_usd == Decimal(138000)
    # EPS translated by flow rate; share counts NEVER multiplied by FX!
    assert out.eps_diluted_usd == Decimal("1.40")
    assert out.diluted_shares == Decimal(20000)
    assert out.dei_shares_outstanding == Decimal(21000)


def test_filing_implied_spread_over_2pct_fails_closed_as_ambiguous() -> None:
    cik = "0001046179"
    accn = "0001046179-25-000002"
    row = _row(cik=cik, accession_number=accn, reporting_currency="TWD")
    facts = [
        # Tag 1 implies 0.030, Tag 2 implies 0.033 (10% spread > 2%)
        _fact(
            cik=cik,
            accession_number=accn,
            tag="Revenue",
            unit="TWD",
            currency="TWD",
            value=Decimal(1000000),
        ),
        _fact(
            cik=cik,
            accession_number=accn,
            tag="Revenue",
            unit="USD",
            currency="USD",
            value=Decimal(30000),
        ),
        _fact(
            cik=cik,
            accession_number=accn,
            tag="ProfitLoss",
            unit="TWD",
            currency="TWD",
            value=Decimal(400000),
        ),
        _fact(
            cik=cik,
            accession_number=accn,
            tag="ProfitLoss",
            unit="USD",
            currency="USD",
            value=Decimal(13200),
        ),
        # BS pair consistent at 0.031
        _fact(
            cik=cik,
            accession_number=accn,
            tag="Assets",
            unit="TWD",
            currency="TWD",
            value=Decimal(3000000),
            period_start=None,
        ),
        _fact(
            cik=cik,
            accession_number=accn,
            tag="Assets",
            unit="USD",
            currency="USD",
            value=Decimal(93000),
            period_start=None,
        ),
    ]
    # Even when external FX table is supplied, ambiguous filing-implied rate MUST fail closed
    fx_table = SecFxRateTable.from_observations(
        [SecFxRateObservation("TWD", date(2024, 12, 31), Decimal("0.031"), "test_fx")]
    )
    implied = extract_filing_implied_fx_rates(facts)
    flow_implied = implied[(cik, accn, "TWD")].flow
    assert flow_implied is not None
    assert flow_implied.status == "AMBIGUOUS"

    res = translate_bulk_fundamentals_to_usd([row], joined_facts=facts, fx_table=fx_table)
    out = res.rows[0]
    assert out.fx_status is SecFxTranslationStatus.AMBIGUOUS_FX_RATE
    assert out.fx_flow_rate_to_usd is None
    assert out.revenue_usd is None
    assert out.net_income_usd is None
    assert out.eps_diluted_usd is None
    # Share counts remain intact
    assert out.diluted_shares == row.diluted_shares
    prov = json.loads(out.fx_provenance_json)
    assert prov["flow"]["status"] == "AMBIGUOUS"


def test_filing_implied_cross_kind_opt_in_and_external_fallback() -> None:
    cik = "0001046179"
    accn = "0001046179-25-000003"
    row = _row(cik=cik, accession_number=accn, reporting_currency="TWD")
    # Only flow dual-currency fact exists; no BS dual-currency fact
    facts = [
        _fact(
            cik=cik,
            accession_number=accn,
            tag="Revenue",
            unit="TWD",
            currency="TWD",
            value=Decimal(1000000),
        ),
        _fact(
            cik=cik,
            accession_number=accn,
            tag="Revenue",
            unit="USD",
            currency="USD",
            value=Decimal(31000),
        ),
    ]
    # Default: no cross-kind implied, no fx_table -> MISSING_FX_RATE for missing BS rate
    res_missing = translate_bulk_fundamentals_to_usd([row], joined_facts=facts)
    assert res_missing.rows[0].fx_status is SecFxTranslationStatus.MISSING_FX_RATE
    assert res_missing.rows[0].fx_flow_rate_to_usd == Decimal("0.031")
    assert res_missing.rows[0].fx_bs_rate_to_usd is None
    assert res_missing.rows[0].total_assets_usd is None

    # Explicit opt-in allow_cross_kind_implied=True uses flow rate for BS with explicit provenance
    res_cross = translate_bulk_fundamentals_to_usd(
        [row], joined_facts=facts, allow_cross_kind_implied=True
    )
    out_cross = res_cross.rows[0]
    assert out_cross.fx_status is SecFxTranslationStatus.TRANSLATED_FILING_IMPLIED
    assert out_cross.fx_bs_rate_to_usd == Decimal("0.031")
    assert out_cross.fx_bs_source == f"{FX_FILING_IMPLIED_SOURCE}:CROSS_KIND_FROM_FLOW"
    assert out_cross.total_assets_usd == Decimal(93000)

    # With external fx_table and allow_cross_kind_implied=False -> BS uses external FX
    fx_table = SecFxRateTable.from_observations(
        [SecFxRateObservation("TWD", date(2024, 12, 31), Decimal("0.0305"), "fed_h10")]
    )
    res_hybrid = translate_bulk_fundamentals_to_usd([row], joined_facts=facts, fx_table=fx_table)
    out_hybrid = res_hybrid.rows[0]
    assert out_hybrid.fx_status is SecFxTranslationStatus.TRANSLATED_EXTERNAL_FX
    assert out_hybrid.fx_flow_source == FX_FILING_IMPLIED_SOURCE
    assert out_hybrid.fx_bs_source == "fed_h10"
    assert out_hybrid.fx_bs_rate_to_usd == Decimal("0.0305")


def test_external_pit_fx_policies_period_average_spot_staleness_and_anti_lookahead() -> None:
    eur_obs = [
        SecFxRateObservation("EUR", date(2024, 1, 15), Decimal("1.08"), "ecb"),
        SecFxRateObservation("EUR", date(2024, 6, 15), Decimal("1.10"), "ecb"),
        SecFxRateObservation("EUR", date(2024, 12, 27), Decimal("1.06"), "ecb"),
        # Filing date spot observation available before acceptance
        SecFxRateObservation(
            "EUR",
            date(2025, 2, 18),
            Decimal("1.04"),
            "ecb",
            available_at=datetime(2025, 2, 18, 22, 0, tzinfo=UTC),
        ),
        # Future observation AFTER accepted_at -> MUST NEVER BE USED (anti-lookahead!)
        SecFxRateObservation(
            "EUR",
            date(2025, 2, 21),
            Decimal("1.99"),
            "ecb",
            available_at=datetime(2025, 2, 21, 12, 0, tzinfo=UTC),
        ),
    ]
    fx_table = SecFxRateTable.from_observations(eur_obs)

    row = _row(
        cik="0000937966",
        symbols=("ASML",),
        accession_number="0000937966-25-000001",
        filing_date=date(2025, 2, 20),
        report_date=date(2024, 12, 31),
        accepted_at=datetime(2025, 2, 20, 12, 0, tzinfo=UTC),
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        reporting_currency="EUR",
        revenue=Decimal(3000),
        total_assets=Decimal(10000),
        eps_diluted=Decimal(15),
        diluted_shares=Decimal(400),
    )

    # 1. PERIOD_AVERAGE + PERIOD_END_SPOT: flow = mean(1.08, 1.10, 1.06) = 1.08, bs = 1.06 (2024-12-27, 4d stale <= 10)
    res_avg = translate_bulk_fundamentals_to_usd(
        [row],
        fx_table=fx_table,
        date_policy=SecFxDatePolicy.PERIOD_END_SPOT,
        flow_method=SecFxFlowMethod.PERIOD_AVERAGE,
        max_staleness_days=10,
    )
    out_avg = res_avg.rows[0]
    assert out_avg.fx_status is SecFxTranslationStatus.TRANSLATED_EXTERNAL_FX
    assert out_avg.fx_flow_rate_to_usd == Decimal("1.08")
    assert out_avg.fx_flow_date == date(2024, 12, 27)
    assert out_avg.fx_bs_rate_to_usd == Decimal("1.06")
    assert out_avg.fx_bs_date == date(2024, 12, 27)
    assert out_avg.revenue_usd == Decimal("3240.00")
    assert out_avg.total_assets_usd == Decimal("10600.00")
    assert out_avg.eps_diluted_usd == Decimal("16.20")
    assert out_avg.diluted_shares == Decimal(400)

    # 2. FILING_DATE_SPOT + SPOT: picks 2025-02-18 (1.04), NEVER 2025-02-21 (1.99)
    res_filing = translate_bulk_fundamentals_to_usd(
        [row],
        fx_table=fx_table,
        date_policy=SecFxDatePolicy.FILING_DATE_SPOT,
        flow_method=SecFxFlowMethod.SPOT,
        max_staleness_days=10,
    )
    out_filing = res_filing.rows[0]
    assert out_filing.fx_status is SecFxTranslationStatus.TRANSLATED_EXTERNAL_FX
    assert out_filing.fx_flow_rate_to_usd == Decimal("1.04")
    assert out_filing.fx_bs_rate_to_usd == Decimal("1.04")
    assert out_filing.fx_bs_date == date(2025, 2, 18)

    # 3. Tight max_staleness_days=2 makes 2024-12-27 (4d before 2024-12-31) stale -> MISSING_FX_RATE
    res_stale = translate_bulk_fundamentals_to_usd(
        [row],
        fx_table=fx_table,
        date_policy=SecFxDatePolicy.PERIOD_END_SPOT,
        flow_method=SecFxFlowMethod.SPOT,
        max_staleness_days=2,
    )
    assert res_stale.rows[0].fx_status is SecFxTranslationStatus.MISSING_FX_RATE
    assert res_stale.rows[0].revenue_usd is None
    assert res_stale.rows[0].total_assets_usd is None

    # 4. Anti-lookahead: period_end after accepted_at.date() or available_at > accepted_at is strictly blocked!
    early_row = _row(
        cik="0000937966",
        symbols=("ASML",),
        accession_number="0000937966-25-000002",
        filing_date=date(2025, 2, 18),
        report_date=date(2025, 2, 22),
        accepted_at=datetime(
            2025, 2, 18, 12, 0, tzinfo=UTC
        ),  # BEFORE 22:00 available_at on 2025-02-18!
        period_start=date(2025, 2, 1),
        period_end=date(2025, 2, 22),
        reporting_currency="EUR",
    )
    res_pit = translate_bulk_fundamentals_to_usd(
        [early_row],
        fx_table=fx_table,
        date_policy=SecFxDatePolicy.FILING_DATE_SPOT,
        flow_method=SecFxFlowMethod.PERIOD_AVERAGE,
        max_staleness_days=5,
        allow_spot_fallback=True,
    )
    # 2025-02-18 obs has available_at=22:00Z > accepted_at=12:00Z, 2025-02-21 is future, 2024-12-27 is >5d stale!
    assert res_pit.rows[0].fx_status is SecFxTranslationStatus.MISSING_FX_RATE
    assert res_pit.rows[0].revenue_usd is None

    # 5. PERIOD_AVERAGE fallback to spot when period_start is None
    no_start_row = _row(
        cik="0000937966",
        symbols=("ASML",),
        reporting_currency="EUR",
        period_start=None,
        period_end=date(2024, 12, 31),
        accepted_at=datetime(2025, 2, 20, 12, 0, tzinfo=UTC),
    )
    res_fb_on = translate_bulk_fundamentals_to_usd(
        [no_start_row],
        fx_table=fx_table,
        flow_method=SecFxFlowMethod.PERIOD_AVERAGE,
        allow_spot_fallback=True,
    )
    assert res_fb_on.rows[0].fx_status is SecFxTranslationStatus.TRANSLATED_EXTERNAL_FX
    assert (
        json.loads(res_fb_on.rows[0].fx_provenance_json)["flow"]["basis"]
        == "period_average_fallback_spot"
    )

    res_fb_off = translate_bulk_fundamentals_to_usd(
        [no_start_row],
        fx_table=fx_table,
        flow_method=SecFxFlowMethod.PERIOD_AVERAGE,
        allow_spot_fallback=False,
    )
    assert res_fb_off.rows[0].fx_status is SecFxTranslationStatus.MISSING_FX_RATE
    assert res_fb_off.rows[0].revenue_usd is None


def test_missing_or_multi_currency_fails_closed() -> None:
    r_none = _row(accession_number="0001-25-000001", reporting_currency=None)
    r_multi = _row(accession_number="0001-25-000002", reporting_currency="MULTI")
    res = translate_bulk_fundamentals_to_usd([r_none, r_multi])
    assert res.counts[SecFxTranslationStatus.MISSING_CURRENCY.value] == 2
    for out in res.rows:
        assert out.fx_status is SecFxTranslationStatus.MISSING_CURRENCY
        assert out.revenue_usd is None
        assert out.total_assets_usd is None
        assert out.diluted_shares == Decimal(5000000)


def test_load_fx_rate_table_csv_parquet_and_rejections(tmp_path: Path) -> None:
    csv_path = tmp_path / "fx.csv"
    csv_path.write_text(
        "currency,date,usd_per_unit,source,available_at\n"
        "EUR,2024-12-31,1.0412,ecb_csv,2024-12-31T21:00:00Z\n"
        "DKK,2024-12-31,0.1396,ecb_csv,2024-12-31T21:00:00+00:00\n",
        encoding="utf-8",
    )
    table_csv = load_fx_rate_table(csv_path)
    assert table_csv.currencies == ("DKK", "EUR")
    assert len(table_csv) == 2
    obs = table_csv.lookup_spot("EUR", date(2024, 12, 31))
    assert obs is not None and obs.usd_per_unit == Decimal("1.0412") and obs.source == "ecb_csv"

    pq_path = tmp_path / "fx.parquet"
    pa_tbl = pa.table(
        {
            "currency": ["CNY", "TWD"],
            "rate_date": [date(2024, 12, 31), date(2024, 12, 31)],
            "usd_per_unit": ["0.1371", "0.0305"],
        }
    )
    pq.write_table(pa_tbl, pq_path)
    table_pq = load_fx_rate_table(pq_path)
    cny_obs = table_pq.lookup_spot("CNY", date(2024, 12, 31))
    assert cny_obs is not None and cny_obs.usd_per_unit == Decimal("0.1371")
    assert cny_obs.source == "external_parquet"

    # Reject conflicting duplicate (currency, rate_date)
    dup_csv = tmp_path / "dup.csv"
    dup_csv.write_text(
        "currency,rate_date,usd_per_unit\nEUR,2024-12-31,1.04\nEUR,2024-12-31,1.05\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="conflicting duplicate"):
        load_fx_rate_table(dup_csv)

    # Reject non-positive rate
    bad_rate = tmp_path / "bad_rate.csv"
    bad_rate.write_text("currency,rate_date,usd_per_unit\nEUR,2024-12-31,0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="strictly positive"):
        load_fx_rate_table(bad_rate)

    # Reject missing required columns
    bad_cols = tmp_path / "bad_cols.csv"
    bad_cols.write_text("currency,rate_date\nEUR,2024-12-31\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required columns"):
        load_fx_rate_table(bad_cols)

    # Reject naive available_at
    naive_csv = tmp_path / "naive.csv"
    naive_csv.write_text(
        "currency,rate_date,usd_per_unit,available_at\nEUR,2024-12-31,1.04,2024-12-31T21:00:00\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        load_fx_rate_table(naive_csv)


def test_write_validate_immutability_and_tamper_detection(tmp_path: Path) -> None:
    usd_row = _row(cik="0000320193", symbols=("AAPL",), form="10-K", reporting_currency="USD")
    res = translate_bulk_fundamentals_to_usd([usd_row])
    out_dir = tmp_path / "usd-dataset"
    manifest = write_usd_translated_fundamentals_dataset(out_dir, res)
    assert manifest["dataset_schema"] == BULK_FUNDAMENTALS_USD_DATASET_SCHEMA
    assert manifest["writer_profile"] == BULK_FUNDAMENTALS_USD_WRITER_PROFILE
    assert validate_usd_translated_fundamentals_dataset(out_dir) == manifest

    # Idempotent rewrite succeeds
    assert write_usd_translated_fundamentals_dataset(out_dir, res) == manifest

    # Mutating result to same output directory raises ValueError (immutability!)
    different_res = translate_bulk_fundamentals_to_usd([usd_row, _row(reporting_currency=None)])
    with pytest.raises(ValueError, match="immutable USD fundamentals dataset differs"):
        write_usd_translated_fundamentals_dataset(out_dir, different_res)

    # Tampering parquet bytes fails hash validation
    tampered_dir = tmp_path / "usd-tampered"
    write_usd_translated_fundamentals_dataset(tampered_dir, res)
    pq_file = tampered_dir / "fundamentals_usd.parquet"
    pq_file.write_bytes(pq_file.read_bytes() + b"x")
    with pytest.raises(ValueError, match="hash or size mismatch"):
        validate_usd_translated_fundamentals_dataset(tampered_dir)

    # Tampering manifest counts fails validation
    bad_manifest_dir = tmp_path / "usd-bad-manifest"
    write_usd_translated_fundamentals_dataset(bad_manifest_dir, res)
    m_path = bad_manifest_dir / "manifest.json"
    m = json.loads(m_path.read_text(encoding="utf-8"))
    m["status_counts"]["NATIVE_USD"] = 999
    m_path.write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(ValueError, match="status_counts mismatch"):
        validate_usd_translated_fundamentals_dataset(bad_manifest_dir)


def _make_mini_bulk_dataset(tmp_path: Path) -> Path:
    cf_zip = tmp_path / "companyfacts.zip"
    sub_zip = tmp_path / "submissions.zip"
    baba_cik = "0001577552"
    asml_cik = "0000937966"
    baba_accn = f"{baba_cik}-25-000001"
    asml_accn = f"{asml_cik}-25-000001"

    baba_cf = {
        "cik": int(baba_cik),
        "entityName": "Synthetic Alibaba Group",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "CNY": [
                            {
                                "val": 1_000_000,
                                "accn": baba_accn,
                                "form": "20-F",
                                "fy": 2024,
                                "fp": "FY",
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "filed": "2025-06-20",
                            }
                        ],
                        "USD": [
                            {
                                "val": 140_000,
                                "accn": baba_accn,
                                "form": "20-F",
                                "fy": 2024,
                                "fp": "FY",
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "filed": "2025-06-20",
                            }
                        ],
                    }
                },
                "Assets": {
                    "units": {
                        "CNY": [
                            {
                                "val": 2_000_000,
                                "accn": baba_accn,
                                "form": "20-F",
                                "fy": 2024,
                                "fp": "FY",
                                "end": "2024-12-31",
                                "filed": "2025-06-20",
                            }
                        ],
                        "USD": [
                            {
                                "val": 276_000,
                                "accn": baba_accn,
                                "form": "20-F",
                                "fy": 2024,
                                "fp": "FY",
                                "end": "2024-12-31",
                                "filed": "2025-06-20",
                            }
                        ],
                    }
                },
            }
        },
    }
    asml_cf = {
        "cik": int(asml_cik),
        "entityName": "Synthetic ASML NV",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "EUR": [
                            {
                                "val": 500_000,
                                "accn": asml_accn,
                                "form": "20-F",
                                "fy": 2024,
                                "fp": "FY",
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "filed": "2025-02-20",
                            }
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "EUR": [
                            {
                                "val": 1_000_000,
                                "accn": asml_accn,
                                "form": "20-F",
                                "fy": 2024,
                                "fp": "FY",
                                "end": "2024-12-31",
                                "filed": "2025-02-20",
                            }
                        ]
                    }
                },
            }
        },
    }
    baba_sub = {
        "cik": baba_cik,
        "name": "Synthetic Alibaba Group",
        "tickers": ["BABA"],
        "filings": {
            "recent": {
                "accessionNumber": [baba_accn],
                "form": ["20-F"],
                "filingDate": ["2025-06-20"],
                "reportDate": ["2024-12-31"],
                "primaryDocument": ["baba-20f.htm"],
                "acceptanceDateTime": ["2025-06-20T12:00:00.000Z"],
            },
            "files": [],
        },
    }
    asml_sub = {
        "cik": asml_cik,
        "name": "Synthetic ASML NV",
        "tickers": ["ASML"],
        "filings": {
            "recent": {
                "accessionNumber": [asml_accn],
                "form": ["20-F"],
                "filingDate": ["2025-02-20"],
                "reportDate": ["2024-12-31"],
                "primaryDocument": ["asml-20f.htm"],
                "acceptanceDateTime": ["2025-02-20T12:00:00.000Z"],
            },
            "files": [],
        },
    }
    with zipfile.ZipFile(cf_zip, "w") as zf:
        zf.writestr(f"CIK{baba_cik}.json", json.dumps(baba_cf))
        zf.writestr(f"CIK{asml_cik}.json", json.dumps(asml_cf))
    with zipfile.ZipFile(sub_zip, "w") as zf:
        zf.writestr(f"CIK{baba_cik}.json", json.dumps(baba_sub))
        zf.writestr(f"CIK{asml_cik}.json", json.dumps(asml_sub))

    bulk_dir = tmp_path / "bulk-src"
    build_bulk_companyfacts_dataset(cf_zip, sub_zip, bulk_dir)
    return bulk_dir


def test_cli_translate_usd_and_dataset_end_to_end_offline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bulk_dir = _make_mini_bulk_dataset(tmp_path)
    fx_csv = tmp_path / "fx-rates.csv"
    fx_csv.write_text(
        "currency,rate_date,usd_per_unit,source\n"
        "EUR,2024-06-30,1.08,ecb\n"
        "EUR,2024-12-31,1.04,ecb\n",
        encoding="utf-8",
    )

    # 1. SDK translate_bulk_dataset_to_usd without external FX: BABA implied, ASML missing
    res_no_fx = translate_bulk_dataset_to_usd(bulk_dir, auto_fetch_fx=False)
    by_sym = {r.symbols[0]: r for r in res_no_fx.rows}
    assert by_sym["BABA"].fx_status is SecFxTranslationStatus.TRANSLATED_FILING_IMPLIED
    assert by_sym["BABA"].revenue_usd == Decimal(140000)
    assert by_sym["BABA"].total_assets_usd == Decimal(276000)
    assert by_sym["ASML"].fx_status is SecFxTranslationStatus.MISSING_FX_RATE
    assert by_sym["ASML"].revenue_usd is None

    # 2. CLI end-to-end with external FX CSV
    out_dir = tmp_path / "bulk-usd-out"
    rc = cli_main(
        [
            "sec",
            "financials",
            "translate-usd",
            "--bulk-dataset",
            str(bulk_dir),
            "--output",
            str(out_dir),
            "--fx-rates",
            str(fx_csv),
            "--date-policy",
            "PERIOD_END_SPOT",
            "--flow-method",
            "PERIOD_AVERAGE",
            "--max-staleness-days",
            "10",
            "--json",
            "--quiet",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["dataset_schema"] == BULK_FUNDAMENTALS_USD_DATASET_SCHEMA
    assert payload["status_counts"]["TRANSLATED_FILING_IMPLIED"] == 1
    assert payload["status_counts"]["TRANSLATED_EXTERNAL_FX"] == 1

    validated = validate_usd_translated_fundamentals_dataset(out_dir)
    assert validated["counts"]["total_rows"] == 2
    rows_out = pq.read_table(out_dir / "fundamentals_usd.parquet").to_pylist()
    out_by_sym = {r["symbols"][0]: r for r in rows_out}
    assert Decimal(out_by_sym["ASML"]["fx_flow_rate_to_usd"]) == Decimal("1.06")
    assert Decimal(out_by_sym["ASML"]["fx_bs_rate_to_usd"]) == Decimal("1.04")
    assert Decimal(out_by_sym["ASML"]["revenue_usd"]) == Decimal("530000.00")
    assert Decimal(out_by_sym["ASML"]["total_assets_usd"]) == Decimal("1040000.00")

    # 3. CLI with --no-filing-implied forces BABA to MISSING_FX_RATE when CNY is not in fx_csv
    out_no_imp = tmp_path / "bulk-usd-no-implied"
    rc2 = cli_main(
        [
            "sec",
            "financials",
            "translate-usd",
            "--bulk-dataset",
            str(bulk_dir),
            "--output",
            str(out_no_imp),
            "--fx-rates",
            str(fx_csv),
            "--no-filing-implied",
            "--json",
            "--quiet",
        ]
    )
    assert rc2 == 0
    payload2 = json.loads(capsys.readouterr().out)
    assert payload2["status_counts"]["TRANSLATED_EXTERNAL_FX"] == 1
    assert payload2["status_counts"]["MISSING_FX_RATE"] == 1

    # 4. SDK build helper matches CLI output cleanly
    sdk_dir = tmp_path / "bulk-usd-sdk"
    sdk_manifest = build_usd_translated_fundamentals_dataset(
        bulk_dir,
        sdk_dir,
        fx_table=load_fx_rate_table(fx_csv),
    )
    assert sdk_manifest["status_counts"]["TRANSLATED_FILING_IMPLIED"] == 1
    assert sdk_manifest["status_counts"]["TRANSLATED_EXTERNAL_FX"] == 1


def test_non_utc_accepted_at_normalized_and_naive_rejected(tmp_path: Path) -> None:
    tz_plus_8 = timezone(timedelta(hours=8))
    # 2025-02-19T02:00:00+08:00 == 2025-02-18T18:00:00Z (UTC date is 2025-02-18!)
    accepted_local = datetime(2025, 2, 19, 2, 0, tzinfo=tz_plus_8)
    row = _row(
        cik="0000937966",
        symbols=("ASML",),
        accession_number="0000937966-25-000099",
        filing_date=date(2025, 2, 19),
        report_date=date(2025, 2, 19),
        accepted_at=accepted_local,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 2, 19),
        reporting_currency="EUR",
    )
    fx_table = SecFxRateTable.from_observations(
        [
            SecFxRateObservation("EUR", date(2025, 2, 18), Decimal("1.05"), "ecb"),
            SecFxRateObservation("EUR", date(2025, 2, 19), Decimal("1.99"), "ecb"),
        ]
    )
    res = translate_bulk_fundamentals_to_usd(
        [row],
        fx_table=fx_table,
        date_policy=SecFxDatePolicy.PERIOD_END_SPOT,
        flow_method=SecFxFlowMethod.SPOT,
    )
    out = res.rows[0]
    assert out.accepted_at == datetime(2025, 2, 18, 18, 0, tzinfo=UTC)
    assert out.fx_flow_date == date(2025, 2, 18)
    assert out.fx_bs_date == date(2025, 2, 18)
    assert out.fx_flow_rate_to_usd == Decimal("1.05")
    prov = json.loads(out.fx_provenance_json)
    assert prov["flow"]["anchor_date"] == "2025-02-18"
    assert prov["flow"]["staleness_days"] == 0

    out_dir = tmp_path / "non-utc-roundtrip"
    write_usd_translated_fundamentals_dataset(out_dir, res)
    validated = validate_usd_translated_fundamentals_dataset(out_dir)
    assert validated["status_counts"]["TRANSLATED_EXTERNAL_FX"] == 1

    # Naive accepted_at MUST fail closed across NATIVE_USD, MISSING_CURRENCY, and non-USD paths
    naive_dt = datetime(2025, 2, 18, 18, 0)  # noqa: DTZ001
    for ccy in ("USD", None, "EUR"):
        with pytest.raises(ValueError, match="timezone-aware"):
            translate_bulk_fundamentals_to_usd([_row(accepted_at=naive_dt, reporting_currency=ccy)])


def test_three_currency_debt_tranches_ignored_for_implied_fx() -> None:
    cik = "0000892450"
    accn = "0001193125-21-142666"
    # Multi-currency debt tranches (KRW/SGD/USD) must NOT synthesize a bogus KRW/USD rate
    facts = [
        _fact(
            cik=cik,
            accession_number=accn,
            namespace="ifrs-full",
            tag="Borrowings",
            unit="KRW",
            currency="KRW",
            value=Decimal(7_316_000_000_000),
            period_start=None,
            period_end=date(2020, 12, 31),
        ),
        _fact(
            cik=cik,
            accession_number=accn,
            namespace="ifrs-full",
            tag="Borrowings",
            unit="SGD",
            currency="SGD",
            value=Decimal(284_000_000),
            period_start=None,
            period_end=date(2020, 12, 31),
        ),
        _fact(
            cik=cik,
            accession_number=accn,
            namespace="ifrs-full",
            tag="Borrowings",
            unit="USD",
            currency="USD",
            value=Decimal(519_600_000),
            period_start=None,
            period_end=date(2020, 12, 31),
        ),
    ]
    assert extract_filing_implied_fx_rates(facts) == {}

    row = _row(
        cik=cik,
        symbols=("KT",),
        accession_number=accn,
        filing_date=date(2021, 4, 29),
        report_date=date(2020, 12, 31),
        accepted_at=datetime(2021, 4, 29, 12, 0, tzinfo=UTC),
        fiscal_year=2020,
        period_start=date(2020, 1, 1),
        period_end=date(2020, 12, 31),
        reporting_currency="KRW",
        revenue=Decimal(1000),
        total_debt=Decimal(7_316_000_000_000),
    )
    fx_table = SecFxRateTable.from_observations(
        [SecFxRateObservation("KRW", date(2020, 12, 31), Decimal("0.00092"), "bok")]
    )
    res = translate_bulk_fundamentals_to_usd(
        [row],
        joined_facts=facts,
        fx_table=fx_table,
        flow_method=SecFxFlowMethod.SPOT,
    )
    out = res.rows[0]
    assert out.fx_status is SecFxTranslationStatus.TRANSLATED_EXTERNAL_FX
    assert out.fx_bs_rate_to_usd == Decimal("0.00092")
    assert out.fx_bs_source == "bok"


def test_filing_implied_fx_isolated_across_accessions_same_cik() -> None:
    cik = "0001577552"
    accn_dual = f"{cik}-24-000001"
    accn_single = f"{cik}-25-000001"
    row_dual = _row(
        cik=cik,
        symbols=("BABA",),
        accession_number=accn_dual,
        fiscal_year=2024,
        reporting_currency="CNY",
    )
    row_single = _row(
        cik=cik,
        symbols=("BABA",),
        accession_number=accn_single,
        fiscal_year=2025,
        reporting_currency="CNY",
    )
    facts = [
        _fact(
            cik=cik,
            accession_number=accn_dual,
            namespace="us-gaap",
            tag="Revenues",
            unit="CNY",
            currency="CNY",
            value=Decimal(1_000_000),
        ),
        _fact(
            cik=cik,
            accession_number=accn_dual,
            namespace="us-gaap",
            tag="Revenues",
            unit="USD",
            currency="USD",
            value=Decimal(140_000),
        ),
        _fact(
            cik=cik,
            accession_number=accn_dual,
            namespace="us-gaap",
            tag="Assets",
            unit="CNY",
            currency="CNY",
            value=Decimal(2_000_000),
            period_start=None,
        ),
        _fact(
            cik=cik,
            accession_number=accn_dual,
            namespace="us-gaap",
            tag="Assets",
            unit="USD",
            currency="USD",
            value=Decimal(280_000),
            period_start=None,
        ),
    ]
    res = translate_bulk_fundamentals_to_usd([row_dual, row_single], joined_facts=facts)
    by_accn = {r.accession_number: r for r in res.rows}
    assert by_accn[accn_dual].fx_status is SecFxTranslationStatus.TRANSLATED_FILING_IMPLIED
    assert by_accn[accn_single].fx_status is SecFxTranslationStatus.MISSING_FX_RATE
    assert by_accn[accn_single].fx_flow_rate_to_usd is None
    assert by_accn[accn_single].revenue_usd is None


def test_strict_bool_rejection_in_manifest_and_cli_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    usd_row = _row(cik="0000320193", symbols=("AAPL",), form="10-K", reporting_currency="USD")
    res = translate_bulk_fundamentals_to_usd([usd_row])
    out_dir = tmp_path / "bool-manifest"
    write_usd_translated_fundamentals_dataset(out_dir, res)
    m_path = out_dir / "manifest.json"
    m = json.loads(m_path.read_text(encoding="utf-8"))
    m["status_counts"]["NATIVE_USD"] = True
    m_path.write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(ValueError, match="status_counts mismatch"):
        validate_usd_translated_fundamentals_dataset(out_dir)

    bulk_dir = _make_mini_bulk_dataset(tmp_path)
    cfg = tmp_path / "bad-cfg.json"
    cfg.write_text(
        json.dumps(
            {
                "bulk_dataset": str(bulk_dir),
                "output": str(tmp_path / "bool-cli-out"),
                "max_staleness_days": True,
            }
        ),
        encoding="utf-8",
    )
    rc = cli_main(["sec", "financials", "translate-usd", "--config", str(cfg), "--quiet"])
    assert rc == 2
    assert "max-staleness-days" in capsys.readouterr().err

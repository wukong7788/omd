import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext

import pytest

from ohmydata.core import RequestSpec, SnapshotMode, SnapshotStore
from ohmydata.providers.sec._event_discovery_models import SecDiscoverySource
from ohmydata.providers.sec.financials import SecCompanyFinancialVintage, SecStatementRow
from ohmydata.providers.sec.quarterly import (
    SecCompanyEligibilityStatus,
    SecQuarterFieldStatus,
    SecQuarterlyFilingPeriodEvidence,
    SecQuarterlyNativeDurationEvidence,
    SecQuarterlyPeriodEvidence,
    SecQuarterlyProjectionStatus,
    SecQuarterlyVintageInput,
    SecQuarterPeriodKind,
    SecQuarterPeriodResolutionStatus,
    SecQuarterPeriodSourceKind,
    classify_sec_company_eligibility_from_root,
    derive_sec_quarterly_period_evidence,
    project_sec_quarterly_facts,
)
from ohmydata.providers.sec.ticker_cik import SEC_TICKER_CIK_URL, SecTickerCikMapping


def _input(
    accession: str,
    *,
    form: str,
    fiscal_year: int,
    fiscal_period: str,
    period_kind: SecQuarterPeriodKind,
    quarter: int,
    label: str,
    start: date | None,
    end: date,
    values: tuple[tuple[str, Decimal, str, str], ...],
) -> SecQuarterlyVintageInput:
    rows = tuple(
        SecStatementRow(
            statement_type=statement,
            standard_concept=concept,
            concept=concept,
            label=concept,
            value=value,
            value_native=str(value),
            unit=unit,
            period_start=start if period_kind is not SecQuarterPeriodKind.INSTANT else None,
            period_end=end,
            period_type="instant" if period_kind is SecQuarterPeriodKind.INSTANT else "duration",
            period_source="xbrl-context",
            context_ref=f"ctx-{concept}",
        )
        for concept, value, unit, statement in values
    )
    vintage = SecCompanyFinancialVintage(
        "ACME",
        "0000000001",
        "Acme Corporation",
        form,
        accession,
        end,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        period_end=end,
        accepted_at=datetime(fiscal_year + 1, 3, 1, tzinfo=UTC),
        rows=rows,
    )
    return SecQuarterlyVintageInput(
        vintage,
        ("a" * 63) + str(quarter),
        SecQuarterlyPeriodEvidence(
            accession,
            fiscal_year,
            quarter,
            period_kind,
            start,
            end,
            label,
        ),
    )


def test_independent_quarter_requires_declared_3m_context_and_keeps_missing() -> None:
    source = _input(
        "0000000001-24-000001",
        form="10-Q",
        fiscal_year=2024,
        fiscal_period="Q1",
        period_kind=SecQuarterPeriodKind.INDEPENDENT_3M,
        quarter=1,
        label="Three Months Ended",
        start=date(2024, 1, 1),
        end=date(2024, 3, 31),
        values=(("Revenue", Decimal("123.50"), "USD", "income_statement"),),
    )
    result = project_sec_quarterly_facts(
        (source,), periods=((2024, 1),), concepts=("Revenue", "NetIncome")
    )
    assert result.fields[0].status is SecQuarterFieldStatus.PRESENT
    assert result.fields[0].observations[0].value == Decimal("123.50")
    assert result.fields[1].status is SecQuarterFieldStatus.MISSING
    assert result.status is SecQuarterlyProjectionStatus.SEC_COMPANY_PARTIAL


def test_companyfacts_calendar_frame_does_not_override_fiscal_focus() -> None:
    source = _input(
        "0000000001-24-000001",
        form="10-Q",
        fiscal_year=2024,
        fiscal_period="Q1",
        period_kind=SecQuarterPeriodKind.INDEPENDENT_3M,
        quarter=1,
        label="Three Months Ended",
        start=date(2024, 10, 1),
        end=date(2024, 12, 31),
        values=(("Revenue", Decimal(10), "USD", "income_statement"),),
    )
    period = SecQuarterlyPeriodEvidence(
        source.vintage.accession_number,
        2024,
        1,
        SecQuarterPeriodKind.INDEPENDENT_3M,
        date(2024, 10, 1),
        date(2024, 12, 31),
        "CY2025Q2",
        SecQuarterPeriodSourceKind.SEC_COMPANYFACTS_FRAME,
    )
    assert period.fiscal_year == 2024
    assert period.fiscal_quarter == 1


def test_period_derivation_returns_unresolved_without_source_duration_label() -> None:
    source = _input(
        "0000000001-24-000001",
        form="10-Q",
        fiscal_year=2024,
        fiscal_period="Q1",
        period_kind=SecQuarterPeriodKind.INDEPENDENT_3M,
        quarter=1,
        label="Three Months Ended",
        start=date(2024, 1, 1),
        end=date(2024, 3, 31),
        values=(("Revenue", Decimal(123), "USD", "income_statement"),),
    )
    observation_id = source.source_observation_id
    raw = SecQuarterlyFilingPeriodEvidence(
        source.vintage.accession_number,
        observation_id,
        2024,
        "Q1",
        date(2024, 3, 31),
    )
    unresolved = derive_sec_quarterly_period_evidence(source.vintage, raw)
    assert unresolved.status is SecQuarterPeriodResolutionStatus.UNRESOLVED
    labeled = SecQuarterlyFilingPeriodEvidence(
        source.vintage.accession_number,
        observation_id,
        2024,
        "Q1",
        date(2024, 3, 31),
        (
            SecQuarterlyNativeDurationEvidence(
                "ctx-Revenue",
                date(2024, 1, 1),
                date(2024, 3, 31),
                "Three Months Ended",
                observation_id,
            ),
        ),
    )
    resolved = derive_sec_quarterly_period_evidence(source.vintage, labeled)
    assert resolved.status is SecQuarterPeriodResolutionStatus.RESOLVED
    assert resolved.evidence[0].period_kind is SecQuarterPeriodKind.INDEPENDENT_3M


def test_q4_uses_exact_bounded_usd_fy_minus_q3_ytd_and_preserves_accessions() -> None:
    fy = _input(
        "0000000001-25-000001",
        form="10-K",
        fiscal_year=2024,
        fiscal_period="FY",
        period_kind=SecQuarterPeriodKind.FY,
        quarter=4,
        label="Year Ended",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        values=(
            ("Revenue", Decimal("123456789012345678901.1234"), "iso4217:USD", "income_statement"),
            ("EPS", Decimal("4.25"), "USD/shares", "income_statement"),
            ("Assets", Decimal(1000), "USD", "balance_sheet"),
        ),
    )
    q3 = _input(
        "0000000001-24-000010",
        form="10-Q",
        fiscal_year=2024,
        fiscal_period="Q3",
        period_kind=SecQuarterPeriodKind.YTD_9M,
        quarter=3,
        label="Nine Months Ended",
        start=date(2024, 1, 1),
        end=date(2024, 9, 30),
        values=(
            ("Revenue", Decimal("123456789012345678900.1234"), "iso4217:USD", "income_statement"),
            ("EPS", Decimal("3.00"), "USD/shares", "income_statement"),
        ),
    )
    with localcontext() as context:
        context.prec = 2
        result = project_sec_quarterly_facts(
            (fy, q3),
            periods=((2024, 4),),
            concepts=("Revenue", "EPS"),
            additive_usd_flow_concepts=("Revenue", "EPS"),
        )
    revenue, eps = result.fields
    assert revenue.status is SecQuarterFieldStatus.PRESENT
    observation = revenue.observations[0]
    assert observation.value == Decimal("1.0000")
    assert observation.accession_numbers == (
        fy.vintage.accession_number,
        q3.vintage.accession_number,
    )
    assert observation.latest_accepted_at == max(fy.vintage.accepted_at, q3.vintage.accepted_at)
    assert observation.operation == "FY_MINUS_Q3_YTD"
    assert len(observation.fact_references) == 2
    assert {ref.accession_number for ref in observation.fact_references} == set(
        observation.accession_numbers
    )
    assert all(ref.context_ref and ref.fact_identity for ref in observation.fact_references)
    assert eps.status is SecQuarterFieldStatus.MISSING
    instant_vintage = replace(
        fy.vintage,
        rows=tuple(
            replace(row, period_start=None, period_type="instant")
            if row.concept == "Assets"
            else row
            for row in fy.vintage.rows
        ),
    )
    instant_input = SecQuarterlyVintageInput(instant_vintage, fy.source_observation_id, fy.period)
    instant = project_sec_quarterly_facts(
        (instant_input,), periods=((2024, 4),), concepts=("Assets",)
    ).fields[0]
    assert instant.status is SecQuarterFieldStatus.PRESENT
    assert instant.observations[0].value == Decimal(1000)


def test_amendment_alternatives_are_ambiguous_and_window_is_capped() -> None:
    original = _input(
        "0000000001-24-000001",
        form="10-Q",
        fiscal_year=2024,
        fiscal_period="Q1",
        period_kind=SecQuarterPeriodKind.INDEPENDENT_3M,
        quarter=1,
        label="Three Months Ended",
        start=date(2024, 1, 1),
        end=date(2024, 3, 31),
        values=(("Revenue", Decimal(1), "USD", "income_statement"),),
    )
    amendment = _input(
        "0000000001-24-000002",
        form="10-Q/A",
        fiscal_year=2024,
        fiscal_period="Q1",
        period_kind=SecQuarterPeriodKind.INDEPENDENT_3M,
        quarter=1,
        label="Three Months Ended",
        start=date(2024, 1, 1),
        end=date(2024, 3, 31),
        values=(("Revenue", Decimal(2), "USD", "income_statement"),),
    )
    result = project_sec_quarterly_facts(
        (original, amendment), periods=((2024, 1),), concepts=("Revenue",)
    )
    assert result.fields[0].status is SecQuarterFieldStatus.AMBIGUOUS
    assert len(result.fields[0].observations) == 2


def test_q4_does_not_mix_amendment_with_original_without_pair_evidence() -> None:
    annual = _input(
        "0000000001-25-000001",
        form="10-K/A",
        fiscal_year=2024,
        fiscal_period="FY",
        period_kind=SecQuarterPeriodKind.FY,
        quarter=4,
        label="Year Ended",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        values=(("Revenue", Decimal(120), "USD", "income_statement"),),
    )
    interim = _input(
        "0000000001-24-000010",
        form="10-Q",
        fiscal_year=2024,
        fiscal_period="Q3",
        period_kind=SecQuarterPeriodKind.YTD_9M,
        quarter=3,
        label="Nine Months Ended",
        start=date(2024, 1, 1),
        end=date(2024, 9, 30),
        values=(("Revenue", Decimal(90), "USD", "income_statement"),),
    )
    missing = project_sec_quarterly_facts(
        (annual, interim),
        periods=((2024, 4),),
        concepts=("Revenue",),
        additive_usd_flow_concepts=("Revenue",),
    )
    assert missing.fields[0].status is SecQuarterFieldStatus.MISSING
    declared = project_sec_quarterly_facts(
        (annual, interim),
        periods=((2024, 4),),
        concepts=("Revenue",),
        additive_usd_flow_concepts=("Revenue",),
        compatible_amendment_pairs=(
            (annual.vintage.accession_number, interim.vintage.accession_number),
        ),
    )
    assert declared.fields[0].observations[0].value == Decimal(30)


def test_q4_instant_fact_does_not_require_annual_flow_evidence() -> None:
    balance = _input(
        "0000000001-25-000020",
        form="10-K",
        fiscal_year=2024,
        fiscal_period="FY",
        period_kind=SecQuarterPeriodKind.INSTANT,
        quarter=4,
        label="DocumentFiscalPeriodFocus FY",
        start=None,
        end=date(2024, 12, 31),
        values=(("Assets", Decimal(125), "USD", "balance_sheet"),),
    )
    result = project_sec_quarterly_facts((balance,), periods=((2024, 4),), concepts=("Assets",))
    field = result.fields[0]
    assert field.status is SecQuarterFieldStatus.PRESENT
    assert field.observations[0].value == Decimal(125)
    assert field.observations[0].operation == "SOURCE"


def test_sec_company_eligibility_replays_root_and_preserves_limitations(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    observation = store.observe(
        RequestSpec("sec", "ticker_cik", {}),
        b"{}",
        datetime(2025, 1, 1, tzinfo=UTC),
        "json-v1",
        SnapshotMode.APPEND,
    )
    mapping = SecTickerCikMapping(
        {"ACME": ("0000000001", "Acme Corporation")}, SEC_TICKER_CIK_URL, observation
    )
    root_url = "https://data.sec.gov/submissions/CIK0000000001.json"
    root_body = {
        "cik": "0000000001",
        "entityType": "operating",
        "filings": {
            "recent": {
                "accessionNumber": ["0000009999-24-000001"],
                "form": ["10-Q"],
                "filingDate": ["2024-05-01"],
                "reportDate": ["2024-03-31"],
                "primaryDocument": ["q.htm"],
                "acceptanceDateTime": ["20240501120000"],
            },
            "files": [],
        },
    }
    root_observation = store.observe(
        RequestSpec("sec", "edgar_submissions", {"cik": "0000000001"}),
        json.dumps(root_body).encode(),
        datetime(2025, 1, 1, tzinfo=UTC),
        "json-v1",
        SnapshotMode.APPEND,
    )
    result = classify_sec_company_eligibility_from_root(
        "ACME", mapping, store, SecDiscoverySource(root_url, root_observation)
    )
    assert result.status is SecCompanyEligibilityStatus.SEC_COMPANY
    assert result.filing_accessions == ("0000009999-24-000001",)
    assert result.submissions_observation_id == root_observation.observation_identity
    assert any("does not prove ETF" in item for item in result.limitations)


def test_quarterly_projection_rejects_cross_cik_arithmetic() -> None:
    fy = _input(
        "0000000001-25-000001",
        form="10-K",
        fiscal_year=2024,
        fiscal_period="FY",
        period_kind=SecQuarterPeriodKind.FY,
        quarter=4,
        label="Year Ended",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        values=(("Revenue", Decimal(100), "USD", "income_statement"),),
    )
    q3 = _input(
        "0000000002-24-000001",
        form="10-Q",
        fiscal_year=2024,
        fiscal_period="Q3",
        period_kind=SecQuarterPeriodKind.YTD_9M,
        quarter=3,
        label="Nine Months Ended",
        start=date(2024, 1, 1),
        end=date(2024, 9, 30),
        values=(("Revenue", Decimal(75), "USD", "income_statement"),),
    )
    q3_vintage = replace(q3.vintage, cik="0000000002")
    q3 = SecQuarterlyVintageInput(q3_vintage, q3.source_observation_id, q3.period)
    with pytest.raises(ValueError, match="one SEC CIK"):
        project_sec_quarterly_facts(
            (fy, q3),
            periods=((2024, 4),),
            concepts=("Revenue",),
            additive_usd_flow_concepts=("Revenue",),
        )

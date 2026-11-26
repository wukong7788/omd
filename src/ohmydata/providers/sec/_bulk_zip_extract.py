"""Pure PIT joining and fundamental extraction from SEC bulk companyfacts + submissions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from ._bulk_zip_io import SecBulkSubmissionMeta
from ._bulk_zip_models import (
    SecBulkCikOutcome,
    SecBulkCikOutcomeStatus,
    SecBulkFundamentalRow,
    SecBulkJoinedFact,
    SecBulkProjectionResult,
    SecPrimaryCurrencySelection,
    _currency_from_unit,
    select_dei_shares,
    select_metric_fact,
    select_primary_currency,
    summarize_currency,
    summarize_taxonomy,
)
from ._companyfacts_models import (
    SecCompanyFactsFact,
    SecCompanyFactsFiling,
    _is_annual_form,
    _is_quarterly_form,
)
from ._companyfacts_periods import _resolve_filing_periods
from ._metric_arithmetic import exact_sum

_ADDITIVE_FLOW_METRICS = (
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "ocf",
    "capex",
)
_DURATION_FUNDAMENTAL_METRICS = (*_ADDITIVE_FLOW_METRICS, "diluted_shares", "eps_diluted")
_INSTANT_BS_METRICS = ("total_assets", "total_debt")


def _duration_days(fact: SecCompanyFactsFact) -> int | None:
    return None if fact.start is None else (fact.end - fact.start).days + 1


def _duration_window(
    facts: list[SecCompanyFactsFact],
    fps: set[str],
    min_days: int,
    max_days: int,
) -> list[SecCompanyFactsFact]:
    return [
        f
        for f in facts
        if f.fp in fps and (days := _duration_days(f)) is not None and min_days <= days <= max_days
    ]


def _year_in_range(year: int, start_year: int | None, end_year: int | None) -> bool:
    if start_year is not None and year < start_year:
        return False
    return not (end_year is not None and year > end_year)


def _fact_provenance(fact: SecCompanyFactsFact) -> dict[str, str]:
    return {
        "namespace": fact.namespace,
        "tag": fact.tag,
        "unit": fact.unit,
        "value": str(fact.value),
    }


def _build_fundamental_row(
    *,
    cik: str,
    submissions: SecBulkSubmissionMeta,
    filing: SecCompanyFactsFiling,
    fiscal_year: int,
    fiscal_period: str,
    period_kind: str,
    duration_facts: list[SecCompanyFactsFact],
    instant_facts: list[SecCompanyFactsFact],
    dei_facts: list[SecCompanyFactsFact],
    currency_selection: SecPrimaryCurrencySelection | None = None,
) -> SecBulkFundamentalRow | None:
    resolved_ccy = currency_selection or select_primary_currency(
        duration_facts + instant_facts,
        form=filing.form,
    )
    allow_ccy = resolved_ccy.basis != "ambiguous_tie"
    values: dict[str, Decimal | None] = {}
    provenance: dict[str, dict[str, Any]] = {}
    selected_starts: set[date] = set()
    used_facts: list[SecCompanyFactsFact] = []
    for metrics, pool, track_start in (
        (_DURATION_FUNDAMENTAL_METRICS, duration_facts, True),
        (_INSTANT_BS_METRICS, instant_facts, False),
    ):
        for metric in metrics:
            if not allow_ccy and metric != "diluted_shares":
                values[metric] = None
                continue
            chosen = select_metric_fact(metric, pool, currency=resolved_ccy.selected)
            values[metric] = chosen.value if chosen is not None else None
            if chosen is not None:
                used_facts.append(chosen)
                if track_start and chosen.start is not None:
                    selected_starts.add(chosen.start)
                provenance[metric] = _fact_provenance(chosen)
    dei_val, dei_end, dei_meta = select_dei_shares(dei_facts)
    if dei_meta is not None:
        provenance["dei_common_shares_outstanding"] = dei_meta
    if not any(v is not None for v in values.values()) and dei_val is None:
        return None
    if resolved_ccy.alternate_currencies:
        provenance["currency_selection"] = resolved_ccy.to_provenance_dict()
    period_start = min(selected_starts) if len(selected_starts) == 1 else None
    all_scope_facts = used_facts or (duration_facts + instant_facts)
    row_currency = summarize_currency(used_facts) if used_facts else resolved_ccy.selected
    return SecBulkFundamentalRow(
        cik=cik,
        entity_name=submissions.entity_name,
        symbols=submissions.symbols,
        accession_number=filing.accession_number,
        form=filing.form,
        is_amendment=filing.form.endswith("/A"),
        filing_date=filing.filing_date or filing.accepted_at.date(),
        report_date=filing.report_date,
        accepted_at=filing.accepted_at,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        period_kind=period_kind,
        period_start=period_start,
        period_end=filing.report_date,
        taxonomy=summarize_taxonomy(all_scope_facts),
        reporting_currency=row_currency,
        revenue=values["revenue"],
        gross_profit=values["gross_profit"],
        operating_income=values["operating_income"],
        net_income=values["net_income"],
        ocf=values["ocf"],
        capex=values["capex"],
        total_assets=values["total_assets"],
        total_debt=values["total_debt"],
        diluted_shares=values["diluted_shares"],
        eps_diluted=values["eps_diluted"],
        dei_shares_outstanding=dei_val,
        dei_shares_outstanding_end=dei_end,
        source_tags_json=json.dumps(provenance, sort_keys=True, separators=(",", ":")),
    )


def _derive_q4_rows(
    *,
    cik: str,
    submissions: SecBulkSubmissionMeta,
    resolved: dict[str, tuple[int, int]],
    by_accn_non_dei: dict[str, list[SecCompanyFactsFact]],
    by_accn_dei: dict[str, list[SecCompanyFactsFact]],
    primary_currencies: Mapping[str, SecPrimaryCurrencySelection],
    start_year: int | None,
    end_year: int | None,
) -> list[SecBulkFundamentalRow]:
    """Derive exact Q4 additive flow rows from non-amendment FY minus Q3 YTD cohorts."""
    fy_by_year: dict[int, list[tuple[SecCompanyFactsFiling, list[SecCompanyFactsFact]]]] = {}
    q3_by_year: dict[int, list[tuple[SecCompanyFactsFiling, list[SecCompanyFactsFact]]]] = {}
    for accn, facts in by_accn_non_dei.items():
        filing = submissions.filings[accn]
        if filing.form.endswith("/A"):
            continue
        period = resolved.get(accn)
        if _is_annual_form(filing.form):
            fy_facts = _duration_window(facts, {"FY"}, 300, 430)
            if not fy_facts:
                continue
            year = period[0] if period is not None else fy_facts[0].fy
            if year is not None:
                fy_by_year.setdefault(year, []).append((filing, fy_facts))
        elif _is_quarterly_form(filing.form):
            ytd_facts = _duration_window(facts, {"Q3"}, 240, 310)
            if not ytd_facts:
                continue
            year = period[0] if period is not None else ytd_facts[0].fy
            if year is not None:
                q3_by_year.setdefault(year, []).append((filing, ytd_facts))

    derived_rows: list[SecBulkFundamentalRow] = []
    for year in sorted(fy_by_year):
        if not _year_in_range(year, start_year, end_year):
            continue
        fy_entries = fy_by_year[year]
        q3_entries = q3_by_year.get(year, [])
        if len(fy_entries) != 1 or len(q3_entries) != 1:
            continue
        fy_filing, fy_facts = fy_entries[0]
        q3_filing, q3_facts = q3_entries[0]
        fy_ccy = primary_currencies.get(fy_filing.accession_number)
        q3_ccy = primary_currencies.get(q3_filing.accession_number)
        if not (fy_ccy and q3_ccy and fy_ccy.selected and fy_ccy.selected == q3_ccy.selected):
            continue
        target_ccy = fy_ccy.selected
        values: dict[str, Decimal | None] = {}
        provenance: dict[str, dict[str, Any]] = {}
        starts: set[date] = set()
        used_facts: list[SecCompanyFactsFact] = []
        for metric in _ADDITIVE_FLOW_METRICS:
            fy_sel = select_metric_fact(metric, fy_facts, currency=target_ccy)
            q3_sel = select_metric_fact(metric, q3_facts, currency=target_ccy)
            if (
                fy_sel is None
                or q3_sel is None
                or fy_sel.start is None
                or q3_sel.start is None
                or fy_sel.namespace != q3_sel.namespace
                or fy_sel.tag != q3_sel.tag
                or fy_sel.unit != q3_sel.unit
                or fy_sel.start != q3_sel.start
                or fy_sel.end <= q3_sel.end
                or not 60 <= (fy_sel.end - q3_sel.end).days <= 120
            ):
                values[metric] = None
                continue
            diff = exact_sum((fy_sel.value, q3_sel.value.copy_negate()))
            values[metric] = diff
            used_facts.append(fy_sel)
            starts.add(q3_sel.end + timedelta(days=1))
            provenance[metric] = {
                "namespace": fy_sel.namespace,
                "tag": f"{fy_sel.tag}-{q3_sel.tag}",
                "unit": fy_sel.unit,
                "value": str(diff),
                "fy_accn": fy_filing.accession_number,
                "q3_accn": q3_filing.accession_number,
            }
        if not any(v is not None for v in values.values()):
            continue
        instant_facts = [f for f in by_accn_non_dei[fy_filing.accession_number] if f.start is None]
        bs_values: dict[str, Decimal | None] = {}
        for metric in _INSTANT_BS_METRICS:
            chosen = select_metric_fact(metric, instant_facts, currency=target_ccy)
            bs_values[metric] = chosen.value if chosen is not None else None
            if chosen is not None:
                provenance[metric] = _fact_provenance(chosen)
        dei_val, dei_end, dei_meta = select_dei_shares(
            by_accn_dei.get(fy_filing.accession_number, [])
        )
        if dei_meta is not None:
            provenance["dei_common_shares_outstanding"] = dei_meta
        if fy_ccy.alternate_currencies or q3_ccy.alternate_currencies:
            alts = sorted(set(fy_ccy.alternate_currencies) | set(q3_ccy.alternate_currencies))
            provenance["currency_selection"] = {
                "selected": target_ccy,
                "alternate_currencies": alts,
                "basis": fy_ccy.basis if fy_ccy.alternate_currencies else q3_ccy.basis,
            }
        derived_rows.append(
            SecBulkFundamentalRow(
                cik=cik,
                entity_name=submissions.entity_name,
                symbols=submissions.symbols,
                accession_number=fy_filing.accession_number,
                form=fy_filing.form,
                is_amendment=False,
                filing_date=fy_filing.filing_date or fy_filing.accepted_at.date(),
                report_date=fy_filing.report_date,
                accepted_at=max(fy_filing.accepted_at, q3_filing.accepted_at),
                fiscal_year=year,
                fiscal_period="Q4",
                period_kind="Q4_DERIVED",
                period_start=min(starts) if len(starts) == 1 else None,
                period_end=fy_filing.report_date,
                taxonomy=summarize_taxonomy(used_facts),
                reporting_currency=summarize_currency(used_facts),
                revenue=values["revenue"],
                gross_profit=values["gross_profit"],
                operating_income=values["operating_income"],
                net_income=values["net_income"],
                ocf=values["ocf"],
                capex=values["capex"],
                total_assets=bs_values["total_assets"],
                total_debt=bs_values["total_debt"],
                diluted_shares=None,
                eps_diluted=None,
                dei_shares_outstanding=dei_val,
                dei_shares_outstanding_end=dei_end,
                source_tags_json=json.dumps(provenance, sort_keys=True, separators=(",", ":")),
            )
        )
    return derived_rows


def extract_cik_bulk_records(
    cik: str,
    raw_facts: tuple[SecCompanyFactsFact, ...],
    submissions: SecBulkSubmissionMeta,
    *,
    start_year: int | None = None,
    end_year: int | None = None,
    acceptance_upper: datetime | None = None,
) -> tuple[list[SecBulkJoinedFact], list[SecBulkFundamentalRow], str | None, str | None]:
    """Join one company's parsed facts strictly to its submissions.zip PIT metadata."""
    by_accn_non_dei: dict[str, list[SecCompanyFactsFact]] = {}
    by_accn_all_non_dei: dict[str, list[SecCompanyFactsFact]] = {}
    by_accn_dei: dict[str, list[SecCompanyFactsFact]] = {}
    eligible_facts: list[SecCompanyFactsFact] = []
    for fact in raw_facts:
        filing = submissions.filings.get(fact.accn)
        if filing is None or fact.form != filing.form:
            continue
        if acceptance_upper is not None and filing.accepted_at > acceptance_upper:
            continue
        filed_by = filing.filing_date or filing.accepted_at.date()
        if fact.filed > filed_by:
            continue
        if fact.namespace == "dei":
            by_accn_dei.setdefault(fact.accn, []).append(fact)
            eligible_facts.append(fact)
        else:
            by_accn_all_non_dei.setdefault(fact.accn, []).append(fact)
            if fact.end == filing.report_date:
                by_accn_non_dei.setdefault(fact.accn, []).append(fact)
                eligible_facts.append(fact)

    resolved = _resolve_filing_periods(tuple(eligible_facts), submissions.filings, acceptance_upper)
    overall_taxonomy = summarize_taxonomy(eligible_facts) if eligible_facts else None
    if overall_taxonomy == "unknown":
        overall_taxonomy = None

    joined_facts: list[SecBulkJoinedFact] = []
    for fact in eligible_facts:
        filing = submissions.filings[fact.accn]
        accn_period = resolved.get(fact.accn)
        eff_fy = accn_period[0] if accn_period is not None else fact.fy
        filter_year = eff_fy if eff_fy is not None else filing.report_date.year
        if not _year_in_range(filter_year, start_year, end_year):
            continue
        joined_facts.append(
            SecBulkJoinedFact(
                cik=cik,
                entity_name=submissions.entity_name,
                symbols=submissions.symbols,
                accession_number=filing.accession_number,
                form=filing.form,
                is_amendment=filing.form.endswith("/A"),
                filing_date=filing.filing_date or filing.accepted_at.date(),
                report_date=filing.report_date,
                accepted_at=filing.accepted_at,
                namespace=fact.namespace,
                tag=fact.tag,
                unit=fact.unit,
                currency=_currency_from_unit(fact.unit),
                value=fact.value,
                period_start=fact.start,
                period_end=fact.end,
                fiscal_year=eff_fy,
                fiscal_period=fact.fp,
                frame=fact.frame,
                taxonomy=overall_taxonomy,
            )
        )

    fundamental_rows: list[SecBulkFundamentalRow] = []
    all_accns = sorted(
        set(by_accn_non_dei) | set(by_accn_dei),
        key=lambda a: (submissions.filings[a].accepted_at, a),
    )
    primary_currencies: dict[str, SecPrimaryCurrencySelection] = {}
    for accn in all_accns:
        filing = submissions.filings[accn]
        facts = by_accn_non_dei.get(accn, [])
        issuer_pit_facts = (
            [
                f
                for a, flist in by_accn_all_non_dei.items()
                if submissions.filings[a].accepted_at <= filing.accepted_at
                for f in flist
            ]
            if len({c for f in facts if (c := _currency_from_unit(f.unit)) is not None}) > 1
            else None
        )
        ccy_sel = select_primary_currency(
            facts,
            form=filing.form,
            filing_facts=by_accn_all_non_dei.get(accn, facts),
            issuer_facts=issuer_pit_facts,
        )
        primary_currencies[accn] = ccy_sel
        dei_facts = by_accn_dei.get(accn, [])
        instant_facts = [f for f in facts if f.start is None]
        accn_period = resolved.get(accn)
        if _is_quarterly_form(filing.form):
            q_facts = _duration_window(facts, {"Q1", "Q2", "Q3"}, 60, 120)
            if accn_period is not None:
                fy, fq = accn_period
                fp = f"Q{fq}"
            else:
                sample = q_facts or facts
                if not sample or sample[0].fy is None or sample[0].fp not in {"Q1", "Q2", "Q3"}:
                    continue
                fy, fp = sample[0].fy, sample[0].fp
            if not _year_in_range(fy, start_year, end_year):
                continue
            row = _build_fundamental_row(
                cik=cik,
                submissions=submissions,
                filing=filing,
                fiscal_year=fy,
                fiscal_period=fp,
                period_kind="Q",
                duration_facts=q_facts,
                instant_facts=instant_facts,
                dei_facts=dei_facts,
                currency_selection=ccy_sel,
            )
            if row is not None:
                fundamental_rows.append(row)
        elif _is_annual_form(filing.form):
            fy_facts = _duration_window(facts, {"FY"}, 300, 430)
            q4_direct_facts = _duration_window(facts, {"FY"}, 60, 120)
            if accn_period is not None:
                fy = accn_period[0]
            else:
                sample = fy_facts or q4_direct_facts or facts
                if not sample or sample[0].fy is None:
                    continue
                fy = sample[0].fy
            if not _year_in_range(fy, start_year, end_year):
                continue
            fy_row = _build_fundamental_row(
                cik=cik,
                submissions=submissions,
                filing=filing,
                fiscal_year=fy,
                fiscal_period="FY",
                period_kind="FY",
                duration_facts=fy_facts,
                instant_facts=instant_facts,
                dei_facts=dei_facts,
                currency_selection=ccy_sel,
            )
            if fy_row is not None:
                fundamental_rows.append(fy_row)
            if q4_direct_facts:
                q4_row = _build_fundamental_row(
                    cik=cik,
                    submissions=submissions,
                    filing=filing,
                    fiscal_year=fy,
                    fiscal_period="Q4",
                    period_kind="Q",
                    duration_facts=q4_direct_facts,
                    instant_facts=[],
                    dei_facts=[],
                    currency_selection=ccy_sel,
                )
                if q4_row is not None:
                    fundamental_rows.append(q4_row)

    fundamental_rows.extend(
        _derive_q4_rows(
            cik=cik,
            submissions=submissions,
            resolved=resolved,
            by_accn_non_dei=by_accn_non_dei,
            by_accn_dei=by_accn_dei,
            primary_currencies=primary_currencies,
            start_year=start_year,
            end_year=end_year,
        )
    )
    row_currencies = {
        r.reporting_currency for r in fundamental_rows if r.reporting_currency is not None
    }
    overall_currency = (
        (next(iter(row_currencies)) if len(row_currencies) == 1 else "MULTI")
        if row_currencies
        else summarize_currency(eligible_facts)
    )
    return joined_facts, fundamental_rows, overall_taxonomy, overall_currency


__all__ = [
    "SecBulkCikOutcome",
    "SecBulkCikOutcomeStatus",
    "SecBulkFundamentalRow",
    "SecBulkJoinedFact",
    "SecBulkProjectionResult",
    "extract_cik_bulk_records",
]

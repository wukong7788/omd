"""SEC periodic filing chronology and canonical fiscal period resolution."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ._companyfacts_models import (
    _ALLOWED_FORMS,
    SecCompanyFactsFact,
    SecCompanyFactsFiling,
    _is_annual_form,
    _is_quarterly_form,
)
from ._filing_xbrl import SecFilingXbrlFact

_MAX_REQUESTED_PERIODS = 400
Fact = SecCompanyFactsFact | SecFilingXbrlFact


def _fact_namespace(fact: Fact) -> str:
    return getattr(fact, "namespace", "us-gaap")


def _duration_days(fact: Fact) -> int | None:
    if fact.start is None:
        return None
    return (fact.end - fact.start).days + 1


def _step_back_quarter(year: int, quarter: int, steps: int) -> tuple[int, int]:
    total = year * 4 + (quarter - 1) - steps
    if total < 4:
        return (1, 1)
    return (total // 4, (total % 4) + 1)


def _quarter_sequence(start: tuple[int, int], end: tuple[int, int]) -> tuple[tuple[int, int], ...]:
    if start > end:
        return ()
    start_idx = start[0] * 4 + (start[1] - 1)
    end_idx = end[0] * 4 + (end[1] - 1)
    if end_idx - start_idx + 1 > _MAX_REQUESTED_PERIODS:
        start_idx = end_idx - _MAX_REQUESTED_PERIODS + 1
    return tuple((idx // 4, (idx % 4) + 1) for idx in range(start_idx, end_idx + 1))


def _validate_window_args(
    count: int | None,
    start_year: int | None,
    end_year: int | None,
) -> None:
    if count is not None and (type(count) is not int or count < 1):
        raise ValueError("count must be >= 1 or None")
    if start_year is not None and (type(start_year) is not int or not 1 <= start_year <= 9999):
        raise ValueError("start_year must be an integer in 1..9999")
    if end_year is not None and (type(end_year) is not int or not 1 <= end_year <= 9999):
        raise ValueError("end_year must be an integer in 1..9999")
    if start_year is not None and end_year is not None and start_year > end_year:
        raise ValueError("start_year must be <= end_year")


def _select_period_window(
    available: set[tuple[int, int]],
    *,
    count: int | None,
    start_year: int | None,
    end_year: int | None,
) -> tuple[tuple[int, int], ...]:
    if not available:
        return ()
    anchor = max(available)
    earliest = min(available)
    if start_year is not None or end_year is not None:
        seq = _quarter_sequence(earliest, anchor)
        seq = tuple(
            (y, q)
            for y, q in seq
            if (start_year is None or y >= start_year) and (end_year is None or y <= end_year)
        )
        return seq[-count:] if count is not None else seq
    if count is None:
        return _quarter_sequence(earliest, anchor)
    if count <= 8:
        start = _step_back_quarter(anchor[0], anchor[1], 7)
        return _quarter_sequence(start, anchor)[-count:]
    target_start = _step_back_quarter(anchor[0], anchor[1], count - 1)
    start = max(earliest, target_start)
    return _quarter_sequence(start, anchor)[-count:]


def _fiscal_period(
    fact: Fact,
    filing: SecCompanyFactsFiling,
    resolved_periods: Mapping[str, tuple[int, int]] | None = None,
) -> tuple[int, int] | None:
    if _fact_namespace(fact) == "dei" or fact.fy is None or fact.fp is None:
        return None
    if fact.accn != filing.accession_number or fact.form != filing.form:
        return None
    # SEC filingDate is a provider field and can be one calendar day after the
    # UTC acceptance date for late US filings. Do not infer it from UTC time.
    filed_by = filing.filing_date or filing.accepted_at.date()
    if fact.filed > filed_by or fact.end != filing.report_date:
        return None
    if resolved_periods is not None and filing.accession_number in resolved_periods:
        resolved_fy, resolved_fq = resolved_periods[filing.accession_number]
        if _is_quarterly_form(fact.form):
            if fact.fp == f"Q{resolved_fq}":
                return resolved_fy, resolved_fq
        elif _is_annual_form(fact.form) and fact.fp == "FY" and resolved_fq == 4:
            return resolved_fy, 4
        return None
    if _is_quarterly_form(fact.form) and fact.fp in {"Q1", "Q2", "Q3"}:
        return fact.fy, int(fact.fp[-1])
    if _is_annual_form(fact.form) and fact.fp == "FY":
        return fact.fy, 4
    return None


@dataclass(frozen=True)
class _PeriodicFilingFactGroup:
    report_date: date
    accession: str
    form: str
    fiscal_quarter: int
    tagged_fiscal_year: int | None


@dataclass
class _PeriodDateGroup:
    fiscal_quarter: int
    tagged_fiscal_years: set[int] = field(default_factory=set)
    accessions: list[str] = field(default_factory=list)
    invalid: bool = False


def _resolve_filing_periods(
    facts: tuple[SecCompanyFactsFact | SecFilingXbrlFact, ...],
    filings: Mapping[str, SecCompanyFactsFiling] | None = None,
    acceptance_upper: datetime | None = None,
) -> dict[str, tuple[int, int]]:
    """Resolve canonical (fiscal_year, fiscal_quarter) for periodic filings.

    Produces a continuous fiscal-quarter sequence from filing/report-period
    chronology while preserving native fact.fy in fact evidence.

    Fails closed (leaving periods uncorrected) when:
    - Same report date has conflicting quarter declarations
    - Implied base years differ by more than 1 (not an off-by-one focus anomaly)
    - Implied base years have a tie or lack a strict majority
    """
    if filings is not None:
        periodic: list[_PeriodicFilingFactGroup] = []
        for accn, fil in filings.items():
            if fil.form not in _ALLOWED_FORMS:
                continue
            if acceptance_upper is not None and fil.accepted_at > acceptance_upper:
                continue
            filed_by = fil.filing_date or fil.accepted_at.date()
            fil_facts = [
                f
                for f in facts
                if getattr(f, "namespace", "us-gaap") != "dei"
                and f.fy is not None
                and f.fp is not None
                and f.accn == accn
                and f.form == fil.form
                and f.end == fil.report_date
                and f.filed <= filed_by
            ]
            if not fil_facts:
                continue
            if _is_annual_form(fil.form):
                q = 4
            else:
                fps = {f.fp for f in fil_facts if f.fp in {"Q1", "Q2", "Q3"}}
                if len(fps) != 1:
                    continue
                q = int(fps.pop()[-1])
            fys = {f.fy for f in fil_facts if f.fy is not None}
            tagged_fy = fys.pop() if len(fys) == 1 else None
            periodic.append(_PeriodicFilingFactGroup(fil.report_date, accn, fil.form, q, tagged_fy))
    else:
        by_accn: dict[str, list[SecCompanyFactsFact | SecFilingXbrlFact]] = {}
        for f in facts:
            if getattr(f, "namespace", "us-gaap") == "dei" or f.fy is None or f.fp is None:
                continue
            if f.form not in _ALLOWED_FORMS:
                continue
            if acceptance_upper is not None and f.filed > acceptance_upper.date() + timedelta(
                days=1
            ):
                continue
            by_accn.setdefault(f.accn, []).append(f)
        periodic = []
        for accn, f_facts in by_accn.items():
            report_date = max(f.end for f in f_facts)
            current_facts = [f for f in f_facts if f.end == report_date]
            if not current_facts:
                continue
            form = current_facts[0].form
            if _is_annual_form(form):
                q = 4
            else:
                fps = {f.fp for f in current_facts if f.fp in {"Q1", "Q2", "Q3"}}
                if len(fps) != 1:
                    continue
                q = int(fps.pop()[-1])
            fys = {f.fy for f in current_facts if f.fy is not None}
            tagged_fy = fys.pop() if len(fys) == 1 else None
            periodic.append(_PeriodicFilingFactGroup(report_date, accn, form, q, tagged_fy))

    if not periodic:
        return {}

    periodic.sort(key=lambda x: (x.report_date, x.accession))

    by_date: dict[date, _PeriodDateGroup] = {}
    for item in periodic:
        group = by_date.get(item.report_date)
        if group is None:
            by_date[item.report_date] = _PeriodDateGroup(
                fiscal_quarter=item.fiscal_quarter,
                tagged_fiscal_years={item.tagged_fiscal_year}
                if item.tagged_fiscal_year is not None
                else set(),
                accessions=[item.accession],
            )
        else:
            if group.fiscal_quarter != item.fiscal_quarter:
                group.invalid = True
            group.accessions.append(item.accession)
            if item.tagged_fiscal_year is not None:
                group.tagged_fiscal_years.add(item.tagged_fiscal_year)

    dates = [d for d in sorted(by_date.keys()) if not by_date[d].invalid]
    chains: list[list[date]] = []
    current_chain: list[date] = []
    for d in dates:
        info = by_date[d]
        if not current_chain:
            current_chain.append(d)
        else:
            prev_d = current_chain[-1]
            prev_info = by_date[prev_d]
            days = (d - prev_d).days
            expected_q = (prev_info.fiscal_quarter % 4) + 1
            is_next_quarter = 60 <= days <= 125 and info.fiscal_quarter == expected_q
            is_next_annual = (
                300 <= days <= 430 and prev_info.fiscal_quarter == 4 and info.fiscal_quarter == 4
            )
            if is_next_quarter or is_next_annual:
                current_chain.append(d)
            else:
                chains.append(current_chain)
                current_chain = [d]
    if current_chain:
        chains.append(current_chain)

    resolved: dict[str, tuple[int, int]] = {}
    for chain in chains:
        deltas: list[int] = []
        cur_delta = 0
        for d in chain:
            deltas.append(cur_delta)
            if by_date[d].fiscal_quarter == 4:
                cur_delta += 1
        implied_bases: list[int] = []
        for d, delta in zip(chain, deltas):
            for tfy in by_date[d].tagged_fiscal_years:
                implied_bases.append(tfy - delta)
        if not implied_bases:
            continue

        distinct_bases = set(implied_bases)
        # Constrain correction to adjacent off-by-one anomalies: max - min <= 1
        if max(distinct_bases) - min(distinct_bases) > 1:
            # Contradiction spans multiple years: do not override; fail closed for this chain
            continue

        counts = Counter(implied_bases)
        most_common = counts.most_common()
        winner, winner_count = most_common[0]
        # Require unique winner with strict majority support
        if len(most_common) > 1 and winner_count == most_common[1][1]:
            continue
        if winner_count <= len(implied_bases) // 2:
            continue

        base = winner
        for d, delta in zip(chain, deltas):
            q = by_date[d].fiscal_quarter
            fy = base + delta
            for accn in by_date[d].accessions:
                resolved[accn] = (fy, q)

    return resolved


def _ordered_periods(
    facts: tuple[Fact, ...],
    filings: Mapping[str, SecCompanyFactsFiling],
    acceptance_upper: datetime | None,
    *,
    count: int | None = 8,
    start_year: int | None = None,
    end_year: int | None = None,
) -> tuple[tuple[int, int], ...]:
    resolved = _resolve_filing_periods(facts, filings, acceptance_upper)
    periods: set[tuple[int, int]] = set()
    for fact in facts:
        filing = filings.get(fact.accn)
        if filing is None or (
            acceptance_upper is not None and filing.accepted_at > acceptance_upper
        ):
            continue
        period = _fiscal_period(fact, filing, resolved)
        if period is not None:
            periods.add(period)
    return _select_period_window(periods, count=count, start_year=start_year, end_year=end_year)


def _periods_from_companyfacts(
    facts: tuple[Fact, ...],
    acceptance_upper: datetime | None,
    count: int | None,
    *,
    start_year: int | None = None,
    end_year: int | None = None,
) -> tuple[tuple[int, int], ...]:
    resolved = _resolve_filing_periods(facts, acceptance_upper=acceptance_upper)
    available: set[tuple[int, int]] = set()
    for fact in facts:
        if _fact_namespace(fact) == "dei" or fact.fy is None or fact.fp is None:
            continue
        # Candidate selection must admit the SEC filingDate after a late UTC
        # acceptance. The exact accepted-at cutoff is enforced after joining
        # submissions; this one-day allowance cannot publish a later filing.
        if acceptance_upper is not None and fact.filed > acceptance_upper.date() + timedelta(
            days=1
        ):
            continue
        accn_period = resolved.get(fact.accn)
        if accn_period is not None:
            available.add(accn_period)
            continue
        duration = _duration_days(fact)
        if duration is None:
            continue
        if _is_quarterly_form(fact.form) and fact.fp in {"Q1", "Q2", "Q3"}:
            if duration <= 310:
                available.add((fact.fy, int(fact.fp[-1])))
        elif _is_annual_form(fact.form) and fact.fp == "FY" and 300 <= duration <= 430:
            available.add((fact.fy, 4))
    if not available:
        return ()
    ordered = _select_period_window(
        available,
        count=count,
        start_year=start_year,
        end_year=end_year,
    )
    if not ordered:
        return ()
    if acceptance_upper is not None:
        # A same-day filing may have been accepted after the requested bound and
        # move the exact anchor back by one quarter. Fetch that alternate window too.
        prior_start = _step_back_quarter(ordered[0][0], ordered[0][1], 1)
        if prior_start < ordered[0]:
            return (prior_start, *ordered)
    return ordered


__all__ = [
    "_MAX_REQUESTED_PERIODS",
    "_duration_days",
    "_fact_namespace",
    "_fiscal_period",
    "_ordered_periods",
    "_periods_from_companyfacts",
    "_resolve_filing_periods",
    "_validate_window_args",
]

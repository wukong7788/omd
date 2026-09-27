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
)
from ._filing_xbrl import SecFilingXbrlFact


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
                if f.accn == accn
                and f.form == fil.form
                and f.end == fil.report_date
                and f.filed <= filed_by
            ]
            if not fil_facts:
                continue
            if fil.form.startswith("10-K"):
                q = 4
            else:
                fps = {f.fp for f in fil_facts if f.fp in {"Q1", "Q2", "Q3"}}
                if len(fps) != 1:
                    continue
                q = int(fps.pop()[-1])
            fys = {f.fy for f in fil_facts}
            tagged_fy = fys.pop() if len(fys) == 1 else None
            periodic.append(_PeriodicFilingFactGroup(fil.report_date, accn, fil.form, q, tagged_fy))
    else:
        by_accn: dict[str, list[SecCompanyFactsFact | SecFilingXbrlFact]] = {}
        for f in facts:
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
            if form.startswith("10-K"):
                q = 4
            else:
                fps = {f.fp for f in current_facts if f.fp in {"Q1", "Q2", "Q3"}}
                if len(fps) != 1:
                    continue
                q = int(fps.pop()[-1])
            fys = {f.fy for f in current_facts}
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
            if 60 <= days <= 125 and info.fiscal_quarter == expected_q:
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


__all__ = ["_resolve_filing_periods"]

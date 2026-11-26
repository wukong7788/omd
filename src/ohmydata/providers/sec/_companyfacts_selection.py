"""Canonical SEC candidate precedence and amendment selection."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime

from ._companyfacts_models import (
    SEC_CANONICAL_CONCEPTS,
    SEC_CANONICAL_IFRS_CONCEPTS,
    SEC_CANONICAL_IFRS_REVENUE_PRIORITY,
    SEC_CANONICAL_REVENUE_PRIORITY,
    SecCanonicalFactEvidence,
    SecCanonicalMetric,
    SecCompanyFactsFact,
    SecCompanyFactsFiling,
    SecFilingXbrlFactEvidence,
)
from ._companyfacts_periods import _resolve_filing_periods
from ._filing_xbrl import SecFilingXbrlFact

Evidence = SecCanonicalFactEvidence | SecFilingXbrlFactEvidence


def finalize_canonical_candidates(
    metric: SecCanonicalMetric,
    valid: list[Evidence],
    filings: Mapping[str, SecCompanyFactsFiling],
) -> tuple[list[Evidence], bool, tuple[Evidence, ...]]:
    """Apply versioned semantic precedence while retaining every native candidate."""
    selected = list({item: item for item in valid}.values())
    suppressed: list[Evidence] = []
    if metric is SecCanonicalMetric.REVENUE:
        preferred_us_gaap = set(SEC_CANONICAL_REVENUE_PRIORITY)
        preferred_ifrs = set(SEC_CANONICAL_IFRS_REVENUE_PRIORITY)
        amendment_bases: dict[str, str] = {}
        for item in selected:
            filing = filings[item.accession_number]
            if not filing.form.endswith("/A"):
                continue
            bases = [
                candidate
                for candidate in filings.values()
                if candidate.form == filing.form.removesuffix("/A")
                and candidate.report_date == filing.report_date
                and candidate.accepted_at < filing.accepted_at
            ]
            if bases:
                amendment_bases[item.accession_number] = max(
                    bases, key=lambda candidate: candidate.accepted_at
                ).accession_number
        groups: dict[tuple[str, date, date, str, str], list[int]] = {}
        for index, item in enumerate(selected):
            ns = getattr(item, "namespace", "us-gaap")
            groups.setdefault(
                (
                    amendment_bases.get(item.accession_number, item.accession_number),
                    item.period_start,
                    item.period_end,
                    item.unit,
                    ns,
                ),
                [],
            ).append(index)
        suppressed_indices: set[int] = set()
        for (_, _, _, _, ns), indices in groups.items():
            if ns == "ifrs-full":
                if any(selected[index].native_tag in preferred_ifrs for index in indices):
                    for index in indices:
                        if selected[index].native_tag in {
                            "RevenueFromContractsWithCustomers",
                            "RevenueFromSaleOfGoods",
                        }:
                            suppressed_indices.add(index)
            elif any(selected[index].native_tag in preferred_us_gaap for index in indices):
                for index in indices:
                    if selected[index].native_tag.startswith("RevenueFromContractWithCustomer"):
                        suppressed_indices.add(index)
        suppressed = [item for index, item in enumerate(selected) if index in suppressed_indices]
        selected = [item for index, item in enumerate(selected) if index not in suppressed_indices]

    incompatible_amendment = any(item.form.endswith("/A") for item in selected)
    if suppressed and len(selected) == 1:
        # The amendment did not change a like-for-like total here: the other
        # retained rows are narrower contract-revenue subsets.
        incompatible_amendment = False
    amendments = [item for item in selected if item.form.endswith("/A")]
    if amendments and any(isinstance(item, SecFilingXbrlFactEvidence) for item in selected):
        # A retained XBRL amendment is later source evidence for this same
        # period. Prefer it only when all source rows agree.
        values = {(item.value, item.unit) for item in selected}
        if len(values) == 1:
            selected = [max(amendments, key=lambda item: item.accepted_at)]
            incompatible_amendment = False
        else:
            incompatible_amendment = True
    return selected, incompatible_amendment, tuple(selected + suppressed)


def _incomplete_xbrl_amendments(
    facts: tuple[SecCompanyFactsFact | SecFilingXbrlFact, ...],
    filings: dict[str, SecCompanyFactsFiling],
    acceptance_upper: datetime | None,
    filing_xbrl_accessions: set[str],
) -> tuple[frozenset[tuple[int, int]], tuple[str, ...]]:
    """Find retained amendments that omit canonical fact classes from their base filing."""
    resolved = _resolve_filing_periods(facts, filings, acceptance_upper)
    amendment_accessions = {
        fact.accn
        for fact in facts
        if fact.form == "10-Q/A" and fact.accn in filings and fact.accn in filing_xbrl_accessions
    }
    incomplete_periods: set[tuple[int, int]] = set()
    incomplete_accessions: set[str] = set()
    for amended_accession in amendment_accessions:
        amended_filing = filings[amended_accession]
        if acceptance_upper is not None and amended_filing.accepted_at > acceptance_upper:
            continue
        bases = [
            filing
            for filing in filings.values()
            if filing.form == "10-Q"
            and filing.report_date == amended_filing.report_date
            and filing.accepted_at < amended_filing.accepted_at
        ]
        if not bases:
            continue
        base = max(bases, key=lambda filing: filing.accepted_at)

        def metric_classes(accession: str) -> set[str]:
            classes: set[str] = set()
            for fact in facts:
                if fact.accn != accession:
                    continue
                ns = getattr(fact, "namespace", "us-gaap")
                concept_map = (
                    SEC_CANONICAL_IFRS_CONCEPTS if ns == "ifrs-full" else SEC_CANONICAL_CONCEPTS
                )
                for metric, tags in concept_map.items():
                    if fact.tag in tags:
                        classes.add(metric)
            return classes

        if metric_classes(base.accession_number) - metric_classes(amended_accession):
            period = resolved.get(amended_accession)
            if period is not None:
                incomplete_periods.add(period)
                incomplete_accessions.add(amended_accession)
    return frozenset(incomplete_periods), tuple(sorted(incomplete_accessions))


__all__ = ["_incomplete_xbrl_amendments", "finalize_canonical_candidates"]

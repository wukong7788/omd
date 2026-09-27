"""Canonical SEC candidate precedence and amendment selection."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from ._companyfacts_models import (
    SEC_CANONICAL_REVENUE_PRIORITY,
    SecCanonicalFactEvidence,
    SecCanonicalMetric,
    SecCompanyFactsFiling,
    SecFilingXbrlFactEvidence,
)

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
        preferred_tags = set(SEC_CANONICAL_REVENUE_PRIORITY)
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
        groups: dict[tuple[str, date, date, str], list[int]] = {}
        for index, item in enumerate(selected):
            groups.setdefault(
                (
                    amendment_bases.get(item.accession_number, item.accession_number),
                    item.period_start,
                    item.period_end,
                    item.unit,
                ),
                [],
            ).append(index)
        suppressed_indices: set[int] = set()
        for indices in groups.values():
            if not any(selected[index].native_tag in preferred_tags for index in indices):
                continue
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


__all__ = ["finalize_canonical_candidates"]

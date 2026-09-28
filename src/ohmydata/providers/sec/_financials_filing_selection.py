"""Deterministic SEC financial-filing selection and amendment lineage."""

from __future__ import annotations

from datetime import date
from typing import Any

from .financials import SecFinancialsRequest


def select_financial_filing_candidates(
    filings: Any, request: SecFinancialsRequest, *, symbol: str
) -> list[Any]:
    """Filter the complete provider collection before applying the request limit."""
    candidates = list(filings)
    eligible: list[Any] = []
    requested_forms = {str(value).upper() for value in request.forms}
    for candidate in candidates:
        form = str(getattr(candidate, "form", "")).upper()
        base_form = form.removesuffix("/A")
        if form not in requested_forms and not (
            request.include_amendments and base_form in requested_forms
        ):
            continue
        if form.endswith("/A") and not request.include_amendments:
            continue
        try:
            candidate_date = date.fromisoformat(str(candidate.filing_date))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid filing date for {symbol}: {getattr(candidate, 'filing_date', None)!r}"
            ) from exc
        if request.start_year and candidate_date.year < request.start_year:
            continue
        if request.end_year and candidate_date.year > request.end_year:
            continue
        eligible.append(candidate)
    eligible.sort(
        key=lambda filing: (str(filing.filing_date), str(filing.accession_number)), reverse=True
    )
    return eligible


def amendment_base_accessions(candidates: list[Any]) -> dict[str, str]:
    """Identify exact same-period original filings for returned amendments."""

    def report_period(filing: Any) -> str | None:
        try:
            value = getattr(filing, "period_of_report", None)
        except (AttributeError, KeyError, TypeError, ValueError, OSError):
            return None
        return str(value) if value else None

    result: dict[str, str] = {}
    for amendment in candidates:
        form = str(getattr(amendment, "form", "")).upper()
        if not form.endswith("/A"):
            continue
        period = report_period(amendment)
        if period is None:
            continue
        amendment_key = (str(amendment.filing_date), str(amendment.accession_number))
        bases = [
            candidate
            for candidate in candidates
            if str(getattr(candidate, "form", "")).upper() == form.removesuffix("/A")
            and report_period(candidate) == period
            and (str(candidate.filing_date), str(candidate.accession_number)) < amendment_key
        ]
        if bases:
            base = max(
                bases,
                key=lambda filing: (str(filing.filing_date), str(filing.accession_number)),
            )
            result[str(amendment.accession_number)] = str(base.accession_number)
    return result


__all__ = ["amendment_base_accessions", "select_financial_filing_candidates"]

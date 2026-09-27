"""Fiscal period selection and typed SEC canonical metric projection."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta

from ._companyfacts_models import (
    _ALLOWED_FORMS,
    _FLOW_METRICS,
    _QUARTER_FRAME,
    _UNITS,
    SEC_CANONICAL_CONCEPTS,
    SecCanonicalFactEvidence,
    SecCanonicalFieldStatus,
    SecCanonicalMetric,
    SecCanonicalQuarterField,
    SecCanonicalQuarterSlot,
    SecCompanyFactsFact,
    SecCompanyFactsFiling,
    SecFilingXbrlFactEvidence,
)
from ._companyfacts_periods import _resolve_filing_periods
from ._companyfacts_selection import finalize_canonical_candidates
from ._filing_xbrl import SecFilingXbrlFact
from ._metric_arithmetic import exact_sum
from .errors import ResourceLimitError, SchemaMismatchError

_SERIALIZATION = "sec-companyfacts-json-v1"
_MAX_FIELD_ALTERNATIVES = 4_096
_MAX_PAIR_ROWS = 4_096
_MAX_PAIR_ATTEMPTS = 10_000


Fact = SecCompanyFactsFact | SecFilingXbrlFact


def _duration_days(fact: Fact) -> int:
    return (fact.end - fact.start).days + 1


def _valid_quarter_frame(fact: Fact) -> bool:
    return fact.frame is None or _QUARTER_FRAME.fullmatch(fact.frame) is not None


def _fiscal_period(
    fact: Fact,
    filing: SecCompanyFactsFiling,
    resolved_periods: Mapping[str, tuple[int, int]] | None = None,
) -> tuple[int, int] | None:
    if fact.accn != filing.accession_number or fact.form != filing.form:
        return None
    # SEC filingDate is a provider field and can be one calendar day after the
    # UTC acceptance date for late US filings. Do not infer it from UTC time.
    filed_by = filing.filing_date or filing.accepted_at.date()
    if fact.filed > filed_by or fact.end != filing.report_date:
        return None
    if resolved_periods is not None and filing.accession_number in resolved_periods:
        resolved_fy, resolved_fq = resolved_periods[filing.accession_number]
        if fact.form.startswith("10-Q"):
            if fact.fp == f"Q{resolved_fq}":
                return resolved_fy, resolved_fq
        elif fact.form.startswith("10-K") and fact.fp == "FY" and resolved_fq == 4:
            return resolved_fy, 4
        return None
    if fact.form.startswith("10-Q") and fact.fp in {"Q1", "Q2", "Q3"}:
        return fact.fy, int(fact.fp[-1])
    if fact.form.startswith("10-K") and fact.fp == "FY":
        return fact.fy, 4
    return None


def _ordered_periods(
    facts: tuple[Fact, ...],
    filings: Mapping[str, SecCompanyFactsFiling],
    acceptance_upper: datetime | None,
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
    if not periods:
        return ()
    anchor = max(periods)
    current: list[tuple[int, int]] = []
    year, quarter = anchor
    for _ in range(4):
        current.append((year, quarter))
        if quarter == 1:
            year, quarter = year - 1, 4
        else:
            quarter -= 1
    current.reverse()
    all_periods = [(year - 1, quarter) for year, quarter in current] + current
    return tuple(sorted(all_periods)[-8:])


def _unmodeled_newer_filings(
    facts: tuple[Fact, ...],
    filings: Mapping[str, SecCompanyFactsFiling],
    acceptance_upper: datetime | None,
) -> tuple[str, ...]:
    """Detect later accepted 10-K/Q accessions lacking resolvable SEC fiscal focus."""
    resolved = _resolve_filing_periods(facts, filings, acceptance_upper)
    matched: set[str] = set()
    accepted: list[datetime] = []
    for fact in facts:
        filing = filings.get(fact.accn)
        if filing is None or (
            acceptance_upper is not None and filing.accepted_at > acceptance_upper
        ):
            continue
        if _fiscal_period(fact, filing, resolved) is not None:
            matched.add(fact.accn)
            accepted.append(filing.accepted_at)
    latest_matched = max(accepted) if accepted else None
    return tuple(
        sorted(
            accession
            for accession, filing in filings.items()
            if filing.form in _ALLOWED_FORMS
            and accession not in matched
            and (acceptance_upper is None or filing.accepted_at <= acceptance_upper)
            and (latest_matched is None or filing.accepted_at > latest_matched)
        )
    )


def _periods_from_companyfacts(
    facts: tuple[Fact, ...], acceptance_upper: datetime | None, count: int
) -> tuple[tuple[int, int], ...]:
    resolved = _resolve_filing_periods(facts, acceptance_upper=acceptance_upper)
    available: set[tuple[int, int]] = set()
    for fact in facts:
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
        elif fact.form.startswith("10-Q") and fact.fp in {"Q1", "Q2", "Q3"}:
            if _duration_days(fact) <= 310:
                available.add((fact.fy, int(fact.fp[-1])))
        elif (
            fact.form.startswith("10-K") and fact.fp == "FY" and 300 <= _duration_days(fact) <= 430
        ):
            available.add((fact.fy, 4))
    if not available:
        return ()
    anchor = max(available)

    def window(end: tuple[int, int]) -> set[tuple[int, int]]:
        current: list[tuple[int, int]] = []
        year, quarter = end
        for _ in range(4):
            current.append((year, quarter))
            year, quarter = (year - 1, 4) if quarter == 1 else (year, quarter - 1)
        current.reverse()
        return {(fy - 1, fq) for fy, fq in current} | set(current)

    periods = window(anchor)
    if acceptance_upper is not None:
        # A same-day filing may have been accepted after the requested bound and
        # move the exact anchor back by one quarter. Fetch that alternate window too.
        prior = (anchor[0] - 1, 4) if anchor[1] == 1 else (anchor[0], anchor[1] - 1)
        periods.update(window(prior))
    ordered = tuple(sorted(periods))
    return ordered if acceptance_upper is not None else ordered[-count:]


def _target_fact_accessions(
    facts: tuple[Fact, ...],
    periods: tuple[tuple[int, int], ...],
    filings: Mapping[str, SecCompanyFactsFiling] | None = None,
) -> tuple[dict[str, date], dict[tuple[int, int], set[str]]]:
    needed: dict[str, date] = {}
    by_period: dict[tuple[int, int], set[str]] = {period: set() for period in periods}
    resolved = _resolve_filing_periods(facts, filings)
    for year, quarter in periods:
        for fact in facts:
            accn_period = resolved.get(fact.accn)
            fact_year = accn_period[0] if accn_period is not None else fact.fy
            fact_quarter = (
                accn_period[1]
                if accn_period is not None
                else (
                    4
                    if fact.form.startswith("10-K") and fact.fp == "FY"
                    else int(fact.fp[-1])
                    if fact.form.startswith("10-Q") and fact.fp in {"Q1", "Q2", "Q3"}
                    else None
                )
            )
            eligible = False
            if quarter < 4:
                eligible = (
                    fact.form.startswith("10-Q")
                    and fact.fp == f"Q{quarter}"
                    and (fact_year, fact_quarter) == (year, quarter)
                    and 60 <= _duration_days(fact) <= 120
                )
            elif (
                fact.form.startswith("10-K")
                and fact.fp == "FY"
                and (fact_year, fact_quarter) == (year, 4)
            ):
                duration = _duration_days(fact)
                eligible = 60 <= duration <= 120 or 300 <= duration <= 430
            elif (
                fact.form.startswith("10-Q")
                and fact.fp == "Q3"
                and (fact_year, fact_quarter) == (year, 3)
            ):
                eligible = 240 <= _duration_days(fact) <= 310
            if eligible:
                old = needed.get(fact.accn)
                if old is not None and old != fact.filed:
                    raise SchemaMismatchError("companyfacts accession has conflicting filed dates")
                needed[fact.accn] = fact.filed
                by_period[(year, quarter)].add(fact.accn)
    return needed, by_period


def _evidence(
    fact: SecCompanyFactsFact,
    filing: SecCompanyFactsFiling,
    observation_id: str,
) -> SecCanonicalFactEvidence:
    return SecCanonicalFactEvidence(
        fact.tag,
        fact.unit,
        fact.value,
        fact.start,
        fact.end,
        fact.fy,
        fact.fp,
        fact.frame,
        fact.accn,
        fact.form,
        filing.accepted_at,
        observation_id,
        filing.filing_url,
    )


def project_sec_companyfacts_quarters(
    facts: tuple[Fact, ...],
    filings: Mapping[str, SecCompanyFactsFiling],
    observation_id: str,
    *,
    acceptance_upper: datetime | None = None,
    count: int = 8,
    requested_periods: tuple[tuple[int, int], ...] | None = None,
    incomplete_periods: frozenset[tuple[int, int]] = frozenset(),
    filing_xbrl_observations: Mapping[str, tuple[str, str]] | None = None,
) -> tuple[SecCanonicalQuarterSlot, ...]:
    """Pure projection helper; only facts joined to exact SEC submission records count."""
    if type(count) is not int or not 1 <= count <= 8:
        raise ValueError("count must be in 1..8")
    if acceptance_upper is not None and (
        not isinstance(acceptance_upper, datetime)
        or acceptance_upper.tzinfo is None
        or acceptance_upper.utcoffset() is None
    ):
        raise ValueError("acceptance_upper must be timezone-aware")
    bound = acceptance_upper.astimezone(UTC) if acceptance_upper is not None else None
    xbrl_observations = filing_xbrl_observations or {}

    def evidence_for(
        fact: Fact, filing: SecCompanyFactsFiling
    ) -> SecCanonicalFactEvidence | SecFilingXbrlFactEvidence:
        if isinstance(fact, SecFilingXbrlFact):
            observation_pair = xbrl_observations.get(fact.accn)
            if observation_pair is None:
                raise SchemaMismatchError("SEC filing XBRL evidence identity is missing")
            return SecFilingXbrlFactEvidence(
                fact.tag,
                fact.unit,
                fact.value,
                fact.start,
                fact.end,
                fact.fiscal_year_focus,
                fact.fiscal_period_focus,
                fact.accession_number,
                fact.form,
                filing.accepted_at,
                observation_pair[0],
                observation_pair[1],
                filing.filing_url,
            )
        return _evidence(fact, filing, observation_id)

    resolved = _resolve_filing_periods(facts, filings, bound)
    periods = requested_periods or _ordered_periods(facts, filings, bound)
    if len(periods) > 8 or any(
        type(year) is not int
        or not 1 <= year <= 9999
        or type(quarter) is not int
        or quarter not in range(1, 5)
        for year, quarter in periods
    ):
        raise ValueError("requested_periods must contain up to eight fiscal quarter keys")
    if count < len(periods):
        periods = periods[-count:]

    def candidates(
        metric: SecCanonicalMetric, year: int, quarter: int
    ) -> tuple[
        list[SecCanonicalFactEvidence | SecFilingXbrlFactEvidence],
        bool,
        tuple[SecCanonicalFactEvidence | SecFilingXbrlFactEvidence, ...],
    ]:
        tags = SEC_CANONICAL_CONCEPTS[metric.value]
        valid: list[SecCanonicalFactEvidence | SecFilingXbrlFactEvidence] = []

        def add_candidate(evidence: SecCanonicalFactEvidence | SecFilingXbrlFactEvidence) -> None:
            if len(valid) >= _MAX_FIELD_ALTERNATIVES:
                raise ResourceLimitError("SEC quarterly field alternative limit exceeded")
            valid.append(evidence)

        if quarter < 4:
            for fact in facts:
                filing = filings.get(fact.accn)
                if filing is None or (bound is not None and filing.accepted_at > bound):
                    continue
                if fact.tag not in tags or fact.form not in {"10-Q", "10-Q/A"}:
                    continue
                if _fiscal_period(fact, filing, resolved) != (year, quarter):
                    continue
                if fact.fp != f"Q{quarter}" or not _valid_quarter_frame(fact):
                    continue
                duration = _duration_days(fact)
                if not 60 <= duration <= 120 or fact.unit not in _UNITS[metric.value]:
                    continue
                add_candidate(evidence_for(fact, filing))
        elif metric.value in _FLOW_METRICS:
            fy_rows: list[Fact] = []
            ytd_rows: list[Fact] = []
            for fact in facts:
                filing = filings.get(fact.accn)
                if filing is None or (bound is not None and filing.accepted_at > bound):
                    continue
                if fact.tag not in tags or fact.unit not in _UNITS[metric.value]:
                    continue
                period = _fiscal_period(fact, filing, resolved)
                if fact.form in {"10-K", "10-K/A"} and fact.fp == "FY" and period == (year, 4):
                    if 300 <= _duration_days(fact) <= 430:
                        if len(fy_rows) >= _MAX_PAIR_ROWS:
                            raise ResourceLimitError("SEC annual candidate limit exceeded")
                        fy_rows.append(fact)
                elif (
                    fact.form in {"10-Q", "10-Q/A"}
                    and fact.fp == "Q3"
                    and period == (year, 3)
                    and 240 <= _duration_days(fact) <= 310
                ):
                    if len(ytd_rows) >= _MAX_PAIR_ROWS:
                        raise ResourceLimitError("SEC Q3 YTD candidate limit exceeded")
                    ytd_rows.append(fact)
            for annual in fy_rows:
                if annual.form.endswith("/A"):
                    add_candidate(evidence_for(annual, filings[annual.accn]))
            for ytd in ytd_rows:
                if ytd.form.endswith("/A"):
                    add_candidate(evidence_for(ytd, filings[ytd.accn]))
            ytd_by_key: dict[tuple[str, str, date], list[Fact]] = {}
            for ytd in ytd_rows:
                ytd_by_key.setdefault((ytd.tag, ytd.unit, ytd.start), []).append(ytd)
            pair_attempts = 0
            for annual in fy_rows:
                for ytd in ytd_by_key.get((annual.tag, annual.unit, annual.start), ()):
                    pair_attempts += 1
                    if pair_attempts > _MAX_PAIR_ATTEMPTS:
                        raise ResourceLimitError("SEC Q4 candidate pair limit exceeded")
                    if annual.end <= ytd.end:
                        continue
                    remaining_days = (annual.end - ytd.end).days
                    if not 60 <= remaining_days <= 120:
                        continue
                    # Amendments remain alternatives. Cross-accession arithmetic that
                    # involves either /A filing is not used absent explicit cohort proof.
                    if annual.form.endswith("/A") or ytd.form.endswith("/A"):
                        continue
                    filing_fy, filing_ytd = filings[annual.accn], filings[ytd.accn]
                    value = exact_sum((annual.value, ytd.value.copy_negate()))
                    add_candidate(
                        SecCanonicalFactEvidence(
                            f"{annual.tag}−{ytd.tag}",
                            annual.unit,
                            value,
                            ytd.end + timedelta(days=1),
                            annual.end,
                            year,
                            "Q4",
                            None,
                            annual.accn,
                            annual.form,
                            max(filing_fy.accepted_at, filing_ytd.accepted_at),
                            observation_id,
                            filing_fy.filing_url,
                            (
                                evidence_for(annual, filing_fy),
                                evidence_for(ytd, filing_ytd),
                            ),
                        )
                    )
        else:
            # No Q4 EPS subtraction. Only an explicitly Q4-focused 10-K fact is
            # eligible; ordinary FY facts do not establish a discrete quarter.
            for fact in facts:
                filing = filings.get(fact.accn)
                if filing is None or (bound is not None and filing.accepted_at > bound):
                    continue
                period = _fiscal_period(fact, filing, resolved)
                if (
                    fact.tag in tags
                    and fact.form in {"10-K", "10-K/A"}
                    and fact.fp == "FY"
                    and period == (year, 4)
                    and fact.unit in _UNITS[metric.value]
                    and 60 <= _duration_days(fact) <= 120
                    and _valid_quarter_frame(fact)
                ):
                    add_candidate(evidence_for(fact, filing))
        # Every retained source accession and unit remains visible even when a
        # versioned semantic rule selects one canonical value.
        return finalize_canonical_candidates(metric, valid, filings)

    slots: list[SecCanonicalQuarterSlot] = []
    for year, quarter in periods:
        fields: list[SecCanonicalQuarterField] = []
        for metric in SecCanonicalMetric:
            found, incompatible_amendment, supporting = candidates(metric, year, quarter)
            if (year, quarter) in incomplete_periods:
                field = SecCanonicalQuarterField(
                    metric,
                    SecCanonicalFieldStatus.COVERAGE_INCOMPLETE,
                    None,
                    None,
                    supporting,
                )
            elif not found:
                field = SecCanonicalQuarterField(
                    metric, SecCanonicalFieldStatus.MISSING, None, None, ()
                )
            elif len(found) > 1 or incompatible_amendment:
                field = SecCanonicalQuarterField(
                    metric, SecCanonicalFieldStatus.AMBIGUOUS, None, None, supporting
                )
            else:
                field = SecCanonicalQuarterField(
                    metric,
                    SecCanonicalFieldStatus.PRESENT,
                    found[0].value,
                    found[0].unit,
                    supporting,
                )
            fields.append(field)
        slots.append(SecCanonicalQuarterSlot(year, quarter, tuple(fields)))
    return tuple(slots)


__all__ = [
    "_periods_from_companyfacts",
    "_target_fact_accessions",
    "_unmodeled_newer_filings",
    "project_sec_companyfacts_quarters",
]

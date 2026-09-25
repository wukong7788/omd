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
)
from ._metric_arithmetic import exact_sum
from .errors import ResourceLimitError, SchemaMismatchError

_SERIALIZATION = "sec-companyfacts-json-v1"
_MAX_FIELD_ALTERNATIVES = 4_096
_MAX_PAIR_ROWS = 4_096
_MAX_PAIR_ATTEMPTS = 10_000


def _duration_days(fact: SecCompanyFactsFact) -> int:
    return (fact.end - fact.start).days + 1


def _valid_quarter_frame(fact: SecCompanyFactsFact) -> bool:
    return fact.frame is None or _QUARTER_FRAME.fullmatch(fact.frame) is not None


def _fiscal_period(
    fact: SecCompanyFactsFact, filing: SecCompanyFactsFiling
) -> tuple[int, int] | None:
    if fact.accn != filing.accession_number or fact.form != filing.form:
        return None
    if fact.filed > filing.accepted_at.date() or fact.end != filing.report_date:
        return None
    if fact.form.startswith("10-Q") and fact.fp in {"Q1", "Q2", "Q3"}:
        return fact.fy, int(fact.fp[-1])
    if fact.form.startswith("10-K") and fact.fp == "FY":
        return fact.fy, 4
    return None


def _ordered_periods(
    facts: tuple[SecCompanyFactsFact, ...],
    filings: Mapping[str, SecCompanyFactsFiling],
    acceptance_upper: datetime | None,
) -> tuple[tuple[int, int], ...]:
    periods: set[tuple[int, int]] = set()
    for fact in facts:
        filing = filings.get(fact.accn)
        if filing is None or (
            acceptance_upper is not None and filing.accepted_at > acceptance_upper
        ):
            continue
        period = _fiscal_period(fact, filing)
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
    facts: tuple[SecCompanyFactsFact, ...],
    filings: Mapping[str, SecCompanyFactsFiling],
    acceptance_upper: datetime | None,
) -> tuple[str, ...]:
    """Detect later accepted 10-K/Q accessions lacking resolvable SEC fiscal focus."""
    matched: set[str] = set()
    accepted: list[datetime] = []
    for fact in facts:
        filing = filings.get(fact.accn)
        if filing is None or (
            acceptance_upper is not None and filing.accepted_at > acceptance_upper
        ):
            continue
        if _fiscal_period(fact, filing) is not None:
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
    facts: tuple[SecCompanyFactsFact, ...], acceptance_upper: datetime | None, count: int
) -> tuple[tuple[int, int], ...]:
    available: set[tuple[int, int]] = set()
    for fact in facts:
        if acceptance_upper is not None and fact.filed > acceptance_upper.date():
            continue
        if fact.form.startswith("10-Q") and fact.fp in {"Q1", "Q2", "Q3"}:
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
    facts: tuple[SecCompanyFactsFact, ...], periods: tuple[tuple[int, int], ...]
) -> tuple[dict[str, date], dict[tuple[int, int], set[str]]]:
    needed: dict[str, date] = {}
    by_period: dict[tuple[int, int], set[str]] = {period: set() for period in periods}
    for year, quarter in periods:
        for fact in facts:
            eligible = False
            if quarter < 4:
                eligible = (
                    fact.form.startswith("10-Q")
                    and fact.fp == f"Q{quarter}"
                    and fact.fy == year
                    and 60 <= _duration_days(fact) <= 120
                )
            elif fact.form.startswith("10-K") and fact.fp == "FY" and fact.fy == year:
                duration = _duration_days(fact)
                eligible = 60 <= duration <= 120 or 300 <= duration <= 430
            elif fact.form.startswith("10-Q") and fact.fp == "Q3" and fact.fy == year:
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
    facts: tuple[SecCompanyFactsFact, ...],
    filings: Mapping[str, SecCompanyFactsFiling],
    observation_id: str,
    *,
    acceptance_upper: datetime | None = None,
    count: int = 8,
    requested_periods: tuple[tuple[int, int], ...] | None = None,
    incomplete_periods: frozenset[tuple[int, int]] = frozenset(),
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
    ) -> tuple[list[SecCanonicalFactEvidence], bool]:
        tags = SEC_CANONICAL_CONCEPTS[metric.value]
        valid: list[SecCanonicalFactEvidence] = []
        incompatible_amendment = False

        def add_candidate(evidence: SecCanonicalFactEvidence) -> None:
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
                if _fiscal_period(fact, filing) != (year, quarter):
                    continue
                if fact.fp != f"Q{quarter}" or not _valid_quarter_frame(fact):
                    continue
                duration = _duration_days(fact)
                if not 60 <= duration <= 120 or fact.unit not in _UNITS[metric.value]:
                    continue
                if fact.form.endswith("/A"):
                    incompatible_amendment = True
                add_candidate(_evidence(fact, filing, observation_id))
        elif metric.value in _FLOW_METRICS:
            fy_rows: list[SecCompanyFactsFact] = []
            ytd_rows: list[SecCompanyFactsFact] = []
            for fact in facts:
                filing = filings.get(fact.accn)
                if filing is None or (bound is not None and filing.accepted_at > bound):
                    continue
                if fact.tag not in tags or fact.unit not in _UNITS[metric.value]:
                    continue
                if fact.fy != year:
                    continue
                if (
                    fact.form in {"10-K", "10-K/A"}
                    and fact.fp == "FY"
                    and _fiscal_period(fact, filing) == (year, 4)
                ):
                    if 300 <= _duration_days(fact) <= 430:
                        if len(fy_rows) >= _MAX_PAIR_ROWS:
                            raise ResourceLimitError("SEC annual candidate limit exceeded")
                        fy_rows.append(fact)
                elif (
                    fact.form in {"10-Q", "10-Q/A"}
                    and fact.fp == "Q3"
                    and _fiscal_period(fact, filing) == (year, 3)
                    and 240 <= _duration_days(fact) <= 310
                ):
                    if len(ytd_rows) >= _MAX_PAIR_ROWS:
                        raise ResourceLimitError("SEC Q3 YTD candidate limit exceeded")
                    ytd_rows.append(fact)
            for annual in fy_rows:
                if annual.form.endswith("/A"):
                    incompatible_amendment = True
                    add_candidate(_evidence(annual, filings[annual.accn], observation_id))
            for ytd in ytd_rows:
                if ytd.form.endswith("/A"):
                    incompatible_amendment = True
                    add_candidate(_evidence(ytd, filings[ytd.accn], observation_id))
            ytd_by_key: dict[tuple[str, str, date], list[SecCompanyFactsFact]] = {}
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
                                _evidence(annual, filing_fy, observation_id),
                                _evidence(ytd, filing_ytd, observation_id),
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
                if (
                    fact.tag in tags
                    and fact.form in {"10-K", "10-K/A"}
                    and fact.fp == "FY"
                    and fact.fy == year
                    and _fiscal_period(fact, filing) == (year, 4)
                    and fact.unit in _UNITS[metric.value]
                    and 60 <= _duration_days(fact) <= 120
                    and _valid_quarter_frame(fact)
                ):
                    if fact.form.endswith("/A"):
                        incompatible_amendment = True
                    add_candidate(_evidence(fact, filing, observation_id))
        # Two tags or two filing accessions are retained as ambiguous alternatives.
        # Every retained source accession and unit is part of the alternative.
        # Equal Q4 values from different Q3 filings are still distinct evidence.
        dedup = {item: item for item in valid}
        return list(dedup.values()), incompatible_amendment

    slots: list[SecCanonicalQuarterSlot] = []
    for year, quarter in periods:
        fields: list[SecCanonicalQuarterField] = []
        for metric in SecCanonicalMetric:
            found, incompatible_amendment = candidates(metric, year, quarter)
            if (year, quarter) in incomplete_periods:
                field = SecCanonicalQuarterField(
                    metric,
                    SecCanonicalFieldStatus.COVERAGE_INCOMPLETE,
                    None,
                    None,
                    tuple(found),
                )
            elif not found:
                field = SecCanonicalQuarterField(
                    metric, SecCanonicalFieldStatus.MISSING, None, None, ()
                )
            elif len(found) > 1 or incompatible_amendment:
                field = SecCanonicalQuarterField(
                    metric, SecCanonicalFieldStatus.AMBIGUOUS, None, None, tuple(found)
                )
            else:
                field = SecCanonicalQuarterField(
                    metric,
                    SecCanonicalFieldStatus.PRESENT,
                    found[0].value,
                    found[0].unit,
                    tuple(found),
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

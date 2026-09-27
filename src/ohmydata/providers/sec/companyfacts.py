"""SEC-only companyfacts fetching and canonical quarterly projection API."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

from ...core import RequestSpec, SnapshotMode, SnapshotStore
from ._companyfacts_models import (
    _MAX_BODY_BYTES,
    _SERIALIZATION,
    SEC_CANONICAL_CONCEPTS,
    SEC_CANONICAL_CONCEPTS_VERSION,
    SEC_COMPANYFACTS_URL,
    Sec8KReleaseEvidence,
    SecCanonicalEvidence,
    SecCanonicalFactEvidence,
    SecCanonicalFieldStatus,
    SecCanonicalMetric,
    SecCanonicalQuarterField,
    SecCanonicalQuarterlyResult,
    SecCanonicalQuarterSlot,
    SecCompanyFactsFact,
    SecCompanyFactsFiling,
    SecFilingXbrlFactEvidence,
    parse_sec_companyfacts_payload,
)
from ._companyfacts_periods import _resolve_filing_periods
from ._companyfacts_projection import (
    _ordered_periods,
    _periods_from_companyfacts,
    _target_fact_accessions,
    _unmodeled_newer_filings,
    project_sec_companyfacts_quarters,
)
from ._companyfacts_submissions import (
    _fetch_history_page,
    _history_page_selection,
    _root_payload,
    _submission_filings,
)
from ._event_discovery_models import SecDiscoverySource
from ._filing_xbrl import SecFilingXbrlFact, fetch_sec_filing_xbrl_source
from .errors import CoverageError, ResourceLimitError, SchemaMismatchError, TransientProviderError
from .http import SecHttpClient, validate_sec_url
from .quarterly import (
    SecCompanyEligibilityStatus,
    SecQuarterlyEligibility,
    classify_sec_company_eligibility_from_root,
)
from .submissions import fetch_sec_submissions_root
from .ticker_cik import (
    SecTickerCikAmbiguousError,
    SecTickerCikNotFoundError,
    fetch_sec_ticker_cik_mapping,
)


def _empty_result(ticker: str, eligibility: SecQuarterlyEligibility) -> SecCanonicalQuarterlyResult:
    return SecCanonicalQuarterlyResult(
        ticker.strip().upper(),
        eligibility.cik,
        eligibility,
        (),
        None,
        tuple(item for item in (eligibility.submissions_observation_id,) if item is not None),
    )


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
            return {
                metric
                for fact in facts
                if fact.accn == accession
                for metric, tags in SEC_CANONICAL_CONCEPTS.items()
                if fact.tag in tags
            }

        if metric_classes(base.accession_number) - metric_classes(amended_accession):
            period = resolved.get(amended_accession)
            if period is not None:
                incomplete_periods.add(period)
                incomplete_accessions.add(amended_accession)
    return frozenset(incomplete_periods), tuple(sorted(incomplete_accessions))


def fetch_sec_canonical_quarters(
    ticker: str,
    client: SecHttpClient,
    store: SnapshotStore,
    *,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    acceptance_upper: datetime | None = None,
    count: int = 8,
    enrich_8k_q4_eps: bool = False,
) -> SecCanonicalQuarterlyResult:
    """Fetch retained SEC companyfacts and project up to eight fiscal quarters.

    Ticker/submissions transient failures are returned as fail-closed eligibility
    outcomes. Companyfacts transport/schema failures propagate to the caller.
    """
    if not isinstance(client, SecHttpClient) or not isinstance(store, SnapshotStore):
        raise TypeError("client and store must be SEC HTTP and snapshot clients")
    if not callable(utc_now):
        raise TypeError("utc_now must be callable")
    if type(count) is not int or not 1 <= count <= 8:
        raise ValueError("count must be in 1..8")
    canonical_ticker = ticker.strip().upper() if type(ticker) is str else ""
    if not canonical_ticker:
        raise ValueError("ticker must be non-empty")
    bound = acceptance_upper
    if bound is not None and (
        not isinstance(bound, datetime) or bound.tzinfo is None or bound.utcoffset() is None
    ):
        raise ValueError("acceptance_upper must be timezone-aware")
    try:
        mapping = fetch_sec_ticker_cik_mapping(client, store, utc_now=utc_now)
        try:
            resolution = mapping.resolve(canonical_ticker)
        except SecTickerCikNotFoundError:
            from .quarterly import SecQuarterlyEligibility

            eligibility = SecQuarterlyEligibility(
                SecCompanyEligibilityStatus.NON_SEC,
                canonical_ticker,
                None,
                None,
                mapping.observation.observation_identity,
                None,
                (),
                "ticker is not listed in this SEC ticker mapping observation",
            )
            return _empty_result(canonical_ticker, eligibility)
        except SecTickerCikAmbiguousError:
            from .quarterly import SecQuarterlyEligibility

            eligibility = SecQuarterlyEligibility(
                SecCompanyEligibilityStatus.UNKNOWN,
                canonical_ticker,
                None,
                None,
                mapping.observation.observation_identity,
                None,
                (),
                "SEC ticker mapping has conflicting CIK identities",
            )
            return _empty_result(canonical_ticker, eligibility)
        root_source = fetch_sec_submissions_root(store, client, resolution.cik, utc_now=utc_now)
    except TransientProviderError:
        from .quarterly import SecQuarterlyEligibility

        eligibility = SecQuarterlyEligibility(
            SecCompanyEligibilityStatus.TRANSIENT_FAILURE,
            canonical_ticker,
            None,
            None,
            None,
            None,
            (),
            "SEC ticker or submissions source request failed transiently",
        )
        return _empty_result(canonical_ticker, eligibility)

    eligibility = classify_sec_company_eligibility_from_root(
        canonical_ticker, mapping, store, root_source
    )
    if eligibility.status is not SecCompanyEligibilityStatus.SEC_COMPANY:
        return _empty_result(canonical_ticker, eligibility)

    cik = f"{int(resolution.cik):010d}"
    root_payload = _root_payload(store, root_source, cik)
    url = SEC_COMPANYFACTS_URL.format(cik=cik)
    response = client.open(
        url,
        accept="application/json",
        max_bytes=_MAX_BODY_BYTES,
        redirect_validator=lambda actual: _exact_companyfacts_url(url, actual),
    )
    try:
        if validate_sec_url(response.url) != validate_sec_url(url):
            raise SchemaMismatchError("SEC companyfacts redirect changed source URL")
        try:
            body = response.body.read()
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise TransientProviderError("SEC companyfacts response read failed") from exc
    finally:
        response.body.close()
    raw_facts = parse_sec_companyfacts_payload(body, cik)
    observation = store.observe(
        RequestSpec("sec", "companyfacts", {"cik": cik}),
        body,
        utc_now(),
        _SERIALIZATION,
        SnapshotMode.APPEND,
    )
    initial_periods = _periods_from_companyfacts(raw_facts, bound, count)
    if not initial_periods:
        return SecCanonicalQuarterlyResult(
            canonical_ticker,
            cik,
            eligibility,
            (),
            observation.observation_identity,
            (root_source.observation.observation_identity,),
            False,
            False,
            (),
        )
    root_filings, _ = _submission_filings(store, cik, root_source)
    needed, by_period = _target_fact_accessions(raw_facts, initial_periods, root_filings)
    history_names, uncovered = _history_page_selection(root_payload, cik, needed, set(root_filings))
    if len(history_names) > 256:
        raise ResourceLimitError("targeted SEC history page count exceeds limit")
    historical_sources: list[SecDiscoverySource] = []
    total_history_bytes = 0
    for name in history_names:
        source = _fetch_history_page(client, store, cik, name, utc_now)
        replayed = store.replay_observation(source.observation, max_payload_bytes=8 * 1024**2)
        total_history_bytes += len(replayed.payload)
        if total_history_bytes > 64 * 1024**2:
            raise ResourceLimitError("targeted SEC history bytes exceed limit")
        historical_sources.append(source)
    filings, submission_observations = _submission_filings(
        store, cik, root_source, tuple(historical_sources)
    )
    filing_xbrl_facts: list[SecFilingXbrlFact] = []
    filing_xbrl_observations: dict[str, tuple[str, str]] = {}
    unresolved_before_xbrl = _unmodeled_newer_filings(raw_facts, filings, bound)
    for accession in unresolved_before_xbrl:
        filing = filings.get(accession)
        if (
            filing is None
            or filing.form not in {"10-Q", "10-Q/A"}
            or filing.primary_document is None
        ):
            continue
        try:
            source = fetch_sec_filing_xbrl_source(
                client=client,
                store=store,
                cik=cik,
                accession_number=accession,
                form=filing.form,
                report_date=filing.report_date,
                filing_date=filing.filing_date or filing.accepted_at.date(),
                accepted_at=filing.accepted_at,
                primary_document=filing.primary_document,
                utc_now=utc_now,
            )
        except CoverageError:
            # A 404 for this filing's index/instance is incomplete evidence; it
            # must not be treated as an empty filing or as resolved coverage.
            continue
        filing_xbrl_observations[accession] = (
            source.index_observation.observation_identity,
            source.instance_observation.observation_identity,
        )
        if not source.facts:
            continue
        filing_xbrl_facts.extend(source.facts)
    combined_facts = raw_facts + tuple(filing_xbrl_facts)
    needed, by_period = _target_fact_accessions(combined_facts, initial_periods, filings)
    uncovered = tuple(sorted(set(uncovered) | (set(needed) - set(filings))))
    unresolved_period_accessions = _unmodeled_newer_filings(combined_facts, filings, bound)
    if unresolved_period_accessions:
        uncovered = tuple(sorted(set(uncovered) | set(unresolved_period_accessions)))
        return SecCanonicalQuarterlyResult(
            canonical_ticker,
            cik,
            eligibility,
            (),
            observation.observation_identity,
            submission_observations,
            False,
            False,
            uncovered,
            unresolved_period_accessions,
            filing_xbrl_observation_ids=tuple(
                observation_id
                for pair in filing_xbrl_observations.values()
                for observation_id in pair
            ),
        )
    resolved_periods = _ordered_periods(combined_facts, filings, bound)
    incomplete_amendment_periods, incomplete_amendment_accessions = _incomplete_xbrl_amendments(
        combined_facts, filings, bound, set(filing_xbrl_observations)
    )
    requested_periods = resolved_periods[-count:]
    xbrl_resolved = _resolve_filing_periods(combined_facts, filings, bound)
    xbrl_periods = {
        xbrl_resolved[accession]
        for accession in filing_xbrl_observations
        if accession in xbrl_resolved
    }
    if not set(requested_periods).issubset(set(initial_periods) | xbrl_periods):
        # A same-day filing (or revision) can move the accepted-time anchor
        # farther back than the candidate window. Its older accessions were
        # not selected for historical replay, so no complete projection exists.
        return SecCanonicalQuarterlyResult(
            canonical_ticker,
            cik,
            eligibility,
            (),
            observation.observation_identity,
            submission_observations,
            False,
            False,
            uncovered,
        )
    required_accessions = {
        accession for period in requested_periods for accession in by_period.get(period, ())
    }
    uncovered = tuple(accession for accession in uncovered if accession in required_accessions)
    uncovered = tuple(sorted(set(uncovered) | set(incomplete_amendment_accessions)))
    uncovered_periods = frozenset(
        period
        for period in requested_periods
        if by_period.get(period, set()).intersection(uncovered)
    ) | frozenset(period for period in requested_periods if period in incomplete_amendment_periods)
    slots = project_sec_companyfacts_quarters(
        combined_facts,
        filings,
        observation.observation_identity,
        acceptance_upper=bound,
        count=count,
        requested_periods=requested_periods,
        incomplete_periods=uncovered_periods,
        filing_xbrl_observations=filing_xbrl_observations,
    )
    result = SecCanonicalQuarterlyResult(
        canonical_ticker,
        cik,
        eligibility,
        slots,
        observation.observation_identity,
        submission_observations,
        not uncovered,
        bool(resolved_periods),
        uncovered,
        filing_xbrl_observation_ids=tuple(
            observation_id for pair in filing_xbrl_observations.values() for observation_id in pair
        ),
    )
    if enrich_8k_q4_eps:
        from .earnings_8k import (
            enrich_sec_canonical_quarters_with_8k_eps,
            fetch_sec_8k_quarterly_eps,
        )

        missing_q4s: list[tuple[int, int, date]] = []
        for slot in result.slots:
            if slot.fiscal_quarter == 4:
                eps_field = slot.field(SecCanonicalMetric.GAAP_DILUTED_EPS)
                if eps_field.status is SecCanonicalFieldStatus.MISSING:
                    rev_field = slot.field(SecCanonicalMetric.REVENUE)
                    period_end = rev_field.evidence[0].period_end if rev_field.evidence else None
                    if period_end is not None:
                        missing_q4s.append((slot.fiscal_year, 4, period_end))
        if missing_q4s:
            eps_result = fetch_sec_8k_quarterly_eps(
                canonical_ticker,
                client,
                store,
                target_quarters=tuple(missing_q4s),
                acceptance_upper=bound,
                utc_now=utc_now,
            )
            result = enrich_sec_canonical_quarters_with_8k_eps(result, eps_result)
    return result


def _exact_companyfacts_url(expected: str, actual: str) -> str:
    if validate_sec_url(actual) != validate_sec_url(expected):
        raise SchemaMismatchError("SEC companyfacts redirect changed source URL")
    return actual


__all__ = [
    "SEC_CANONICAL_CONCEPTS",
    "SEC_CANONICAL_CONCEPTS_VERSION",
    "SEC_COMPANYFACTS_URL",
    "Sec8KReleaseEvidence",
    "SecCanonicalEvidence",
    "SecCanonicalFactEvidence",
    "SecCanonicalFieldStatus",
    "SecCanonicalMetric",
    "SecCanonicalQuarterField",
    "SecCanonicalQuarterSlot",
    "SecCanonicalQuarterlyResult",
    "SecCompanyFactsFact",
    "SecCompanyFactsFiling",
    "SecFilingXbrlFactEvidence",
    "fetch_sec_canonical_quarters",
    "parse_sec_companyfacts_payload",
    "project_sec_companyfacts_quarters",
]

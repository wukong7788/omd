"""SEC-only companyfacts fetching and canonical quarterly projection API."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from ...core import RequestSpec, SnapshotMode, SnapshotStore
from ._companyfacts_models import (
    _MAX_BODY_BYTES,
    _SERIALIZATION,
    SEC_CANONICAL_CONCEPTS,
    SEC_CANONICAL_CONCEPTS_VERSION,
    SEC_CANONICAL_IFRS_CONCEPTS,
    SEC_COMPANYFACTS_URL,
    SEC_FUNDAMENTAL_CONCEPTS,
    SEC_MULTI_TAXONOMY_CONCEPTS_VERSION,
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
    SecFundamentalMetric,
    normalize_sec_cik,
    parse_sec_companyfacts_payload,
)
from ._companyfacts_periods import _resolve_filing_periods
from ._companyfacts_projection import (
    _ordered_periods,
    _periods_from_companyfacts,
    _target_fact_accessions,
    _unmodeled_newer_filings,
    _validate_window_args,
    project_sec_companyfacts_quarters,
)
from ._companyfacts_selection import _incomplete_xbrl_amendments
from ._companyfacts_submissions import (
    _classify_cik_eligibility_from_root,
    _enrich_result_with_8k_q4_eps,
    _fetch_companyfacts_body,
    _fetch_history_page,  # noqa: F401
    _fetch_targeted_history_pages,
    _history_page_selection,
    _root_payload,
    _submission_filings,
    _summarize_taxonomy_and_currency,
)
from ._event_discovery_models import SecDiscoverySource
from ._filing_xbrl import SecFilingXbrlFact, fetch_sec_filing_xbrl_source
from .errors import CoverageError, TransientProviderError
from .http import SecHttpClient
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


def _fetch_sec_canonical_quarters_from_root(
    *,
    canonical_ticker: str,
    cik: str,
    eligibility: SecQuarterlyEligibility,
    root_source: SecDiscoverySource,
    root_payload: dict[str, object],
    client: SecHttpClient,
    store: SnapshotStore,
    utc_now: Callable[[], datetime],
    bound: datetime | None,
    count: int | None,
    start_year: int | None,
    end_year: int | None,
    enrich_8k_q4_eps: bool,
) -> SecCanonicalQuarterlyResult:
    url = SEC_COMPANYFACTS_URL.format(cik=cik)
    body = _fetch_companyfacts_body(client, url, _MAX_BODY_BYTES)
    raw_facts = parse_sec_companyfacts_payload(body, cik)
    observation = store.observe(
        RequestSpec("sec", "companyfacts", {"cik": cik}),
        body,
        utc_now(),
        _SERIALIZATION,
        SnapshotMode.APPEND,
    )
    initial_periods = _periods_from_companyfacts(
        raw_facts, bound, count, start_year=start_year, end_year=end_year
    )
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
    historical_sources = _fetch_targeted_history_pages(client, store, cik, history_names, utc_now)
    filings, submission_observations = _submission_filings(
        store, cik, root_source, historical_sources
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
    taxonomy, reporting_currency, dei_shares = _summarize_taxonomy_and_currency(
        raw_facts, filings, bound
    )
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
            taxonomy=taxonomy,
            reporting_currency=reporting_currency,
            dei_shares_outstanding=dei_shares,
        )
    resolved_periods = _ordered_periods(
        combined_facts,
        filings,
        bound,
        count=count,
        start_year=start_year,
        end_year=end_year,
    )
    incomplete_amendment_periods, incomplete_amendment_accessions = _incomplete_xbrl_amendments(
        combined_facts, filings, bound, set(filing_xbrl_observations)
    )
    requested_periods = resolved_periods[-count:] if count is not None else resolved_periods
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
            taxonomy=taxonomy,
            reporting_currency=reporting_currency,
            dei_shares_outstanding=dei_shares,
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
        start_year=start_year,
        end_year=end_year,
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
        taxonomy=taxonomy,
        reporting_currency=reporting_currency,
        dei_shares_outstanding=dei_shares,
    )
    if enrich_8k_q4_eps:
        result = _enrich_result_with_8k_q4_eps(
            result, canonical_ticker, client, store, bound, utc_now
        )
    return result


def fetch_sec_canonical_quarters(
    ticker: str,
    client: SecHttpClient,
    store: SnapshotStore,
    *,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    acceptance_upper: datetime | None = None,
    count: int | None = 8,
    start_year: int | None = None,
    end_year: int | None = None,
    enrich_8k_q4_eps: bool = False,
) -> SecCanonicalQuarterlyResult:
    """Fetch retained SEC companyfacts and project fiscal quarters.

    Ticker/submissions transient failures are returned as fail-closed eligibility
    outcomes. Companyfacts transport/schema failures propagate to the caller.
    """
    if not isinstance(client, SecHttpClient) or not isinstance(store, SnapshotStore):
        raise TypeError("client and store must be SEC HTTP and snapshot clients")
    if not callable(utc_now):
        raise TypeError("utc_now must be callable")
    _validate_window_args(count, start_year, end_year)
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

    cik = f"{int(resolution.cik):010d}"
    eligibility = classify_sec_company_eligibility_from_root(
        canonical_ticker, mapping, store, root_source
    )
    root_payload = _root_payload(store, root_source, cik)
    if eligibility.status is SecCompanyEligibilityStatus.UNKNOWN:
        fpi_eligibility = _classify_cik_eligibility_from_root(
            canonical_ticker,
            cik,
            root_payload,
            root_source.observation.observation_identity,
            mapping.observation.observation_identity,
        )
        if fpi_eligibility.status is SecCompanyEligibilityStatus.SEC_COMPANY:
            eligibility = fpi_eligibility
    if eligibility.status is not SecCompanyEligibilityStatus.SEC_COMPANY:
        return _empty_result(canonical_ticker, eligibility)

    return _fetch_sec_canonical_quarters_from_root(
        canonical_ticker=canonical_ticker,
        cik=cik,
        eligibility=eligibility,
        root_source=root_source,
        root_payload=root_payload,
        client=client,
        store=store,
        utc_now=utc_now,
        bound=bound,
        count=count,
        start_year=start_year,
        end_year=end_year,
        enrich_8k_q4_eps=enrich_8k_q4_eps,
    )


def fetch_sec_canonical_quarters_by_cik(
    cik: str | int,
    client: SecHttpClient,
    store: SnapshotStore,
    *,
    ticker: str | None = None,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    acceptance_upper: datetime | None = None,
    count: int | None = 8,
    start_year: int | None = None,
    end_year: int | None = None,
    enrich_8k_q4_eps: bool = False,
) -> SecCanonicalQuarterlyResult:
    """Fetch retained SEC companyfacts directly by CIK (including delisted companies)."""
    if not isinstance(client, SecHttpClient) or not isinstance(store, SnapshotStore):
        raise TypeError("client and store must be SEC HTTP and snapshot clients")
    if not callable(utc_now):
        raise TypeError("utc_now must be callable")
    normalized_cik = normalize_sec_cik(cik)
    _validate_window_args(count, start_year, end_year)
    if ticker is not None and (type(ticker) is not str or not ticker.strip()):
        raise ValueError("ticker must be a non-empty string when provided")
    bound = acceptance_upper
    if bound is not None and (
        not isinstance(bound, datetime) or bound.tzinfo is None or bound.utcoffset() is None
    ):
        raise ValueError("acceptance_upper must be timezone-aware")
    fallback_ticker = ticker.strip().upper() if ticker is not None else f"CIK{normalized_cik}"
    try:
        root_source = fetch_sec_submissions_root(store, client, normalized_cik, utc_now=utc_now)
    except TransientProviderError:
        eligibility = SecQuarterlyEligibility(
            SecCompanyEligibilityStatus.TRANSIENT_FAILURE,
            fallback_ticker,
            normalized_cik,
            None,
            None,
            None,
            (),
            "SEC submissions source request failed transiently",
        )
        return _empty_result(fallback_ticker, eligibility)

    root_payload = _root_payload(store, root_source, normalized_cik)
    resolved_ticker = fallback_ticker
    has_real_ticker = ticker is not None
    if ticker is None:
        raw_tickers = root_payload.get("tickers")
        if isinstance(raw_tickers, (list, tuple)):
            for item in raw_tickers:
                if isinstance(item, str) and item.strip():
                    resolved_ticker = item.strip().upper()
                    has_real_ticker = True
                    break
    if enrich_8k_q4_eps and not has_real_ticker:
        raise ValueError(
            "enrich_8k_q4_eps requires a real ticker; supply ticker=... for delisted CIKs"
        )
    eligibility = _classify_cik_eligibility_from_root(
        resolved_ticker,
        normalized_cik,
        root_payload,
        root_source.observation.observation_identity,
    )
    if eligibility.status is not SecCompanyEligibilityStatus.SEC_COMPANY:
        return _empty_result(resolved_ticker, eligibility)
    return _fetch_sec_canonical_quarters_from_root(
        canonical_ticker=resolved_ticker,
        cik=normalized_cik,
        eligibility=eligibility,
        root_source=root_source,
        root_payload=root_payload,
        client=client,
        store=store,
        utc_now=utc_now,
        bound=bound,
        count=count,
        start_year=start_year,
        end_year=end_year,
        enrich_8k_q4_eps=enrich_8k_q4_eps,
    )


__all__ = [
    "SEC_CANONICAL_CONCEPTS",
    "SEC_CANONICAL_CONCEPTS_VERSION",
    "SEC_CANONICAL_IFRS_CONCEPTS",
    "SEC_COMPANYFACTS_URL",
    "SEC_FUNDAMENTAL_CONCEPTS",
    "SEC_MULTI_TAXONOMY_CONCEPTS_VERSION",
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
    "SecFundamentalMetric",
    "fetch_sec_canonical_quarters",
    "fetch_sec_canonical_quarters_by_cik",
    "normalize_sec_cik",
    "parse_sec_companyfacts_payload",
    "project_sec_companyfacts_quarters",
]

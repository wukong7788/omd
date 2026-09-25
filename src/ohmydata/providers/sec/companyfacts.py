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
    SEC_COMPANYFACTS_URL,
    SecCanonicalFactEvidence,
    SecCanonicalFieldStatus,
    SecCanonicalMetric,
    SecCanonicalQuarterField,
    SecCanonicalQuarterlyResult,
    SecCanonicalQuarterSlot,
    SecCompanyFactsFact,
    SecCompanyFactsFiling,
    parse_sec_companyfacts_payload,
)
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
from .errors import ResourceLimitError, SchemaMismatchError, TransientProviderError
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


def fetch_sec_canonical_quarters(
    ticker: str,
    client: SecHttpClient,
    store: SnapshotStore,
    *,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    acceptance_upper: datetime | None = None,
    count: int = 8,
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
    needed, by_period = _target_fact_accessions(raw_facts, initial_periods)
    root_filings, _ = _submission_filings(store, cik, root_source)
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
    uncovered = tuple(sorted(set(uncovered) | (set(needed) - set(filings))))
    unresolved_period_accessions = _unmodeled_newer_filings(raw_facts, filings, bound)
    if unresolved_period_accessions:
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
        )
    resolved_periods = _ordered_periods(raw_facts, filings, bound)
    requested_periods = resolved_periods[-count:]
    if not set(requested_periods).issubset(initial_periods):
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
    uncovered_periods = frozenset(
        period
        for period in requested_periods
        if by_period.get(period, set()).intersection(uncovered)
    )
    slots = project_sec_companyfacts_quarters(
        raw_facts,
        filings,
        observation.observation_identity,
        acceptance_upper=bound,
        count=count,
        requested_periods=requested_periods,
        incomplete_periods=uncovered_periods,
    )
    return SecCanonicalQuarterlyResult(
        canonical_ticker,
        cik,
        eligibility,
        slots,
        observation.observation_identity,
        submission_observations,
        not uncovered,
        bool(resolved_periods),
        uncovered,
    )


def _exact_companyfacts_url(expected: str, actual: str) -> str:
    if validate_sec_url(actual) != validate_sec_url(expected):
        raise SchemaMismatchError("SEC companyfacts redirect changed source URL")
    return actual


__all__ = [
    "SEC_CANONICAL_CONCEPTS",
    "SEC_CANONICAL_CONCEPTS_VERSION",
    "SEC_COMPANYFACTS_URL",
    "SecCanonicalFactEvidence",
    "SecCanonicalFieldStatus",
    "SecCanonicalMetric",
    "SecCanonicalQuarterField",
    "SecCanonicalQuarterSlot",
    "SecCanonicalQuarterlyResult",
    "SecCompanyFactsFact",
    "SecCompanyFactsFiling",
    "fetch_sec_canonical_quarters",
    "parse_sec_companyfacts_payload",
    "project_sec_companyfacts_quarters",
]

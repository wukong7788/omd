"""Bounded submissions-root joins and targeted historical page selection."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

from ...core import RequestSpec, SnapshotMode, SnapshotStore
from ._companyfacts_models import (
    _ACCESSION,
    _ALLOWED_FORMS,
    SecCanonicalFieldStatus,
    SecCanonicalMetric,
    SecCanonicalQuarterlyResult,
    SecCompanyFactsFact,
    SecCompanyFactsFiling,
    _accepted,
    _currency_from_unit,
    _date,
)
from ._event_discovery_models import SecDiscoverySource
from .edgar import historical_basenames, historical_submission_url
from .errors import ResourceLimitError, SchemaMismatchError, TransientProviderError
from .event_discovery import _DiscoveryReplayCache, _strict_json, _submission_rows, _validate_source
from .http import SecHttpClient, validate_sec_url
from .quarterly import SecCompanyEligibilityStatus, SecQuarterlyEligibility


def _exact_companyfacts_url(expected: str, actual: str) -> str:
    if validate_sec_url(actual) != validate_sec_url(expected):
        raise SchemaMismatchError("SEC source redirect changed URL")
    return actual


def _history_page_selection(
    root_payload: dict[str, Any], cik: str, needed: Mapping[str, date], known_accessions: set[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    refs = root_payload.get("filings", {}).get("files")
    if not isinstance(refs, (list, tuple)):
        raise SchemaMismatchError("SEC submissions history references missing")
    names = set(historical_basenames(root_payload, cik))
    candidates: set[str] = set()
    uncovered: list[str] = []
    by_name: dict[str, tuple[date, date]] = {}
    for raw in refs:
        if not isinstance(raw, Mapping):
            raise SchemaMismatchError("SEC historical reference schema mismatch")
        name = raw.get("name")
        if type(name) is not str or name not in names:
            raise SchemaMismatchError("SEC historical reference name mismatch")
        start_raw, end_raw = raw.get("filingFrom"), raw.get("filingTo")
        if type(start_raw) is not str or type(end_raw) is not str:
            continue
        start, end = _date(start_raw, "filingFrom"), _date(end_raw, "filingTo")
        if start > end:
            raise SchemaMismatchError("SEC historical filing range is reversed")
        by_name[name] = (start, end)
    for accession, filed in needed.items():
        if accession in known_accessions:
            continue
        matches = [name for name, (start, end) in by_name.items() if start <= filed <= end]
        if not matches:
            uncovered.append(accession)
        candidates.update(matches)
    return tuple(sorted(candidates)), tuple(sorted(uncovered))


def _root_payload(store: SnapshotStore, source: SecDiscoverySource, cik: str) -> dict[str, Any]:
    payload, _ = _validate_source(
        store,
        source,
        cik,
        root=True,
        _replay_cache=_DiscoveryReplayCache(store),
    )
    return payload


def _fetch_history_page(
    client: SecHttpClient,
    store: SnapshotStore,
    cik: str,
    basename: str,
    utc_now: Callable[[], datetime],
) -> SecDiscoverySource:
    url = historical_submission_url(cik, basename)
    response = client.open(
        url,
        accept="application/json",
        max_bytes=8 * 1024**2,
        redirect_validator=lambda actual: _exact_companyfacts_url(url, actual),
    )
    try:
        if validate_sec_url(response.url) != validate_sec_url(url):
            raise SchemaMismatchError("SEC history redirect changed source URL")
        try:
            body = response.body.read()
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise TransientProviderError("SEC history response read failed") from exc
    finally:
        response.body.close()
    if type(body) is not bytes or len(body) > 8 * 1024**2:
        raise ResourceLimitError("SEC history response exceeds byte limit")
    child = _strict_json(body)
    _submission_rows(child, cik, child=True)
    observation = store.observe(
        RequestSpec("sec", "edgar_submissions_history", {"cik": cik, "basename": basename}),
        body,
        utc_now(),
        "sec-submissions-json-v1",
        SnapshotMode.APPEND,
    )
    return SecDiscoverySource(url, observation)


def _submission_filings(
    store: SnapshotStore,
    cik: str,
    root_source: SecDiscoverySource,
    historical_sources: tuple[SecDiscoverySource, ...] = (),
) -> tuple[dict[str, SecCompanyFactsFiling], tuple[str, ...]]:
    cache = _DiscoveryReplayCache(store)
    by_accession: dict[str, SecCompanyFactsFiling] = {}
    sources = (root_source, *historical_sources)
    for index, source in enumerate(sources):
        basename = source.url.rsplit("/", 1)[-1] if index else None
        payload, _ = _validate_source(
            store,
            source,
            cik,
            root=index == 0,
            basename=basename,
            _replay_cache=cache,
        )
        for row in _submission_rows(payload, cik, child=index != 0):
            accession = row.get("accessionNumber")
            form = row.get("form")
            report = row.get("reportDate")
            document = row.get("primaryDocument")
            if (
                type(accession) is not str
                or not _ACCESSION.fullmatch(accession)
                or type(form) is not str
                or form not in _ALLOWED_FORMS
            ):
                continue
            report_date = _date(report, "reportDate") if report else None
            accepted_at = _accepted(row.get("acceptanceDateTime"))
            if report_date is None:
                continue
            filing_date = _date(row.get("filingDate"), "filingDate")
            if document is not None and (
                type(document) is not str
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,256}", document)
                or document in {".", ".."}
            ):
                raise SchemaMismatchError("SEC primary document is not a safe basename")
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{accession.replace('-', '')}/{document}"
                if document is not None
                else None
            )
            filing = SecCompanyFactsFiling(
                accession, form, report_date, accepted_at, document, url, filing_date
            )
            old = by_accession.get(accession)
            if old is not None and old != filing:
                raise SchemaMismatchError("conflicting SEC accession metadata")
            by_accession[accession] = filing
    observations = tuple(source.observation.observation_identity for source in sources)
    return by_accession, observations


def _fetch_targeted_history_pages(
    client: SecHttpClient,
    store: SnapshotStore,
    cik: str,
    history_names: tuple[str, ...],
    utc_now: Callable[[], datetime],
) -> tuple[SecDiscoverySource, ...]:
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
    return tuple(historical_sources)


def _summarize_taxonomy_and_currency(
    facts: tuple[SecCompanyFactsFact, ...],
    filings: dict[str, SecCompanyFactsFiling],
    bound: datetime | None,
) -> tuple[str | None, str | None, tuple[SecCompanyFactsFact, ...]]:
    namespaces: set[str] = set()
    currencies: set[str] = set()
    dei_shares: list[SecCompanyFactsFact] = []
    for fact in facts:
        filing = filings.get(fact.accn)
        if filing is None or (bound is not None and filing.accepted_at > bound):
            continue
        if fact.namespace == "dei":
            if fact.tag == "EntityCommonStockSharesOutstanding" and fact.unit.lower() == "shares":
                dei_shares.append(fact)
            continue
        namespaces.add(fact.namespace)
        currency = _currency_from_unit(fact.unit)
        if currency is not None:
            currencies.add(currency)
    taxonomy = (
        None if not namespaces else next(iter(namespaces)) if len(namespaces) == 1 else "mixed"
    )
    reporting_currency = (
        None if not currencies else next(iter(currencies)) if len(currencies) == 1 else "MULTI"
    )
    dei_shares.sort(key=lambda item: (item.end, item.filed, item.accn))
    return taxonomy, reporting_currency, tuple(dei_shares)


def _classify_cik_eligibility_from_root(
    ticker: str,
    cik: str,
    root_payload: dict[str, object],
    submissions_observation_id: str,
    ticker_mapping_observation_id: str | None = None,
) -> SecQuarterlyEligibility:
    raw_entity_type = root_payload.get("entityType")
    entity_type = raw_entity_type if isinstance(raw_entity_type, str) else None
    normalized_entity = entity_type.strip().lower() if entity_type is not None else None
    rows = _submission_rows(root_payload, cik, child=False)
    accessions = tuple(
        str(item["accessionNumber"])
        for item in rows
        if isinstance(item.get("accessionNumber"), str)
    )
    forms = tuple(str(item["form"]) for item in rows if isinstance(item.get("form"), str))
    filings_obj = root_payload.get("filings")
    has_history = isinstance(filings_obj, dict) and bool(filings_obj.get("files"))
    has_supported_form = any(form in _ALLOWED_FORMS for form in forms)
    if normalized_entity == "investment":
        return SecQuarterlyEligibility(
            SecCompanyEligibilityStatus.NON_COMPANY,
            ticker,
            cik,
            entity_type,
            ticker_mapping_observation_id,
            submissions_observation_id,
            (),
            "SEC entityType is investment",
        )
    if has_supported_form or has_history:
        return SecQuarterlyEligibility(
            SecCompanyEligibilityStatus.SEC_COMPANY,
            ticker,
            cik,
            entity_type,
            ticker_mapping_observation_id,
            submissions_observation_id,
            accessions,
            "SEC CIK identity and periodic filing evidence match",
        )
    return SecQuarterlyEligibility(
        SecCompanyEligibilityStatus.UNKNOWN,
        ticker,
        cik,
        entity_type,
        ticker_mapping_observation_id,
        submissions_observation_id,
        (),
        "SEC evidence is missing, unsupported, or inconsistent",
    )


def _fetch_companyfacts_body(client: SecHttpClient, url: str, max_bytes: int) -> bytes:
    response = client.open(
        url,
        accept="application/json",
        max_bytes=max_bytes,
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
    if type(body) is not bytes or len(body) > max_bytes:
        raise ResourceLimitError("SEC companyfacts response exceeds byte limit")
    return body


def _enrich_result_with_8k_q4_eps(
    result: SecCanonicalQuarterlyResult,
    canonical_ticker: str,
    client: SecHttpClient,
    store: SnapshotStore,
    bound: datetime | None,
    utc_now: Callable[[], datetime],
) -> SecCanonicalQuarterlyResult:
    from .earnings_8k import enrich_sec_canonical_quarters_with_8k_eps, fetch_sec_8k_quarterly_eps

    missing_q4s = [
        (slot.fiscal_year, 4, rev_field.evidence[0].period_end)
        for slot in result.slots
        if slot.fiscal_quarter == 4
        and slot.field(SecCanonicalMetric.GAAP_DILUTED_EPS).status
        is SecCanonicalFieldStatus.MISSING
        and (rev_field := slot.field(SecCanonicalMetric.REVENUE)).evidence
    ]
    if not missing_q4s:
        return result
    for idx in range(0, len(missing_q4s), 8):
        chunk = tuple(missing_q4s[idx : idx + 8])
        eps_result = fetch_sec_8k_quarterly_eps(
            canonical_ticker,
            client,
            store,
            target_quarters=chunk,
            acceptance_upper=bound,
            utc_now=utc_now,
        )
        result = enrich_sec_canonical_quarters_with_8k_eps(result, eps_result)
    return result


__all__ = [
    "_classify_cik_eligibility_from_root",
    "_enrich_result_with_8k_q4_eps",
    "_exact_companyfacts_url",
    "_fetch_companyfacts_body",
    "_fetch_history_page",
    "_fetch_targeted_history_pages",
    "_history_page_selection",
    "_root_payload",
    "_submission_filings",
    "_summarize_taxonomy_and_currency",
]

"""End-to-end incremental SEC financials sync engine with cursors, rate limiting, and receipts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ...core.snapshot import SnapshotStore
from ._event_discovery_models import (
    _FORMS as _DISCOVERY_POLICY_FORMS,
)
from ._event_discovery_models import (
    SecDiscoveryCursor,
    SecDiscoveryMode,
    SecDiscoveryPolicy,
    SecDiscoverySource,
    SecFilingEventKind,
    SecRootCoverageStatus,
)
from ._financials_sync_batch import (
    SecIncrementalBatchWriter,
    default_write_financials_incremental_batch,
    resolve_default_batch_hooks,
    validate_batch_id,
    validate_default_financials_incremental_batch,
)
from ._financials_sync_discovery import (
    SecFilingFetcher,
    compute_run_id,
    discover_extra_form_events,
    ensure_aware_utc,
    filter_new_events,
    make_edgartools_filing_fetcher,
    normalize_forms,
    normalize_universe,
    validate_fetched_vintages,
)
from ._financials_sync_models import (
    DEFAULT_DISCOVERY_POLICY_VERSION,
    DEFAULT_SYNC_FORMS,
    SEC_DISCOVERY_CURSOR_SCHEMA,
    SEC_FINANCIALS_SYNC_RECEIPT_SCHEMA,
    SUPPORTED_SYNC_FORMS,
    SecCursorRecord,
    SecFinancialsSyncReceipt,
    SecSyncCikReceipt,
    SecSyncOutcomeStatus,
    sanitize_error_message,
    utc_iso,
)
from ._financials_sync_state import (
    SecCursorStore,
    SecRateLimiter,
    load_cik_universe,
    normalize_cik,
    write_immutable_json_exclusive,
)
from ._statement_parser import SecStatementParseError, SecUnitEvidenceError
from .edgartools_adapter import SecFinancialsClient
from .errors import (
    CoverageError,
    PermanentProviderError,
    ResourceLimitError,
    SchemaMismatchError,
    SnapshotIntegrityError,
    TransientProviderError,
)
from .http import SecHttpClient
from .live_discovery import fetch_sec_discovery_batch, fetch_sec_incremental_discovery

_DEFAULT_INITIAL_LOWER = datetime(1993, 1, 1, tzinfo=UTC)

_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    TransientProviderError,
    TimeoutError,
    ConnectionError,
)
_PERMANENT_ERRORS: tuple[type[Exception], ...] = (
    PermanentProviderError,
    CoverageError,
    ResourceLimitError,
    SnapshotIntegrityError,
    SecStatementParseError,
    SecUnitEvidenceError,
    ValueError,
    OSError,
)
_CLASSIFIED_CIK_ERRORS: tuple[type[Exception], ...] = _TRANSIENT_ERRORS + _PERMANENT_ERRORS


def _default_utc_now() -> datetime:
    return datetime.now(UTC)


def sync_sec_financials_incremental(
    root: str | Path,
    ciks: Sequence[str],
    http_client: SecHttpClient,
    snapshot_store: SnapshotStore,
    filing_fetcher: SecFilingFetcher | None = None,
    *,
    financials_client: SecFinancialsClient | None = None,
    forms: Sequence[str] = DEFAULT_SYNC_FORMS,
    as_of: datetime | None = None,
    overlap: timedelta = timedelta(hours=24),
    rate_limiter: SecRateLimiter | None = None,
    utc_now: Callable[[], datetime] = _default_utc_now,
    batch_writer: SecIncrementalBatchWriter | None = None,
    batch_validator: Callable[[str | Path], Mapping[str, Any]] | None = None,
    cursor_store: SecCursorStore | None = None,
    initial_acceptance_lower: datetime | None = None,
    policy_version: str = DEFAULT_DISCOVERY_POLICY_VERSION,
    run_id: str | None = None,
) -> SecFinancialsSyncReceipt:
    """Run an end-to-end incremental SEC financials sync over a CIK universe."""
    if not isinstance(http_client, SecHttpClient):
        raise TypeError("http_client must be a SecHttpClient")
    if not isinstance(snapshot_store, SnapshotStore):
        raise TypeError("snapshot_store must be a SnapshotStore")
    if not callable(utc_now):
        raise TypeError("utc_now must be callable")
    if type(overlap) is not timedelta or overlap < timedelta(0):
        raise ValueError("overlap must be a non-negative timedelta")
    if rate_limiter is not None and not isinstance(rate_limiter, SecRateLimiter):
        raise TypeError("rate_limiter must be a SecRateLimiter or None")
    if filing_fetcher is None:
        if financials_client is None:
            raise ValueError("either filing_fetcher or financials_client must be provided")
        resolved_fetcher = make_edgartools_filing_fetcher(financials_client)
    else:
        resolved_fetcher = filing_fetcher

    root_path = Path(root)
    normalized_ciks = normalize_universe(ciks)
    normalized_forms = normalize_forms(forms)
    requested_forms_set = frozenset(normalized_forms)
    supported_policy_forms = tuple(f for f in normalized_forms if f in _DISCOVERY_POLICY_FORMS)
    extra_forms = frozenset(f for f in normalized_forms if f not in _DISCOVERY_POLICY_FORMS)
    policy_forms = supported_policy_forms if supported_policy_forms else ("10-K",)

    started_at = ensure_aware_utc(utc_now(), "utc_now()")
    effective_as_of = started_at if as_of is None else ensure_aware_utc(as_of, "as_of")
    effective_initial_lower = (
        _DEFAULT_INITIAL_LOWER
        if initial_acceptance_lower is None
        else ensure_aware_utc(initial_acceptance_lower, "initial_acceptance_lower")
    )
    if effective_initial_lower > effective_as_of:
        raise ValueError("initial_acceptance_lower exceeds as_of")

    store = cursor_store if cursor_store is not None else SecCursorStore(root_path)
    default_writer, default_validator = resolve_default_batch_hooks()
    active_writer = batch_writer if batch_writer is not None else default_writer
    active_validator: Callable[[str | Path], Mapping[str, Any]] | None
    if batch_validator is not None:
        active_validator = batch_validator
    elif batch_writer is None:
        active_validator = default_validator
    else:
        active_validator = None

    receipts_dir = root_path / "receipts"
    effective_run_id = compute_run_id(
        receipts_dir,
        requested_run_id=run_id,
        started_at=started_at,
        ciks=normalized_ciks,
        forms=normalized_forms,
        as_of=effective_as_of,
    )

    sensitive_values: tuple[str, ...] = tuple(
        val
        for val in (
            getattr(http_client, "_ua", None),
            getattr(financials_client, "user_agent", None) if financials_client else None,
        )
        if isinstance(val, str) and val
    )
    http_shares_limiter = (
        rate_limiter is not None
        and type(http_client) is SecHttpClient
        and getattr(http_client, "_limiter", None) is rate_limiter
    )

    outcomes: list[SecSyncCikReceipt] = []
    reconciled_ciks: list[str] = []
    failed_ciks: list[str] = []
    transient_failed_ciks: list[str] = []
    permanent_failed_ciks: list[str] = []
    batch_ids: list[str] = []

    for cik in normalized_ciks:
        prior_record: SecCursorRecord | None = None
        prior_cursor: SecDiscoveryCursor | None = None
        reconciled = False
        discovery_mode = SecDiscoveryMode.RECONCILE
        discovered_count = 0
        new_count = 0

        try:
            prior_record = store.load_record(cik)
            prior_cursor = None if prior_record is None else prior_record.cursor
            canonical_cik = str(int(cik))

            if prior_cursor is not None:
                if effective_as_of < prior_cursor.acceptance_at:
                    raise ValueError(f"as_of precedes prior cursor acceptance_at for CIK {cik}")
                acceptance_lower = prior_cursor.acceptance_at - overlap
                inc_policy = SecDiscoveryPolicy(
                    canonical_cik,
                    policy_forms,
                    acceptance_lower,
                    effective_as_of,
                    overlap,
                    SecDiscoveryMode.INCREMENTAL,
                    policy_version,
                )
                if rate_limiter is not None and not http_shares_limiter:
                    rate_limiter.acquire()
                root_result = fetch_sec_incremental_discovery(
                    http_client,
                    snapshot_store,
                    policy=inc_policy,
                    prior_cursor=prior_cursor,
                    clock=utc_now,
                )
                if root_result.status is SecRootCoverageStatus.COMPLETE:
                    assert root_result.window is not None
                    discovery_mode = SecDiscoveryMode.INCREMENTAL
                    reconciled = False
                    sources: tuple[SecDiscoverySource, ...] = (root_result.root_source,)
                    base_events = root_result.window.events
                else:
                    reconcile_policy = SecDiscoveryPolicy(
                        canonical_cik,
                        policy_forms,
                        acceptance_lower,
                        effective_as_of,
                        overlap,
                        SecDiscoveryMode.RECONCILE,
                        policy_version,
                    )
                    if rate_limiter is not None and not http_shares_limiter:
                        rate_limiter.acquire()
                    batch = fetch_sec_discovery_batch(
                        http_client,
                        snapshot_store,
                        policy=reconcile_policy,
                        prior_cursor=prior_cursor,
                        clock=utc_now,
                    )
                    discovery_mode = SecDiscoveryMode.RECONCILE
                    reconciled = True
                    sources = batch.sources
                    base_events = batch.events
            else:
                acceptance_lower = effective_initial_lower
                init_policy = SecDiscoveryPolicy(
                    canonical_cik,
                    policy_forms,
                    acceptance_lower,
                    effective_as_of,
                    overlap,
                    SecDiscoveryMode.RECONCILE,
                    policy_version,
                )
                if rate_limiter is not None and not http_shares_limiter:
                    rate_limiter.acquire()
                batch = fetch_sec_discovery_batch(
                    http_client,
                    snapshot_store,
                    policy=init_policy,
                    prior_cursor=None,
                    clock=utc_now,
                )
                discovery_mode = SecDiscoveryMode.RECONCILE
                reconciled = True
                sources = batch.sources
                base_events = batch.events

            matching_base = [
                e
                for e in base_events
                if e.form in requested_forms_set
                and e.kind is SecFilingEventKind.SEC_FILING_ACCEPTED
            ]
            extra_events = discover_extra_form_events(
                snapshot_store,
                sources,
                canonical_cik=canonical_cik,
                extra_forms=extra_forms,
                acceptance_lower=acceptance_lower,
                acceptance_upper=effective_as_of,
            )
            all_events = tuple(
                sorted(
                    (*matching_base, *extra_events),
                    key=lambda e: (e.acceptance_at, e.accession, e.key),
                )
            )
            discovered_count = len(all_events)

            cursor_candidates = [
                SecDiscoveryCursor(e.acceptance_at, e.accession) for e in all_events
            ]
            if prior_cursor is not None:
                cursor_candidates.append(prior_cursor)
            candidate_cursor = max(
                cursor_candidates,
                key=lambda item: (item.acceptance_at, item.accession),
                default=None,
            )

            new_events = filter_new_events(all_events, prior_record)
            new_count = len(new_events)

            if new_count == 0:
                saved_cursor = prior_cursor
                if candidate_cursor is not None and (
                    prior_cursor is None
                    or (candidate_cursor.acceptance_at, candidate_cursor.accession)
                    > (prior_cursor.acceptance_at, prior_cursor.accession)
                ):
                    preserved_processed = (
                        prior_record.processed_accessions if prior_record is not None else None
                    )
                    preserved_batch = (
                        prior_record.last_batch_id if prior_record is not None else None
                    )
                    store.save(
                        cik,
                        candidate_cursor,
                        updated_at=ensure_aware_utc(utc_now(), "utc_now()"),
                        last_batch_id=preserved_batch,
                        processed_accessions=preserved_processed,
                    )
                    saved_cursor = candidate_cursor

                if reconciled:
                    reconciled_ciks.append(cik)
                outcomes.append(
                    SecSyncCikReceipt(
                        cik=cik,
                        status=SecSyncOutcomeStatus.EMPTY,
                        reconciled=reconciled,
                        discovery_mode=discovery_mode.value,
                        discovered_event_count=discovered_count,
                        new_event_count=0,
                        vintage_count=0,
                        batch_id=None,
                        prior_cursor=prior_cursor,
                        new_cursor=saved_cursor,
                    )
                )
                continue

            if rate_limiter is not None:
                rate_limiter.acquire()
            raw_vintages = resolved_fetcher(cik, new_events)
            vintages = validate_fetched_vintages(cik, new_events, raw_vintages)

            cik_batch_id = validate_batch_id(f"{effective_run_id}-cik-{cik}")
            batch_metadata: dict[str, Any] = {
                "run_id": effective_run_id,
                "cik": cik,
                "discovery_mode": discovery_mode.value,
                "reconciled": reconciled,
                "forms": list(normalized_forms),
                "as_of": utc_iso(effective_as_of),
                "discovered_accessions": sorted({e.accession for e in new_events}),
            }
            manifest = active_writer(root_path, cik_batch_id, vintages, metadata=batch_metadata)
            if not isinstance(manifest, Mapping):
                raise SchemaMismatchError("batch writer did not return a manifest mapping")
            if active_validator is not None:
                active_validator(root_path / "batches" / cik_batch_id)

            assert candidate_cursor is not None
            prior_processed: set[str] = set()
            if prior_record is not None:
                if prior_record.processed_accessions is not None:
                    prior_processed.update(prior_record.processed_accessions)
                else:
                    prior_processed.add(prior_record.cursor.accession)
            next_processed = tuple(sorted(prior_processed | {e.accession for e in new_events}))

            store.save(
                cik,
                candidate_cursor,
                updated_at=ensure_aware_utc(utc_now(), "utc_now()"),
                last_batch_id=cik_batch_id,
                processed_accessions=next_processed,
            )

            batch_ids.append(cik_batch_id)
            if reconciled:
                reconciled_ciks.append(cik)
            outcomes.append(
                SecSyncCikReceipt(
                    cik=cik,
                    status=SecSyncOutcomeStatus.SYNCED,
                    reconciled=reconciled,
                    discovery_mode=discovery_mode.value,
                    discovered_event_count=discovered_count,
                    new_event_count=new_count,
                    vintage_count=len(vintages),
                    batch_id=cik_batch_id,
                    prior_cursor=prior_cursor,
                    new_cursor=candidate_cursor,
                )
            )
        except _CLASSIFIED_CIK_ERRORS as exc:
            status = (
                SecSyncOutcomeStatus.TRANSIENT_FAILURE
                if isinstance(exc, _TRANSIENT_ERRORS)
                else SecSyncOutcomeStatus.PERMANENT_FAILURE
            )
            failed_ciks.append(cik)
            if status is SecSyncOutcomeStatus.TRANSIENT_FAILURE:
                transient_failed_ciks.append(cik)
            else:
                permanent_failed_ciks.append(cik)
            if reconciled:
                reconciled_ciks.append(cik)
            outcomes.append(
                SecSyncCikReceipt(
                    cik=cik,
                    status=status,
                    reconciled=reconciled,
                    discovery_mode=discovery_mode.value,
                    discovered_event_count=discovered_count,
                    new_event_count=new_count,
                    vintage_count=0,
                    batch_id=None,
                    prior_cursor=prior_cursor,
                    new_cursor=prior_cursor,
                    error_type=type(exc).__name__,
                    error_message=sanitize_error_message(exc, sensitive_values=sensitive_values),
                )
            )

    completed_at = ensure_aware_utc(utc_now(), "utc_now()")
    receipt_path = receipts_dir / f"sync-{effective_run_id}.json"
    receipt = SecFinancialsSyncReceipt(
        schema_version=SEC_FINANCIALS_SYNC_RECEIPT_SCHEMA,
        run_id=effective_run_id,
        batch_id=effective_run_id,
        started_at=started_at,
        completed_at=completed_at,
        as_of=effective_as_of,
        forms=normalized_forms,
        overlap_seconds=overlap.total_seconds(),
        ciks_requested=normalized_ciks,
        synced=sum(1 for o in outcomes if o.status is SecSyncOutcomeStatus.SYNCED),
        empty=sum(1 for o in outcomes if o.status is SecSyncOutcomeStatus.EMPTY),
        transient_failure=len(transient_failed_ciks),
        permanent_failure=len(permanent_failed_ciks),
        reconciled_ciks=tuple(reconciled_ciks),
        failed_ciks=tuple(failed_ciks),
        transient_failed_ciks=tuple(transient_failed_ciks),
        permanent_failed_ciks=tuple(permanent_failed_ciks),
        batch_ids=tuple(batch_ids),
        outcomes=tuple(outcomes),
        receipt_path=receipt_path,
    )
    write_immutable_json_exclusive(receipt_path, receipt.to_dict())
    return receipt


__all__ = [
    "DEFAULT_DISCOVERY_POLICY_VERSION",
    "DEFAULT_SYNC_FORMS",
    "SEC_DISCOVERY_CURSOR_SCHEMA",
    "SEC_FINANCIALS_SYNC_RECEIPT_SCHEMA",
    "SUPPORTED_SYNC_FORMS",
    "SecCursorRecord",
    "SecCursorStore",
    "SecFilingFetcher",
    "SecFinancialsSyncReceipt",
    "SecIncrementalBatchWriter",
    "SecRateLimiter",
    "SecSyncCikReceipt",
    "SecSyncOutcomeStatus",
    "default_write_financials_incremental_batch",
    "load_cik_universe",
    "make_edgartools_filing_fetcher",
    "normalize_cik",
    "sync_sec_financials_incremental",
    "validate_default_financials_incremental_batch",
]

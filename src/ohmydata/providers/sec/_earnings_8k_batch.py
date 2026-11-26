"""Batch runner and discovery-event stream integration for Form 8-K quarterly metrics."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum

from ...core import RequestSpec, SnapshotMode, SnapshotStore
from ._companyfacts_models import Sec8KReleaseEvidence
from ._earnings_8k_discovery import (
    _EXHIBIT_BYTES,
    _INDEX_BYTES,
    _RELEASE_SEARCH_DAYS,
    _exhibit_basenames,
    _fetch_html,
    _Filing8K,
    _normalize_8k_cik,
)
from ._earnings_8k_metrics import (
    _DEFAULT_METRICS,
    Sec8KMetric,
    Sec8KMetricStatus,
    Sec8KQuarterlyMetricsItem,
    Sec8KQuarterlyMetricsResult,
    _make_evidence,
    _resolve_field,
    _validate_bound,
    _validate_metrics,
    fetch_sec_8k_quarterly_metrics,
)
from ._earnings_8k_parse_metrics import Sec8KMetricCandidate, parse_sec_8k_earnings_metrics
from ._event_discovery_models import SecFilingDiscoveryEvent, SecFilingEventKind
from .errors import (
    AuthenticationError,
    CoverageError,
    PermanentProviderError,
    PermissionDeniedError,
    RateLimitError,
    ResourceLimitError,
    SchemaMismatchError,
    SnapshotIntegrityError,
    TransientProviderError,
)
from .http import SecHttpClient

_SECRET_HEADER = re.compile(
    r"(?:user-agent|authorization|bearer|token|password|secret|api[_-]?key)\s*[:=][^,;\r\n]+",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TRANSIENT_EXC = (TransientProviderError, RateLimitError, TimeoutError, ConnectionError)
_PERMANENT_EXC = (
    PermanentProviderError,
    AuthenticationError,
    PermissionDeniedError,
    CoverageError,
    SchemaMismatchError,
    ResourceLimitError,
    SnapshotIntegrityError,
    OSError,
    ValueError,
    TypeError,
    KeyError,
)
_ISOLATED_EXC = (*_TRANSIENT_EXC, *_PERMANENT_EXC)


class Sec8KBatchOutcomeStatus(str, Enum):
    """Typed per-target outcome status for Form 8-K batch and event-stream runs."""

    OK = "OK"
    EMPTY = "EMPTY"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


@dataclass(frozen=True)
class Sec8KBatchTarget:
    """One target issuer and quarter list for batched Form 8-K metrics extraction."""

    target_quarters: tuple[tuple[int, int, date], ...]
    cik: str | None = None
    ticker: str | None = None
    metrics: tuple[Sec8KMetric, ...] | None = None
    acceptance_upper: datetime | None = None

    def __post_init__(self) -> None:
        norm_cik = _normalize_8k_cik(self.cik) if self.cik is not None else None
        norm_ticker: str | None = None
        if self.ticker is not None:
            if type(self.ticker) is not str or not self.ticker.strip():
                raise ValueError("ticker must be a non-empty string when provided")
            norm_ticker = self.ticker.strip().upper()
        if norm_cik is None and norm_ticker is None:
            raise ValueError("Sec8KBatchTarget requires at least one of cik or ticker")
        object.__setattr__(self, "cik", norm_cik)
        object.__setattr__(self, "ticker", norm_ticker)


@dataclass(frozen=True)
class Sec8KBatchItemReceipt:
    """Isolated per-target or per-event receipt for a Form 8-K batch run."""

    target_id: str
    cik: str | None
    ticker: str | None
    status: Sec8KBatchOutcomeStatus
    result: Sec8KQuarterlyMetricsResult | None = None
    error_type: str | None = None
    error_message: str | None = None
    event_key: str | None = None
    accession_number: str | None = None


@dataclass(frozen=True)
class Sec8KBatchResult:
    """Aggregated summary and isolated per-target receipts for an 8-K batch run."""

    receipts: tuple[Sec8KBatchItemReceipt, ...]
    ok_count: int
    empty_count: int
    transient_failure_count: int
    permanent_failure_count: int


def _sanitize_error_message(exc: Exception, *, client: SecHttpClient | None = None) -> str:
    raw = str(exc)
    if client is not None:
        ua = getattr(client, "_ua", None)
        if type(ua) is str and ua:
            raw = raw.replace(ua, "[REDACTED]")
            norm_ua = " ".join(ua.split())
            if norm_ua:
                raw = raw.replace(norm_ua, "[REDACTED]")
    redacted = _SECRET_HEADER.sub("[REDACTED]", raw)
    redacted = _EMAIL.sub("[REDACTED]", redacted)
    return " ".join(redacted.split())[:240]


def _classify_batch_exception(exc: Exception) -> Sec8KBatchOutcomeStatus:
    if isinstance(exc, _TRANSIENT_EXC):
        return Sec8KBatchOutcomeStatus.TRANSIENT_FAILURE
    return Sec8KBatchOutcomeStatus.PERMANENT_FAILURE


def _result_outcome_status(result: Sec8KQuarterlyMetricsResult) -> Sec8KBatchOutcomeStatus:
    has_data = any(
        field.status is not Sec8KMetricStatus.MISSING
        for item in result.items
        for field in item.fields
    )
    return Sec8KBatchOutcomeStatus.OK if has_data else Sec8KBatchOutcomeStatus.EMPTY


def _summarize_batch(receipts: list[Sec8KBatchItemReceipt]) -> Sec8KBatchResult:
    return Sec8KBatchResult(
        receipts=tuple(receipts),
        ok_count=sum(1 for r in receipts if r.status is Sec8KBatchOutcomeStatus.OK),
        empty_count=sum(1 for r in receipts if r.status is Sec8KBatchOutcomeStatus.EMPTY),
        transient_failure_count=sum(
            1 for r in receipts if r.status is Sec8KBatchOutcomeStatus.TRANSIENT_FAILURE
        ),
        permanent_failure_count=sum(
            1 for r in receipts if r.status is Sec8KBatchOutcomeStatus.PERMANENT_FAILURE
        ),
    )


def fetch_sec_8k_quarterly_batch(
    targets: Sequence[Sec8KBatchTarget],
    client: SecHttpClient,
    store: SnapshotStore,
    *,
    metrics: tuple[Sec8KMetric, ...] = _DEFAULT_METRICS,
    acceptance_upper: datetime | None = None,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Sec8KBatchResult:
    """Execute direct 8-K quarterly metrics extraction across targets with failure isolation."""
    if not isinstance(client, SecHttpClient) or not isinstance(store, SnapshotStore):
        raise TypeError("client and store must be SEC HTTP and snapshot clients")
    if not callable(utc_now):
        raise TypeError("utc_now must be callable")
    default_metrics = _validate_metrics(metrics)
    default_bound = _validate_bound(acceptance_upper)

    receipts: list[Sec8KBatchItemReceipt] = []
    for target in targets:
        if not isinstance(target, Sec8KBatchTarget):
            raise TypeError("targets must contain only Sec8KBatchTarget instances")
        target_id = target.cik or target.ticker or "unknown"
        try:
            res = fetch_sec_8k_quarterly_metrics(
                client,
                store,
                cik=target.cik,
                ticker=target.ticker,
                target_quarters=target.target_quarters,
                metrics=target.metrics or default_metrics,
                acceptance_upper=(
                    target.acceptance_upper
                    if target.acceptance_upper is not None
                    else default_bound
                ),
                utc_now=utc_now,
            )
            receipts.append(
                Sec8KBatchItemReceipt(
                    target_id=target_id,
                    cik=res.cik,
                    ticker=res.ticker,
                    status=_result_outcome_status(res),
                    result=res,
                )
            )
        except _ISOLATED_EXC as exc:
            receipts.append(
                Sec8KBatchItemReceipt(
                    target_id=target_id,
                    cik=target.cik,
                    ticker=target.ticker,
                    status=_classify_batch_exception(exc),
                    error_type=type(exc).__name__,
                    error_message=_sanitize_error_message(exc, client=client),
                )
            )
    return _summarize_batch(receipts)


def _event_has_item_2_02(event: SecFilingDiscoveryEvent) -> bool:
    if event.kind is SecFilingEventKind.SEC_8K_ITEM_2_02:
        return True
    if not event.items:
        return False
    return "2.02" in [part.strip() for part in event.items.split(",")]


def _fetch_single_event_metrics(
    event: SecFilingDiscoveryEvent,
    client: SecHttpClient,
    store: SnapshotStore,
    *,
    metrics: tuple[Sec8KMetric, ...],
    utc_now: Callable[[], datetime],
) -> Sec8KQuarterlyMetricsResult:
    normalized_cik = _normalize_8k_cik(event.cik)
    filing_date = date.fromisoformat(event.filing_date)
    report_date = date.fromisoformat(event.report_date) if event.report_date else filing_date
    filing = _Filing8K(
        accession=event.accession,
        form=event.form,
        filing_date=filing_date,
        report_date=report_date,
        accepted_at=event.acceptance_at.astimezone(UTC),
    )

    compact = filing.accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(normalized_cik)}/{compact}"
    index_url = f"{base}/{filing.accession}-index.htm"
    index_body = _fetch_html(client, index_url, _INDEX_BYTES)
    index_obs = store.observe(
        RequestSpec(
            "sec", "edgar_8k_index", {"cik": normalized_cik, "accession": filing.accession}
        ),
        index_body,
        utc_now(),
        "sec-8k-index-html-v1",
        SnapshotMode.APPEND,
    )
    doc_names = _exhibit_basenames(index_body, normalized_cik, filing.accession)

    cands_by_key: dict[
        tuple[Sec8KMetric, date], list[tuple[Sec8KMetricCandidate, Sec8KReleaseEvidence]]
    ] = {}
    ambigs_by_key: dict[tuple[Sec8KMetric, date], list[tuple[str, list[Sec8KReleaseEvidence]]]] = {}
    explicit_quarters: dict[date, int] = {}

    for doc_name in doc_names:
        exhibit_url = f"{base}/{doc_name}"
        exhibit_body = _fetch_html(client, exhibit_url, _EXHIBIT_BYTES)
        exhibit_obs = store.observe(
            RequestSpec(
                "sec",
                "edgar_8k_exhibit",
                {"cik": normalized_cik, "accession": filing.accession, "document": doc_name},
            ),
            exhibit_body,
            utc_now(),
            "sec-8k-exhibit-html-v1",
            SnapshotMode.APPEND,
        )
        parsed = parse_sec_8k_earnings_metrics(exhibit_body, metrics=metrics)
        for (metric, pend), amb in parsed.ambiguities.items():
            if not pend <= filing_date <= pend + timedelta(days=_RELEASE_SEARCH_DAYS):
                continue
            evs = [
                _make_evidence(
                    c,
                    pend.year,
                    f"Q{c.explicit_quarter}" if c.explicit_quarter is not None else "Q",
                    filing,
                    index_url,
                    exhibit_url,
                    index_obs.observation_identity,
                    exhibit_obs.observation_identity,
                )
                for c in amb.candidates
            ]
            ambigs_by_key.setdefault((metric, pend), []).append((amb.reason, evs))
        for (metric, pend), cand in parsed.values.items():
            if not pend <= filing_date <= pend + timedelta(days=_RELEASE_SEARCH_DAYS):
                continue
            if cand.explicit_quarter is not None:
                explicit_quarters[pend] = cand.explicit_quarter
            ev = _make_evidence(
                cand,
                pend.year,
                f"Q{cand.explicit_quarter}" if cand.explicit_quarter is not None else "Q",
                filing,
                index_url,
                exhibit_url,
                index_obs.observation_identity,
                exhibit_obs.observation_identity,
            )
            cands_by_key.setdefault((metric, pend), []).append((cand, ev))

    discovered_periods = sorted(
        {pend for _, pend in cands_by_key} | {pend for _, pend in ambigs_by_key}
    )
    items: list[Sec8KQuarterlyMetricsItem] = []
    for pend in discovered_periods:
        fields = tuple(
            _resolve_field(
                metric,
                cands_by_key.get((metric, pend), []),
                ambigs_by_key.get((metric, pend), []),
            )
            for metric in metrics
        )
        items.append(
            Sec8KQuarterlyMetricsItem(
                fiscal_year=None,
                fiscal_quarter=explicit_quarters.get(pend),
                period_end=pend,
                fields=fields,
            )
        )

    return Sec8KQuarterlyMetricsResult(
        cik=normalized_cik,
        ticker=None,
        metrics=metrics,
        items=tuple(items),
        mapping_observation_id=None,
        submissions_observation_ids=event.source_observation_identities,
        coverage_complete=True,
    )


def _deduplicate_8k_events(
    events: Sequence[SecFilingDiscoveryEvent],
    bound: datetime | None,
) -> list[SecFilingDiscoveryEvent]:
    """Filter to in-window 8-K/8-K/A events and deduplicate paired accession events.

    ``event_discovery.py`` emits both ``SEC_FILING_ACCEPTED`` and
    ``SEC_8K_ITEM_2_02`` for the same ``(cik, accession)`` when ``items``
    includes ``2.02``. Deduplicate by ``(norm_cik, accession)``, preferring
    ``SEC_8K_ITEM_2_02`` while preserving deterministic first-seen order.
    """
    ordered: list[SecFilingDiscoveryEvent] = []
    index_by_key: dict[tuple[str, str], int] = {}
    for event in events:
        if not isinstance(event, SecFilingDiscoveryEvent):
            raise TypeError("events must contain only SecFilingDiscoveryEvent instances")
        if event.form not in {"8-K", "8-K/A"}:
            continue
        if bound is not None and event.acceptance_at.astimezone(UTC) > bound:
            continue
        try:
            dedup_key = (_normalize_8k_cik(event.cik), event.accession)
        except (TypeError, ValueError):
            ordered.append(event)
            continue
        existing_idx = index_by_key.get(dedup_key)
        if existing_idx is None:
            index_by_key[dedup_key] = len(ordered)
            ordered.append(event)
        elif (
            event.kind is SecFilingEventKind.SEC_8K_ITEM_2_02
            and ordered[existing_idx].kind is not SecFilingEventKind.SEC_8K_ITEM_2_02
        ):
            ordered[existing_idx] = event
    return ordered


def fetch_sec_8k_metrics_for_discovery_events(
    events: Sequence[SecFilingDiscoveryEvent],
    client: SecHttpClient,
    store: SnapshotStore,
    *,
    metrics: tuple[Sec8KMetric, ...] = _DEFAULT_METRICS,
    acceptance_upper: datetime | None = None,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Sec8KBatchResult:
    """Process a stream of ``SecFilingDiscoveryEvent`` objects for Form 8-K / 8-K/A metrics.

    Non-8-K events and events accepted after ``acceptance_upper`` are skipped.
    Paired ``SEC_FILING_ACCEPTED`` + ``SEC_8K_ITEM_2_02`` events for the same
    ``(cik, accession)`` are deduplicated (preferring ``SEC_8K_ITEM_2_02``).
    Per-event errors are isolated into typed receipts without aborting the stream.
    """
    if not isinstance(client, SecHttpClient) or not isinstance(store, SnapshotStore):
        raise TypeError("client and store must be SEC HTTP and snapshot clients")
    if not callable(utc_now):
        raise TypeError("utc_now must be callable")
    validated_metrics = _validate_metrics(metrics)
    bound = _validate_bound(acceptance_upper)

    deduped_events = _deduplicate_8k_events(events, bound)
    receipts: list[Sec8KBatchItemReceipt] = []
    for event in deduped_events:
        norm_cik: str | None = None
        try:
            norm_cik = _normalize_8k_cik(event.cik)
            target_id = f"{norm_cik}:{event.accession}"
            if not _event_has_item_2_02(event):
                empty_result = Sec8KQuarterlyMetricsResult(
                    cik=norm_cik,
                    ticker=None,
                    metrics=validated_metrics,
                    items=(),
                    mapping_observation_id=None,
                    submissions_observation_ids=event.source_observation_identities,
                    coverage_complete=True,
                )
                receipts.append(
                    Sec8KBatchItemReceipt(
                        target_id=target_id,
                        cik=norm_cik,
                        ticker=None,
                        status=Sec8KBatchOutcomeStatus.EMPTY,
                        result=empty_result,
                        event_key=event.key,
                        accession_number=event.accession,
                    )
                )
                continue
            res = _fetch_single_event_metrics(
                event, client, store, metrics=validated_metrics, utc_now=utc_now
            )
            receipts.append(
                Sec8KBatchItemReceipt(
                    target_id=target_id,
                    cik=norm_cik,
                    ticker=None,
                    status=_result_outcome_status(res),
                    result=res,
                    event_key=event.key,
                    accession_number=event.accession,
                )
            )
        except _ISOLATED_EXC as exc:
            receipts.append(
                Sec8KBatchItemReceipt(
                    target_id=f"{norm_cik or event.cik}:{event.accession}",
                    cik=norm_cik,
                    ticker=None,
                    status=_classify_batch_exception(exc),
                    error_type=type(exc).__name__,
                    error_message=_sanitize_error_message(exc, client=client),
                    event_key=event.key,
                    accession_number=event.accession,
                )
            )
    return _summarize_batch(receipts)


__all__ = [
    "Sec8KBatchItemReceipt",
    "Sec8KBatchOutcomeStatus",
    "Sec8KBatchResult",
    "Sec8KBatchTarget",
    "fetch_sec_8k_metrics_for_discovery_events",
    "fetch_sec_8k_quarterly_batch",
]

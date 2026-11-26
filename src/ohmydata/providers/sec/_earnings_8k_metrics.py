"""Multi-metric Form 8-K extraction (Revenue, Gross Profit, GAAP Diluted EPS)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum

from ...core import RequestSpec, SnapshotMode, SnapshotStore
from ._companyfacts_models import Sec8KReleaseEvidence
from ._companyfacts_submissions import _fetch_history_page, _root_payload
from ._earnings_8k_discovery import (
    _EXHIBIT_BYTES,
    _INDEX_BYTES,
    _MAX_HISTORY_BYTES,
    _RELEASE_SEARCH_DAYS,
    _discover_8k_filings,
    _exhibit_basenames,
    _fetch_html,
    _Filing8K,
    _normalize_8k_cik,
    _target_windows,
    _targeted_history_pages,
    _validate_target_quarters,
)
from ._earnings_8k_parse_metrics import (
    Sec8KMetric,
    Sec8KMetricCandidate,
    parse_sec_8k_earnings_metrics,
)
from .errors import ResourceLimitError
from .event_discovery import _strict_json, _submission_rows
from .http import SecHttpClient
from .submissions import fetch_sec_submissions_root
from .ticker_cik import fetch_sec_ticker_cik_mapping

_DEFAULT_METRICS: tuple[Sec8KMetric, ...] = (
    Sec8KMetric.REVENUE,
    Sec8KMetric.GROSS_PROFIT,
    Sec8KMetric.GAAP_DILUTED_EPS,
)
_MAX_METRICS_QUARTERS = 64


class Sec8KMetricStatus(str, Enum):
    """Classification status for a direct Form 8-K metric."""

    PRESENT = "PRESENT"
    MISSING = "MISSING"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class Sec8KMetricField:
    """One direct GAAP metric outcome for a target quarter with full exhibit evidence."""

    metric: Sec8KMetric
    status: Sec8KMetricStatus
    value: Decimal | None = None
    unit: str | None = None
    native_label: str | None = None
    declared_scale: str | None = None
    evidence: tuple[Sec8KReleaseEvidence, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class Sec8KQuarterlyMetricsItem:
    """Direct GAAP metrics disclosed for one discrete quarter in Form 8-K Item 2.02 exhibits."""

    fiscal_year: int | None
    fiscal_quarter: int | None
    period_end: date
    fields: tuple[Sec8KMetricField, ...]

    def field(self, metric: Sec8KMetric) -> Sec8KMetricField:
        for item in self.fields:
            if item.metric is metric:
                return item
        raise KeyError(metric)


@dataclass(frozen=True)
class Sec8KQuarterlyMetricsResult:
    """Direct 8-K earnings release multi-metric query result."""

    cik: str
    ticker: str | None
    metrics: tuple[Sec8KMetric, ...]
    items: tuple[Sec8KQuarterlyMetricsItem, ...]
    mapping_observation_id: str | None
    submissions_observation_ids: tuple[str, ...]
    coverage_complete: bool


def _validate_metrics(metrics: tuple[Sec8KMetric, ...]) -> tuple[Sec8KMetric, ...]:
    if not isinstance(metrics, tuple) or not metrics:
        raise ValueError("metrics must be a non-empty tuple of Sec8KMetric values")
    if any(not isinstance(m, Sec8KMetric) for m in metrics):
        raise TypeError("metrics must contain only Sec8KMetric members")
    if len(set(metrics)) != len(metrics):
        raise ValueError("metrics must not contain duplicates")
    return metrics


def _validate_bound(acceptance_upper: datetime | None) -> datetime | None:
    if acceptance_upper is None:
        return None
    if (
        not isinstance(acceptance_upper, datetime)
        or acceptance_upper.tzinfo is None
        or acceptance_upper.utcoffset() is None
    ):
        raise ValueError("acceptance_upper must be timezone-aware")
    return acceptance_upper.astimezone(UTC)


def _make_evidence(
    cand: Sec8KMetricCandidate,
    fiscal_year: int,
    fiscal_period_focus: str,
    filing: _Filing8K,
    index_url: str,
    exhibit_url: str,
    index_obs_id: str,
    exhibit_obs_id: str,
) -> Sec8KReleaseEvidence:
    return Sec8KReleaseEvidence(
        native_label=cand.native_label,
        unit=cand.unit,
        value=cand.value,
        period_end=cand.period_end,
        fiscal_year_focus=fiscal_year,
        fiscal_period_focus=fiscal_period_focus,
        accession_number=filing.accession,
        form=filing.form,
        accepted_at=filing.accepted_at,
        filing_url=index_url,
        exhibit_url=exhibit_url,
        index_observation_id=index_obs_id,
        exhibit_observation_id=exhibit_obs_id,
    )


def _resolve_field(
    metric: Sec8KMetric,
    cands: list[tuple[Sec8KMetricCandidate, Sec8KReleaseEvidence]],
    ambigs: list[tuple[str, list[Sec8KReleaseEvidence]]],
) -> Sec8KMetricField:
    if ambigs:
        all_ev: list[Sec8KReleaseEvidence] = []
        for _, ev_list in ambigs:
            all_ev.extend(ev_list)
        all_ev.extend(ev for _, ev in cands)
        return Sec8KMetricField(
            metric=metric,
            status=Sec8KMetricStatus.AMBIGUOUS,
            evidence=tuple(all_ev),
            reason=ambigs[0][0],
        )
    if not cands:
        return Sec8KMetricField(
            metric=metric,
            status=Sec8KMetricStatus.MISSING,
            reason=f"no direct GAAP {metric.value} found in 8-K exhibits",
        )
    distinct_values = {ev.value for _, ev in cands}
    if len(distinct_values) > 1:
        return Sec8KMetricField(
            metric=metric,
            status=Sec8KMetricStatus.AMBIGUOUS,
            evidence=tuple(ev for _, ev in cands),
            reason=f"conflicting 8-K GAAP {metric.value} values across filings/exhibits",
        )
    best_cand, best_ev = cands[-1]
    evidences = (best_ev, *(ev for _, ev in cands[:-1]))
    return Sec8KMetricField(
        metric=metric,
        status=Sec8KMetricStatus.PRESENT,
        value=best_ev.value,
        unit=best_ev.unit,
        native_label=best_ev.native_label,
        declared_scale=best_cand.declared_scale,
        evidence=evidences,
    )


def fetch_sec_8k_quarterly_metrics(
    client: SecHttpClient,
    store: SnapshotStore,
    *,
    cik: str | None = None,
    ticker: str | None = None,
    target_quarters: tuple[tuple[int, int, date], ...],
    metrics: tuple[Sec8KMetric, ...] = _DEFAULT_METRICS,
    acceptance_upper: datetime | None = None,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Sec8KQuarterlyMetricsResult:
    """Fetch direct GAAP Revenue, Gross Profit, and/or Diluted EPS from Form 8-K Item 2.02.

    Supports CIK-first resolution (for delisted issuers) as well as ticker lookup.
    Applies exact Decimal scaling only when the exhibit table header unambiguously
    declares a monetary scale; rejects guidance, non-GAAP, segment, margin %, and YTD rows.
    """
    if not isinstance(client, SecHttpClient) or not isinstance(store, SnapshotStore):
        raise TypeError("client and store must be SEC HTTP and snapshot clients")
    if not callable(utc_now):
        raise TypeError("utc_now must be callable")
    validated_metrics = _validate_metrics(metrics)
    _validate_target_quarters(target_quarters, max_quarters=_MAX_METRICS_QUARTERS)
    bound = _validate_bound(acceptance_upper)

    canonical_ticker: str | None = None
    if ticker is not None:
        if type(ticker) is not str or not ticker.strip():
            raise ValueError("ticker must be non-empty when provided")
        canonical_ticker = ticker.strip().upper()

    mapping_obs_id: str | None = None
    if cik is not None:
        normalized_cik = _normalize_8k_cik(cik)
    elif canonical_ticker is not None:
        mapping = fetch_sec_ticker_cik_mapping(client, store, utc_now=utc_now)
        resolved = mapping.resolve(canonical_ticker)
        normalized_cik = f"{int(resolved.cik):010d}"
        mapping_obs_id = mapping.observation.observation_identity
    else:
        raise ValueError("either cik or ticker must be provided")

    root_source = fetch_sec_submissions_root(store, client, normalized_cik, utc_now=utc_now)
    root = _root_payload(store, root_source, normalized_cik)

    history_names = _targeted_history_pages(root, normalized_cik, target_quarters)
    all_rows = list(_submission_rows(root, normalized_cik, child=False))
    total_history_bytes = 0
    historical_obs_ids: list[str] = [root_source.observation.observation_identity]

    for name in history_names:
        source = _fetch_history_page(client, store, normalized_cik, name, utc_now)
        replayed = store.replay_observation(source.observation, max_payload_bytes=8 * 1024**2)
        total_history_bytes += len(replayed.payload)
        if total_history_bytes > _MAX_HISTORY_BYTES:
            raise ResourceLimitError("targeted SEC history bytes exceed limit")
        child_payload = _strict_json(replayed.payload)
        all_rows.extend(_submission_rows(child_payload, normalized_cik, child=True))
        historical_obs_ids.append(source.observation.observation_identity)

    target_windows = _target_windows(target_quarters)
    filings = _discover_8k_filings(all_rows, bound, target_windows)

    cands_by_key: dict[
        tuple[Sec8KMetric, date], list[tuple[Sec8KMetricCandidate, Sec8KReleaseEvidence]]
    ] = {}
    ambigs_by_key: dict[tuple[Sec8KMetric, date], list[tuple[str, list[Sec8KReleaseEvidence]]]] = {}

    for filing in filings:
        eligible_targets = [
            (fy, fq, pend)
            for fy, fq, pend in target_quarters
            if pend <= filing.filing_date <= pend + timedelta(days=_RELEASE_SEARCH_DAYS)
        ]
        if not eligible_targets:
            continue

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
        if not doc_names:
            continue

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
            for fy, fq, pend in eligible_targets:
                parsed = parse_sec_8k_earnings_metrics(
                    exhibit_body,
                    metrics=validated_metrics,
                    expected_period_end=pend,
                )
                fp_focus = f"Q{fq}"
                for metric in validated_metrics:
                    key = (metric, pend)
                    if key in parsed.ambiguities:
                        amb = parsed.ambiguities[key]
                        evs = [
                            _make_evidence(
                                c,
                                fy,
                                fp_focus,
                                filing,
                                index_url,
                                exhibit_url,
                                index_obs.observation_identity,
                                exhibit_obs.observation_identity,
                            )
                            for c in amb.candidates
                        ]
                        ambigs_by_key.setdefault(key, []).append((amb.reason, evs))
                    elif key in parsed.values:
                        cand = parsed.values[key]
                        ev = _make_evidence(
                            cand,
                            fy,
                            fp_focus,
                            filing,
                            index_url,
                            exhibit_url,
                            index_obs.observation_identity,
                            exhibit_obs.observation_identity,
                        )
                        cands_by_key.setdefault(key, []).append((cand, ev))

    items: list[Sec8KQuarterlyMetricsItem] = []
    for fy, fq, pend in target_quarters:
        fields = tuple(
            _resolve_field(
                metric,
                cands_by_key.get((metric, pend), []),
                ambigs_by_key.get((metric, pend), []),
            )
            for metric in validated_metrics
        )
        items.append(Sec8KQuarterlyMetricsItem(fy, fq, pend, fields))

    return Sec8KQuarterlyMetricsResult(
        cik=normalized_cik,
        ticker=canonical_ticker,
        metrics=validated_metrics,
        items=tuple(items),
        mapping_observation_id=mapping_obs_id,
        submissions_observation_ids=tuple(historical_obs_ids),
        coverage_complete=True,
    )


__all__ = [
    "_DEFAULT_METRICS",
    "Sec8KMetric",
    "Sec8KMetricField",
    "Sec8KMetricStatus",
    "Sec8KQuarterlyMetricsItem",
    "Sec8KQuarterlyMetricsResult",
    "_make_evidence",
    "_resolve_field",
    "_validate_bound",
    "_validate_metrics",
    "fetch_sec_8k_quarterly_metrics",
]

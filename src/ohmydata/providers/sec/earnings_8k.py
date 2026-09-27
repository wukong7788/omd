"""SEC Form 8-K Item 2.02 earnings release GAAP diluted EPS discovery and enrichment."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum

from ...core import RequestSpec, SnapshotMode, SnapshotStore
from ._companyfacts_models import (
    Sec8KReleaseEvidence,
    SecCanonicalFieldStatus,
    SecCanonicalMetric,
    SecCanonicalQuarterField,
    SecCanonicalQuarterlyResult,
    SecCanonicalQuarterSlot,
)
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
    _target_windows,
    _targeted_history_pages,
    _validate_target_quarters,
)
from ._earnings_8k_parse import Sec8KEpsAmbiguousError, parse_sec_8k_earnings_release
from .errors import CoverageError, ResourceLimitError
from .event_discovery import _strict_json, _submission_rows
from .http import SecHttpClient, validate_sec_url
from .submissions import fetch_sec_submissions_root
from .ticker_cik import fetch_sec_ticker_cik_mapping

_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")


class Sec8KEpsStatus(str, Enum):
    """Classification status for Form 8-K GAAP diluted EPS evidence."""

    PRESENT = "PRESENT"
    MISSING = "MISSING"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class Sec8KQuarterlyEpsItem:
    """Direct GAAP diluted EPS disclosed in an SEC Form 8-K Item 2.02 earnings exhibit."""

    fiscal_year: int
    fiscal_quarter: int
    period_end: date
    status: Sec8KEpsStatus
    value: Decimal | None = None
    unit: str | None = None
    native_label: str | None = None
    accession_number: str | None = None
    form: str | None = None
    filing_date: date | None = None
    accepted_at: datetime | None = None
    filing_url: str | None = None
    index_url: str | None = None
    exhibit_url: str | None = None
    index_observation_id: str | None = None
    exhibit_observation_id: str | None = None
    alternatives: tuple[Sec8KQuarterlyEpsItem, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class Sec8KQuarterlyEpsResult:
    """Direct 8-K earnings release GAAP diluted EPS query result."""

    ticker: str
    cik: str
    items: tuple[Sec8KQuarterlyEpsItem, ...]
    mapping_observation_id: str
    submissions_observation_ids: tuple[str, ...]
    coverage_complete: bool


def _make_candidate_item(
    fy: int,
    fq: int,
    period_end: date,
    val: Decimal,
    native_label: str,
    filing: _Filing8K,
    index_url: str,
    exhibit_url: str,
    index_obs_id: str,
    exhibit_obs_id: str,
) -> Sec8KQuarterlyEpsItem:
    return Sec8KQuarterlyEpsItem(
        fiscal_year=fy,
        fiscal_quarter=fq,
        period_end=period_end,
        status=Sec8KEpsStatus.PRESENT,
        value=val,
        unit="USD/shares",
        native_label=native_label,
        accession_number=filing.accession,
        form=filing.form,
        filing_date=filing.filing_date,
        accepted_at=filing.accepted_at,
        filing_url=index_url,
        index_url=index_url,
        exhibit_url=exhibit_url,
        index_observation_id=index_obs_id,
        exhibit_observation_id=exhibit_obs_id,
    )


def _missing_item(fy: int, fq: int, pend: date) -> Sec8KQuarterlyEpsItem:
    return Sec8KQuarterlyEpsItem(
        fiscal_year=fy,
        fiscal_quarter=fq,
        period_end=pend,
        status=Sec8KEpsStatus.MISSING,
        reason="no direct GAAP diluted EPS found in 8-K exhibits",
    )


def _ambiguous_item(
    fy: int,
    fq: int,
    pend: date,
    cands: list[Sec8KQuarterlyEpsItem],
    reason: str,
) -> Sec8KQuarterlyEpsItem:
    best = cands[-1]
    return replace(
        best,
        fiscal_year=fy,
        fiscal_quarter=fq,
        period_end=pend,
        status=Sec8KEpsStatus.AMBIGUOUS,
        value=None,
        unit=None,
        native_label=None,
        alternatives=tuple(cands),
        reason=reason,
    )


def _present_item(cands: list[Sec8KQuarterlyEpsItem]) -> Sec8KQuarterlyEpsItem:
    best = cands[-1]
    return replace(best, alternatives=tuple(cands[:-1]))


def fetch_sec_8k_quarterly_eps(
    ticker: str,
    client: SecHttpClient,
    store: SnapshotStore,
    *,
    target_quarters: tuple[tuple[int, int, date], ...],
    acceptance_upper: datetime | None = None,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Sec8KQuarterlyEpsResult:
    """Fetch direct GAAP diluted EPS from SEC Form 8-K Item 2.02 earnings releases.

    Only direct, non-derived GAAP diluted EPS values disclosed in exhibit tables
    matching target discrete quarter ends are admitted. Non-GAAP, guidance, and
    annual-minus-9M derivations are strictly excluded.
    """
    if not isinstance(client, SecHttpClient) or not isinstance(store, SnapshotStore):
        raise TypeError("client and store must be SEC HTTP and snapshot clients")
    if not callable(utc_now):
        raise TypeError("utc_now must be callable")
    canonical_ticker = ticker.strip().upper() if type(ticker) is str else ""
    if not canonical_ticker:
        raise ValueError("ticker must be non-empty")
    _validate_target_quarters(target_quarters)
    if acceptance_upper is not None and (
        not isinstance(acceptance_upper, datetime)
        or acceptance_upper.tzinfo is None
        or acceptance_upper.utcoffset() is None
    ):
        raise ValueError("acceptance_upper must be timezone-aware")
    bound = acceptance_upper.astimezone(UTC) if acceptance_upper is not None else None

    mapping = fetch_sec_ticker_cik_mapping(client, store, utc_now=utc_now)
    resolved = mapping.resolve(canonical_ticker)
    cik = f"{int(resolved.cik):010d}"

    root_source = fetch_sec_submissions_root(store, client, cik, utc_now=utc_now)
    root = _root_payload(store, root_source, cik)

    history_names = _targeted_history_pages(root, cik, target_quarters)
    all_rows = list(_submission_rows(root, cik, child=False))
    total_history_bytes = 0
    historical_obs_ids: list[str] = [root_source.observation.observation_identity]

    for name in history_names:
        source = _fetch_history_page(client, store, cik, name, utc_now)
        replayed = store.replay_observation(source.observation, max_payload_bytes=8 * 1024**2)
        total_history_bytes += len(replayed.payload)
        if total_history_bytes > _MAX_HISTORY_BYTES:
            raise ResourceLimitError("targeted SEC history bytes exceed limit")
        child_payload = _strict_json(replayed.payload)
        all_rows.extend(_submission_rows(child_payload, cik, child=True))
        historical_obs_ids.append(source.observation.observation_identity)

    target_windows = _target_windows(target_quarters)
    filings = _discover_8k_filings(all_rows, bound, target_windows)
    candidates_by_period: dict[date, list[Sec8KQuarterlyEpsItem]] = {}
    within_exhibit_ambiguities: dict[date, list[Sec8KQuarterlyEpsItem]] = {}

    for filing in filings:
        eligible_targets = [
            (fy, fq, pend)
            for fy, fq, pend in target_quarters
            if pend <= filing.filing_date <= pend + timedelta(days=_RELEASE_SEARCH_DAYS)
        ]
        if not eligible_targets:
            continue

        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{filing.accession.replace('-', '')}"
        index_url = f"{base}/{filing.accession}-index.htm"
        index_body = _fetch_html(client, index_url, _INDEX_BYTES)
        index_observation = store.observe(
            RequestSpec("sec", "edgar_8k_index", {"cik": cik, "accession": filing.accession}),
            index_body,
            utc_now(),
            "sec-8k-index-html-v1",
            SnapshotMode.APPEND,
        )
        doc_names = _exhibit_basenames(index_body, cik, filing.accession)
        if not doc_names:
            continue

        for doc_name in doc_names:
            exhibit_url = f"{base}/{doc_name}"
            exhibit_body = _fetch_html(client, exhibit_url, _EXHIBIT_BYTES)
            exhibit_observation = store.observe(
                RequestSpec(
                    "sec",
                    "edgar_8k_exhibit",
                    {"cik": cik, "accession": filing.accession, "document": doc_name},
                ),
                exhibit_body,
                utc_now(),
                "sec-8k-exhibit-html-v1",
                SnapshotMode.APPEND,
            )
            for fy, fq, pend in eligible_targets:
                try:
                    parsed_values = parse_sec_8k_earnings_release(
                        exhibit_body, expected_period_end=pend
                    )
                except Sec8KEpsAmbiguousError as exc:
                    if exc.period_end == pend:
                        for cand in exc.candidates:
                            within_exhibit_ambiguities.setdefault(pend, []).append(
                                _make_candidate_item(
                                    fy,
                                    fq,
                                    pend,
                                    cand.value,
                                    cand.native_label,
                                    filing,
                                    index_url,
                                    exhibit_url,
                                    index_observation.observation_identity,
                                    exhibit_observation.observation_identity,
                                )
                            )
                    continue

                if pend in parsed_values:
                    val, native_label = parsed_values[pend]
                    candidates_by_period.setdefault(pend, []).append(
                        _make_candidate_item(
                            fy,
                            fq,
                            pend,
                            val,
                            native_label,
                            filing,
                            index_url,
                            exhibit_url,
                            index_observation.observation_identity,
                            exhibit_observation.observation_identity,
                        )
                    )

    resolved_items: list[Sec8KQuarterlyEpsItem] = []
    for fy, fq, pend in target_quarters:
        if pend in within_exhibit_ambiguities:
            cands = within_exhibit_ambiguities[pend] + candidates_by_period.get(pend, [])
            msg = "conflicting GAAP diluted EPS candidate rows in 8-K exhibit"
            resolved_items.append(_ambiguous_item(fy, fq, pend, cands, msg))
            continue

        cands = candidates_by_period.get(pend, [])
        if not cands:
            resolved_items.append(_missing_item(fy, fq, pend))
            continue

        distinct_values = {c.value for c in cands if c.value is not None}
        if len(distinct_values) > 1:
            msg = "conflicting 8-K GAAP diluted EPS values across filings/exhibits"
            resolved_items.append(_ambiguous_item(fy, fq, pend, cands, msg))
        else:
            resolved_items.append(_present_item(cands))

    return Sec8KQuarterlyEpsResult(
        canonical_ticker,
        cik,
        tuple(resolved_items),
        mapping.observation.observation_identity,
        tuple(historical_obs_ids),
        True,
    )


def _validate_present_item(item: Sec8KQuarterlyEpsItem) -> None:
    if not isinstance(item.value, Decimal):
        raise TypeError("PRESENT item must have valid Decimal value")
    if not item.value.is_finite():
        raise ValueError("PRESENT item value must be finite Decimal")
    if not item.unit or not item.native_label:
        raise ValueError("PRESENT item must have non-empty unit and native_label")
    if not item.accession_number or _ACCESSION.fullmatch(item.accession_number) is None:
        raise ValueError("PRESENT item must have valid accession_number")
    if item.form not in {"8-K", "8-K/A"}:
        raise ValueError("PRESENT item must have valid form (8-K or 8-K/A)")
    if (
        not isinstance(item.accepted_at, datetime)
        or item.accepted_at.tzinfo is None
        or item.accepted_at.utcoffset() is None
    ):
        raise ValueError("PRESENT item must have timezone-aware accepted_at")
    for url, name in (
        (item.filing_url, "filing_url"),
        (item.index_url, "index_url"),
        (item.exhibit_url, "exhibit_url"),
    ):
        if not url:
            raise ValueError(f"PRESENT item must have non-empty {name}")
        validate_sec_url(url)
    if not item.index_observation_id or not item.exhibit_observation_id:
        raise ValueError("PRESENT item must have non-empty index and exhibit observation IDs")


def _validate_ambiguous_item(item: Sec8KQuarterlyEpsItem) -> None:
    if not item.alternatives:
        raise ValueError("AMBIGUOUS item must have non-empty alternatives")
    for alt in item.alternatives:
        if alt.status is not Sec8KEpsStatus.PRESENT:
            raise ValueError(f"AMBIGUOUS alternative must have status PRESENT, got {alt.status}")
        if (alt.fiscal_year, alt.fiscal_quarter, alt.period_end) != (
            item.fiscal_year,
            item.fiscal_quarter,
            item.period_end,
        ):
            raise ValueError("AMBIGUOUS alternative period mismatch with parent item")
        _validate_present_item(alt)


def _item_to_release_evidence(
    item: Sec8KQuarterlyEpsItem,
    fiscal_year: int,
) -> Sec8KReleaseEvidence:
    _validate_present_item(item)
    assert item.value is not None and item.unit and item.native_label and item.accession_number
    assert item.form and item.accepted_at and item.filing_url and item.exhibit_url
    assert item.index_observation_id and item.exhibit_observation_id
    return Sec8KReleaseEvidence(
        native_label=item.native_label,
        unit=item.unit,
        value=item.value,
        period_end=item.period_end,
        fiscal_year_focus=fiscal_year,
        fiscal_period_focus="Q4",
        accession_number=item.accession_number,
        form=item.form,
        accepted_at=item.accepted_at,
        filing_url=item.filing_url,
        exhibit_url=item.exhibit_url,
        index_observation_id=item.index_observation_id,
        exhibit_observation_id=item.exhibit_observation_id,
    )


def enrich_sec_canonical_quarters_with_8k_eps(
    canonical_result: SecCanonicalQuarterlyResult,
    eps_result: Sec8KQuarterlyEpsResult,
) -> SecCanonicalQuarterlyResult:
    """Enrich missing Q4 GAAP diluted EPS fields in a SecCanonicalQuarterlyResult.

    Preserves existing 10-K explicit Q4 facts as first priority. Only MISSING Q4
    EPS fields are populated from direct 8-K evidence.
    """
    if not isinstance(canonical_result, SecCanonicalQuarterlyResult):
        raise TypeError("canonical_result must be a SecCanonicalQuarterlyResult")
    if not isinstance(eps_result, Sec8KQuarterlyEpsResult):
        raise TypeError("eps_result must be a Sec8KQuarterlyEpsResult")
    if not eps_result.coverage_complete:
        raise CoverageError(
            f"SEC 8-K EPS result for ticker {eps_result.ticker!r} has incomplete coverage"
        )
    if canonical_result.ticker != eps_result.ticker:
        raise ValueError(
            f"ticker mismatch: canonical={canonical_result.ticker!r}, 8k={eps_result.ticker!r}"
        )
    if canonical_result.cik is not None and canonical_result.cik != eps_result.cik:
        raise ValueError(f"CIK mismatch: canonical={canonical_result.cik!r}, 8k={eps_result.cik!r}")

    eps_by_key: dict[tuple[int, int, date], Sec8KQuarterlyEpsItem] = {}
    for item in eps_result.items:
        key = (item.fiscal_year, item.fiscal_quarter, item.period_end)
        if key in eps_by_key:
            raise ValueError(f"duplicate 8-K EPS item for period {key}")
        eps_by_key[key] = item
        if item.status is Sec8KEpsStatus.PRESENT:
            _validate_present_item(item)
        elif item.status is Sec8KEpsStatus.AMBIGUOUS:
            _validate_ambiguous_item(item)
        elif item.status is not Sec8KEpsStatus.MISSING:
            raise ValueError(f"unrecognized 8-K EPS item status: {item.status}")

    new_slots: list[SecCanonicalQuarterSlot] = []
    for slot in canonical_result.slots:
        if slot.fiscal_quarter != 4:
            new_slots.append(slot)
            continue
        eps_field = slot.field(SecCanonicalMetric.GAAP_DILUTED_EPS)
        if eps_field.status is not SecCanonicalFieldStatus.MISSING:
            new_slots.append(slot)
            continue

        rev_field = slot.field(SecCanonicalMetric.REVENUE)
        if not rev_field.evidence:
            new_slots.append(slot)
            continue
        period_end = rev_field.evidence[0].period_end

        item = eps_by_key.get((slot.fiscal_year, 4, period_end))
        new_field: SecCanonicalQuarterField | None = None
        if item is not None and item.status is Sec8KEpsStatus.PRESENT:
            evidence = _item_to_release_evidence(item, slot.fiscal_year)
            new_field = SecCanonicalQuarterField(
                SecCanonicalMetric.GAAP_DILUTED_EPS,
                SecCanonicalFieldStatus.PRESENT,
                item.value,
                item.unit,
                (evidence,),
            )
        elif item is not None and item.status is Sec8KEpsStatus.AMBIGUOUS:
            evidences = tuple(
                _item_to_release_evidence(alt, slot.fiscal_year) for alt in item.alternatives
            )
            new_field = SecCanonicalQuarterField(
                SecCanonicalMetric.GAAP_DILUTED_EPS,
                SecCanonicalFieldStatus.AMBIGUOUS,
                None,
                None,
                evidences,
            )

        if new_field is not None:
            updated_fields = tuple(
                new_field if f.metric is SecCanonicalMetric.GAAP_DILUTED_EPS else f
                for f in slot.fields
            )
            new_slots.append(replace(slot, fields=updated_fields))
        else:
            new_slots.append(slot)

    all_sub_ids = tuple(
        dict.fromkeys(
            (*canonical_result.submissions_observation_ids, *eps_result.submissions_observation_ids)
        )
    )
    return replace(
        canonical_result, slots=tuple(new_slots), submissions_observation_ids=all_sub_ids
    )


__all__ = [
    "Sec8KEpsAmbiguousError",
    "Sec8KEpsStatus",
    "Sec8KQuarterlyEpsItem",
    "Sec8KQuarterlyEpsResult",
    "enrich_sec_canonical_quarters_with_8k_eps",
    "fetch_sec_8k_quarterly_eps",
]

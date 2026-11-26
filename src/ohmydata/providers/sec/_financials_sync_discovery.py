"""Discovery supplement, event filtering, and edgartools adapter helpers for incremental sync."""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from ...core.snapshot import SnapshotStore
from ._event_discovery_models import (
    SecDiscoverySource,
    SecFilingDiscoveryEvent,
    SecFilingEventKind,
    _canonical,
    _parse_stamp,
    _stamp,
)
from ._financials_sync_models import SUPPORTED_SYNC_FORMS, SecCursorRecord, utc_iso
from ._financials_sync_state import normalize_cik
from .edgartools_adapter import SecFinancialsClient
from .errors import CoverageError, ResourceLimitError, SchemaMismatchError
from .event_discovery import (
    _MAX_EVENTS,
    _MAX_ROWS,
    _MAX_SOURCE_BYTES,
    _MAX_STRING,
    _MAX_TOTAL_BYTES,
    _strict_json,
    _submission_rows,
)
from .financials import SecCompanyFinancialVintage, SecFinancialsRequest

_ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

SecFilingFetcher = Callable[
    [str, tuple[SecFilingDiscoveryEvent, ...]], Sequence[SecCompanyFinancialVintage]
]


def ensure_aware_utc(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def normalize_forms(forms: Sequence[str]) -> tuple[str, ...]:
    if not forms:
        raise ValueError("forms cannot be empty")
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in forms:
        if not isinstance(raw, str) or raw not in SUPPORTED_SYNC_FORMS:
            raise ValueError(f"unsupported SEC sync form: {raw!r}")
        if raw not in seen:
            seen.add(raw)
            ordered.append(raw)
    return tuple(ordered)


def normalize_universe(ciks: Sequence[str]) -> tuple[str, ...]:
    if not ciks:
        raise ValueError("ciks cannot be empty")
    seen: set[str] = set()
    ordered: list[str] = []
    for item in ciks:
        norm = normalize_cik(item)
        if norm not in seen:
            seen.add(norm)
            ordered.append(norm)
    return tuple(ordered)


def discover_extra_form_events(
    store: SnapshotStore,
    sources: tuple[SecDiscoverySource, ...],
    *,
    canonical_cik: str,
    extra_forms: frozenset[str],
    acceptance_lower: datetime,
    acceptance_upper: datetime,
) -> tuple[SecFilingDiscoveryEvent, ...]:
    """Extract additional form events (e.g. 20-F, 40-F, 6-K) from already-retained snapshots."""
    if not extra_forms:
        return ()
    padded = canonical_cik.zfill(10)
    rows: dict[str, tuple[dict[str, object], list[str]]] = {}
    observed_bytes = 0
    row_count = 0
    for idx, source in enumerate(sources):
        replay = store.replay_observation(source.observation, max_payload_bytes=_MAX_SOURCE_BYTES)
        observed_bytes += len(replay.payload)
        if observed_bytes > _MAX_TOTAL_BYTES:
            raise ResourceLimitError("aggregate submissions bytes exceeded")
        payload = _strict_json(replay.payload)
        for row in _submission_rows(payload, padded, child=idx > 0):
            row_count += 1
            if row_count > _MAX_ROWS:
                raise ResourceLimitError("aggregate filing row limit exceeded")
            accession = row["accessionNumber"]
            if not isinstance(accession, str) or _ACCESSION_RE.fullmatch(accession) is None:
                raise SchemaMismatchError("invalid accession")
            prior = rows.get(accession)
            if prior is not None and prior[0] != row:
                raise SchemaMismatchError("conflicting submission metadata")
            if prior is None:
                rows[accession] = (row, [source.observation.observation_identity])
            elif source.observation.observation_identity not in prior[1]:
                prior[1].append(source.observation.observation_identity)

    events: list[SecFilingDiscoveryEvent] = []
    for accession, (row, identities) in rows.items():
        form = row["form"]
        filing_date = row["filingDate"]
        report_date = row["reportDate"]
        document = row["primaryDocument"]
        if not all(
            isinstance(value, str) and len(value.encode()) <= _MAX_STRING
            for value in (form, filing_date, document)
        ) or (
            report_date is not None
            and (not isinstance(report_date, str) or len(report_date.encode()) > _MAX_STRING)
        ):
            raise SchemaMismatchError("invalid submission metadata")
        accepted = _parse_stamp(row["acceptanceDateTime"], "acceptance timestamp")
        if form not in extra_forms or not (acceptance_lower <= accepted <= acceptance_upper):
            continue
        items = row.get("items")
        if items is not None and (not isinstance(items, str) or len(items.encode()) > _MAX_STRING):
            raise SchemaMismatchError("invalid submission items")
        metadata = {
            "accession": accession,
            "form": form,
            "filing_date": filing_date,
            "report_date": report_date,
            "acceptance_native": row["acceptanceDateTime"],
            "acceptance_at": _stamp(accepted),
            "primary_document": document,
            "items": items,
        }
        digest = hashlib.sha256(_canonical(metadata)).hexdigest()
        kind = SecFilingEventKind.SEC_FILING_ACCEPTED
        key = hashlib.sha256(
            _canonical(
                {
                    "schema_version": 1,
                    "provider": "sec",
                    "cik": canonical_cik,
                    "accession": accession,
                    "kind": kind.value,
                }
            )
        ).hexdigest()
        if len(events) >= _MAX_EVENTS:
            raise ResourceLimitError("discovery event limit exceeded")
        events.append(
            SecFilingDiscoveryEvent(
                key=key,
                cik=canonical_cik,
                accession=accession,
                kind=kind,
                form=cast(str, form),
                filing_date=cast(str, filing_date),
                report_date=report_date,
                acceptance_native=cast(str, row["acceptanceDateTime"]),
                acceptance_at=accepted,
                primary_document=cast(str, document),
                items=items,
                metadata_digest=digest,
                source_observation_identities=tuple(sorted(identities)),
            )
        )
    events.sort(key=lambda item: item.key)
    return tuple(events)


def filter_new_events(
    events: tuple[SecFilingDiscoveryEvent, ...],
    prior_record: SecCursorRecord | None,
) -> tuple[SecFilingDiscoveryEvent, ...]:
    """Deduplicate overlap-window events against prior cursor state without dropping late filings."""
    if prior_record is None:
        return events
    if prior_record.processed_accessions is not None:
        seen = frozenset(prior_record.processed_accessions)
        return tuple(e for e in events if e.accession not in seen)
    pc = prior_record.cursor
    return tuple(
        e for e in events if (e.acceptance_at, e.accession) > (pc.acceptance_at, pc.accession)
    )


def compute_run_id(
    receipts_dir: Path,
    *,
    requested_run_id: str | None,
    started_at: datetime,
    ciks: tuple[str, ...],
    forms: tuple[str, ...],
    as_of: datetime,
) -> str:
    if requested_run_id is not None:
        if (
            not isinstance(requested_run_id, str)
            or _SAFE_RUN_ID_RE.fullmatch(requested_run_id) is None
        ):
            raise ValueError(f"invalid run_id: {requested_run_id!r}")
        if (receipts_dir / f"sync-{requested_run_id}.json").exists():
            raise SchemaMismatchError(
                f"sync receipt already exists for run_id={requested_run_id!r}"
            )
        return requested_run_id

    compact_ts = started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    digest = hashlib.sha256(
        _canonical(
            {
                "ciks": list(ciks),
                "forms": list(forms),
                "as_of": utc_iso(as_of),
                "started_at": utc_iso(started_at),
            }
        )
    ).hexdigest()[:8]
    base = f"{compact_ts}-{digest}"
    candidate = base
    seq = 2
    while (receipts_dir / f"sync-{candidate}.json").exists():
        candidate = f"{base}-{seq}"
        seq += 1
    return candidate


def validate_fetched_vintages(
    cik: str,
    new_events: tuple[SecFilingDiscoveryEvent, ...],
    vintages: Sequence[SecCompanyFinancialVintage],
) -> tuple[SecCompanyFinancialVintage, ...]:
    items = tuple(vintages)
    if not items:
        raise CoverageError(
            f"filing fetcher returned no vintages for {len(new_events)} discovered event(s) "
            f"for CIK {cik}"
        )
    seen_accessions: set[str] = set()
    for item in items:
        if not isinstance(item, SecCompanyFinancialVintage):
            raise SchemaMismatchError(
                f"filing fetcher returned invalid object type {type(item).__name__}"
            )
        if normalize_cik(item.cik) != cik:
            raise SchemaMismatchError(
                f"vintage CIK {item.cik!r} does not match requested CIK {cik}"
            )
        if item.accession_number in seen_accessions:
            raise SchemaMismatchError(
                f"filing fetcher returned duplicate accession {item.accession_number!r} "
                f"for CIK {cik}"
            )
        seen_accessions.add(item.accession_number)
    discovered_accessions = {e.accession for e in new_events}
    unexpected = sorted(seen_accessions - discovered_accessions)
    if unexpected:
        raise SchemaMismatchError(
            f"filing fetcher returned unexpected accession(s) for CIK {cik}: {unexpected[0]}"
        )
    missing = sorted(discovered_accessions - seen_accessions)
    if missing:
        raise CoverageError(
            f"filing fetcher omitted {len(missing)} discovered accession(s) for CIK {cik}: "
            f"{missing[0]}"
        )
    return items


def make_edgartools_filing_fetcher(
    client: SecFinancialsClient,
    *,
    availability_policy: str = "accepted-at-plus-lag",
    lag_days: int = 0,
    include_dimensions: bool = True,
    parser_version: str = "sec-live-financial-parser-v2-edgartools-6.4.0",
    symbol_resolver: Callable[[str], str] | None = None,
) -> SecFilingFetcher:
    """Create a ``filing_fetcher`` adapter bridging discovery events to ``SecFinancialsClient``."""
    if not isinstance(client, SecFinancialsClient):
        raise TypeError("client must be a SecFinancialsClient")
    supports_ciks = "ciks" in inspect.signature(SecFinancialsRequest).parameters

    def _fetch(
        cik: str, events: tuple[SecFilingDiscoveryEvent, ...]
    ) -> tuple[SecCompanyFinancialVintage, ...]:
        norm_cik = normalize_cik(cik)
        target_accessions = {e.accession for e in events}
        event_forms = tuple(sorted({e.form for e in events}))
        filing_years = [int(e.filing_date[:4]) for e in events if len(e.filing_date) >= 4]
        start_year = min(filing_years) if filing_years else None
        end_year = max(filing_years) if filing_years else None
        include_amendments = any(f.endswith("/A") for f in event_forms)

        request_kwargs: dict[str, Any] = {
            "forms": event_forms,
            "start_year": start_year,
            "end_year": end_year,
            "availability_policy": availability_policy,
            "lag_days": lag_days,
            "include_amendments": include_amendments,
            "include_dimensions": include_dimensions,
            "parser_version": parser_version,
        }
        if supports_ciks:
            request_kwargs["ciks"] = (norm_cik,)
        else:
            if symbol_resolver is None:
                raise CoverageError(
                    "SecFinancialsRequest does not yet support ciks=; provide symbol_resolver or "
                    "a custom filing_fetcher"
                )
            request_kwargs["symbols"] = (symbol_resolver(norm_cik),)

        req = SecFinancialsRequest(**request_kwargs)
        vintages = client.fetch_company_financials(req)
        return tuple(v for v in vintages if v.accession_number in target_accessions)

    return _fetch


__all__ = [
    "SecFilingFetcher",
    "compute_run_id",
    "discover_extra_form_events",
    "ensure_aware_utc",
    "filter_new_events",
    "make_edgartools_filing_fetcher",
    "normalize_forms",
    "normalize_universe",
    "validate_fetched_vintages",
]

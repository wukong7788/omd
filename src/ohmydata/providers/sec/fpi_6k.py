"""Plugin-based foreign private issuer (FPI) SEC Form 6-K quarterly extraction."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from ...core import RequestSpec, SnapshotMode, SnapshotStore
from ._companyfacts_submissions import _fetch_history_page, _root_payload
from ._fpi_6k_index import (
    _INDEX_BYTES,
    _discover_6k_filings,
    _fetch_sec_document,
    _select_history_pages,
    _validate_utc_bound,
    _validate_year_bounds,
    parse_fpi_6k_index,
)
from ._fpi_6k_models import (
    SecFpi6KExhibitContext,
    SecFpi6KExhibitParser,
    SecFpi6KMetricRow,
    SecFpi6KParserRegistry,
    SecFpi6KPluginParseOutput,
    SecFpi6KQuarterRecord,
    SecFpi6KResult,
    SecFpi6KShareBasis,
    SecFpi6KSourceKind,
    SecTsm6KExhibitParser,
    default_fpi_6k_registry,
    normalize_fpi_cik,
)
from ._fpi_6k_xbrl import _MAX_XBRL_BYTES, parse_sec_fpi_6k_xbrl
from .errors import ResourceLimitError
from .event_discovery import _strict_json, _submission_rows
from .http import SecHttpClient
from .submissions import fetch_sec_submissions_root

_MAX_EXHIBIT_BYTES = 2 * 1024 * 1024
_MAX_HISTORY_BYTES = 64 * 1024**2
_XBRL_PARSER_ID = "sec-fpi-6k-xbrl-v1"


def fetch_sec_fpi_6k_quarters(
    cik: str,
    client: SecHttpClient,
    store: SnapshotStore,
    *,
    registry: SecFpi6KParserRegistry | None = None,
    count: int | None = 8,
    start_year: int | None = None,
    end_year: int | None = None,
    acceptance_upper: datetime | None = None,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SecFpi6KResult:
    """Fetch foreign private issuer Form 6-K disclosures using CIK-first resolution.

    Resolution priority for each Form 6-K filing:
    1. Retained XBRL / inline-XBRL attachment (``us-gaap`` + ``ifrs-full`` + ``dei``).
    2. Registered issuer-specific EX-99.1 plugin from ``registry``.
    3. Explicit ``UNSUPPORTED`` outcome with retained filing-index provenance.
    """
    normalized_cik = normalize_fpi_cik(cik)
    if not isinstance(client, SecHttpClient) or not isinstance(store, SnapshotStore):
        raise TypeError("client and store must be SEC HTTP and snapshot clients")
    if not callable(utc_now):
        raise TypeError("utc_now must be callable")
    if count is not None and (type(count) is not int or count < 1):
        raise ValueError("count must be None or a positive integer")
    _validate_year_bounds(start_year, end_year)
    bound = _validate_utc_bound(acceptance_upper)

    active_registry = default_fpi_6k_registry() if registry is None else registry
    if not isinstance(active_registry, SecFpi6KParserRegistry):
        raise TypeError("registry must be a SecFpi6KParserRegistry or None")

    root_source = fetch_sec_submissions_root(store, client, normalized_cik, utc_now=utc_now)
    root = _root_payload(store, root_source, normalized_cik)

    all_rows = list(_submission_rows(root, normalized_cik, child=False))
    submissions_obs_ids: list[str] = [root_source.observation.observation_identity]

    filings = _discover_6k_filings(all_rows, start_year=start_year, end_year=end_year, bound=bound)
    need_history = start_year is not None or count is None or len(filings) < count
    if need_history:
        history_names = _select_history_pages(
            root, normalized_cik, start_year=start_year, end_year=end_year
        )
        total_history_bytes = 0
        for name in history_names:
            source = _fetch_history_page(client, store, normalized_cik, name, utc_now)
            replayed = store.replay_observation(source.observation, max_payload_bytes=8 * 1024**2)
            total_history_bytes += len(replayed.payload)
            if total_history_bytes > _MAX_HISTORY_BYTES:
                raise ResourceLimitError("SEC 6-K history bytes exceed limit")
            child_payload = _strict_json(replayed.payload)
            all_rows.extend(_submission_rows(child_payload, normalized_cik, child=True))
            submissions_obs_ids.append(source.observation.observation_identity)
        filings = _discover_6k_filings(
            all_rows, start_year=start_year, end_year=end_year, bound=bound
        )

    if count is not None and len(filings) > count:
        filings = filings[-count:]

    if not filings:
        return SecFpi6KResult(
            cik=normalized_cik,
            records=(),
            submissions_observation_ids=tuple(submissions_obs_ids),
            coverage_complete=False,
        )

    plugin = active_registry.get(normalized_cik)
    records: list[SecFpi6KQuarterRecord] = []
    unsupported: list[str] = []
    empty_accns: list[str] = []

    for filing in filings:
        compact = filing.accession.replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{int(normalized_cik)}/{compact}"
        index_url = f"{base}/{filing.accession}-index.htm"
        index_body = _fetch_sec_document(client, index_url, "text/html", _INDEX_BYTES)
        index_obs = store.observe(
            RequestSpec(
                "sec", "edgar_fpi_6k_index", {"cik": normalized_cik, "accession": filing.accession}
            ),
            index_body,
            utc_now(),
            "sec-fpi-6k-index-html-v1",
            SnapshotMode.APPEND,
        )
        docs = parse_fpi_6k_index(index_body, normalized_cik, filing.accession)

        record: SecFpi6KQuarterRecord | None = None
        xbrl_candidate_obs_id: str | None = None
        xbrl_candidate_url: str | None = None
        xbrl_was_empty = False

        # Priority 1: XBRL / inline-XBRL attachment
        xbrl_candidates = docs.xbrl_documents or docs.inline_xbrl_documents
        if xbrl_candidates:
            xbrl_doc = xbrl_candidates[0]
            xbrl_url = f"{base}/{xbrl_doc}"
            accept_hdr = (
                "application/xml,text/xml,*/*"
                if xbrl_doc.lower().endswith(".xml")
                else "application/xhtml+xml,text/html,*/*"
            )
            xbrl_body = _fetch_sec_document(client, xbrl_url, accept_hdr, _MAX_XBRL_BYTES)
            xbrl_obs = store.observe(
                RequestSpec(
                    "sec",
                    "edgar_fpi_6k_xbrl",
                    {"cik": normalized_cik, "accession": filing.accession, "document": xbrl_doc},
                ),
                xbrl_body,
                utc_now(),
                "sec-fpi-6k-xbrl-bytes-v1",
                SnapshotMode.APPEND,
            )
            parsed_xbrl = parse_sec_fpi_6k_xbrl(
                xbrl_body, cik=normalized_cik, expected_period_end=filing.report_date
            )
            if parsed_xbrl.metrics:
                record = SecFpi6KQuarterRecord(
                    cik=normalized_cik,
                    accession_number=filing.accession,
                    form=filing.form,
                    filing_date=filing.filing_date,
                    accepted_at=filing.accepted_at,
                    period_end=parsed_xbrl.period_end,
                    source_kind=SecFpi6KSourceKind.XBRL_ATTACHMENT,
                    parser_id=_XBRL_PARSER_ID,
                    reporting_currency=parsed_xbrl.reporting_currency,
                    share_basis=SecFpi6KShareBasis.UNKNOWN,
                    metrics=parsed_xbrl.metrics,
                    index_observation_id=index_obs.observation_identity,
                    source_observation_id=xbrl_obs.observation_identity,
                    fiscal_year=parsed_xbrl.fiscal_year,
                    fiscal_quarter=parsed_xbrl.fiscal_quarter,
                    index_url=index_url,
                    source_url=xbrl_url,
                )
            else:
                xbrl_was_empty = True
                xbrl_candidate_obs_id = xbrl_obs.observation_identity
                xbrl_candidate_url = xbrl_url

        # Priority 2: Registered EX-99.1 plugin
        if record is None and plugin is not None and docs.exhibit_documents:
            exhibit_doc, exhibit_type = docs.exhibit_documents[0]
            exhibit_url = f"{base}/{exhibit_doc}"
            exhibit_body = _fetch_sec_document(client, exhibit_url, "text/html", _MAX_EXHIBIT_BYTES)
            exhibit_obs = store.observe(
                RequestSpec(
                    "sec",
                    "edgar_fpi_6k_exhibit",
                    {"cik": normalized_cik, "accession": filing.accession, "document": exhibit_doc},
                ),
                exhibit_body,
                utc_now(),
                "sec-fpi-6k-exhibit-html-v1",
                SnapshotMode.APPEND,
            )
            ctx = SecFpi6KExhibitContext(
                cik=normalized_cik,
                accession_number=filing.accession,
                form=filing.form,
                filing_date=filing.filing_date,
                report_date=filing.report_date,
                accepted_at=filing.accepted_at,
                primary_document=filing.primary_document,
                exhibit_document=exhibit_doc,
                exhibit_type=exhibit_type,
            )
            parsed_plugin = plugin.parse(exhibit_body, context=ctx)
            if parsed_plugin is not None:
                has_metrics = bool(parsed_plugin.metrics)
                record = SecFpi6KQuarterRecord(
                    cik=normalized_cik,
                    accession_number=filing.accession,
                    form=filing.form,
                    filing_date=filing.filing_date,
                    accepted_at=filing.accepted_at,
                    period_end=parsed_plugin.period_end,
                    source_kind=(
                        SecFpi6KSourceKind.EX99_PLUGIN if has_metrics else SecFpi6KSourceKind.EMPTY
                    ),
                    parser_id=plugin.parser_id,
                    reporting_currency=parsed_plugin.reporting_currency,
                    share_basis=parsed_plugin.share_basis,
                    metrics=parsed_plugin.metrics,
                    index_observation_id=index_obs.observation_identity,
                    source_observation_id=exhibit_obs.observation_identity,
                    fiscal_year=parsed_plugin.fiscal_year,
                    fiscal_quarter=parsed_plugin.fiscal_quarter,
                    index_url=index_url,
                    source_url=exhibit_url,
                    reason=None if has_metrics else "registered EX-99.1 plugin returned no metrics",
                )
                if not has_metrics:
                    empty_accns.append(filing.accession)
            else:
                record = SecFpi6KQuarterRecord(
                    cik=normalized_cik,
                    accession_number=filing.accession,
                    form=filing.form,
                    filing_date=filing.filing_date,
                    accepted_at=filing.accepted_at,
                    period_end=filing.report_date,
                    source_kind=SecFpi6KSourceKind.UNSUPPORTED,
                    parser_id=None,
                    reporting_currency=None,
                    share_basis=SecFpi6KShareBasis.UNKNOWN,
                    metrics=(),
                    index_observation_id=index_obs.observation_identity,
                    source_observation_id=exhibit_obs.observation_identity,
                    index_url=index_url,
                    source_url=exhibit_url,
                    reason="registered EX-99.1 plugin declined non-earnings 6-K exhibit",
                )
                unsupported.append(filing.accession)

        # Priority 3: Explicit EMPTY (XBRL had 0 facts) or UNSUPPORTED outcome
        if record is None:
            if xbrl_was_empty:
                record = SecFpi6KQuarterRecord(
                    cik=normalized_cik,
                    accession_number=filing.accession,
                    form=filing.form,
                    filing_date=filing.filing_date,
                    accepted_at=filing.accepted_at,
                    period_end=filing.report_date,
                    source_kind=SecFpi6KSourceKind.EMPTY,
                    parser_id=_XBRL_PARSER_ID,
                    reporting_currency=None,
                    share_basis=SecFpi6KShareBasis.UNKNOWN,
                    metrics=(),
                    index_observation_id=index_obs.observation_identity,
                    source_observation_id=xbrl_candidate_obs_id,
                    index_url=index_url,
                    source_url=xbrl_candidate_url,
                    reason="XBRL attachment contained no canonical quarterly facts",
                )
                empty_accns.append(filing.accession)
            else:
                reason = (
                    "no XBRL attachment and no EX-99.1 exhibit found in filing index"
                    if plugin is not None
                    else "no XBRL attachment and no registered EX-99.1 parser for CIK"
                )
                record = SecFpi6KQuarterRecord(
                    cik=normalized_cik,
                    accession_number=filing.accession,
                    form=filing.form,
                    filing_date=filing.filing_date,
                    accepted_at=filing.accepted_at,
                    period_end=filing.report_date,
                    source_kind=SecFpi6KSourceKind.UNSUPPORTED,
                    parser_id=None,
                    reporting_currency=None,
                    share_basis=SecFpi6KShareBasis.UNKNOWN,
                    metrics=(),
                    index_observation_id=index_obs.observation_identity,
                    source_observation_id=None,
                    index_url=index_url,
                    source_url=None,
                    reason=reason,
                )
                unsupported.append(filing.accession)

        records.append(record)

    return SecFpi6KResult(
        cik=normalized_cik,
        records=tuple(records),
        submissions_observation_ids=tuple(submissions_obs_ids),
        coverage_complete=not unsupported and not empty_accns,
        unsupported_accessions=tuple(unsupported),
        empty_accessions=tuple(empty_accns),
    )


__all__ = [
    "SecFpi6KExhibitContext",
    "SecFpi6KExhibitParser",
    "SecFpi6KMetricRow",
    "SecFpi6KParserRegistry",
    "SecFpi6KPluginParseOutput",
    "SecFpi6KQuarterRecord",
    "SecFpi6KResult",
    "SecFpi6KShareBasis",
    "SecFpi6KSourceKind",
    "SecTsm6KExhibitParser",
    "default_fpi_6k_registry",
    "fetch_sec_fpi_6k_quarters",
    "normalize_fpi_cik",
]

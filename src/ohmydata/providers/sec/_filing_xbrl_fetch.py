"""SEC filing index and XBRL instance acquisition with immutable snapshots."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from ...core import RequestSpec, SnapshotMode, SnapshotObservationRef, SnapshotStore
from ._filing_xbrl_parse import SecFilingXbrlFact, parse_sec_filing_xbrl_instance
from .errors import (
    CoverageError,
    PermanentProviderError,
    ResourceLimitError,
    SchemaMismatchError,
    TransientProviderError,
)
from .event_discovery import _strict_json
from .http import SecHttpClient, validate_sec_url

_MAX_BYTES = 8 * 1024**2
_INDEX_SERIALIZATION = "sec-filing-directory-json-v1"
_INSTANCE_SERIALIZATION = "sec-filing-document-bytes-v1"


@dataclass(frozen=True)
class SecFilingXbrlSource:
    facts: tuple[SecFilingXbrlFact, ...]
    index_observation: SnapshotObservationRef
    instance_observation: SnapshotObservationRef
    filing_url: str


def _exact_url(expected: str, actual: str) -> str:
    if validate_sec_url(actual) != validate_sec_url(expected):
        raise SchemaMismatchError("SEC filing XBRL redirect changed source URL")
    return actual


def _read_sec(client: SecHttpClient, url: str, max_bytes: int) -> bytes:
    try:
        response = client.open(
            url,
            accept="application/json"
            if url.endswith("index.json")
            else "application/xml,text/xml,*/*",
            max_bytes=max_bytes,
            redirect_validator=lambda actual: _exact_url(url, actual),
        )
    except PermanentProviderError as exc:
        if str(exc) == "SEC HTTP status 404":
            raise CoverageError("SEC filing XBRL source is unavailable") from exc
        raise
    try:
        if validate_sec_url(response.url) != validate_sec_url(url):
            raise SchemaMismatchError("SEC filing XBRL redirect changed source URL")
        try:
            body = response.body.read()
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise TransientProviderError("SEC filing XBRL response read failed") from exc
    finally:
        response.body.close()
    if type(body) is not bytes or len(body) > max_bytes:
        raise ResourceLimitError("SEC filing XBRL response exceeds byte limit")
    return body


def fetch_sec_filing_xbrl_source(
    *,
    client: SecHttpClient,
    store: SnapshotStore,
    cik: str,
    accession_number: str,
    form: str,
    report_date: date,
    filing_date: date,
    accepted_at: datetime,
    primary_document: str,
    utc_now: Callable[[], datetime],
) -> SecFilingXbrlSource:
    """Fetch and retain the exact filing index and unique XBRL instance source."""
    compact = accession_number.replace("-", "")
    directory_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{compact}"
    index_url = f"{directory_url}/index.json"
    index_body = _read_sec(client, index_url, 2 * 1024**2)
    root = _strict_json(index_body)
    directory = root.get("directory")
    expected_dir = f"/Archives/edgar/data/{int(cik)}/{compact}"
    if not isinstance(directory, dict) or directory.get("name") != expected_dir:
        raise SchemaMismatchError("SEC filing XBRL index identity mismatch")
    items = directory.get("item")
    if not isinstance(items, list) or not 1 <= len(items) <= 10_000:
        raise SchemaMismatchError("SEC filing XBRL index entries are missing or malformed")
    instance_candidates: list[str] = []
    filenames: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise SchemaMismatchError("SEC filing XBRL index entry is malformed")
        name = item.get("name")
        if type(name) is not str or not re.fullmatch(r"[A-Za-z0-9._-]{1,256}", name):
            raise SchemaMismatchError("SEC filing XBRL index filename is invalid")
        filenames.append(name)
    if len(filenames) != len(set(filenames)) or primary_document not in filenames:
        raise SchemaMismatchError("SEC filing XBRL index primary document is missing or duplicated")
    instance_candidates = [name for name in filenames if name.lower().endswith("_htm.xml")]
    if len(instance_candidates) != 1:
        raise SchemaMismatchError("SEC filing XBRL instance is missing or ambiguous")
    primary_stem, primary_suffix = (
        primary_document.rsplit(".", 1) if "." in primary_document else ("", "")
    )
    if (
        primary_suffix.lower() not in {"htm", "html"}
        or instance_candidates[0].lower() != f"{primary_stem}_htm.xml".lower()
    ):
        raise SchemaMismatchError(
            "SEC filing XBRL instance does not match submissions primary document"
        )
    index_observation = store.observe(
        RequestSpec(
            "sec", "company-filing-directory", {"cik": cik, "accession_number": accession_number}
        ),
        index_body,
        utc_now(),
        "sec-filing-directory-json-v1",
        SnapshotMode.APPEND,
    )
    if index_observation.snapshot_fetched_at < accepted_at:
        raise SchemaMismatchError("SEC filing XBRL index observation predates filing acceptance")
    instance_url = f"{directory_url}/{instance_candidates[0]}"
    instance_body = _read_sec(client, instance_url, _MAX_BYTES)
    instance_observation = store.observe(
        RequestSpec(
            "sec",
            "company-filing-document",
            {
                "cik": cik,
                "accession_number": accession_number,
                "filename": instance_candidates[0],
            },
        ),
        instance_body,
        utc_now(),
        _INSTANCE_SERIALIZATION,
        SnapshotMode.APPEND,
    )
    if instance_observation.snapshot_fetched_at < accepted_at:
        raise SchemaMismatchError("SEC filing XBRL instance observation predates filing acceptance")
    parsed = parse_sec_filing_xbrl_instance(
        instance_body,
        cik=cik,
        accession_number=accession_number,
        form=form,
        report_date=report_date,
        filing_date=filing_date,
        accepted_at=accepted_at,
    )
    return SecFilingXbrlSource(parsed.facts, index_observation, instance_observation, instance_url)


__all__ = ["SecFilingXbrlSource", "fetch_sec_filing_xbrl_source"]

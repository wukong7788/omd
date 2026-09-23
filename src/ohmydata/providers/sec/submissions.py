"""Retain a complete SEC Submissions root and its advertised history pages."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ...core import RequestSpec, SnapshotMode, SnapshotStore
from ._event_discovery_models import SecDiscoverySource
from .edgar import historical_basenames, historical_submission_url
from .errors import (
    PermanentProviderError,
    ResourceLimitError,
    SchemaMismatchError,
    TransientProviderError,
)
from .event_discovery import _strict_json, _submission_rows
from .http import SecHttpClient, validate_sec_url

_SOURCE_LIMIT = 8 * 1024**2
_TOTAL_LIMIT = 64 * 1024**2
_ROW_LIMIT = 100_000
_SERIALIZATION = "sec-submissions-json-v1"


@dataclass(frozen=True)
class SecSubmissionsClosure:
    """A retained root and every history page named by that exact root."""

    cik: str
    root_source: SecDiscoverySource
    historical_sources: tuple[SecDiscoverySource, ...]


def _exact_url(expected: str, actual: str) -> str:
    if validate_sec_url(actual) != validate_sec_url(expected):
        raise PermanentProviderError("SEC submissions redirect changed source URL")
    return actual


def _fetch(client: SecHttpClient, url: str, remaining: int) -> bytes:
    if remaining <= 0:
        raise ResourceLimitError("aggregate submissions bytes exceeded")
    response = client.open(
        url,
        accept="application/json",
        max_bytes=min(_SOURCE_LIMIT, remaining),
        redirect_validator=lambda actual: _exact_url(url, actual),
    )
    try:
        _exact_url(url, response.url)
        try:
            body: bytes = response.body.read()
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise TransientProviderError("SEC submissions response read failed") from exc
        if type(body) is not bytes or len(body) > min(_SOURCE_LIMIT, remaining):
            raise ResourceLimitError("submissions response bytes exceeded")
        declared_length = response.headers.get("Content-Length")
        if declared_length is not None and len(body) != int(declared_length):
            raise TransientProviderError("SEC submissions response length mismatch")
        return body
    finally:
        response.body.close()


def fetch_sec_submissions_closure(
    store: SnapshotStore,
    client: SecHttpClient,
    cik: str,
    *,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SecSubmissionsClosure:
    """Fetch and append immutable observations for one complete advertised closure.

    Each HTTP GET uses the injected SEC client's limiter and classified retries.
    Failed later pages leave earlier observations retained, but no closure is returned.
    A subsequent invocation fetches a fresh root and all pages it advertises.
    """
    if type(cik) is not str or re.fullmatch(r"[0-9]{10}", cik) is None:
        raise ValueError("CIK must be ten digits")
    if not isinstance(store, SnapshotStore) or not isinstance(client, SecHttpClient):
        raise TypeError("invalid submissions fetch dependencies")

    root_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    root_bytes = _fetch(client, root_url, _TOTAL_LIMIT)
    root = _strict_json(root_bytes)
    filings = root.get("filings")
    if not isinstance(filings, dict) or not isinstance(filings.get("files"), list):
        raise SchemaMismatchError("historical submissions references missing")
    names = historical_basenames(root, cik)
    observed = len(root_bytes)
    rows: dict[str, dict[str, object]] = {}
    row_count = 0

    def validate_rows(payload: dict[str, Any], *, child: bool) -> None:
        nonlocal row_count
        for row in _submission_rows(payload, cik, child=child):
            row_count += 1
            if row_count > _ROW_LIMIT:
                raise ResourceLimitError("aggregate filing row limit exceeded")
            accession = row["accessionNumber"]
            if (
                not isinstance(accession, str)
                or re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession) is None
            ):
                raise SchemaMismatchError("invalid accession")
            old = rows.setdefault(accession, row)
            if old != row:
                raise SchemaMismatchError("conflicting submission metadata")

    validate_rows(root, child=False)

    def retain(
        endpoint: str, parameters: dict[str, str], url: str, body: bytes
    ) -> SecDiscoverySource:
        fetched_at = utc_now()
        observation = store.observe(
            RequestSpec("sec", endpoint, parameters),
            body,
            fetched_at,
            _SERIALIZATION,
            SnapshotMode.APPEND,
        )
        return SecDiscoverySource(url, observation)

    root_source = retain("edgar_submissions", {"cik": cik}, root_url, root_bytes)
    children: list[SecDiscoverySource] = []
    for name in names:
        url = historical_submission_url(cik, name)
        body = _fetch(client, url, _TOTAL_LIMIT - observed)
        observed += len(body)
        child = _strict_json(body)
        validate_rows(child, child=True)
        children.append(
            retain("edgar_submissions_history", {"cik": cik, "basename": name}, url, body)
        )
    return SecSubmissionsClosure(cik, root_source, tuple(children))


__all__ = ["SecSubmissionsClosure", "fetch_sec_submissions_closure"]

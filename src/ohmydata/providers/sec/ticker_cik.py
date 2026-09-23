"""SEC's official ticker to Central Index Key mapping."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, cast

from ...core import RequestSpec, SnapshotMode, SnapshotObservationRef, SnapshotStore
from .errors import (
    CoverageError,
    PermanentProviderError,
    SchemaMismatchError,
    TransientProviderError,
)
from .http import SecHttpClient, validate_sec_url

SEC_TICKER_CIK_URL = "https://www.sec.gov/files/company_tickers.json"
_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,11}$")
_CIK = re.compile(r"^[0-9]{10}$")
_SERIALIZATION = "sec-company-tickers-json-v1"
_MAX_BYTES = 16 * 1024 * 1024


class SecTickerCikNotFoundError(CoverageError):
    """The official mapping contains no row for the requested ticker."""


class SecTickerCikAmbiguousError(PermanentProviderError):
    """The official mapping contains conflicting rows for a ticker."""


def _exact_url(expected: str, actual: str) -> str:
    if validate_sec_url(actual) != validate_sec_url(expected):
        raise PermanentProviderError("SEC ticker mapping redirect changed source URL")
    return actual


@dataclass(frozen=True)
class SecTickerCikResolution:
    """A validated ticker identity with the retained SEC source observation."""

    ticker: str
    cik: str
    title: str
    source_url: str
    observation: SnapshotObservationRef


@dataclass(frozen=True)
class SecTickerCikMapping:
    """One retained SEC mapping, reusable for many ticker resolutions."""

    entries: Mapping[str, tuple[str, str] | None]
    source_url: str
    observation: SnapshotObservationRef

    def resolve(self, ticker: str) -> SecTickerCikResolution:
        canonical = _canonical_ticker(ticker)
        result = self.entries.get(canonical)
        if result is None:
            if canonical in self.entries:
                raise SecTickerCikAmbiguousError(f"SEC ticker maps to multiple CIKs: {canonical}")
            raise SecTickerCikNotFoundError(f"SEC ticker not found: {canonical}")
        return SecTickerCikResolution(
            canonical, result[0], result[1], self.source_url, self.observation
        )


def _canonical_ticker(ticker: str) -> str:
    if type(ticker) is not str:
        raise TypeError("ticker must be a string")
    value = ticker.strip().upper()
    if not _TICKER.fullmatch(value):
        raise ValueError("invalid SEC ticker")
    return value


def _strict_json(body: bytes) -> dict[str, Any]:
    if type(body) is not bytes:
        raise SchemaMismatchError("SEC ticker mapping body must be bytes")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SchemaMismatchError("duplicate key in SEC ticker mapping")
            result[key] = value
        return result

    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SchemaMismatchError("invalid SEC ticker mapping JSON") from exc
    if type(value) is not dict:
        raise SchemaMismatchError("SEC ticker mapping must be an object")
    return value


def _parse_mapping(payload: dict[str, Any]) -> dict[str, tuple[str, str] | None]:
    mapping: dict[str, tuple[str, str] | None] = {}
    for index, raw_row in payload.items():
        if not isinstance(index, str) or not index.isdigit():
            raise SchemaMismatchError("SEC ticker mapping index is invalid")
        if type(raw_row) is not dict:
            raise SchemaMismatchError("SEC ticker mapping row is invalid")
        row = cast(dict[str, Any], raw_row)
        if type(row.get("cik_str")) is not int or not 1 <= row["cik_str"] <= 9999999999:
            raise SchemaMismatchError("SEC ticker mapping CIK is invalid")
        raw_ticker, title = row.get("ticker"), row.get("title")
        if type(raw_ticker) is not str or not _TICKER.fullmatch(raw_ticker.strip().upper()):
            raise SchemaMismatchError("SEC ticker mapping ticker is invalid")
        if type(title) is not str or not title.strip():
            raise SchemaMismatchError("SEC ticker mapping title is invalid")
        ticker = raw_ticker.strip().upper()
        cik = f"{row['cik_str']:010d}"
        previous = mapping.get(ticker)
        current = (cik, title.strip())
        if previous is not None and previous != current:
            mapping[ticker] = None
        elif ticker not in mapping:
            mapping[ticker] = current
    return mapping


def resolve_sec_ticker_cik(payload: bytes | dict[str, Any], ticker: str) -> tuple[str, str]:
    """Resolve one ticker from an already fetched official mapping payload.

    The returned tuple is ``(cik, title)``.  This pure helper is intended for
    deterministic replay and tests; :func:`fetch_sec_ticker_cik` retains the
    provider response and returns the source observation as well.
    """
    canonical = _canonical_ticker(ticker)
    parsed = _strict_json(payload) if type(payload) is bytes else payload
    if type(parsed) is not dict:
        raise SchemaMismatchError("SEC ticker mapping must be an object")
    entries = _parse_mapping(parsed)
    result = entries.get(canonical)
    if result is None:
        if canonical in entries:
            raise SecTickerCikAmbiguousError(f"SEC ticker maps to multiple CIKs: {canonical}")
        raise SecTickerCikNotFoundError(f"SEC ticker not found: {canonical}")
    return result


def fetch_sec_ticker_cik(
    client: SecHttpClient,
    store: SnapshotStore,
    ticker: str,
    *,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SecTickerCikResolution:
    """Fetch, retain, validate, and resolve one ticker through SEC's mapping."""
    canonical = _canonical_ticker(ticker)
    return fetch_sec_ticker_cik_mapping(client, store, utc_now=utc_now).resolve(canonical)


def fetch_sec_ticker_cik_mapping(
    client: SecHttpClient,
    store: SnapshotStore,
    *,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SecTickerCikMapping:
    """Fetch and retain the official mapping once for batch ticker lookups."""
    if not isinstance(client, SecHttpClient) or not isinstance(store, SnapshotStore):
        raise TypeError("invalid SEC ticker mapping dependencies")
    if not callable(utc_now):
        raise TypeError("utc_now must be callable")
    validate_sec_url(SEC_TICKER_CIK_URL)
    url = SEC_TICKER_CIK_URL
    response = client.open(
        url,
        accept="application/json",
        max_bytes=_MAX_BYTES,
        redirect_validator=lambda actual: _exact_url(url, actual),
    )
    try:
        if validate_sec_url(response.url) != validate_sec_url(url):
            raise PermanentProviderError("SEC ticker mapping redirect changed source URL")
        try:
            body = response.body.read()
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise TransientProviderError("SEC ticker mapping response read failed") from exc
        if type(body) is not bytes or len(body) > _MAX_BYTES:
            raise SchemaMismatchError("SEC ticker mapping response is too large")
        entries = _parse_mapping(_strict_json(body))
        observation = store.observe(
            RequestSpec("sec", "ticker_cik", {}),
            body,
            utc_now(),
            _SERIALIZATION,
            SnapshotMode.APPEND,
        )
        return SecTickerCikMapping(MappingProxyType(entries), url, observation)
    finally:
        response.body.close()


__all__ = [
    "SEC_TICKER_CIK_URL",
    "SecTickerCikAmbiguousError",
    "SecTickerCikMapping",
    "SecTickerCikNotFoundError",
    "SecTickerCikResolution",
    "fetch_sec_ticker_cik",
    "fetch_sec_ticker_cik_mapping",
    "resolve_sec_ticker_cik",
]

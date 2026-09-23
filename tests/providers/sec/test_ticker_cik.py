from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from email.message import Message
from urllib.request import Request

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.providers.sec import (
    SecHttpClient,
    SecTickerCikAmbiguousError,
    SecTickerCikNotFoundError,
    fetch_sec_ticker_cik,
    fetch_sec_ticker_cik_mapping,
    resolve_sec_ticker_cik,
)
from ohmydata.providers.sec.errors import PermanentProviderError, SchemaMismatchError

MAPPING_URL = "https://www.sec.gov/files/company_tickers.json"


def _payload(*rows: dict[str, object]) -> bytes:
    return json.dumps({str(i): row for i, row in enumerate(rows)}).encode()


class _Response:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def close(self) -> None:
        self._body.close()


class _RedirectResponse(_Response):
    status = 302

    def __init__(self, location: str) -> None:
        super().__init__(b"")
        self.headers["Location"] = location


class _Opener:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[str] = []

    def open(self, request: Request, timeout: float = 0) -> _Response:
        self.calls.append(request.full_url)
        return _Response(self.body)


class _RedirectOpener:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def open(self, request: Request, timeout: float = 0) -> _Response:
        self.calls.append(request.full_url)
        return _RedirectResponse("https://data.sec.gov/files/company_tickers.json")


def _client(body: bytes) -> tuple[SecHttpClient, _Opener]:
    opener = _Opener(body)
    return (
        SecHttpClient(
            "synthetic-test contact@example.invalid",
            opener=opener,
            clock=lambda: 0.0,
            sleep=lambda _: None,
        ),
        opener,
    )


def test_resolves_case_insensitive_ticker_and_zero_pads_cik() -> None:
    body = _payload({"cik_str": 320193, "ticker": "aapl", "title": " Apple Inc. "})

    assert resolve_sec_ticker_cik(body, " aapl ") == ("0000320193", "Apple Inc.")


def test_missing_ticker_is_typed_coverage_error() -> None:
    with pytest.raises(SecTickerCikNotFoundError, match="MSFT"):
        resolve_sec_ticker_cik(
            _payload({"cik_str": 320193, "ticker": "AAPL", "title": "Apple"}), "MSFT"
        )


def test_conflicting_duplicate_ticker_is_rejected() -> None:
    body = _payload(
        {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"},
        {"cik_str": 789019, "ticker": "aapl", "title": "Other"},
    )

    with pytest.raises(SecTickerCikAmbiguousError, match="AAPL"):
        resolve_sec_ticker_cik(body, "AAPL")


def test_conflicting_ticker_does_not_block_unrelated_resolution() -> None:
    body = _payload(
        {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"},
        {"cik_str": 789019, "ticker": "aapl", "title": "Other"},
        {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet"},
    )

    assert resolve_sec_ticker_cik(body, "GOOG") == ("0001652044", "Alphabet")


def test_mapping_snapshot_retains_ambiguity_without_blocking_other_tickers(tmp_path) -> None:
    body = _payload(
        {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"},
        {"cik_str": 789019, "ticker": "aapl", "title": "Other"},
        {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet"},
    )
    client, _ = _client(body)
    mapping = fetch_sec_ticker_cik_mapping(client, SnapshotStore(tmp_path))

    assert mapping.resolve("GOOG").cik == "0001652044"
    with pytest.raises(SecTickerCikAmbiguousError):
        mapping.resolve("AAPL")


def test_redirect_target_is_rejected_before_second_request(tmp_path) -> None:
    opener = _RedirectOpener()
    client = SecHttpClient("synthetic-test contact@example.invalid", opener=opener)

    with pytest.raises(PermanentProviderError, match="redirect changed source URL"):
        fetch_sec_ticker_cik_mapping(client, SnapshotStore(tmp_path))
    assert len(opener.calls) == 1


def test_malformed_official_row_fails_closed() -> None:
    body = _payload({"cik_str": "320193", "ticker": "AAPL", "title": "Apple"})

    with pytest.raises(SchemaMismatchError, match="CIK"):
        resolve_sec_ticker_cik(body, "AAPL")


def test_fetch_retains_mapping_and_returns_provenance(tmp_path) -> None:
    body = _payload({"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."})
    client, opener = _client(body)

    result = fetch_sec_ticker_cik(
        client,
        SnapshotStore(tmp_path),
        "AAPL",
        utc_now=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    )

    assert result.ticker == "AAPL"
    assert result.cik == "0000320193"
    assert result.title == "Apple Inc."
    assert result.source_url == MAPPING_URL
    assert result.observation.endpoint == "ticker_cik"
    assert opener.calls == ["https://www.sec.gov:443/files/company_tickers.json"]


def test_mapping_fetch_is_reusable_for_batch_lookups(tmp_path) -> None:
    body = _payload(
        {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp."},
    )
    client, opener = _client(body)

    mapping = fetch_sec_ticker_cik_mapping(
        client,
        SnapshotStore(tmp_path),
        utc_now=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    )

    assert mapping.resolve("aapl").cik == "0000320193"
    assert mapping.resolve("MSFT").cik == "0000789019"
    assert opener.calls == ["https://www.sec.gov:443/files/company_tickers.json"]

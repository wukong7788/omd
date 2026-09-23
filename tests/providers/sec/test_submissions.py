from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from email.message import Message
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from ohmydata.core import RequestSpec, SnapshotStore
from ohmydata.providers.sec import (
    SecDiscoveryMode,
    SecDiscoveryPolicy,
    SecHttpClient,
    discover_sec_filing_events,
    fetch_sec_submissions_closure,
)
from ohmydata.providers.sec.errors import (
    PermanentProviderError,
    SchemaMismatchError,
    TransientProviderError,
)

CIK = "0000000001"
ROOT = f"https://data.sec.gov/submissions/CIK{CIK}.json"
NAME = f"CIK{CIK}-submissions-001.json"
HISTORY = f"https://data.sec.gov/submissions/{NAME}"
AT = datetime(2025, 1, 1, tzinfo=UTC)


def _row(accession: str, form: str = "10-Q", items: str | None = None) -> dict:
    row = {
        "accessionNumber": [accession],
        "form": [form],
        "filingDate": ["2024-05-01"],
        "reportDate": ["2024-03-31"],
        "primaryDocument": ["filing.htm"],
        "acceptanceDateTime": ["2024-05-01T12:00:00Z"],
    }
    if items is not None:
        row["items"] = [items]
    return row


def _payloads() -> dict[str, bytes]:
    return {
        ROOT: json.dumps(
            {
                "cik": 1,
                "filings": {
                    "recent": _row("0000000001-24-000001", "8-K", "2.02,9.01"),
                    "files": [{"name": NAME}],
                },
            }
        ).encode(),
        HISTORY: json.dumps({"cik": 1, **_row("0000000001-23-000002")}).encode(),
    }


class _Response:
    status = 200

    def __init__(self, data: bytes) -> None:
        self.body = io.BytesIO(data)
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self.headers["Content-Length"] = str(len(data))

    def read(self, size: int = -1) -> bytes:
        return self.body.read(size)

    def close(self) -> None:
        self.body.close()


class _Opener:
    def __init__(self, payloads: dict[str, bytes], *, fail_once: str | None = None) -> None:
        self.payloads = payloads
        self.fail_once = fail_once
        self.calls: list[str] = []

    def open(self, request: Request, timeout: float = 0) -> _Response:
        url = request.full_url.replace("data.sec.gov:443", "data.sec.gov")
        self.calls.append(url)
        if url == self.fail_once:
            self.fail_once = None
            raise HTTPError(request.full_url, 503, "unavailable", Message(), None)
        if url not in self.payloads:
            raise HTTPError(request.full_url, 404, "missing", Message(), None)
        return _Response(self.payloads[url])


def _client(opener: _Opener, *, attempts: int = 3) -> SecHttpClient:
    tick = iter(i * 0.3 for i in range(100))
    return SecHttpClient(
        "synthetic-test contact@example.invalid",
        opener=opener,
        clock=lambda: next(tick),
        sleep=lambda _: None,
        max_attempts=attempts,
    )


def _policy() -> SecDiscoveryPolicy:
    return SecDiscoveryPolicy(
        "1",
        ("10-Q", "8-K"),
        datetime(2023, 1, 1, tzinfo=UTC),
        datetime(2024, 12, 31, tzinfo=UTC),
        timedelta(days=1),
        SecDiscoveryMode.RECONCILE,
        "synthetic-v1",
    )


def test_fetches_complete_closure_and_discovers_events(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    opener = _Opener(_payloads(), fail_once=HISTORY)
    closure = fetch_sec_submissions_closure(store, _client(opener), CIK, utc_now=lambda: AT)
    assert opener.calls == [ROOT, HISTORY, HISTORY]
    assert closure.root_source.observation.endpoint == "edgar_submissions"
    assert closure.historical_sources[0].observation.endpoint == "edgar_submissions_history"
    assert store.replay_observation(closure.root_source.observation).payload == _payloads()[ROOT]
    batch = discover_sec_filing_events(
        store,
        closure.root_source,
        closure.historical_sources,
        policy=_policy(),
        prior_cursor=None,
    )
    assert len(batch.events) == 3
    assert {event.kind.value for event in batch.events} == {
        "SEC_FILING_ACCEPTED",
        "SEC_8K_ITEM_2_02",
    }
    again = fetch_sec_submissions_closure(
        store, _client(_Opener(_payloads())), CIK, utc_now=lambda: AT
    )
    assert again == closure


def test_history_failure_returns_no_closure_but_retains_root(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    opener = _Opener(_payloads(), fail_once=HISTORY)
    with pytest.raises(TransientProviderError):
        fetch_sec_submissions_closure(store, _client(opener, attempts=1), CIK, utc_now=lambda: AT)
    root_spec = RequestSpec("sec", "edgar_submissions", {"cik": CIK})
    assert (tmp_path / "sec" / "edgar_submissions" / root_spec.request_identity).exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_page",
        "duplicate_key",
        "bad_columns",
        "conflicting_accession",
        "unsafe_history_name",
    ],
)
def test_fails_closed_on_invalid_closure(tmp_path, mutation: str) -> None:
    payloads = _payloads()
    if mutation == "missing_page":
        del payloads[HISTORY]
    elif mutation == "duplicate_key":
        payloads[ROOT] = b'{"cik":1,"cik":1}'
    elif mutation == "bad_columns":
        child = json.loads(payloads[HISTORY])
        child["form"] = []
        payloads[HISTORY] = json.dumps(child).encode()
    elif mutation == "conflicting_accession":
        child = json.loads(payloads[HISTORY])
        child["accessionNumber"] = ["0000000001-24-000001"]
        payloads[HISTORY] = json.dumps(child).encode()
    else:
        root = json.loads(payloads[ROOT])
        root["filings"]["files"][0]["name"] = "../evil.json"
        payloads[ROOT] = json.dumps(root).encode()
    expected = PermanentProviderError if mutation == "missing_page" else SchemaMismatchError
    with pytest.raises(expected):
        fetch_sec_submissions_closure(
            SnapshotStore(tmp_path), _client(_Opener(payloads)), CIK, utc_now=lambda: AT
        )


def test_rejects_invalid_cik_without_network(tmp_path) -> None:
    opener = _Opener(_payloads())
    with pytest.raises(ValueError):
        fetch_sec_submissions_closure(SnapshotStore(tmp_path), _client(opener), "1")
    assert opener.calls == []


def test_empty_advertised_history_is_complete(tmp_path) -> None:
    payloads = _payloads()
    root = json.loads(payloads[ROOT])
    root["filings"]["files"] = []
    payloads[ROOT] = json.dumps(root).encode()
    closure = fetch_sec_submissions_closure(
        SnapshotStore(tmp_path), _client(_Opener(payloads)), CIK, utc_now=lambda: AT
    )
    assert closure.historical_sources == ()


def test_response_read_failure_is_classified_transient(tmp_path) -> None:
    class BrokenResponse(_Response):
        def read(self, size: int = -1) -> bytes:
            raise TimeoutError("synthetic read timeout")

    class BrokenOpener(_Opener):
        def open(self, request: Request, timeout: float = 0) -> _Response:
            return BrokenResponse(b"{}")

    with pytest.raises(TransientProviderError, match="response read failed"):
        fetch_sec_submissions_closure(
            SnapshotStore(tmp_path), _client(BrokenOpener(_payloads())), CIK, utc_now=lambda: AT
        )


def test_rejects_short_successful_json_response(tmp_path) -> None:
    class ShortResponse(_Response):
        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self.headers.replace_header("Content-Length", str(len(data) + 1))

    class ShortOpener(_Opener):
        def open(self, request: Request, timeout: float = 0) -> _Response:
            return ShortResponse(self.payloads[ROOT])

    with pytest.raises(TransientProviderError, match="length mismatch"):
        fetch_sec_submissions_closure(
            SnapshotStore(tmp_path), _client(ShortOpener(_payloads())), CIK, utc_now=lambda: AT
        )
    assert not (tmp_path / "sec").exists()


def test_rejects_non_sec_source_redirect(tmp_path) -> None:
    class RedirectOpener(_Opener):
        def open(self, request: Request, timeout: float = 0) -> _Response:
            response = _Response(b"")
            response.status = 302
            response.headers["Location"] = "https://data.sec.gov/submissions/other.json"
            return response

    with pytest.raises(PermanentProviderError):
        fetch_sec_submissions_closure(
            SnapshotStore(tmp_path), _client(RedirectOpener(_payloads())), CIK, utc_now=lambda: AT
        )

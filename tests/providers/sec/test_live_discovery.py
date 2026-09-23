import io
import json
from datetime import UTC, datetime, timedelta
from email.message import Message
from types import SimpleNamespace

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.providers.sec import (
    SecDiscoveryCursor,
    SecDiscoveryMode,
    SecDiscoveryPolicy,
    SecEventDiscoveryLedger,
    SecHttpClient,
    fetch_sec_discovery_batch,
)
from ohmydata.providers.sec.errors import SchemaMismatchError, TransientProviderError


def _policy(prior=None):
    return SecDiscoveryPolicy(
        "1",
        ("10-Q", "10-Q/A", "10-K", "10-K/A", "8-K", "8-K/A"),
        datetime(2024, 1, 1, tzinfo=UTC)
        if prior is None
        else prior.acceptance_at - timedelta(days=2),
        datetime(2024, 12, 31, tzinfo=UTC),
        timedelta(days=2),
        SecDiscoveryMode.INCREMENTAL,
        "test-v1",
    )


def _rows(accessions, forms, *, items=None):
    n = len(accessions)
    return {
        "accessionNumber": accessions,
        "form": forms,
        "filingDate": ["2024-05-01"] * n,
        "reportDate": ["2024-03-31"] * n,
        "primaryDocument": ["filing.htm"] * n,
        "acceptanceDateTime": ["2024-05-01T12:00:00Z"] * n,
        "items": items if items is not None else [None] * n,
    }


class FakeClient(SecHttpClient):
    def __init__(self, pages):
        super().__init__("Synthetic Test contact@example.invalid")
        self.pages = pages
        self.calls = []

    def open(self, url, *, accept, max_bytes=None, **kwargs):
        self.calls.append((url, accept, max_bytes))
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        body = value if isinstance(value, io.BytesIO) else io.BytesIO(json.dumps(value).encode())
        return SimpleNamespace(url=url, body=body, headers=Message())


def test_fetches_complete_closure_and_appends_cursor(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    root_url = "https://data.sec.gov/submissions/CIK0000000001.json"
    name = "CIK0000000001-submissions-001.json"
    child_url = f"https://data.sec.gov/submissions/{name}"
    client = FakeClient(
        {
            root_url: {
                "cik": "0000000001",
                "filings": {
                    "recent": _rows(
                        ["0000000001-24-000001", "0000000001-24-000002"],
                        ["10-Q", "8-K"],
                        items=[None, "2.02,9.01"],
                    ),
                    "files": [{"name": name}],
                },
            },
            child_url: {
                "cik": "0000000001",
                **_rows(["0000000001-24-000003"], ["10-Q/A"]),
            },
        }
    )
    batch = fetch_sec_discovery_batch(
        client,
        store,
        policy=_policy(),
        prior_cursor=None,
        clock=lambda: datetime(2024, 5, 2, tzinfo=UTC),
    )
    assert len(batch.events) == 4
    assert {event.form for event in batch.events} == {"10-Q", "10-Q/A", "8-K"}
    assert sum(event.kind.value == "SEC_8K_ITEM_2_02" for event in batch.events) == 1
    assert len(batch.sources) == 2
    assert [call[0] for call in client.calls] == [root_url, child_url]
    assert all(call[2] <= 8 * 1024**2 for call in client.calls)
    ledger = SecEventDiscoveryLedger(tmp_path / "ledger", store=store)
    receipt = ledger.append(batch, expected_receipt_id=None)
    assert receipt.cursor == batch.candidate_cursor
    assert len(ledger.load()[1]) == 4


def test_failed_history_does_not_advance_ledger(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    root_url = "https://data.sec.gov/submissions/CIK0000000001.json"
    name = "CIK0000000001-submissions-001.json"
    client = FakeClient(
        {
            root_url: {
                "cik": "0000000001",
                "filings": {"recent": _rows([], []), "files": [{"name": name}]},
            },
            f"https://data.sec.gov/submissions/{name}": TransientProviderError("offline"),
        }
    )
    with pytest.raises(TransientProviderError):
        fetch_sec_discovery_batch(
            client,
            store,
            policy=_policy(),
            prior_cursor=None,
            clock=lambda: datetime(2024, 5, 2, tzinfo=UTC),
        )
    assert SecEventDiscoveryLedger(tmp_path / "ledger", store=store).load() == (None, ())


def test_malformed_root_and_invalid_window_fail_before_append(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    root_url = "https://data.sec.gov/submissions/CIK0000000001.json"
    client = FakeClient({root_url: {"cik": "0000000001", "filings": {"recent": _rows([], [])}}})
    with pytest.raises(SchemaMismatchError):
        fetch_sec_discovery_batch(
            client,
            store,
            policy=_policy(),
            prior_cursor=None,
            clock=lambda: datetime(2024, 5, 2, tzinfo=UTC),
        )
    prior = SecDiscoveryCursor(datetime(2024, 1, 2, tzinfo=UTC), "0000000001-24-000001")
    with pytest.raises(ValueError, match="overlap"):
        fetch_sec_discovery_batch(
            client,
            store,
            policy=_policy(),
            prior_cursor=prior,
            clock=lambda: datetime(2024, 5, 2, tzinfo=UTC),
        )
    assert len(client.calls) == 1


def test_body_read_failure_is_transient(tmp_path):
    class BrokenBody(io.BytesIO):
        def read(self, size=-1):
            raise OSError("synthetic socket error")

    root_url = "https://data.sec.gov/submissions/CIK0000000001.json"
    client = FakeClient({root_url: BrokenBody()})
    with pytest.raises(TransientProviderError, match="response read"):
        fetch_sec_discovery_batch(
            client,
            SnapshotStore(tmp_path),
            policy=_policy(),
            prior_cursor=None,
            clock=lambda: datetime(2024, 5, 2, tzinfo=UTC),
        )

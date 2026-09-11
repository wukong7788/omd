"""Independent regressions for retained-source closure and aggregate budgets."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from ohmydata.core import RequestSpec, SnapshotStore
from ohmydata.core.errors import ResourceLimitError, SchemaMismatchError, SnapshotIntegrityError
from ohmydata.providers.sec import event_discovery as discovery

_URL = "https://data.sec.gov/submissions/CIK0000000001.json"
_AT = datetime(2025, 1, 1, tzinfo=UTC)


def policy():
    return discovery.SecDiscoveryPolicy(
        "1",
        ("10-Q",),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 12, 31, tzinfo=UTC),
        timedelta(days=1),
        discovery.SecDiscoveryMode.INCREMENTAL,
        "synthetic-review-v1",
    )


def row(*, empty=False):
    values = {
        "accessionNumber": "0000000001-24-000001",
        "form": "10-Q",
        "filingDate": "2024-05-01",
        "reportDate": "2024-03-31",
        "primaryDocument": "synthetic.htm",
        "acceptanceDateTime": "2024-05-01T12:00:00Z",
    }
    return {key: [] if empty else [value] for key, value in values.items()}


def source(store, payload, *, name=None):
    endpoint = "edgar_submissions" if name is None else "edgar_submissions_history"
    params = {"cik": "0000000001"} if name is None else {"cik": "0000000001", "basename": name}
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    observation = store.observe(RequestSpec("sec", endpoint, params), raw, _AT, "json-v1")
    url = _URL if name is None else f"https://data.sec.gov/submissions/{name}"
    return discovery.SecDiscoverySource(url, observation), len(raw)


def test_explicit_empty_closure_is_valid_but_missing_page_list_is_not(tmp_path):
    store = SnapshotStore(tmp_path)
    complete, _ = source(store, {"cik": "1", "filings": {"recent": row(empty=True), "files": []}})
    result = discovery.discover_sec_filing_events(
        store,
        complete,
        (),
        policy=policy(),
        prior_cursor=None,
    )
    assert result.events == () and result.candidate_cursor is None
    absent, _ = source(store, {"cik": "1", "filings": {"recent": row(empty=True)}})
    with pytest.raises(SchemaMismatchError, match="references"):
        discovery.discover_sec_filing_events(store, absent, (), policy=policy(), prior_cursor=None)


def test_duplicate_rows_still_consume_aggregate_row_budget(tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path)
    name = "CIK0000000001-submissions-001.json"
    root, _ = source(store, {"cik": "1", "filings": {"recent": row(), "files": [{"name": name}]}})
    child, _ = source(store, {"cik": "1", **row()}, name=name)
    monkeypatch.setattr(discovery, "_MAX_ROWS", 2)
    result = discovery.discover_sec_filing_events(
        store,
        root,
        (child,),
        policy=policy(),
        prior_cursor=None,
    )
    assert len(result.events) == 1
    monkeypatch.setattr(discovery, "_MAX_ROWS", 1)
    with pytest.raises(ResourceLimitError, match="aggregate filing"):
        discovery.discover_sec_filing_events(
            store, root, (child,), policy=policy(), prior_cursor=None
        )


def test_remaining_aggregate_bytes_limit_the_next_replay_before_reading(tmp_path):
    class RecordingStore(SnapshotStore):
        def __init__(self, root):
            super().__init__(root)
            self.limits = []

        def replay_observation(self, *args, **kwargs):
            self.limits.append(kwargs.get("max_payload_bytes"))
            return super().replay_observation(*args, **kwargs)

    store = RecordingStore(tmp_path)
    name = "CIK0000000001-submissions-001.json"
    root, root_size = source(
        store, {"cik": "1", "filings": {"recent": row(), "files": [{"name": name}]}}
    )
    child, child_size = source(store, {"cik": "1", **row()}, name=name)
    cache = discovery._DiscoveryReplayCache(store, max_total_bytes=root_size + child_size - 1)
    with pytest.raises(SnapshotIntegrityError, match="limit"):
        discovery.discover_sec_filing_events(
            store,
            root,
            (child,),
            policy=policy(),
            prior_cursor=None,
            _replay_cache=cache,
        )
    assert store.limits[-1] == child_size - 1


@pytest.mark.parametrize(
    "raw,error",
    [
        (b'{"extra":' + b"[" * 65 + b"0" + b"]" * 65 + b"}", ResourceLimitError),
        (b'{"extra":NaN}', SchemaMismatchError),
        (b'{"extra":Infinity}', SchemaMismatchError),
    ],
)
def test_untrusted_source_json_fails_before_discovery(tmp_path, raw, error):
    store = SnapshotStore(tmp_path)
    root, _ = source(store, raw)
    with pytest.raises(error):
        discovery.discover_sec_filing_events(store, root, (), policy=policy(), prior_cursor=None)

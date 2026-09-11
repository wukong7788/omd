import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ohmydata.core import RequestSpec, SnapshotMode, SnapshotStore
from ohmydata.core.errors import (
    CoverageError,
    ResourceLimitError,
    SchemaMismatchError,
    SnapshotIntegrityError,
)
from ohmydata.providers.sec.event_discovery import (
    SecDiscoveryMode,
    SecDiscoveryPolicy,
    SecDiscoverySource,
    canonical_batch_bytes,
    discover_sec_filing_events,
)


def _policy() -> SecDiscoveryPolicy:
    return SecDiscoveryPolicy(
        "1",
        ("8-K", "10-Q"),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 12, 31, tzinfo=UTC),
        timedelta(days=2),
        SecDiscoveryMode.INCREMENTAL,
        "synthetic-v1",
    )


def _source(store: SnapshotStore, endpoint: str, params: dict[str, object], url: str, body: dict):
    observation = store.observe(
        RequestSpec("sec", endpoint, params),
        json.dumps(body).encode(),
        datetime(2025, 1, 1, tzinfo=UTC),
        "json-v1",
        SnapshotMode.APPEND,
    )
    return SecDiscoverySource(url, observation)


def _row(accession: str, form: str = "8-K", items: str | None = "2.02,9.01") -> dict:
    values = {
        "accessionNumber": [accession],
        "form": [form],
        "filingDate": ["2024-05-01"],
        "reportDate": ["2024-03-31"],
        "primaryDocument": ["x.htm"],
        "acceptanceDateTime": ["2024-05-01T12:00:00Z"],
    }
    if items is not None:
        values["items"] = [items]
    return values


def test_discovers_complete_snapshot_closure_and_rebuilds(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    name = "CIK0000000001-submissions-001.json"
    root = _source(
        store,
        "edgar_submissions",
        {"cik": "0000000001", "required_accessions": ["0000000001-24-000001"]},
        "https://data.sec.gov/submissions/CIK0000000001.json",
        {
            "cik": "0000000001",
            "filings": {"recent": _row("0000000001-24-000001"), "files": [{"name": name}]},
        },
    )
    child = _source(
        store,
        "edgar_submissions_history",
        {"cik": "0000000001", "basename": name},
        f"https://data.sec.gov/submissions/{name}",
        {"cik": "0000000001", **_row("0000000001-24-000002", "10-Q", None)},
    )
    batch = discover_sec_filing_events(store, root, (child,), policy=_policy(), prior_cursor=None)
    assert sorted(event.kind.value for event in batch.events) == [
        "SEC_8K_ITEM_2_02",
        "SEC_FILING_ACCEPTED",
        "SEC_FILING_ACCEPTED",
    ]
    assert batch.candidate_cursor is not None
    assert batch.candidate_cursor.accession == "0000000001-24-000002"
    assert type(batch).from_canonical_payload(batch.canonical_payload(), store) == batch
    assert canonical_batch_bytes(batch)


def test_fails_closed_for_missing_history_and_naive_acceptance(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    name = "CIK0000000001-submissions-001.json"
    root = _source(
        store,
        "edgar_submissions",
        {"cik": "0000000001", "required_accessions": ["0000000001-24-000001"]},
        "https://data.sec.gov/submissions/CIK0000000001.json",
        {
            "cik": "0000000001",
            "filings": {"recent": _row("0000000001-24-000001"), "files": [{"name": name}]},
        },
    )
    with pytest.raises(CoverageError):
        discover_sec_filing_events(store, root, (), policy=_policy(), prior_cursor=None)
    no_tz = _source(
        store,
        "edgar_submissions",
        {"cik": "0000000001", "required_accessions": ["0000000001-24-000003"]},
        "https://data.sec.gov/submissions/CIK0000000001.json",
        {
            "cik": "0000000001",
            "filings": {
                "recent": {
                    **_row("0000000001-24-000003"),
                    "acceptanceDateTime": ["2024-05-01T12:00:00"],
                },
                "files": [],
            },
        },
    )
    with pytest.raises(SchemaMismatchError):
        discover_sec_filing_events(store, no_tz, (), policy=_policy(), prior_cursor=None)


@pytest.mark.parametrize("items, expected", [("2.02", 2), ("2.020", 1), (" 2.02", 0), (None, 1)])
def test_8k_items_are_exact_tokens_and_missing_is_unknown(tmp_path, items, expected) -> None:
    store = SnapshotStore(tmp_path)
    root = _source(
        store,
        "edgar_submissions",
        {"cik": "0000000001", "required_accessions": ["0000000001-24-000001"]},
        "https://data.sec.gov/submissions/CIK0000000001.json",
        {
            "cik": "0000000001",
            "filings": {"recent": _row("0000000001-24-000001", items=items), "files": []},
        },
    )
    if items == " 2.02":
        with pytest.raises(SchemaMismatchError):
            discover_sec_filing_events(store, root, (), policy=_policy(), prior_cursor=None)
    else:
        assert (
            len(
                discover_sec_filing_events(
                    store, root, (), policy=_policy(), prior_cursor=None
                ).events
            )
            == expected
        )


def test_duplicate_conflict_and_cursor_keeps_late_overlap_events(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    rows = _row("0000000001-24-000001")
    rows["accessionNumber"].append("0000000001-24-000002")
    rows["form"].append("8-K/A")
    rows["filingDate"].append("2024-05-01")
    rows["reportDate"].append("2024-03-31")
    rows["primaryDocument"].append("amend.htm")
    rows["acceptanceDateTime"].append("2024-05-01T12:00:00Z")
    rows["items"].append("9.01")
    root = _source(
        store,
        "edgar_submissions",
        {"cik": "0000000001", "required_accessions": ["0000000001-24-000001"]},
        "https://data.sec.gov/submissions/CIK0000000001.json",
        {"cik": "0000000001", "filings": {"recent": rows, "files": []}},
    )
    prior = __import__(
        "ohmydata.providers.sec.event_discovery", fromlist=["SecDiscoveryCursor"]
    ).SecDiscoveryCursor(datetime(2024, 6, 1, tzinfo=UTC), "0000000001-24-999999")
    batch = discover_sec_filing_events(
        store, root, (), policy=replace(_policy(), forms=("8-K", "8-K/A")), prior_cursor=prior
    )
    assert len(batch.events) == 3 and batch.candidate_cursor == prior
    conflict = _source(
        store,
        "edgar_submissions",
        {"cik": "0000000001", "required_accessions": ["0000000001-24-000001"]},
        "https://data.sec.gov/submissions/CIK0000000001.json",
        {
            "cik": "0000000001",
            "filings": {
                "recent": {**rows, "primaryDocument": ["changed.htm", "amend.htm"]},
                "files": [],
            },
        },
    )
    # A duplicated accession across complete retained pages with divergent metadata fails.
    name = "CIK0000000001-submissions-001.json"
    root_with_history = _source(
        store,
        "edgar_submissions",
        {"cik": "0000000001", "required_accessions": ["0000000001-24-000001"]},
        "https://data.sec.gov/submissions/CIK0000000001.json",
        {
            "cik": "0000000001",
            "filings": {"recent": _row("0000000001-24-000001"), "files": [{"name": name}]},
        },
    )
    child = _source(
        store,
        "edgar_submissions_history",
        {"cik": "0000000001", "basename": name},
        f"https://data.sec.gov/submissions/{name}",
        {
            "cik": "0000000001",
            **_row("0000000001-24-000001"),
            "primaryDocument": ["changed.htm"],
        },
    )
    with pytest.raises(SchemaMismatchError):
        discover_sec_filing_events(
            store, root_with_history, (child,), policy=_policy(), prior_cursor=None
        )
    assert conflict.url  # independent snapshot remains valid; no implicit source correction.


def test_closure_request_source_and_resource_failures(tmp_path, monkeypatch) -> None:
    store = SnapshotStore(tmp_path)
    name = "CIK0000000001-submissions-001.json"
    root = _source(
        store,
        "edgar_submissions",
        {"cik": "0000000001", "required_accessions": ["0000000001-24-000001"]},
        "https://data.sec.gov/submissions/CIK0000000001.json",
        {
            "cik": "0000000001",
            "filings": {"recent": _row("0000000001-24-000001"), "files": [{"name": name}]},
        },
    )
    child = _source(
        store,
        "edgar_submissions_history",
        {"cik": "0000000001", "basename": name},
        f"https://data.sec.gov/submissions/{name}",
        {"cik": "0000000001", **_row("0000000001-24-000002")},
    )
    with pytest.raises(CoverageError):
        discover_sec_filing_events(store, root, (child, child), policy=_policy(), prior_cursor=None)
    with pytest.raises(SchemaMismatchError):
        discover_sec_filing_events(
            store,
            replace(root, observation=replace(root.observation, endpoint="wrong")),
            (child,),
            policy=_policy(),
            prior_cursor=None,
        )
    wrong_request = _source(
        store,
        "edgar_submissions",
        {"cik": "0000000002", "required_accessions": ["0000000001-24-000001"]},
        "https://data.sec.gov/submissions/CIK0000000001.json",
        {"cik": "0000000001", "filings": {"recent": _row("0000000001-24-000001"), "files": []}},
    )
    with pytest.raises(SchemaMismatchError):
        discover_sec_filing_events(store, wrong_request, (), policy=_policy(), prior_cursor=None)
    monkeypatch.setattr("ohmydata.providers.sec.event_discovery._MAX_ROWS", 1)
    with pytest.raises(ResourceLimitError):
        discover_sec_filing_events(store, root, (child,), policy=_policy(), prior_cursor=None)


def test_replay_cache_reuses_exact_descriptor_but_counts_distinct_observations(tmp_path) -> None:
    from ohmydata.providers.sec.event_discovery import _DiscoveryReplayCache

    class CountingStore(SnapshotStore):
        calls = 0

        def replay_observation(self, *args, **kwargs):
            self.calls += 1
            return super().replay_observation(*args, **kwargs)

    store = CountingStore(tmp_path)
    root = _source(
        store,
        "edgar_submissions",
        {"cik": "0000000001", "required_accessions": ["0000000001-24-000001"]},
        "https://data.sec.gov/submissions/CIK0000000001.json",
        {"cik": "0000000001", "filings": {"recent": _row("0000000001-24-000001"), "files": []}},
    )
    cache = _DiscoveryReplayCache(store)
    first = discover_sec_filing_events(
        store, root, (), policy=_policy(), prior_cursor=None, _replay_cache=cache
    )
    second = discover_sec_filing_events(
        store, root, (), policy=_policy(), prior_cursor=None, _replay_cache=cache
    )
    assert first == second and store.calls == 1
    copied = store.observe(
        RequestSpec(
            "sec",
            "edgar_submissions",
            {"cik": "0000000001", "required_accessions": ["0000000001-24-000001"]},
        ),
        json.dumps(
            {"cik": "0000000001", "filings": {"recent": _row("0000000001-24-000001"), "files": []}}
        ).encode(),
        datetime(2025, 1, 2, tzinfo=UTC),
        "json-v1",
        SnapshotMode.APPEND,
    )
    discover_sec_filing_events(
        store,
        SecDiscoverySource(root.url, copied),
        (),
        policy=_policy(),
        prior_cursor=None,
        _replay_cache=cache,
    )
    assert store.calls == 2


def test_incremental_upper_and_canonical_source_payload_fail_before_replay(tmp_path) -> None:
    from ohmydata.providers.sec.event_discovery import SecDiscoveryCursor

    store = SnapshotStore(tmp_path)
    root = _source(
        store,
        "edgar_submissions",
        {"cik": "0000000001", "required_accessions": ["0000000001-24-000001"]},
        "https://data.sec.gov/submissions/CIK0000000001.json",
        {"cik": "0000000001", "filings": {"recent": _row("0000000001-24-000001"), "files": []}},
    )
    with pytest.raises(ValueError, match="incremental"):
        discover_sec_filing_events(
            store,
            root,
            (),
            policy=_policy(),
            prior_cursor=SecDiscoveryCursor(
                datetime(2025, 1, 1, tzinfo=UTC), "0000000001-24-999999"
            ),
        )
    descriptor = root.canonical_payload()
    descriptor["observation"]["provider"] = "../escape"  # type: ignore[index]
    with pytest.raises(SchemaMismatchError):
        SecDiscoverySource.from_canonical_payload(descriptor, store)
    batch = discover_sec_filing_events(store, root, (), policy=_policy(), prior_cursor=None)
    malformed = batch.canonical_payload()
    malformed["schema_version"] = True
    with pytest.raises(SchemaMismatchError):
        type(batch).from_canonical_payload(malformed, store)


def test_strict_json_rejects_duplicate_keys(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    raw = b'{"cik":"0000000001","cik":"0000000001","filings":{"recent":{"accessionNumber":[],"form":[],"filingDate":[],"reportDate":[],"primaryDocument":[],"acceptanceDateTime":[]},"files":[]}}'
    observation = store.observe(
        RequestSpec(
            "sec",
            "edgar_submissions",
            {"cik": "0000000001", "required_accessions": ["0000000001-24-000001"]},
        ),
        raw,
        datetime(2025, 1, 1, tzinfo=UTC),
        "json-v1",
        SnapshotMode.APPEND,
    )
    with pytest.raises(SchemaMismatchError, match="JSON"):
        discover_sec_filing_events(
            store,
            SecDiscoverySource("https://data.sec.gov/submissions/CIK0000000001.json", observation),
            (),
            policy=_policy(),
            prior_cursor=None,
        )


def test_exact_resource_boundaries_and_generator_sentinel(tmp_path, monkeypatch) -> None:
    import ohmydata.providers.sec.event_discovery as discovery

    store = SnapshotStore(tmp_path)
    root = _source(
        store,
        "edgar_submissions",
        {"cik": "0000000001", "required_accessions": ["0000000001-24-000001"]},
        "https://data.sec.gov/submissions/CIK0000000001.json",
        {"cik": "0000000001", "filings": {"recent": _row("0000000001-24-000001"), "files": []}},
    )
    payload_size = len(store.replay_observation(root.observation).payload)
    monkeypatch.setattr(discovery, "_MAX_SOURCE_BYTES", payload_size)
    assert discover_sec_filing_events(store, root, (), policy=_policy(), prior_cursor=None).events
    monkeypatch.setattr(discovery, "_MAX_SOURCE_BYTES", payload_size - 1)
    with pytest.raises(SnapshotIntegrityError):
        discover_sec_filing_events(store, root, (), policy=_policy(), prior_cursor=None)
    yielded = 0

    def too_many():
        nonlocal yielded
        for _ in range(17):
            yielded += 1
            yield root

    monkeypatch.setattr(discovery, "_MAX_SOURCE_BYTES", payload_size)
    with pytest.raises(ResourceLimitError):
        discover_sec_filing_events(store, root, too_many(), policy=_policy(), prior_cursor=None)
    assert yielded == 17
    monkeypatch.setattr(discovery, "_MAX_EVENTS", 1)
    with pytest.raises(ResourceLimitError):
        discover_sec_filing_events(store, root, (), policy=_policy(), prior_cursor=None)


def test_schema_arrays_and_unknown_canonical_payload_fail_closed(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    bad = _source(
        store,
        "edgar_submissions",
        {"cik": "0000000001", "required_accessions": ["0000000001-24-000001"]},
        "https://data.sec.gov/submissions/CIK0000000001.json",
        {
            "cik": "0000000001",
            "filings": {"recent": {**_row("0000000001-24-000001"), "form": []}, "files": []},
        },
    )
    with pytest.raises(SchemaMismatchError):
        discover_sec_filing_events(store, bad, (), policy=_policy(), prior_cursor=None)
    root = _source(
        store,
        "edgar_submissions",
        {"cik": "0000000001", "required_accessions": ["0000000001-24-000002"]},
        "https://data.sec.gov/submissions/CIK0000000001.json",
        {"cik": "0000000001", "filings": {"recent": _row("0000000001-24-000002"), "files": []}},
    )
    batch = discover_sec_filing_events(store, root, (), policy=_policy(), prior_cursor=None)
    payload = batch.canonical_payload()
    payload["unexpected"] = None
    with pytest.raises(SchemaMismatchError):
        type(batch).from_canonical_payload(payload, store)


def test_discovery_only_root_request_accepts_exact_cik_only(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    root = _source(
        store,
        "edgar_submissions",
        {"cik": "0000000001"},
        "https://data.sec.gov/submissions/CIK0000000001.json",
        {"cik": "0000000001", "filings": {"recent": _row("0000000001-24-000001"), "files": []}},
    )
    assert discover_sec_filing_events(store, root, (), policy=_policy(), prior_cursor=None).events

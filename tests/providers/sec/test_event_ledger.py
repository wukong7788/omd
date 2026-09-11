import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ohmydata.core import RequestSpec, SnapshotMode, SnapshotStore
from ohmydata.core.errors import ResourceLimitError, SnapshotIntegrityError
from ohmydata.providers.sec import _event_ledger_io as io
from ohmydata.providers.sec.event_discovery import (
    SecDiscoveryMode,
    SecDiscoveryPolicy,
    SecDiscoverySource,
    discover_sec_filing_events,
)
from ohmydata.providers.sec.event_ledger import (
    SecEventDiscoveryLedger,
    SecEventLedgerConflictError,
)


def batch(store, *, prior=None, number=1, document="synthetic.htm", observed_day=1):
    rows = {
        "accessionNumber": [f"0000000001-24-{number:06d}"],
        "form": ["10-Q"],
        "filingDate": ["2024-05-01"],
        "reportDate": ["2024-03-31"],
        "primaryDocument": [document],
        "acceptanceDateTime": ["2024-05-01T12:00:00Z"],
    }
    observation = store.observe(
        RequestSpec(
            "sec",
            "edgar_submissions",
            {"cik": "0000000001", "required_accessions": [f"0000000001-24-{number:06d}"]},
        ),
        json.dumps({"cik": "1", "filings": {"recent": rows, "files": []}}).encode(),
        datetime(2025, 1, observed_day, tzinfo=UTC),
        "json-v1",
        SnapshotMode.APPEND,
    )
    policy = SecDiscoveryPolicy(
        "1",
        ("10-Q",),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 12, 31, tzinfo=UTC),
        timedelta(days=2),
        SecDiscoveryMode.INCREMENTAL,
        "synthetic-v1",
    )
    return discover_sec_filing_events(
        store,
        SecDiscoverySource("https://data.sec.gov/submissions/CIK0000000001.json", observation),
        (),
        policy=policy,
        prior_cursor=prior,
    )


def test_restart_dedup_cursor_and_old_exact_retry(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    root = tmp_path / "ledger"
    ledger = SecEventDiscoveryLedger(root, store=store)
    assert ledger.load() == (None, ()) and not root.exists()
    first = batch(store)
    one = ledger.append(first, expected_receipt_id=None)
    second = batch(store, prior=one.cursor, number=2)
    two = ledger.append(second, expected_receipt_id=one.receipt_id)
    third = batch(store, prior=two.cursor, number=1, observed_day=2)
    three = ledger.append(third, expected_receipt_id=two.receipt_id)
    assert three.new_event_ids == () and three.event_count == 2 and three.cursor == two.cursor
    assert ledger.append(first, expected_receipt_id=None) == one
    restarted = SecEventDiscoveryLedger(root, store=SnapshotStore(store.root))
    head, events = restarted.load()
    assert head == three
    assert events == tuple(sorted(first.events + second.events, key=lambda event: event.key))
    assert len(list(root.glob("generation-*"))) == 3


def test_stale_parent_and_conflicting_metadata_do_not_advance(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    ledger = SecEventDiscoveryLedger(tmp_path / "ledger", store=store)
    first = batch(store)
    one = ledger.append(first, expected_receipt_id=None)
    with pytest.raises(SecEventLedgerConflictError, match="stale"):
        ledger.append(batch(store, prior=one.cursor, number=2), expected_receipt_id=None)
    with pytest.raises(SecEventLedgerConflictError, match="metadata"):
        ledger.append(
            batch(store, prior=one.cursor, document="changed.htm"),
            expected_receipt_id=one.receipt_id,
        )
    assert ledger.load() == (one, first.events)


def test_concurrent_exact_retry_has_one_generation(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    ledger = SecEventDiscoveryLedger(tmp_path / "ledger", store=store)
    first = batch(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ledger.append(first, expected_receipt_id=None), range(2)))
    assert results[0] == results[1]
    assert ledger.load() == (results[0], first.events)
    assert len(list(ledger.root.glob("generation-*"))) == 1


def test_concurrent_distinct_append_one_stale_parent(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    ledger = SecEventDiscoveryLedger(tmp_path / "ledger", store=store)
    batches = [batch(store), batch(store, number=2)]

    def attempt(value):
        try:
            return ledger.append(value, expected_receipt_id=None)
        except SecEventLedgerConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, batches))
    assert sum(result is not None for result in results) == 1
    assert ledger.load()[0] in results


@pytest.mark.parametrize("point", ["before_rename", "after_rename"])
def test_interruption_preserves_valid_prefix_and_retry(tmp_path, monkeypatch, point):
    store = SnapshotStore(tmp_path / "snapshots")
    ledger = SecEventDiscoveryLedger(tmp_path / "ledger", store=store)
    first = batch(store)
    original = io.os.rename

    def fail(*args, **kwargs):
        if point == "after_rename":
            original(*args, **kwargs)
        raise OSError("synthetic interruption")

    with monkeypatch.context() as context:
        context.setattr(io.os, "rename", fail)
        with pytest.raises(OSError, match="synthetic"):
            ledger.append(first, expected_receipt_id=None)
    before = ledger.load()
    assert (before[0] is None) == (point == "before_rename")
    receipt = ledger.append(first, expected_receipt_id=None)
    assert ledger.load() == (receipt, first.events)
    assert receipt.generation == 1


@pytest.mark.parametrize(
    "mutation", ["count", "cursor", "event", "unknown", "duplicate", "whitespace"]
)
def test_hash_correct_tampering_rejected(tmp_path, mutation):
    store = SnapshotStore(tmp_path / "snapshots")
    ledger = SecEventDiscoveryLedger(tmp_path / "ledger", store=store)
    ledger.append(batch(store), expected_receipt_id=None)
    path = ledger.root / "generation-00000001" / "manifest.json"
    value = json.loads(path.read_bytes())
    if mutation == "count":
        value["event_count"] += 1
    elif mutation == "cursor":
        value["cursor"]["accession"] = "0000000001-24-999999"
    elif mutation == "event":
        value["batch"]["events"][0]["primary_document"] = "forged.htm"
        value["batch_identity"] = hashlib.sha256(io.canonical_bytes(value["batch"])).hexdigest()
    elif mutation == "unknown":
        value["unknown"] = True
    value.pop("receipt_id")
    value["receipt_id"] = hashlib.sha256(io.canonical_bytes(value)).hexdigest()
    raw = io.canonical_bytes(value)
    if mutation == "duplicate":
        raw = b'{"schema":"sec-event-discovery-ledger-v1",' + raw[1:]
    if mutation == "whitespace":
        raw += b"\n"
    path.write_bytes(raw)
    with pytest.raises((SnapshotIntegrityError, ValueError)):
        ledger.load()


@pytest.mark.parametrize("mutation", ["gap", "partial", "symlink", "extra", "ancestor"])
def test_invalid_filesystem_rejected(tmp_path, mutation):
    store = SnapshotStore(tmp_path / "snapshots")
    root = tmp_path / "ledger"
    ledger = SecEventDiscoveryLedger(root, store=store)
    ledger.append(batch(store), expected_receipt_id=None)
    generation = root / "generation-00000001"
    if mutation == "gap":
        generation.rename(root / "generation-00000002")
    elif mutation == "partial":
        (generation / "manifest.json").unlink()
    elif mutation == "symlink":
        (root / "generation-00000002").symlink_to(generation, target_is_directory=True)
    elif mutation == "extra":
        (root / ".arbitrary").mkdir()
    else:
        (tmp_path / "alias").symlink_to(root, target_is_directory=True)
        ledger = SecEventDiscoveryLedger(tmp_path / "alias", store=store)
    with pytest.raises(SnapshotIntegrityError):
        ledger.load()


def test_forged_batch_and_resource_limits_preserve_head(tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path / "snapshots")
    ledger = SecEventDiscoveryLedger(tmp_path / "ledger", store=store)
    first = batch(store)
    with pytest.raises(SnapshotIntegrityError):
        ledger.append(replace(first, batch_identity="0" * 64), expected_receipt_id=None)
    receipt = ledger.append(first, expected_receipt_id=None)
    second = batch(store, prior=receipt.cursor, number=2)
    with monkeypatch.context() as context:
        context.setattr(io, "MAX_GENERATIONS", 1)
        with pytest.raises(ResourceLimitError):
            ledger.append(second, expected_receipt_id=receipt.receipt_id)
        assert ledger.append(first, expected_receipt_id=None) == receipt
    size = (ledger.root / "generation-00000001" / "manifest.json").stat().st_size
    with monkeypatch.context() as context:
        context.setattr(io, "MAX_TOTAL_MANIFEST_BYTES", size)
        assert ledger.load()[0] == receipt
        with pytest.raises(ResourceLimitError):
            ledger.append(second, expected_receipt_id=receipt.receipt_id)
    assert ledger.load() == (receipt, first.events)


def test_decoder_rejects_deep_json_before_decode():
    with pytest.raises(ResourceLimitError):
        io.decode_manifest(b"[" * 65 + b"0" + b"]" * 65)


@pytest.mark.parametrize("mutation", ["fifo", "nonempty_lock", "hardlinked_lock"])
def test_special_files_are_rejected_without_blocking(tmp_path, mutation):
    store = SnapshotStore(tmp_path / "snapshots")
    ledger = SecEventDiscoveryLedger(tmp_path / "ledger", store=store)
    ledger.append(batch(store), expected_receipt_id=None)
    if mutation == "fifo":
        path = ledger.root / "generation-00000001" / "manifest.json"
        path.unlink()
        os.mkfifo(path)
    elif mutation == "nonempty_lock":
        (ledger.root / ".writer.lock").write_bytes(b"invalid")
    else:
        os.link(ledger.root / ".writer.lock", tmp_path / "lock_copy")
    with pytest.raises(SnapshotIntegrityError):
        ledger.load()


def test_new_ledger_ancestors_are_durably_created(tmp_path, monkeypatch):
    original_mkdir, original_fsync = io.os.mkdir, io.os.fsync
    pending = []
    synced = []

    def mkdir(*args, **kwargs):
        original_mkdir(*args, **kwargs)
        pending.append(kwargs["dir_fd"])

    def fsync(fd):
        if pending:
            assert pending.pop() == fd
            synced.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(io.os, "mkdir", mkdir)
    monkeypatch.setattr(io.os, "fsync", fsync)
    with io.root_directory(tmp_path / "new" / "ledger", create=True) as fd:
        assert fd is not None
    assert len(synced) == 2 and pending == []


def test_sec_import_does_not_require_fcntl():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['fcntl'] = None; import ohmydata.providers.sec",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr

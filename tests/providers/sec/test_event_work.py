import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.core.errors import SnapshotIntegrityError
from ohmydata.providers.sec import _event_ledger_io as io
from ohmydata.providers.sec.event_dependencies import (
    SecDataVersionId,
    SecDataVersionKind,
    SecDependencyEdge,
    SecDependencyIndex,
)
from ohmydata.providers.sec.event_ledger import SecEventLedgerConflictError
from ohmydata.providers.sec.event_work import (
    SecEventWorkCommand,
    SecEventWorkErrorClass,
    SecEventWorkLedger,
    SecEventWorkSpec,
)
from ohmydata.providers.sec.event_work import (
    SecEventWorkPhase as Phase,
)
from tests.providers.sec.test_event_ledger import batch

TIME = datetime(2025, 1, 2, tzinfo=UTC)
INPUT = SecDataVersionId(SecDataVersionKind.RAW_FACT, "a" * 64)
OUTPUT = SecDataVersionId(SecDataVersionKind.NORMALIZED_FACT, "b" * 64)


def command(phase, number, **kwargs):
    fields = {
        "command_id": hashlib.sha256(str(number).encode()).hexdigest(),
        "recorded_at": TIME + timedelta(seconds=number),
        "target_state": phase,
        "error_class": None,
        "reason_code": "SYNTHETIC_REPORT",
        "retry_at": None,
        "output_version_ids": (),
        "quality_evidence_ids": (),
        "dependency_edges": (),
    }
    fields.update(kwargs)
    return SecEventWorkCommand(**fields)


def setup(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    discovered = batch(store)
    event = discovered.events[0]
    spec = SecEventWorkSpec(event.key, event.metadata_digest, "synthetic-v1", "c" * 64, (INPUT,), 3)
    ledger = SecEventWorkLedger(tmp_path / "work", store=store)
    return store, discovered, spec, ledger


def advance_to_fetch(discovered, spec, ledger):
    head = None
    for i, phase in enumerate((Phase.DISCOVERED, Phase.QUEUED, Phase.FETCHING)):
        head = ledger.append(
            discovered,
            spec,
            command(phase, i),
            expected_receipt_id=None if head is None else head.receipt_id,
        )
    return head


def test_full_progression_restart_and_exact_old_retry(tmp_path):
    store, discovered, spec, ledger = setup(tmp_path)
    assert ledger.load() == (None, None, ())
    head = advance_to_fetch(discovered, spec, ledger)
    validating = command(Phase.VALIDATING, 3, output_version_ids=(OUTPUT,))
    head = ledger.append(discovered, spec, validating, expected_receipt_id=head.receipt_id)
    dependency = SecDependencyEdge(INPUT, OUTPUT, "1", "c" * 64, TIME + timedelta(seconds=4))
    ready = command(
        Phase.READY,
        4,
        output_version_ids=(OUTPUT,),
        quality_evidence_ids=("d" * 64,),
        dependency_edges=(dependency,),
    )
    receipt = ledger.append(discovered, spec, ready, expected_receipt_id=head.receipt_id)
    restarted = SecEventWorkLedger(ledger.root, store=SnapshotStore(store.root))
    loaded, state, edges = restarted.load()
    assert state is not None
    assert loaded == receipt and state.state is Phase.READY and state.attempts == 1
    assert state.output_version_ids == (OUTPUT,) and state.quality_evidence_ids == ("d" * 64,)
    assert edges == (dependency,)
    assert SecDependencyIndex(edges).affected_versions(
        (INPUT,), knowledge_cutoff=ready.recorded_at
    ) == (OUTPUT,)
    old = restarted.append(discovered, spec, command(Phase.DISCOVERED, 0), expected_receipt_id=None)
    assert old.generation == 1 and restarted.load()[0] == receipt
    with pytest.raises(SecEventLedgerConflictError, match="transition"):
        restarted.append(
            discovered, spec, command(Phase.QUEUED, 5), expected_receipt_id=receipt.receipt_id
        )


def test_retry_gates_and_total_attempt_exhaustion(tmp_path):
    _, discovered, spec, ledger = setup(tmp_path)
    head = advance_to_fetch(discovered, spec, ledger)
    with pytest.raises(SecEventLedgerConflictError, match="cannot retry"):
        ledger.append(
            discovered,
            spec,
            command(
                Phase.RETRY_WAIT,
                3,
                error_class=SecEventWorkErrorClass.PERMANENT,
                retry_at=TIME + timedelta(seconds=4),
            ),
            expected_receipt_id=head.receipt_id,
        )
    for attempt in (1, 2):
        n = attempt * 10
        head = ledger.append(
            discovered,
            spec,
            command(
                Phase.RETRY_WAIT,
                n,
                error_class=SecEventWorkErrorClass.TRANSIENT,
                retry_at=TIME + timedelta(seconds=n + 2),
            ),
            expected_receipt_id=head.receipt_id,
        )
        with pytest.raises(SecEventLedgerConflictError, match="not due"):
            ledger.append(
                discovered, spec, command(Phase.QUEUED, n + 1), expected_receipt_id=head.receipt_id
            )
        head = ledger.append(
            discovered, spec, command(Phase.QUEUED, n + 2), expected_receipt_id=head.receipt_id
        )
        head = ledger.append(
            discovered, spec, command(Phase.FETCHING, n + 3), expected_receipt_id=head.receipt_id
        )
    with pytest.raises(SecEventLedgerConflictError, match="cannot retry"):
        ledger.append(
            discovered,
            spec,
            command(
                Phase.RETRY_WAIT,
                30,
                error_class=SecEventWorkErrorClass.TRANSIENT,
                retry_at=TIME + timedelta(seconds=31),
            ),
            expected_receipt_id=head.receipt_id,
        )
    head = ledger.append(
        discovered,
        spec,
        command(Phase.FAILED, 31, error_class=SecEventWorkErrorClass.TRANSIENT),
        expected_receipt_id=head.receipt_id,
    )
    assert ledger.load()[1].attempts == 3
    assert ledger.load()[0] == head


@pytest.mark.parametrize(
    "failure", ["unknown_event", "early", "jump", "extra_evidence", "bool_attempts"]
)
def test_initialization_rejects_invalid_reports(tmp_path, failure):
    _, discovered, spec, ledger = setup(tmp_path)
    item = command(Phase.DISCOVERED, 0)
    if failure == "unknown_event":
        spec = replace(spec, event_key="f" * 64)
    elif failure == "early":
        item = replace(item, recorded_at=datetime(2024, 1, 1, tzinfo=UTC))
    elif failure == "jump":
        item = replace(item, target_state=Phase.READY)
    elif failure == "extra_evidence":
        item = replace(item, quality_evidence_ids=("a" * 64,))
    else:
        with pytest.raises(ValueError):
            replace(spec, max_attempts=True)
        return
    with pytest.raises(SecEventLedgerConflictError):
        ledger.append(discovered, spec, item, expected_receipt_id=None)
    assert ledger.load() == (None, None, ())


def test_stale_conflicting_retry_other_work_and_concurrent_retry(tmp_path):
    _, discovered, spec, ledger = setup(tmp_path)
    first = command(Phase.DISCOVERED, 0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(
                lambda _: ledger.append(discovered, spec, first, expected_receipt_id=None), range(2)
            )
        )
    assert receipts[0] == receipts[1]
    with pytest.raises(SecEventLedgerConflictError, match="different content"):
        ledger.append(
            discovered, spec, replace(first, reason_code="CHANGED"), expected_receipt_id=None
        )
    with pytest.raises(SecEventLedgerConflictError, match="stale"):
        ledger.append(discovered, spec, command(Phase.QUEUED, 1), expected_receipt_id=None)
    with pytest.raises(SecEventLedgerConflictError, match="another work"):
        ledger.append(
            discovered, replace(spec, processing_version="v2"), first, expected_receipt_id=None
        )


@pytest.mark.parametrize("failure", ["outputs", "quality", "edge_input", "edge_cik", "edge_time"])
def test_ready_requires_exact_declared_bindings(tmp_path, failure):
    _, discovered, spec, ledger = setup(tmp_path)
    head = advance_to_fetch(discovered, spec, ledger)
    head = ledger.append(
        discovered,
        spec,
        command(Phase.VALIDATING, 3, output_version_ids=(OUTPUT,)),
        expected_receipt_id=head.receipt_id,
    )
    edge = SecDependencyEdge(INPUT, OUTPUT, "1", "c" * 64, TIME + timedelta(seconds=4))
    item = command(
        Phase.READY,
        4,
        output_version_ids=(OUTPUT,),
        quality_evidence_ids=("d" * 64,),
        dependency_edges=(edge,),
    )
    if failure == "outputs":
        item = replace(item, output_version_ids=(INPUT,))
    elif failure == "quality":
        item = replace(item, quality_evidence_ids=())
    else:
        if failure == "edge_input":
            edge = replace(
                edge, input_version=SecDataVersionId(SecDataVersionKind.RAW_FACT, "f" * 64)
            )
        elif failure == "edge_cik":
            edge = replace(edge, canonical_cik="2")
        else:
            edge = replace(edge, recorded_at=TIME)
        item = replace(item, dependency_edges=(edge,))
    with pytest.raises(SecEventLedgerConflictError):
        ledger.append(discovered, spec, item, expected_receipt_id=head.receipt_id)
    assert ledger.load()[0] == head


def test_hash_correct_state_tampering_and_interruption(tmp_path, monkeypatch):
    _, discovered, spec, ledger = setup(tmp_path)
    original = io.os.rename

    def fail(*args, **kwargs):
        raise OSError("synthetic interruption")

    monkeypatch.setattr(io.os, "rename", fail)
    with pytest.raises(OSError):
        ledger.append(discovered, spec, command(Phase.DISCOVERED, 0), expected_receipt_id=None)
    assert ledger.load() == (None, None, ())
    monkeypatch.setattr(io.os, "rename", original)
    ledger.append(discovered, spec, command(Phase.DISCOVERED, 0), expected_receipt_id=None)
    path = ledger.root / "generation-00000001" / "manifest.json"
    value = json.loads(path.read_bytes())
    value["state"]["attempts"] = 2
    value.pop("receipt_id")
    value["receipt_id"] = hashlib.sha256(io.canonical_bytes(value)).hexdigest()
    path.write_bytes(io.canonical_bytes(value))
    with pytest.raises(SnapshotIntegrityError, match="reconstruction"):
        ledger.load()


def test_quarantined_terminal_retains_validation_outputs(tmp_path):
    _, discovered, spec, ledger = setup(tmp_path)
    head = advance_to_fetch(discovered, spec, ledger)
    head = ledger.append(
        discovered,
        spec,
        command(Phase.VALIDATING, 3, output_version_ids=(OUTPUT,)),
        expected_receipt_id=head.receipt_id,
    )
    head = ledger.append(
        discovered, spec, command(Phase.QUARANTINED, 4), expected_receipt_id=head.receipt_id
    )
    _, state, edges = ledger.load()
    assert state.state is Phase.QUARANTINED and state.output_version_ids == (OUTPUT,)
    assert state.quality_evidence_ids == edges == ()
    with pytest.raises(SecEventLedgerConflictError, match="transition"):
        ledger.append(
            discovered,
            spec,
            command(Phase.READY, 5, output_version_ids=(OUTPUT,), quality_evidence_ids=("d" * 64,)),
            expected_receipt_id=head.receipt_id,
        )


def test_decreasing_command_time_and_hash_correct_command_change(tmp_path):
    _, discovered, spec, ledger = setup(tmp_path)
    first = ledger.append(discovered, spec, command(Phase.DISCOVERED, 1), expected_receipt_id=None)
    with pytest.raises(SecEventLedgerConflictError, match="precedes"):
        ledger.append(
            discovered, spec, command(Phase.QUEUED, 0), expected_receipt_id=first.receipt_id
        )
    path = ledger.root / "generation-00000001" / "manifest.json"
    value = json.loads(path.read_bytes())
    value["command"]["target_state"] = "READY"
    value.pop("receipt_id")
    value["receipt_id"] = hashlib.sha256(io.canonical_bytes(value)).hexdigest()
    path.write_bytes(io.canonical_bytes(value))
    with pytest.raises(SnapshotIntegrityError):
        ledger.load()


def test_source_observation_revalidated_once_per_load_and_tampering_rejected(tmp_path, monkeypatch):
    store, discovered, spec, ledger = setup(tmp_path)
    advance_to_fetch(discovered, spec, ledger)
    original = store.replay_observation
    calls = []

    def replay(*args, **kwargs):
        calls.append(args[0].observation_identity)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "replay_observation", replay)
    assert ledger.load()[1].state is Phase.FETCHING
    assert calls == [discovered.sources[0].observation.observation_identity]
    discovered.sources[0].observation.path.write_bytes(b"{}")
    with pytest.raises(SnapshotIntegrityError):
        ledger.load()


def test_work_collection_and_utf8_boundaries(tmp_path):
    _, _, spec, _ = setup(tmp_path)
    ids = tuple(SecDataVersionId(SecDataVersionKind.RAW_FACT, f"{i:064x}") for i in range(1024))
    assert len(replace(spec, input_version_ids=ids).input_version_ids) == 1024
    with pytest.raises(ValueError, match="bounded"):
        replace(spec, input_version_ids=ids + (ids[-1],))
    item = command(Phase.DISCOVERED, 0)
    assert len(replace(item, output_version_ids=ids).output_version_ids) == 1024
    with pytest.raises(ValueError, match="bounded"):
        replace(item, output_version_ids=ids + (ids[-1],))
    quality = tuple(value.identity for value in ids)
    assert len(replace(item, quality_evidence_ids=quality).quality_evidence_ids) == 1024
    with pytest.raises(ValueError, match="bounded"):
        replace(item, quality_evidence_ids=quality + (quality[-1],))
    assert replace(item, reason_code="é" * 512).reason_code == "é" * 512
    with pytest.raises(ValueError, match="bounded"):
        replace(item, reason_code="é" * 513)

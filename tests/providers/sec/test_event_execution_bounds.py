from dataclasses import replace
from datetime import timedelta

import pytest

from ohmydata.core import RequestSpec, SnapshotMode
from ohmydata.core.errors import ResourceLimitError, SnapshotIntegrityError
from ohmydata.providers.sec import _event_execution_io as execution_io
from ohmydata.providers.sec import _event_ledger_io as io
from ohmydata.providers.sec._event_execution_receipts import (
    base,
    descriptor,
    reference,
    request,
    retain_sec_execution_outputs,
)
from ohmydata.providers.sec.event_execution import (
    SecExecutionRecovery,
    SecExecutionRecoveryResult,
    SecExecutionRetry,
    SecExecutionStatus,
)
from ohmydata.providers.sec.event_work import SecEventWorkPhase
from tests.providers.sec.test_event_execution import TIME, fixture, run


@pytest.mark.parametrize("stage", ["acquire", "validate"])
@pytest.mark.parametrize(
    "field,value", [("attempt", True), ("max_attempts", True), ("max_attempts", 1.0)]
)
def test_hash_correct_receipt_wrong_json_types_rejected(tmp_path, stage, field, value):
    parts = list(fixture(tmp_path))
    parts[2] = replace(parts[2], max_attempts=1)
    handler = parts[3] if stage == "acquire" else parts[4]

    def forged(operation, acquisition=None, outputs=None):
        artifact = handler.store.observe(
            RequestSpec("sec", "synthetic-malicious-artifact", {"stage": stage}),
            b'{"synthetic":true}',
            TIME,
            "synthetic-json-v1",
        )
        payload = base(operation, stage, TIME)
        if stage == "acquire":
            payload["outputs"] = [descriptor(artifact)]
        else:
            payload.update(
                acquisition=descriptor(acquisition),
                ready=True,
                evidence=[descriptor(artifact)],
                dependencies=[],
            )
        if field == "attempt":
            payload[field] = value
        else:
            payload["spec"][field] = value
        # Deliberately bypass production codecs; the retained snapshot hashes are valid.
        import json

        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return handler.store.observe(
            request(operation, stage),
            raw,
            TIME,
            f"sec-event-execution-{stage}-v1",
            SnapshotMode.FROZEN,
        )

    if stage == "acquire":
        handler.execute = forged
    else:
        handler.validate = forged
    with pytest.raises(SnapshotIntegrityError):
        run(parts)


def test_expired_execute_retry_does_not_start_recovery_callback(tmp_path):
    parts = fixture(tmp_path)

    def expired(operation):
        parts[5][0] = TIME + timedelta(days=3)
        raise SecExecutionRetry(TIME + timedelta(days=4))

    parts[3].execute = expired
    assert run(parts).status is SecExecutionStatus.YIELDED
    assert parts[3].recoveries == 1
    assert parts[0].ledger.load()[1].state is SecEventWorkPhase.FETCHING


@pytest.mark.parametrize("stage", ["acquire", "validate"])
def test_recovery_directory_swap_prevents_next_callback(tmp_path, stage):
    parts = fixture(tmp_path)
    if stage == "validate":
        assert run(parts, max_steps=4).status is SecExecutionStatus.YIELDED
    owner = parts[3] if stage == "acquire" else parts[4]

    def swapped(operation):
        folder = parts[0].ledger.root / ".execution"
        folder.rename(tmp_path / "moved-execution")
        folder.mkdir()
        return SecExecutionRecoveryResult(SecExecutionRecovery.NOT_STARTED)

    owner.recover = swapped
    with pytest.raises(SnapshotIntegrityError, match="replaced"):
        run(parts)
    assert owner.executions == 0


@pytest.mark.parametrize("stage", ["acquire", "validate"])
def test_crash_after_locator_link_recovers_without_any_repeated_callback(
    tmp_path, monkeypatch, stage
):
    parts = fixture(tmp_path)
    real_link = execution_io.os.link
    crashed = []

    def interrupted(source, destination, **kwargs):
        real_link(source, destination, **kwargs)
        if str(destination).endswith(f"-{stage}.json") and not crashed:
            crashed.append(True)
            raise OSError("synthetic interruption after link")

    monkeypatch.setattr(execution_io.os, "link", interrupted)
    with pytest.raises(OSError, match="synthetic interruption"):
        run(parts)
    counts = (parts[3].executions, parts[3].recoveries, parts[4].executions, parts[4].recoveries)
    assert run(parts).status is SecExecutionStatus.READY
    if stage == "validate":
        assert counts == (
            parts[3].executions,
            parts[3].recoveries,
            parts[4].executions,
            parts[4].recoveries,
        )
    else:
        assert counts[:2] == (parts[3].executions, parts[3].recoveries)


def test_expired_initial_deadline_does_not_call_handlers(tmp_path):
    parts = fixture(tmp_path)
    parts[5][0] = TIME + timedelta(days=3)
    assert run(parts).status is SecExecutionStatus.YIELDED
    assert parts[0].ledger.load()[1] is None
    assert parts[3].recoveries == parts[4].recoveries == 0


def test_replay_budget_does_not_accept_partial_acquisition(tmp_path):
    parts = fixture(tmp_path)
    with pytest.raises((ResourceLimitError, SnapshotIntegrityError)):
        run(parts, max_replay_bytes=1)
    assert parts[0].ledger.load()[1].state is SecEventWorkPhase.FETCHING
    assert parts[4].executions == 0


def test_locator_conflict_preserves_original_bytes(tmp_path):
    parts = fixture(tmp_path)
    run(parts)
    folder = parts[0].ledger.root / ".execution"
    path = next(folder.glob("*-acquire.json"))
    before = path.read_bytes()
    with (
        execution_io.execution_lock(parts[0].ledger.root) as fd,
        pytest.raises(SnapshotIntegrityError, match="conflict"),
    ):
        execution_io.write_locator(fd, path.name, {"synthetic": "wrong locator"})
    assert path.read_bytes() == before
    with io.root_directory(parts[0].ledger.root, create=False) as fd:
        assert io.generation_names(fd, allow_execution=True)


@pytest.mark.parametrize("count", [16, 17])
def test_output_count_boundary_is_enforced_before_receipt(tmp_path, count):
    parts = fixture(tmp_path)
    handler = parts[3]

    def outputs(operation):
        refs = tuple(
            handler.store.observe(
                RequestSpec("sec", "synthetic-many-outputs", {"ordinal": i}),
                b"synthetic",
                TIME,
                "synthetic-bytes-v1",
            )
            for i in range(count)
        )
        return retain_sec_execution_outputs(
            store=handler.store, operation=operation, outputs=refs, recorded_at=TIME
        )

    handler.execute = outputs
    if count == 16:
        assert run(parts).status is SecExecutionStatus.READY
        assert len(parts[0].ledger.load()[1].output_version_ids) == 16
    else:
        with pytest.raises(ResourceLimitError, match="1..16"):
            run(parts)
        assert parts[4].executions == 0


@pytest.mark.parametrize("extra", [0, 1])
def test_output_payload_eight_mib_boundary(tmp_path, extra):
    parts = fixture(tmp_path)
    handler = parts[3]

    def output(operation):
        ref = handler.store.observe(
            RequestSpec("sec", "synthetic-large-output", {}),
            b"x" * (8 * 1024**2 + extra),
            TIME,
            "synthetic-bytes-v1",
        )
        return retain_sec_execution_outputs(
            store=handler.store, operation=operation, outputs=(ref,), recorded_at=TIME
        )

    handler.execute = output
    if extra:
        with pytest.raises((SnapshotIntegrityError, ResourceLimitError)):
            run(parts)
        assert parts[4].executions == 0
    else:
        assert run(parts).status is SecExecutionStatus.READY


def test_exact_aggregate_byte_budget_counts_unique_retained_dependencies(tmp_path):
    import json

    parts = fixture(tmp_path)
    completed = run(parts)
    store = parts[0].store
    acquire = json.loads(store.replay_observation(completed.output_receipt).payload)
    validate = json.loads(store.replay_observation(completed.validation_receipt).payload)
    refs = (
        completed.output_receipt,
        completed.validation_receipt,
        *(reference(v, store) for v in acquire["outputs"]),
        *(reference(v, store) for v in validate["evidence"]),
    )
    unique = {ref.observation_identity: ref for ref in refs}
    required = sum(len(store.replay_observation(ref).payload) for ref in unique.values())
    assert run(parts, max_replay_bytes=required).status is SecExecutionStatus.READY
    with pytest.raises((SnapshotIntegrityError, ResourceLimitError)):
        run(parts, max_replay_bytes=required - 1)
    assert parts[3].executions == parts[4].executions == 1


def test_metadata_entry_budget_and_naive_clock_fail_explicitly(tmp_path):
    parts = fixture(tmp_path)
    parts[0].clock = lambda: TIME.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        run(parts)
    assert parts[3].executions == 0
    folder = parts[0].ledger.root / ".execution"
    for i in range(33):
        (folder / f"stage-{i:032x}").write_bytes(b"")
    with pytest.raises(ResourceLimitError, match="entry limit"):
        run(parts)

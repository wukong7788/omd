import json
import multiprocessing
import os
from datetime import timedelta

import pytest

from ohmydata.core import RequestSpec, SnapshotStore
from ohmydata.core.errors import AuthenticationError, SnapshotIntegrityError
from ohmydata.providers.sec import _event_ledger_io as ledger_io
from ohmydata.providers.sec._event_execution_io import execution_lock
from ohmydata.providers.sec.event_dependencies import SecDataVersionId, SecDataVersionKind
from ohmydata.providers.sec.event_execution import (
    SecEventExecutor,
    SecExecutionDependency,
    SecExecutionRecovery,
    SecExecutionRecoveryResult,
    SecExecutionRetry,
    SecExecutionStatus,
    SecExecutionValidation,
    retain_sec_execution_outputs,
    retain_sec_execution_validation,
)
from ohmydata.providers.sec.event_work import SecEventWorkLedger, SecEventWorkPhase
from tests.providers.sec.test_event_work import TIME, setup


class Handler:
    version = "synthetic-v1"

    def __init__(self, store, clock):
        self.store, self.clock = store, clock
        self.receipts = {}
        self.executions = self.recoveries = 0
        self.failure = None
        self.crash = False
        self.unknown = False

    def recover(self, operation):
        self.recoveries += 1
        if self.unknown:
            return SecExecutionRecoveryResult(SecExecutionRecovery.UNKNOWN)
        if operation.key in self.receipts:
            return SecExecutionRecoveryResult(
                SecExecutionRecovery.COMPLETE, self.receipts[operation.key]
            )
        return SecExecutionRecoveryResult(SecExecutionRecovery.NOT_STARTED)

    def execute(self, operation):
        self.executions += 1
        if self.failure:
            raise self.failure
        output = self.store.observe(
            RequestSpec("sec", "synthetic-output", {"operation": operation.key}),
            b'{"synthetic_value":"100.00"}',
            self.clock(),
            "synthetic-output-v1",
        )
        ref = retain_sec_execution_outputs(
            store=self.store, operation=operation, outputs=(output,), recorded_at=self.clock()
        )
        self.receipts[operation.key] = ref
        if self.crash:
            raise RuntimeError("synthetic crash after acquisition retention")
        return ref


class Validator(Handler):
    version = "synthetic-validator-v1"
    ready = True

    def validate(self, operation, acquisition, outputs):
        self.executions += 1
        if self.failure:
            raise self.failure
        evidence = self.store.observe(
            RequestSpec("sec", "synthetic-validation", {"operation": operation.key}),
            json.dumps({"synthetic": True, "ready": self.ready}).encode(),
            self.clock(),
            "synthetic-validation-v1",
        )
        dependencies = (
            ()
            if not self.ready
            else (
                SecExecutionDependency(
                    operation.spec.input_version_ids[0],
                    SecDataVersionId(SecDataVersionKind.RAW_FACT, outputs[0].fact_version),
                    "e" * 64,
                ),
            )
        )
        ref = retain_sec_execution_validation(
            store=self.store,
            operation=operation,
            acquisition=acquisition,
            validation=SecExecutionValidation(self.ready, (evidence,), dependencies),
            recorded_at=self.clock(),
            canonical_cik="1",
        )
        self.receipts[operation.key] = ref
        if self.crash:
            raise RuntimeError("synthetic crash after validation retention")
        return ref


def fixture(tmp_path):
    store, batch, spec, ledger = setup(tmp_path)
    time = [TIME]
    clock = lambda: time[0]
    executor = SecEventExecutor(ledger, store=store, clock=clock)
    handler, validator = Handler(store, clock), Validator(store, clock)
    return executor, batch, spec, handler, validator, time


def run(parts, **kwargs):
    executor, batch, spec, handler, validator, _ = parts
    return executor.run(
        batch, spec, handler, validator, deadline=TIME + timedelta(days=2), **kwargs
    )


@pytest.mark.parametrize("ready", [True, False])
def test_full_cycle_warm_restart_no_callbacks(tmp_path, ready):
    parts = fixture(tmp_path)
    executor, batch, spec, handler, validator, _ = parts
    validator.ready = ready
    first = run(parts)
    assert first.status is (SecExecutionStatus.READY if ready else SecExecutionStatus.QUARANTINED)
    counts = (handler.executions, handler.recoveries, validator.executions, validator.recoveries)
    assert counts == (1, 1, 1, 1)
    restarted = SecEventExecutor(
        SecEventWorkLedger(executor.ledger.root, store=SnapshotStore(executor.ledger.store.root)),
        store=SnapshotStore(executor.store.root),
        clock=handler.clock,
    )
    second = restarted.run(batch, spec, handler, validator, deadline=TIME + timedelta(days=1))
    assert second.state == first.state and second.output_receipt == first.output_receipt
    assert second.validation_receipt == first.validation_receipt
    assert (
        handler.executions,
        handler.recoveries,
        validator.executions,
        validator.recoveries,
    ) == counts
    assert len(executor.ledger.load()[2]) == int(ready)


@pytest.mark.parametrize("side", [3, 4])
def test_crash_after_callback_retention_recovers_complete(tmp_path, side):
    parts = fixture(tmp_path)
    parts[side].crash = True
    with pytest.raises(RuntimeError, match="synthetic crash"):
        run(parts)
    assert parts[0].ledger.load()[1].state is (
        SecEventWorkPhase.FETCHING if side == 3 else SecEventWorkPhase.VALIDATING
    )
    parts[5][0] += timedelta(minutes=1)
    parts[side].crash = False
    completed = run(parts)
    assert completed.status is SecExecutionStatus.READY
    assert parts[3].executions == parts[4].executions == 1


@pytest.mark.parametrize("steps", [1, 2, 3, 4])
def test_step_yield_is_resumable(tmp_path, steps):
    parts = fixture(tmp_path)
    yielded = run(parts, max_steps=steps)
    assert yielded.status is SecExecutionStatus.YIELDED
    assert run(parts).status is SecExecutionStatus.READY
    assert parts[3].executions == parts[4].executions == 1


@pytest.mark.parametrize("side", [3, 4])
def test_unknown_is_durable_failed_without_execution(tmp_path, side):
    parts = fixture(tmp_path)
    parts[side].unknown = True
    result = run(parts)
    assert result.status is SecExecutionStatus.FAILED and parts[side].executions == 0
    parts[side].unknown = False
    assert run(parts).status is SecExecutionStatus.FAILED
    assert parts[side].executions == 0


def test_retry_due_and_exhaustion_are_persisted_without_sleep(tmp_path):
    parts = fixture(tmp_path)
    for attempt in range(1, 4):
        parts[3].failure = SecExecutionRetry(parts[5][0] + timedelta(seconds=10))
        result = run(parts)
        assert result.state.attempts == attempt
        if attempt < 3:
            assert result.status is SecExecutionStatus.WAITING
            assert run(parts).status is SecExecutionStatus.WAITING
            assert parts[3].executions == attempt
            parts[5][0] += timedelta(seconds=10)
        else:
            assert result.status is SecExecutionStatus.FAILED
    assert parts[3].executions == 3 and parts[4].executions == 0


@pytest.mark.parametrize("side", [3, 4])
def test_permanent_failure_is_not_retried(tmp_path, side):
    parts = fixture(tmp_path)
    parts[side].failure = AuthenticationError("synthetic credential rejected")
    assert run(parts).status is SecExecutionStatus.FAILED
    assert run(parts).status is SecExecutionStatus.FAILED
    assert parts[side].executions == 1


@pytest.mark.parametrize("which", ["acquire", "validate"])
def test_missing_or_wrong_locator_blocks_terminal_reuse(tmp_path, which):
    parts = fixture(tmp_path)
    run(parts)
    path = next((parts[0].ledger.root / ".execution").glob(f"*-{which}.json"))
    data = json.loads(path.read_text())
    data["response_sha256"] = "f" * 64
    path.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")))
    with pytest.raises(SnapshotIntegrityError):
        run(parts)
    assert parts[3].executions == parts[4].executions == 1


def test_execution_directory_is_not_allowed_in_discovery_ledger(tmp_path):
    parts = fixture(tmp_path)
    run(parts)
    with ledger_io.root_directory(parts[0].ledger.root, create=False) as fd:
        with pytest.raises(SnapshotIntegrityError, match="unexpected"):
            ledger_io.generation_names(fd)
        assert len(ledger_io.generation_names(fd, allow_execution=True)) == 5


@pytest.mark.parametrize("kind", ["symlink", "unknown", "fifo"])
def test_unsafe_execution_entries_fail_before_callbacks(tmp_path, kind):
    parts = fixture(tmp_path)
    folder = parts[0].ledger.root / ".execution"
    folder.mkdir(parents=True)
    if kind == "symlink":
        (folder / "lock").symlink_to(tmp_path / "outside")
    elif kind == "fifo":
        os.mkfifo(folder / "lock")
    else:
        (folder / "unexpected").write_text("synthetic")
    with pytest.raises((SnapshotIntegrityError, OSError)):
        run(parts)
    assert parts[3].executions == 0


def _hold_then_crash(path, connection):
    with execution_lock(path):
        connection.send("locked")
        connection.recv()
        os._exit(7)


def test_process_lock_is_nonblocking_and_released_after_crash(tmp_path):
    parts = fixture(tmp_path)
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_then_crash, args=(parts[0].ledger.root, child))
    process.start()
    try:
        assert parent.poll(5) and parent.recv() == "locked"
        with pytest.raises(RuntimeError, match="busy"):
            run(parts)
        parent.send("crash")
        process.join(5)
        assert process.exitcode == 7
        assert run(parts).status is SecExecutionStatus.READY
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
        child.close()


@pytest.mark.parametrize("steps", [0, True, 33])
def test_invalid_steps_rejected_before_creation(tmp_path, steps):
    parts = fixture(tmp_path)
    with pytest.raises(ValueError, match="max_steps"):
        run(parts, max_steps=steps)
    assert not parts[0].ledger.root.exists()

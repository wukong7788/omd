from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ohmydata.core import RequestSpec, SnapshotMode, SnapshotStore
from ohmydata.providers.sec import (
    SecDataVersionKind,
    SecEventWorkLedger,
    SecEventWorkSpec,
    SecExecutionOperation,
    SecExecutionRecovery,
    SecExecutionRecoveryResult,
    SecExecutionStatus,
    SecMetricRecomputeHandler,
    SecMetricRecomputeValidator,
    SecPitMode,
    SecTargetMetricInputs,
    build_recompute_work_spec,
    execute_sec_metric_recompute,
    retain_sec_execution_outputs,
    validate_recompute_target_binding,
)
from ohmydata.providers.sec._event_ledger_io import canonical_bytes
from tests.providers.sec.test_event_recompute import (
    TIME,
    build_graph_config,
    make_discovery_batch,
    not_started,
    target,
    valid_recompute_manifest,
    version,
)
from tests.providers.sec.test_quarter_ttm import _qualities, _versions


def test_recompute_missing_recovery_callbacks_and_unknown(tmp_path: Path) -> None:
    """Public interfaces must require explicit recovery callbacks; default NOT_STARTED is unsafe."""
    store = SnapshotStore(tmp_path / "store")
    ledger = SecEventWorkLedger(tmp_path / "ledger", store=store)
    batch, event = make_discovery_batch(store)
    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    inputs = SecTargetMetricInputs(build_graph_config(versions), versions, _qualities(versions))
    tgt = target(version("out"))

    # 1. Missing handler_recovery
    with pytest.raises(TypeError, match="explicit handler_recovery callback"):
        execute_sec_metric_recompute(
            ledger=ledger,
            store=store,
            batch=batch,
            event=event,
            target=tgt,
            inputs=inputs,
            deadline=TIME + timedelta(days=2),
            clock=lambda: TIME,
            handler_recovery=None,  # type: ignore[arg-type]
            validator_recovery=not_started,
        )

    # 2. Missing validator_recovery
    with pytest.raises(TypeError, match="explicit validator_recovery callback"):
        execute_sec_metric_recompute(
            ledger=ledger,
            store=store,
            batch=batch,
            event=event,
            target=tgt,
            inputs=inputs,
            deadline=TIME + timedelta(days=2),
            clock=lambda: TIME,
            handler_recovery=not_started,
            validator_recovery=None,  # type: ignore[arg-type]
        )

    spec = build_recompute_work_spec(
        event=event, target=tgt, inputs=inputs, processing_version="v1"
    )

    # 3. Handler constructor requires recovery
    with pytest.raises(TypeError, match="handler requires an explicit recovery callback"):
        SecMetricRecomputeHandler(
            store=store,
            target=tgt,
            inputs=inputs,
            spec=spec,
            version="v1",
            clock=lambda: TIME,
            recovery=None,  # type: ignore[arg-type]
        )

    # 4. Validator constructor requires recovery
    with pytest.raises(TypeError, match="validator requires an explicit recovery callback"):
        SecMetricRecomputeValidator(
            store=store,
            target=tgt,
            inputs=inputs,
            spec=spec,
            version="v1",
            clock=lambda: TIME,
            recovery=None,  # type: ignore[arg-type]
        )

    # 4b. Handler and Validator constructor require spec and inputs
    with pytest.raises(TypeError, match="spec must be SecEventWorkSpec"):
        SecMetricRecomputeHandler(
            store=store,
            target=tgt,
            inputs=inputs,
            spec=None,  # type: ignore[arg-type]
            version="v1",
            clock=lambda: TIME,
            recovery=not_started,
        )
    with pytest.raises(TypeError, match="inputs must be SecTargetMetricInputs"):
        SecMetricRecomputeValidator(
            store=store,
            target=tgt,
            inputs=None,  # type: ignore[arg-type]
            spec=spec,
            version="v1",
            clock=lambda: TIME,
            recovery=not_started,
        )

    # 5. UNKNOWN recovery callback halts with FAILED status
    res_unknown = execute_sec_metric_recompute(
        ledger=ledger,
        store=store,
        batch=batch,
        event=event,
        target=tgt,
        inputs=inputs,
        deadline=TIME + timedelta(days=2),
        clock=lambda: TIME,
        handler_recovery=lambda op: SecExecutionRecoveryResult(SecExecutionRecovery.UNKNOWN),
        validator_recovery=not_started,
    )
    assert res_unknown.status is SecExecutionStatus.FAILED
    assert res_unknown.execution_result.state is not None
    assert res_unknown.execution_result.state.state.value == "FAILED"


def test_recompute_malformed_recovered_result_fails(tmp_path: Path) -> None:
    """Recovered COMPLETE receipt with malformed payload, wrong target, or NaN must fail."""
    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    inputs = SecTargetMetricInputs(build_graph_config(versions), versions, _qualities(versions))
    tgt = target(version("out"))

    # Helper to forge an output receipt in a specific store
    def forge_receipt(
        store: SnapshotStore, spec: SecEventWorkSpec, op: SecExecutionOperation, manifest: dict
    ) -> object:
        req = RequestSpec(
            "sec",
            "metric-recompute-fact",
            {"work_identity": spec.work_identity, "target_identity": tgt.target_identity},
        )
        obs = store.observe(
            req,
            canonical_bytes(manifest),
            TIME,
            "sec-metric-recompute-fact-v1",
            SnapshotMode.FROZEN,
        )
        return retain_sec_execution_outputs(
            store=store, operation=op, outputs=(obs,), recorded_at=TIME
        )

    def make_manifest(spec: SecEventWorkSpec, **overrides) -> dict:
        return valid_recompute_manifest(spec, tgt, inputs.config, **overrides)

    # 1. Wrong schema
    store1 = SnapshotStore(tmp_path / "store1")
    _, event1 = make_discovery_batch(store1)
    spec1 = build_recompute_work_spec(
        event=event1, target=tgt, inputs=inputs, processing_version="sec-metric-recompute-v1"
    )
    op1 = SecExecutionOperation(spec1, 1, "sec-metric-recompute-validator-v1")
    rcpt_bad_schema = forge_receipt(
        store1,
        spec1,
        op1,
        make_manifest(spec1, schema="wrong-schema-v1"),
    )
    handler1 = SecMetricRecomputeHandler(
        store=store1,
        target=tgt,
        inputs=inputs,
        version="sec-metric-recompute-v1",
        clock=lambda: TIME,
        recovery=lambda _: SecExecutionRecoveryResult(
            SecExecutionRecovery.COMPLETE, rcpt_bad_schema
        ),
        spec=spec1,
    )
    with pytest.raises(ValueError, match="unexpected recompute output schema"):
        handler1.recover(op1)

    # 2. NaN final_value
    store2 = SnapshotStore(tmp_path / "store2")
    _, event2 = make_discovery_batch(store2)
    spec2 = build_recompute_work_spec(
        event=event2, target=tgt, inputs=inputs, processing_version="sec-metric-recompute-v1"
    )
    op2 = SecExecutionOperation(spec2, 1, "sec-metric-recompute-validator-v1")
    rcpt_nan = forge_receipt(
        store2,
        spec2,
        op2,
        make_manifest(spec2, final_value="NaN"),
    )
    handler_nan = SecMetricRecomputeHandler(
        store=store2,
        target=tgt,
        inputs=inputs,
        version="sec-metric-recompute-v1",
        clock=lambda: TIME,
        recovery=lambda _: SecExecutionRecoveryResult(SecExecutionRecovery.COMPLETE, rcpt_nan),
        spec=spec2,
    )
    with pytest.raises(ValueError, match="metric values must be finite Decimal values"):
        handler_nan.recover(op2)

    # 3. Target mismatch
    store3 = SnapshotStore(tmp_path / "store3")
    _, event3 = make_discovery_batch(store3)
    spec3 = build_recompute_work_spec(
        event=event3, target=tgt, inputs=inputs, processing_version="sec-metric-recompute-v1"
    )
    op3 = SecExecutionOperation(spec3, 1, "sec-metric-recompute-validator-v1")
    rcpt_wrong_target = forge_receipt(
        store3,
        spec3,
        op3,
        make_manifest(spec3, target_identity="f" * 64),
    )
    handler_target = SecMetricRecomputeHandler(
        store=store3,
        target=tgt,
        inputs=inputs,
        version="sec-metric-recompute-v1",
        clock=lambda: TIME,
        recovery=lambda _: SecExecutionRecoveryResult(
            SecExecutionRecovery.COMPLETE, rcpt_wrong_target
        ),
        spec=spec3,
    )
    with pytest.raises(ValueError, match="output target identity mismatch"):
        handler_target.recover(op3)


def test_recompute_all_input_edges_registered(tmp_path: Path) -> None:
    """Validator must register every declared input dependency within the 128 cap."""
    store = SnapshotStore(tmp_path / "store")
    ledger = SecEventWorkLedger(tmp_path / "ledger", store=store)
    batch, event = make_discovery_batch(store)
    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    inputs = SecTargetMetricInputs(build_graph_config(versions), versions, _qualities(versions))
    tgt = target(version("out"))

    res = execute_sec_metric_recompute(
        ledger=ledger,
        store=store,
        batch=batch,
        event=event,
        target=tgt,
        inputs=inputs,
        deadline=TIME + timedelta(days=2),
        clock=lambda: TIME,
        handler_recovery=not_started,
        validator_recovery=not_started,
    )
    assert res.status is SecExecutionStatus.READY

    _, _, registered_edges = ledger.load()
    # All 4 input fact versions must have registered dependency edges!
    assert len(registered_edges) == 4
    input_ids_in_edges = {e.input_version.identity for e in registered_edges}
    expected_input_ids = {v.normalized_version_id for v in versions}
    assert input_ids_in_edges == expected_input_ids


def test_recompute_mismatched_operation_rejected(tmp_path: Path) -> None:
    """Public handler.execute must bind supplied operation.spec to expected work."""
    store = SnapshotStore(tmp_path / "store")
    _, event = make_discovery_batch(store)
    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    inputs = SecTargetMetricInputs(build_graph_config(versions), versions, _qualities(versions))
    tgt = target(version("out"))

    expected_spec = build_recompute_work_spec(
        event=event, target=tgt, inputs=inputs, processing_version="sec-metric-recompute-v1"
    )
    handler = SecMetricRecomputeHandler(
        store=store,
        target=tgt,
        inputs=inputs,
        spec=expected_spec,
        version="sec-metric-recompute-v1",
        clock=lambda: TIME,
        recovery=not_started,
    )

    unrelated_spec = SecEventWorkSpec(
        event_key="e" * 64,
        metadata_digest="0" * 64,
        processing_version="sec-metric-recompute-v1",
        configuration_identity="0" * 64,
        input_version_ids=(version("input", SecDataVersionKind.NORMALIZED_FACT),),
        max_attempts=1,
    )
    unrelated_op = SecExecutionOperation(unrelated_spec, 1, "sec-metric-recompute-validator-v1")

    with pytest.raises(
        ValueError, match="operation spec does not match expected recompute work spec"
    ):
        handler.execute(unrelated_op)


def test_recompute_valuation_binding_including_none(tmp_path: Path) -> None:
    """Graph config valuation_at=None aligns with graph semantics and binds cleanly."""
    store = SnapshotStore(tmp_path / "store")
    batch, event = make_discovery_batch(store)
    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    qualities = _qualities(versions)

    # 1. Config with valuation_at=None binds cleanly to target with valuation_at=TIME
    config_none_val = build_graph_config(versions, valuation=None)
    assert config_none_val.valuation_at is None
    tgt = target(version("out"), valuation_at=TIME)
    inputs = SecTargetMetricInputs(config_none_val, versions, qualities)

    validate_recompute_target_binding(event=event, target=tgt, inputs=inputs)
    spec = build_recompute_work_spec(
        event=event, target=tgt, inputs=inputs, processing_version="sec-metric-recompute-v1"
    )
    assert spec is not None

    # Execution also succeeds with valuation_at=None
    ledger = SecEventWorkLedger(tmp_path / "ledger_none_val", store=store)
    res = execute_sec_metric_recompute(
        ledger=ledger,
        store=store,
        batch=batch,
        event=event,
        target=tgt,
        inputs=inputs,
        deadline=TIME + timedelta(days=2),
        clock=lambda: TIME,
        handler_recovery=not_started,
        validator_recovery=not_started,
    )
    assert res.status is SecExecutionStatus.READY
    assert res.final_value == Decimal("11.00")

    # 2. Config with mismatched valuation_at raises ValueError
    config_mismatch_val = build_graph_config(versions, valuation=TIME + timedelta(days=1))
    inputs_mismatch = SecTargetMetricInputs(config_mismatch_val, versions, qualities)
    with pytest.raises(ValueError, match="graph config valuation_at disagrees"):
        validate_recompute_target_binding(event=event, target=tgt, inputs=inputs_mismatch)

    # 3. Retrospective SYSTEM_REPLAY permits knowledge_cutoff <= valuation_at,
    # but cutoff later than valuation is rejected in every mode, including SYSTEM_REPLAY.
    t_val = datetime(2025, 1, 1, 12, tzinfo=UTC)
    t_cutoff = datetime(2025, 1, 5, 12, tzinfo=UTC)
    tgt_replay = target(
        version("out_replay"),
        valuation_at=t_val,
        cutoff=t_val,
        mode=SecPitMode.SYSTEM_REPLAY,
    )
    object.__setattr__(tgt_replay, "knowledge_cutoff", t_cutoff)
    config_replay = build_graph_config(
        versions, valuation=None, cutoff=t_cutoff, mode=SecPitMode.SYSTEM_REPLAY
    )
    inputs_replay = SecTargetMetricInputs(config_replay, versions, qualities)
    with pytest.raises(ValueError, match="target knowledge cutoff cannot exceed valuation time"):
        validate_recompute_target_binding(event=event, target=tgt_replay, inputs=inputs_replay)

    # 4. Valid SYSTEM_REPLAY where knowledge_cutoff <= valuation_at is admitted cleanly
    tgt_replay_valid = target(
        version("out_replay_valid"),
        valuation_at=t_val,
        cutoff=t_val,
        mode=SecPitMode.SYSTEM_REPLAY,
    )
    config_replay_valid = build_graph_config(
        versions, valuation=None, cutoff=t_val, mode=SecPitMode.SYSTEM_REPLAY
    )
    inputs_replay_valid = SecTargetMetricInputs(config_replay_valid, versions, qualities)
    validate_recompute_target_binding(
        event=event, target=tgt_replay_valid, inputs=inputs_replay_valid
    )
    spec_replay = build_recompute_work_spec(
        event=event,
        target=tgt_replay_valid,
        inputs=inputs_replay_valid,
        processing_version="sec-metric-recompute-v1",
    )
    assert spec_replay is not None

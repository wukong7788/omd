from decimal import Decimal
from pathlib import Path

import pytest

from ohmydata.core import RequestSpec, SnapshotMode, SnapshotStore
from ohmydata.providers.sec import (
    SecDataVersionKind,
    SecEventWorkSpec,
    SecExecutionOperation,
    SecExecutionRecovery,
    SecExecutionRecoveryResult,
    SecMetricRecomputeHandler,
    SecMetricRecomputeValidator,
    SecTargetMetricInputs,
    build_recompute_work_spec,
    retain_sec_execution_outputs,
)
from ohmydata.providers.sec._event_ledger_io import canonical_bytes
from tests.providers.sec.test_event_recompute import (
    TIME,
    build_graph_config,
    make_discovery_batch,
    not_started,
    target,
    version,
)
from tests.providers.sec.test_quarter_ttm import _qualities, _versions


def test_recompute_recovery_complete_invalid_observation(tmp_path: Path) -> None:
    """Exact regression: recovery returns COMPLETE with an invalid or malformed observation."""
    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    inputs = SecTargetMetricInputs(build_graph_config(versions), versions, _qualities(versions))
    tgt = target(version("out"))

    # 1. COMPLETE with receipt whose observation is missing final_unit
    store1 = SnapshotStore(tmp_path / "store1")
    _, event1 = make_discovery_batch(store1)
    spec1 = build_recompute_work_spec(
        event=event1, target=tgt, inputs=inputs, processing_version="sec-metric-recompute-v1"
    )
    op1 = SecExecutionOperation(spec1, 1, "sec-metric-recompute-validator-v1")
    req1 = RequestSpec(
        "sec",
        "metric-recompute-fact",
        {"work_identity": spec1.work_identity, "target_identity": tgt.target_identity},
    )
    m_no_unit = {
        "schema": "sec-metric-recompute-fact-v1",
        "work_identity": spec1.work_identity,
        "target_identity": tgt.target_identity,
        "target_recipe_identity": tgt.recipe_identity,
        "old_output_version": tgt.output_version.canonical_payload(),
        "graph_configuration_identity": inputs.config.configuration_identity,
        "claim": "OUTPUT_BYTES_REPLAYED",
        "result_identity": "0" * 64,
        "final_value_identity": "0" * 64,
        "final_value": "10.00",
        "final_currency": "USD",
        "terminal_identities": ["0" * 64],
        "step_identities": ["0" * 64],
        "recorded_at": "2025-01-01T12:00:00Z",
    }
    obs_invalid = store1.observe(
        req1,
        canonical_bytes(m_no_unit),
        TIME,
        "sec-metric-recompute-fact-v1",
        SnapshotMode.FROZEN,
    )
    rcpt_invalid = retain_sec_execution_outputs(
        store=store1, operation=op1, outputs=(obs_invalid,), recorded_at=TIME
    )

    handler_bad = SecMetricRecomputeHandler(
        store=store1,
        target=tgt,
        inputs=inputs,
        spec=spec1,
        version="sec-metric-recompute-v1",
        clock=lambda: TIME,
        recovery=lambda _: SecExecutionRecoveryResult(SecExecutionRecovery.COMPLETE, rcpt_invalid),
    )
    with pytest.raises(ValueError, match="manifest fields do not match exact expected set"):
        handler_bad.recover(op1)

    # 2. COMPLETE with receipt whose observation has wrong endpoint
    store2 = SnapshotStore(tmp_path / "store2")
    _, event2 = make_discovery_batch(store2)
    spec2 = build_recompute_work_spec(
        event=event2, target=tgt, inputs=inputs, processing_version="sec-metric-recompute-v1"
    )
    op2 = SecExecutionOperation(spec2, 1, "sec-metric-recompute-validator-v1")
    req_ep = RequestSpec(
        "sec",
        "wrong-ep",
        {"work_identity": spec2.work_identity, "target_identity": tgt.target_identity},
    )
    obs_wrong_ep = store2.observe(
        req_ep,
        canonical_bytes(m_no_unit),
        TIME,
        "sec-metric-recompute-fact-v1",
        SnapshotMode.FROZEN,
    )
    rcpt_wrong_ep = retain_sec_execution_outputs(
        store=store2, operation=op2, outputs=(obs_wrong_ep,), recorded_at=TIME
    )
    handler_wrong_ep = SecMetricRecomputeHandler(
        store=store2,
        target=tgt,
        inputs=inputs,
        spec=spec2,
        version="sec-metric-recompute-v1",
        clock=lambda: TIME,
        recovery=lambda _: SecExecutionRecoveryResult(SecExecutionRecovery.COMPLETE, rcpt_wrong_ep),
    )
    with pytest.raises(ValueError, match="invalid output observation endpoint"):
        handler_wrong_ep.recover(op2)

    # 3. COMPLETE with receipt whose observation has wrong mode (APPEND instead of FROZEN)
    store3 = SnapshotStore(tmp_path / "store3")
    _, event3 = make_discovery_batch(store3)
    spec3 = build_recompute_work_spec(
        event=event3, target=tgt, inputs=inputs, processing_version="sec-metric-recompute-v1"
    )
    op3 = SecExecutionOperation(spec3, 1, "sec-metric-recompute-validator-v1")
    req3 = RequestSpec(
        "sec",
        "metric-recompute-fact",
        {"work_identity": spec3.work_identity, "target_identity": tgt.target_identity},
    )
    valid_m = dict(m_no_unit, final_unit="USD")
    obs_append = store3.observe(
        req3,
        canonical_bytes(valid_m),
        TIME,
        "sec-metric-recompute-fact-v1",
        SnapshotMode.APPEND,
    )
    rcpt_append = retain_sec_execution_outputs(
        store=store3, operation=op3, outputs=(obs_append,), recorded_at=TIME
    )
    handler_append = SecMetricRecomputeHandler(
        store=store3,
        target=tgt,
        inputs=inputs,
        spec=spec3,
        version="sec-metric-recompute-v1",
        clock=lambda: TIME,
        recovery=lambda _: SecExecutionRecoveryResult(SecExecutionRecovery.COMPLETE, rcpt_append),
    )
    with pytest.raises(ValueError, match="invalid output observation mode: expected FROZEN"):
        handler_append.recover(op3)

    # 4. COMPLETE with receipt whose observation has wrong serialization
    store4 = SnapshotStore(tmp_path / "store4")
    _, event4 = make_discovery_batch(store4)
    spec4 = build_recompute_work_spec(
        event=event4, target=tgt, inputs=inputs, processing_version="sec-metric-recompute-v1"
    )
    op4 = SecExecutionOperation(spec4, 1, "sec-metric-recompute-validator-v1")
    obs_wrong_ser = store4.observe(
        req3,
        canonical_bytes(valid_m),
        TIME,
        "sec-metric-recompute-fact-v999",
        SnapshotMode.FROZEN,
    )
    rcpt_wrong_ser = retain_sec_execution_outputs(
        store=store4, operation=op4, outputs=(obs_wrong_ser,), recorded_at=TIME
    )
    handler_wrong_ser = SecMetricRecomputeHandler(
        store=store4,
        target=tgt,
        inputs=inputs,
        spec=spec4,
        version="sec-metric-recompute-v1",
        clock=lambda: TIME,
        recovery=lambda _: SecExecutionRecoveryResult(
            SecExecutionRecovery.COMPLETE, rcpt_wrong_ser
        ),
    )
    with pytest.raises(ValueError, match="invalid output observation serialization"):
        handler_wrong_ser.recover(op4)

    # 5. COMPLETE with receipt=None raises explicitly
    store5 = SnapshotStore(tmp_path / "store5")
    _, event5 = make_discovery_batch(store5)
    spec5 = build_recompute_work_spec(
        event=event5, target=tgt, inputs=inputs, processing_version="sec-metric-recompute-v1"
    )
    op5 = SecExecutionOperation(spec5, 1, "sec-metric-recompute-validator-v1")
    res_bad = SecExecutionRecoveryResult(SecExecutionRecovery.NOT_STARTED)
    object.__setattr__(res_bad, "status", SecExecutionRecovery.COMPLETE)
    object.__setattr__(res_bad, "receipt", None)
    handler_none_rcpt = SecMetricRecomputeHandler(
        store=store5,
        target=tgt,
        inputs=inputs,
        spec=spec5,
        version="sec-metric-recompute-v1",
        clock=lambda: TIME,
        recovery=lambda _: res_bad,
    )
    with pytest.raises(ValueError, match="COMPLETE recovery requires valid receipt observation"):
        handler_none_rcpt.recover(op5)


def test_recompute_direct_validator_and_handler_mismatched_specs(tmp_path: Path) -> None:
    """Exact regression: direct handler and validator methods reject mismatched operation specs."""
    store = SnapshotStore(tmp_path / "store")
    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    inputs = SecTargetMetricInputs(build_graph_config(versions), versions, _qualities(versions))
    tgt = target(version("out"))
    _, event = make_discovery_batch(store)
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
    validator = SecMetricRecomputeValidator(
        store=store,
        target=tgt,
        inputs=inputs,
        spec=expected_spec,
        version="sec-metric-recompute-validator-v1",
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
    mismatched_op = SecExecutionOperation(unrelated_spec, 1, "sec-metric-recompute-validator-v1")

    # 1. Handler execute with mismatched spec
    with pytest.raises(
        ValueError, match="operation spec does not match expected recompute work spec"
    ):
        handler.execute(mismatched_op)

    # 2. Handler recover with mismatched spec
    with pytest.raises(
        ValueError, match="operation spec does not match expected recompute work spec"
    ):
        handler.recover(mismatched_op)

    # 3. Validator validate with mismatched spec
    dummy_req = RequestSpec("sec", "dummy", {})
    dummy_obs = store.observe(dummy_req, b"{}", TIME, "dummy-v1", SnapshotMode.FROZEN)
    with pytest.raises(
        ValueError, match="operation spec does not match expected recompute work spec"
    ):
        validator.validate(mismatched_op, dummy_obs, (dummy_obs,))

    # 4. Validator recover with mismatched spec
    with pytest.raises(
        ValueError, match="operation spec does not match expected recompute work spec"
    ):
        validator.recover(mismatched_op)

from decimal import Decimal
from pathlib import Path

import pytest

from ohmydata.core import RequestSpec, SnapshotMode, SnapshotStore
from ohmydata.providers.sec import SecTargetMetricInputs, build_recompute_work_spec
from ohmydata.providers.sec._event_execution_receipts import ReceiptReplay
from ohmydata.providers.sec._event_ledger_io import canonical_bytes
from ohmydata.providers.sec._event_recompute_codec import (
    decode_and_validate_recompute_observation,
    decode_and_validate_recompute_result,
)
from ohmydata.providers.sec._metric_common import identity
from tests.providers.sec.test_event_recompute import (
    TIME,
    build_graph_config,
    digest,
    make_discovery_batch,
    target,
    valid_recompute_manifest,
    version,
)
from tests.providers.sec.test_quarter_ttm import _qualities, _versions


def test_recompute_codec_manifest_strict_regressions(tmp_path: Path) -> None:
    """Exact regressions for missing final_value, missing unit, extra fields, wrong schema/claim/unit."""
    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    inputs = SecTargetMetricInputs(build_graph_config(versions), versions, _qualities(versions))
    tgt = target(version("out"))
    store = SnapshotStore(tmp_path / "store")
    _, event = make_discovery_batch(store)
    spec = build_recompute_work_spec(
        event=event, target=tgt, inputs=inputs, processing_version="sec-metric-recompute-v1"
    )

    base_manifest = valid_recompute_manifest(spec, tgt, inputs.config)

    # Baseline: valid manifest passes
    valid_bytes = canonical_bytes(base_manifest)
    decoded = decode_and_validate_recompute_result(
        valid_bytes,
        expected_work_identity=spec.work_identity,
        expected_target=tgt,
        expected_config=inputs.config,
    )
    assert decoded["final_value"] == "10.00"

    # 1. Missing final_value key
    m_no_val = dict(base_manifest)
    del m_no_val["final_value"]
    with pytest.raises(ValueError, match="manifest fields do not match exact expected set"):
        decode_and_validate_recompute_result(
            canonical_bytes(m_no_val),
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
        )

    # 2. Missing final_unit key
    m_no_unit = dict(base_manifest)
    del m_no_unit["final_unit"]
    with pytest.raises(ValueError, match="manifest fields do not match exact expected set"):
        decode_and_validate_recompute_result(
            canonical_bytes(m_no_unit),
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
        )

    # 3. Extra fields
    m_extra = dict(base_manifest, unexpected_field="extra_data")
    with pytest.raises(ValueError, match="manifest fields do not match exact expected set"):
        decode_and_validate_recompute_result(
            canonical_bytes(m_extra),
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
        )

    # 4. Empty final_unit
    m_empty_unit = dict(base_manifest, final_unit="")
    with pytest.raises(ValueError, match="final_unit must be a nonempty bounded string"):
        decode_and_validate_recompute_result(
            canonical_bytes(m_empty_unit),
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
        )

    # 5. Non-string final_unit
    m_int_unit = dict(base_manifest, final_unit=42)
    with pytest.raises(ValueError, match="final_unit must be a nonempty bounded string"):
        decode_and_validate_recompute_result(
            canonical_bytes(m_int_unit),
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
        )

    # 6. Wrong schema
    m_wrong_schema = dict(base_manifest, schema="unexpected-schema-v1")
    with pytest.raises(ValueError, match="unexpected recompute output schema"):
        decode_and_validate_recompute_result(
            canonical_bytes(m_wrong_schema),
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
        )

    # 7. Wrong claim
    m_wrong_claim = dict(base_manifest, claim="UNVERIFIED")
    with pytest.raises(ValueError, match="unexpected recompute output claim"):
        decode_and_validate_recompute_result(
            canonical_bytes(m_wrong_claim),
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
        )

    m_empty_terminals = dict(base_manifest, terminal_identities=[])
    with pytest.raises(ValueError, match="terminal_identities must be a list with count"):
        decode_and_validate_recompute_result(
            canonical_bytes(m_empty_terminals),
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
        )

    duplicate_terminal = base_manifest["terminal_identities"][0]
    m_duplicate_terminals = dict(
        base_manifest,
        terminal_identities=[duplicate_terminal] * len(inputs.config.declarations),
    )
    with pytest.raises(ValueError, match="terminal_identities must contain unique identities"):
        decode_and_validate_recompute_result(
            canonical_bytes(m_duplicate_terminals),
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
        )

    m_duplicate_steps = dict(
        base_manifest,
        step_identities=[base_manifest["step_identities"][0]] * 2,
    )
    with pytest.raises(ValueError, match="step_identities must contain unique identities"):
        decode_and_validate_recompute_result(
            canonical_bytes(m_duplicate_steps),
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
        )

    m_wrong_final_id = dict(base_manifest, final_value_identity=digest("wrong-final"))
    with pytest.raises(ValueError, match="final_value_identity must equal the final step identity"):
        decode_and_validate_recompute_result(
            canonical_bytes(m_wrong_final_id),
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
        )

    m_wrong_result_id = dict(base_manifest, result_identity=digest("wrong-result"))
    with pytest.raises(ValueError, match="result_identity does not match canonical"):
        decode_and_validate_recompute_result(
            canonical_bytes(m_wrong_result_id),
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
        )

    m_short_terminals = dict(
        base_manifest, terminal_identities=base_manifest["terminal_identities"][:-1]
    )
    with pytest.raises(ValueError, match="terminal_identities count does not match"):
        decode_and_validate_recompute_result(
            canonical_bytes(m_short_terminals),
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
            expected_config=inputs.config,
        )

    extra_step = digest("extra-step")
    extended_steps = [*base_manifest["step_identities"], extra_step]
    m_extra_step = dict(
        base_manifest,
        step_identities=extended_steps,
        final_value_identity=extra_step,
    )
    m_extra_step["result_identity"] = identity(
        "sec-metric-graph-result-v1",
        {
            "configuration_identity": inputs.config.configuration_identity,
            "terminal_value_identities": tuple(m_extra_step["terminal_identities"]),
            "step_value_identities": tuple(extended_steps),
            "final_id": extra_step,
        },
    )
    with pytest.raises(ValueError, match="step_identities count does not match"):
        decode_and_validate_recompute_result(
            canonical_bytes(m_extra_step),
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
            expected_config=inputs.config,
        )

    m_invalid_currency = dict(base_manifest, final_currency=None)
    with pytest.raises(ValueError, match="final_currency must be a nonempty bounded string"):
        decode_and_validate_recompute_result(
            canonical_bytes(m_invalid_currency),
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
        )


def test_recompute_observation_strict_regressions(tmp_path: Path) -> None:
    """Exact regressions for wrong observation mode, wrong endpoint, wrong serialization."""
    store = SnapshotStore(tmp_path / "store")
    replay = ReceiptReplay(store)
    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    inputs = SecTargetMetricInputs(build_graph_config(versions), versions, _qualities(versions))
    tgt = target(version("out"))
    _, event = make_discovery_batch(store)
    spec = build_recompute_work_spec(
        event=event, target=tgt, inputs=inputs, processing_version="sec-metric-recompute-v1"
    )

    valid_manifest = {
        "schema": "sec-metric-recompute-fact-v1",
        "work_identity": spec.work_identity,
        "target_identity": tgt.target_identity,
        "target_recipe_identity": tgt.recipe_identity,
        "old_output_version": tgt.output_version.canonical_payload(),
        "graph_configuration_identity": inputs.config.configuration_identity,
        "claim": "OUTPUT_BYTES_REPLAYED",
        "result_identity": "0" * 64,
        "final_value_identity": "0" * 64,
        "final_value": "10.00",
        "final_unit": "USD",
        "final_currency": "USD",
        "terminal_identities": ["0" * 64],
        "step_identities": ["0" * 64],
        "recorded_at": "2025-01-01T12:00:00Z",
    }
    req = RequestSpec(
        "sec",
        "metric-recompute-fact",
        {"work_identity": spec.work_identity, "target_identity": tgt.target_identity},
    )

    # 1. Wrong observation mode (e.g. APPEND instead of FROZEN)
    obs_append = store.observe(
        req,
        canonical_bytes(valid_manifest),
        TIME,
        "sec-metric-recompute-fact-v1",
        SnapshotMode.APPEND,
    )
    with pytest.raises(ValueError, match="invalid output observation mode: expected FROZEN"):
        decode_and_validate_recompute_observation(
            obs_append,
            replay,
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
            expected_config=inputs.config,
        )

    # 2. Wrong endpoint
    req_wrong_ep = RequestSpec(
        "sec",
        "wrong-endpoint",
        {"work_identity": spec.work_identity, "target_identity": tgt.target_identity},
    )
    obs_wrong_ep = store.observe(
        req_wrong_ep,
        canonical_bytes(valid_manifest),
        TIME,
        "sec-metric-recompute-fact-v1",
        SnapshotMode.FROZEN,
    )
    with pytest.raises(ValueError, match="invalid output observation endpoint"):
        decode_and_validate_recompute_observation(
            obs_wrong_ep,
            replay,
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
            expected_config=inputs.config,
        )

    # 3. Wrong serialization version
    obs_wrong_ser = store.observe(
        req,
        canonical_bytes(valid_manifest),
        TIME,
        "sec-metric-recompute-fact-v999",
        SnapshotMode.FROZEN,
    )
    with pytest.raises(ValueError, match="invalid output observation serialization"):
        decode_and_validate_recompute_observation(
            obs_wrong_ser,
            replay,
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
            expected_config=inputs.config,
        )

    # 4. Wrong provider
    req_wrong_prov = RequestSpec(
        "other_provider",
        "metric-recompute-fact",
        {"work_identity": spec.work_identity, "target_identity": tgt.target_identity},
    )
    obs_wrong_prov = store.observe(
        req_wrong_prov,
        canonical_bytes(valid_manifest),
        TIME,
        "sec-metric-recompute-fact-v1",
        SnapshotMode.FROZEN,
    )
    with pytest.raises(ValueError, match="invalid output observation provider"):
        decode_and_validate_recompute_observation(
            obs_wrong_prov,
            replay,
            expected_work_identity=spec.work_identity,
            expected_target=tgt,
            expected_config=inputs.config,
        )

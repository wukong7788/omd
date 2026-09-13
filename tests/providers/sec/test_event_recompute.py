import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ohmydata.core import RequestSpec, SnapshotStore
from ohmydata.providers.sec import (
    SecDataVersionId,
    SecDataVersionKind,
    SecDatedInvalidationTarget,
    SecDependencyEdge,
    SecDiscoveryBatch,
    SecDiscoveryMode,
    SecDiscoveryPolicy,
    SecDiscoverySource,
    SecEventWorkLedger,
    SecEventWorkSpec,
    SecExecutionOperation,
    SecExecutionRecovery,
    SecExecutionRecoveryResult,
    SecExecutionStatus,
    SecFilingDiscoveryEvent,
    SecMetricGraphConfig,
    SecMetricStep,
    SecMetricTerminalDeclaration,
    SecPitMode,
    SecPitPolicy,
    SecTargetMetricInputs,
    discover_sec_filing_events,
    execute_sec_metric_recompute,
    validate_recompute_target_binding,
)
from ohmydata.providers.sec import (
    SecMetricRecipe as Recipe,
)
from ohmydata.providers.sec._metric_common import identity
from tests.providers.sec.test_quarter_ttm import _qualities, _versions

TIME = datetime(2025, 1, 1, 12, tzinfo=UTC)


def not_started(operation: SecExecutionOperation) -> SecExecutionRecoveryResult:
    return SecExecutionRecoveryResult(SecExecutionRecovery.NOT_STARTED)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def version(
    name: str, kind: SecDataVersionKind = SecDataVersionKind.DERIVED_METRIC
) -> SecDataVersionId:
    return SecDataVersionId(kind, digest(name))


def edge(
    source: SecDataVersionId,
    output: SecDataVersionId,
    *,
    cik: str = "1",
    recipe: str = "r",
    recorded_at: datetime = TIME,
) -> SecDependencyEdge:
    return SecDependencyEdge(source, output, cik, digest(recipe), recorded_at)


def target(
    output: SecDataVersionId,
    *,
    cik: str = "1",
    instrument: str = "class-a",
    recipe: str = "r",
    valuation_at: datetime = TIME,
    cutoff: datetime = TIME,
    mode: SecPitMode = SecPitMode.MARKET_KNOWN,
    recorded_at: datetime = TIME,
) -> SecDatedInvalidationTarget:
    return SecDatedInvalidationTarget(
        output,
        cik,
        instrument,
        digest(f"binding-{instrument}"),
        digest(recipe),
        valuation_at,
        cutoff,
        mode,
        recorded_at,
    )


def terminal_decl(v, name, quarter=1):
    return SecMetricTerminalDeclaration(
        name,
        "NORMALIZED_FACT",
        v.normalized_version_id,
        "REVENUE",
        "INDEPENDENT_QUARTER",
        2023,
        quarter,
        v.row.period_start,
        v.row.period_end,
        v.row.concept,
        "USD",
        "synthetic-GAAP",
        "company-common-equity",
        None,
        "synthetic-cohort",
        "synthetic-security",
        "synthetic-declaration",
    )


def build_graph_config(
    versions, *, cutoff=TIME, valuation=TIME, mode=SecPitMode.MARKET_KNOWN, cik="1"
):
    decls = tuple(terminal_decl(v, f"t{i}", i + 1) for i, v in enumerate(versions))
    steps = (
        SecMetricStep(
            "ttm", Recipe.FOUR_QUARTER_TTM_V1, ("t0", "t1", "t2", "t3"), None, None, None, None
        ),
    )
    return SecMetricGraphConfig(
        cik,
        decls,
        steps,
        mode,
        cutoff,
        SecPitPolicy(
            "sec-financial-normalized-v1",
            "adapter-v1",
            "normalization-v1",
            "c" * 64,
            "quality-v1",
            cutoff,
        ),
        valuation,
        "synthetic-scope",
    )


def make_discovery_batch(
    store: SnapshotStore, accession: str = "0000000001-24-000001"
) -> tuple[SecDiscoveryBatch, SecFilingDiscoveryEvent]:
    payload = {
        "cik": "0000000001",
        "filings": {
            "recent": {
                "accessionNumber": [accession],
                "act": ["34"],
                "form": ["10-Q"],
                "filingDate": ["2024-05-01"],
                "reportDate": ["2024-03-31"],
                "acceptanceDateTime": ["2024-05-01T10:00:00.000Z"],
                "fileNumber": ["001-00001"],
                "filmNumber": ["24000001"],
                "items": [""],
                "size": [1024],
                "isXBRL": [1],
                "isInlineXBRL": [1],
                "primaryDocument": ["doc.htm"],
                "primaryDocDescription": ["10-Q"],
            },
            "files": [],
        },
    }
    spec = RequestSpec("sec", "edgar_submissions", {"cik": "0000000001"})
    obs = store.observe(
        spec, __import__("json").dumps(payload).encode(), TIME, "sec-edgar-submissions-json-v1"
    )
    source = SecDiscoverySource(
        url="https://data.sec.gov/submissions/CIK0000000001.json", observation=obs
    )
    policy = SecDiscoveryPolicy(
        "1",
        ("10-Q",),
        TIME - timedelta(days=365),
        TIME + timedelta(days=365),
        timedelta(days=7),
        SecDiscoveryMode.INCREMENTAL,
        "synthetic-v1",
    )
    batch = discover_sec_filing_events(store, source, (), policy=policy, prior_cursor=None)
    return batch, batch.events[0]


def valid_recompute_manifest(
    spec: SecEventWorkSpec,
    tgt: SecDatedInvalidationTarget,
    config: SecMetricGraphConfig,
    **overrides,
) -> dict:
    terminals = [digest(f"terminal-{i}") for i in range(len(config.declarations))]
    steps = [digest(f"step-{i}") for i in range(len(config.steps))]
    final_id = steps[-1]
    result_id = identity(
        "sec-metric-graph-result-v1",
        {
            "configuration_identity": config.configuration_identity,
            "terminal_value_identities": tuple(terminals),
            "step_value_identities": tuple(steps),
            "final_id": final_id,
        },
    )
    manifest = {
        "schema": "sec-metric-recompute-fact-v1",
        "work_identity": spec.work_identity,
        "target_identity": tgt.target_identity,
        "target_recipe_identity": tgt.recipe_identity,
        "old_output_version": tgt.output_version.canonical_payload(),
        "graph_configuration_identity": config.configuration_identity,
        "claim": "OUTPUT_BYTES_REPLAYED",
        "result_identity": result_id,
        "final_value_identity": final_id,
        "final_value": "10.00",
        "final_unit": "USD",
        "final_currency": "USD",
        "terminal_identities": terminals,
        "step_identities": steps,
        "recorded_at": "2025-01-01T12:00:00Z",
    }
    manifest.update(overrides)
    return manifest


def test_recompute_real_compute_and_warm_zero_compute(tmp_path: Path, monkeypatch) -> None:
    store = SnapshotStore(tmp_path / "store")
    ledger_root = tmp_path / "ledger"
    ledger = SecEventWorkLedger(ledger_root, store=store)

    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    qualities = _qualities(versions)
    config = build_graph_config(versions)
    inputs = SecTargetMetricInputs(config, versions, qualities)

    batch, event = make_discovery_batch(store)
    out_id = version("ttm-metric")
    tgt = target(out_id)

    # 1. Cold execution
    res1 = execute_sec_metric_recompute(
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
    assert res1.status is SecExecutionStatus.READY
    assert res1.computed is True
    assert res1.final_value == Decimal("11.00")
    assert res1.result_identity is not None
    assert res1.replayed_payload is not None
    assert res1.replayed_payload["claim"] == "OUTPUT_BYTES_REPLAYED"

    # 2. Warm execution: compute_sec_metric_graph must NOT be called
    import ohmydata.providers.sec.event_recompute as mod

    called = 0
    orig_compute = mod.compute_sec_metric_graph

    def spy_compute(*args, **kwargs):
        nonlocal called
        called += 1
        return orig_compute(*args, **kwargs)

    monkeypatch.setattr(mod, "compute_sec_metric_graph", spy_compute)

    res2 = execute_sec_metric_recompute(
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
    assert called == 0  # Warm zero computation
    assert res2.status is SecExecutionStatus.READY
    assert res2.computed is False
    assert res2.final_value == Decimal("11.00")
    assert res2.result_identity == res1.result_identity


def test_recompute_changed_inputs_new_work(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "store")
    batch, event = make_discovery_batch(store)
    out_id = version("ttm-metric")
    tgt = target(out_id)

    # Work 1: original inputs (1.10 + 2.20 + 3.30 + 4.40 = 11.00)
    v1 = _versions(
        tmp_path / "facts1",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    inputs1 = SecTargetMetricInputs(build_graph_config(v1), v1, _qualities(v1))
    ledger1 = SecEventWorkLedger(tmp_path / "ledger1", store=store)
    res1 = execute_sec_metric_recompute(
        ledger=ledger1,
        store=store,
        batch=batch,
        event=event,
        target=tgt,
        inputs=inputs1,
        deadline=TIME + timedelta(days=2),
        clock=lambda: TIME,
        handler_recovery=not_started,
        validator_recovery=not_started,
    )
    assert res1.final_value == Decimal("11.00")

    # Work 2: changed input fact (5.00 + 2.20 + 3.30 + 4.40 = 14.90)
    v2 = _versions(
        tmp_path / "facts2",
        (Decimal("5.00"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    inputs2 = SecTargetMetricInputs(build_graph_config(v2), v2, _qualities(v2))
    assert inputs2.config.configuration_identity != inputs1.config.configuration_identity

    ledger2 = SecEventWorkLedger(tmp_path / "ledger2", store=store)
    res2 = execute_sec_metric_recompute(
        ledger=ledger2,
        store=store,
        batch=batch,
        event=event,
        target=tgt,
        inputs=inputs2,
        deadline=TIME + timedelta(days=2),
        clock=lambda: TIME,
        handler_recovery=not_started,
        validator_recovery=not_started,
    )
    assert res2.final_value == Decimal("14.90")
    assert res2.result_identity != res1.result_identity
    assert res2.spec.work_identity != res1.spec.work_identity


def test_recompute_mismatch_rejections(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "store")
    _, event = make_discovery_batch(store)
    versions = _versions(tmp_path / "facts")
    config = build_graph_config(versions)
    inputs = SecTargetMetricInputs(config, versions, _qualities(versions))

    # 1. CIK mismatch
    tgt_bad_cik = target(version("out"), cik="2")
    with pytest.raises(ValueError, match="event CIK disagrees"):
        validate_recompute_target_binding(event=event, target=tgt_bad_cik, inputs=inputs)

    # 2. Mode mismatch
    tgt_bad_mode = target(version("out"), mode=SecPitMode.SYSTEM_REPLAY)
    with pytest.raises(ValueError, match="graph config mode disagrees"):
        validate_recompute_target_binding(event=event, target=tgt_bad_mode, inputs=inputs)

    # 3. Knowledge cutoff mismatch
    tgt_bad_cutoff = target(version("out"), cutoff=TIME - timedelta(days=1))
    with pytest.raises(ValueError, match="graph config knowledge cutoff disagrees"):
        validate_recompute_target_binding(event=event, target=tgt_bad_cutoff, inputs=inputs)

    # 4. Valuation mismatch
    tgt_bad_val = target(version("out"), valuation_at=TIME + timedelta(days=1), cutoff=TIME)
    with pytest.raises(ValueError, match="graph config valuation_at disagrees"):
        validate_recompute_target_binding(event=event, target=tgt_bad_val, inputs=inputs)


def test_recompute_crash_recovery_using_existing_executor(tmp_path: Path, monkeypatch) -> None:
    store = SnapshotStore(tmp_path / "store")
    ledger_root = tmp_path / "ledger"
    ledger = SecEventWorkLedger(ledger_root, store=store)
    batch, event = make_discovery_batch(store)
    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    inputs = SecTargetMetricInputs(build_graph_config(versions), versions, _qualities(versions))
    tgt = target(version("out"))

    # Initial compute to obtain retained receipt
    res_cold = execute_sec_metric_recompute(
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
    retained_receipt = res_cold.execution_result.output_receipt

    # New work ledger simulating crash after handler retained output before VALIDATING advanced
    ledger_recovered = SecEventWorkLedger(tmp_path / "ledger_rec", store=store)
    import ohmydata.providers.sec.event_recompute as mod

    called = 0
    orig_compute = mod.compute_sec_metric_graph

    def fail_compute(*args, **kwargs):
        nonlocal called
        called += 1
        return orig_compute(*args, **kwargs)

    monkeypatch.setattr(mod, "compute_sec_metric_graph", fail_compute)

    # Handler recovery reports COMPLETE with retained receipt
    res_recovered = execute_sec_metric_recompute(
        ledger=ledger_recovered,
        store=store,
        batch=batch,
        event=event,
        target=tgt,
        inputs=inputs,
        deadline=TIME + timedelta(days=2),
        clock=lambda: TIME,
        handler_recovery=lambda op: SecExecutionRecoveryResult(
            SecExecutionRecovery.COMPLETE, retained_receipt
        ),
        validator_recovery=not_started,
    )
    assert called == 0  # Recomputation was bypassed via explicit COMPLETE recovery!
    assert res_recovered.status is SecExecutionStatus.READY
    assert res_recovered.final_value == Decimal("11.00")

    # UNKNOWN recovery reports UNCONFIRMED_SIDE_EFFECT failure without retrying
    ledger_unknown = SecEventWorkLedger(tmp_path / "ledger_unk", store=store)
    res_unknown = execute_sec_metric_recompute(
        ledger=ledger_unknown,
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

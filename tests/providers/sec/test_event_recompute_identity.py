import itertools
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ohmydata.core import RequestSpec, SnapshotStore
from ohmydata.core.errors import ResourceLimitError
from ohmydata.providers.sec import (
    SecConsumerCommit,
    SecDataVersionKind,
    SecDatedInputChange,
    SecDatedInvalidationIndex,
    SecDiscoveryMode,
    SecDiscoveryPolicy,
    SecDiscoverySource,
    SecEventWorkLedger,
    SecExecutionStatus,
    SecMetricExternalInput,
    SecQualityRecord,
    SecQualityStatus,
    SecTargetMetricInputs,
    build_recompute_work_spec,
    discover_sec_filing_events,
    execute_sec_metric_recompute,
    plan_sec_dated_invalidation,
)
from tests.providers.sec.test_event_recompute import (
    TIME,
    build_graph_config,
    digest,
    edge,
    make_discovery_batch,
    not_started,
    target,
    version,
)
from tests.providers.sec.test_quarter_ttm import _qualities, _versions


def test_late_and_reconcile_events_using_existing_discovery() -> None:
    raw_old = version("raw-old", SecDataVersionKind.RAW_FACT)
    out_id = version("derived-out")
    t_target = datetime(2025, 1, 1, 12, tzinfo=UTC)
    t_late = datetime(2025, 1, 2, 12, tzinfo=UTC)
    t_val = datetime(2025, 1, 4, 12, tzinfo=UTC)

    # Target 1 has cutoff at Jan 1 (prior to late data at Jan 2)
    t1 = target(out_id, valuation_at=t_val, cutoff=t_target)
    idx = SecDatedInvalidationIndex((edge(raw_old, out_id),), (t1,))

    chg_late = SecDatedInputChange(
        raw_old,
        None,
        "1",
        TIME - timedelta(days=1),
        TIME + timedelta(days=5),
        t_late,
        t_late,
        "caller-evidence",
        t_late,
    )

    # Invalidation planning at t_late preserves target cutoff: target is excluded (no lookahead!)
    plan = plan_sec_dated_invalidation(idx, (chg_late,), known_at=t_late + timedelta(hours=1))
    assert len(plan.targets) == 0

    # Target 2 has cutoff at Jan 2 13:00 (after late data): selected and eligible for recompute
    t2 = target(out_id, valuation_at=t_val, cutoff=t_late + timedelta(hours=1))
    idx2 = SecDatedInvalidationIndex((edge(raw_old, out_id),), (t2,))
    plan2 = plan_sec_dated_invalidation(idx2, (chg_late,), known_at=t_late + timedelta(hours=1))
    assert len(plan2.targets) == 1


def test_discovery_reconciliation_catches_prior_events(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "store")
    payload = {
        "cik": "0000000001",
        "filings": {
            "recent": {
                "accessionNumber": ["0000000001-24-000001", "0000000001-24-000002"],
                "act": ["34", "34"],
                "form": ["10-Q", "10-Q"],
                "filingDate": ["2024-01-10", "2024-05-10"],
                "reportDate": ["2023-12-31", "2024-03-31"],
                "acceptanceDateTime": ["2024-01-10T10:00:00.000Z", "2024-05-10T10:00:00.000Z"],
                "fileNumber": ["001-00001", "001-00001"],
                "filmNumber": ["24000001", "24000002"],
                "items": ["", ""],
                "size": [1024, 1024],
                "isXBRL": [1, 1],
                "isInlineXBRL": [1, 1],
                "primaryDocument": ["doc1.htm", "doc2.htm"],
                "primaryDocDescription": ["10-Q", "10-Q"],
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

    policy_incr = SecDiscoveryPolicy(
        "1",
        ("10-Q",),
        datetime(2024, 5, 1, tzinfo=UTC),
        datetime(2024, 5, 31, tzinfo=UTC),
        timedelta(days=2),
        SecDiscoveryMode.INCREMENTAL,
        "synthetic-v1",
    )
    b_incr = discover_sec_filing_events(store, source, (), policy=policy_incr, prior_cursor=None)
    assert len(b_incr.events) == 1
    assert b_incr.events[0].accession == "0000000001-24-000002"

    policy_recon = SecDiscoveryPolicy(
        "1",
        ("10-Q",),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 5, 31, tzinfo=UTC),
        timedelta(days=30),
        SecDiscoveryMode.RECONCILE,
        "synthetic-v1",
    )
    b_recon = discover_sec_filing_events(store, source, (), policy=policy_recon, prior_cursor=None)
    assert len(b_recon.events) == 2


# =====================================================================
# Regression tests covering defect fixes (1) through (8)
# =====================================================================


def test_recompute_quality_only_change_and_removed_quality(tmp_path: Path) -> None:
    """Changing or removing quality records must produce new work and never warm-reuse READY."""
    store = SnapshotStore(tmp_path / "store")
    batch, event = make_discovery_batch(store)
    out_id = version("ttm-metric")
    tgt = target(out_id)

    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    config = build_graph_config(versions)

    # 1. Base run with PASS quality records
    q_pass = _qualities(versions)
    inputs_pass = SecTargetMetricInputs(config, versions, q_pass)
    ledger = SecEventWorkLedger(tmp_path / "ledger", store=store)

    spec_pass = build_recompute_work_spec(
        event=event,
        target=tgt,
        inputs=inputs_pass,
        processing_version="sec-metric-recompute-v1",
    )
    res_pass = execute_sec_metric_recompute(
        ledger=ledger,
        store=store,
        batch=batch,
        event=event,
        target=tgt,
        inputs=inputs_pass,
        deadline=TIME + timedelta(days=2),
        clock=lambda: TIME,
        handler_recovery=not_started,
        validator_recovery=not_started,
    )
    assert res_pass.status is SecExecutionStatus.READY
    assert res_pass.computed is True

    # 2. Changed quality: updated recorded_at timestamp
    q_updated = tuple(
        SecQualityRecord(
            v.normalized_version_id,
            "quality-v1",
            SecQualityStatus.PASS,
            v.recorded_at + timedelta(minutes=1),
        )
        for v in versions
    )
    inputs_updated_q = SecTargetMetricInputs(config, versions, q_updated)
    spec_updated_q = build_recompute_work_spec(
        event=event,
        target=tgt,
        inputs=inputs_updated_q,
        processing_version="sec-metric-recompute-v1",
    )
    assert spec_updated_q.configuration_identity != spec_pass.configuration_identity
    assert spec_updated_q.work_identity != spec_pass.work_identity

    # Recompute with updated quality must compute anew on fresh ledger, not warm-reuse PASS
    ledger_updated_q = SecEventWorkLedger(tmp_path / "ledger_updated_q", store=store)
    res_updated_q = execute_sec_metric_recompute(
        ledger=ledger_updated_q,
        store=store,
        batch=batch,
        event=event,
        target=tgt,
        inputs=inputs_updated_q,
        deadline=TIME + timedelta(days=2),
        clock=lambda: TIME,
        handler_recovery=not_started,
        validator_recovery=not_started,
    )
    assert res_updated_q.computed is True
    assert res_updated_q.spec.work_identity == spec_updated_q.work_identity

    # 3. Removed quality: empty quality_records prevents warm reuse on existing ledger
    inputs_no_qual = SecTargetMetricInputs(config, versions, ())
    spec_no_qual = build_recompute_work_spec(
        event=event,
        target=tgt,
        inputs=inputs_no_qual,
        processing_version="sec-metric-recompute-v1",
    )
    assert spec_no_qual.configuration_identity != spec_pass.configuration_identity
    assert spec_no_qual.configuration_identity != spec_updated_q.configuration_identity
    assert spec_no_qual.work_identity != spec_pass.work_identity

    # Executing against ledger that contains READY for spec_pass must NEVER warm-reuse it
    from ohmydata.core.errors import SnapshotIntegrityError

    with pytest.raises(SnapshotIntegrityError, match="execution work/discovery identity mismatch"):
        execute_sec_metric_recompute(
            ledger=ledger,
            store=store,
            batch=batch,
            event=event,
            target=tgt,
            inputs=inputs_no_qual,
            deadline=TIME + timedelta(days=2),
            clock=lambda: TIME,
            handler_recovery=not_started,
            validator_recovery=not_started,
        )


def test_recompute_commit_only_change(tmp_path: Path) -> None:
    """Commit changes must change work identity and avoid warm reuse."""
    store = SnapshotStore(tmp_path / "store")
    _, event = make_discovery_batch(store)
    tgt = target(version("out"))
    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    qualities = _qualities(versions)
    config = build_graph_config(versions)

    commit1 = SecConsumerCommit(
        versions[0].normalized_version_id,
        qualities[0].quality_record_id,
        digest("consumer-dataset-1"),
        TIME,
    )
    inputs1 = SecTargetMetricInputs(config, versions, qualities, consumer_commits=(commit1,))
    spec1 = build_recompute_work_spec(
        event=event, target=tgt, inputs=inputs1, processing_version="sec-metric-recompute-v1"
    )

    commit2 = SecConsumerCommit(
        versions[0].normalized_version_id,
        qualities[0].quality_record_id,
        digest("consumer-dataset-2"),
        TIME,
    )
    inputs2 = SecTargetMetricInputs(config, versions, qualities, consumer_commits=(commit2,))
    spec2 = build_recompute_work_spec(
        event=event, target=tgt, inputs=inputs2, processing_version="sec-metric-recompute-v1"
    )

    assert spec1.configuration_identity != spec2.configuration_identity
    assert spec1.work_identity != spec2.work_identity


def test_recompute_external_input_change(tmp_path: Path) -> None:
    """External input additions or changes must produce new work identity."""
    store = SnapshotStore(tmp_path / "store")
    _, event = make_discovery_batch(store)
    tgt = target(version("out"))
    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    qualities = _qualities(versions)
    config = build_graph_config(versions)

    ext1 = SecMetricExternalInput(
        metric="COMPANY_MARKET_CAP",
        value=Decimal("1000000.00"),
        unit="USD",
        currency="USD",
        valuation_at=TIME,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 1),
        forecast_horizon=None,
        accounting_scope="consolidated",
        attribution_scope="attributable",
        security_basis="common",
        company_total_equity=True,
        source_reference="source-1",
        source_available_at=TIME,
        observation_identity="0" * 64,
        observed_at=TIME,
        recorded_at=TIME,
        quality_reference="qual-1",
        quality_recorded_at=TIME,
        commit_identity=None,
        committed_at=None,
    )
    inputs1 = SecTargetMetricInputs(config, versions, qualities, external_inputs=(ext1,))
    spec1 = build_recompute_work_spec(
        event=event, target=tgt, inputs=inputs1, processing_version="sec-metric-recompute-v1"
    )

    ext2 = SecMetricExternalInput(
        metric="COMPANY_MARKET_CAP",
        value=Decimal("2000000.00"),
        unit="USD",
        currency="USD",
        valuation_at=TIME,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 1),
        forecast_horizon=None,
        accounting_scope="consolidated",
        attribution_scope="attributable",
        security_basis="common",
        company_total_equity=True,
        source_reference="source-1",
        source_available_at=TIME,
        observation_identity="0" * 64,
        observed_at=TIME,
        recorded_at=TIME,
        quality_reference="qual-1",
        quality_recorded_at=TIME,
        commit_identity=None,
        committed_at=None,
    )
    inputs2 = SecTargetMetricInputs(config, versions, qualities, external_inputs=(ext2,))
    spec2 = build_recompute_work_spec(
        event=event, target=tgt, inputs=inputs2, processing_version="sec-metric-recompute-v1"
    )

    assert spec1.configuration_identity != spec2.configuration_identity
    assert spec1.work_identity != spec2.work_identity


def test_recompute_mutated_identity_rejected(tmp_path: Path) -> None:
    """Mutated nested objects must fail seal validation before warm path."""
    store = SnapshotStore(tmp_path / "store")
    _, event = make_discovery_batch(store)
    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    qualities = _qualities(versions)
    config = build_graph_config(versions)
    tgt = target(version("out"))

    # 1. Mutated target
    object.__setattr__(tgt, "target_identity", "f" * 64)
    inputs = SecTargetMetricInputs(config, versions, qualities)
    with pytest.raises(ValueError, match="target identity mismatch or mutated"):
        build_recompute_work_spec(
            event=event, target=tgt, inputs=inputs, processing_version="sec-metric-recompute-v1"
        )

    # 2. Mutated config
    tgt_valid = target(version("out"))
    object.__setattr__(config, "configuration_identity", "f" * 64)
    inputs_mut_config = SecTargetMetricInputs(config, versions, qualities)
    with pytest.raises(ValueError, match="graph config identity mismatch or mutated"):
        build_recompute_work_spec(
            event=event,
            target=tgt_valid,
            inputs=inputs_mut_config,
            processing_version="sec-metric-recompute-v1",
        )

    # 3. Mutated fact version
    config_valid = build_graph_config(versions)
    object.__setattr__(versions[0], "content_identity", "f" * 64)
    inputs_mut_ver = SecTargetMetricInputs(config_valid, versions, qualities)
    with pytest.raises(ValueError, match="normalized financial fact version mutated"):
        build_recompute_work_spec(
            event=event,
            target=tgt_valid,
            inputs=inputs_mut_ver,
            processing_version="sec-metric-recompute-v1",
        )

    # 4. Mutated quality record
    versions_valid = _versions(
        tmp_path / "facts2",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    qualities_mut = _qualities(versions_valid)
    object.__setattr__(qualities_mut[0], "quality_record_id", "f" * 64)
    inputs_mut_q = SecTargetMetricInputs(
        build_graph_config(versions_valid), versions_valid, qualities_mut
    )
    with pytest.raises(ValueError, match="quality record mutated or invalid seal"):
        build_recompute_work_spec(
            event=event,
            target=tgt_valid,
            inputs=inputs_mut_q,
            processing_version="sec-metric-recompute-v1",
        )


def test_recompute_bounded_generators_and_caps(tmp_path: Path) -> None:
    """Aggregate bounded iteration must bound generator consumption before materialization."""
    versions = _versions(
        tmp_path / "facts",
        (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40")),
    )
    qualities = _qualities(versions)
    config = build_graph_config(versions)

    # 1. Infinite generator in versions is bounded before materialization and raises ResourceLimitError
    with pytest.raises(ResourceLimitError, match="limit of 1024"):
        SecTargetMetricInputs(
            config,
            (v for _ in itertools.count() for v in (versions[0],)),
            qualities,
        )

    # 2. Aggregate limit across collections (e.g. 600 + 500 = 1100 > 1024)
    gen_v = (versions[0] for _ in range(600))
    gen_q = (qualities[0] for _ in range(500))
    with pytest.raises(ResourceLimitError, match="limit of 1024"):
        SecTargetMetricInputs(config, gen_v, gen_q)

    # 3. Exact typed elements enforced
    with pytest.raises(TypeError, match="versions elements must be exact"):
        SecTargetMetricInputs(config, ("not-a-fact-version",), ())

"""v2 finding closures retain history and verify every retained observation."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ohmydata.core import RequestSpec, SnapshotMode, SnapshotStore
from ohmydata.core.errors import SnapshotConflictError, SnapshotIntegrityError
from ohmydata.providers.sec import (
    SecPitMode,
    SecPitPolicy,
    SecQualityEvidenceRef,
    SecQualityFinding,
    SecQualityFindingStatus,
    SecQualityIssueClass,
    load_sec_pit_bundle,
    select_sec_financial_versions,
    select_sec_quality_findings,
    write_sec_pit_bundle,
)
from tests.providers.sec.test_pit_bundle import CONFIG, _records, _version


def _finding(version, *, evidence=None, **changes):
    ref = SecQualityEvidenceRef(
        version.observation.observation_identity, version.observation.fact_version
    )
    values = {
        "normalized_version_id": version.normalized_version_id,
        "affected_fields": ("concept",),
        "finding_key": "source",
        "rule_id": "rule",
        "rule_version": "v1",
        "adapter_version": version.adapter_version,
        "issue_class": SecQualityIssueClass.UNKNOWN,
        "missing_reason": None,
        "reason": "Synthetic caller assertion.",
        "evidence": (ref,) if evidence is None else evidence,
        "status": SecQualityFindingStatus.OPEN,
        "detected_at": datetime(2024, 5, 2, 10, tzinfo=UTC),
        "recorded_at": datetime(2024, 5, 2, 11, tzinfo=UTC),
    }
    values.update(changes)
    return SecQualityFinding(**values)


def _resolver(*observations):
    mapping = {item.observation_identity: item for item in observations}
    return lambda identity: mapping[identity]


def test_v1_default_writer_matches_independent_golden_and_loads(tmp_path: Path) -> None:
    version = _version(tmp_path / "source")
    quality, commit = _records(version)
    store = SnapshotStore(tmp_path / "bundle")
    ref = write_sec_pit_bundle(
        store=store,
        batch_identity="batch-1",
        versions=[version],
        quality_records=[quality],
        consumer_commits=[commit],
        captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
    )
    golden = Path("tests/fixtures/sec/pit_bundle_v1_golden.json").read_bytes()
    assert store.replay(ref).payload == golden.rstrip(b"\n")
    assert (
        load_sec_pit_bundle(
            store=store,
            bundle_ref=ref,
            source_store=SnapshotStore(tmp_path / "source"),
            resolve_observation=_resolver(version.observation),
        ).quality_findings
        == ()
    )


def test_v2_round_trip_history_ids_asof_and_shared_replay(tmp_path: Path) -> None:
    version = _version(tmp_path / "source")
    first = _finding(version)
    confirmed = _finding(
        version,
        status=SecQualityFindingStatus.CONFIRMED,
        reason="Synthetic confirmation.",
        adjudicated_at=datetime(2024, 5, 2, 12, tzinfo=UTC),
        recorded_at=datetime(2024, 5, 2, 12, tzinfo=UTC),
        supersedes_finding_id=first.finding_id,
    )
    dismissed = _finding(
        version,
        status=SecQualityFindingStatus.DISMISSED,
        reason="Synthetic dismissal.",
        adjudicated_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
        recorded_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
        supersedes_finding_id=confirmed.finding_id,
    )
    retracted = _finding(
        version,
        status=SecQualityFindingStatus.RETRACTED,
        reason="Synthetic retraction.",
        adjudicated_at=datetime(2024, 5, 2, 14, tzinfo=UTC),
        recorded_at=datetime(2024, 5, 2, 14, tzinfo=UTC),
        supersedes_finding_id=dismissed.finding_id,
    )
    replay_limits = []

    class CountedSource(SnapshotStore):
        def replay_observation(self, observation_ref, expected=None, max_payload_bytes=None):
            replay_limits.append(max_payload_bytes)
            return super().replay_observation(observation_ref, expected, max_payload_bytes)

    source = CountedSource(tmp_path / "source")
    bundle = SnapshotStore(tmp_path / "bundle")
    quality, commit = _records(version)
    calls = []
    resolver = lambda identity: calls.append(identity) or version.observation
    ref = write_sec_pit_bundle(
        store=bundle,
        batch_identity="v2",
        versions=[version],
        quality_records=[quality],
        consumer_commits=[commit],
        quality_findings=[retracted, first, confirmed, dismissed, first],
        source_store=source,
        resolve_observation=resolver,
        captured_at=datetime(2024, 5, 2, 15, tzinfo=UTC),
    )
    assert ref.serialization_identifier == "sec-pit-bundle-v2" and calls == [
        version.observation.observation_identity
    ]
    assert replay_limits == [8 * 1024 * 1024]
    replay_limits.clear()
    load_calls: list[str] = []
    loaded = load_sec_pit_bundle(
        store=SnapshotStore(tmp_path / "bundle"),
        bundle_ref=ref,
        source_store=CountedSource(tmp_path / "source"),
        resolve_observation=lambda identity: load_calls.append(identity) or version.observation,
    )
    assert load_calls == [version.observation.observation_identity]
    assert replay_limits == [8 * 1024 * 1024]
    assert {item.finding_id for item in loaded.quality_findings} == {
        first.finding_id,
        confirmed.finding_id,
        dismissed.finding_id,
        retracted.finding_id,
    }
    assert select_sec_quality_findings(
        loaded.quality_findings,
        normalized_version_id=version.normalized_version_id,
        rule_version="v1",
        cutoff=datetime(2024, 5, 2, 12, tzinfo=UTC),
    ) == (confirmed,)
    assert select_sec_quality_findings(
        loaded.quality_findings,
        normalized_version_id=version.normalized_version_id,
        rule_version="v1",
        cutoff=datetime(2024, 5, 2, 15, tzinfo=UTC),
    ) == (retracted,)
    history = [first, confirmed, dismissed, retracted]
    for hour in (10, 11, 12, 13, 14, 15):
        cutoff = datetime(2024, 5, 2, hour, tzinfo=UTC)
        assert select_sec_quality_findings(
            loaded.quality_findings,
            normalized_version_id=version.normalized_version_id,
            rule_version="v1",
            cutoff=cutoff,
        ) == select_sec_quality_findings(
            history,
            normalized_version_id=version.normalized_version_id,
            rule_version="v1",
            cutoff=cutoff,
        )
        policy = SecPitPolicy(
            version.schema_version,
            version.adapter_version,
            version.normalization_version,
            CONFIG,
            "quality-v1",
            cutoff,
        )
        for mode in (SecPitMode.MARKET_KNOWN, SecPitMode.SYSTEM_REPLAY):
            query = {"mode": mode, "knowledge_cutoff": cutoff, "policy": policy}
            expected = select_sec_financial_versions(
                [version],
                **query,
                quality_records=[quality],
                consumer_commits=[commit],
            )
            actual = select_sec_financial_versions(
                loaded.versions,
                **query,
                quality_records=loaded.quality_records,
                consumer_commits=loaded.consumer_commits,
            )
            assert actual == expected
            assert len(actual) == int(hour >= (12 if mode is SecPitMode.SYSTEM_REPLAY else 11))


def test_v2_rejects_evidence_mapping_fact_time_and_frozen_conflict(tmp_path: Path) -> None:
    version = _version(tmp_path / "source")
    source = SnapshotStore(tmp_path / "source")
    bundle = SnapshotStore(tmp_path / "bundle")
    finding = _finding(version)
    kwargs = {
        "store": bundle,
        "batch_identity": "v2",
        "versions": [version],
        "quality_records": [],
        "quality_findings": [finding],
        "source_store": source,
        "captured_at": datetime(2024, 5, 2, 13, tzinfo=UTC),
    }
    with pytest.raises(ValueError, match="resolver"):
        write_sec_pit_bundle(
            **kwargs,
            resolve_observation=lambda _: replace(
                version.observation, observation_identity="f" * 64
            ),
        )
    with pytest.raises(Exception, match="reference mismatch"):
        write_sec_pit_bundle(
            **kwargs,
            resolve_observation=lambda _: replace(version.observation, fact_version="f" * 64),
        )
    late = _finding(
        version,
        recorded_at=datetime(2024, 5, 2, 9, tzinfo=UTC),
        detected_at=datetime(2024, 5, 2, 9, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="precedes production"):
        write_sec_pit_bundle(
            **{**kwargs, "quality_findings": [late]},
            resolve_observation=_resolver(version.observation),
        )
    ref = write_sec_pit_bundle(**kwargs, resolve_observation=_resolver(version.observation))
    with pytest.raises(SnapshotConflictError):
        write_sec_pit_bundle(
            store=bundle,
            batch_identity="v2",
            versions=[version],
            quality_records=[],
            captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
        )
    assert load_sec_pit_bundle(
        store=bundle,
        bundle_ref=ref,
        source_store=source,
        resolve_observation=_resolver(version.observation),
    ).quality_findings == (finding,)


def test_v2_cross_provider_evidence_and_budget_are_verified(tmp_path: Path) -> None:
    version = _version(tmp_path / "source")
    source = SnapshotStore(tmp_path / "source")
    other = source.observe(
        RequestSpec("synthetic", "proof", {"id": "one"}),
        b"proof",
        datetime(2024, 5, 2, 10, 30, tzinfo=UTC),
        "synthetic-v1",
    )
    evidence = SecQualityEvidenceRef(other.observation_identity, other.fact_version)
    finding = _finding(version, evidence=(evidence,))
    ref = write_sec_pit_bundle(
        store=SnapshotStore(tmp_path / "bundle"),
        batch_identity="cross",
        versions=[version],
        quality_records=[],
        quality_findings=[finding],
        source_store=source,
        resolve_observation=_resolver(version.observation, other),
        captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
    )
    assert ref.serialization_identifier == "sec-pit-bundle-v2"
    loaded = load_sec_pit_bundle(
        store=SnapshotStore(tmp_path / "bundle"),
        bundle_ref=ref,
        source_store=SnapshotStore(tmp_path / "source"),
        resolve_observation=_resolver(version.observation, other),
    )
    assert loaded.quality_findings == (finding,)
    assert loaded.quality_findings[0].evidence == (evidence,)
    with pytest.raises(ValueError, match="record limit"):
        write_sec_pit_bundle(
            store=SnapshotStore(tmp_path / "small"),
            batch_identity="small",
            versions=[version],
            quality_records=[],
            quality_findings=[finding],
            source_store=source,
            resolve_observation=_resolver(version.observation, other),
            captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
            max_records=2,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("finding_id", "f" * 64),
        ("affected_fields", {"concept": 1}),
        ("issue_class", "UNKNOWN_ENUM"),
        ("detected_at", "2024-05-02T10:00:00"),
        ("evidence", [{"observation_identity": "f" * 64, "fact_version": "f" * 64}]),
    ],
)
def test_v2_hashed_malformed_findings_are_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    version = _version(tmp_path / "source")
    source = SnapshotStore(tmp_path / "source")
    bundle = SnapshotStore(tmp_path / "bundle")
    finding = _finding(version)
    ref = write_sec_pit_bundle(
        store=bundle,
        batch_identity="valid",
        versions=[version],
        quality_records=[],
        quality_findings=[finding],
        source_store=source,
        resolve_observation=_resolver(version.observation),
        captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
    )
    payload = json.loads(bundle.replay(ref).payload)
    payload["quality_findings"][0][field] = value
    bad_store = SnapshotStore(tmp_path / f"bad-{field}")
    bad = bad_store.write(
        RequestSpec("sec", "pit-bundle", {"batch_identity": "valid"}),
        json.dumps(payload, separators=(",", ":")).encode(),
        datetime(2024, 5, 2, 13, tzinfo=UTC),
        "sec-pit-bundle-v2",
        SnapshotMode.FROZEN,
    )
    with pytest.raises((TypeError, ValueError)):
        load_sec_pit_bundle(
            store=bad_store,
            bundle_ref=bad,
            source_store=source,
            resolve_observation=_resolver(version.observation),
        )


def test_v2_writer_rejects_orphan_fork_and_scope_change(tmp_path: Path) -> None:
    version = _version(tmp_path / "source")
    source = SnapshotStore(tmp_path / "source")
    first = _finding(version)
    root2 = _finding(version, reason="other root")
    child = _finding(
        version,
        status=SecQualityFindingStatus.CONFIRMED,
        reason="child",
        adjudicated_at=datetime(2024, 5, 2, 12, tzinfo=UTC),
        recorded_at=datetime(2024, 5, 2, 12, tzinfo=UTC),
        supersedes_finding_id=first.finding_id,
    )
    fork = _finding(
        version,
        status=SecQualityFindingStatus.DISMISSED,
        reason="fork",
        adjudicated_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
        recorded_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
        supersedes_finding_id=first.finding_id,
    )
    base = {
        "store": SnapshotStore(tmp_path / "bundle"),
        "batch_identity": "chains",
        "versions": [version],
        "quality_records": [],
        "source_store": source,
        "resolve_observation": _resolver(version.observation),
        "captured_at": datetime(2024, 5, 2, 14, tzinfo=UTC),
    }
    scope_changed = replace(child, affected_fields=("value_native",))
    for findings in ([child], [first, root2], [first, child, fork], [first, scope_changed]):
        with pytest.raises(ValueError):
            write_sec_pit_bundle(**base, quality_findings=findings)


def test_v2_writer_rejects_mutated_finding_identity(tmp_path: Path) -> None:
    version = _version(tmp_path / "source")
    finding = _finding(version)
    object.__setattr__(finding, "finding_id", "f" * 64)
    with pytest.raises(ValueError, match="identity"):
        write_sec_pit_bundle(
            store=SnapshotStore(tmp_path / "bundle"),
            batch_identity="mutated",
            versions=[version],
            quality_records=[],
            quality_findings=[finding],
            source_store=SnapshotStore(tmp_path / "source"),
            resolve_observation=_resolver(version.observation),
            captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
        )


def test_v2_retry_is_canonical_and_v1_to_v2_frozen_conflicts(tmp_path: Path) -> None:
    version = _version(tmp_path / "source")
    source = SnapshotStore(tmp_path / "source")
    bundle = SnapshotStore(tmp_path / "bundle")
    finding = _finding(version)
    args = {
        "store": bundle,
        "batch_identity": "same",
        "versions": [version],
        "quality_records": [],
        "quality_findings": [finding, finding],
        "source_store": source,
        "resolve_observation": _resolver(version.observation),
        "captured_at": datetime(2024, 5, 2, 13, tzinfo=UTC),
    }
    first = write_sec_pit_bundle(**args)
    repeated = write_sec_pit_bundle(**{**args, "captured_at": datetime(2024, 5, 2, 15, tzinfo=UTC)})
    assert first == repeated
    v1 = write_sec_pit_bundle(
        store=SnapshotStore(tmp_path / "v1"),
        batch_identity="same",
        versions=[version],
        quality_records=[],
        captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
    )
    with pytest.raises(SnapshotConflictError):
        write_sec_pit_bundle(
            store=SnapshotStore(tmp_path / "v1"),
            batch_identity="same",
            versions=[version],
            quality_records=[],
            quality_findings=[finding],
            source_store=source,
            resolve_observation=_resolver(version.observation),
            captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
        )
    assert v1.serialization_identifier == "sec-pit-bundle-v1"


def test_v2_loader_rejects_missing_and_corrupt_evidence(tmp_path: Path) -> None:
    version = _version(tmp_path / "source")
    source = SnapshotStore(tmp_path / "source")
    finding = _finding(version)
    bundle = SnapshotStore(tmp_path / "bundle")
    ref = write_sec_pit_bundle(
        store=bundle,
        batch_identity="evidence",
        versions=[version],
        quality_records=[],
        quality_findings=[finding],
        source_store=source,
        resolve_observation=_resolver(version.observation),
        captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
    )
    with pytest.raises(SnapshotIntegrityError):
        load_sec_pit_bundle(
            store=bundle,
            bundle_ref=ref,
            source_store=SnapshotStore(tmp_path / "missing"),
            resolve_observation=_resolver(version.observation),
        )
    path = next(source.root.rglob("response.bin"))
    path.write_bytes(b"corrupt")
    with pytest.raises(SnapshotIntegrityError):
        load_sec_pit_bundle(
            store=bundle,
            bundle_ref=ref,
            source_store=source,
            resolve_observation=_resolver(version.observation),
        )


def test_v2_aggregate_budget_stops_generator_before_unbounded_scan(tmp_path: Path) -> None:
    version = _version(tmp_path / "source")
    source = SnapshotStore(tmp_path / "source")
    consumed = 0

    def findings():
        nonlocal consumed
        while True:
            consumed += 1
            yield _finding(version, finding_key=f"key-{consumed}")

    with pytest.raises(ValueError, match="limit"):
        write_sec_pit_bundle(
            store=SnapshotStore(tmp_path / "bundle"),
            batch_identity="bounded",
            versions=[version],
            quality_records=[],
            quality_findings=findings(),
            source_store=source,
            resolve_observation=_resolver(version.observation),
            captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
            max_records=3,
        )
    assert consumed == 2

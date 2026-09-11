"""Recovery, immutable publication and query parity for receipt bundles."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.core import snapshot as snapshot_module
from ohmydata.core.errors import SnapshotConflictError
from ohmydata.providers.sec import (
    SecPitMode,
    SecPitPolicy,
    SecQualityRecord,
    SecQualityStatus,
    SecStatementRow,
    load_sec_pit_bundle,
    select_sec_financial_versions,
    write_sec_pit_bundle,
)
from tests.providers.sec.test_pit_bundle import CONFIG, _records, _version, _write


def test_frozen_retry_preserves_capture_and_changed_content_conflicts(tmp_path: Path) -> None:
    version, quality, commit, store, ref = _write(tmp_path)
    repeated = write_sec_pit_bundle(
        store=store,
        batch_identity="batch-1",
        versions=[version, version],
        quality_records=[quality, quality],
        consumer_commits=[commit, commit],
        captured_at=datetime(2024, 5, 2, 15, tzinfo=UTC),
    )
    assert repeated == ref
    assert store.replay(repeated).manifest["retrieved_at"] == "2024-05-02T13:00:00Z"
    with pytest.raises(SnapshotConflictError):
        write_sec_pit_bundle(
            store=store,
            batch_identity="batch-1",
            versions=[version],
            quality_records=[quality],
            consumer_commits=(),
            captured_at=datetime(2024, 5, 2, 15, tzinfo=UTC),
        )
    assert store.replay(ref).payload == store.replay(repeated).payload


@pytest.mark.parametrize("mode", [SecPitMode.MARKET_KNOWN, SecPitMode.SYSTEM_REPLAY])
def test_reopened_stores_preserve_queries_before_and_after_revocation(
    tmp_path: Path, mode: SecPitMode
) -> None:
    version = _version(tmp_path / "source")
    quality, commit = _records(version)
    revoked = SecQualityRecord(
        version.normalized_version_id,
        "quality-v1",
        SecQualityStatus.REVOKED,
        datetime(2024, 5, 2, 14, tzinfo=UTC),
        quality.quality_record_id,
    )
    store = SnapshotStore(tmp_path / "bundle")
    ref = write_sec_pit_bundle(
        store=store,
        batch_identity="revocation",
        versions=[version],
        quality_records=[revoked, quality],
        consumer_commits=[commit],
        captured_at=datetime(2024, 5, 2, 15, tzinfo=UTC),
    )
    del store
    loaded = load_sec_pit_bundle(
        store=SnapshotStore(tmp_path / "bundle"),
        bundle_ref=ref,
        source_store=SnapshotStore(tmp_path / "source"),
        resolve_observation=lambda _: version.observation,
    )
    for hour, count in [(13, 1), (15, 0)]:
        quality_cutoff = datetime(2024, 5, 2, hour, tzinfo=UTC)
        knowledge = (
            quality_cutoff
            if mode is SecPitMode.SYSTEM_REPLAY
            else datetime(2024, 5, 1, 22, tzinfo=UTC)
        )
        policy = SecPitPolicy(
            version.schema_version,
            version.adapter_version,
            version.normalization_version,
            CONFIG,
            "quality-v1",
            quality_cutoff,
        )
        expected = select_sec_financial_versions(
            [version],
            mode=mode,
            knowledge_cutoff=knowledge,
            policy=policy,
            quality_records=[quality, revoked],
            consumer_commits=[commit],
        )
        actual = select_sec_financial_versions(
            loaded.versions,
            mode=mode,
            knowledge_cutoff=knowledge,
            policy=policy,
            quality_records=loaded.quality_records,
            consumer_commits=loaded.consumer_commits,
        )
        assert len(actual) == count
        assert actual == expected


def test_shared_observation_is_resolved_and_replayed_once(tmp_path: Path) -> None:
    first = _version(tmp_path / "source")
    second = _version(tmp_path / "source", normalization_version="normalization-v2")
    assert first.observation == second.observation
    qualities = [_records(version)[0] for version in (first, second)]
    store = SnapshotStore(tmp_path / "bundle")
    ref = write_sec_pit_bundle(
        store=store,
        batch_identity="shared",
        versions=[second, first],
        quality_records=qualities,
        captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
    )
    counts = {"resolve": 0, "replay": 0}

    class CountedSource(SnapshotStore):
        def replay_observation(self, observation_ref, expected=None, max_payload_bytes=None):
            counts["replay"] += 1
            return super().replay_observation(observation_ref, expected, max_payload_bytes)

    def resolver(identity):
        assert identity == first.observation.observation_identity
        counts["resolve"] += 1
        return first.observation

    loaded = load_sec_pit_bundle(
        store=store,
        bundle_ref=ref,
        source_store=CountedSource(tmp_path / "source"),
        resolve_observation=resolver,
    )
    assert counts == {"resolve": 1, "replay": 1}
    assert {version.normalized_version_id for version in loaded.versions} == {
        first.normalized_version_id,
        second.normalized_version_id,
    }


@pytest.mark.parametrize("value", [None, Decimal("12345678901234567890.000000000000000012300")])
def test_row_native_precision_null_and_dimensions_survive_restart(tmp_path: Path, value) -> None:
    row = SecStatementRow(
        "income_statement",
        "Revenue",
        "us-gaap:Revenue",
        "Synthetic segment",
        value,
        None if value is None else str(value),
        "USD",
        -3,
        date(2023, 1, 1),
        date(2023, 3, 31),
        period_type="duration",
        dimension="SyntheticSegment",
        period_key="2023Q1",
        context_ref="synthetic-context",
        unit_ref="USD-unit",
        decimals_native="-3",
        period_source="native-context",
    )
    version = _version(tmp_path / "source", row=row)
    quality, _ = _records(version)
    store = SnapshotStore(tmp_path / "bundle")
    ref = write_sec_pit_bundle(
        store=store,
        batch_identity="precision",
        versions=[version],
        quality_records=[quality],
        captured_at=datetime(2024, 5, 2, 13, tzinfo=UTC),
    )
    loaded = load_sec_pit_bundle(
        store=SnapshotStore(store.root),
        bundle_ref=ref,
        source_store=SnapshotStore(tmp_path / "source"),
        resolve_observation=lambda _: version.observation,
    )
    restored = loaded.versions[0].row
    assert restored.to_dict() == row.to_dict()
    if value is not None:
        assert restored.value is not None
        assert restored.value.as_tuple() == value.as_tuple()


def test_concurrent_writes_and_interruption_preserve_valid_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = _version(tmp_path / "source")
    quality, _ = _records(version)
    store = SnapshotStore(tmp_path / "bundle")
    captured = datetime(2024, 5, 2, 13, tzinfo=UTC)

    def write(batch="concurrent", records=(quality,)):
        return write_sec_pit_bundle(
            store=store,
            batch_identity=batch,
            versions=[version],
            quality_records=records,
            captured_at=captured,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = list(pool.map(lambda _: write(), range(4)))
    assert all(ref == refs[0] for ref in refs)
    old_payload = store.replay(refs[0]).payload

    def conflict(records):
        try:
            return write("race", records)
        except SnapshotConflictError:
            return None

    changed = replace(quality, status=SecQualityStatus.QUARANTINED)
    with ThreadPoolExecutor(max_workers=2) as pool:
        raced = list(pool.map(conflict, [(quality,), (changed,)]))
    winners = [ref for ref in raced if ref is not None]
    assert len(winners) == 1
    store.replay(winners[0])

    def interrupt(*args, **kwargs):
        raise OSError("synthetic interrupted atomic publication")

    with monkeypatch.context() as patch:
        patch.setattr(snapshot_module.os, "rename", interrupt)
        with pytest.raises(OSError, match="synthetic interrupted"):
            write("interrupted")
    assert store.replay(refs[0]).payload == old_payload
    assert not list(store.root.rglob(".tmp-*"))
    recovered = write("interrupted")
    assert store.replay(recovered).payload

"""Durability and adversarial-replay coverage for observed SEC bundles."""

from __future__ import annotations

import json
import shutil
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ohmydata.core import RequestSpec, SnapshotMode, SnapshotStore
from ohmydata.core import snapshot as snapshot_module
from ohmydata.providers.sec import (
    SecObservedFinancialConsumerCommit,
    SecObservedFinancialQualityRecord,
    SecQualityStatus,
    load_sec_observed_financial_bundle,
    write_sec_observed_financial_bundle,
)
from tests.providers.sec.test_observed_xbrl_financials import _produce, _replay_policy

pytest.importorskip("edgar")


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def _closure(root: Path):
    production, source, source_ref, packages, package_ref, _, output = _produce(root / "input")
    policy = _replay_policy(production, production.produced_at + timedelta(minutes=5))
    quality = SecObservedFinancialQualityRecord(
        production.production_identity,
        policy.quality_policy_version,
        SecQualityStatus.PASS,
        production.produced_at,
    )
    commit = SecObservedFinancialConsumerCommit(
        production.production_identity,
        quality.quality_record_id,
        policy.consumer_dataset_identity,
        production.produced_at,
    )
    receipts = {
        source_ref.observation_identity: (source, source_ref),
        package_ref.observation_identity: (packages, package_ref),
        production.output_observation.observation_identity: (output, production.output_observation),
    }
    return production, quality, commit, receipts


def _write(store: SnapshotStore, batch: str, production, quality, commit, receipts, **limits):
    return write_sec_observed_financial_bundle(
        store=store,
        batch_identity=batch,
        productions=(production,),
        quality_records=(quality,),
        consumer_commits=(commit,),
        captured_at=datetime(2024, 5, 2, tzinfo=UTC),
        resolve_observation=receipts if callable(receipts) else receipts.__getitem__,
        **limits,
    )


def _semantic_bundle(store: SnapshotStore, original, batch: str, mutate):
    """Publish altered canonical JSON through SnapshotStore, preserving storage integrity."""
    data = json.loads(store.replay(original).payload)
    data["batch_identity"] = batch
    mutate(data)
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return store.write(
        RequestSpec("sec", "observed-financial-bundle", {"batch_identity": batch}),
        payload,
        datetime(2024, 5, 2, tzinfo=UTC),
        "sec-observed-financial-bundle-v1",
        SnapshotMode.FROZEN,
    )


def _altered_observation(root: Path, store: SnapshotStore, observation):
    replay = store.replay_observation(observation)
    request = replay.manifest["canonical_request"]
    return SnapshotStore(root).observe(
        RequestSpec(
            request["provider"],
            request["endpoint"],
            request["parameters"],
            tuple(request["fields"]),
        ),
        replay.payload + b" ",
        observation.snapshot_fetched_at,
        observation.serialization_identifier,
        observation.mode,
    )


def _receipt(observation):
    return {
        "observation_identity": observation.observation_identity,
        "snapshot_identity": observation.snapshot_identity,
        "fact_version": observation.fact_version,
        "mode": observation.mode.value,
        "provider": observation.provider,
        "endpoint": observation.endpoint,
        "request_identity": observation.request_identity,
        "response_sha256": observation.response_sha256,
        "serialization_identifier": observation.serialization_identifier,
        "snapshot_fetched_at": observation.snapshot_fetched_at.isoformat().replace("+00:00", "Z"),
    }


def _quality_receipt(record):
    return {
        "production_identity": record.production_identity,
        "quality_policy_version": record.quality_policy_version,
        "status": record.status.value,
        "recorded_at": record.recorded_at.isoformat().replace("+00:00", "Z"),
        "supersedes_quality_record_id": record.supersedes_quality_record_id,
        "quality_record_id": record.quality_record_id,
    }


def _commit_receipt(commit):
    return {
        "production_identity": commit.production_identity,
        "quality_record_id": commit.quality_record_id,
        "consumer_dataset_identity": commit.consumer_dataset_identity,
        "committed_at": commit.committed_at.isoformat().replace("+00:00", "Z"),
        "commit_id": commit.commit_id,
    }


def test_relocated_copied_dependency_roots_replay_without_original_paths(tmp_path):
    production, quality, commit, receipts = _closure(tmp_path)
    bundle_store = SnapshotStore(tmp_path / "bundle")
    ref = _write(bundle_store, "relocated", production, quality, commit, receipts)
    moved: dict[str, tuple[SnapshotStore, object]] = {}
    for index, (identity, (store, observation)) in enumerate(receipts.items()):
        destination = tmp_path / "moved" / str(index)
        shutil.copytree(store.root, destination)
        moved[identity] = (
            SnapshotStore(destination),
            replace(observation, path=destination / observation.path.relative_to(store.root)),
        )
    shutil.rmtree(tmp_path / "input")
    loaded = load_sec_observed_financial_bundle(
        store=SnapshotStore(bundle_store.root),
        bundle_ref=ref,
        resolve_observation=moved.__getitem__,
    )
    assert loaded.productions[0].production_identity == production.production_identity
    assert loaded.quality_records == (quality,)
    assert loaded.consumer_commits == (commit,)


def test_writer_deduplicates_equivalent_original_and_relocated_productions(tmp_path):
    production, quality, commit, receipts = _closure(tmp_path)
    original_store = SnapshotStore(tmp_path / "original-bundle")
    original = _write(original_store, "original", production, quality, commit, receipts)
    moved: dict[str, tuple[SnapshotStore, object]] = {}
    for index, (identity, (store, observation)) in enumerate(receipts.items()):
        destination = tmp_path / "relocated" / str(index)
        shutil.copytree(store.root, destination)
        moved[identity] = (
            SnapshotStore(destination),
            replace(observation, path=destination / observation.path.relative_to(store.root)),
        )
    relocated = load_sec_observed_financial_bundle(
        store=original_store, bundle_ref=original, resolve_observation=moved.__getitem__
    ).productions[0]
    mixed_store = SnapshotStore(tmp_path / "mixed-bundle")
    mixed = write_sec_observed_financial_bundle(
        store=mixed_store,
        batch_identity="path-free-duplicate",
        productions=(production, relocated),
        quality_records=(quality,),
        consumer_commits=(commit,),
        captured_at=datetime(2024, 5, 2, tzinfo=UTC),
        resolve_observation=lambda identity: moved.get(identity, receipts[identity]),
        max_productions=2,
        max_rows=2 * len(production.vintage.rows),
    )
    loaded = load_sec_observed_financial_bundle(
        store=mixed_store, bundle_ref=mixed, resolve_observation=moved.__getitem__
    )
    assert loaded.productions == (relocated,)


@pytest.mark.parametrize("dependency", ["source", "package", "output"])
def test_loader_rejects_missing_or_substituted_dependency_for_each_receipt(tmp_path, dependency):
    production, quality, commit, receipts = _closure(tmp_path)
    store = SnapshotStore(tmp_path / "bundle")
    ref = _write(store, f"{dependency}-base", production, quality, commit, receipts)
    identity = {
        "source": production.evidence.source_observation.observation_identity,
        "package": production.evidence.package_observation.observation_identity,
        "output": production.output_observation.observation_identity,
    }[dependency]
    absent = dict(receipts)
    absent.pop(identity)
    with pytest.raises(KeyError):
        load_sec_observed_financial_bundle(
            store=store, bundle_ref=ref, resolve_observation=absent.__getitem__
        )
    substitute = dict(receipts)
    substitute[identity] = next(value for key, value in receipts.items() if key != identity)
    with pytest.raises(ValueError, match="receipt mismatch"):
        load_sec_observed_financial_bundle(
            store=store, bundle_ref=ref, resolve_observation=substitute.__getitem__
        )


@pytest.mark.parametrize("dependency", ["source", "package", "output"])
def test_loader_rejects_hash_correct_altered_payload_for_each_dependency(tmp_path, dependency):
    production, quality, commit, receipts = _closure(tmp_path)
    store = SnapshotStore(tmp_path / "bundle")
    ref = _write(store, f"{dependency}-payload", production, quality, commit, receipts)
    identity = {
        "source": production.evidence.source_observation.observation_identity,
        "package": production.evidence.package_observation.observation_identity,
        "output": production.output_observation.observation_identity,
    }[dependency]
    old_store, old_observation = receipts[identity]
    changed = _altered_observation(tmp_path / f"altered-{dependency}", old_store, old_observation)
    altered = dict(receipts)
    altered[identity] = (SnapshotStore(tmp_path / f"altered-{dependency}"), changed)
    with pytest.raises(ValueError, match="receipt mismatch"):
        load_sec_observed_financial_bundle(
            store=store, bundle_ref=ref, resolve_observation=altered.__getitem__
        )


def test_loader_rebuild_rejects_altered_output_with_matching_canonical_receipt(tmp_path):
    production, quality, commit, receipts = _closure(tmp_path)
    store = SnapshotStore(tmp_path / "bundle")
    original = _write(store, "output-base", production, quality, commit, receipts)
    old_store, old_output = receipts[production.output_observation.observation_identity]
    altered_output = _altered_observation(tmp_path / "altered-output", old_store, old_output)
    altered_store = SnapshotStore(tmp_path / "altered-output")

    def mutate(data):
        data["productions"][0]["output_receipt"] = _receipt(altered_output)

    bad = _semantic_bundle(store, original, "output-rebuild-mismatch", mutate)
    resolver = dict(receipts)
    resolver[altered_output.observation_identity] = (altered_store, altered_output)
    with pytest.raises(ValueError, match="output"):
        load_sec_observed_financial_bundle(
            store=store, bundle_ref=bad, resolve_observation=resolver.__getitem__
        )


def test_loader_rejects_hash_correct_complete_but_orphaned_quality_graph(tmp_path):
    production, quality, commit, receipts = _closure(tmp_path)
    store = SnapshotStore(tmp_path / "bundle")
    original = _write(store, "graph-base", production, quality, commit, receipts)
    orphan = SecObservedFinancialQualityRecord(
        production.production_identity,
        quality.quality_policy_version,
        SecQualityStatus.PASS,
        quality.recorded_at,
        "0" * 64,
    )
    rebound = SecObservedFinancialConsumerCommit(
        production.production_identity,
        orphan.quality_record_id,
        commit.consumer_dataset_identity,
        commit.committed_at,
    )

    def mutate(data):
        data["quality_records"] = [_quality_receipt(orphan)]
        data["consumer_commits"] = [_commit_receipt(rebound)]

    bad = _semantic_bundle(store, original, "orphaned-graph", mutate)
    with pytest.raises(ValueError, match="invalid first quality"):
        load_sec_observed_financial_bundle(
            store=store, bundle_ref=bad, resolve_observation=receipts.__getitem__
        )


@pytest.mark.parametrize("field", ["source_receipt", "package_receipt", "output_receipt"])
def test_loader_rejects_hash_correct_semantic_receipt_tampering(tmp_path, field):
    production, quality, commit, receipts = _closure(tmp_path)
    store = SnapshotStore(tmp_path / "bundle")
    original = _write(store, "receipt-base", production, quality, commit, receipts)

    def mutate(data):
        data["productions"][0][field]["endpoint"] = "wrong-endpoint"

    bad = _semantic_bundle(store, original, f"bad-{field}", mutate)
    with pytest.raises(ValueError, match="receipt mismatch"):
        load_sec_observed_financial_bundle(
            store=store, bundle_ref=bad, resolve_observation=receipts.__getitem__
        )


@pytest.mark.parametrize(
    "kind", ["unknown-field", "production-id", "quality-id", "commit-id", "capture"]
)
def test_loader_rejects_hash_correct_bad_envelope_and_derived_ids(tmp_path, kind):
    production, quality, commit, receipts = _closure(tmp_path)
    store = SnapshotStore(tmp_path / "bundle")
    original = _write(store, "envelope-base", production, quality, commit, receipts)

    def mutate(data):
        if kind == "unknown-field":
            data["unexpected"] = True
        elif kind == "production-id":
            data["productions"][0]["production_identity"] = "0" * 64
        elif kind == "quality-id":
            data["quality_records"][0]["quality_record_id"] = "0" * 64
        elif kind == "commit-id":
            data["consumer_commits"][0]["commit_id"] = "0" * 64
        else:
            data["captured_at"] = "2024-05-01T00:00:00Z"

    bad = _semantic_bundle(store, original, f"bad-{kind}", mutate)
    with pytest.raises((TypeError, ValueError)):
        load_sec_observed_financial_bundle(
            store=store, bundle_ref=bad, resolve_observation=receipts.__getitem__
        )


def test_writer_validates_unselected_policy_and_dataset_graphs_before_write(tmp_path):
    production, quality, commit, receipts = _closure(tmp_path)
    store = SnapshotStore(tmp_path / "bundle")
    foreign_policy = SecObservedFinancialQualityRecord(
        production.production_identity,
        "foreign-policy",
        SecQualityStatus.PASS,
        production.produced_at,
    )
    orphan_commit = SecObservedFinancialConsumerCommit(
        production.production_identity,
        foreign_policy.quality_record_id,
        "b" * 64,
        production.produced_at,
    )
    with pytest.raises(ValueError, match="invalid dependency"):
        write_sec_observed_financial_bundle(
            store=store,
            batch_identity="unselected-orphan",
            productions=(production,),
            quality_records=(quality,),
            consumer_commits=(commit, orphan_commit),
            captured_at=datetime(2024, 5, 2, tzinfo=UTC),
            resolve_observation=receipts.__getitem__,
        )
    fork_a = SecObservedFinancialQualityRecord(
        production.production_identity,
        "foreign-policy",
        SecQualityStatus.PASS,
        production.produced_at,
    )
    fork_b = SecObservedFinancialQualityRecord(
        production.production_identity,
        "foreign-policy",
        SecQualityStatus.REVOKED,
        production.produced_at + timedelta(seconds=1),
        fork_a.quality_record_id,
    )
    fork_c = SecObservedFinancialQualityRecord(
        production.production_identity,
        "foreign-policy",
        SecQualityStatus.PASS,
        production.produced_at + timedelta(seconds=2),
        fork_a.quality_record_id,
    )
    with pytest.raises(ValueError, match="chronological chain"):
        write_sec_observed_financial_bundle(
            store=store,
            batch_identity="unselected-fork",
            productions=(production,),
            quality_records=(quality, fork_a, fork_b, fork_c),
            consumer_commits=(commit,),
            captured_at=datetime(2024, 5, 2, tzinfo=UTC),
            resolve_observation=receipts.__getitem__,
        )
    assert not list(store.root.rglob("response.bin"))


def test_concurrent_bundle_writes_and_rename_interruption_leave_only_valid_snapshots(
    tmp_path, monkeypatch
):
    production, quality, commit, receipts = _closure(tmp_path)
    store = SnapshotStore(tmp_path / "bundle")

    def write(batch="race"):
        return _write(store, batch, production, quality, commit, receipts)

    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = list(pool.map(lambda _: write(), range(4)))
    assert all(ref == refs[0] for ref in refs)
    original = store.replay(refs[0]).payload

    def interrupt(*args, **kwargs):
        raise OSError("synthetic rename interruption")

    with monkeypatch.context() as patch:
        patch.setattr(snapshot_module.os, "rename", interrupt)
        with pytest.raises(OSError, match="synthetic rename interruption"):
            write("interrupted")
    assert store.replay(refs[0]).payload == original
    assert not list(store.root.rglob(".tmp-*"))
    recovered = write("interrupted")
    assert (
        load_sec_observed_financial_bundle(
            store=store, bundle_ref=recovered, resolve_observation=receipts.__getitem__
        )
        .productions[0]
        .production_identity
        == production.production_identity
    )

"""Offline restart coverage for observed financial lifecycle bundles."""

import json
import socket
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ohmydata.core import RequestSpec, SnapshotConflictError, SnapshotMode, SnapshotStore
from ohmydata.providers.sec import (
    SecObservedFinancialConsumerCommit,
    SecObservedFinancialQualityRecord,
    SecQualityStatus,
    load_sec_observed_financial_bundle,
    select_sec_observed_financial_productions,
    write_sec_observed_financial_bundle,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_observed_xbrl_financials import _produce, _replay_policy

pytest.importorskip("edgar")


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def _records(production):
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
    return policy, quality, commit


def test_round_trip_rebuilds_identical_observed_output_without_loader_writes(tmp_path, monkeypatch):
    production, source, source_ref, packages, package_ref, _, output = _produce(tmp_path / "input")
    policy, quality, commit = _records(production)
    foreign_quality = SecObservedFinancialQualityRecord(
        production.production_identity,
        "observed-quality-v2",
        SecQualityStatus.PASS,
        production.produced_at,
    )
    foreign_commit = SecObservedFinancialConsumerCommit(
        production.production_identity,
        foreign_quality.quality_record_id,
        policy.consumer_dataset_identity,
        production.produced_at,
    )
    receipts = {
        source_ref.observation_identity: (source, source_ref),
        package_ref.observation_identity: (packages, package_ref),
        production.output_observation.observation_identity: (output, production.output_observation),
    }
    bundle_store = SnapshotStore(tmp_path / "bundle")
    bundle_ref = write_sec_observed_financial_bundle(
        store=bundle_store,
        batch_identity="offline-restart",
        productions=(production,),
        quality_records=(quality, foreign_quality),
        consumer_commits=(commit, foreign_commit),
        captured_at=datetime(2024, 5, 2, tzinfo=UTC),
        resolve_observation=receipts.__getitem__,
    )
    golden = output.replay_observation(production.output_observation).payload
    monkeypatch.setattr(SnapshotStore, "write", lambda *_: pytest.fail("loader wrote"))
    loaded = load_sec_observed_financial_bundle(
        store=bundle_store, bundle_ref=bundle_ref, resolve_observation=receipts.__getitem__
    )
    assert loaded.productions[0].production_identity == production.production_identity
    assert loaded.productions[0].vintage.vintage_identity == production.vintage.vintage_identity
    assert output.replay_observation(production.output_observation).payload == golden
    assert select_sec_observed_financial_productions(
        loaded.productions, loaded.quality_records, loaded.consumer_commits, policy
    ) == select_sec_observed_financial_productions((production,), (quality,), (commit,), policy)
    foreign_policy = replace(policy, quality_policy_version="observed-quality-v2")
    assert (
        select_sec_observed_financial_productions(
            loaded.productions, loaded.quality_records, loaded.consumer_commits, foreign_policy
        )[0].commit
        == foreign_commit
    )


def test_writer_failure_does_not_create_bundle(tmp_path):
    production, source, source_ref, packages, package_ref, _, output = _produce(tmp_path / "input")
    _, quality, commit = _records(production)
    receipts = {
        source_ref.observation_identity: (source, source_ref),
        package_ref.observation_identity: (packages, package_ref),
        production.output_observation.observation_identity: (output, production.output_observation),
    }
    store = SnapshotStore(tmp_path / "bundle")
    with pytest.raises(ValueError, match="captured_at precedes"):
        write_sec_observed_financial_bundle(
            store=store,
            batch_identity="too-early",
            productions=(production,),
            quality_records=(quality,),
            consumer_commits=(commit,),
            captured_at=production.produced_at - timedelta(seconds=1),
            resolve_observation=receipts.__getitem__,
        )
    assert not list(store.root.rglob("response.bin"))


def test_same_dataset_foreign_policy_commit_is_ignored_but_bad_binding_fails(tmp_path):
    production, *_ = _produce(tmp_path / "input")
    policy, quality, commit = _records(production)
    foreign = SecObservedFinancialQualityRecord(
        production.production_identity,
        "observed-quality-v2",
        SecQualityStatus.PASS,
        production.produced_at,
    )
    foreign_commit = SecObservedFinancialConsumerCommit(
        production.production_identity,
        foreign.quality_record_id,
        policy.consumer_dataset_identity,
        production.produced_at,
    )
    selected = select_sec_observed_financial_productions(
        (production,), (quality, foreign), (commit, foreign_commit), policy
    )
    assert selected[0].commit == commit
    orphan = SecObservedFinancialConsumerCommit(
        production.production_identity,
        "0" * 64,
        policy.consumer_dataset_identity,
        production.produced_at,
    )
    with pytest.raises(ValueError, match="visible selected-policy"):
        select_sec_observed_financial_productions(
            (production,), (quality, foreign), (orphan,), policy
        )


def test_writer_rejects_tampered_vintage_before_bundle_write(tmp_path):
    production, source, source_ref, packages, package_ref, _, output = _produce(tmp_path / "input")
    _, quality, commit = _records(production)
    receipts = {
        source_ref.observation_identity: (source, source_ref),
        package_ref.observation_identity: (packages, package_ref),
        production.output_observation.observation_identity: (output, production.output_observation),
    }
    original = production.vintage.vintage_identity
    object.__setattr__(production.vintage, "vintage_identity", "0" * 64)
    with pytest.raises(ValueError, match="vintage_identity"):
        write_sec_observed_financial_bundle(
            store=SnapshotStore(tmp_path / "bundle"),
            batch_identity="tampered",
            productions=(production,),
            quality_records=(quality,),
            consumer_commits=(commit,),
            captured_at=datetime(2024, 5, 2, tzinfo=UTC),
            resolve_observation=receipts.__getitem__,
        )
    assert production.vintage.vintage_identity == "0" * 64
    object.__setattr__(production.vintage, "vintage_identity", original)


def test_loader_rejects_noncanonical_array_order_and_budget_preflights(tmp_path):
    production, source, source_ref, packages, package_ref, _, output = _produce(tmp_path / "input")
    _, quality, commit = _records(production)
    receipts = {
        source_ref.observation_identity: (source, source_ref),
        package_ref.observation_identity: (packages, package_ref),
        production.output_observation.observation_identity: (output, production.output_observation),
    }
    store = SnapshotStore(tmp_path / "bundle")
    with pytest.raises(ValueError, match="byte limit"):
        write_sec_observed_financial_bundle(
            store=store,
            batch_identity="tiny",
            productions=(production,),
            quality_records=(quality,),
            consumer_commits=(commit,),
            captured_at=datetime(2024, 5, 2, tzinfo=UTC),
            resolve_observation=lambda _: pytest.fail("resolver called"),
            max_bundle_bytes=1,
        )
    ref = write_sec_observed_financial_bundle(
        store=store,
        batch_identity="valid",
        productions=(production,),
        quality_records=(quality,),
        consumer_commits=(commit,),
        captured_at=datetime(2024, 5, 2, tzinfo=UTC),
        resolve_observation=receipts.__getitem__,
    )
    data = json.loads(store.replay(ref).payload)
    data["batch_identity"] = "bad-order"
    data["productions"] = data["productions"] * 2
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    bad = store.write(
        RequestSpec("sec", "observed-financial-bundle", {"batch_identity": "bad-order"}),
        payload,
        datetime(2024, 5, 2, tzinfo=UTC),
        "sec-observed-financial-bundle-v1",
        SnapshotMode.FROZEN,
    )
    with pytest.raises(ValueError, match="noncanonical"):
        load_sec_observed_financial_bundle(
            store=store, bundle_ref=bad, resolve_observation=receipts.__getitem__
        )


def test_empty_idempotent_and_frozen_collision(tmp_path):
    store = SnapshotStore(tmp_path / "bundle")
    captured = datetime(2024, 5, 2, tzinfo=UTC)
    kwargs = {
        "store": store,
        "batch_identity": "empty",
        "productions": (),
        "quality_records": (),
        "consumer_commits": (),
        "captured_at": captured,
        "resolve_observation": lambda _: pytest.fail("empty resolved a dependency"),
    }
    first = write_sec_observed_financial_bundle(**kwargs)
    assert write_sec_observed_financial_bundle(**kwargs) == first
    loaded = load_sec_observed_financial_bundle(
        store=store, bundle_ref=first, resolve_observation=kwargs["resolve_observation"]
    )
    assert loaded.productions == loaded.quality_records == loaded.consumer_commits == ()
    with pytest.raises(SnapshotConflictError):
        write_sec_observed_financial_bundle(
            **{**kwargs, "captured_at": captured + timedelta(seconds=1)}
        )


def test_full_graph_rejects_unselected_policy_orphan_chain_and_time(tmp_path):
    production, source, source_ref, packages, package_ref, _, output = _produce(tmp_path / "input")
    _, quality, commit = _records(production)
    receipts = {
        source_ref.observation_identity: (source, source_ref),
        package_ref.observation_identity: (packages, package_ref),
        production.output_observation.observation_identity: (output, production.output_observation),
    }
    store = SnapshotStore(tmp_path / "bundle")
    orphan = SecObservedFinancialQualityRecord(
        production.production_identity,
        "unselected-policy",
        SecQualityStatus.PASS,
        production.produced_at,
        "0" * 64,
    )
    before = SecObservedFinancialQualityRecord(
        production.production_identity,
        "unselected-policy-2",
        SecQualityStatus.PASS,
        production.produced_at - timedelta(seconds=1),
    )
    for batch, record in (("orphan", orphan), ("before", before)):
        with pytest.raises(ValueError):
            write_sec_observed_financial_bundle(
                store=store,
                batch_identity=batch,
                productions=(production,),
                quality_records=(quality, record),
                consumer_commits=(commit,),
                captured_at=datetime(2024, 5, 2, tzinfo=UTC),
                resolve_observation=receipts.__getitem__,
            )
    bad_commit = SecObservedFinancialConsumerCommit(
        "0" * 64, quality.quality_record_id, commit.consumer_dataset_identity, commit.committed_at
    )
    with pytest.raises(ValueError, match="invalid dependency"):
        write_sec_observed_financial_bundle(
            store=store,
            batch_identity="cross-production-commit",
            productions=(production,),
            quality_records=(quality,),
            consumer_commits=(bad_commit,),
            captured_at=datetime(2024, 5, 2, tzinfo=UTC),
            resolve_observation=receipts.__getitem__,
        )


def test_generator_and_dependency_limits_fail_before_bundle_write(tmp_path):
    production, source, source_ref, packages, package_ref, _, output = _produce(tmp_path / "input")
    _, quality, commit = _records(production)
    receipts = {
        source_ref.observation_identity: (source, source_ref),
        package_ref.observation_identity: (packages, package_ref),
        production.output_observation.observation_identity: (output, production.output_observation),
    }
    store = SnapshotStore(tmp_path / "bundle")
    consumed = 0

    def productions():
        nonlocal consumed
        for item in (production, production):
            consumed += 1
            yield item

    with pytest.raises(ValueError, match="production limit"):
        write_sec_observed_financial_bundle(
            store=store,
            batch_identity="generator-limit",
            productions=productions(),
            quality_records=(quality,),
            consumer_commits=(commit,),
            captured_at=datetime(2024, 5, 2, tzinfo=UTC),
            resolve_observation=receipts.__getitem__,
            max_productions=1,
        )
    assert consumed == 2
    with pytest.raises(Exception, match="dependency byte limit|payload exceeds"):
        write_sec_observed_financial_bundle(
            store=store,
            batch_identity="dependency-limit",
            productions=(production,),
            quality_records=(quality,),
            consumer_commits=(commit,),
            captured_at=datetime(2024, 5, 2, tzinfo=UTC),
            resolve_observation=receipts.__getitem__,
            max_dependency_bytes=1,
        )
    assert not list(store.root.rglob("response.bin"))


def test_restarted_lifecycle_respects_pass_revoke_repass_cutoffs(tmp_path):
    production, source, source_ref, packages, package_ref, _, output = _produce(tmp_path / "input")
    policy, passed, first_commit = _records(production)
    revoked = SecObservedFinancialQualityRecord(
        production.production_identity,
        policy.quality_policy_version,
        SecQualityStatus.REVOKED,
        production.produced_at + timedelta(seconds=1),
        passed.quality_record_id,
    )
    repassed = SecObservedFinancialQualityRecord(
        production.production_identity,
        policy.quality_policy_version,
        SecQualityStatus.PASS,
        production.produced_at + timedelta(seconds=2),
        revoked.quality_record_id,
    )
    second_commit = SecObservedFinancialConsumerCommit(
        production.production_identity,
        repassed.quality_record_id,
        policy.consumer_dataset_identity,
        production.produced_at + timedelta(seconds=2),
    )
    receipts = {
        source_ref.observation_identity: (source, source_ref),
        package_ref.observation_identity: (packages, package_ref),
        production.output_observation.observation_identity: (output, production.output_observation),
    }
    store = SnapshotStore(tmp_path / "bundle")
    ref = write_sec_observed_financial_bundle(
        store=store,
        batch_identity="history",
        productions=(production,),
        quality_records=(passed, revoked, repassed),
        consumer_commits=(first_commit, second_commit),
        captured_at=datetime(2024, 5, 2, tzinfo=UTC),
        resolve_observation=receipts.__getitem__,
    )
    loaded = load_sec_observed_financial_bundle(
        store=store, bundle_ref=ref, resolve_observation=receipts.__getitem__
    )
    assert select_sec_observed_financial_productions(
        loaded.productions,
        loaded.quality_records,
        loaded.consumer_commits,
        replace(policy, knowledge_cutoff=production.produced_at),
    )
    assert not select_sec_observed_financial_productions(
        loaded.productions,
        loaded.quality_records,
        loaded.consumer_commits,
        replace(policy, knowledge_cutoff=revoked.recorded_at),
    )
    assert (
        select_sec_observed_financial_productions(
            loaded.productions,
            loaded.quality_records,
            loaded.consumer_commits,
            replace(policy, knowledge_cutoff=repassed.recorded_at),
        )[0].commit
        == second_commit
    )

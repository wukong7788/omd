"""Boundary and selector regressions for observed SEC lifecycle bundles."""

from __future__ import annotations

import socket
from copy import copy
from datetime import UTC, datetime, timedelta

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.providers.sec import (
    SecObservedFinancialConsumerCommit,
    SecObservedFinancialQualityRecord,
    SecQualityStatus,
    load_sec_observed_financial_bundle,
    select_sec_observed_financial_productions,
    write_sec_observed_financial_bundle,
)
from tests.providers.sec.test_observed_bundle_durability import _closure, _write
from tests.providers.sec.test_observed_xbrl_financials import _produce, _replay_policy

pytest.importorskip("edgar")


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def test_raw_quality_commit_generators_and_aggregate_rows_stop_before_write(tmp_path):
    production, quality, commit, receipts = _closure(tmp_path)
    store = SnapshotStore(tmp_path / "bundle")

    quality_values = (item for item in (quality, quality))
    with pytest.raises(ValueError, match="quality record limit"):
        write_sec_observed_financial_bundle(
            store=store,
            batch_identity="quality-generator",
            productions=(production,),
            quality_records=quality_values,
            consumer_commits=(commit,),
            captured_at=datetime(2024, 5, 2, tzinfo=UTC),
            resolve_observation=receipts.__getitem__,
            max_quality_records=1,
        )
    commit_values = (item for item in (commit, commit))
    with pytest.raises(ValueError, match="commit limit"):
        write_sec_observed_financial_bundle(
            store=store,
            batch_identity="commit-generator",
            productions=(production,),
            quality_records=(quality,),
            consumer_commits=commit_values,
            captured_at=datetime(2024, 5, 2, tzinfo=UTC),
            resolve_observation=receipts.__getitem__,
            max_commits=1,
        )
    with pytest.raises(ValueError, match="row limit"):
        write_sec_observed_financial_bundle(
            store=store,
            batch_identity="aggregate-rows",
            productions=(production, production),
            quality_records=(quality,),
            consumer_commits=(commit,),
            captured_at=datetime(2024, 5, 2, tzinfo=UTC),
            resolve_observation=receipts.__getitem__,
            max_productions=2,
            max_rows=len(production.vintage.rows),
        )
    assert not list(store.root.rglob("response.bin"))


def test_aggregate_dependency_bytes_count_unique_receipts_once(tmp_path):
    production, quality, commit, receipts = _closure(tmp_path)
    store = SnapshotStore(tmp_path / "bundle")
    dependency_bytes = sum(
        source.replay_observation(observation).manifest["response_byte_size"]
        for source, observation in receipts.values()
    )
    with pytest.raises(Exception, match="dependency byte limit|payload exceeds"):
        _write(
            store,
            "dependency-overflow",
            production,
            quality,
            commit,
            receipts,
            max_dependency_bytes=dependency_bytes - 1,
        )
    reference = write_sec_observed_financial_bundle(
        store=store,
        batch_identity="dependency-deduplicated",
        productions=(production, production),
        quality_records=(quality,),
        consumer_commits=(commit,),
        captured_at=datetime(2024, 5, 2, tzinfo=UTC),
        resolve_observation=receipts.__getitem__,
        max_productions=2,
        max_rows=2 * len(production.vintage.rows),
        max_dependency_bytes=dependency_bytes,
    )
    assert store.replay(reference).payload
    assert load_sec_observed_financial_bundle(
        store=store,
        bundle_ref=reference,
        resolve_observation=receipts.__getitem__,
        max_dependency_bytes=dependency_bytes,
    ).productions
    with pytest.raises(Exception, match="dependency byte limit|payload exceeds"):
        load_sec_observed_financial_bundle(
            store=store,
            bundle_ref=reference,
            resolve_observation=receipts.__getitem__,
            max_dependency_bytes=dependency_bytes - 1,
        )


def test_bundle_bytes_and_loader_count_row_limits_use_exact_boundaries(tmp_path):
    first, quality, commit, receipts = _closure(tmp_path)
    second, source, source_ref, packages, package_ref, _, output = _produce(
        tmp_path / "second", produced=first.produced_at + timedelta(seconds=1)
    )
    second_policy = _replay_policy(second, second.produced_at + timedelta(minutes=5))
    second_quality = SecObservedFinancialQualityRecord(
        second.production_identity,
        second_policy.quality_policy_version,
        SecQualityStatus.PASS,
        second.produced_at,
    )
    second_commit = SecObservedFinancialConsumerCommit(
        second.production_identity,
        second_quality.quality_record_id,
        second_policy.consumer_dataset_identity,
        second.produced_at,
    )
    receipts.update(
        {
            source_ref.observation_identity: (source, source_ref),
            package_ref.observation_identity: (packages, package_ref),
            second.output_observation.observation_identity: (output, second.output_observation),
        }
    )
    probe_store = SnapshotStore(tmp_path / "probe")
    probe = write_sec_observed_financial_bundle(
        store=probe_store,
        batch_identity="two-productions",
        productions=(first, second),
        quality_records=(quality, second_quality),
        consumer_commits=(commit, second_commit),
        captured_at=datetime(2024, 5, 2, tzinfo=UTC),
        resolve_observation=receipts.__getitem__,
        max_productions=2,
        max_rows=len(first.vintage.rows) + len(second.vintage.rows),
    )
    size = len(probe_store.replay(probe).payload)
    exact_store = SnapshotStore(tmp_path / "exact")
    exact = write_sec_observed_financial_bundle(
        store=exact_store,
        batch_identity="two-productions",
        productions=(first, second),
        quality_records=(quality, second_quality),
        consumer_commits=(commit, second_commit),
        captured_at=datetime(2024, 5, 2, tzinfo=UTC),
        resolve_observation=receipts.__getitem__,
        max_productions=2,
        max_rows=len(first.vintage.rows) + len(second.vintage.rows),
        max_bundle_bytes=size,
    )
    with pytest.raises(ValueError, match="byte limit"):
        write_sec_observed_financial_bundle(
            store=SnapshotStore(tmp_path / "small"),
            batch_identity="two-productions",
            productions=(first, second),
            quality_records=(quality, second_quality),
            consumer_commits=(commit, second_commit),
            captured_at=datetime(2024, 5, 2, tzinfo=UTC),
            resolve_observation=receipts.__getitem__,
            max_productions=2,
            max_rows=len(first.vintage.rows) + len(second.vintage.rows),
            max_bundle_bytes=size - 1,
        )
    assert load_sec_observed_financial_bundle(
        store=exact_store,
        bundle_ref=exact,
        resolve_observation=receipts.__getitem__,
        max_productions=2,
        max_rows=len(first.vintage.rows) + len(second.vintage.rows),
    ).productions
    with pytest.raises(ValueError, match="record limit"):
        load_sec_observed_financial_bundle(
            store=exact_store,
            bundle_ref=exact,
            resolve_observation=receipts.__getitem__,
            max_productions=1,
        )
    with pytest.raises(ValueError, match="row limit"):
        load_sec_observed_financial_bundle(
            store=exact_store,
            bundle_ref=exact,
            resolve_observation=receipts.__getitem__,
            max_productions=2,
            max_rows=len(first.vintage.rows),
        )


def test_writer_counts_rows_before_validating_tampered_seal_or_resolving(tmp_path):
    production, quality, commit, _ = _closure(tmp_path)
    oversized_vintage = copy(production.vintage)
    object.__setattr__(oversized_vintage, "rows", production.vintage.rows * 2)
    oversized = copy(production)
    object.__setattr__(oversized, "vintage", oversized_vintage)
    with pytest.raises(ValueError, match="row limit"):
        _write(
            SnapshotStore(tmp_path / "bundle"),
            "oversized-before-seal",
            oversized,
            quality,
            commit,
            lambda _: pytest.fail("resolver called before row limit"),
            max_rows=len(production.vintage.rows),
        )


def test_selector_rejects_foreign_policy_cross_production_and_selected_future_quality(tmp_path):
    first, quality, commit, _ = _closure(tmp_path)
    second, *_ = _produce(tmp_path / "second", produced=first.produced_at + timedelta(seconds=1))
    policy = _replay_policy(first, first.produced_at + timedelta(minutes=5))
    foreign = SecObservedFinancialQualityRecord(
        second.production_identity,
        "foreign-policy",
        SecQualityStatus.PASS,
        second.produced_at,
    )
    cross_commit = SecObservedFinancialConsumerCommit(
        first.production_identity,
        foreign.quality_record_id,
        policy.consumer_dataset_identity,
        second.produced_at,
    )
    with pytest.raises(ValueError, match="selected-policy"):
        select_sec_observed_financial_productions(
            (first, second), (quality, foreign), (commit, cross_commit), policy
        )
    future = SecObservedFinancialQualityRecord(
        first.production_identity,
        policy.quality_policy_version,
        SecQualityStatus.PASS,
        policy.knowledge_cutoff + timedelta(seconds=1),
        quality.quality_record_id,
    )
    future_commit = SecObservedFinancialConsumerCommit(
        first.production_identity,
        future.quality_record_id,
        policy.consumer_dataset_identity,
        policy.knowledge_cutoff,
    )
    with pytest.raises(ValueError, match="visible selected-policy"):
        select_sec_observed_financial_productions(
            (first,), (quality, future), (commit, future_commit), policy
        )

"""Acceptance coverage for in-memory known-by replay of observed financials."""

import socket
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import ohmydata.providers.sec.observed_xbrl_financials as observed_financials
from ohmydata.core import SnapshotStore
from ohmydata.providers.sec import (
    SecObservedFinancialConsumerCommit,
    SecObservedFinancialProduction,
    SecObservedFinancialQualityRecord,
    SecQualityStatus,
    select_sec_observed_financial_productions,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_observed_xbrl_financials import _produce, _replay_policy


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def _pass(result, policy, at=None):
    return SecObservedFinancialQualityRecord(
        result.production_identity,
        policy.quality_policy_version,
        SecQualityStatus.PASS,
        at or result.produced_at,
    )


def _commit(result, policy, quality, at=None):
    return SecObservedFinancialConsumerCommit(
        result.production_identity,
        quality.quality_record_id,
        policy.consumer_dataset_identity,
        at or quality.recorded_at,
    )


@pytest.mark.parametrize(
    "field",
    ["output_schema_version", "parser_version", "configuration_version", "configuration_identity"],
)
def test_explicit_versions_and_dataset_policy_are_required(tmp_path, field):
    result, *_ = _produce(tmp_path)
    policy = _replay_policy(result, datetime(2024, 5, 2, tzinfo=UTC))
    quality = _pass(result, policy)
    commit = _commit(result, policy, quality)
    object.__setattr__(policy, field, "b" * 64 if field == "configuration_identity" else "other-v1")
    assert not select_sec_observed_financial_productions((result,), (quality,), (commit,), policy)
    policy = _replay_policy(result, datetime(2024, 5, 2, tzinfo=UTC))
    wrong_dataset = SecObservedFinancialConsumerCommit(
        result.production_identity, quality.quality_record_id, "b" * 64, quality.recorded_at
    )
    assert not select_sec_observed_financial_productions(
        (result,), (quality,), (wrong_dataset,), policy
    )
    wrong_policy = SecObservedFinancialQualityRecord(
        result.production_identity, "other-policy-v1", SecQualityStatus.PASS, result.produced_at
    )
    assert not select_sec_observed_financial_productions((result,), (wrong_policy,), (), policy)


def test_repass_requires_its_own_commit_and_temporal_constraints(tmp_path):
    result, *_ = _produce(tmp_path)
    at = result.produced_at
    policy = _replay_policy(result, at + timedelta(seconds=2))
    first = _pass(result, policy, at)
    quarantine = SecObservedFinancialQualityRecord(
        result.production_identity,
        policy.quality_policy_version,
        SecQualityStatus.QUARANTINED,
        at + timedelta(seconds=1),
        first.quality_record_id,
    )
    second = SecObservedFinancialQualityRecord(
        result.production_identity,
        policy.quality_policy_version,
        SecQualityStatus.PASS,
        at + timedelta(seconds=2),
        quarantine.quality_record_id,
    )
    old = _commit(result, policy, first, at)
    assert not select_sec_observed_financial_productions(
        (result,), (first, quarantine, second), (old,), policy
    )
    new = _commit(result, policy, second)
    assert select_sec_observed_financial_productions(
        (result,), (first, quarantine, second), (old, new), policy
    )
    with pytest.raises(ValueError, match="predates production"):
        select_sec_observed_financial_productions(
            (result,), (_pass(result, policy, at - timedelta(seconds=1)),), (), policy
        )
    with pytest.raises(ValueError, match="timing"):
        select_sec_observed_financial_productions(
            (result,),
            (first,),
            (_commit(result, policy, first, at - timedelta(seconds=1)),),
            policy,
        )


def test_chain_empty_duplicates_and_bounded_generators(tmp_path):
    result, *_ = _produce(tmp_path)
    policy = _replay_policy(result, datetime(2024, 5, 2, tzinfo=UTC))
    assert select_sec_observed_financial_productions((), (), (), policy) == ()
    quality = _pass(result, policy)
    commit = _commit(result, policy, quality)
    assert select_sec_observed_financial_productions(
        (result, result), (quality, quality), (commit, commit), policy
    )
    orphan = SecObservedFinancialQualityRecord(
        result.production_identity,
        policy.quality_policy_version,
        SecQualityStatus.PASS,
        result.produced_at,
        "a" * 64,
    )
    with pytest.raises(ValueError, match="invalid first"):
        select_sec_observed_financial_productions((result,), (orphan,), (), policy)
    fork = SecObservedFinancialQualityRecord(
        result.production_identity,
        policy.quality_policy_version,
        SecQualityStatus.QUARANTINED,
        result.produced_at + timedelta(seconds=1),
        quality.quality_record_id,
    )
    tie = SecObservedFinancialQualityRecord(
        result.production_identity,
        policy.quality_policy_version,
        SecQualityStatus.REVOKED,
        result.produced_at + timedelta(seconds=1),
        quality.quality_record_id,
    )
    with pytest.raises(ValueError, match="same-time"):
        select_sec_observed_financial_productions((result,), (quality, fork, tie), (), policy)
    with pytest.raises(ValueError, match="quality record limit"):
        select_sec_observed_financial_productions(
            (result,), (item for item in (quality, quality)), (), policy, max_quality_records=1
        )
    with pytest.raises(ValueError, match="commit limit"):
        select_sec_observed_financial_productions(
            (result,), (), (item for item in (commit, commit)), policy, max_commits=1
        )
    with pytest.raises(ValueError, match="max_rows"):
        select_sec_observed_financial_productions((result,), (), (), policy, max_rows=True)


def test_cutoff_future_isolation_pass_binding_and_seal_tampering(tmp_path):
    result, *_ = _produce(tmp_path / "history")
    at = result.produced_at
    policy = _replay_policy(result, at)
    passed = _pass(result, policy)
    commit = _commit(result, policy, passed)
    revoke = SecObservedFinancialQualityRecord(
        result.production_identity,
        policy.quality_policy_version,
        SecQualityStatus.REVOKED,
        at + timedelta(seconds=1),
        passed.quality_record_id,
    )
    assert select_sec_observed_financial_productions((result,), (passed, revoke), (commit,), policy)
    assert not select_sec_observed_financial_productions(
        (result,), (passed, revoke), (commit,), _replay_policy(result, at + timedelta(seconds=1))
    )
    quarantined = SecObservedFinancialQualityRecord(
        result.production_identity, policy.quality_policy_version, SecQualityStatus.QUARANTINED, at
    )
    with pytest.raises(ValueError, match="visible selected-policy"):
        select_sec_observed_financial_productions(
            (result,), (quarantined,), (_commit(result, policy, quarantined),), policy
        )
    result, *_ = _produce(tmp_path / "seal")
    policy = _replay_policy(result, datetime(2024, 5, 2, tzinfo=UTC))
    object.__setattr__(result.vintage, "symbol", "ALTERED")
    with pytest.raises(ValueError, match="vintage_identity"):
        select_sec_observed_financial_productions((result,), (), (), policy)


def test_stale_attestations_are_rejected_without_mutating_input(tmp_path):
    result, *_ = _produce(tmp_path)
    policy = _replay_policy(result, datetime(2024, 5, 2, tzinfo=UTC))
    quality = _pass(result, policy)
    original = quality.quality_record_id
    object.__setattr__(quality, "status", SecQualityStatus.QUARANTINED)
    for _ in range(2):
        with pytest.raises(ValueError, match="quality_record_id"):
            select_sec_observed_financial_productions((result,), (quality,), (), policy)
        assert quality.quality_record_id == original
    quality = _pass(result, policy)
    commit = _commit(result, policy, quality)
    original = commit.commit_id
    object.__setattr__(commit, "committed_at", commit.committed_at + timedelta(seconds=1))
    for _ in range(2):
        with pytest.raises(ValueError, match="commit_id"):
            select_sec_observed_financial_productions((result,), (quality,), (commit,), policy)
        assert commit.commit_id == original


def test_deterministic_multiple_packages_and_seal_replace_rejection(tmp_path):
    first, *_ = _produce(tmp_path / "first", produced=datetime(2024, 5, 1, 23, tzinfo=UTC))
    second, *_ = _produce(tmp_path / "second", produced=datetime(2024, 5, 1, 23, 1, tzinfo=UTC))
    policy = _replay_policy(first, datetime(2024, 5, 2, tzinfo=UTC))
    assert second.configuration_identity == policy.configuration_identity
    q1, q2 = _pass(first, policy), _pass(second, policy)
    c1, c2 = _commit(first, policy, q1), _commit(second, policy, q2)
    selected = select_sec_observed_financial_productions(
        (second, first), (q2, q1), (c2, c1), policy
    )
    assert tuple(item.production for item in selected) == (first, second)
    with pytest.raises(ValueError, match="output receipt|binding"):
        replace(first, produced_at=first.produced_at + timedelta(seconds=1))
    with pytest.raises(ValueError, match="result bytes|binding"):
        replace(first, request=replace(first.request, symbol="ALTERED"))
    with pytest.raises(ValueError, match="result bytes|binding"):
        replace(first, vintage=replace(first.vintage, symbol="ALTERED"))
    with pytest.raises(ValueError, match="output receipt|binding"):
        replace(first, output_observation=replace(first.output_observation, endpoint="ALTERED"))
    with pytest.raises(ValueError, match="created by production"):
        SecObservedFinancialProduction(
            first.request,
            first.evidence,
            first.output_observation,
            first.vintage,
            first.produced_at,
            object(),
            "a" * 64,
        )


def test_production_and_row_caps_stop_generators_before_seal_work(tmp_path):
    result, *_ = _produce(tmp_path)
    policy = _replay_policy(result, datetime(2024, 5, 2, tzinfo=UTC))
    consumed = 0

    def productions():
        nonlocal consumed
        for item in (result, result, result):
            consumed += 1
            yield item

    with pytest.raises(ValueError, match="production limit"):
        select_sec_observed_financial_productions(productions(), (), (), policy, max_productions=1)
    assert consumed == 2
    with pytest.raises(ValueError, match="aggregate row limit"):
        select_sec_observed_financial_productions((result, result), (), (), policy, max_rows=1)


def test_selector_never_replays_or_parses_and_future_commit_is_not_visible(tmp_path, monkeypatch):
    result, *_ = _produce(tmp_path)
    at = result.produced_at
    policy = _replay_policy(result, at)
    quality = _pass(result, policy)
    future = _commit(result, policy, quality, at + timedelta(seconds=1))
    monkeypatch.setattr(SnapshotStore, "replay_observation", lambda *_: pytest.fail("replay"))
    monkeypatch.setattr(
        observed_financials, "_rows_from_documents", lambda *_: pytest.fail("parser")
    )
    assert not select_sec_observed_financial_productions((result,), (quality,), (future,), policy)
    visible = _replay_policy(result, at + timedelta(seconds=1))
    assert select_sec_observed_financial_productions((result,), (quality,), (future,), visible)


def test_nested_receipt_and_stale_commit_remain_rejected_without_input_mutation(tmp_path):
    result, *_ = _produce(tmp_path / "receipt")
    policy = _replay_policy(result, datetime(2024, 5, 2, tzinfo=UTC))
    object.__setattr__(result.evidence.source_observation, "request_identity", "a" * 64)
    with pytest.raises(ValueError, match="evidence binding"):
        select_sec_observed_financial_productions((result,), (), (), policy)

    result, *_ = _produce(tmp_path / "commit")
    policy = _replay_policy(result, datetime(2024, 5, 2, tzinfo=UTC))
    quality = _pass(result, policy)
    commit = _commit(result, policy, quality)
    object.__setattr__(commit, "committed_at", commit.committed_at + timedelta(seconds=1))
    changed_at, original_id = commit.committed_at, commit.commit_id
    for _ in range(2):
        with pytest.raises(ValueError, match="commit_id"):
            select_sec_observed_financial_productions((result,), (quality,), (commit,), policy)
        assert (commit.committed_at, commit.commit_id) == (changed_at, original_id)


def test_stale_production_and_vintage_identities_reject_without_repair(tmp_path):
    result, *_ = _produce(tmp_path / "vintage")
    policy = _replay_policy(result, datetime(2024, 5, 2, tzinfo=UTC))
    original_vintage_id = result.vintage.vintage_identity
    object.__setattr__(result.vintage, "symbol", "ALTERED")
    for _ in range(2):
        with pytest.raises(ValueError, match="vintage_identity"):
            select_sec_observed_financial_productions((result,), (), (), policy)
        assert (result.vintage.symbol, result.vintage.vintage_identity) == (
            "ALTERED",
            original_vintage_id,
        )

    result, *_ = _produce(tmp_path / "production")
    policy = _replay_policy(result, datetime(2024, 5, 2, tzinfo=UTC))
    object.__setattr__(result, "production_identity", "a" * 64)
    for _ in range(2):
        with pytest.raises(ValueError, match="production_identity"):
            select_sec_observed_financial_productions((result,), (), (), policy)
        assert result.production_identity == "a" * 64

"""Document replay inherits time semantics without inheriting SGML capabilities."""

import socket
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.providers.sec import (
    SecDocumentFinancialReplayResult,
    SecObservedFinancialQualityRecord,
    SecQualityStatus,
    select_sec_observed_financial_productions,
)
from ohmydata.providers.sec import (
    select_sec_document_financial_productions as select,
)
from ohmydata.providers.sec.document_financials import SecDocumentFinancialProduction

sys.path.insert(0, str(Path(__file__).parent))
from test_document_financials import build
from test_observed_financial_replay import _commit, _pass
from test_observed_xbrl_financials import _produce, _replay_policy


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network"))
    result, *_ = build(tmp_path)
    policy = _replay_policy(result, result.produced_at)
    quality = _pass(result, policy)
    commit = _commit(result, policy, quality)
    monkeypatch.setattr(SnapshotStore, "observe", lambda *_: pytest.fail("query write"))
    monkeypatch.setattr(SnapshotStore, "replay_observation", lambda *_: pytest.fail("query read"))
    return result, policy, quality, commit


def test_selected_identity_and_future_quality_isolation(case):
    result, policy, quality, commit = case
    revoke = SecObservedFinancialQualityRecord(
        result.production_identity,
        policy.quality_policy_version,
        SecQualityStatus.REVOKED,
        result.produced_at + timedelta(seconds=1),
        quality.quality_record_id,
    )
    selected = select((result,), (quality, revoke), (commit,), policy)
    assert selected == (SecDocumentFinancialReplayResult(result, quality, commit),)
    assert not select(
        (result,),
        (quality, revoke),
        (commit,),
        replace(policy, knowledge_cutoff=revoke.recorded_at),
    )
    assert not select((result,), (quality,), (), policy)
    assert not select((result,), (), (), policy)
    assert not select(
        (result,),
        (quality,),
        (commit,),
        replace(policy, knowledge_cutoff=result.produced_at - timedelta(microseconds=1)),
    )


def test_reapproval_needs_new_exact_commit(case):
    result, policy, first, commit = case
    revoked = SecObservedFinancialQualityRecord(
        result.production_identity,
        policy.quality_policy_version,
        SecQualityStatus.REVOKED,
        result.produced_at + timedelta(seconds=1),
        first.quality_record_id,
    )
    passed = SecObservedFinancialQualityRecord(
        result.production_identity,
        policy.quality_policy_version,
        SecQualityStatus.PASS,
        result.produced_at + timedelta(seconds=2),
        revoked.quality_record_id,
    )
    policy = replace(policy, knowledge_cutoff=passed.recorded_at)
    assert not select((result,), (first, revoked, passed), (commit,), policy)
    new = _commit(result, policy, passed)
    assert select((result,), (first, revoked, passed), (commit, new), policy)[0].commit == new


@pytest.mark.parametrize(
    "field",
    [
        "output_schema_version",
        "parser_version",
        "configuration_version",
        "configuration_identity",
        "consumer_dataset_identity",
        "quality_policy_version",
    ],
)
def test_explicit_policy_fields(case, field):
    result, policy, quality, commit = case
    wrong = "b" * 64 if field.endswith("identity") else "synthetic-other-v1"
    assert not select((result,), (quality,), (commit,), replace(policy, **{field: wrong}))


def test_wrong_identity_and_visible_orphan_commit(case):
    result, policy, quality, commit = case
    other = replace(quality, production_identity="f" * 64)
    with pytest.raises(ValueError, match="visible"):
        select((result,), (other,), (commit,), policy)
    foreign = replace(commit, production_identity="f" * 64)
    assert not select((result,), (quality,), (foreign,), policy)


def test_chronology_and_same_time_conflict(case):
    result, policy, quality, commit = case
    bad = replace(quality, status=SecQualityStatus.QUARANTINED)
    with pytest.raises(ValueError, match="same-time"):
        select((result,), (quality, bad), (commit,), policy)
    with pytest.raises(ValueError, match="predates"):
        select(
            (result,),
            (replace(quality, recorded_at=result.produced_at - timedelta(seconds=1)),),
            (),
            policy,
        )
    with pytest.raises(ValueError, match="timing"):
        select(
            (result,),
            (quality,),
            (replace(commit, committed_at=commit.committed_at - timedelta(seconds=1)),),
            policy,
        )


@pytest.mark.parametrize(
    "target,field",
    [
        ("production", "production_identity"),
        ("vintage", "vintage_identity"),
        ("source_package", "package_identity"),
    ],
)
def test_tampering_rejected_without_repair(case, target, field):
    result, policy, quality, commit = case
    value = result if target == "production" else getattr(result, target)
    object.__setattr__(value, field, "f" * 64)
    with pytest.raises(ValueError, match="identity"):
        select((result,), (quality,), (commit,), policy)
    assert getattr(value, field) == "f" * 64


@pytest.mark.parametrize("name", ["productions", "quality_records", "consumer_commits"])
def test_duplicate_occurrences_count_before_dedup(case, name):
    result, policy, quality, commit = case
    arguments = {
        "productions": (result,),
        "quality_records": (quality,),
        "consumer_commits": (commit,),
        "policy": policy,
    }
    value = arguments[name][0]
    arguments[name] = (value for _ in range(2))
    limit = {
        "productions": "max_productions",
        "quality_records": "max_quality_records",
        "consumer_commits": "max_commits",
    }[name]
    with pytest.raises(ValueError, match="limit"):
        select(**arguments, **{limit: 1})
    assert len(select((result, result), (quality, quality), (commit, commit), policy)) == 1


def test_byte_admission_precedes_production_hashing(case, monkeypatch):
    result, policy, quality, commit = case
    monkeypatch.setattr(
        SecDocumentFinancialProduction,
        "__post_init__",
        lambda *_: pytest.fail("hashed before admission"),
    )
    with pytest.raises(ValueError, match="byte admission"):
        select((result,), (quality,), (commit,), policy, max_admission_bytes=1)
    object.__setattr__(result.vintage.rows[0], "label", "x" * (6 * 1024 * 1024))
    with pytest.raises(ValueError, match="byte admission"):
        select((result,), (), (), policy)


def test_old_and_new_types_remain_separate(tmp_path):
    result, *_ = build(tmp_path / "new")
    old, *_ = _produce(tmp_path / "old")
    policy = _replay_policy(result, result.produced_at)
    with pytest.raises(TypeError):
        select_sec_observed_financial_productions((result,), (), (), policy)
    with pytest.raises(TypeError):
        select((old,), (), (), policy)
    old_policy = _replay_policy(old, old.produced_at)
    quality = _pass(old, old_policy)
    commit = _commit(old, old_policy, quality)
    selected = select_sec_observed_financial_productions((old,), (quality,), (commit,), old_policy)
    assert selected[0].production.production_identity == old.production_identity
    assert selected[0].quality.quality_record_id == quality.quality_record_id
    assert selected[0].commit.commit_id == commit.commit_id


def test_astral_serialization_admission_exact_boundary():
    from ohmydata.providers.sec.document_financial_replay import _admit

    # Two nodes charge 128 overhead bytes; each astral codepoint needs
    # two six-byte surrogate escapes in ensure_ascii JSON.
    _admit(("\U0001f600\U0001f600",), 152)
    with pytest.raises(ValueError, match="byte admission"):
        _admit(("\U0001f600\U0001f600",), 151)

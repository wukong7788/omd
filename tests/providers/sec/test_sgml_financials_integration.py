"""Retained raw SEC bytes survive production restart and downstream replay."""

import socket
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ohmydata.core import SnapshotStore
from ohmydata.core.errors import SnapshotIntegrityError
from ohmydata.providers.sec import (
    SecPitMode,
    SecPitPolicy,
    evaluate_sec_structural_quality,
    load_sec_pit_bundle,
    produce_sec_financials_from_sgml,
    select_sec_financial_versions,
    select_sec_quality_findings,
    write_sec_pit_bundle,
)
from tests.providers.sec.test_pit_bundle import _records
from tests.providers.sec.test_sgml_financials import _observation, _raw, _request

PRODUCED = datetime(2024, 5, 1, 23, tzinfo=UTC)
EVALUATED = datetime(2024, 5, 2, 10, tzinfo=UTC)
CAPTURED = datetime(2024, 5, 2, 13, tzinfo=UTC)


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def _produce(source, observation, projection):
    return produce_sec_financials_from_sgml(
        source_store=source,
        source_observation=observation,
        projection_store=projection,
        request=_request(),
        produced_at=PRODUCED,
    )


def test_raw_restart_findings_bundle_and_pit_modes_remain_bound(tmp_path):
    # Undefined native unit is preserved as missing, not repaired by the builder.
    raw = _raw().replace(b'<unit id="usd"><measure>iso4217:USD</measure></unit>', b"")
    source, observation = _observation(tmp_path, raw)
    projection = SnapshotStore(tmp_path / "projection")
    replay_limits = []

    class CountedSource(SnapshotStore):
        def replay_observation(self, observation_ref, expected=None, max_payload_bytes=None):
            replay_limits.append(max_payload_bytes)
            return super().replay_observation(observation_ref, expected, max_payload_bytes)

    original = _produce(CountedSource(source.root), observation, projection)
    assert replay_limits == [8 * 1024 * 1024]
    assert original.vintage.rows[0].value == Decimal(123)
    assert original.vintage.rows[0].unit is None
    payload = projection.replay_observation(original.projection_observation).payload
    restarted = _produce(SnapshotStore(source.root), observation, SnapshotStore(projection.root))
    assert restarted == original
    assert projection.replay_observation(restarted.projection_observation).payload == payload
    assert source.replay_observation(observation).payload == raw

    report = evaluate_sec_structural_quality(
        restarted.versions, detected_at=EVALUATED, recorded_at=EVALUATED
    )
    assert len(report.findings) == 1
    assert report.findings[0].affected_fields == ("unit",)
    version = restarted.versions[0]
    quality, commit = _records(version)
    bundle = SnapshotStore(tmp_path / "bundle")
    ref = write_sec_pit_bundle(
        store=bundle,
        batch_identity="raw-chain",
        versions=restarted.versions,
        quality_records=[quality],
        consumer_commits=[commit],
        quality_findings=report.findings,
        source_store=projection,
        resolve_observation=lambda _: restarted.projection_observation,
        captured_at=CAPTURED,
    )
    assert ref.serialization_identifier == "sec-pit-bundle-v2"
    loaded = load_sec_pit_bundle(
        store=SnapshotStore(bundle.root),
        bundle_ref=ref,
        source_store=SnapshotStore(projection.root),
        resolve_observation=lambda _: restarted.projection_observation,
    )
    assert loaded.versions == restarted.versions
    assert loaded.quality_findings == report.findings
    for cutoff, expected in ((PRODUCED, ()), (EVALUATED, report.findings)):
        assert (
            select_sec_quality_findings(
                loaded.quality_findings,
                normalized_version_id=version.normalized_version_id,
                rule_version=report.rule_version,
                cutoff=cutoff,
            )
            == expected
        )
    policy = SecPitPolicy(
        version.schema_version,
        version.adapter_version,
        version.normalization_version,
        version.configuration_identity,
        quality.quality_policy_version,
        CAPTURED,
    )
    for mode in (SecPitMode.MARKET_KNOWN, SecPitMode.SYSTEM_REPLAY):
        results = select_sec_financial_versions(
            loaded.versions,
            mode=mode,
            knowledge_cutoff=CAPTURED,
            policy=policy,
            quality_records=loaded.quality_records,
            consumer_commits=loaded.consumer_commits,
        )
        assert len(results) == 1
        assert results[0].version == version


def test_invalid_raw_does_not_publish_or_change_existing_projection(tmp_path):
    source, observation = _observation(tmp_path, _raw())
    projection = SnapshotStore(tmp_path / "projection")
    original = _produce(source, observation, projection)
    payload = projection.replay_observation(original.projection_observation).payload
    files = {p.relative_to(projection.root) for p in projection.root.rglob("*") if p.is_file()}
    bad_source, bad_observation = _observation(
        tmp_path / "invalid", _raw().replace(b"<TYPE>EX-101.INS", b"<TYPE>EX-101.OTHER")
    )
    with pytest.raises(ValueError, match="missing required"):
        _produce(bad_source, bad_observation, projection)
    assert projection.replay_observation(original.projection_observation).payload == payload
    assert files == {
        p.relative_to(projection.root) for p in projection.root.rglob("*") if p.is_file()
    }
    # Same old projection remains independently readable after raw integrity loss.
    raw_path = next(source.root.rglob("response.bin"))
    raw_path.write_bytes(b"synthetic corrupted raw")
    with pytest.raises(SnapshotIntegrityError):
        _produce(SnapshotStore(source.root), observation, projection)
    assert projection.replay_observation(original.projection_observation).payload == payload
    assert files == {
        p.relative_to(projection.root) for p in projection.root.rglob("*") if p.is_file()
    }


def test_crlf_header_preserves_raw_bytes_and_native_values(tmp_path):
    raw = _raw().replace(b"\n", b"\r\n")
    source, observation = _observation(tmp_path, raw)
    result = _produce(source, observation, SnapshotStore(tmp_path / "projection"))
    assert result.vintage.accepted_at == datetime(2024, 5, 1, 21, tzinfo=UTC)
    assert result.vintage.rows[0].value == Decimal(123)
    assert source.replay_observation(observation).payload == raw

"""Exercise invalid but correctly hashed snapshots at the public bundle boundary."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ohmydata.core import RequestSpec, SnapshotMode, SnapshotStore
from ohmydata.core.errors import SnapshotIntegrityError
from ohmydata.providers.sec import (
    SecQualityRecord,
    SecQualityStatus,
    load_sec_pit_bundle,
    write_sec_pit_bundle,
)
from tests.providers.sec.test_pit_bundle import _records, _version, _write


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-field",
        "unknown-field",
        "schema",
        "duplicate-json-key",
        "nonfinite",
        "batch",
        "version-id",
        "content-id",
        "quality-id",
        "commit-id",
        "source-id",
        "naive-time",
        "orphan-revoke",
        "missing-quality",
        "early-capture",
    ],
)
def test_valid_snapshot_hash_cannot_hide_invalid_bundle(tmp_path: Path, mutation: str) -> None:
    version, quality, _, store, ref = _write(tmp_path)
    data = json.loads(store.replay(ref).payload)
    captured = datetime(2024, 5, 2, 13, tzinfo=UTC)
    if mutation == "missing-field":
        del data["versions"]
    elif mutation == "unknown-field":
        data["extra"] = "not-supported"
    elif mutation == "schema":
        data["schema"] = "sec-pit-bundle-v999"
    elif mutation == "batch":
        data["batch_identity"] = "different-batch"
    elif mutation in {"version-id", "content-id", "source-id"}:
        key = {
            "version-id": "normalized_version_id",
            "content-id": "content_identity",
            "source-id": "source_artifact_identity",
        }[mutation]
        data["versions"][0][key] = "f" * 64
    elif mutation == "quality-id":
        data["quality_records"][0]["quality_record_id"] = "f" * 64
    elif mutation == "commit-id":
        data["consumer_commits"][0]["commit_id"] = "f" * 64
    elif mutation == "naive-time":
        data["versions"][0]["recorded_at"] = "2024-05-02T10:00:00"
    elif mutation == "orphan-revoke":
        revoke = replace(quality, status=SecQualityStatus.REVOKED)
        data["quality_records"][0]["status"] = revoke.status.value
        data["quality_records"][0]["quality_record_id"] = revoke.quality_record_id
        data["consumer_commits"] = []
    elif mutation == "missing-quality":
        data["quality_records"] = []
    elif mutation == "early-capture":
        captured = datetime(2024, 5, 2, 11, tzinfo=UTC)
    payload = json.dumps(data).encode()
    if mutation == "duplicate-json-key":
        payload = b'{"schema":"sec-pit-bundle-v1",' + payload[1:]
    elif mutation == "nonfinite":
        payload = payload.replace(b'"row_ordinal": 0', b'"row_ordinal": NaN')
    injected_store = SnapshotStore(tmp_path / "injected")
    injected = injected_store.write(
        RequestSpec("sec", "pit-bundle", {"batch_identity": "batch-1"}),
        payload,
        captured,
        "sec-pit-bundle-v1",
        SnapshotMode.FROZEN,
    )
    with pytest.raises((ValueError, TypeError, SnapshotIntegrityError)):
        load_sec_pit_bundle(
            store=injected_store,
            bundle_ref=injected,
            source_store=SnapshotStore(tmp_path / "source"),
            resolve_observation=lambda _: version.observation,
        )


@pytest.mark.parametrize(
    "case",
    [
        "fake-version",
        "orphan-revoke",
        "early-quality",
        "early-commit",
        "revoked-pass",
        "early-capture",
    ],
)
def test_writer_rejects_invalid_closure_before_persisting(tmp_path: Path, case: str) -> None:
    version = _version(tmp_path / "source")
    quality, commit = _records(version)
    versions = [version]
    qualities = [quality]
    commits = [commit]
    captured = datetime(2024, 5, 2, 13, tzinfo=UTC)
    if case == "fake-version":
        versions = [SimpleNamespace(**vars(version))]
    elif case == "orphan-revoke":
        qualities = [replace(quality, status=SecQualityStatus.REVOKED)]
        commits = []
    elif case == "early-quality":
        qualities = [replace(quality, recorded_at=datetime(2024, 5, 2, 9, tzinfo=UTC))]
        commits = []
    elif case == "early-commit":
        commits = [replace(commit, committed_at=version.recorded_at)]
    elif case == "revoked-pass":
        qualities.append(
            SecQualityRecord(
                version.normalized_version_id,
                "quality-v1",
                SecQualityStatus.REVOKED,
                datetime(2024, 5, 2, 11, 30, tzinfo=UTC),
                quality.quality_record_id,
            )
        )
    elif case == "early-capture":
        captured = quality.recorded_at
    store = SnapshotStore(tmp_path / "bundle")
    with pytest.raises((ValueError, TypeError)):
        write_sec_pit_bundle(
            store=store,
            batch_identity="invalid",
            versions=versions,
            quality_records=qualities,
            consumer_commits=commits,
            captured_at=captured,
        )
    assert not list(store.root.rglob("manifest.json"))


@pytest.mark.parametrize(
    "case",
    [
        "wrong-id",
        "missing-snapshot",
        "naive-source",
        "duplicate-source-key",
        "bad-decimal",
        "wrong-source-row",
    ],
)
def test_source_dependencies_fail_explicitly(tmp_path: Path, case: str) -> None:
    version, _, _, store, ref = _write(tmp_path)
    source = SnapshotStore(tmp_path / "source")
    observation = version.observation
    if case == "wrong-id":
        observation = replace(observation, observation_identity="f" * 64)
    elif case == "missing-snapshot":
        source = SnapshotStore(tmp_path / "missing")
    else:
        payload = source.replay_observation(observation).payload
        data = json.loads(payload)
        if case == "naive-source":
            data["source_available_at"] = "2024-05-01T21:00:00"
        elif case == "bad-decimal":
            data["rows"][0]["value"] = {"decimal": "NaN"}
        elif case == "wrong-source-row":
            data["rows"][0]["value"] = {"decimal": "999.00"}
        payload = json.dumps(data).encode()
        if case == "duplicate-source-key":
            payload = payload.replace(
                b'{"accession_number":', b'{"schema":"duplicate","accession_number":', 1
            )
        observation = source.observe(
            RequestSpec("sec", "financial-typed-rows", {"accession": version.accession_number}),
            payload,
            version.observation.snapshot_fetched_at,
            "sec-financial-typed-rows-projection-v1",
        )
        # Rebuild the receipt snapshot's observation reference to reach the source decoder.
        receipts = json.loads(store.replay(ref).payload)
        receipts["versions"][0]["observation_id"] = observation.observation_identity
        store = SnapshotStore(tmp_path / "injected")
        ref = store.write(
            RequestSpec("sec", "pit-bundle", {"batch_identity": "batch-1"}),
            json.dumps(receipts).encode(),
            datetime(2024, 5, 2, 13, tzinfo=UTC),
            "sec-pit-bundle-v1",
            SnapshotMode.FROZEN,
        )
    with pytest.raises((ValueError, TypeError, SnapshotIntegrityError)):
        load_sec_pit_bundle(
            store=store,
            bundle_ref=ref,
            source_store=source,
            resolve_observation=lambda _: observation,
        )


def test_record_limit_stops_generator_and_byte_limit_precedes_json_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = _version(tmp_path / "source")
    consumed = []

    def unbounded():
        while True:
            consumed.append(1)
            assert len(consumed) <= 2, "limit checked after consuming the input"
            yield version

    store = SnapshotStore(tmp_path / "bundle")
    with pytest.raises(ValueError, match="limit"):
        write_sec_pit_bundle(
            store=store,
            batch_identity="bounded",
            versions=unbounded(),
            quality_records=(),
            captured_at=version.recorded_at,
            max_records=1,
        )
    assert len(consumed) == 2

    def unexpected_encoder(*args, **kwargs):
        pytest.fail("oversized field reached JSON encoder")

    monkeypatch.setattr(json.JSONEncoder, "iterencode", unexpected_encoder)

    class OversizedText(str):
        def encode(self, *args, **kwargs):
            pytest.fail("oversized field allocated encoded bytes")

    with pytest.raises(ValueError, match="byte limit"):
        write_sec_pit_bundle(
            store=store,
            batch_identity=OversizedText("x" * 1025),
            versions=(),
            quality_records=(),
            captured_at=version.recorded_at,
            max_bytes=1024,
        )

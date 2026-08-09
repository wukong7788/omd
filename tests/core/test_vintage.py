import hashlib
import json
import multiprocessing
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportMissingParameterType=false, reportUnknownMemberType=false
import pytest

from ohmydata.core import (
    RequestSpec,
    SnapshotMode,
    SnapshotStore,
    SourceFactObservation,
)
from ohmydata.core import (
    SourceFactRegistry as _SourceFactRegistry,
)


class SourceFactRegistry(_SourceFactRegistry):
    """Test adapter: materialize every registration through a real snapshot ref."""

    def register(self, observation, *, store=None, observation_ref=None):
        original = observation
        if store is None:
            store = SnapshotStore(self.root / "snapshots")
        if observation_ref is None:
            assert observation.snapshot_fetched_at is not None
            spec = RequestSpec(
                observation.provider,
                observation.endpoint,
                {"source_key": observation.source_key, "nonce": observation.observation_identity},
            )
            observation_ref = store.observe(
                spec,
                observation.source_value_sha256.encode(),
                observation.snapshot_fetched_at,
                "test-json-v1",
                SnapshotMode.APPEND,
            )
            observation = replace(
                observation,
                snapshot_identity=observation_ref.snapshot_identity,
                observation_identity=observation_ref.observation_identity,
                request_identity=observation_ref.request_identity,
                payload_sha256=observation_ref.response_sha256,
                fact_version=observation_ref.fact_version,
                supersedes_fact_version=None
                if self.observations
                else observation.supersedes_fact_version,
            )
        super().register(observation, store=store, observation_ref=observation_ref)
        return original


def _process_append(root_str: str, prefix: str) -> None:
    root = Path(root_str)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reg = SourceFactRegistry(root)
    for i in range(5):
        v = f"{i + 1:064x}"
        reg.register(
            SourceFactObservation(
                provider="fake",
                endpoint="e",
                source_key=f"{prefix}{i}",
                fact_version=v,
                snapshot_identity=v,
                observation_identity=v,
                request_identity=v,
                source_value_sha256=v,
                payload_sha256=v,
                provider_first_observed_at=now,
                snapshot_fetched_at=now,
            )
        )


def _obs(version: str, when: datetime, prior: str | None = None) -> SourceFactObservation:
    return SourceFactObservation(
        provider="fake",
        endpoint="etf_basic",
        source_key="510300.SH",
        snapshot_identity=("b" * 63) + version[0],
        observation_identity=("c" * 63) + version[0],
        request_identity="d" * 64,
        payload_sha256="e" * 64,
        source_value_sha256=version,
        fact_version=version,
        provider_first_observed_at=when,
        snapshot_fetched_at=when,
        supersedes_fact_version=prior,
    )


def test_registry_idempotent_and_revision_lineage(tmp_path: Path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    registry = SourceFactRegistry(tmp_path)
    first = _obs("a" * 64, now)
    assert registry.register(first) == first
    assert registry.register(first) == first
    second = _obs("f" * 64, now + timedelta(days=1), first.fact_version)
    registry.register(second)
    assert len(registry.observations) == 2
    assert len(list((tmp_path / "manifests").glob("*.json"))) == 2
    assert len(list((tmp_path / "records").glob("*.json"))) == 2
    assert SourceFactRegistry(tmp_path).manifest().observation_count == 2


def test_real_registry_requires_store_and_observation_ref(tmp_path: Path):
    registry = _SourceFactRegistry(tmp_path)
    observation = _obs("a" * 64, datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="store and observation_ref"):
        registry.register(observation)
    with pytest.raises(ValueError, match="store and observation_ref"):
        registry.register(observation, store=object())
    with pytest.raises(ValueError, match="store and observation_ref"):
        registry.register(observation, observation_ref=object())


def test_unreachable_orphan_generation_ignored_after_later_append(tmp_path: Path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    registry = SourceFactRegistry(tmp_path)
    registry.register(_obs("a" * 64, now))
    head = json.loads((tmp_path / "manifest.json").read_text())
    current = json.loads((tmp_path / "manifests" / head["generation"]).read_text())
    orphan_record = _obs("b" * 64, now).to_dict()
    orphan_rows = [*current["observations"], orphan_record]
    orphan_identity = hashlib.sha256(
        json.dumps(orphan_rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    orphan = {
        **current,
        "registry_identity": orphan_identity,
        "observations": orphan_rows,
        "observation_count": 2,
        "previous_registry_identity": current["registry_identity"],
    }
    (tmp_path / "manifests" / f"orphan-{orphan_identity}.json").write_text(
        json.dumps(orphan, sort_keys=True)
    )
    (tmp_path / "manifests" / "orphan-malformed.json").write_text("not-json")
    registry.register(_obs("c" * 64, now + timedelta(days=1)))
    loaded = SourceFactRegistry(tmp_path)
    assert len(loaded.observations) == 2
    assert all(item.fact_version != "b" * 64 for item in loaded.observations)


def test_registry_rejects_forged_lineage_and_tamper(tmp_path: Path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    registry = SourceFactRegistry(tmp_path)
    with pytest.raises(ValueError):
        registry.register(_obs("f" * 64, now, "a" * 64))
    registry.register(_obs("a" * 64, now))
    (tmp_path / "manifest.json").write_text("{}")
    with pytest.raises(ValueError):
        SourceFactRegistry(tmp_path)


def test_two_keys_and_revision_chain_reload(tmp_path: Path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    registry = SourceFactRegistry(tmp_path)
    first = SourceFactObservation(
        provider="fake",
        endpoint="etf_basic",
        source_key="510050.SH",
        fact_version="a" * 64,
        snapshot_identity="b" * 64,
        observation_identity="c" * 64,
        request_identity="d" * 64,
        payload_sha256="e" * 64,
        source_value_sha256="a" * 64,
        provider_first_observed_at=now,
        snapshot_fetched_at=now,
    )
    second = SourceFactObservation(
        provider="fake",
        endpoint="etf_basic",
        source_key="510300.SH",
        fact_version="b" * 64,
        snapshot_identity="f" * 64,
        observation_identity="1" * 64,
        request_identity="d" * 64,
        payload_sha256="e" * 64,
        source_value_sha256="b" * 64,
        provider_first_observed_at=now,
        snapshot_fetched_at=now,
    )
    registry.register(first)
    registry.register(second)
    revised = SourceFactObservation(
        provider="fake",
        endpoint="etf_basic",
        source_key="510300.SH",
        fact_version="c" * 64,
        snapshot_identity="2" * 64,
        observation_identity="3" * 64,
        request_identity="d" * 64,
        payload_sha256="e" * 64,
        source_value_sha256="c" * 64,
        provider_first_observed_at=now,
        snapshot_fetched_at=now + timedelta(days=1),
        supersedes_fact_version=second.fact_version,
    )
    registry.register(revised)
    loaded = SourceFactRegistry(tmp_path)
    assert len(loaded.observations) == 3
    assert revised.supersedes_fact_version == second.fact_version


def test_two_registry_instances_concurrent_append(tmp_path: Path):
    barrier = threading.Barrier(2)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    def worker(prefix: str):
        reg = SourceFactRegistry(tmp_path)
        barrier.wait()
        for i in range(5):
            v = f"{i + 1:064x}"
            reg.register(
                SourceFactObservation(
                    provider="fake",
                    endpoint="e",
                    source_key=f"{prefix}{i}",
                    fact_version=v,
                    snapshot_identity=v,
                    observation_identity=v,
                    request_identity=v,
                    payload_sha256=v,
                    source_value_sha256=v,
                    provider_first_observed_at=now,
                    snapshot_fetched_at=now,
                )
            )

    threads = [threading.Thread(target=worker, args=(p,)) for p in ("a", "b")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(SourceFactRegistry(tmp_path).observations) == 10


def test_orphan_and_temp_files_ignored(tmp_path: Path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reg = SourceFactRegistry(tmp_path)
    reg.register(_obs("a" * 64, now))
    (tmp_path / "records" / f"{'f' * 64}.json").write_text("orphan")
    (tmp_path / ".tmp-partial").write_text("partial")
    assert len(SourceFactRegistry(tmp_path).observations) == 1


def test_referenced_record_tamper_rejected(tmp_path: Path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reg = SourceFactRegistry(tmp_path)
    reg.register(_obs("a" * 64, now))
    path = next((tmp_path / "records").glob("*.json"))
    path.write_text("tampered")
    with pytest.raises(ValueError):
        SourceFactRegistry(tmp_path)


def test_earlier_manifest_tamper_rejected(tmp_path: Path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reg = SourceFactRegistry(tmp_path)
    reg.register(_obs("a" * 64, now))
    reg.register(_obs("b" * 64, now + timedelta(days=1), "a" * 64))
    manifests = sorted((tmp_path / "manifests").glob("*.json"))
    manifests[0].write_text("tampered")
    with pytest.raises(ValueError):
        SourceFactRegistry(tmp_path)


def test_generation_link_splice_rejected(tmp_path: Path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reg = SourceFactRegistry(tmp_path)
    reg.register(_obs("a" * 64, now))
    reg.register(_obs("b" * 64, now + timedelta(days=1), "a" * 64))
    manifests = sorted((tmp_path / "manifests").glob("*.json"))
    data = json.loads(manifests[-1].read_text())
    data["previous_registry_identity"] = "0" * 64
    manifests[-1].write_text(json.dumps(data))
    with pytest.raises(ValueError):
        SourceFactRegistry(tmp_path)


def test_immutable_record_collision_rejected(tmp_path: Path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reg = SourceFactRegistry(tmp_path)
    reg.register(_obs("a" * 64, now))
    path = next((tmp_path / "records").glob("*.json"))
    path.write_text("different")
    with pytest.raises(ValueError):
        SourceFactRegistry(tmp_path)


def test_multiprocess_append(tmp_path: Path):
    ctx = multiprocessing.get_context("fork")
    ps = [ctx.Process(target=_process_append, args=(str(tmp_path), p)) for p in ("a", "b")]
    [p.start() for p in ps]
    [p.join(10) for p in ps]
    assert all(p.exitcode == 0 for p in ps) and len(SourceFactRegistry(tmp_path).observations) == 10

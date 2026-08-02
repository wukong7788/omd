import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ohmydata.core.errors import SnapshotConflictError, SnapshotIntegrityError
from ohmydata.core.snapshot import SnapshotMode, SnapshotObservationRef, SnapshotRef, SnapshotStore
from ohmydata.core.specs import RequestSpec


def test_append_roundtrip_and_idempotency(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {"symbol": "SYNTH"}, ("close",))
    store = SnapshotStore(tmp_path)
    when = datetime(2024, 1, 1, tzinfo=UTC)
    first = store.write(spec, b"abc", when, "json-rows-v1")
    second = store.write(spec, b"abc", datetime(2025, 1, 1, tzinfo=UTC), "json-rows-v1")
    assert first == second
    replay = store.replay(first, spec)
    assert replay.payload == b"abc"


def test_append_different_payloads_and_frozen_conflict(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    when = datetime(2024, 1, 1, tzinfo=UTC)
    a = store.write(spec, b"a", when, "json-v1")
    b = store.write(spec, b"b", when, "json-v1")
    assert a.path != b.path
    frozen = store.write(spec, b"a", when, "json-v1", SnapshotMode.FROZEN)
    assert store.replay(frozen).payload == b"a"


@pytest.mark.parametrize(
    "field",
    [
        "manifest_schema_version",
        "provider",
        "endpoint",
        "canonical_request",
        "request_identity",
        "response_sha256",
        "response_byte_size",
        "serialization_identifier",
        "retrieved_at",
        "snapshot_identity",
        "mode",
        "path",
    ],
)
def test_replay_rejects_manifest_tampering(tmp_path: Path, field: str) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    ref = SnapshotStore(tmp_path).write(spec, b"abc", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    manifest_path = ref.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[field] = (
        "tampered" if field not in {"manifest_schema_version", "response_byte_size"} else 999
    )
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(SnapshotIntegrityError):
        SnapshotStore(tmp_path).replay(ref, spec)


def test_replay_rejects_missing_truncated_and_expected_request(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    ref = store.write(spec, b"abc", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    (ref.path / "response.bin").write_bytes(b"a")
    with pytest.raises(SnapshotIntegrityError):
        store.replay(ref)

    other = RequestSpec("synthetic", "bars", {"different": True}, ())
    fresh = store.write(other, b"z", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    with pytest.raises(SnapshotIntegrityError):
        store.replay(fresh, spec)


def test_malformed_existing_winner_is_integrity_error(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    ref = store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    (ref.path / "manifest.json").write_text("{}")
    with pytest.raises(SnapshotIntegrityError):
        store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")


def test_canonical_and_extra_field_tamper_rejected_without_expected(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    ref = store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    manifest_path = ref.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["canonical_request"]["provider"] = "other"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(SnapshotIntegrityError):
        store.replay(ref)
    manifest["canonical_request"] = spec.canonical_payload
    manifest["extra"] = True
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(SnapshotIntegrityError):
        store.replay(ref)
    (ref.path / "response.bin").unlink()
    with pytest.raises(SnapshotIntegrityError):
        store.replay(ref)


def test_concurrent_append_same_payload_is_idempotent(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)

    def write() -> SnapshotRef:
        return store.write(spec, b"abc", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")

    def worker(_: int) -> SnapshotRef:
        return write()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, [0, 1, 2, 3]))
    assert len({r.path for r in results}) == 1
    assert not list(tmp_path.rglob(".tmp-*"))


def test_concurrent_append_different_payloads_are_distinct(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)

    def worker(payload: bytes) -> SnapshotRef:
        return store.write(spec, payload, datetime(2024, 1, 1, tzinfo=UTC), "json-v1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        refs = list(pool.map(worker, [b"a", b"b"]))
    assert {store.replay(ref).payload for ref in refs} == {b"a", b"b"}
    assert not list(tmp_path.rglob(".tmp-*"))


def test_concurrent_frozen_same_and_different_payloads(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)

    def worker(payload: bytes) -> SnapshotRef | Exception:
        try:
            return store.write(
                spec, payload, datetime(2024, 1, 1, tzinfo=UTC), "json-v1", SnapshotMode.FROZEN
            )
        except SnapshotConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        same = list(pool.map(worker, [b"a", b"a"]))
    assert same[0] == same[1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(worker, [b"a", b"b"]))
    assert sum(isinstance(item, SnapshotConflictError) for item in outcomes) == 1


def test_pre_publish_interruption_leaves_no_final_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)

    def interrupt(*args: object, **kwargs: object) -> None:
        raise RuntimeError("interrupted")

    monkeypatch.setattr("ohmydata.core.snapshot.os.rename", interrupt)
    with pytest.raises(RuntimeError):
        store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    assert not list(tmp_path.rglob("response.bin"))
    assert not list(tmp_path.rglob(".tmp-*"))


def test_pre_publish_oserror_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)

    def interrupt(*_args: object) -> None:
        raise OSError("stop")

    monkeypatch.setattr("ohmydata.core.snapshot.os.rename", interrupt)
    with pytest.raises(OSError):
        store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    assert not list(tmp_path.rglob("response.bin"))
    assert not list(tmp_path.rglob(".tmp-*"))


def test_fresh_frozen_different_race(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)

    def worker(payload: bytes) -> SnapshotRef | Exception:
        try:
            return store.write(
                spec, payload, datetime(2024, 1, 1, tzinfo=UTC), "json-v1", SnapshotMode.FROZEN
            )
        except SnapshotConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, [b"a", b"b"]))
    assert sum(isinstance(x, SnapshotConflictError) for x in results) == 1
    winner = next(x for x in results if isinstance(x, SnapshotRef))
    assert store.replay(winner).payload in {b"a", b"b"}
    assert not list(tmp_path.rglob(".tmp-*"))


@pytest.mark.parametrize(
    "field",
    [
        "provider",
        "endpoint",
        "request_identity",
        "response_sha256",
        "serialization_identifier",
        "snapshot_identity",
        "mode",
        "path",
    ],
)
def test_snapshot_ref_metadata_tamper(tmp_path: Path, field: str) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    ref = store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    bad = replace(ref, **{field: (Path(tmp_path) / "bad" if field == "path" else "tampered")})
    with pytest.raises(SnapshotIntegrityError):
        store.replay(bad)


def test_non_utc_timestamp_rejected(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    ref = store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    manifest = json.loads((ref.path / "manifest.json").read_text())
    manifest["retrieved_at"] = "2024-01-01T08:00:00+08:00"
    (ref.path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SnapshotIntegrityError):
        store.replay(ref)


def test_write_normalizes_aware_timestamp_to_utc(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    ref = SnapshotStore(tmp_path).write(
        spec, b"a", datetime(2024, 1, 1, 8, tzinfo=timezone(timedelta(hours=8))), "json-v1"
    )
    assert json.loads((ref.path / "manifest.json").read_text())["retrieved_at"].endswith("Z")


def test_fresh_frozen_same_race(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)

    def worker(_: int) -> SnapshotRef:
        return store.write(
            spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1", SnapshotMode.FROZEN
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        refs = list(pool.map(worker, [1, 2]))
    assert refs[0] == refs[1] and store.replay(refs[0]).payload == b"a"
    assert not list(tmp_path.rglob(".tmp-*"))


@pytest.mark.parametrize(
    "component", ["provider", "endpoint", "request", "mode", "serialization", "response"]
)
def test_path_component_tamper_rejected(tmp_path: Path, component: str) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    ref = store.write(spec, b"abc", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    parts = list(ref.path.parts)
    if component in {"serialization", "response"}:
        parts[-1 if component == "response" else -2] = "0" * 64
    else:
        parts[{"provider": -6, "endpoint": -5, "request": -4, "mode": -3}[component]] = "tampered"
    bad_path = Path(*parts)
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ref.path, bad_path, dirs_exist_ok=True)
    with pytest.raises(SnapshotIntegrityError):
        store.replay(replace(ref, path=bad_path))


def test_frozen_serialization_conflict(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1", SnapshotMode.FROZEN)
    with pytest.raises(SnapshotConflictError):
        store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "csv-v1", SnapshotMode.FROZEN)


@pytest.mark.parametrize("mutator", ["nan", "shape", "provider", "endpoint"])
def test_manifest_canonical_validation_without_expected(tmp_path: Path, mutator: str) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    ref = store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    manifest = json.loads((ref.path / "manifest.json").read_text())
    if mutator == "nan":
        manifest["canonical_request"]["parameters"] = {"x": float("nan")}
    elif mutator == "shape":
        manifest["canonical_request"] = {"provider": "synthetic"}
    else:
        manifest["canonical_request"][mutator] = "other"
    (ref.path / "manifest.json").write_text(json.dumps(manifest, allow_nan=True))
    with pytest.raises(SnapshotIntegrityError):
        store.replay(ref)


@pytest.mark.parametrize(
    "payload, timestamp, serialization, mode",
    [
        ("x", datetime(2024, 1, 1, tzinfo=UTC), "json-v1", SnapshotMode.APPEND),
        (b"x", datetime(2024, 1, 1), "json-v1", SnapshotMode.APPEND),  # noqa: DTZ001
        (b"x", datetime(2024, 1, 1, tzinfo=UTC), ".", SnapshotMode.APPEND),
        (b"x", datetime(2024, 1, 1, tzinfo=UTC), "a/b", SnapshotMode.APPEND),
        (b"x", datetime(2024, 1, 1, tzinfo=UTC), "json-v1", "append"),
    ],
)
def test_write_input_validation(
    tmp_path: Path, payload: object, timestamp: datetime, serialization: str, mode: object
) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    with pytest.raises((TypeError, ValueError)):
        SnapshotStore(tmp_path).write(spec, payload, timestamp, serialization, mode)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["response", "canonical", "serialization", "timestamp"])
def test_existing_winner_tamper_never_idempotent(tmp_path: Path, field: str) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    ref = store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    if field == "response":
        (ref.path / "response.bin").write_bytes(b"tampered")
    else:
        manifest = json.loads((ref.path / "manifest.json").read_text())
        manifest[
            {
                "canonical": "canonical_request",
                "serialization": "serialization_identifier",
                "timestamp": "retrieved_at",
            }[field]
        ] = {"provider": "x"} if field == "canonical" else "tampered"
        (ref.path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SnapshotIntegrityError):
        store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")


def test_observation_roundtrip_order_and_fact_version(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    first = store.observe(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    later = store.observe(spec, b"a", datetime(2024, 1, 2, tzinfo=UTC), "json-v1")
    snapshot = store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    refs = store.observations(snapshot)
    assert refs == (first, later)
    assert first.fact_version == later.fact_version == snapshot.fact_version
    assert store.replay_observation(first).payload == b"a"
    assert store.provider_first_observed_at(snapshot) == datetime(2024, 1, 1, tzinfo=UTC)


def test_replay_observation_rejects_forged_reference(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    ref = store.observe(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    with pytest.raises(SnapshotIntegrityError):
        store.replay_observation(replace(ref, provider="../escape"))
    with pytest.raises(SnapshotIntegrityError):
        store.replay_observation(replace(ref, path=tmp_path / "other"))


def test_observation_schema_version_bool_rejected(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    ref = store.observe(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    manifest_path = ref.path
    manifest = json.loads(manifest_path.read_text())
    manifest["observation_schema_version"] = True
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(SnapshotIntegrityError):
        store.replay_observation(ref)


def test_fact_version_contract_and_mode_independence(tmp_path: Path) -> None:
    import hashlib

    spec = RequestSpec("synthetic", "bars", {"x": 1}, ("close",))
    store = SnapshotStore(tmp_path)
    a = store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1", SnapshotMode.APPEND)
    f = store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1", SnapshotMode.FROZEN)
    expected = hashlib.sha256(
        b'{"request_identity":"'
        + spec.request_identity.encode()
        + b'","response_sha256":"'
        + hashlib.sha256(b"a").hexdigest().encode()
        + b'","serialization_identifier":"json-v1"}'
    ).hexdigest()
    assert a.fact_version == f.fact_version == expected
    assert (
        store.write(spec, b"b", datetime(2024, 1, 1, tzinfo=UTC), "json-v1").fact_version
        != a.fact_version
    )
    assert (
        store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "csv-v1").fact_version
        != a.fact_version
    )
    other = RequestSpec("synthetic", "bars", {"x": 2}, ("close",))
    assert (
        store.write(other, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1").fact_version
        != a.fact_version
    )


def test_legacy_manifest_and_first_observed(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    when = datetime(2024, 1, 1, tzinfo=UTC)
    ref = store.write(spec, b"a", when, "json-v1")
    assert frozenset(json.loads((ref.path / "manifest.json").read_text())) == frozenset(
        {
            "manifest_schema_version",
            "provider",
            "endpoint",
            "canonical_request",
            "request_identity",
            "response_sha256",
            "response_byte_size",
            "serialization_identifier",
            "retrieved_at",
            "snapshot_identity",
            "mode",
        }
    )
    assert store.observations(ref) == ()
    assert store.provider_first_observed_at(ref) == when
    assert store.write(spec, b"a", datetime(2025, 1, 1, tzinfo=UTC), "json-v1") == ref
    assert store.provider_first_observed_at(ref) == when


@pytest.mark.parametrize(
    "field",
    [
        "observation_schema_version",
        "provider",
        "endpoint",
        "request_identity",
        "response_sha256",
        "serialization_identifier",
        "snapshot_identity",
        "fact_version",
        "mode",
        "snapshot_fetched_at",
        "observation_identity",
    ],
)
def test_observation_manifest_each_field_tamper(tmp_path: Path, field: str) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    ref = store.observe(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    data = json.loads(ref.path.read_text())
    data[field] = 2 if field == "observation_schema_version" else "tampered"
    ref.path.write_text(json.dumps(data))
    with pytest.raises(SnapshotIntegrityError):
        store.replay_observation(ref)


@pytest.mark.parametrize(
    "field",
    [
        "path",
        "observation_identity",
        "snapshot_identity",
        "fact_version",
        "mode",
        "provider",
        "endpoint",
        "request_identity",
        "response_sha256",
        "serialization_identifier",
        "snapshot_fetched_at",
    ],
)
def test_observation_ref_each_field_tamper(tmp_path: Path, field: str) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    ref = store.observe(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    value = (
        tmp_path / "../escape"
        if field == "path"
        else (datetime(2024, 1, 1) if field == "snapshot_fetched_at" else "../escape")  # noqa: DTZ001
    )
    with pytest.raises((SnapshotIntegrityError, TypeError, ValueError)):
        store.replay_observation(replace(ref, **{field: value}))


def test_observation_input_validation_and_expected_request(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    with pytest.raises((TypeError, ValueError)):
        store.observe(spec, "x", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.observe(spec, b"x", datetime(2024, 1, 1, tzinfo=UTC), ".")
    with pytest.raises(TypeError):
        store.observe(spec, b"x", datetime(2024, 1, 1, tzinfo=UTC), "json-v1", "append")  # type: ignore[arg-type]
    ref = store.observe(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    with pytest.raises(SnapshotIntegrityError):
        store.replay_observation(ref, RequestSpec("synthetic", "bars", {"x": 1}, ()))


def test_observation_backdating_and_revision_ledgers(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    first = store.observe(spec, b"a", datetime(2024, 1, 2, tzinfo=UTC), "json-v1")
    with pytest.raises(SnapshotIntegrityError):
        store.observe(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    second = store.observe(spec, b"b", datetime(2024, 1, 3, tzinfo=UTC), "json-v1")
    assert first.fact_version != second.fact_version
    assert (
        len(
            store.observations(store.write(spec, b"a", datetime(2024, 1, 2, tzinfo=UTC), "json-v1"))
        )
        == 1
    )
    assert (
        len(
            store.observations(store.write(spec, b"b", datetime(2024, 1, 3, tzinfo=UTC), "json-v1"))
        )
        == 1
    )


def test_observation_offset_z_and_frozen_behavior(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    ref = store.observe(
        spec, b"a", datetime(2024, 1, 1, 8, tzinfo=timezone(timedelta(hours=8))), "json-v1"
    )
    assert ref.snapshot_fetched_at.tzinfo == UTC
    assert json.loads(ref.path.read_text())["snapshot_fetched_at"].endswith("Z")
    store.observe(spec, b"a", datetime(2024, 1, 2, tzinfo=UTC), "json-v1", SnapshotMode.FROZEN)
    with pytest.raises(SnapshotConflictError):
        store.observe(spec, b"b", datetime(2024, 1, 3, tzinfo=UTC), "json-v1", SnapshotMode.FROZEN)


@pytest.mark.parametrize("bad", [datetime(2024, 1, 1), "x"])  # noqa: DTZ001
def test_observe_timestamp_validation(tmp_path: Path, bad: object) -> None:
    with pytest.raises(TypeError):
        SnapshotStore(tmp_path).observe(RequestSpec("p", "e", {}), b"x", bad, "json-v1")  # type: ignore[arg-type]


def test_observation_manifest_malformed_missing_extra_nonutc_and_ledger(tmp_path: Path) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    ref = store.observe(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    data = json.loads(ref.path.read_text())
    ref.path.write_text("{")
    with pytest.raises(SnapshotIntegrityError):
        store.replay_observation(ref)
    ref.path.write_text(json.dumps(data))
    data.pop("mode")
    ref.path.write_text(json.dumps(data))
    with pytest.raises(SnapshotIntegrityError):
        store.replay_observation(ref)
    data["mode"] = "append"
    data["extra"] = 1
    ref.path.write_text(json.dumps(data))
    with pytest.raises(SnapshotIntegrityError):
        store.replay_observation(ref)
    data.pop("extra")
    data["snapshot_fetched_at"] = "2024-01-01T00:00:00+08:00"
    ref.path.write_text(json.dumps(data))
    with pytest.raises(SnapshotIntegrityError):
        store.replay_observation(ref)
    ref.path.parent.joinpath("junk").mkdir()
    with pytest.raises(SnapshotIntegrityError):
        store.observations(store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1"))


def test_concurrent_receipts_and_publish_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    snap = store.write(spec, b"a", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")

    def worker(day: int) -> SnapshotObservationRef:
        return store.observe(spec, b"a", datetime(2024, 1, day, tzinfo=UTC), "json-v1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        same = list(pool.map(lambda _: worker(2), [1, 2]))  # type: ignore[arg-type]
        diff = list(pool.map(worker, [3, 4]))
    assert same[0] == same[1]
    assert [x.snapshot_fetched_at for x in store.observations(snap)] == sorted(
        x.snapshot_fetched_at for x in store.observations(snap)
    )
    assert len(diff) == 2
    assert [r.snapshot_fetched_at.day for r in store.observations(snap)] == [2, 3, 4]
    existing_receipts = list(tmp_path.rglob("observation.json"))
    original = (
        tmp_path
        / "synthetic"
        / "bars"
        / spec.request_identity
        / "observations"
        / snap.snapshot_identity
    )
    real_rename = __import__("os").rename

    def fail_once(src: object, dst: object) -> None:
        if str(src).startswith(str(original / ".tmp-")):
            raise RuntimeError("interrupt")
        real_rename(src, dst)

    monkeypatch.setattr("ohmydata.core.snapshot.os.rename", fail_once)
    with pytest.raises(RuntimeError):
        store.observe(spec, b"a", datetime(2024, 1, 5, tzinfo=UTC), "json-v1")
    assert list(tmp_path.rglob("observation.json")) == existing_receipts
    assert not list(tmp_path.rglob(".tmp-*"))


def test_snapshot_module_has_no_provider_or_dataframe_imports() -> None:
    source = Path(__file__).parents[2].joinpath("src/ohmydata/core/snapshot.py").read_text()
    assert all(token not in source for token in ("pandas", "polars", "tushare"))


def test_observe_invalid_serialization_and_mode(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    spec = RequestSpec("p", "e", {})
    with pytest.raises(ValueError):
        store.observe(spec, b"x", datetime(2024, 1, 1, tzinfo=UTC), ".")
    with pytest.raises(TypeError):
        store.observe(spec, b"x", datetime(2024, 1, 1, tzinfo=UTC), "json-v1", "append")  # type: ignore[arg-type]


def test_replay_observation_linked_snapshot_tamper(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    spec = RequestSpec("p", "e", {})
    ref = store.observe(spec, b"x", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    snapshot = store.write(spec, b"x", datetime(2024, 1, 1, tzinfo=UTC), "json-v1")
    (snapshot.path / "response.bin").write_bytes(b"tamper")
    with pytest.raises(SnapshotIntegrityError):
        store.replay_observation(ref)


def test_backdated_observation_tamper_rejected_on_read(tmp_path: Path) -> None:
    import hashlib

    spec = RequestSpec("synthetic", "bars", {}, ())
    store = SnapshotStore(tmp_path)
    ref = store.observe(spec, b"a", datetime(2024, 1, 2, tzinfo=UTC), "json-v1")
    data = json.loads(ref.path.read_text())
    data["snapshot_fetched_at"] = "2024-01-01T00:00:00Z"
    data["observation_identity"] = hashlib.sha256(
        json.dumps(
            {
                "snapshot_fetched_at": data["snapshot_fetched_at"],
                "snapshot_identity": ref.snapshot_identity,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    new_path = ref.path.parent.parent / data["observation_identity"] / "observation.json"
    new_path.parent.mkdir()
    ref.path.unlink()
    new_path.write_text(json.dumps(data))
    forged = replace(
        ref,
        path=new_path,
        observation_identity=data["observation_identity"],
        snapshot_fetched_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(SnapshotIntegrityError):
        store.replay_observation(forged)

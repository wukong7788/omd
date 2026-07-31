import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ohmydata.core.errors import SnapshotConflictError, SnapshotIntegrityError
from ohmydata.core.snapshot import SnapshotMode, SnapshotRef, SnapshotStore
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

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import pytest

from ohmydata.core.errors import SnapshotIntegrityError
from ohmydata.core.snapshot import SnapshotStore
from ohmydata.core.specs import RequestSpec


def test_replay_payload_limit_is_exact_and_default_is_compatible(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    ref = store.write(
        RequestSpec("test", "payload", {}), b"abcd", datetime(2024, 1, 1, tzinfo=UTC), "bytes"
    )
    assert store.replay(ref).payload == b"abcd"
    assert store.replay(ref, max_payload_bytes=4).payload == b"abcd"
    with pytest.raises(SnapshotIntegrityError, match="exceeds limit"):
        store.replay(ref, max_payload_bytes=3)
    with pytest.raises(ValueError, match="invalid payload limit"):
        store.replay(ref, max_payload_bytes=-1)


@pytest.mark.parametrize("observation_replay", [False, True])
@pytest.mark.parametrize("limit", [0, 3, 4, 5])
def test_every_payload_read_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observation_replay: bool, limit: int
) -> None:
    store = SnapshotStore(tmp_path)
    spec = RequestSpec("test", "payload", {})
    fetched = datetime(2024, 1, 1, tzinfo=UTC)
    observation = store.observe(spec, b"abcd", fetched, "bytes")
    ref = store.write(spec, b"abcd", fetched, "bytes")
    original_open = Path.open
    reads: list[int] = []

    class ReadGuard:
        def __init__(self, handle: Any) -> None:
            self.handle = handle

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            self.handle.close()

        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            assert 0 <= size <= limit + 1, "unbounded internal payload read"
            return self.handle.read(size)

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        handle = original_open(path, *args, **kwargs)
        return ReadGuard(handle) if path.name == "response.bin" else handle

    monkeypatch.setattr(Path, "open", guarded_open)

    def replay() -> Any:
        if observation_replay:
            return store.replay_observation(observation, max_payload_bytes=limit)
        return store.replay(ref, max_payload_bytes=limit)

    if limit < 4:
        with pytest.raises(SnapshotIntegrityError, match="exceeds limit"):
            replay()
    else:
        assert replay().payload == b"abcd"
    assert reads and all(0 <= size <= limit + 1 for size in reads)


@pytest.mark.parametrize("limit", [-1, True, 1.5, "4"])
def test_invalid_limits_are_rejected_for_observation_replay(tmp_path: Path, limit: Any) -> None:
    store = SnapshotStore(tmp_path)
    observation = store.observe(
        RequestSpec("test", "payload", {}), b"abcd", datetime(2024, 1, 1, tzinfo=UTC), "bytes"
    )
    with pytest.raises(ValueError, match="invalid payload limit"):
        store.replay_observation(observation, max_payload_bytes=limit)

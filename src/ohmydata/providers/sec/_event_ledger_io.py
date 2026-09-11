"""Bounded POSIX filesystem operations for immutable discovery generations."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import chain
from pathlib import Path

from .errors import ResourceLimitError, SnapshotIntegrityError

MAX_MANIFEST_BYTES = 16 * 1024**2
MAX_TOTAL_MANIFEST_BYTES = 128 * 1024**2
MAX_GENERATIONS = 1024
STAGE_PREFIX = ".event-staging-"
LOCK_NAME = ".writer.lock"


def canonical_bytes(value: object) -> bytes:
    """Encode within the manifest budget, rejecting oversized/deep structures."""
    pending = [iter((value,))]
    nodes = 0
    while pending:
        try:
            item = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        nodes += 1
        if nodes > 500_000 or len(pending) > 65:
            raise ResourceLimitError("event manifest structure limit exceeded")
        if type(item) is dict:
            pending.append(chain.from_iterable(item.items()))
        elif type(item) is list:
            pending.append(iter(item))
        elif type(item) is str:
            if len(item) > 1024 or len(item.encode("utf-8")) > 1024:
                raise ResourceLimitError("event manifest string limit exceeded")
        elif item is not None and type(item) not in (int, bool):
            raise SnapshotIntegrityError("invalid event manifest value")
    chunks: list[bytes] = []
    size = 0
    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    for chunk in encoder.iterencode(value):
        encoded = chunk.encode("utf-8")
        size += len(encoded)
        if size > MAX_MANIFEST_BYTES:
            raise ResourceLimitError("event manifest bytes exceeded")
        chunks.append(encoded)
    return b"".join(chunks)


def decode_manifest(raw: bytes) -> dict[str, object]:
    depth = 0
    nodes = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
            nodes += 1
        elif byte in (91, 123):
            depth += 1
            nodes += 1
        elif byte in (93, 125):
            depth -= 1
        elif byte in (44, 58):
            nodes += 1
        if depth > 64 or nodes > 500_000:
            raise ResourceLimitError("event manifest JSON structure limit exceeded")

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise SnapshotIntegrityError("duplicate event manifest key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SnapshotIntegrityError("invalid event manifest JSON") from exc
    if type(value) is not dict or canonical_bytes(value) != raw:
        raise SnapshotIntegrityError("noncanonical event manifest")
    return value


@contextmanager
def root_directory(path: Path, *, create: bool) -> Iterator[int | None]:
    """Walk without following any ancestor symlink, using directory-relative FDs."""
    if os.name != "posix":
        raise NotImplementedError("SEC event ledger requires POSIX filesystem support")
    absolute = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    os.fsync(fd)
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if create:
                    raise
                yield None
                return
            except OSError as exc:
                raise SnapshotIntegrityError("unsafe event ledger directory") from exc
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def generation_names(fd: int, *, allow_execution: bool = False) -> list[str]:
    generations: list[str] = []
    stages = 0
    with os.scandir(fd) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_GENERATIONS + 1024 + 1:
                raise ResourceLimitError("event ledger directory entry limit exceeded")
            name = entry.name
            if entry.is_symlink():
                raise SnapshotIntegrityError("event ledger symlink rejected")
            if name == LOCK_NAME:
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_size != 0 or info.st_nlink != 1:
                    raise SnapshotIntegrityError("invalid event ledger lock")
            elif allow_execution and name == ".execution" and entry.is_dir(follow_symlinks=False):
                # Execution has its own bounded strict replay; discovery ledgers reject it.
                pass
            elif re.fullmatch(re.escape(STAGE_PREFIX) + r"[0-9a-f]{32}", name):
                stages += 1
                if stages > 1024 or not entry.is_dir(follow_symlinks=False):
                    raise ResourceLimitError("invalid or excessive event staging directories")
            elif name.startswith("generation-") and entry.is_dir(follow_symlinks=False):
                generations.append(name)
                if len(generations) > MAX_GENERATIONS:
                    raise ResourceLimitError("event generation limit exceeded")
            else:
                raise SnapshotIntegrityError("unexpected event ledger entry")
    generations.sort()
    if generations != [generation_name(i) for i in range(1, len(generations) + 1)]:
        raise SnapshotIntegrityError("event generation sequence has gaps or invalid names")
    return generations


def generation_name(generation: int) -> str:
    return f"generation-{generation:08d}"


def read_manifest(fd: int, name: str, remaining_bytes: int) -> bytes:
    directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        with os.scandir(directory) as entries:
            first = next(entries, None)
            if first is None or first.name != "manifest.json" or next(entries, None) is not None:
                raise SnapshotIntegrityError("partial or unexpected event generation content")
        info = os.stat("manifest.json", dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SnapshotIntegrityError("unsafe event manifest file")
        descriptor = os.open(
            "manifest.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SnapshotIntegrityError("unsafe event manifest file")
            limit = min(MAX_MANIFEST_BYTES, remaining_bytes)
            if info.st_size > limit:
                raise ResourceLimitError("event manifest read budget exceeded")
            chunks: list[bytes] = []
            size = 0
            while chunk := os.read(descriptor, min(65536, limit - size + 1)):
                size += len(chunk)
                if size > limit:
                    raise ResourceLimitError("event manifest read budget exceeded")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def publish_manifest(fd: int, generation: int, raw: bytes) -> None:
    stage = STAGE_PREFIX + uuid.uuid4().hex
    os.mkdir(stage, mode=0o700, dir_fd=fd)
    directory = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        descriptor = os.open(
            "manifest.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
        target = generation_name(generation)
        try:
            os.stat(target, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise SnapshotIntegrityError("event generation already exists")
        os.rename(stage, target, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(directory)
        # Interrupted staging is intentionally retained; replay ignores it.

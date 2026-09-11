"""Bounded receipt locators and one crash-released per-work POSIX lock."""

from __future__ import annotations

import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import _event_ledger_io as io
from .errors import ResourceLimitError, SnapshotIntegrityError

MAX_RECEIPT_BYTES = 256 * 1024
DIRECTORY = ".execution"


@contextmanager
def execution_lock(root: Path):
    import fcntl

    with io.root_directory(root, create=True) as root_fd:
        assert root_fd is not None
        try:
            os.mkdir(DIRECTORY, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        else:
            os.fsync(root_fd)
        fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            with os.scandir(fd) as entries:
                for index, entry in enumerate(entries):
                    if index >= 32:
                        raise ResourceLimitError("execution metadata entry limit exceeded")
                    if (
                        entry.is_symlink()
                        or not entry.is_file(follow_symlinks=False)
                        or not re.fullmatch(
                            r"lock|[0-9a-f]{64}-(acquire|validate)\.json|stage-[0-9a-f]{32}",
                            entry.name,
                        )
                        or entry.stat(follow_symlinks=False).st_size > MAX_RECEIPT_BYTES
                    ):
                        raise SnapshotIntegrityError("unsafe execution metadata entry")
            lock = os.open(
                "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd
            )
            try:
                info = os.fstat(lock)
                if not stat.S_ISREG(info.st_mode) or info.st_size != 0 or info.st_nlink != 1:
                    raise SnapshotIntegrityError("unsafe execution lock")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError("SEC event execution busy") from exc
                os.fsync(fd)
                yield fd
            finally:
                os.close(lock)
        finally:
            os.close(fd)


def read_locator(fd: int, name: str) -> dict[str, object] | None:
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECEIPT_BYTES:
            raise SnapshotIntegrityError("unsafe execution receipt locator")
        chunks = []
        total = 0
        while part := os.read(file_fd, min(65536, MAX_RECEIPT_BYTES + 1 - total)):
            chunks.append(part)
            total += len(part)
            if total > MAX_RECEIPT_BYTES:
                raise ResourceLimitError("execution locator byte limit exceeded")
        return io.decode_manifest(b"".join(chunks))
    finally:
        os.close(file_fd)


def check_root(fd: int, root: Path) -> None:
    """Reject path replacement while a caller callback holds the original lock."""
    with io.root_directory(root, create=False) as root_fd:
        if root_fd is None:
            raise SnapshotIntegrityError("execution root disappeared")
        candidate = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            actual, expected = os.fstat(candidate), os.fstat(fd)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise SnapshotIntegrityError("execution directory replaced during operation")
        finally:
            os.close(candidate)


def write_locator(fd: int, name: str, value: dict[str, object]) -> None:
    raw = io.canonical_bytes(value)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ResourceLimitError("execution locator byte limit exceeded")
    prior = read_locator(fd, name)
    if prior is not None:
        if io.canonical_bytes(prior) != raw:
            raise SnapshotIntegrityError("execution receipt locator conflict")
        return
    temporary = f"stage-{uuid.uuid4().hex}"
    file_fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
    )
    try:
        position = 0
        while position < len(raw):
            written = os.write(file_fd, raw[position:])
            if written <= 0:
                raise OSError("execution receipt write made no progress")
            position += written
        os.fsync(file_fd)
    finally:
        os.close(file_fd)
    try:
        os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.fsync(fd)
    except FileExistsError:
        prior = read_locator(fd, name)
        if prior is None or io.canonical_bytes(prior) != raw:
            raise SnapshotIntegrityError("execution receipt locator conflict") from None
    finally:
        os.unlink(temporary, dir_fd=fd)
        os.fsync(fd)

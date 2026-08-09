"""Cross-platform advisory file locking for the source-fact registry."""

from contextlib import contextmanager
from importlib import import_module
from typing import Any

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows backend
    _fcntl = None


@contextmanager
def file_lock(handle: Any):
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        return
    msvcrt: Any = import_module("msvcrt")

    handle.seek(0)
    handle.write(b"0")
    handle.flush()
    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    try:
        yield
    finally:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

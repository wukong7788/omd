"""Shared helpers for pure Tushare evidence recipes."""

from __future__ import annotations

import hashlib
from typing import Any

from ..observations import serialize_tushare_frame


def canonical_frame_hash(frame: Any) -> str:
    """Canonical content hash for a derived evidence frame.

    Reuses the deterministic cell encoding so equal frames hash equal across
    runs and processes.
    """
    return hashlib.sha256(serialize_tushare_frame(frame)).hexdigest()


__all__ = ["canonical_frame_hash"]

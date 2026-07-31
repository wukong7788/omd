"""Tushare exception classification with safe, redacted messages."""

from __future__ import annotations

import socket

from ...core.errors import (
    AuthenticationError,
    OhMyDataError,
    PermanentProviderError,
    PermissionDeniedError,
    RateLimitError,
    TransientProviderError,
)

_AUTH = ("token", "auth", "authentication", "invalid api", "密钥", "登录")
_PERM = ("permission", "forbidden", "权限", "无权限")
_RATE = ("rate limit", "频率", "too many", "请求过于频繁")


def classify_tushare_exception(exc: Exception) -> OhMyDataError:
    if isinstance(exc, OhMyDataError):
        return exc
    if isinstance(exc, (TimeoutError, ConnectionError, socket.timeout, socket.gaierror)):
        return TransientProviderError("transient Tushare provider failure")
    text = str(exc).lower()
    if any(x in text for x in _RATE):
        return RateLimitError("Tushare rate limit reached")
    if any(x in text for x in _PERM):
        return PermissionDeniedError("Tushare permission denied")
    if any(x in text for x in _AUTH):
        return AuthenticationError("Tushare authentication failed")
    return PermanentProviderError("Tushare provider failure")


__all__ = ["classify_tushare_exception"]

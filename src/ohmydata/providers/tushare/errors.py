"""Tushare exception classification with safe codes and redacted messages."""

from __future__ import annotations

import re
import socket

from ...core.errors import (
    AuthenticationError,
    OhMyDataError,
    PermanentProviderError,
    PermissionDeniedError,
    ProviderError,
    RateLimitError,
    TransientProviderError,
    UnknownProviderError,
    UnsupportedProviderError,
)

_SAFE_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}\Z")
_SENSITIVE_CODE_WORDS = ("token", "secret", "password", "credential", "account")

# Tushare 1.4.29 discards its response code and raises only result['msg'].
# These complete synthetic phrases are the only generic-message classifications;
# broader provider messages deliberately remain unknown.
_SYNTHETIC_PATTERNS: tuple[tuple[re.Pattern[str], type[ProviderError]], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), error_type)
    for pattern, error_type in (
        (
            r"(?:invalid api token|invalid token|token invalid|authentication failed)",
            AuthenticationError,
        ),
        (r"(?:permission denied|forbidden|access denied|无权限)", PermissionDeniedError),
        (r"(?:rate limit exceeded|too many requests|请求过于频繁)", RateLimitError),
        (r"(?:unsupported api|unsupported endpoint|not supported)", UnsupportedProviderError),
        (r"(?:invalid parameter|invalid argument)", PermanentProviderError),
    )
)


def _safe_provider_code(exc: Exception) -> int | str | None:
    for attribute in ("code", "status_code"):
        try:
            value = getattr(exc, attribute, None)
        except Exception:  # noqa: BLE001,S112 - ignore unsafe provider properties and try fallback
            continue
        if type(value) is int:
            return value
        if (
            isinstance(value, str)
            and _SAFE_CODE.fullmatch(value)
            and not any(word in value.lower() for word in _SENSITIVE_CODE_WORDS)
        ):
            return value
    return None


def classify_tushare_exception(exc: Exception) -> OhMyDataError:
    if isinstance(exc, OhMyDataError):
        return exc
    provider_code = _safe_provider_code(exc)
    if isinstance(exc, (TimeoutError, ConnectionError, socket.timeout, socket.gaierror)):
        return TransientProviderError(
            "transient Tushare provider failure", provider_code=provider_code
        )

    # The message is inspected transiently and never copied to the mapped error.
    try:
        message = str(exc).strip()
    except Exception:  # noqa: BLE001 - malformed provider exception text is unknown
        return UnknownProviderError("unknown Tushare provider failure", provider_code=provider_code)
    for pattern, error_type in _SYNTHETIC_PATTERNS:
        if pattern.fullmatch(message):
            return error_type("Tushare provider failure", provider_code=provider_code)
    return UnknownProviderError("unknown Tushare provider failure", provider_code=provider_code)


__all__ = ["classify_tushare_exception"]

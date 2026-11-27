"""Sharadar exception classification and strict secret redaction."""

from __future__ import annotations

import urllib.parse

from ohmydata.core.errors import (
    AuthenticationError,
    PermanentProviderError,
    PermissionDeniedError,
    ProviderError,
    RateLimitError,
    TransientProviderError,
)


class SharadarError(ProviderError):
    """Base error for Sharadar Direct bulk download and reader operations."""


def redact_secret(text: str, api_key: str) -> str:
    """Redact plain and URL-encoded forms of ``api_key`` from ``text``."""
    if not text or not api_key:
        return text
    variants = {
        api_key,
        urllib.parse.quote(api_key, safe=""),
        urllib.parse.quote_plus(api_key),
    }
    redacted = text
    for variant in sorted((v for v in variants if v), key=len, reverse=True):
        redacted = redacted.replace(variant, "***")
    return redacted


def classify_sharadar_http_error(
    status_code: int,
    reason: str,
    api_key: str,
) -> ProviderError:
    """Map an HTTP status code from Sharadar Direct to an OMD ProviderError."""
    safe_reason = redact_secret(str(reason).strip(), api_key)
    message = (
        f"Sharadar HTTP {status_code}: {safe_reason}"
        if safe_reason
        else f"Sharadar HTTP {status_code}"
    )

    if status_code == 401:
        return AuthenticationError(message, provider_code=status_code)
    if status_code == 403:
        return PermissionDeniedError(message, provider_code=status_code)
    if status_code == 404:
        return PermanentProviderError(message, provider_code=status_code)
    if status_code == 429:
        return RateLimitError(message, provider_code=status_code)
    if 500 <= status_code <= 599:
        return TransientProviderError(message, provider_code=status_code)
    return PermanentProviderError(message, provider_code=status_code)


__all__ = [
    "SharadarError",
    "classify_sharadar_http_error",
    "redact_secret",
]

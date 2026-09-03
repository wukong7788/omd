"""SEC-specific error classification."""

from ...core.errors import (
    AuthenticationError,
    CoverageError,
    EmptyResponseError,
    PermanentProviderError,
    PermissionDeniedError,
    RateLimitError,
    SchemaMismatchError,
    SnapshotIntegrityError,
    TransientProviderError,
)

__all__ = [
    "AuthenticationError",
    "CoverageError",
    "EmptyResponseError",
    "PermanentProviderError",
    "PermissionDeniedError",
    "RateLimitError",
    "SchemaMismatchError",
    "SnapshotIntegrityError",
    "TransientProviderError",
]

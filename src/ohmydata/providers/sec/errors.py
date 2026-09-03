"""SEC-specific error classification."""

from ...core.errors import (
    AmbiguousPartitionError,
    AuthenticationError,
    CoverageError,
    EmptyResponseError,
    PermanentProviderError,
    PermissionDeniedError,
    RateLimitError,
    ResourceLimitError,
    SchemaMismatchError,
    SnapshotIntegrityError,
    TransientProviderError,
)

__all__ = [
    "AmbiguousPartitionError",
    "AuthenticationError",
    "CoverageError",
    "EmptyResponseError",
    "PermanentProviderError",
    "PermissionDeniedError",
    "RateLimitError",
    "ResourceLimitError",
    "SchemaMismatchError",
    "SnapshotIntegrityError",
    "TransientProviderError",
]

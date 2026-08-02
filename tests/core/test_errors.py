from ohmydata.core.errors import (
    AttemptRecord,
    RateLimitError,
    RetryExhaustedError,
    TransientProviderError,
)


def test_core_exports_are_explicit() -> None:
    from ohmydata import core

    assert "RequestSpec" in core.__all__ and "SnapshotStore" in core.__all__
    assert (
        not hasattr(core, "random") and not hasattr(core, "time") and not hasattr(core, "Generic")
    )
    assert "Callable" not in core.__all__ and "_finite_number" not in core.__all__
    expected = {
        "AuthenticationError",
        "AttemptRecord",
        "CoverageError",
        "EmptyDisposition",
        "EmptyResponseError",
        "FetchProvenance",
        "OhMyDataError",
        "PaginationError",
        "PermanentProviderError",
        "PermissionDeniedError",
        "ProviderError",
        "RateLimitDecision",
        "RateLimitError",
        "RateLimitPolicy",
        "RateLimiter",
        "RequestSpec",
        "RetryExhaustedError",
        "RetryPolicy",
        "RetryResult",
        "SchemaMismatchError",
        "SnapshotConflictError",
        "SnapshotIntegrityError",
        "SnapshotMode",
        "SnapshotObservationRef",
        "SnapshotRef",
        "SnapshotReplay",
        "SnapshotStore",
        "TransientProviderError",
        "execute_with_retry",
    }
    assert set(core.__all__) == expected


def test_hierarchy_and_secret_safe_exhaustion() -> None:
    assert issubclass(RateLimitError, TransientProviderError)
    err = RetryExhaustedError((AttemptRecord(1, "RateLimitError", 1.0),))
    assert "secret" not in repr(err).lower()
    assert isinstance(err.attempts, tuple)

from ohmydata.core.errors import (
    AttemptRecord,
    AuthenticationError,
    PermanentProviderError,
    PermissionDeniedError,
    ProviderError,
    ProviderErrorCategory,
    RateLimitError,
    RetryExhaustedError,
    TransientProviderError,
    UnknownProviderError,
    UnsupportedProviderError,
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
        "AvailabilityBasis",
        "AvailabilityEvidence",
        "AvailabilityPrecision",
        "RawFactEnvelope",
        "RawFactQualityFlag",
        "RawFactRevisionStatus",
        "CoverageError",
        "EmptyDisposition",
        "EmptyResponseError",
        "FetchProvenance",
        "IdentityConflictError",
        "InstrumentIdentity",
        "InstrumentIdentityIndex",
        "InstrumentResolution",
        "InstrumentType",
        "IssuerIdentity",
        "ProviderInstrumentAlias",
        "OhMyDataError",
        "PaginationError",
        "PermanentProviderError",
        "PermissionDeniedError",
        "ProviderError",
        "ProviderErrorCategory",
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
        "UnknownProviderError",
        "UnsupportedProviderError",
        "execute_with_retry",
        "SourceFactObservation",
        "SourceFactRegistry",
        "SourceFactRegistryManifest",
        "SourceFactRevisionRef",
        "SourceResolutionStatus",
    }
    assert set(core.__all__) == expected


def test_hierarchy_and_secret_safe_exhaustion() -> None:
    assert issubclass(RateLimitError, TransientProviderError)
    err = RetryExhaustedError((AttemptRecord(1, "RateLimitError", 1.0),))
    assert "secret" not in repr(err).lower()
    assert isinstance(err.attempts, tuple)


def test_provider_error_categories_preserve_hierarchy_and_defaults() -> None:
    assert issubclass(UnsupportedProviderError, PermanentProviderError)
    assert issubclass(UnknownProviderError, PermanentProviderError)
    assert ProviderError().category is ProviderErrorCategory.UNKNOWN
    assert PermanentProviderError("x").category is ProviderErrorCategory.PERMANENT
    assert AuthenticationError("x").category is ProviderErrorCategory.AUTHENTICATION
    assert PermissionDeniedError("x").category is ProviderErrorCategory.PERMISSION
    assert RateLimitError("x").category is ProviderErrorCategory.RATE_LIMIT
    assert TransientProviderError("x").category is ProviderErrorCategory.TRANSIENT


def test_provider_error_preserves_exception_positional_args() -> None:
    no_args = ProviderError()
    multiple_args = ProviderError("provider detail", 17)

    assert no_args.args == ()
    assert str(no_args) == ""
    assert multiple_args.args == ("provider detail", 17)
    assert str(multiple_args) == "('provider detail', 17)"

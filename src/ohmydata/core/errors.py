from dataclasses import dataclass
from enum import StrEnum


class ProviderErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    UNSUPPORTED = "unsupported"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


class OhMyDataError(Exception):
    pass


class ProviderError(OhMyDataError):
    default_category = ProviderErrorCategory.UNKNOWN

    def __init__(
        self,
        *args: object,
        category: ProviderErrorCategory | None = None,
        provider_code: int | str | None = None,
    ) -> None:
        super().__init__(*args)
        self.category = category or self.default_category
        self.provider_code = provider_code


class PermanentProviderError(ProviderError):
    default_category = ProviderErrorCategory.PERMANENT


class AuthenticationError(PermanentProviderError):
    default_category = ProviderErrorCategory.AUTHENTICATION


class PermissionDeniedError(PermanentProviderError):
    default_category = ProviderErrorCategory.PERMISSION


class UnsupportedProviderError(PermanentProviderError):
    default_category = ProviderErrorCategory.UNSUPPORTED


class UnknownProviderError(PermanentProviderError):
    default_category = ProviderErrorCategory.UNKNOWN


class EmptyResponseError(PermanentProviderError):
    pass


class SchemaMismatchError(PermanentProviderError):
    pass


class PaginationError(PermanentProviderError):
    pass


class TransientProviderError(ProviderError):
    default_category = ProviderErrorCategory.TRANSIENT


class RateLimitError(TransientProviderError):
    default_category = ProviderErrorCategory.RATE_LIMIT


class SnapshotIntegrityError(OhMyDataError):
    pass


class SnapshotConflictError(SnapshotIntegrityError):
    pass


class CoverageError(OhMyDataError):
    pass


class IdentityConflictError(OhMyDataError):
    pass


class AmbiguousPartitionError(OhMyDataError):
    pass


class ResourceLimitError(OhMyDataError):
    pass


@dataclass(frozen=True)
class AttemptRecord:
    attempt: int
    exception_type: str | None
    retry_delay_seconds: float | None


class RetryExhaustedError(TransientProviderError):
    def __init__(
        self,
        attempts: tuple[AttemptRecord, ...],
        *,
        category: ProviderErrorCategory | None = None,
        provider_code: int | str | None = None,
    ):
        self.attempts = attempts
        super().__init__(
            f"retry exhausted after {len(attempts)} attempts",
            category=category,
            provider_code=provider_code,
        )

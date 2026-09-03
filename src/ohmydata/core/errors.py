from dataclasses import dataclass


class OhMyDataError(Exception):
    pass


class ProviderError(OhMyDataError):
    pass


class PermanentProviderError(ProviderError):
    pass


class AuthenticationError(PermanentProviderError):
    pass


class PermissionDeniedError(PermanentProviderError):
    pass


class EmptyResponseError(PermanentProviderError):
    pass


class SchemaMismatchError(PermanentProviderError):
    pass


class PaginationError(PermanentProviderError):
    pass


class TransientProviderError(ProviderError):
    pass


class RateLimitError(TransientProviderError):
    pass


class SnapshotIntegrityError(OhMyDataError):
    pass


class SnapshotConflictError(SnapshotIntegrityError):
    pass


class CoverageError(OhMyDataError):
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
    def __init__(self, attempts: tuple[AttemptRecord, ...]):
        self.attempts = attempts
        super().__init__(f"retry exhausted after {len(attempts)} attempts")

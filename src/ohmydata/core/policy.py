import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from .errors import AttemptRecord, RetryExhaustedError, TransientProviderError

__all__ = ["AttemptRecord", "RetryPolicy", "RetryResult", "execute_with_retry"]

T = TypeVar("T")


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _valid_attempts(value: object) -> bool:
    return type(value) is int and value >= 1


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 60.0
    jitter_ratio: float = 0.0

    def __post_init__(self):
        if (
            not _valid_attempts(self.max_attempts)
            or any(
                not _finite_number(x)
                for x in (
                    self.base_delay_seconds,
                    self.backoff_multiplier,
                    self.max_delay_seconds,
                    self.jitter_ratio,
                )
            )
            or self.max_attempts < 1
            or self.base_delay_seconds < 0
            or self.backoff_multiplier < 1
            or self.max_delay_seconds < 0
            or not 0 <= self.jitter_ratio <= 1
        ):
            raise ValueError("invalid retry policy")


@dataclass(frozen=True)
class RetryResult(Generic[T]):
    value: T
    attempts: tuple[AttemptRecord, ...]


def execute_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    random_value: Callable[[], float] = random.random,
    classifier: Callable[[Exception], bool] = lambda e: isinstance(e, TransientProviderError),
) -> RetryResult[T]:
    policy = policy or RetryPolicy()
    records: list[AttemptRecord] = []
    for i in range(policy.max_attempts):
        try:
            value = fn()
            records.append(AttemptRecord(i + 1, None, None))
            return RetryResult(value, tuple(records))
        except Exception as exc:
            if not isinstance(exc, TransientProviderError) or not classifier(exc):
                raise
            delay = None
            if i + 1 < policy.max_attempts:
                r = random_value()
                if not 0 <= r <= 1:
                    raise ValueError("random value out of range")
                bounded = min(
                    policy.max_delay_seconds,
                    policy.base_delay_seconds * policy.backoff_multiplier**i,
                )
                delay = bounded * (1 + policy.jitter_ratio * (2 * r - 1))
                sleep(delay)
            records.append(AttemptRecord(i + 1, type(exc).__name__, delay))
            if i + 1 == policy.max_attempts:
                err = RetryExhaustedError(
                    tuple(
                        AttemptRecord(x.attempt, x.exception_type, x.retry_delay_seconds)
                        for x in records
                    )
                )
                err.__cause__ = exc
                raise err from exc
    raise AssertionError

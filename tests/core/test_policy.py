from typing import Any, cast

import pytest

from ohmydata.core.errors import (
    AttemptRecord,
    PermanentProviderError,
    RetryExhaustedError,
    TransientProviderError,
)
from ohmydata.core.policy import RetryPolicy, execute_with_retry


def test_retry_total_attempts_and_jitter() -> None:
    calls = 0
    sleeps: list[float] = []

    def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TransientProviderError()
        return "ok"

    result = execute_with_retry(
        fn, RetryPolicy(3, 2, 2, 10, 0.5), sleep=sleeps.append, random_value=lambda: 0.5
    )
    assert result.value == "ok" and calls == 3 and sleeps == [2.0, 4.0]


def test_permanent_failure_is_immediate() -> None:
    err = PermanentProviderError("x")
    with pytest.raises(PermanentProviderError) as raised:
        execute_with_retry(lambda: (_ for _ in ()).throw(err))
    assert raised.value is err


def test_unknown_exception_and_exhaustion_history() -> None:
    err = RuntimeError("opaque")
    with pytest.raises(RuntimeError) as raised:
        execute_with_retry(lambda: (_ for _ in ()).throw(err))
    assert raised.value is err
    final = TransientProviderError("hidden")
    with pytest.raises(RetryExhaustedError) as exhausted:
        execute_with_retry(
            lambda: (_ for _ in ()).throw(final), RetryPolicy(max_attempts=2), sleep=lambda _: None
        )
    assert exhausted.value.__cause__ is final and len(exhausted.value.attempts) == 2


def test_value_error_not_retried() -> None:
    calls = 0

    def fn() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("bad")

    with pytest.raises(ValueError):
        execute_with_retry(fn, RetryPolicy(3))
    assert calls == 1


@pytest.mark.parametrize(
    "field", ["base_delay_seconds", "backoff_multiplier", "max_delay_seconds", "jitter_ratio"]
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, "bad"])
def test_all_numeric_invalid(field: str, value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        RetryPolicy(**{field: value})  # type: ignore[arg-type]


def test_classifier_never_retries_non_transient() -> None:
    for exc in [ValueError("v"), TypeError("t"), PermanentProviderError("p"), RuntimeError("r")]:
        with pytest.raises(type(exc)):
            execute_with_retry(
                lambda exc=exc: (_ for _ in ()).throw(exc),
                RetryPolicy(3),
                classifier=lambda _: True,
            )


def test_max_attempts_bool_and_classifier_veto() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=True)  # type: ignore[arg-type]
    exc = TransientProviderError("hidden")
    with pytest.raises(TransientProviderError) as raised:
        execute_with_retry(
            lambda: (_ for _ in ()).throw(exc), RetryPolicy(3), classifier=lambda _: False
        )
    assert raised.value is exc


def test_exhaustion_history_exact_and_secret_safe() -> None:
    exc = TransientProviderError("hidden provider message")
    with pytest.raises(RetryExhaustedError) as raised:
        execute_with_retry(lambda: (_ for _ in ()).throw(exc), RetryPolicy(2), sleep=lambda _: None)
    assert all(isinstance(item, AttemptRecord) for item in raised.value.attempts)
    assert "hidden provider message" not in repr(raised.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"base_delay_seconds": -1.0},
        {"backoff_multiplier": 0.5},
        {"jitter_ratio": 2.0},
    ],
)
def test_policy_validation(kwargs: dict[str, int | float]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**cast(Any, kwargs))

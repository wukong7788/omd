from threading import Thread

import pytest

from ohmydata.core.rate_limit import RateLimitDecision, RateLimiter, RateLimitPolicy


def test_first_and_subsequent_and_regression() -> None:
    now = [0.0]
    sleeps: list[float] = []
    limiter = RateLimiter(RateLimitPolicy(1.0), lambda: now[0], sleeps.append)
    assert limiter.acquire().waited_seconds == 0
    now[0] = 0.2
    assert limiter.acquire().waited_seconds == 0.8
    now[0] = 0.1
    assert limiter.acquire().waited_seconds == 1.1
    assert all(x >= 0 for x in sleeps)


def test_instances_independent() -> None:
    sleeps: list[float] = []
    a = RateLimiter(RateLimitPolicy(1), lambda: 0, sleeps.append)
    b = RateLimiter(RateLimitPolicy(1), lambda: 0, sleeps.append)
    assert a.acquire().waited_seconds == b.acquire().waited_seconds == 0


def test_threaded_serialization() -> None:
    current = [0.0]
    sleeps: list[float] = []

    def sleep(x: float) -> None:
        sleeps.append(x)
        current[0] += x

    limiter = RateLimiter(RateLimitPolicy(0.1), lambda: current[0], sleep)
    decisions: list[RateLimitDecision] = []
    threads = [Thread(target=lambda: decisions.append(limiter.acquire())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(d.acquired_at for d in decisions) == pytest.approx([0.0, 0.1, 0.2, 0.3])
    assert sleeps == pytest.approx([0.1, 0.1, 0.1])


def test_validation() -> None:
    with pytest.raises(ValueError):
        RateLimitPolicy(-1)


@pytest.mark.parametrize("value", [True, "bad", float("nan"), float("inf"), float("-inf")])
def test_invalid_policy_matrix(value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        RateLimitPolicy(value)  # type: ignore[arg-type]


def test_nonfinite_clock_before_sleep() -> None:
    with pytest.raises(ValueError):
        RateLimiter(RateLimitPolicy(0), lambda: float("nan"), lambda _: None).acquire()


def test_nonfinite_clock_after_sleep() -> None:
    now = [0.0]

    def sleep(_: float) -> None:
        now[0] = float("inf")

    limiter = RateLimiter(RateLimitPolicy(1.0), lambda: now[0], sleep)
    limiter.acquire()
    now[0] = 0.1
    with pytest.raises(ValueError):
        limiter.acquire()

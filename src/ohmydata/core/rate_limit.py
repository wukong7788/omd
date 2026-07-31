import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitPolicy:
    min_interval_seconds: float

    def __post_init__(self):
        if (
            isinstance(self.min_interval_seconds, bool)
            or not math.isfinite(float(self.min_interval_seconds))
            or self.min_interval_seconds < 0
        ):
            raise ValueError("negative interval")


@dataclass(frozen=True)
class RateLimitDecision:
    waited_seconds: float
    acquired_at: float


class RateLimiter:
    def __init__(
        self,
        policy: RateLimitPolicy,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.policy, self.clock, self.sleep, self._last = policy, clock, sleep, None
        self._lock = threading.Lock()

    def acquire(self) -> RateLimitDecision:
        with self._lock:
            now = self.clock()
            if not math.isfinite(now):
                raise ValueError("clock must return finite value")
            wait = (
                0
                if self._last is None
                else max(0, self.policy.min_interval_seconds - (now - self._last))
            )
            if wait:
                self.sleep(wait)
                now = self.clock()
                if not math.isfinite(now):
                    raise ValueError("clock must return finite value")
            self._last = now
            return RateLimitDecision(wait, now)

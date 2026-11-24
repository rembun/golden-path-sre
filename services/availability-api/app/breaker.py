"""A small consecutive-failure circuit breaker.

States:
  closed    - calls go through; consecutive failures are counted.
  open      - calls are refused immediately (fail fast) for `reset_timeout_s`.
  half_open - after the timeout, exactly one trial call is let through.
              Success closes the breaker; failure opens it again.

Why it exists: during a venue-capacity outage, clients retry aggressively. Without
a breaker, every retry waits for the full timeout and adds load to a dependency
that is trying to recover. With it, we answer in milliseconds and send the
dependency almost nothing until it is healthy again.

The service runs one asyncio event loop per process, so no locking is needed.
State is per process (per pod), which is fine: each pod protects itself.
"""
import time
from typing import Callable

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int,
        reset_timeout_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._reset_timeout_s = reset_timeout_s
        self._clock = clock
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False

    @property
    def state(self) -> str:
        if self._state == OPEN and self._clock() - self._opened_at >= self._reset_timeout_s:
            return HALF_OPEN
        return self._state

    def allow(self) -> bool:
        """Return True if a call may be attempted now."""
        state = self.state
        if state == CLOSED:
            return True
        if state == HALF_OPEN and not self._trial_in_flight:
            self._trial_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        if self._state == OPEN and not self._trial_in_flight:
            # A slow call that started before the breaker opened. Ignore it:
            # only the half-open trial is allowed to close the breaker.
            return
        self._state = CLOSED
        self._failures = 0
        self._trial_in_flight = False

    def record_failure(self) -> None:
        if self._state == OPEN:
            if self._trial_in_flight:
                # The half-open trial failed: open again for a full timeout.
                self._trial_in_flight = False
                self._open()
            return
        self._failures += 1
        if self._failures >= self._threshold:
            self._open()

    def _open(self) -> None:
        self._state = OPEN
        self._opened_at = self._clock()
        self._failures = 0

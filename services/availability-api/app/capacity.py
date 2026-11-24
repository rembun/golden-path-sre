"""Client for the venue-capacity service, called on every availability check.

Design choices (see DECISIONS.md):
  * A hard deadline for the WHOLE call, well under the 300 ms SLO threshold. httpx's
    own timeout applies to each phase separately (pool wait, connect, write, read), so
    on its own a "150 ms" timeout can add up to several times that.
  * No retries. Retrying inside the request multiplies load on a struggling
    dependency, which is exactly how retry storms get worse.
  * A circuit breaker so that, once the dependency is clearly down, we fail fast.
"""
import asyncio
from dataclasses import dataclass

import httpx
from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

from .breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker

_meter = metrics.get_meter("availability-api")
# One counter, one low-cardinality label ("outcome"): ok, timeout, circuit_open,
# connection_error, auth_rejected, upstream_error, bad_response.
# Tells us WHY availability checks fail, which the HTTP SLI alone cannot.
_requests = _meter.create_counter(
    "capacity.requests",
    unit="{request}",
    description="Calls to the venue-capacity service, by outcome",
)

# Breaker state as a gauge (0 closed, 1 half-open, 2 open). Registered once per
# process; it reports the breaker of the most recently created client, which in
# a running service is the only one.
_BREAKER_VALUE = {CLOSED: 0, HALF_OPEN: 1, OPEN: 2}
_active_breaker: CircuitBreaker | None = None


def _observe_breaker(_: CallbackOptions):
    if _active_breaker is not None:
        yield Observation(_BREAKER_VALUE[_active_breaker.state])


_meter.create_observable_gauge(
    "capacity.circuit.state",
    callbacks=[_observe_breaker],
    description="Circuit breaker state for venue-capacity calls: 0 closed, 1 half-open, 2 open",
)


@dataclass(frozen=True)
class Capacity:
    accepting_bookings: bool


class CapacityUnavailable(Exception):
    """Live capacity could not be confirmed. `reason` is a low-cardinality label."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CapacityClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_s: float,
        breaker: CircuitBreaker,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        global _active_breaker
        self._breaker = breaker
        self._deadline_s = timeout_s
        _active_breaker = breaker
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
        )

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    async def check(self, venue_id: str) -> Capacity:
        if not self._breaker.allow():
            _requests.add(1, {"outcome": "circuit_open"})
            raise CapacityUnavailable("circuit_open")
        try:
            async with asyncio.timeout(self._deadline_s):
                response = await self._client.get(f"/venues/{venue_id}/capacity")
        except (TimeoutError, httpx.TimeoutException):
            self._fail("timeout")
        except httpx.HTTPError:
            self._fail("connection_error")

        if response.status_code in (401, 403):
            # Credentials rejected. Counts towards the breaker: hammering a
            # dependency with bad credentials helps nobody.
            self._fail("auth_rejected")
        if response.status_code != 200:
            self._fail("upstream_error")
        try:
            accepting = bool(response.json()["accepting_bookings"])
        except (ValueError, KeyError, TypeError):
            self._fail("bad_response")

        self._breaker.record_success()
        _requests.add(1, {"outcome": "ok"})
        return Capacity(accepting_bookings=accepting)

    def _fail(self, reason: str) -> None:
        self._breaker.record_failure()
        _requests.add(1, {"outcome": reason})
        raise CapacityUnavailable(reason)

    async def aclose(self) -> None:
        await self._client.aclose()

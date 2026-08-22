"""Resilience primitives for agent connectors: retry + circuit breaker.

Track D3 of the connector-hardening effort. These are pure-logic building
blocks: every notion of time (delays between retries, the breaker's
cool-down clock) is injectable, so the offline test suite exercises the
full state machines without ever sleeping.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TypeVar

T = TypeVar("T")

#: Injectable sleep used between retry attempts (defaults to asyncio.sleep).
SleepFn = Callable[[float], Awaitable[None]]
#: Injectable monotonic clock used by the circuit breaker (offline tests).
Clock = Callable[[], float]


async def retry_async(
    async_fn: Callable[[], Awaitable[T]],
    attempts: int = 2,
    base_delay: float = 0.5,
    sleep: SleepFn | None = None,
    retry_on: type[Exception] | tuple[type[Exception], ...] = Exception,
) -> T:
    """Run ``async_fn`` up to ``attempts`` times with exponential backoff.

    ``attempts`` is the *total* number of tries, so ``attempts=2`` means one
    initial call plus one retry. The wait before retry *n* is
    ``base_delay * 2 ** (n - 1)`` (0.5s, 1s, 2s, ...). When every attempt
    fails, the last exception is re-raised.

    Cancellation is never retried: ``asyncio.CancelledError`` propagates
    immediately so task teardown (D4) stays prompt.

    ``sleep`` may be injected for offline tests; ``retry_on`` narrows which
    exceptions count as retryable (default: any ``Exception``).
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    sleep_fn: SleepFn = sleep or asyncio.sleep
    delay = base_delay
    for attempt in range(attempts):
        try:
            return await async_fn()
        except asyncio.CancelledError:
            raise  # cancellation is caller intent, not a transient failure
        except retry_on:
            if attempt == attempts - 1:
                raise  # exhausted: surface the final failure untouched
            await sleep_fn(delay)
            delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover


class CircuitState(str, Enum):
    """Breaker states: CLOSED (normal) -> OPEN (fast-fail) -> HALF_OPEN (probe)."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a call is attempted while the circuit is open."""


class CircuitBreaker:
    """Classic three-state circuit breaker around async calls.

    * ``CLOSED``: calls pass through; each failure bumps
      ``consecutive_failures``; reaching ``failure_threshold`` trips the
      breaker to ``OPEN``.
    * ``OPEN``: calls fail fast with :class:`CircuitOpenError` until
      ``reset_timeout_s`` has elapsed since the breaker opened.
    * ``HALF_OPEN``: a single probe call is allowed. Success closes the
      circuit (failure count reset); failure re-opens it for a full
      cool-down. Concurrent probes fail fast while one is in flight.

    ``asyncio.CancelledError`` is *not* a failure: a cancelled probe frees
    the half-open slot and propagates, so user-initiated aborts (D4) never
    punish the connector.

    Time comes from the injectable ``clock`` (default ``time.monotonic``),
    which keeps the class pure logic and fully offline-testable.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        reset_timeout_s: float = 30.0,
        clock: Clock | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self._clock: Clock = clock or time.monotonic
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    # -- state machine --------------------------------------------------
    def _allow_call(self) -> None:
        """Gate the next call; raise :class:`CircuitOpenError` to fast-fail."""
        if self.state is CircuitState.CLOSED:
            return
        if self.state is CircuitState.OPEN:
            assert self._opened_at is not None
            if self._clock() - self._opened_at >= self.reset_timeout_s:
                # Cool-down elapsed: transition and admit a single probe.
                self.state = CircuitState.HALF_OPEN
                self._probe_in_flight = True
                return
            raise CircuitOpenError(
                f"circuit open (breaker at {self.state.value}, "
                f"{self.consecutive_failures} consecutive failures) - "
                f"retry allowed after {self.reset_timeout_s}s cool-down"
            )
        # HALF_OPEN: exactly one probe at a time.
        if self._probe_in_flight:
            raise CircuitOpenError("circuit half-open: probe already in flight")
        self._probe_in_flight = True

    def _record_success(self) -> None:
        self.consecutive_failures = 0
        self._opened_at = None
        self._probe_in_flight = False
        self.state = CircuitState.CLOSED

    def _record_failure(self) -> None:
        self.consecutive_failures += 1
        self._probe_in_flight = False
        if (
            self.state is CircuitState.HALF_OPEN
            or self.consecutive_failures >= self.failure_threshold
        ):
            self.state = CircuitState.OPEN
            self._opened_at = self._clock()

    def _record_cancel(self) -> None:
        # A cancelled probe never completed: free the half-open slot without
        # counting it as a failure.
        self._probe_in_flight = False

    # -- public API ------------------------------------------------------
    async def call(self, async_fn: Callable[[], Awaitable[T]]) -> T:
        """Invoke ``async_fn`` through the breaker.

        Raises :class:`CircuitOpenError` without calling ``async_fn`` when
        the circuit is open (fast-fail). Exceptions from ``async_fn`` are
        recorded and re-raised unchanged.
        """
        self._allow_call()
        try:
            result = await async_fn()
        except asyncio.CancelledError:
            self._record_cancel()
            raise
        except Exception:
            self._record_failure()
            raise
        self._record_success()
        return result

"""A request budget that several clients, threads and tasks can share."""

from __future__ import annotations

import math
import threading
from asyncio import sleep as asleep
from time import monotonic, sleep

__all__ = ["RateLimiter"]


def _positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number")
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a finite number greater than 0")
    return float(value)


class RateLimiter:
    """A token bucket: ``rate`` tokens per second, holding at most ``burst``.

    Every request takes one token first, so one instance shared by every client
    that talks to a host keeps them all inside one budget, however many threads
    or tasks run them. ``acquire`` blocks the calling thread; ``aacquire`` sleeps
    only the calling task. Both draw from the same bucket.

    ``penalize`` is the brake for a host that has started refusing: it pauses
    every holder, not only the one that saw the refusal, because the host
    throttles the address, and a sibling request is what would extend the block.

    ``initial_penalty`` and ``max_penalty`` shape the pause an ``HttpClient``
    imposes when a request through this limiter is throttled: the server's
    ``Retry-After`` if it sent one, else ``initial_penalty`` doubled for each
    consecutive throttled answer; either capped at ``max_penalty``. They belong
    here rather than on ``RetryConfig`` because a store's block outlasts any
    wait worth spending inside one request.

    The bucket starts full. Waiters are not queued in arrival order: whoever
    re-checks first after a token appears takes it.
    """

    def __init__(
        self,
        rate: float,
        burst: int = 1,
        *,
        initial_penalty: float = 30.0,
        max_penalty: float = 900.0,
    ) -> None:
        self._rate = _positive(rate, "rate")
        if isinstance(burst, bool) or not isinstance(burst, int):
            raise TypeError("burst must be an integer")
        if burst < 1:
            raise ValueError("burst must be at least 1")
        self._initial_penalty = _positive(initial_penalty, "initial_penalty")
        self._max_penalty = _positive(max_penalty, "max_penalty")
        if self._max_penalty < self._initial_penalty:
            raise ValueError("max_penalty must be at least initial_penalty")
        self._burst = burst
        self._tokens = float(burst)
        self._refilled_at = monotonic()
        self._paused_until = 0.0
        self._throttle_streak = 0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Take one token, blocking this thread until one is available."""
        while (wait := self._take()) > 0:
            sleep(wait)

    async def aacquire(self) -> None:
        """Take one token, sleeping this task (not the loop) until one is available."""
        while (wait := self._take()) > 0:
            await asleep(wait)

    def penalize(self, seconds: float) -> None:
        """Hold every token back until ``seconds`` from now.

        Only ever extends: a shorter penalty arriving during a longer one leaves
        the longer one in place, so a burst of throttled answers cannot shorten
        the pause the first of them earned.
        """
        if isinstance(seconds, bool) or not isinstance(seconds, int | float):
            raise TypeError("seconds must be a number")
        if not (math.isfinite(seconds) and seconds >= 0):
            raise ValueError("seconds must be a finite number of at least 0")
        with self._lock:
            self._paused_until = max(self._paused_until, monotonic() + seconds)

    def _throttled(self, retry_after: float | None) -> float:
        """Record one throttled answer, pause every holder, and return the pause.

        The server's ``Retry-After`` wins when it sent one. Otherwise the pause
        starts at ``initial_penalty`` and doubles for each consecutive throttled
        answer this limiter has seen. Either is capped at ``max_penalty``.
        """
        with self._lock:
            streak = self._throttle_streak
            self._throttle_streak += 1
        if retry_after is not None:
            delay = min(retry_after, self._max_penalty)
        else:
            doubled = self._initial_penalty * 2.0 ** min(streak, 64)
            delay = min(doubled, self._max_penalty)
        self.penalize(delay)
        return delay

    def _succeeded(self) -> None:
        """A usable answer ends the throttled streak."""
        with self._lock:
            self._throttle_streak = 0

    def _take(self) -> float:
        """Take a token and return 0, or return how long until one may be taken."""
        with self._lock:
            now = monotonic()
            if now < self._paused_until:
                return self._paused_until - now
            elapsed = max(0.0, now - self._refilled_at)
            self._tokens = min(float(self._burst), self._tokens + elapsed * self._rate)
            self._refilled_at = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            return (1.0 - self._tokens) / self._rate

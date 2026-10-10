"""Limits that keep one worker's memory and connections bounded under load. Each worker process keeps its own
counters (no shared store yet), which is enough to protect that worker; an exact limit across workers would need
something like Redis."""

import time
from collections.abc import AsyncIterator, Callable, Hashable
from contextlib import asynccontextmanager

from fastapi import status

from app.core.exceptions import AppError


class ServiceBusyError(AppError):
    """This worker is at its limit for this kind of request. Clients wait `retryAfter` seconds and try again."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_busy"
    message = "The server is busy right now. Try again in a moment."

    def __init__(self, retry_after: int = 5) -> None:
        super().__init__(details={"retryAfter": retry_after}, headers={"Retry-After": str(retry_after)})


class TooManyRequestsError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "too_many_requests"
    message = "Too many requests. Slow down and try again in a moment."

    def __init__(self, retry_after: int) -> None:
        super().__init__(details={"retryAfter": retry_after}, headers={"Retry-After": str(retry_after)})


class ConcurrencyLimit:
    """At most `limit` requests of a kind in progress at once in this worker; more get a quick 503 instead of
    queueing and holding memory. A plain counter: the event loop runs one coroutine at a time."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.active = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Holds one slot while the block runs.

        Raises:
            ServiceBusyError: Every slot is taken.
        """
        if self.active >= self.limit:
            raise ServiceBusyError()
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1


class PerKeyLimit:
    """Per key (a user): one request in progress at a time, and a token bucket that allows short bursts (`burst`
    requests at once, e.g. the batches of a big save) but no more than one per `every` seconds over time."""

    MAX_KEYS = 100_000

    def __init__(self, *, burst: int, every: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._burst = burst
        self._every = every
        self._clock = clock
        self._buckets: dict[Hashable, tuple[float, float]] = {}  # key -> (tokens, updated at)
        self._busy: set[Hashable] = set()

    @asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        """Takes a token and marks `key` busy while the block runs.

        Raises:
            TooManyRequestsError: `key` has a request in progress, or has used up its tokens.
        """
        if key in self._busy:
            raise TooManyRequestsError(retry_after=1)
        self._take(key)
        self._busy.add(key)
        try:
            yield
        finally:
            self._busy.discard(key)

    def _take(self, key: Hashable) -> None:
        now = self._clock()
        tokens, updated = self._buckets.get(key, (float(self._burst), now))
        tokens = min(float(self._burst), tokens + (now - updated) / self._every)
        if tokens < 1:
            raise TooManyRequestsError(retry_after=max(1, round((1 - tokens) * self._every)))
        if len(self._buckets) >= self.MAX_KEYS:
            # Keys that are full again carry no information: drop them.
            full = now - self._burst * self._every
            self._buckets = {k: v for k, v in self._buckets.items() if v[1] > full}
        self._buckets[key] = (tokens - 1, now)

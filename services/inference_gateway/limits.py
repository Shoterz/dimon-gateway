"""Shared concurrency, queue, timeout, and rate-limit primitives."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .errors import GatewayError


T = TypeVar("T")


class BoundedExecutor:
    def __init__(self, active_limit: int, queue_limit: int, timeout_seconds: float):
        if active_limit <= 0 or queue_limit < 0 or timeout_seconds <= 0:
            raise ValueError("executor limits must be positive and queue_limit non-negative")
        self.active_limit = int(active_limit)
        self.queue_limit = int(queue_limit)
        self.timeout_seconds = float(timeout_seconds)
        self._active = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return self._active

    @property
    def queue_depth(self) -> int:
        return len(self._waiters)

    async def _acquire(self) -> None:
        waiter: asyncio.Future[None] | None = None
        async with self._lock:
            if self._active < self.active_limit and not self._waiters:
                self._active += 1
                return
            if len(self._waiters) >= self.queue_limit:
                raise GatewayError(
                    "CAPACITY_EXCEEDED",
                    "inference capacity is full",
                    429,
                )
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)

        try:
            await asyncio.shield(waiter)
        except BaseException:
            transferred = False
            async with self._lock:
                try:
                    self._waiters.remove(waiter)
                    waiter.cancel()
                except ValueError:
                    transferred = waiter.done() and not waiter.cancelled()
            if transferred:
                await self._release()
            raise

    async def _release(self) -> None:
        async with self._lock:
            while self._waiters:
                waiter = self._waiters.popleft()
                if waiter.cancelled():
                    continue
                waiter.set_result(None)
                return
            self._active -= 1

    async def run(
        self,
        awaitable_factory: Callable[[], Awaitable[T]],
        *,
        on_acquired: Callable[[float], None] | None = None,
    ) -> T:
        acquired = False
        queued_at = time.perf_counter()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self._acquire()
                acquired = True
                if on_acquired is not None:
                    on_acquired((time.perf_counter() - queued_at) * 1000)
                return await awaitable_factory()
        except TimeoutError as exc:
            raise GatewayError(
                "UPSTREAM_TIMEOUT",
                "inference request exceeded its deadline",
                504,
            ) from exc
        finally:
            if acquired:
                await self._release()


class SlidingWindowLimiter:
    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("rate limits must be positive")
        self.limit = int(limit)
        self.window_seconds = float(window_seconds)
        self.clock = clock
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def check(self, identity_key: str) -> float | None:
        now = self.clock()
        attempts = self._attempts[identity_key]
        cutoff = now - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if len(attempts) >= self.limit:
            return max(0.0, attempts[0] + self.window_seconds - now)
        attempts.append(now)
        return None

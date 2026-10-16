"""Async test helpers for the bundled CPython/SQLite runtime."""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import Awaitable, Callable
from unittest.mock import patch


async def _run_in_test_thread(func, /, *args, **kwargs):
    result = queue.SimpleQueue()

    def worker():
        try:
            result.put((True, func(*args, **kwargs)))
        except BaseException as exc:  # pragma: no cover - re-raised below
            result.put((False, exc))

    threading.Thread(target=worker, daemon=True).start()
    while result.empty():
        await asyncio.sleep(0.001)
    succeeded, value = result.get()
    if succeeded:
        return value
    raise value


def run_async_test(factory: Callable[[], Awaitable[object]]):
    """Run an ASGI test coroutine without the SQLite executor shutdown hang."""
    loop = asyncio.new_event_loop()
    try:
        with patch("asyncio.to_thread", _run_in_test_thread):
            return loop.run_until_complete(factory())
    finally:
        loop.close()

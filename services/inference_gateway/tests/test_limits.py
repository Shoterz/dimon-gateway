from __future__ import annotations

import asyncio
import unittest

from services.inference_gateway.errors import GatewayError
from services.inference_gateway.limits import BoundedExecutor, SlidingWindowLimiter


class BoundedExecutorTests(unittest.TestCase):
    def test_active_queue_and_excess_capacity_are_bounded(self) -> None:
        async def scenario() -> None:
            executor = BoundedExecutor(active_limit=1, queue_limit=1, timeout_seconds=2)
            release = asyncio.Event()

            async def blocked(name: str) -> str:
                await release.wait()
                return name

            first = asyncio.create_task(executor.run(lambda: blocked("first")))
            await asyncio.sleep(0)
            second = asyncio.create_task(executor.run(lambda: blocked("second")))
            await asyncio.sleep(0)
            self.assertEqual(executor.active_count, 1)
            self.assertEqual(executor.queue_depth, 1)

            with self.assertRaises(GatewayError) as raised:
                await executor.run(lambda: blocked("third"))
            self.assertEqual(raised.exception.code, "CAPACITY_EXCEEDED")
            self.assertEqual(raised.exception.status_code, 429)

            release.set()
            self.assertEqual(await asyncio.gather(first, second), ["first", "second"])
            self.assertEqual(executor.active_count, 0)
            self.assertEqual(executor.queue_depth, 0)

        asyncio.run(scenario())

    def test_queue_admission_is_fifo(self) -> None:
        async def scenario() -> None:
            executor = BoundedExecutor(active_limit=1, queue_limit=2, timeout_seconds=2)
            gates = [asyncio.Event() for _ in range(3)]
            admitted = [asyncio.Event() for _ in range(3)]
            started: list[int] = []

            async def work(index: int) -> int:
                started.append(index)
                admitted[index].set()
                await gates[index].wait()
                return index

            tasks = []
            for index in range(3):
                tasks.append(asyncio.create_task(executor.run(lambda index=index: work(index))))
                await asyncio.sleep(0)
            self.assertEqual(started, [0])
            gates[0].set()
            await asyncio.wait_for(admitted[1].wait(), 1)
            self.assertEqual(started, [0, 1])
            gates[1].set()
            await asyncio.wait_for(admitted[2].wait(), 1)
            self.assertEqual(started, [0, 1, 2])
            gates[2].set()
            self.assertEqual(await asyncio.gather(*tasks), [0, 1, 2])

        asyncio.run(scenario())

    def test_cancelling_active_and_queued_work_releases_every_permit(self) -> None:
        async def scenario() -> None:
            executor = BoundedExecutor(active_limit=1, queue_limit=1, timeout_seconds=2)
            never = asyncio.Event()

            async def blocked() -> None:
                await never.wait()

            active = asyncio.create_task(executor.run(blocked))
            await asyncio.sleep(0)
            queued = asyncio.create_task(executor.run(blocked))
            await asyncio.sleep(0)
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            self.assertEqual(executor.queue_depth, 0)
            active.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await active
            self.assertEqual(executor.active_count, 0)

            self.assertEqual(await executor.run(lambda: asyncio.sleep(0, result="ok")), "ok")

        asyncio.run(scenario())

    def test_timeout_cancels_work_and_releases_capacity(self) -> None:
        async def scenario() -> None:
            executor = BoundedExecutor(active_limit=1, queue_limit=0, timeout_seconds=0.01)
            cancelled = asyncio.Event()

            async def slow() -> None:
                try:
                    await asyncio.sleep(60)
                finally:
                    cancelled.set()

            with self.assertRaises(GatewayError) as raised:
                await executor.run(slow)
            self.assertEqual(raised.exception.code, "UPSTREAM_TIMEOUT")
            self.assertEqual(raised.exception.status_code, 504)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(executor.active_count, 0)

        asyncio.run(scenario())


class SlidingWindowLimiterTests(unittest.TestCase):
    def test_rate_windows_are_per_identity_and_expire(self) -> None:
        now = [100.0]
        limiter = SlidingWindowLimiter(2, 10, clock=lambda: now[0])

        self.assertIsNone(limiter.check("alice"))
        self.assertIsNone(limiter.check("alice"))
        self.assertEqual(limiter.check("alice"), 10.0)
        self.assertIsNone(limiter.check("bob"))

        now[0] = 110.0
        self.assertIsNone(limiter.check("alice"))

    def test_invalid_limits_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SlidingWindowLimiter(0, 10)
        with self.assertRaises(ValueError):
            SlidingWindowLimiter(1, 0)


if __name__ == "__main__":
    unittest.main()

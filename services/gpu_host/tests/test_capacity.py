from __future__ import annotations

import threading
import time
import unittest

from services.gpu_host.capacity import (
    CapacityProfile,
    GpuCapacityBroker,
    InsufficientCapacity,
)


class FakeTelemetry:
    def __init__(self, free_mb: int):
        self.free_mb = free_mb

    def free_vram_mb(self) -> int:
        return self.free_mb


class CapacityTests(unittest.TestCase):
    def test_compatible_reservations_and_release(self) -> None:
        broker = GpuCapacityBroker(FakeTelemetry(12_000), safety_reserve_mb=512)
        qwen = CapacityProfile("qwen", 7168, 30, 300)
        stt = CapacityProfile("nemotron", 1184, 30, 75)
        one = broker.acquire("qwen", qwen, timeout=0)
        two = broker.acquire("nemotron", stt, timeout=0)
        self.assertEqual(broker.reserved_mb, 8352)
        broker.release(one)
        broker.release(two)
        self.assertEqual(broker.reserved_mb, 0)

    def test_insufficient_memory_is_bounded(self) -> None:
        broker = GpuCapacityBroker(FakeTelemetry(2_000), safety_reserve_mb=512)
        with self.assertRaises(InsufficientCapacity):
            broker.acquire("qwen", CapacityProfile("qwen", 7168, 30, 300), timeout=0)

    def test_waiters_are_fifo_and_timed_out_waiter_is_cancelled(self) -> None:
        telemetry = FakeTelemetry(9_000)
        broker = GpuCapacityBroker(telemetry, safety_reserve_mb=512)
        profile = CapacityProfile("large", 7000, 30, 300)
        held = broker.acquire("held", profile, timeout=0)
        order: list[str] = []

        def wait(name: str, timeout: float) -> None:
            try:
                lease = broker.acquire(name, profile, timeout=timeout)
            except InsufficientCapacity:
                order.append(f"{name}:timeout")
                return
            order.append(name)
            broker.release(lease)

        first = threading.Thread(target=wait, args=("first", 1.0))
        expired = threading.Thread(target=wait, args=("expired", 0.03))
        second = threading.Thread(target=wait, args=("second", 1.0))
        first.start()
        time.sleep(0.01)
        expired.start()
        time.sleep(0.01)
        second.start()
        expired.join()
        broker.release(held)
        first.join()
        second.join()
        self.assertEqual(order, ["expired:timeout", "first", "second"])


if __name__ == "__main__":
    unittest.main()

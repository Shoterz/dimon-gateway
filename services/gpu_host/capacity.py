"""Conservative FIFO GPU capacity reservations."""

from __future__ import annotations

import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Protocol


class InsufficientCapacity(RuntimeError):
    pass


class GpuTelemetry(Protocol):
    def free_vram_mb(self) -> int: ...


@dataclass(frozen=True)
class CapacityProfile:
    name: str
    reserved_vram_mb: int
    startup_timeout_seconds: float
    idle_timeout_seconds: float

    def __post_init__(self) -> None:
        if not self.name or self.reserved_vram_mb <= 0:
            raise ValueError("capacity profile must have a name and positive reservation")
        if self.startup_timeout_seconds < 0 or self.idle_timeout_seconds < 0:
            raise ValueError("capacity profile timeouts cannot be negative")


@dataclass(frozen=True)
class Lease:
    lease_id: str
    service: str
    reserved_vram_mb: int


class NvidiaSmiTelemetry:
    def free_vram_mb(self) -> int:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            text=True,
            capture_output=True,
            timeout=3,
        )
        values = [int(line.strip()) for line in result.stdout.splitlines() if line.strip()]
        if not values:
            raise RuntimeError("GPU_TELEMETRY_UNAVAILABLE")
        return min(values)


class GpuCapacityBroker:
    def __init__(self, telemetry: GpuTelemetry, *, safety_reserve_mb: int = 512):
        if safety_reserve_mb < 0:
            raise ValueError("safety reserve cannot be negative")
        self.telemetry = telemetry
        self.safety_reserve_mb = safety_reserve_mb
        self._leases: dict[str, Lease] = {}
        self._waiters: deque[str] = deque()
        self._condition = threading.Condition()

    @property
    def reserved_mb(self) -> int:
        with self._condition:
            return sum(item.reserved_vram_mb for item in self._leases.values())

    def _fits(self, requested_mb: int) -> bool:
        return (
            self.reserved_mb + requested_mb + self.safety_reserve_mb
            <= self.telemetry.free_vram_mb()
        )

    def acquire(
        self,
        service: str,
        profile: CapacityProfile,
        timeout: float,
    ) -> Lease:
        if timeout < 0:
            raise ValueError("timeout cannot be negative")
        waiter = uuid.uuid4().hex
        deadline = time.monotonic() + timeout
        with self._condition:
            self._waiters.append(waiter)
            while True:
                first = self._waiters and self._waiters[0] == waiter
                if first and self._fits(profile.reserved_vram_mb):
                    self._waiters.popleft()
                    lease = Lease(uuid.uuid4().hex, service, profile.reserved_vram_mb)
                    self._leases[lease.lease_id] = lease
                    self._condition.notify_all()
                    return lease
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    try:
                        self._waiters.remove(waiter)
                    except ValueError:
                        pass
                    self._condition.notify_all()
                    raise InsufficientCapacity("GPU_CAPACITY_UNAVAILABLE")
                self._condition.wait(remaining)

    def release(self, lease: Lease) -> None:
        with self._condition:
            self._leases.pop(lease.lease_id, None)
            self._condition.notify_all()

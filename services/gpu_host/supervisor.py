"""Capability-oriented GPU host supervision."""

from __future__ import annotations

from dataclasses import dataclass

from .capacity import CapacityProfile, GpuCapacityBroker, InsufficientCapacity, Lease
from .processes import ManagedProcess, ProcessStateError


@dataclass(frozen=True)
class ReadinessResult:
    capability: str
    ready: bool
    reason_code: str


class HostSupervisor:
    def __init__(self, broker: GpuCapacityBroker, capabilities):
        self.broker = broker
        self.capabilities: dict[str, tuple[ManagedProcess, CapacityProfile]] = dict(capabilities)
        self._leases: dict[str, Lease] = {}

    def ensure(self, capability: str) -> ReadinessResult:
        configured = self.capabilities.get(capability)
        if configured is None:
            return ReadinessResult(capability, False, "CAPABILITY_NOT_CONFIGURED")
        process, profile = configured
        if process.ready():
            return ReadinessResult(capability, True, "READY")
        try:
            lease = self.broker.acquire(capability, profile, profile.startup_timeout_seconds)
        except (InsufficientCapacity, RuntimeError):
            return ReadinessResult(capability, False, "GPU_CAPACITY_UNAVAILABLE")
        try:
            process.start()
        except ProcessStateError as error:
            self.broker.release(lease)
            return ReadinessResult(capability, False, str(error))
        self._leases[capability] = lease
        return ReadinessResult(capability, True, "READY")

    def stop_all(self) -> None:
        for capability, (process, _) in reversed(tuple(self.capabilities.items())):
            process.stop()
            lease = self._leases.pop(capability, None)
            if lease is not None:
                self.broker.release(lease)

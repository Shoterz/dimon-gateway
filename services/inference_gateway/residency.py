"""Fail-closed evidence that Dimon's four model workers are resident."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from apps.dimon_live.gpu_overlord_status import (
    GpuServiceStatus,
    GpuStatusError,
    GpuStatusSnapshot,
)


EXPECTED_WORKERS = {
    "qwen": ("llama", "qwen35-9b-abliterated-q5-4k"),
    "nemotron": ("nemotron", "nemotron-en"),
    "kokoro": ("kokoro", "kokoro-82m"),
    "faster_whisper": ("faster_whisper", "faster-whisper-medium-cuda"),
}


@dataclass(frozen=True)
class ProbeEvidence:
    name: str
    worker_pid: int
    ok: bool
    reason_code: str
    model: str | None = None
    device: str | None = None
    compute_type: str | None = None
    context_tokens: int | None = None


@dataclass(frozen=True)
class ResidentWorkerEvidence:
    name: str
    service: str
    state: str
    worker_pid: int
    profile: str
    active_jobs: int
    heartbeat_age_seconds: float
    budget_mb: int
    observed_vram_mb: int


@dataclass(frozen=True)
class ResidencySnapshot:
    ready: bool
    reason_code: str
    total_mb: int
    free_mb: int
    safety_reserve_mb: int
    workers: tuple[ResidentWorkerEvidence, ...]


class FourWorkerResidencyGate:
    def __init__(self, status_client, *, max_heartbeat_age_seconds: float = 5.0):
        if max_heartbeat_age_seconds <= 0:
            raise ValueError("max_heartbeat_age_seconds must be positive")
        self.status_client = status_client
        self.max_heartbeat_age_seconds = float(max_heartbeat_age_seconds)

    async def check(
        self, recent_probes: Mapping[str, ProbeEvidence]
    ) -> ResidencySnapshot:
        try:
            status = await self.status_client.read()
        except GpuStatusError:
            return ResidencySnapshot(
                False, "GPU_STATUS_UNAVAILABLE", 0, 0, 0, ()
            )
        workers: list[ResidentWorkerEvidence] = []
        if status.free_mb < max(512, status.safety_reserve_mb):
            return self._result(status, workers, "GPU_RESERVE_LOW")

        for name, (service_name, expected_profile) in EXPECTED_WORKERS.items():
            service = status.service(service_name)
            if service is None or service.worker_pid is None or service.worker_pid <= 0:
                return self._result(status, workers, "WORKER_MISSING")
            if service.profile != expected_profile:
                return self._result(status, workers, "PROFILE_MISMATCH")
            if service.state not in {"ready", "busy"}:
                return self._result(status, workers, "WORKER_STATE_INVALID")
            if service.heartbeat_age_seconds > self.max_heartbeat_age_seconds:
                return self._result(status, workers, "WORKER_STALE")
            if service.budget_mb <= 0 or service.observed_vram_mb > service.budget_mb:
                return self._result(status, workers, "WORKER_OVER_BUDGET")
            if name == "nemotron" and service.active_jobs > 1:
                return self._result(status, workers, "NEMOTRON_OVER_CAPACITY")
            probe = recent_probes.get(name)
            if not isinstance(probe, ProbeEvidence) or not probe.ok:
                return self._result(status, workers, "PROBE_FAILED")
            if probe.name != name or probe.worker_pid != service.worker_pid:
                return self._result(status, workers, "WORKER_PID_CHANGED")
            workers.append(self._worker(name, service))

        return self._result(status, workers, "READY")

    @staticmethod
    def _worker(name: str, service: GpuServiceStatus) -> ResidentWorkerEvidence:
        assert service.worker_pid is not None
        assert service.profile is not None
        return ResidentWorkerEvidence(
            name=name,
            service=service.service,
            state=service.state,
            worker_pid=service.worker_pid,
            profile=service.profile,
            active_jobs=service.active_jobs,
            heartbeat_age_seconds=service.heartbeat_age_seconds,
            budget_mb=service.budget_mb,
            observed_vram_mb=service.observed_vram_mb,
        )

    @staticmethod
    def _result(
        status: GpuStatusSnapshot,
        workers: list[ResidentWorkerEvidence],
        reason_code: str,
    ) -> ResidencySnapshot:
        return ResidencySnapshot(
            ready=reason_code == "READY",
            reason_code=reason_code,
            total_mb=status.total_mb,
            free_mb=status.free_mb,
            safety_reserve_mb=status.safety_reserve_mb,
            workers=tuple(workers),
        )

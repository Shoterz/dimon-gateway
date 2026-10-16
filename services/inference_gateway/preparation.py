"""Truthful, idempotent warm-up for Dimon's four resident model workers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from apps.dimon_live.gpu_overlord_status import GpuServiceStatus, GpuStatusError

from .residency import ProbeEvidence, ResidencySnapshot
from .gpu_reset import GpuStackResetError


_STAGE_NAMES = ("capacity", "nemotron", "qwen", "kokoro", "faster_whisper")
_TRANSIENT_STARTUP_RESIDENCY_REASONS = frozenset(
    {"WORKER_MISSING", "WORKER_STALE"}
)


class GatewayPreparation:
    """Warm real workers and keep their residency proof fresh for a short window."""

    def __init__(
        self,
        *,
        recognizer,
        intent_upstream,
        speech_synthesizer,
        residency_gate,
        qwen_probe: Callable[[], Awaitable[ProbeEvidence]],
        faster_whisper_probe: Callable[[], Awaitable[ProbeEvidence]],
        transcription_lease,
        ready_seconds: int = 300,
        health_interval_seconds: float = 45.0,
        utc_now: Callable[[], datetime] | None = None,
        worker_resetter: Callable[[], Awaitable[frozenset[int]]] | None = None,
        startup_retry_seconds: float = 0.5,
        startup_timeout_seconds: float = 15.0,
    ) -> None:
        if ready_seconds <= 0:
            raise ValueError("ready_seconds must be positive")
        if not 0 < health_interval_seconds <= 45:
            raise ValueError("health_interval_seconds must be between 0 and 45")
        if startup_retry_seconds <= 0:
            raise ValueError("startup_retry_seconds must be positive")
        if startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")
        self.recognizer = recognizer
        self.intent_upstream = intent_upstream
        self.speech_synthesizer = speech_synthesizer
        self.residency_gate = residency_gate
        self.qwen_probe = qwen_probe
        self.faster_whisper_probe = faster_whisper_probe
        self.transcription_lease = transcription_lease
        self.ready_seconds = int(ready_seconds)
        self.health_interval_seconds = float(health_interval_seconds)
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self.worker_resetter = worker_resetter
        self.startup_retry_seconds = float(startup_retry_seconds)
        self.startup_timeout_seconds = float(startup_timeout_seconds)

        self.task: asyncio.Task[None] | None = None
        self.monitor_task: asyncio.Task[None] | None = None
        self.state = "IDLE"
        self.reason_code = "IDLE"
        self.started_at: datetime | None = None
        self.completed_at: datetime | None = None
        self.ready_until: datetime | None = None
        self.stages = self._new_stages("IDLE")
        self.workers: list[dict[str, object]] = []
        self.gpu: dict[str, int] | None = None
        self._lock = asyncio.Lock()
        self._cycle_lock = asyncio.Lock()

    @staticmethod
    def _new_stages(state: str) -> dict[str, dict[str, object]]:
        return {
            name: {"state": state, "latency_ms": None, "reason_code": state}
            for name in _STAGE_NAMES
        }

    async def start(self) -> dict[str, object]:
        previous_monitor: asyncio.Task[None] | None = None
        async with self._lock:
            if self.task is None or self.task.done():
                if self.monitor_task is not None and not self.monitor_task.done():
                    previous_monitor = self.monitor_task
                    previous_monitor.cancel()
                self.monitor_task = None
                self.state = "PREPARING"
                self.reason_code = "PREPARING"
                self.started_at = self._now()
                self.completed_at = None
                self.ready_until = None
                self.workers = []
                self.gpu = None
                self.stages = self._new_stages("PENDING")
                self.task = asyncio.create_task(self._run())
        if previous_monitor is not None:
            await asyncio.gather(previous_monitor, return_exceptions=True)
        return self.snapshot()

    async def status(self) -> dict[str, object]:
        if self.ready_until is not None and self._now() >= self.ready_until:
            self._expire()
        return self.snapshot()

    async def stop(self) -> None:
        current = asyncio.current_task()
        pending = tuple(
            task
            for task in (self.task, self.monitor_task)
            if task is not None and task is not current and not task.done()
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _run(self) -> None:
        try:
            await self._run_cycle(reset_workers=True)
        except asyncio.CancelledError:
            self.state = "DEGRADED"
            self.reason_code = "PREPARATION_CANCELLED"
            raise
        finally:
            self.completed_at = self._now()
        self.ready_until = self._now() + timedelta(seconds=self.ready_seconds)
        if self.monitor_task is None or self.monitor_task.done():
            self.monitor_task = asyncio.create_task(self._monitor())

    async def _run_cycle(self, *, reset_workers: bool = False) -> None:
        async with self._cycle_lock:
            capacity_started = time.monotonic()
            old_worker_pids: frozenset[int] = frozenset()
            if reset_workers and self.worker_resetter is not None:
                self._set_stage("capacity", "RUNNING", None, "RESETTING_GPU_WORKERS")
                try:
                    old_worker_pids = await self.worker_resetter()
                except GpuStackResetError as exc:
                    latency = self._elapsed_ms(capacity_started)
                    self._set_stage("capacity", "FAILED", latency, exc.reason_code)
                    self.state = "DEGRADED"
                    self.reason_code = exc.reason_code
                    self.workers = []
                    self.gpu = None
                    return
            wait_for_startup = reset_workers and self.worker_resetter is not None
            self._set_stage("capacity", "RUNNING", None, "CHECKING")
            operations = (
                ("qwen", self._probe_qwen),
                ("kokoro", self._probe_kokoro),
                ("nemotron", self._probe_nemotron),
                ("faster_whisper", self.faster_whisper_probe),
            )
            if wait_for_startup:
                results = []
                for name, operation in operations:
                    results.append(
                        await self._run_probe_stage(
                            name,
                            operation,
                            retry_until=(
                                time.monotonic() + self.startup_timeout_seconds
                            ),
                        )
                    )
            else:
                results = await asyncio.gather(
                    *(
                        self._run_probe_stage(name, operation)
                        for name, operation in operations
                    )
                )
            evidence = {item.name: item for item in results}
            residency = await self.residency_gate.check(evidence)
            if wait_for_startup:
                residency_deadline = time.monotonic() + self.startup_timeout_seconds
                while (
                    not residency.ready
                    and residency.reason_code
                    in _TRANSIENT_STARTUP_RESIDENCY_REASONS
                    and time.monotonic() < residency_deadline
                ):
                    await asyncio.sleep(
                        min(
                            self.startup_retry_seconds,
                            max(0.0, residency_deadline - time.monotonic()),
                        )
                    )
                    residency = await self.residency_gate.check(evidence)
            capacity_latency = self._elapsed_ms(capacity_started)
            new_worker_pids = {worker.worker_pid for worker in residency.workers}
            stale_after_reset = bool(old_worker_pids.intersection(new_worker_pids))
            if residency.ready and not stale_after_reset:
                self._set_stage("capacity", "READY", capacity_latency, "READY")
                self.state = "READY"
                self.reason_code = "READY"
            else:
                reason_code = (
                    "GPU_RESET_STALE_WORKER" if stale_after_reset else residency.reason_code
                )
                self._set_stage(
                    "capacity", "FAILED", capacity_latency, reason_code
                )
                self.state = "DEGRADED"
                self.reason_code = reason_code
            self._capture_residency(residency, evidence)

    async def _run_probe_stage(
        self,
        name: str,
        operation: Callable[[], Awaitable[ProbeEvidence]],
        *,
        retry_until: float | None = None,
    ) -> ProbeEvidence:
        self._set_stage(name, "RUNNING", None, "PROBING")
        started = time.monotonic()
        while True:
            try:
                evidence = await operation()
                if not isinstance(evidence, ProbeEvidence) or evidence.name != name:
                    evidence = ProbeEvidence(
                        name, 0, False, f"{name.upper()}_INVALID_PROBE"
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # fail closed without exposing upstream details
                evidence = ProbeEvidence(
                    name, 0, False, f"{name.upper()}_UNAVAILABLE"
                )
            if evidence.ok or retry_until is None or time.monotonic() >= retry_until:
                break
            await asyncio.sleep(
                min(
                    self.startup_retry_seconds,
                    max(0.0, retry_until - time.monotonic()),
                )
            )
        self._set_stage(
            name,
            "READY" if evidence.ok else "FAILED",
            self._elapsed_ms(started),
            evidence.reason_code,
        )
        return evidence

    async def _probe_nemotron(self) -> ProbeEvidence:
        worker = await self._service("nemotron")
        if (
            worker is not None
            and worker.state == "busy"
            and worker.active_jobs == 1
            and worker.worker_pid is not None
        ):
            return ProbeEvidence("nemotron", worker.worker_pid, True, "READY")

        target = getattr(self.recognizer, "captions", self.recognizer)

        async def ignore(_text: str) -> None:
            return None

        stream = None
        try:
            try:
                stream = await target.open_stream(ignore)
            except Exception:
                worker = await self._service("nemotron")
                if (
                    worker is not None
                    and worker.state == "busy"
                    and worker.active_jobs == 1
                    and worker.worker_pid is not None
                ):
                    return ProbeEvidence(
                        "nemotron", worker.worker_pid, True, "READY"
                    )
                raise
        finally:
            if stream is not None:
                await stream.close()

        worker = await self._service("nemotron")
        if worker is None or worker.worker_pid is None:
            return ProbeEvidence("nemotron", 0, False, "NEMOTRON_UNAVAILABLE")
        return ProbeEvidence("nemotron", worker.worker_pid, True, "READY")

    async def _probe_qwen(self) -> ProbeEvidence:
        worker = await self._service("llama")
        if (
            worker is not None
            and worker.state == "busy"
            and worker.active_jobs > 0
            and worker.worker_pid is not None
        ):
            return await self.qwen_probe()
        await self.intent_upstream.extract("Send John Smith S$5 from savings")
        return await self.qwen_probe()

    async def _probe_kokoro(self) -> ProbeEvidence:
        worker = await self._service("kokoro")
        if (
            worker is not None
            and worker.state == "busy"
            and worker.active_jobs > 0
            and worker.worker_pid is not None
        ):
            return ProbeEvidence("kokoro", worker.worker_pid, True, "READY")
        result = await self.speech_synthesizer("Dimon is ready.")
        wav = getattr(result, "data", result)
        if (
            not isinstance(wav, bytes)
            or len(wav) < 12
            or wav[:4] != b"RIFF"
            or wav[8:12] != b"WAVE"
        ):
            return ProbeEvidence("kokoro", 0, False, "KOKORO_INVALID_AUDIO")
        worker = await self._service("kokoro")
        if worker is None or worker.worker_pid is None:
            return ProbeEvidence("kokoro", 0, False, "KOKORO_UNAVAILABLE")
        return ProbeEvidence("kokoro", worker.worker_pid, True, "READY")

    async def _service(self, name: str) -> GpuServiceStatus | None:
        try:
            status = await self.residency_gate.status_client.read()
        except GpuStatusError:
            return None
        return status.service(name)

    async def _monitor(self) -> None:
        try:
            while self.ready_until is not None and self._now() < self.ready_until:
                await asyncio.sleep(self.health_interval_seconds)
                if self.ready_until is None:
                    return
                if self._now() >= self.ready_until:
                    self._expire()
                    return
                await self._run_cycle()
        except asyncio.CancelledError:
            raise

    def _capture_residency(
        self,
        residency: ResidencySnapshot,
        probes: dict[str, ProbeEvidence],
    ) -> None:
        self.workers = []
        for worker in residency.workers:
            evidence = probes.get(worker.name)
            public_worker = {
                "name": worker.name,
                "service": worker.service,
                "state": worker.state,
                "worker_pid": worker.worker_pid,
                "profile": worker.profile,
                "active_jobs": worker.active_jobs,
                "heartbeat_age_seconds": worker.heartbeat_age_seconds,
                "budget_mb": worker.budget_mb,
                "observed_vram_mb": worker.observed_vram_mb,
            }
            if evidence is not None:
                public_worker.update(
                    {
                        "model": evidence.model,
                        "device": evidence.device,
                        "compute_type": evidence.compute_type,
                        "context_tokens": evidence.context_tokens,
                    }
                )
            self.workers.append(public_worker)
        self.gpu = {
            "total_mb": residency.total_mb,
            "free_mb": residency.free_mb,
            "safety_reserve_mb": residency.safety_reserve_mb,
        }

    def _expire(self) -> None:
        self.state = "IDLE"
        self.reason_code = "READY_WINDOW_EXPIRED"
        self.stages = self._new_stages("IDLE")
        self.workers = []
        self.gpu = None
        self.ready_until = None

    def _set_stage(
        self,
        name: str,
        state: str,
        latency_ms: float | None,
        reason_code: str,
    ) -> None:
        self.stages[name] = {
            "state": state,
            "latency_ms": latency_ms,
            "reason_code": reason_code,
        }

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round(max(0.0, (time.monotonic() - started) * 1000), 3)

    def _now(self) -> datetime:
        value = self._utc_now()
        if value.tzinfo is None:
            raise ValueError("utc_now must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def snapshot(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reason_code": self.reason_code,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "ready_until": self.ready_until.isoformat() if self.ready_until else None,
            "stages": [
                {"name": name, **dict(self.stages[name])} for name in _STAGE_NAMES
            ],
            "workers": [dict(worker) for worker in self.workers],
            "gpu": dict(self.gpu) if self.gpu is not None else None,
        }

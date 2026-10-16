"""Process-local, best-effort warm-up coordination for Dimon demos."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from .gpu_overlord_status import GpuStatusError, GpuStatusSnapshot
from .model_adapter import ExtractedIntent


StageName = Literal["capacity", "nemotron", "qwen", "kokoro", "faster_whisper"]
StageState = Literal["PENDING", "RUNNING", "READY", "FAILED"]
PreparationState = Literal["IDLE", "PREPARING", "READY", "DEGRADED", "FAILED"]

_STAGE_NAMES: tuple[StageName, ...] = (
    "capacity", "nemotron", "qwen", "kokoro", "faster_whisper"
)
_WARNING = "Readiness is rechecked while the demo-ready window is active."
_QWEN_PROMPT = "Send John Smith S$5 from savings"
_KOKORO_PHRASE = "Dimon is ready."
_REQUIRED_WORKERS = (
    ("qwen", "llama", "qwen35-9b-abliterated-q5-4k", "QWEN_UNAVAILABLE"),
    ("nemotron", "nemotron", "nemotron-en", "NEMOTRON_UNAVAILABLE"),
    ("kokoro", "kokoro", "kokoro-82m", "KOKORO_UNAVAILABLE"),
    (
        "faster_whisper",
        "faster_whisper",
        "faster-whisper-medium-cuda",
        "FASTER_WHISPER_UNAVAILABLE",
    ),
)


@dataclass
class _Stage:
    name: StageName
    state: StageState = "PENDING"
    latency_ms: int = 0
    reason_code: str | None = None


class _StageFailure(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class DemoPreparationCoordinator:
    def __init__(
        self,
        *,
        status_client,
        extractor,
        recognizer,
        lifecycle,
        synthesizer,
        faster_whisper_recognizer,
        capacity_timeout_seconds: float = 2.0,
        nemotron_timeout_seconds: float = 120.0,
        qwen_timeout_seconds: float = 120.0,
        kokoro_timeout_seconds: float = 30.0,
        faster_whisper_timeout_seconds: float = 120.0,
        ready_window_seconds: float = 60.0,
        max_wav_bytes: int = 8 * 1_048_576,
        utc_now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        timeouts = (
            capacity_timeout_seconds,
            nemotron_timeout_seconds,
            qwen_timeout_seconds,
            kokoro_timeout_seconds,
            faster_whisper_timeout_seconds,
            ready_window_seconds,
        )
        if any(value <= 0 for value in timeouts):
            raise ValueError("preparation timeouts must be positive")
        if max_wav_bytes < 12:
            raise ValueError("max_wav_bytes must be at least 12")

        self._status_client = status_client
        self._extractor = extractor
        self._recognizer = recognizer
        self._lifecycle = lifecycle
        self._synthesizer = synthesizer
        self._faster_whisper_recognizer = faster_whisper_recognizer
        self._timeouts = {
            "capacity": capacity_timeout_seconds,
            "nemotron": nemotron_timeout_seconds,
            "qwen": qwen_timeout_seconds,
            "kokoro": kokoro_timeout_seconds,
            "faster_whisper": faster_whisper_timeout_seconds,
        }
        self._ready_window_seconds = ready_window_seconds
        self._max_wav_bytes = max_wav_bytes
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._state: PreparationState = "IDLE"
        self._started_at: datetime | None = None
        self._completed_at: datetime | None = None
        self._ready_until: datetime | None = None
        self._stages = [_Stage(name) for name in _STAGE_NAMES]
        self._workers: list[dict[str, object]] = []
        self._gpu: dict[str, int] | None = None
        self._reason_code = "IDLE"

    async def start(self) -> dict[str, object]:
        async with self._lock:
            if self._task is not None and not self._task.done():
                return self._snapshot()
            self._state = "PREPARING"
            self._started_at = self._normalized_now()
            self._completed_at = None
            self._ready_until = None
            self._stages = [_Stage(name) for name in _STAGE_NAMES]
            self._workers = []
            self._gpu = None
            self._reason_code = "PREPARING"
            self._task = asyncio.create_task(self._run())
            return self._snapshot()

    async def status(self) -> dict[str, object]:
        if self._state == "READY":
            await self._refresh_ready_state()
        return self._snapshot()

    async def wait(self) -> dict[str, object]:
        task = self._task
        if task is not None:
            await asyncio.shield(task)
        return self._snapshot()

    async def stop(self) -> None:
        """Cancel an in-progress warm-up when the owning service shuts down."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        try:
            await self._run_stage(
                "capacity", self._capacity_preflight, "GPU_STATUS_UNAVAILABLE"
            )
            await self._run_stage(
                "nemotron", self._warm_nemotron, "NEMOTRON_UNAVAILABLE"
            )
            parallel_results = await asyncio.gather(
                self._run_stage("qwen", self._warm_qwen, "QWEN_UNAVAILABLE"),
                self._run_stage(
                    "kokoro", self._warm_kokoro, "KOKORO_UNAVAILABLE"
                ),
                self._run_stage(
                    "faster_whisper",
                    self._warm_faster_whisper,
                    "FASTER_WHISPER_UNAVAILABLE",
                ),
                return_exceptions=True,
            )
            for result in parallel_results:
                if isinstance(result, BaseException):
                    raise result
            await self._verify_resident_workers()
        except _StageFailure as exc:
            self._state = "FAILED"
            self._reason_code = exc.reason_code
            self._completed_at = self._normalized_now()
            return
        except asyncio.CancelledError:
            for stage in self._stages:
                if stage.state == "RUNNING":
                    stage.state = "FAILED"
                    stage.reason_code = "PREPARATION_CANCELLED"
            self._state = "FAILED"
            self._reason_code = "PREPARATION_CANCELLED"
            self._completed_at = self._normalized_now()
            raise

        self._completed_at = self._normalized_now()
        self._ready_until = self._completed_at + timedelta(
            seconds=self._ready_window_seconds
        )
        self._state = "READY"
        self._reason_code = "READY"

    async def _run_stage(
        self,
        name: StageName,
        operation: Callable[[], Awaitable[None]],
        unavailable_code: str,
    ) -> None:
        stage = self._stage(name)
        stage.state = "RUNNING"
        started = self._monotonic()
        try:
            await asyncio.wait_for(operation(), timeout=self._timeouts[name])
        except asyncio.TimeoutError:
            stage.state = "FAILED"
            stage.reason_code = "PREPARATION_TIMEOUT"
            raise _StageFailure(stage.reason_code) from None
        except _StageFailure as exc:
            stage.state = "FAILED"
            stage.reason_code = exc.reason_code
            raise
        except Exception:
            stage.state = "FAILED"
            stage.reason_code = unavailable_code
            raise _StageFailure(unavailable_code) from None
        finally:
            elapsed = self._monotonic() - started
            stage.latency_ms = max(0, round(elapsed * 1_000))
        stage.state = "READY"

    async def _verify_resident_workers(self) -> None:
        try:
            snapshot = await asyncio.wait_for(
                self._status_client.read(), timeout=self._timeouts["capacity"]
            )
        except Exception:
            stage = self._stage("capacity")
            stage.state = "FAILED"
            stage.reason_code = "GPU_STATUS_UNAVAILABLE"
            raise _StageFailure(stage.reason_code) from None

        self._capture_residency(snapshot)

    async def _capacity_preflight(self) -> None:
        try:
            snapshot = await self._status_client.read()
        except GpuStatusError:
            raise _StageFailure("GPU_STATUS_UNAVAILABLE") from None
        whisper = snapshot.service("whisper")
        if whisper is not None and (
            whisper.active_jobs > 0 or whisper.state in {"starting", "busy", "draining"}
        ):
            raise _StageFailure("WHISPER_BUSY")

    async def _warm_nemotron(self) -> None:
        if self._lifecycle is None:
            raise _StageFailure("NEMOTRON_UNAVAILABLE")
        lifecycle_lease = None
        stream = None

        async def ignore_partial(_text: str) -> None:
            return None

        try:
            lifecycle_lease = await self._lifecycle.acquire()
            stream = await self._recognizer.open_stream(ignore_partial)
            if stream is None or not callable(getattr(stream, "close", None)):
                raise _StageFailure("NEMOTRON_UNAVAILABLE")
            await stream.close()
            stream = None
        finally:
            try:
                if stream is not None and callable(getattr(stream, "close", None)):
                    await stream.close()
            finally:
                if lifecycle_lease is not None:
                    await lifecycle_lease.release()

    async def _warm_qwen(self) -> None:
        result = await asyncio.to_thread(self._extractor.extract, _QWEN_PROMPT)
        if not isinstance(result, ExtractedIntent):
            raise _StageFailure("QWEN_UNAVAILABLE")

    async def _warm_kokoro(self) -> None:
        payload = await self._synthesizer(_KOKORO_PHRASE)
        if (
            not isinstance(payload, bytes)
            or len(payload) < 12
            or len(payload) > self._max_wav_bytes
            or payload[:4] != b"RIFF"
            or payload[8:12] != b"WAVE"
        ):
            raise _StageFailure("KOKORO_UNAVAILABLE")

    async def _warm_faster_whisper(self) -> None:
        if self._faster_whisper_recognizer is None:
            raise _StageFailure("FASTER_WHISPER_UNAVAILABLE")

        async def ignore_partial(_text: str) -> None:
            return None

        result = await self._faster_whisper_recognizer.transcribe(
            b"\x00" * 6_400, ignore_partial
        )
        if not isinstance(result, str):
            raise _StageFailure("FASTER_WHISPER_UNAVAILABLE")

    def _capture_residency(self, snapshot: GpuStatusSnapshot) -> None:
        workers: list[dict[str, object]] = []
        for name, service_name, expected_profile, reason_code in _REQUIRED_WORKERS:
            worker = snapshot.service(service_name)
            invalid = (
                worker is None
                or worker.worker_pid is None
                or worker.profile != expected_profile
                or worker.state not in {"ready", "busy"}
                or worker.heartbeat_age_seconds > 5
                or worker.observed_vram_mb > worker.budget_mb
                or (name == "nemotron" and worker.active_jobs > 1)
            )
            if invalid:
                stage = self._stage(name)  # type: ignore[arg-type]
                stage.state = "FAILED"
                stage.reason_code = reason_code
                raise _StageFailure(reason_code)
            public: dict[str, object] = {
                "name": name,
                "service": service_name,
                "profile": worker.profile,
                "state": worker.state,
                "worker_pid": worker.worker_pid,
                "active_jobs": worker.active_jobs,
                "heartbeat_age_seconds": worker.heartbeat_age_seconds,
                "budget_mb": worker.budget_mb,
                "observed_vram_mb": worker.observed_vram_mb,
            }
            if name == "qwen":
                public.update({
                    "model": "qwen35-9b-abliterated-q5",
                    "device": "cuda",
                    "compute_type": "Q5_K_M",
                    "context_tokens": 4096,
                })
            elif name == "faster_whisper":
                public.update({
                    "model": "medium",
                    "device": "cuda",
                    "compute_type": "int8_float16",
                    "context_tokens": None,
                })
            workers.append(public)
        if snapshot.free_mb < snapshot.safety_reserve_mb:
            stage = self._stage("capacity")
            stage.state = "FAILED"
            stage.reason_code = "GPU_RESERVE_LOW"
            raise _StageFailure(stage.reason_code)
        self._workers = workers
        self._gpu = {
            "total_mb": snapshot.total_mb,
            "free_mb": snapshot.free_mb,
            "safety_reserve_mb": snapshot.safety_reserve_mb,
        }

    async def _refresh_ready_state(self) -> None:
        now = self._normalized_now()
        if self._ready_until is None or now >= self._ready_until:
            self._state = "DEGRADED"
            self._reason_code = "READY_WINDOW_EXPIRED"
            self._workers = []
            self._gpu = None
            return
        try:
            snapshot: GpuStatusSnapshot = await asyncio.wait_for(
                self._status_client.read(), timeout=self._timeouts["capacity"]
            )
        except Exception:
            self._state = "DEGRADED"
            self._reason_code = "GPU_STATUS_UNAVAILABLE"
            self._stage("capacity").state = "FAILED"
            self._stage("capacity").reason_code = "GPU_STATUS_UNAVAILABLE"
            return
        try:
            self._capture_residency(snapshot)
        except _StageFailure as exc:
            self._state = "DEGRADED"
            self._reason_code = exc.reason_code
            self._workers = []
            self._gpu = None

    def _stage(self, name: StageName) -> _Stage:
        return next(stage for stage in self._stages if stage.name == name)

    def _normalized_now(self) -> datetime:
        value = self._utc_now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _snapshot(self) -> dict[str, object]:
        return {
            "state": self._state,
            "reason_code": self._reason_code,
            "best_effort": False,
            "started_at": self._iso(self._started_at),
            "completed_at": self._iso(self._completed_at),
            "ready_until": self._iso(self._ready_until),
            "stages": [
                {
                    "name": stage.name,
                    "state": stage.state,
                    "latency_ms": stage.latency_ms,
                    "reason_code": stage.reason_code,
                }
                for stage in self._stages
            ],
            "workers": [dict(worker) for worker in self._workers],
            "gpu": dict(self._gpu) if self._gpu is not None else None,
            "warning": _WARNING,
        }

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

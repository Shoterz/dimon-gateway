"""GPU-lease-aware lifecycle for the model-holding child process."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

import httpx

from services.faster_whisper_cuda.install import (
    REQUIRED_COMPUTE_TYPE,
    REQUIRED_DEVICE,
    REQUIRED_MODEL,
)


PROFILE = "faster-whisper-medium-cuda"


class CapacityBroker(Protocol):
    def acquire(self, profile: str, *, hard: bool) -> dict[str, object]: ...

    def worker_started(self, worker_pid: int) -> None: ...

    def set_active_jobs(self, active_jobs: int) -> None: ...

    def release(self) -> None: ...


class ProcessPort(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int | None: ...


ProcessFactory = Callable[[Sequence[str], Mapping[str, str]], ProcessPort]
ReadyChecker = Callable[[ProcessPort], Mapping[str, object]]
ProbeChecker = Callable[[ProcessPort], Mapping[str, object]]
ProcessStopper = Callable[[ProcessPort], None]


class WorkerBusyError(RuntimeError):
    pass


def default_process_factory(
    command: Sequence[str], environment: Mapping[str, str]
) -> ProcessPort:
    return subprocess.Popen(
        list(command),
        env=dict(environment),
        start_new_session=True,
    )


def default_stop_process(process: ProcessPort) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except Exception:
        process.kill()
        process.wait(timeout=5)


def default_ready_checker(
    process: ProcessPort,
    *,
    url: str = "http://127.0.0.1:8008/health",
    timeout_seconds: float = 180.0,
) -> Mapping[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("WORKER_EXITED_DURING_STARTUP")
        try:
            response = httpx.get(url, timeout=1.0)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                return payload
        except Exception as error:
            last_error = error
        time.sleep(0.2)
    raise RuntimeError("WORKER_STARTUP_TIMEOUT") from last_error


def default_probe_checker(
    process: ProcessPort,
    *,
    url: str = "http://127.0.0.1:8008/probe",
) -> Mapping[str, object]:
    if process.poll() is not None:
        raise RuntimeError("WORKER_EXITED_BEFORE_PROBE")
    response = httpx.post(url, timeout=60.0)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("WORKER_PROBE_FAILED")
    return payload


class ManagedCudaWhisper:
    def __init__(
        self,
        *,
        broker: CapacityBroker,
        worker_command: Sequence[str],
        worker_environment: Mapping[str, str],
        process_factory: ProcessFactory = default_process_factory,
        ready_checker: ReadyChecker = default_ready_checker,
        probe_checker: ProbeChecker = default_probe_checker,
        process_stopper: ProcessStopper = default_stop_process,
    ) -> None:
        self._broker = broker
        self._worker_command = tuple(worker_command)
        self._worker_environment = dict(worker_environment)
        self._process_factory = process_factory
        self._ready_checker = ready_checker
        self._probe_checker = probe_checker
        self._process_stopper = process_stopper
        self._condition = threading.Condition(threading.RLock())
        self._state = "idle"
        self._process: ProcessPort | None = None
        self._lease_held = False
        self._active_jobs = 0
        self._ready_at: float | None = None
        self._last_probe_at: float | None = None
        self._last_error: str | None = None
        self._closed = False

    def ensure_loaded(self, *, hard: bool = True) -> dict[str, object]:
        with self._condition:
            while self._state in {"loading", "draining"}:
                self._condition.wait()
            if self._closed:
                raise RuntimeError("SERVICE_CLOSED")
            if self._worker_running_locked() and self._state in {"ready", "busy"}:
                return self._snapshot_locked()
            self._state = "loading"
            self._last_error = None

        process: ProcessPort | None = None
        lease_held = False
        try:
            self._broker.acquire(PROFILE, hard=hard)
            lease_held = True
            process = self._process_factory(
                self._worker_command, self._worker_environment
            )
            health = self._ready_checker(process)
            self._validate_health(process, health)
            probe = self._probe_checker(process)
            if probe.get("ok") is not True:
                raise RuntimeError("WORKER_PROBE_FAILED")
            last_probe_at = probe.get("last_probe_at")
            if not isinstance(last_probe_at, (int, float)):
                raise RuntimeError("WORKER_PROBE_INVALID")
            self._broker.worker_started(process.pid)
            with self._condition:
                self._process = process
                self._lease_held = True
                self._state = "ready"
                self._ready_at = time.time()
                self._last_probe_at = float(last_probe_at)
                self._condition.notify_all()
                snapshot = self._snapshot_locked()
            threading.Thread(
                target=self._watch_process,
                args=(process,),
                name="faster-whisper-worker-watch",
                daemon=True,
            ).start()
            return snapshot
        except BaseException as error:
            if process is not None:
                self._process_stopper(process)
            if lease_held:
                self._broker.release()
            with self._condition:
                self._process = None
                self._lease_held = False
                self._active_jobs = 0
                self._state = "degraded"
                self._last_error = self._reason_code(error)
                self._ready_at = None
                self._last_probe_at = None
                self._condition.notify_all()
            raise

    def begin_job(self) -> dict[str, object]:
        while True:
            self.ensure_loaded()
            with self._condition:
                if not self._worker_running_locked() or self._state not in {
                    "ready",
                    "busy",
                }:
                    continue
                if self._active_jobs:
                    raise WorkerBusyError("WORKER_BUSY")
                self._active_jobs = 1
                self._state = "busy"
                try:
                    self._broker.set_active_jobs(1)
                except BaseException:
                    self._active_jobs = 0
                    self._state = "ready"
                    self._condition.notify_all()
                    raise
                return self._snapshot_locked()

    def end_job(self) -> None:
        with self._condition:
            if self._active_jobs <= 0:
                return
            self._active_jobs = 0
            self._state = "ready" if self._worker_running_locked() else "degraded"
            try:
                self._broker.set_active_jobs(0)
            finally:
                self._condition.notify_all()

    def run_probe(self) -> dict[str, object]:
        self.begin_job()
        try:
            with self._condition:
                process = self._process
            if process is None:
                raise RuntimeError("WORKER_UNAVAILABLE")
            result = self._probe_checker(process)
            last_probe_at = result.get("last_probe_at")
            if result.get("ok") is not True or not isinstance(
                last_probe_at, (int, float)
            ):
                raise RuntimeError("WORKER_PROBE_FAILED")
            with self._condition:
                self._last_probe_at = float(last_probe_at)
        finally:
            self.end_job()
        return self.snapshot()

    def evict_if_idle(self) -> bool:
        with self._condition:
            if self._state in {"loading", "draining"} or self._active_jobs:
                return False
            process = self._process
            self._state = "draining"
        if process is not None:
            self._process_stopper(process)
        with self._condition:
            if self._process is process:
                self._process = None
            self._active_jobs = 0
            self._ready_at = None
            self._last_probe_at = None
            self._release_lease_locked()
            self._state = "idle"
            self._condition.notify_all()
        return True

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            while self._state == "loading":
                self._condition.wait()
            process = self._process
            self._state = "draining"
        if process is not None:
            self._process_stopper(process)
        with self._condition:
            self._process = None
            self._active_jobs = 0
            self._ready_at = None
            self._last_probe_at = None
            self._release_lease_locked()
            self._state = "idle"
            self._condition.notify_all()

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return self._snapshot_locked()

    def _watch_process(self, process: ProcessPort) -> None:
        try:
            process.wait()
        except Exception:
            pass
        with self._condition:
            if self._process is not process or self._state == "draining":
                return
            self._process = None
            self._active_jobs = 0
            self._ready_at = None
            self._last_probe_at = None
            self._last_error = "WORKER_EXITED"
            try:
                if self._lease_held:
                    self._broker.set_active_jobs(0)
            finally:
                self._release_lease_locked()
                self._state = "degraded"
                self._condition.notify_all()

    def _worker_running_locked(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _snapshot_locked(self) -> dict[str, object]:
        loaded = self._worker_running_locked() and self._state in {"ready", "busy"}
        return {
            "status": self._state,
            "ready": loaded and self._last_probe_at is not None,
            "loaded": loaded,
            "frontend_pid": os.getpid(),
            "worker_pid": self._process.pid if loaded and self._process else None,
            "model": REQUIRED_MODEL if loaded else None,
            "device": REQUIRED_DEVICE if loaded else None,
            "compute_type": REQUIRED_COMPUTE_TYPE if loaded else None,
            "active_jobs": self._active_jobs,
            "ready_at": self._ready_at,
            "last_probe_at": self._last_probe_at,
            "reason_code": self._last_error,
        }

    def _release_lease_locked(self) -> None:
        if not self._lease_held:
            return
        self._broker.release()
        self._lease_held = False

    @staticmethod
    def _validate_health(
        process: ProcessPort, health: Mapping[str, object]
    ) -> None:
        expected = {
            "ready": True,
            "loaded": True,
            "model": REQUIRED_MODEL,
            "device": REQUIRED_DEVICE,
            "compute_type": REQUIRED_COMPUTE_TYPE,
            "pid": process.pid,
        }
        if any(health.get(key) != value for key, value in expected.items()):
            raise RuntimeError("WORKER_IDENTITY_MISMATCH")

    @staticmethod
    def _reason_code(error: BaseException) -> str:
        message = str(error)
        if message and message.isupper() and " " not in message:
            return message[:80]
        return "WORKER_START_FAILED"

from __future__ import annotations

import threading
import time
import unittest

from services.faster_whisper_cuda.managed import (
    ManagedCudaWhisper,
    WorkerBusyError,
)


class RecordingBroker:
    def __init__(self, *, worker_error: Exception | None = None) -> None:
        self.events: list[tuple[object, ...]] = []
        self.lease = False
        self.worker_error = worker_error

    def acquire(self, profile: str, *, hard: bool) -> dict[str, object]:
        self.events.append(("acquire", profile, hard))
        self.lease = True
        return {"ok": True}

    def worker_started(self, worker_pid: int) -> None:
        self.events.append(("worker_started", worker_pid))
        if self.worker_error:
            raise self.worker_error

    def set_active_jobs(self, active_jobs: int) -> None:
        self.events.append(("active_jobs", active_jobs))

    def release(self) -> None:
        if self.lease:
            self.events.append(("release",))
        self.lease = False


class FakeProcess:
    next_pid = 61_000

    def __init__(self) -> None:
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._exited = threading.Event()

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0
        self._exited.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._exited.set()

    def wait(self, timeout=None):
        if not self._exited.wait(timeout):
            raise TimeoutError("still running")
        return self.returncode

    def crash(self) -> None:
        self.returncode = 7
        self._exited.set()


class ManagedCudaWhisperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = RecordingBroker()
        self.processes: list[FakeProcess] = []
        self.events = self.broker.events
        self.health = {
            "ready": True,
            "loaded": True,
            "model": "medium",
            "device": "cuda",
            "compute_type": "int8_float16",
            "last_probe_at": 100.0,
        }

        def process_factory(command, environment):
            del environment
            self.events.append(("launch", tuple(command)))
            process = FakeProcess()
            self.processes.append(process)
            return process

        def ready_checker(process):
            self.events.append(("health", process.pid))
            return dict(self.health, pid=process.pid)

        def probe_checker(process):
            self.events.append(("probe", process.pid))
            return {"ok": True, "last_probe_at": 101.0}

        self.manager = ManagedCudaWhisper(
            broker=self.broker,
            worker_command=("nested-python", "-m", "worker"),
            worker_environment={"SAFE": "1"},
            process_factory=process_factory,
            ready_checker=ready_checker,
            probe_checker=probe_checker,
        )
        self.addCleanup(self.manager.close)

    def test_gpu_lease_is_acquired_before_child_launch(self) -> None:
        status = self.manager.ensure_loaded()

        names = [event[0] for event in self.events]
        self.assertLess(names.index("acquire"), names.index("launch"))
        self.assertLess(names.index("health"), names.index("probe"))
        self.assertLess(names.index("probe"), names.index("worker_started"))
        self.assertTrue(status["ready"])

    def test_child_pid_is_published_only_after_identity_and_probe(self) -> None:
        status = self.manager.ensure_loaded()

        pid = self.processes[0].pid
        self.assertIn(("worker_started", pid), self.events)
        self.assertEqual(status["worker_pid"], pid)
        self.assertEqual(status["model"], "medium")
        self.assertEqual(status["device"], "cuda")
        self.assertEqual(status["compute_type"], "int8_float16")
        self.assertEqual(status["last_probe_at"], 101.0)

    def test_load_failure_terminates_child_and_releases_once(self) -> None:
        self.health["device"] = "cpu"

        with self.assertRaisesRegex(RuntimeError, "WORKER_IDENTITY_MISMATCH"):
            self.manager.ensure_loaded()

        self.assertTrue(self.processes[0].terminated)
        self.assertFalse(self.broker.lease)
        self.assertEqual(self.events.count(("release",)), 1)
        self.assertFalse(self.manager.snapshot()["loaded"])

    def test_unexpected_child_exit_releases_and_clears_loaded_status(self) -> None:
        self.manager.ensure_loaded()
        process = self.processes[0]

        process.crash()
        deadline = time.monotonic() + 1
        while self.broker.lease and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertFalse(self.manager.snapshot()["loaded"])
        self.assertFalse(self.broker.lease)
        self.assertEqual(self.events.count(("release",)), 1)

    def test_idle_eviction_stops_child_and_releases(self) -> None:
        self.manager.ensure_loaded()

        self.assertTrue(self.manager.evict_if_idle())

        self.assertTrue(self.processes[0].terminated)
        self.assertFalse(self.manager.snapshot()["loaded"])
        self.assertFalse(self.broker.lease)

    def test_busy_eviction_is_rejected(self) -> None:
        self.manager.begin_job()

        self.assertFalse(self.manager.evict_if_idle())
        self.assertFalse(self.processes[0].terminated)
        self.manager.end_job()
        self.assertTrue(self.manager.evict_if_idle())

    def test_warm_is_idempotent_for_same_live_child(self) -> None:
        first = self.manager.ensure_loaded()
        second = self.manager.ensure_loaded()

        self.assertEqual(first["worker_pid"], second["worker_pid"])
        self.assertEqual(len(self.processes), 1)
        self.assertEqual([event[0] for event in self.events].count("acquire"), 1)

    def test_only_one_managed_job_is_admitted(self) -> None:
        self.manager.begin_job()

        with self.assertRaises(WorkerBusyError):
            self.manager.begin_job()

        self.assertEqual(self.manager.snapshot()["active_jobs"], 1)
        self.manager.end_job()
        self.assertEqual(self.manager.snapshot()["active_jobs"], 0)


if __name__ == "__main__":
    unittest.main()

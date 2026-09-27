from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.gpu_host.capacity import CapacityProfile, GpuCapacityBroker
from services.gpu_host.processes import ManagedProcess, ProcessStateError, SubprocessAdapter
from services.gpu_host.supervisor import HostSupervisor


class FakeTelemetry:
    def free_vram_mb(self) -> int:
        return 12_000


class FakeAdapter:
    def __init__(self):
        self.pid = 4000
        self.alive: set[int] = set()
        self.identities: dict[int, str] = {}
        self.stopped: list[int] = []

    def spawn(self, command, log_path):
        self.pid += 1
        self.alive.add(self.pid)
        self.identities[self.pid] = f"identity-{self.pid}"
        return self.pid

    def identity(self, pid):
        return self.identities.get(pid)

    def is_alive(self, pid):
        return pid in self.alive

    def stop_group(self, pid, timeout):
        self.stopped.append(pid)
        self.alive.discard(pid)


class ManagedProcessTests(unittest.TestCase):
    def test_process_identity_survives_launcher_exec(self) -> None:
        stat = "123 (launcher) S " + " ".join(["0"] * 18 + ["456"] + ["0"] * 8)
        with patch(
            "services.gpu_host.processes.Path.read_text",
            side_effect=[stat, "boot-id\n"],
        ):
            before_exec = SubprocessAdapter().identity(123)
        stat_after = stat.replace("(launcher)", "(python)")
        with patch(
            "services.gpu_host.processes.Path.read_text",
            side_effect=[stat_after, "boot-id\n"],
        ):
            after_exec = SubprocessAdapter().identity(123)
        self.assertEqual(before_exec, after_exec)

    def test_start_ready_and_stop_only_recorded_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            adapter = FakeAdapter()
            ready = {"value": False}
            process = ManagedProcess(
                name="qwen",
                command=("services/qwen/run.sh",),
                state_dir=Path(temp),
                readiness=lambda: ready["value"],
                adapter=adapter,
                startup_timeout_seconds=0.03,
                poll_seconds=0.001,
                backoff_seconds=0,
            )
            with self.assertRaises(ProcessStateError):
                process.start()
            self.assertEqual(adapter.stopped, [4001])

            ready["value"] = True
            pid = process.start()
            self.assertTrue(process.ready())
            process.stop()
            self.assertEqual(adapter.stopped[-1], pid)

    def test_stale_or_foreign_pid_record_is_never_signalled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = FakeAdapter()
            process = ManagedProcess(
                name="qwen",
                command=("services/qwen/run.sh",),
                state_dir=root,
                readiness=lambda: True,
                adapter=adapter,
            )
            (root / "qwen.json").write_text(
                '{"pid":42,"identity":"different","command_hash":"bad"}',
                encoding="utf-8",
            )
            process.stop()
            self.assertEqual(adapter.stopped, [])
            self.assertFalse((root / "qwen.json").exists())

    def test_crash_backoff_prevents_restart_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            adapter = FakeAdapter()
            process = ManagedProcess(
                name="qwen",
                command=("services/qwen/run.sh",),
                state_dir=Path(temp),
                readiness=lambda: False,
                adapter=adapter,
                startup_timeout_seconds=0,
                backoff_seconds=60,
            )
            with self.assertRaises(ProcessStateError):
                process.start()
            with self.assertRaises(ProcessStateError):
                process.start()
            self.assertEqual(adapter.pid, 4001)

    def test_failed_upstream_can_restart_after_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            adapter = FakeAdapter()
            ready = {"value": False}
            process = ManagedProcess(
                name="qwen",
                command=("services/qwen/run.sh",),
                state_dir=Path(temp),
                readiness=lambda: ready["value"],
                adapter=adapter,
                startup_timeout_seconds=0,
                backoff_seconds=0,
            )
            with self.assertRaises(ProcessStateError):
                process.start()
            ready["value"] = True
            restarted_pid = process.start()
            self.assertEqual(restarted_pid, 4002)
            self.assertTrue(process.ready())

    def test_supervisor_acquires_capacity_and_returns_typed_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            adapter = FakeAdapter()
            process = ManagedProcess(
                name="qwen",
                command=("services/qwen/run.sh",),
                state_dir=Path(temp),
                readiness=lambda: True,
                adapter=adapter,
            )
            broker = GpuCapacityBroker(FakeTelemetry(), safety_reserve_mb=512)
            supervisor = HostSupervisor(
                broker,
                {"intent": (process, CapacityProfile("qwen", 7168, 30, 300))},
            )
            result = supervisor.ensure("intent")
            self.assertTrue(result.ready)
            self.assertEqual(result.reason_code, "READY")
            supervisor.stop_all()
            self.assertEqual(broker.reserved_mb, 0)


if __name__ == "__main__":
    unittest.main()

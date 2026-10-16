from __future__ import annotations

import asyncio
import os
import stat
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from apps.dimon_live.gpu_overlord_status import (
    GpuServiceStatus,
    GpuStatusError,
    GpuStatusSnapshot,
)
from services.inference_gateway.gpu_reset import (
    GpuStackResetError,
    SystemdGpuStackResetter,
    remove_verified_stale_socket,
)


def snapshot(*pids: int) -> GpuStatusSnapshot:
    services = tuple(
        GpuServiceStatus(
            service=name,
            state="ready",
            active_jobs=0,
            worker_pid=pid,
            profile=f"{name}-profile",
            budget_mb=1_000,
            observed_vram_mb=800,
            heartbeat_age_seconds=0.1,
        )
        for name, pid in zip(
            ("llama", "nemotron", "kokoro", "faster_whisper"),
            pids,
            strict=True,
        )
    )
    return GpuStatusSnapshot(12_282, 2_000, 512, services)


class ResetStatusClient:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.started = False

    async def read(self) -> GpuStatusSnapshot:
        self.events.append("status:started" if self.started else "status:before")
        return snapshot(*(201, 202, 203, 204) if self.started else (101, 102, 103, 104))


class GpuStackResetTests(unittest.TestCase):
    def test_refuses_to_stop_workers_when_initial_status_is_unavailable(self) -> None:
        async def scenario():
            events: list[str] = []

            class UnavailableStatusClient:
                async def read(self):
                    raise GpuStatusError()

            async def command(action: str, unit: str) -> None:
                events.append(f"systemctl:{action}:{unit}")

            async def control_plane_safe() -> bool:
                return True

            resetter = SystemdGpuStackResetter(
                UnavailableStatusClient(),
                command_runner=command,
                control_plane_safe=control_plane_safe,
            )
            with self.assertRaises(GpuStackResetError) as captured:
                await resetter.reset()
            return events, captured.exception.reason_code

        events, reason_code = asyncio.run(scenario())

        self.assertEqual(reason_code, "GPU_STATUS_UNAVAILABLE")
        self.assertEqual(events, [])

    def test_stop_release_start_order_returns_old_worker_pids(self) -> None:
        async def scenario():
            events: list[str] = []
            status = ResetStatusClient(events)
            resident_pids = {101, 102, 103, 104}

            async def command(action: str, unit: str) -> None:
                events.append(f"systemctl:{action}:{unit}")
                if action == "stop":
                    resident_pids.clear()
                elif action == "start":
                    status.started = True

            async def gpu_pids() -> set[int]:
                events.append("gpu-pids")
                return set(resident_pids)

            async def control_plane_safe() -> bool:
                return True

            async def clean_socket() -> None:
                events.append("socket:cleanup")

            resetter = SystemdGpuStackResetter(
                status,
                command_runner=command,
                gpu_pid_reader=gpu_pids,
                control_plane_safe=control_plane_safe,
                stale_socket_cleanup=clean_socket,
                poll_seconds=0.001,
                timeout_seconds=0.05,
            )
            old_pids = await resetter.reset()
            return events, old_pids

        events, old_pids = asyncio.run(scenario())

        self.assertEqual(old_pids, frozenset({101, 102, 103, 104}))
        self.assertEqual(
            events,
            [
                "status:before",
                "systemctl:stop:dimon-faster-whisper-medium.service",
                "systemctl:stop:jarvis-nemotron.service",
                "systemctl:stop:gpu-overlord-stack.service",
                "gpu-pids",
                "socket:cleanup",
                "systemctl:reset-failed:gpu-overlord-stack.service",
                "systemctl:start:gpu-overlord-stack.service",
                "systemctl:reset-failed:jarvis-nemotron.service",
                "systemctl:start:jarvis-nemotron.service",
                "systemctl:reset-failed:dimon-faster-whisper-medium.service",
                "systemctl:start:dimon-faster-whisper-medium.service",
                "status:started",
            ],
        )

    def test_removes_only_a_verified_stale_unix_socket(self) -> None:
        async def scenario():
            class StaleSocket:
                removed = False

                def lstat(self):
                    return SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=os.getuid())

                def unlink(self):
                    self.removed = True

            path = StaleSocket()
            with patch(
                "services.inference_gateway.gpu_reset.asyncio.open_unix_connection",
                new=AsyncMock(side_effect=ConnectionRefusedError),
            ):
                await remove_verified_stale_socket(path)
            return path.removed

        self.assertTrue(asyncio.run(scenario()))

    def test_refuses_to_remove_a_live_unix_socket(self) -> None:
        async def scenario():
            writer = SimpleNamespace(
                close=lambda: None,
                wait_closed=AsyncMock(),
            )
            with patch(
                "services.inference_gateway.gpu_reset.asyncio.open_unix_connection",
                new=AsyncMock(return_value=(object(), writer)),
            ):
                with self.assertRaises(GpuStackResetError) as captured:
                    await remove_verified_stale_socket(object())
            return captured.exception.reason_code, writer.wait_closed.await_count

        reason_code, close_waits = asyncio.run(scenario())
        self.assertEqual(reason_code, "GPU_SOCKET_STILL_ACTIVE")
        self.assertEqual(close_waits, 1)

    def test_never_starts_replacement_workers_when_old_gpu_pids_remain(self) -> None:
        async def scenario():
            events: list[str] = []
            status = ResetStatusClient(events)

            async def command(action: str, unit: str) -> None:
                events.append(f"systemctl:{action}:{unit}")

            async def gpu_pids() -> set[int]:
                return {101}

            async def control_plane_safe() -> bool:
                return True

            resetter = SystemdGpuStackResetter(
                status,
                command_runner=command,
                gpu_pid_reader=gpu_pids,
                control_plane_safe=control_plane_safe,
                poll_seconds=0.001,
                timeout_seconds=0.005,
            )
            with self.assertRaises(GpuStackResetError) as captured:
                await resetter.reset()
            return events, captured.exception.reason_code

        events, reason_code = asyncio.run(scenario())

        self.assertEqual(reason_code, "GPU_RELEASE_TIMEOUT")
        self.assertEqual(
            events,
            [
                "status:before",
                "systemctl:stop:dimon-faster-whisper-medium.service",
                "systemctl:stop:jarvis-nemotron.service",
                "systemctl:stop:gpu-overlord-stack.service",
            ],
        )

    def test_refuses_to_stop_workers_when_gateway_requires_gpu_stack(self) -> None:
        async def scenario():
            events: list[str] = []
            status = ResetStatusClient(events)

            async def command(action: str, unit: str) -> None:
                events.append(f"systemctl:{action}:{unit}")

            async def control_plane_safe() -> bool:
                return False

            resetter = SystemdGpuStackResetter(
                status,
                command_runner=command,
                control_plane_safe=control_plane_safe,
            )
            with self.assertRaises(GpuStackResetError) as captured:
                await resetter.reset()
            return events, captured.exception.reason_code

        events, reason_code = asyncio.run(scenario())

        self.assertEqual(reason_code, "GPU_RESET_CONTROL_PLANE_BOUND")
        self.assertEqual(events, [])

    def test_waits_for_all_four_supervisor_registrations_before_probing(self) -> None:
        async def scenario():
            class RegisteringStatusClient:
                def __init__(self) -> None:
                    self.started = False
                    self.registration_reads = 0

                async def read(self) -> GpuStatusSnapshot:
                    if not self.started:
                        return snapshot(101, 102, 103, 104)
                    self.registration_reads += 1
                    if self.registration_reads == 1:
                        return GpuStatusSnapshot(
                            12_282,
                            10_000,
                            512,
                            snapshot(201, 202, 203, 204).services[:1],
                        )
                    return snapshot(201, 202, 203, 204)

            status = RegisteringStatusClient()

            async def command(action: str, _unit: str) -> None:
                if action == "start":
                    status.started = True

            async def no_gpu_pids() -> set[int]:
                return set()

            async def control_plane_safe() -> bool:
                return True

            resetter = SystemdGpuStackResetter(
                status,
                command_runner=command,
                gpu_pid_reader=no_gpu_pids,
                control_plane_safe=control_plane_safe,
                poll_seconds=0.001,
                timeout_seconds=0.05,
            )
            await resetter.reset()
            return status.registration_reads

        self.assertEqual(asyncio.run(scenario()), 2)


if __name__ == "__main__":
    unittest.main()

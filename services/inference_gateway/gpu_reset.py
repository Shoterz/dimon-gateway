"""Ordered reset of Dimon's user-owned GPU worker stack."""

from __future__ import annotations

import asyncio
import os
import stat
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from apps.dimon_live.gpu_overlord_status import GpuStatusError


CommandRunner = Callable[[str, str], Awaitable[None]]
GpuPidReader = Callable[[], Awaitable[set[int]]]
ControlPlaneCheck = Callable[[], Awaitable[bool]]
StaleSocketCleanup = Callable[[], Awaitable[None]]

_STOP_UNITS = (
    "dimon-faster-whisper-medium.service",
    "jarvis-nemotron.service",
    "gpu-overlord-stack.service",
)
_EXPECTED_SERVICES = frozenset({"llama", "nemotron", "kokoro", "faster_whisper"})


class GpuStackResetError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


async def _systemctl(action: str, unit: str) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            action,
            unit,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
    except (OSError, asyncio.SubprocessError) as exc:
        raise GpuStackResetError(f"GPU_STACK_{action.upper()}_FAILED") from exc
    if process.returncode != 0:
        raise GpuStackResetError(f"GPU_STACK_{action.upper()}_FAILED") from RuntimeError(
            stderr.decode("utf-8", errors="replace")[:512]
        )


async def _nvidia_compute_pids() -> set[int]:
    try:
        process = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-compute-apps=pid",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
    except (OSError, asyncio.SubprocessError) as exc:
        raise GpuStackResetError("GPU_RELEASE_UNVERIFIED") from exc
    if process.returncode != 0:
        raise GpuStackResetError("GPU_RELEASE_UNVERIFIED")
    pids: set[int] = set()
    for line in stdout.decode("ascii", errors="ignore").splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid > 0:
            pids.add(pid)
    return pids


async def _gateway_survives_worker_stop(
    stop_units: tuple[str, ...] = _STOP_UNITS,
) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "show",
            "dimon-inference-gateway.service",
            "--property=Requires",
            "--property=Requisite",
            "--property=BindsTo",
            "--property=PartOf",
            "--value",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
    except (OSError, asyncio.SubprocessError):
        return False
    if process.returncode != 0:
        return False
    stop_unit_set = set(stop_units)
    dependencies = set(stdout.decode("utf-8", errors="replace").split())
    return stop_unit_set.isdisjoint(dependencies)


async def remove_verified_stale_socket(socket_path: Path) -> None:
    """Remove an unowned Overlord socket without ever unlinking a live one."""

    writer: asyncio.StreamWriter | None = None
    try:
        _, writer = await asyncio.open_unix_connection(socket_path)
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    except OSError as exc:
        raise GpuStackResetError("GPU_SOCKET_STATE_UNVERIFIED") from exc
    else:
        writer.close()
        await writer.wait_closed()
        raise GpuStackResetError("GPU_SOCKET_STILL_ACTIVE")

    try:
        metadata = socket_path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise GpuStackResetError("GPU_SOCKET_UNSAFE")
    try:
        socket_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise GpuStackResetError("GPU_SOCKET_CLEANUP_FAILED") from exc


class SystemdGpuStackResetter:
    """Stop, prove release, and restart the exact Dimon GPU systemd unit."""

    def __init__(
        self,
        status_client,
        *,
        command_runner: CommandRunner = _systemctl,
        gpu_pid_reader: GpuPidReader = _nvidia_compute_pids,
        control_plane_safe: ControlPlaneCheck | None = None,
        stop_units: tuple[str, ...] = _STOP_UNITS,
        overlord_socket_path: Path | None = None,
        stale_socket_cleanup: StaleSocketCleanup | None = None,
        poll_seconds: float = 0.1,
        timeout_seconds: float = 30.0,
    ) -> None:
        if poll_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("GPU reset timings must be positive")
        self.status_client = status_client
        self.command_runner = command_runner
        self.gpu_pid_reader = gpu_pid_reader
        if not stop_units:
            raise ValueError("GPU reset requires at least one systemd unit")
        self.stop_units = tuple(stop_units)
        self.start_units = tuple(reversed(self.stop_units))
        self.control_plane_safe = control_plane_safe or (
            lambda: _gateway_survives_worker_stop(self.stop_units)
        )
        if stale_socket_cleanup is not None:
            self.stale_socket_cleanup = stale_socket_cleanup
        elif overlord_socket_path is not None:
            self.stale_socket_cleanup = lambda: remove_verified_stale_socket(
                overlord_socket_path
            )
        else:
            self.stale_socket_cleanup = None
        self.poll_seconds = float(poll_seconds)
        self.timeout_seconds = float(timeout_seconds)

    async def reset(self) -> frozenset[int]:
        if not await self.control_plane_safe():
            raise GpuStackResetError("GPU_RESET_CONTROL_PLANE_BOUND")
        try:
            before = await self.status_client.read()
        except GpuStatusError as exc:
            raise GpuStackResetError("GPU_STATUS_UNAVAILABLE") from exc
        old_pids = frozenset(
            service.worker_pid
            for service in before.services
            if service.worker_pid is not None
        )

        for unit in self.stop_units:
            await self.command_runner("stop", unit)
        await self._wait_for_release(old_pids)
        if self.stale_socket_cleanup is not None:
            await self.stale_socket_cleanup()
        for unit in self.start_units:
            await self.command_runner("reset-failed", unit)
            await self.command_runner("start", unit)
        await self._wait_for_supervisor()
        return old_pids

    async def _wait_for_release(self, old_pids: frozenset[int]) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            resident = await self.gpu_pid_reader()
            if old_pids.isdisjoint(resident):
                return
            if time.monotonic() >= deadline:
                raise GpuStackResetError("GPU_RELEASE_TIMEOUT")
            await asyncio.sleep(self.poll_seconds)

    async def _wait_for_supervisor(self) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                status = await self.status_client.read()
            except GpuStatusError:
                status = None
            if status is not None:
                registered = {service.service for service in status.services}
                if _EXPECTED_SERVICES.issubset(registered):
                    return
            if time.monotonic() >= deadline:
                raise GpuStackResetError("GPU_SUPERVISOR_START_TIMEOUT") from None
            await asyncio.sleep(self.poll_seconds)

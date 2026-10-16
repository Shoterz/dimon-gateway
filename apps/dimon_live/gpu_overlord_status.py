"""Bounded, read-only access to GPU Overlord's status protocol."""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from pathlib import Path


_VALID_STATES = frozenset({"idle", "starting", "ready", "busy", "draining", "error"})
_STATUS_REQUEST = b'{"op":"status"}\n'


@dataclass(frozen=True, init=False)
class GpuServiceStatus:
    service: str
    state: str
    active_jobs: int
    budget_mb: int
    observed_vram_mb: int
    worker_pid: int | None = None
    profile: str | None = None
    heartbeat_age_seconds: float = 0.0

    def __init__(
        self,
        *,
        service: str,
        state: str,
        active_jobs: int,
        budget_mb: int,
        observed_vram_mb: int,
        worker_pid: int | None = None,
        profile: str | None = None,
        heartbeat_age_seconds: float = 0.0,
        worker_present: bool | None = None,
    ) -> None:
        if worker_present is True and worker_pid is None:
            # Compatibility for the previous boolean-only status record. New
            # parsed status always carries the supervisor's real positive PID.
            worker_pid = 1
        elif worker_present is False:
            worker_pid = None
        object.__setattr__(self, "service", service)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "active_jobs", active_jobs)
        object.__setattr__(self, "budget_mb", budget_mb)
        object.__setattr__(self, "observed_vram_mb", observed_vram_mb)
        object.__setattr__(self, "worker_pid", worker_pid)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "heartbeat_age_seconds", heartbeat_age_seconds)

    @property
    def worker_present(self) -> bool:
        return self.worker_pid is not None


@dataclass(frozen=True)
class GpuStatusSnapshot:
    total_mb: int
    free_mb: int
    safety_reserve_mb: int
    services: tuple[GpuServiceStatus, ...]

    def service(self, name: str) -> GpuServiceStatus | None:
        return next((item for item in self.services if item.service == name), None)


class GpuStatusError(RuntimeError):
    reason_code = "GPU_STATUS_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.reason_code)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GpuStatusError()
    return value


def _non_negative_integer(mapping: dict[str, object], name: str) -> int:
    value = mapping.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GpuStatusError()
    return value


def _non_negative_number(mapping: dict[str, object], name: str) -> float:
    value = mapping.get(name)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise GpuStatusError()
    return float(value)


def parse_gpu_status(response: object) -> GpuStatusSnapshot:
    envelope = _mapping(response)
    if envelope.get("ok") is not True:
        raise GpuStatusError()

    status = _mapping(envelope.get("status"))
    gpu = _mapping(status.get("gpu"))
    raw_services = status.get("services")
    if not isinstance(raw_services, list):
        raise GpuStatusError()

    services: list[GpuServiceStatus] = []
    seen_services: set[str] = set()
    for raw_service in raw_services:
        item = _mapping(raw_service)
        service = item.get("service")
        state = item.get("state")
        worker_pid = item.get("worker_pid")
        profile = item.get("profile")
        if (
            not isinstance(service, str)
            or not service
            or len(service) > 128
            or service in seen_services
            or state not in _VALID_STATES
            or (
                profile is not None
                and (
                    not isinstance(profile, str)
                    or not profile
                    or len(profile) > 128
                )
            )
            or (
                worker_pid is not None
                and (
                    not isinstance(worker_pid, int)
                    or isinstance(worker_pid, bool)
                    or worker_pid <= 0
                )
            )
        ):
            raise GpuStatusError()
        seen_services.add(service)
        services.append(
            GpuServiceStatus(
                service=service,
                state=state,
                active_jobs=_non_negative_integer(item, "active_jobs"),
                worker_pid=worker_pid,
                profile=profile,
                budget_mb=_non_negative_integer(item, "budget_mb"),
                observed_vram_mb=_non_negative_integer(item, "observed_vram_mb"),
                heartbeat_age_seconds=_non_negative_number(
                    item, "heartbeat_age_seconds"
                ),
            )
        )

    return GpuStatusSnapshot(
        total_mb=_non_negative_integer(gpu, "total_mb"),
        free_mb=_non_negative_integer(gpu, "free_mb"),
        safety_reserve_mb=_non_negative_integer(gpu, "safety_reserve_mb"),
        services=tuple(services),
    )


class GpuOverlordStatusClient:
    def __init__(
        self,
        socket_path: Path,
        *,
        timeout_seconds: float = 2.0,
        max_response_bytes: int = 65_536,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._socket_path = socket_path
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes

    async def read(self) -> GpuStatusSnapshot:
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(self._timeout_seconds):
                reader, writer = await asyncio.open_unix_connection(
                    self._socket_path,
                    limit=self._max_response_bytes,
                )
                writer.write(_STATUS_REQUEST)
                await writer.drain()
                encoded = await reader.readuntil(b"\n")
                if len(encoded) > self._max_response_bytes:
                    raise GpuStatusError()
                response = json.loads(encoded.decode("utf-8"))
            return parse_gpu_status(response)
        except GpuStatusError:
            raise
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise GpuStatusError() from None
        finally:
            if writer is not None:
                writer.close()

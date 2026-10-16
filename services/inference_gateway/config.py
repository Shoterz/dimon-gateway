"""Strict startup configuration for the loopback inference gateway."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+@-]{0,127}$")
_SERVICE_KEY = re.compile(r"^[\x21-\x7e]{32,256}$")
_SYSTEMD_UNIT = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")
_DEFAULT_GPU_RESET_UNITS = (
    "dimon-faster-whisper-medium.service",
    "jarvis-nemotron.service",
    "gpu-overlord-stack.service",
)


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _loopback_address(value: str, label: str) -> None:
    import ipaddress

    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a loopback IP address") from exc
    if not address.is_loopback:
        raise ValueError(f"{label} must be a loopback IP address")


def _loopback_url(value: str, label: str, schemes: set[str]) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in schemes
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{label} must be a valid loopback URL")
    _loopback_address(parsed.hostname, label)
    return value.rstrip("/")


def _revision(env: Mapping[str, str], key: str) -> str:
    value = _required(env, key)
    if _REVISION.fullmatch(value) is None:
        raise ValueError(f"{key} is not a safe revision identifier")
    return value


@dataclass(frozen=True)
class GatewayConfig:
    bind_host: str
    bind_port: int
    service_key: str = field(repr=False)
    qwen_api_base: str
    qwen_model: str
    qwen_revision: str
    max_raw_output_bytes: int
    nemotron_url: str
    nemotron_control_socket: Path | None
    nemotron_revision: str
    kokoro_url: str
    kokoro_revision: str
    transcription_backend: str
    whisper_url: str | None
    whisper_revision: str | None
    gpu_overlord_socket: Path
    gpu_reset_units: tuple[str, ...]

    @property
    def transcription_url(self) -> str:
        if self.transcription_backend in {"whisper_cpu", "dual"}:
            assert self.whisper_url is not None
            return self.whisper_url
        return self.nemotron_url

    @property
    def transcription_revision(self) -> str:
        if self.transcription_backend in {"whisper_cpu", "dual"}:
            assert self.whisper_revision is not None
            return self.whisper_revision
        return self.nemotron_revision

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "GatewayConfig":
        bind_host = env.get("DIMON_GATEWAY_HOST", "127.0.0.1").strip()
        _loopback_address(bind_host, "DIMON_GATEWAY_HOST")
        try:
            bind_port = int(env.get("DIMON_GATEWAY_PORT", "8891"))
        except ValueError as exc:
            raise ValueError("DIMON_GATEWAY_PORT must be an integer") from exc
        if not 1 <= bind_port <= 65535:
            raise ValueError("DIMON_GATEWAY_PORT must be between 1 and 65535")

        service_key = _required(env, "DIMON_INFERENCE_SERVICE_KEY")
        if _SERVICE_KEY.fullmatch(service_key) is None:
            raise ValueError(
                "DIMON_INFERENCE_SERVICE_KEY must be 32-256 printable ASCII characters without spaces"
            )

        qwen = _loopback_url(
            env.get("DIMON_QWEN_API_BASE", "http://127.0.0.1:8080/v1").strip(),
            "DIMON_QWEN_API_BASE",
            {"http", "https"},
        )
        qwen_model = env.get("DIMON_QWEN_MODEL", "qwen35-9b-abliterated-q5").strip()
        if not qwen_model:
            raise ValueError("DIMON_QWEN_MODEL is required")
        qwen_revision = _revision(env, "DIMON_QWEN_REVISION")
        try:
            max_raw_output_bytes = int(
                env.get("DIMON_GATEWAY_MAX_RAW_OUTPUT_BYTES", "8192")
            )
        except ValueError as exc:
            raise ValueError("DIMON_GATEWAY_MAX_RAW_OUTPUT_BYTES must be an integer") from exc
        if not 256 <= max_raw_output_bytes <= 65_536:
            raise ValueError("DIMON_GATEWAY_MAX_RAW_OUTPUT_BYTES is outside its safe range")
        nemotron = _loopback_url(
            env.get("DIMON_NEMOTRON_URL", "ws://127.0.0.1:8004/v1/realtime").strip(),
            "DIMON_NEMOTRON_URL",
            {"ws", "wss"},
        )
        control_socket_value = env.get("DIMON_NEMOTRON_CONTROL_SOCKET", "").strip()
        control_socket = Path(control_socket_value) if control_socket_value else None
        if control_socket is not None and not control_socket.is_absolute():
            raise ValueError("DIMON_NEMOTRON_CONTROL_SOCKET must be an absolute path")
        nemotron_revision = _revision(env, "DIMON_NEMOTRON_REVISION")
        transcription_backend = (
            env.get("DIMON_GATEWAY_STT_BACKEND", "nemotron").strip().lower()
            or "nemotron"
        )
        if transcription_backend not in {"nemotron", "whisper_cpu", "dual"}:
            raise ValueError(
                "DIMON_GATEWAY_STT_BACKEND must be nemotron, whisper_cpu, or dual"
            )
        whisper_url: str | None = None
        whisper_revision: str | None = None
        if transcription_backend in {"whisper_cpu", "dual"}:
            whisper_url = _loopback_url(
                env.get("DIMON_WHISPER_URL", "ws://127.0.0.1:8003/ws").strip(),
                "DIMON_WHISPER_URL",
                {"ws", "wss"},
            )
            whisper_revision = _revision(env, "DIMON_WHISPER_REVISION")
        kokoro = _loopback_url(
            env.get("DIMON_KOKORO_URL", "http://127.0.0.1:3001").strip(),
            "DIMON_KOKORO_URL",
            {"http", "https"},
        )
        kokoro_revision = _revision(env, "DIMON_KOKORO_REVISION")
        gpu_overlord_socket_value = _required(env, "DIMON_GPU_OVERLORD_SOCKET")
        gpu_overlord_socket = Path(gpu_overlord_socket_value)
        if not gpu_overlord_socket.is_absolute():
            raise ValueError("DIMON_GPU_OVERLORD_SOCKET must be an absolute path")
        raw_reset_units = env.get(
            "DIMON_GPU_RESET_UNITS", ",".join(_DEFAULT_GPU_RESET_UNITS)
        )
        gpu_reset_units = tuple(
            unit.strip() for unit in raw_reset_units.split(",") if unit.strip()
        )
        if (
            not gpu_reset_units
            or len(gpu_reset_units) > 16
            or len(set(gpu_reset_units)) != len(gpu_reset_units)
            or any(_SYSTEMD_UNIT.fullmatch(unit) is None for unit in gpu_reset_units)
        ):
            raise ValueError(
                "DIMON_GPU_RESET_UNITS must be unique comma-separated systemd service names"
            )
        return cls(
            bind_host=bind_host,
            bind_port=bind_port,
            service_key=service_key,
            qwen_api_base=qwen,
            qwen_model=qwen_model,
            qwen_revision=qwen_revision,
            max_raw_output_bytes=max_raw_output_bytes,
            nemotron_url=nemotron,
            nemotron_control_socket=control_socket,
            nemotron_revision=nemotron_revision,
            kokoro_url=kokoro,
            kokoro_revision=kokoro_revision,
            transcription_backend=transcription_backend,
            whisper_url=whisper_url,
            whisper_revision=whisper_revision,
            gpu_overlord_socket=gpu_overlord_socket,
            gpu_reset_units=gpu_reset_units,
        )

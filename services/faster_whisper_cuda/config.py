"""Strict child-worker configuration for CUDA Medium."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from services.faster_whisper_cuda.install import (
    REQUIRED_COMPUTE_TYPE,
    REQUIRED_DEVICE,
    REQUIRED_MODEL,
)


@dataclass(frozen=True)
class WorkerSettings:
    host: str
    port: int
    model_path: Path
    model: str = REQUIRED_MODEL
    device: str = REQUIRED_DEVICE
    compute_type: str = REQUIRED_COMPUTE_TYPE
    max_audio_bytes: int = 8 * 1_048_576

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "WorkerSettings":
        env = os.environ if environ is None else environ
        host = env.get("DIMON_FASTER_WHISPER_WORKER_HOST", "127.0.0.1").strip()
        port = int(env.get("DIMON_FASTER_WHISPER_WORKER_PORT", "8008"))
        raw_model_path = env.get("DIMON_FASTER_WHISPER_MODEL_PATH", "").strip()
        model = env.get("DIMON_FASTER_WHISPER_MODEL", REQUIRED_MODEL).strip()
        device = env.get("DIMON_FASTER_WHISPER_DEVICE", REQUIRED_DEVICE).strip()
        compute_type = env.get(
            "DIMON_FASTER_WHISPER_COMPUTE_TYPE", REQUIRED_COMPUTE_TYPE
        ).strip()
        max_audio_bytes = int(
            env.get("DIMON_FASTER_WHISPER_MAX_AUDIO_BYTES", str(8 * 1_048_576))
        )
        if host != "127.0.0.1":
            raise ValueError("worker host must be 127.0.0.1")
        if port != 8008:
            raise ValueError("worker port must be 8008")
        if not raw_model_path:
            raise ValueError("DIMON_FASTER_WHISPER_MODEL_PATH is required")
        model_path = Path(raw_model_path).resolve()
        if not model_path.is_dir() or not (model_path / "model.bin").is_file():
            raise ValueError("Medium model directory is incomplete")
        if (model, device, compute_type) != (
            REQUIRED_MODEL,
            REQUIRED_DEVICE,
            REQUIRED_COMPUTE_TYPE,
        ):
            raise ValueError("worker identity must be medium/cuda/int8_float16")
        if max_audio_bytes <= 0 or max_audio_bytes > 64 * 1_048_576:
            raise ValueError("max audio bytes is outside the bounded range")
        return cls(
            host=host,
            port=port,
            model_path=model_path,
            model=model,
            device=device,
            compute_type=compute_type,
            max_audio_bytes=max_audio_bytes,
        )

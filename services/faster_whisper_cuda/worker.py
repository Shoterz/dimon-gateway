"""Private model-holding Faster-Whisper Medium CUDA worker."""

from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from services.faster_whisper_cuda.config import WorkerSettings
from services.faster_whisper_cuda.install import (
    REQUIRED_COMPUTE_TYPE,
    REQUIRED_DEVICE,
    REQUIRED_MODEL,
)
from services.faster_whisper_cuda.protocol import AudioAccumulator


class SegmentPort(Protocol):
    text: str
    avg_logprob: float
    no_speech_prob: float


class ModelPort(Protocol):
    model: object

    def transcribe(
        self, audio: np.ndarray, **kwargs: object
    ) -> tuple[Iterable[SegmentPort], object]: ...


ModelFactory = Callable[..., ModelPort]

DIMON_HOTWORDS = "Dimon NVIDIA NVDA PayNow PayLah SGD digiWealth"


@dataclass(frozen=True)
class TranscriptionSegment:
    avg_logprob: float
    no_speech_prob: float


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    segments: tuple[TranscriptionSegment, ...]


def default_model_factory(*args: object, **kwargs: object) -> ModelPort:
    from faster_whisper import WhisperModel

    return WhisperModel(*args, **kwargs)


class CudaMediumEngine:
    identity = {
        "model": REQUIRED_MODEL,
        "device": REQUIRED_DEVICE,
        "compute_type": REQUIRED_COMPUTE_TYPE,
    }

    def __init__(
        self,
        model_path: Path,
        *,
        model_factory: ModelFactory = default_model_factory,
        startup_probe: bool = True,
    ) -> None:
        if not model_path.is_dir() or not (model_path / "model.bin").is_file():
            raise RuntimeError("MODEL_MISSING")
        self._backend = model_factory(
            str(model_path),
            device=REQUIRED_DEVICE,
            compute_type=REQUIRED_COMPUTE_TYPE,
            num_workers=1,
            local_files_only=True,
        )
        effective = getattr(self._backend, "model", None)
        device = getattr(effective, "device", None)
        compute_type = getattr(effective, "compute_type", None)
        if device != REQUIRED_DEVICE or compute_type != REQUIRED_COMPUTE_TYPE:
            raise RuntimeError("WORKER_IDENTITY_MISMATCH")
        self.ready_at = time.time()
        self.last_probe_at: float | None = None
        if startup_probe:
            self.probe()

    def _transcribe(self, audio: np.ndarray) -> TranscriptionResult:
        segments, _ = self._backend.transcribe(
            audio,
            language="en",
            task="transcribe",
            beam_size=5,
            condition_on_previous_text=False,
            hotwords=DIMON_HOTWORDS,
        )
        text_parts: list[str] = []
        quality: list[TranscriptionSegment] = []
        for segment in segments:
            text = str(segment.text).strip()
            if text:
                text_parts.append(text)
            try:
                avg_logprob = float(segment.avg_logprob)
                no_speech_prob = float(segment.no_speech_prob)
            except (TypeError, ValueError):
                continue
            if math.isfinite(avg_logprob) and math.isfinite(no_speech_prob):
                quality.append(TranscriptionSegment(avg_logprob, no_speech_prob))
        return TranscriptionResult(" ".join(text_parts), tuple(quality))

    def probe(self) -> None:
        self._transcribe(np.zeros(4_000, dtype=np.float32))
        self.last_probe_at = time.time()

    def transcribe(self, audio: np.ndarray) -> TranscriptionResult:
        return self._transcribe(audio)


class TranscriberPort(Protocol):
    identity: dict[str, str]
    last_probe_at: float | None

    def probe(self) -> None: ...

    def transcribe(self, audio: np.ndarray) -> TranscriptionResult: ...


class WorkerState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_jobs = 0

    def try_begin(self) -> bool:
        with self._lock:
            if self._active_jobs:
                return False
            self._active_jobs = 1
            return True

    def finish(self) -> None:
        with self._lock:
            if self._active_jobs <= 0:
                raise RuntimeError("worker active job count is already zero")
            self._active_jobs = 0

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"active_jobs": self._active_jobs}


def create_worker_app(
    transcriber: TranscriberPort,
    *,
    max_audio_bytes: int = 8 * 1_048_576,
    state: WorkerState | None = None,
):
    worker_state = state or WorkerState()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.worker_state = worker_state
    ready_at = getattr(transcriber, "ready_at", time.time())

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ready",
            "ready": True,
            "loaded": True,
            **transcriber.identity,
            "pid": os.getpid(),
            "active_jobs": worker_state.snapshot()["active_jobs"],
            "ready_at": ready_at,
            "last_probe_at": transcriber.last_probe_at,
        }

    @app.post("/probe")
    async def probe() -> dict[str, object]:
        if not worker_state.try_begin():
            return {"ok": False, "reason_code": "WORKER_BUSY"}
        try:
            await asyncio.to_thread(transcriber.probe)
            return {"ok": True, "last_probe_at": transcriber.last_probe_at}
        finally:
            worker_state.finish()

    @app.websocket("/ws")
    async def transcribe(websocket: WebSocket) -> None:
        await websocket.accept()
        if not worker_state.try_begin():
            await websocket.close(code=1013)
            return
        payload = AudioAccumulator(max_audio_bytes)
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                chunk = message.get("bytes")
                if isinstance(chunk, bytes):
                    try:
                        payload.append(chunk)
                    except OverflowError:
                        await websocket.close(code=1009)
                        return
                    continue
                if message.get("text") != "done":
                    await websocket.close(code=1008)
                    return
                try:
                    audio = payload.decode()
                except ValueError:
                    await websocket.close(code=1008)
                    return
                try:
                    result = await asyncio.to_thread(transcriber.transcribe, audio)
                except Exception:
                    await websocket.close(code=1011)
                    return
                await websocket.send_json(
                    {
                        "text": result.text,
                        "segments": [
                            {
                                "avg_logprob": segment.avg_logprob,
                                "no_speech_prob": segment.no_speech_prob,
                            }
                            for segment in result.segments
                        ],
                    }
                )
                return
        except WebSocketDisconnect:
            return
        finally:
            worker_state.finish()

    return app


def main() -> None:
    settings = WorkerSettings.from_env()
    engine = CudaMediumEngine(settings.model_path)
    import uvicorn

    uvicorn.run(
        create_worker_app(engine, max_audio_bytes=settings.max_audio_bytes),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()

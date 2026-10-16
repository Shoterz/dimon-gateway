"""Utterance-scoped streaming transcription with bounded startup audio."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .voice import VoiceError, VoiceLimitError


class LiveTranscriptionSession:
    def __init__(
        self,
        recognizer: Any,
        lease: Any,
        on_partial: Callable[[str], Awaitable[None]],
        *,
        acquire_timeout: float,
        max_pending_bytes: int,
        trace_id: str | None = None,
        attempt_id: str | None = None,
        on_stream_ready: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        if acquire_timeout <= 0 or max_pending_bytes <= 0:
            raise ValueError("live transcription limits must be positive")
        self.recognizer = recognizer
        self.lease = lease
        self.on_partial = on_partial
        self.acquire_timeout = float(acquire_timeout)
        self.max_pending_bytes = int(max_pending_bytes)
        self.trace_id = trace_id
        self.attempt_id = attempt_id
        self.on_stream_ready = on_stream_ready
        self.nemotron_state: str | None = None
        self.nemotron_failure_phase: str | None = None
        self.recognition_path: str | None = None
        self.transcript_disagreement = False
        self.faster_whisper_avg_logprob: float | None = None
        self.faster_whisper_no_speech_prob: float | None = None
        self.faster_whisper_confidence: str | None = None
        self.stream: Any = None
        self._open_task: asyncio.Task[None] | None = None
        self._pending: list[bytes] = []
        self._pending_bytes = 0
        self._lock = asyncio.Lock()
        self._leased = False
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise VoiceError("STREAM_CLOSED")
        if self._open_task is None:
            self._open_task = asyncio.create_task(self._open())
            await asyncio.sleep(0)

    async def _open(self) -> None:
        deadline = time.monotonic() + self.acquire_timeout
        try:
            while not await self.lease.acquire():
                if time.monotonic() >= deadline:
                    raise VoiceError("VOICE_BUSY")
                await asyncio.sleep(0.05)
            self._leased = True
            open_stream = self.recognizer.open_stream
            parameters = inspect.signature(open_stream).parameters.values()
            supports_keywords = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
            names = {parameter.name for parameter in parameters}
            trace_fields = {}
            if supports_keywords or "trace_id" in names:
                trace_fields["trace_id"] = self.trace_id
            if supports_keywords or "attempt_id" in names:
                trace_fields["attempt_id"] = self.attempt_id
            stream = await open_stream(self.on_partial, **trace_fields)
            self._capture_stream_metadata(stream)
            if self.on_stream_ready is not None:
                await self.on_stream_ready(stream)
            async with self._lock:
                if self._closed:
                    await stream.close()
                    return
                self.stream = stream
                pending, self._pending = self._pending, []
                self._pending_bytes = 0
                for chunk in pending:
                    await stream.push_audio(chunk)
        except BaseException:
            self._release_lease()
            raise

    async def push(self, audio: bytes) -> None:
        if not isinstance(audio, bytes) or not audio:
            raise VoiceLimitError("MALFORMED_AUDIO")
        async with self._lock:
            if self._closed:
                raise VoiceError("STREAM_CLOSED")
            if self.stream is not None:
                await self.stream.push_audio(audio)
                return
            if self._pending_bytes + len(audio) > self.max_pending_bytes:
                raise VoiceLimitError("AUDIO_LIMIT_EXCEEDED")
            self._pending.append(audio)
            self._pending_bytes += len(audio)

    async def finish(self) -> str:
        await self.start()
        try:
            assert self._open_task is not None
            await self._open_task
            if self.stream is None:
                raise VoiceError("STREAM_CLOSED")
            result = (await self.stream.finish()).strip()
            self._capture_stream_metadata(self.stream)
            return result
        finally:
            await self._cleanup()

    async def cancel(self) -> None:
        if self._open_task is not None and not self._open_task.done():
            self._open_task.cancel()
            await asyncio.gather(self._open_task, return_exceptions=True)
        elif self._open_task is not None:
            with contextlib.suppress(BaseException):
                await self._open_task
        await self._cleanup()

    async def _cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        stream, self.stream = self.stream, None
        self._pending.clear()
        self._pending_bytes = 0
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.close()
        self._release_lease()

    def _release_lease(self) -> None:
        if not self._leased:
            return
        self._leased = False
        self.lease.release()

    def _capture_stream_metadata(self, stream: Any) -> None:
        state = getattr(stream, "nemotron_state", None)
        phase = getattr(stream, "nemotron_failure_phase", None)
        if state in {"READY", "FAILED"}:
            self.nemotron_state = state
        if isinstance(phase, str) and phase.startswith("nemotron_"):
            self.nemotron_failure_phase = phase
        path = getattr(stream, "recognition_path", None)
        if path in {"nemotron_final", "faster_whisper_final"}:
            self.recognition_path = path
        disagreement = getattr(stream, "transcript_disagreement", None)
        if isinstance(disagreement, bool):
            self.transcript_disagreement = disagreement
        for field in (
            "faster_whisper_avg_logprob",
            "faster_whisper_no_speech_prob",
        ):
            value = getattr(stream, field, None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(self, field, float(value))
        confidence = getattr(stream, "faster_whisper_confidence", None)
        if confidence in {"normal", "low"}:
            self.faster_whisper_confidence = confidence

"""Primary-first speech adapters with a single outage-only TRTC attempt."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .speech import result_summary
from .voice import VoiceLimitError


logger = logging.getLogger(__name__)

_AVAILABILITY_REASON_CODES = frozenset(
    {
        "UPSTREAM_TIMEOUT",
        "UPSTREAM_UNAVAILABLE",
        "WHISPER_UNAVAILABLE",
        "NEMOTRON_UNAVAILABLE",
        "KOKORO_UNAVAILABLE",
    }
)
_AVAILABILITY_HTTP_STATUSES = frozenset({502, 503, 504})


def _exception_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        next_error = current.__cause__
        if next_error is None and not current.__suppress_context__:
            next_error = current.__context__
        current = next_error


def is_provider_unavailable(exc: BaseException) -> bool:
    """Return true only for connection, timeout, and upstream outage failures."""

    for error in _exception_chain(exc):
        if getattr(error, "reason_code", None) in _AVAILABILITY_REASON_CODES:
            return True
        if isinstance(error, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
            return True
        if isinstance(error, (httpx.ConnectError, httpx.TimeoutException)):
            return True
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code in _AVAILABILITY_HTTP_STATUSES
    return False


def _reason(exc: BaseException) -> str:
    return str(getattr(exc, "reason_code", type(exc).__name__))


class PrimaryFallbackRecognizer:
    def __init__(self, primary: Any, backup: Any, max_audio_bytes: int):
        if max_audio_bytes <= 0:
            raise ValueError("max_audio_bytes must be positive")
        self.primary = primary
        self.backup = backup
        self.max_audio_bytes = int(max_audio_bytes)

    async def transcribe(
        self,
        audio: bytes,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> str:
        if len(audio) > self.max_audio_bytes:
            raise VoiceLimitError("AUDIO_LIMIT_EXCEEDED")
        try:
            transcript = await self.primary.transcribe(audio, on_partial)
            logger.info("speech_recognition_provider_selected provider=primary")
            return transcript
        except Exception as primary_error:
            if not is_provider_unavailable(primary_error):
                raise
            logger.warning(
                "speech_recognition_primary_unavailable reason_code=%s",
                _reason(primary_error),
            )
            try:
                transcript = await self.backup.transcribe(audio, on_partial)
                logger.info("speech_recognition_provider_selected provider=trtc_backup")
                return transcript
            except Exception as backup_error:
                logger.warning(
                    "speech_recognition_backup_unavailable primary_reason_code=%s backup_reason_code=%s",
                    _reason(primary_error),
                    _reason(backup_error),
                )
                raise primary_error from backup_error

    async def open_stream(
        self,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> "PrimaryFallbackRecognizerStream":
        try:
            primary_stream = await self.primary.open_stream(on_partial)
        except Exception as primary_error:
            if not is_provider_unavailable(primary_error):
                raise
            logger.warning(
                "speech_recognition_primary_unavailable reason_code=%s stage=open",
                _reason(primary_error),
            )
            try:
                backup_stream = await self.backup.open_stream(on_partial)
            except Exception as backup_error:
                logger.warning(
                    "speech_recognition_backup_unavailable primary_reason_code=%s backup_reason_code=%s stage=open",
                    _reason(primary_error),
                    _reason(backup_error),
                )
                raise primary_error from backup_error
            return PrimaryFallbackRecognizerStream(
                backup=self.backup,
                on_partial=on_partial,
                max_audio_bytes=self.max_audio_bytes,
                backup_stream=backup_stream,
                primary_error=primary_error,
            )
        return PrimaryFallbackRecognizerStream(
            backup=self.backup,
            on_partial=on_partial,
            max_audio_bytes=self.max_audio_bytes,
            primary_stream=primary_stream,
        )


class PrimaryFallbackRecognizerStream:
    def __init__(
        self,
        *,
        backup: Any,
        on_partial: Callable[[str], Awaitable[None]],
        max_audio_bytes: int,
        primary_stream: Any | None = None,
        backup_stream: Any | None = None,
        primary_error: Exception | None = None,
    ):
        self.backup = backup
        self.on_partial = on_partial
        self.max_audio_bytes = max_audio_bytes
        self.primary_stream = primary_stream
        self.backup_stream = backup_stream
        self.primary_error = primary_error
        self.audio = bytearray()
        self.closed = False

    async def push_audio(self, audio: bytes) -> None:
        if self.closed:
            raise RuntimeError("speech stream is closed")
        if len(self.audio) + len(audio) > self.max_audio_bytes:
            raise VoiceLimitError("AUDIO_LIMIT_EXCEEDED")
        self.audio.extend(audio)
        if self.backup_stream is not None:
            try:
                await self.backup_stream.push_audio(audio)
            except Exception as backup_error:
                if self.primary_error is not None:
                    raise self.primary_error from backup_error
                raise
            return
        try:
            await self.primary_stream.push_audio(audio)
        except Exception as primary_error:
            if not is_provider_unavailable(primary_error):
                raise
            await self._activate_backup(primary_error)

    async def finish(self) -> str:
        if self.closed:
            raise RuntimeError("speech stream is closed")
        if self.backup_stream is not None:
            try:
                transcript = await self.backup_stream.finish()
                logger.info("speech_recognition_provider_selected provider=trtc_backup")
                return transcript
            except Exception as backup_error:
                if self.primary_error is not None:
                    raise self.primary_error from backup_error
                raise
            finally:
                self.closed = True
        try:
            transcript = await self.primary_stream.finish()
            logger.info("speech_recognition_provider_selected provider=primary")
            return transcript
        except Exception as primary_error:
            if not is_provider_unavailable(primary_error):
                raise
            await self._activate_backup(primary_error)
            try:
                transcript = await self.backup_stream.finish()
                logger.info("speech_recognition_provider_selected provider=trtc_backup")
                return transcript
            except Exception as backup_error:
                raise primary_error from backup_error
        finally:
            self.closed = True

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for stream in (self.primary_stream, self.backup_stream):
            close = getattr(stream, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    await close()

    async def _activate_backup(self, primary_error: Exception) -> None:
        self.primary_error = primary_error
        logger.warning(
            "speech_recognition_primary_unavailable reason_code=%s stage=stream",
            _reason(primary_error),
        )
        close = getattr(self.primary_stream, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                await close()
        try:
            self.backup_stream = await self.backup.open_stream(self.on_partial)
            if self.audio:
                await self.backup_stream.push_audio(bytes(self.audio))
        except Exception as backup_error:
            logger.warning(
                "speech_recognition_backup_unavailable primary_reason_code=%s backup_reason_code=%s stage=stream",
                _reason(primary_error),
                _reason(backup_error),
            )
            raise primary_error from backup_error


class PrimaryFallbackSynthesizer:
    def __init__(self, primary: Any, backup: Any):
        self.primary = primary
        self.backup = backup

    async def __call__(self, text: str) -> bytes:
        return await self._call_primary(lambda: self.primary(text), text)

    async def synthesize_result(self, result: dict[str, Any]) -> bytes:
        synthesize_result = getattr(self.primary, "synthesize_result", None)
        if callable(synthesize_result):
            primary_call = lambda: synthesize_result(result)
        else:
            text = result_summary(result)
            if text is None:
                return await self.primary("")
            primary_call = lambda: self.primary(text)
        try:
            data = await primary_call()
            logger.info("speech_synthesis_provider_selected provider=primary")
            return data
        except Exception as primary_error:
            if not is_provider_unavailable(primary_error):
                raise
            text = result_summary(result)
            if text is None:
                raise primary_error
            return await self._call_backup(primary_error, text)

    async def _call_primary(self, call, backup_text: str) -> bytes:
        try:
            data = await call()
            logger.info("speech_synthesis_provider_selected provider=primary")
            return data
        except Exception as primary_error:
            if not is_provider_unavailable(primary_error):
                raise
            return await self._call_backup(primary_error, backup_text)

    async def _call_backup(self, primary_error: Exception, text: str) -> bytes:
        logger.warning(
            "speech_synthesis_primary_unavailable reason_code=%s",
            _reason(primary_error),
        )
        try:
            data = await self.backup(text)
            logger.info("speech_synthesis_provider_selected provider=trtc_backup")
            return data
        except Exception as backup_error:
            logger.warning(
                "speech_synthesis_backup_unavailable primary_reason_code=%s backup_reason_code=%s",
                _reason(primary_error),
                _reason(backup_error),
            )
            raise primary_error from backup_error

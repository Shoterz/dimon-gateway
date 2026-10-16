"""Authenticated, bounded project protocol for streaming transcription."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from starlette.websockets import WebSocketDisconnect

from apps.dimon_live.inference import VoiceLifecycleLease
from apps.dimon_live.model_adapter import InferenceFailure
from apps.dimon_live.voice import (
    NemotronClient,
    NemotronLifecycleClient,
    VoiceLease,
    nemotron_failure_phase,
)

from .auth import authenticate_service_key
from .errors import GatewayError
from .limits import SlidingWindowLimiter

logger = logging.getLogger(__name__)


class TranscriptionStream(Protocol):
    async def push_audio(self, audio: bytes) -> None: ...
    async def finish(self) -> str: ...
    async def close(self) -> None: ...


class TranscriptionRecognizer(Protocol):
    async def open_stream(
        self,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> TranscriptionStream: ...


async def _ignore_partial(_text: str) -> None:
    return None


class DualTranscriptionRecognizer:
    """Use Nemotron as final truth with Faster-Whisper as a measured fallback."""

    def __init__(
        self,
        fallback: TranscriptionRecognizer,
        primary: TranscriptionRecognizer,
        *,
        caption_open_attempts: int = 2,
        caption_retry_delay: float = 0.2,
        stream_push_timeout: float = 0.5,
        comparison_grace_timeout: float = 0.5,
    ):
        if (
            caption_open_attempts <= 0
            or caption_retry_delay < 0
            or stream_push_timeout <= 0
            or comparison_grace_timeout < 0
        ):
            raise ValueError("invalid caption retry settings")
        self.fallback = fallback
        self.primary = primary
        self.caption_open_attempts = int(caption_open_attempts)
        self.caption_retry_delay = float(caption_retry_delay)
        self.stream_push_timeout = float(stream_push_timeout)
        self.comparison_grace_timeout = float(comparison_grace_timeout)

    async def open_stream(
        self,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> TranscriptionStream:
        fallback = None
        fallback_error: BaseException | None = None
        try:
            fallback = await self.fallback.open_stream(_ignore_partial)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # primary may still provide a valid result
            fallback_error = exc
            logger.warning("faster_whisper_open_failed", exc_info=True)
        primary = None
        primary_failure_phase = None
        try:
            for attempt in range(1, self.caption_open_attempts + 1):
                try:
                    primary = await self.primary.open_stream(on_partial)
                    break
                except Exception as exc:  # noqa: BLE001 - fallback remains available
                    phase = nemotron_failure_phase(getattr(exc, "failure_stage", "connect"))
                    primary_failure_phase = f"nemotron_{phase}"
                    logger.warning(
                        "nemotron_primary_open_failed phase=%s attempt=%d max_attempts=%d",
                        phase, attempt, self.caption_open_attempts,
                    )
                    if attempt < self.caption_open_attempts and self.caption_retry_delay:
                        await asyncio.sleep(self.caption_retry_delay)
        except asyncio.CancelledError:
            if fallback is not None:
                await fallback.close()
            raise
        if primary is None and fallback is None:
            assert fallback_error is not None
            raise fallback_error
        return _DualTranscriptionStream(
            fallback,
            primary,
            fallback_error=fallback_error,
            nemotron_failure_phase=primary_failure_phase,
            stream_push_timeout=self.stream_push_timeout,
            comparison_grace_timeout=self.comparison_grace_timeout,
        )


class _DualTranscriptionStream:
    def __init__(
        self,
        fallback: TranscriptionStream | None,
        primary: TranscriptionStream | None,
        *,
        fallback_error: BaseException | None = None,
        nemotron_failure_phase: str | None = None,
        stream_push_timeout: float = 0.5,
        comparison_grace_timeout: float = 0.5,
    ):
        self.fallback = fallback
        self.primary = primary
        self.fallback_error = fallback_error
        self.nemotron_state = "READY" if primary is not None else "FAILED"
        self.nemotron_failure_phase = nemotron_failure_phase
        self.recognition_path: str | None = None
        self.transcript_disagreement = False
        self.faster_whisper_avg_logprob: float | None = None
        self.faster_whisper_no_speech_prob: float | None = None
        self.faster_whisper_confidence: str | None = None
        self.stream_push_timeout = float(stream_push_timeout)
        self.comparison_grace_timeout = float(comparison_grace_timeout)
        self.closed = False

    async def _drop_primary(self, failure_phase: str | None = None) -> None:
        if failure_phase is not None:
            self.nemotron_state = "FAILED"
            self.nemotron_failure_phase = failure_phase
        primary, self.primary = self.primary, None
        if primary is not None:
            with contextlib.suppress(Exception):
                await primary.close()

    async def _drop_fallback(self, error: BaseException | None = None) -> None:
        if error is not None:
            self.fallback_error = error
        fallback, self.fallback = self.fallback, None
        if fallback is not None:
            with contextlib.suppress(Exception):
                await fallback.close()

    async def push_audio(self, audio: bytes) -> None:
        fallback_push = (
            asyncio.create_task(self.fallback.push_audio(audio))
            if self.fallback is not None
            else None
        )
        primary_push = (
            asyncio.create_task(self.primary.push_audio(audio))
            if self.primary is not None
            else None
        )
        if fallback_push is not None:
            try:
                await asyncio.wait_for(
                    fallback_push, timeout=self.stream_push_timeout
                )
            except (asyncio.TimeoutError, TimeoutError) as exc:
                logger.warning("Faster-Whisper fallback stream timed out")
                await self._drop_fallback(exc)
            except Exception as exc:  # primary remains authoritative
                logger.warning("Faster-Whisper fallback stream failed", exc_info=True)
                await self._drop_fallback(exc)
        if primary_push is not None:
            try:
                await asyncio.wait_for(
                    primary_push, timeout=self.stream_push_timeout
                )
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning("Nemotron primary stream timed out")
                await self._drop_primary("nemotron_stream_closed")
            except Exception:
                logger.warning("Nemotron primary stream failed", exc_info=True)
                await self._drop_primary("nemotron_stream_closed")
        if self.primary is None and self.fallback is None:
            assert self.fallback_error is not None
            raise self.fallback_error

    async def finish(self) -> str:
        primary_stream = self.primary
        fallback_stream = self.fallback
        primary_finish = asyncio.create_task(primary_stream.finish()) if primary_stream else None
        fallback_finish = asyncio.create_task(fallback_stream.finish()) if fallback_stream else None
        try:
            primary_result: str | BaseException | None = None
            fallback_result: str | BaseException | None = self.fallback_error
            if primary_finish is not None:
                try:
                    primary_result = await primary_finish
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # inspected below; fallback may succeed
                    primary_result = exc

            primary_text = (
                primary_result.strip()
                if isinstance(primary_result, str) and primary_result.strip()
                else None
            )
            if fallback_finish is not None:
                try:
                    if primary_text:
                        fallback_result = await asyncio.wait_for(
                            fallback_finish,
                            timeout=self.comparison_grace_timeout,
                        )
                    else:
                        fallback_result = await fallback_finish
                except (asyncio.TimeoutError, TimeoutError):
                    fallback_result = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    fallback_result = exc
            fallback_text = (
                fallback_result.strip()
                if isinstance(fallback_result, str) and fallback_result.strip()
                else None
            )
            if fallback_stream is not None:
                self.faster_whisper_avg_logprob = getattr(
                    fallback_stream, "avg_logprob", None
                )
                self.faster_whisper_no_speech_prob = getattr(
                    fallback_stream, "no_speech_prob", None
                )
                self.faster_whisper_confidence = getattr(
                    fallback_stream, "confidence", None
                )
            if primary_text and fallback_text:
                normalize = lambda value: re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
                self.transcript_disagreement = normalize(primary_text) != normalize(fallback_text)
            if primary_text:
                self.nemotron_state = "READY"
                self.nemotron_failure_phase = None
                self.recognition_path = "nemotron_final"
                return primary_text
            self.nemotron_state = "FAILED"
            if self.nemotron_failure_phase is None:
                if isinstance(primary_result, BaseException):
                    phase = nemotron_failure_phase(
                        getattr(primary_result, "failure_stage", "")
                    )
                    self.nemotron_failure_phase = f"nemotron_{phase}"
                else:
                    self.nemotron_failure_phase = "nemotron_empty_final"
            if fallback_text:
                self.recognition_path = "faster_whisper_final"
                return fallback_text
            for result in (fallback_result, primary_result):
                if isinstance(result, BaseException):
                    raise result
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "no recognizer returned a transcript")
        finally:
            pending = [
                task for task in (primary_finish, fallback_finish)
                if task is not None and not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await self.close()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._drop_primary()
        await self._drop_fallback()


def pcm16le_to_float32le(audio: bytes) -> bytes:
    if not audio or len(audio) % 2:
        raise GatewayError(
            "INVALID_INFERENCE_REQUEST",
            "audio must be non-empty PCM16 frames",
            422,
        )
    converted = bytearray()
    for (sample,) in struct.iter_unpack("<h", audio):
        converted.extend(struct.pack("<f", sample / 32768.0))
    return bytes(converted)


class _NemotronPcm16Stream:
    def __init__(
        self,
        stream: Any,
        lifecycle_lease: VoiceLifecycleLease | None = None,
    ):
        self.stream = stream
        self.lifecycle_lease = lifecycle_lease
        self.finished = False
        self.disconnected = False
        self.closed = False

    async def push_audio(self, audio: bytes) -> None:
        try:
            await self.stream.push_audio(pcm16le_to_float32le(audio))
        except BaseException:
            self.disconnected = True
            raise

    async def finish(self) -> str:
        try:
            transcript = await self.stream.finish()
        except BaseException:
            self.disconnected = True
            raise
        else:
            self.finished = True
            return transcript

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.stream.close()
        finally:
            if self.lifecycle_lease is not None:
                await self.lifecycle_lease.release(disconnected=self.disconnected)


class NemotronPcm16Recognizer:
    def __init__(self, url: str, *, control_socket: Path | None = None):
        self.client = NemotronClient(url)
        self.lifecycle = (
            NemotronLifecycleClient(control_socket, timeout=120)
            if control_socket is not None
            else None
        )

    async def open_stream(
        self,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> _NemotronPcm16Stream:
        lifecycle_lease = None
        if self.lifecycle is not None:
            lifecycle_lease = await self.lifecycle.acquire()
        try:
            stream = await self.client.open_stream(on_partial)
        except BaseException:
            if lifecycle_lease is not None:
                with contextlib.suppress(Exception):
                    await lifecycle_lease.release(disconnected=True)
            raise
        return _NemotronPcm16Stream(stream, lifecycle_lease)


class FasterWhisperPcm16Recognizer:
    """Adapter for the existing Jarvis Faster-Whisper worker."""

    def __init__(
        self,
        url: str,
        *,
        websocket_connect=None,
        connect_timeout: float = 3.0,
        result_timeout: float = 60.0,
    ):
        self.url = url
        self.websocket_connect = websocket_connect
        self.connect_timeout = float(connect_timeout)
        self.result_timeout = float(result_timeout)

    async def open_stream(self, on_partial):
        del on_partial
        connector = self.websocket_connect
        if connector is None:
            from websockets.asyncio.client import connect

            connector = connect
        try:
            connection = connector(
                self.url,
                open_timeout=self.connect_timeout,
                close_timeout=0.2,
                ping_interval=None,
                max_size=1_048_576,
                proxy=None,
            )
            websocket = await asyncio.wait_for(
                connection.__aenter__(),
                timeout=self.connect_timeout,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise InferenceFailure(
                "UPSTREAM_TIMEOUT",
                "Faster-Whisper connection timed out",
            ) from exc
        except Exception as exc:
            raise InferenceFailure(
                "UPSTREAM_UNAVAILABLE",
                "Faster-Whisper service is unavailable",
            ) from exc
        return _FasterWhisperPcm16Stream(
            connection,
            websocket,
            result_timeout=self.result_timeout,
        )


class _FasterWhisperPcm16Stream:
    def __init__(self, connection, websocket, *, result_timeout: float):
        self.connection = connection
        self.websocket = websocket
        self.result_timeout = result_timeout
        self.avg_logprob: float | None = None
        self.no_speech_prob: float | None = None
        self.confidence: str | None = None
        self.closed = False

    async def push_audio(self, audio: bytes) -> None:
        try:
            await self.websocket.send(pcm16le_to_float32le(audio))
        except Exception as exc:
            raise InferenceFailure(
                "UPSTREAM_UNAVAILABLE",
                "Faster-Whisper audio stream failed",
            ) from exc

    async def finish(self) -> str:
        try:
            await self.websocket.send("done")
            response = await asyncio.wait_for(
                self.websocket.recv(),
                timeout=self.result_timeout,
            )
            result = _decode_faster_whisper_result(response)
            self.avg_logprob = result.avg_logprob
            self.no_speech_prob = result.no_speech_prob
            self.confidence = result.confidence
            return result.text
        except InferenceFailure:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise InferenceFailure(
                "UPSTREAM_TIMEOUT",
                "Faster-Whisper result timed out",
            ) from exc
        except Exception as exc:
            raise InferenceFailure(
                "UPSTREAM_UNAVAILABLE",
                "Faster-Whisper result is unavailable",
            ) from exc
        finally:
            await self.close()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            await self.connection.__aexit__(None, None, None)


@dataclass(frozen=True)
class _FasterWhisperResult:
    text: str
    avg_logprob: float | None
    no_speech_prob: float | None
    confidence: str


def _decode_faster_whisper_result(response: object) -> _FasterWhisperResult:
    if not isinstance(response, str):
        raise InferenceFailure(
            "UPSTREAM_UNAVAILABLE",
            "Faster-Whisper returned an invalid result",
        )
    try:
        payload = json.loads(response)
    except json.JSONDecodeError as exc:
        raise InferenceFailure(
            "UPSTREAM_UNAVAILABLE",
            "Faster-Whisper returned an invalid result",
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {"text", "segments"}:
        raise InferenceFailure(
            "UPSTREAM_UNAVAILABLE",
            "Faster-Whisper returned an invalid result",
        )
    text = payload["text"]
    segments = payload["segments"]
    if not isinstance(text, str) or not isinstance(segments, list) or len(segments) > 1024:
        raise InferenceFailure(
            "UPSTREAM_UNAVAILABLE",
            "Faster-Whisper returned an invalid result",
        )
    avg_logprobs: list[float] = []
    no_speech_probs: list[float] = []
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) != {
            "avg_logprob",
            "no_speech_prob",
        }:
            raise InferenceFailure(
                "UPSTREAM_UNAVAILABLE",
                "Faster-Whisper returned an invalid result",
            )
        for value in segment.values():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InferenceFailure(
                    "UPSTREAM_UNAVAILABLE",
                    "Faster-Whisper returned an invalid result",
                )
            if not math.isfinite(float(value)):
                raise InferenceFailure(
                    "UPSTREAM_UNAVAILABLE",
                    "Faster-Whisper returned an invalid result",
                )
        avg_logprobs.append(float(segment["avg_logprob"]))
        no_speech_probs.append(float(segment["no_speech_prob"]))
    avg_logprob = min(avg_logprobs) if avg_logprobs else None
    no_speech_prob = max(no_speech_probs) if no_speech_probs else None
    confidence = (
        "low"
        if (
            avg_logprob is None
            or no_speech_prob is None
            or avg_logprob <= -0.45
            or no_speech_prob >= 0.35
        )
        else "normal"
    )
    return _FasterWhisperResult(
        text=text.replace("\x00", "").strip(),
        avg_logprob=avg_logprob,
        no_speech_prob=no_speech_prob,
        confidence=confidence,
    )


class TranscriptionGateway:
    def __init__(
        self,
        *,
        service_key: str,
        recognizer: TranscriptionRecognizer,
        lease: VoiceLease | None = None,
        limiter: SlidingWindowLimiter | None = None,
        max_audio_bytes: int = 4 * 1_048_576,
        max_duration_seconds: float = 30,
        client_idle_timeout_seconds: float = 5,
        max_event_bytes: int = 1024,
        max_transcript_bytes: int = 8192,
    ):
        if min(max_audio_bytes, max_event_bytes, max_transcript_bytes) <= 0:
            raise ValueError("transcription byte limits must be positive")
        if max_duration_seconds <= 0 or client_idle_timeout_seconds <= 0:
            raise ValueError(
                "transcription duration and idle timeout must be positive"
            )
        self.service_key = service_key
        self.recognizer = recognizer
        self.lease = lease or VoiceLease()
        self.limiter = limiter or SlidingWindowLimiter(6, 60)
        self.max_audio_bytes = int(max_audio_bytes)
        self.max_duration_seconds = float(max_duration_seconds)
        self.client_idle_timeout_seconds = float(client_idle_timeout_seconds)
        self.max_event_bytes = int(max_event_bytes)
        self.max_transcript_bytes = int(max_transcript_bytes)

    @staticmethod
    def _event(event_type: str, **fields: object) -> dict[str, object]:
        return {"type": event_type, "protocol_version": 1, **fields}

    async def _send_error(self, websocket, code: str) -> None:
        await websocket.send_json(self._event("error", code=code))

    async def handle(self, websocket) -> None:
        try:
            identity = authenticate_service_key(
                websocket.headers.get("authorization"), self.service_key
            )
        except GatewayError as exc:
            setattr(websocket, "_dimon_outcome_code", exc.code)
            await websocket.close(code=4401)
            return

        setattr(websocket, "_dimon_identity", identity)
        setattr(
            websocket,
            "_dimon_trace_id",
            self._optional_uuid4(websocket.headers.get("x-dimon-trace-id")),
        )
        setattr(
            websocket,
            "_dimon_attempt_id",
            self._optional_uuid4(websocket.headers.get("x-dimon-attempt-id")),
        )

        if self.limiter.check(identity.subject) is not None:
            setattr(websocket, "_dimon_outcome_code", "RATE_LIMITED")
            await websocket.close(code=4429)
            return
        if not await self.lease.acquire():
            setattr(websocket, "_dimon_outcome_code", "VOICE_BUSY")
            await websocket.accept()
            await self._send_error(websocket, "VOICE_BUSY")
            await websocket.close(code=1013)
            return

        stream: TranscriptionStream | None = None
        disconnected = False
        stage = "transcription_start"
        try:
            await websocket.accept()

            async def partial(text: str) -> None:
                if (
                    not isinstance(text, str)
                    or not text
                    or len(text.encode("utf-8")) > self.max_transcript_bytes
                ):
                    raise GatewayError(
                        "UPSTREAM_UNAVAILABLE",
                        "transcription upstream returned an invalid partial",
                        503,
                    )
                await websocket.send_json(self._event("transcript.partial", text=text))

            stage = "nemotron_stream_open"
            stream = await self.recognizer.open_stream(partial)
            stage = "audio_receive"
            await websocket.send_json(
                self._event(
                    "session.ready",
                    sample_rate=16_000,
                    encoding="pcm_s16le",
                    nemotron_state=getattr(stream, "nemotron_state", "READY"),
                    nemotron_failure_phase=getattr(stream, "nemotron_failure_phase", None),
                )
            )
            total = 0
            while True:
                try:
                    message = await asyncio.wait_for(
                        websocket.receive(),
                        timeout=self.client_idle_timeout_seconds,
                    )
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    raise GatewayError(
                        "UPSTREAM_TIMEOUT",
                        "transcription client stopped sending audio",
                        504,
                    ) from exc
                if message.get("type") == "websocket.disconnect":
                    disconnected = True
                    return
                audio = message.get("bytes")
                if audio is not None:
                    if not isinstance(audio, bytes) or not audio or len(audio) % 2:
                        raise GatewayError(
                            "INVALID_INFERENCE_REQUEST",
                            "audio must be non-empty PCM16 frames",
                            422,
                        )
                    total += len(audio)
                    if total > self.max_audio_bytes:
                        raise GatewayError(
                            "AUDIO_LIMIT_EXCEEDED",
                            "audio exceeds the byte limit",
                            422,
                        )
                    if total / 2 / 16_000 > self.max_duration_seconds:
                        raise GatewayError(
                            "UTTERANCE_LIMIT_EXCEEDED",
                            "audio exceeds the duration limit",
                            422,
                        )
                    stage = "nemotron_audio_push"
                    await stream.push_audio(audio)
                    stage = "audio_receive"
                    continue
                text = message.get("text")
                if not isinstance(text, str) or len(text.encode("utf-8")) > self.max_event_bytes:
                    raise GatewayError(
                        "INVALID_INFERENCE_REQUEST",
                        "transcription event is invalid",
                        422,
                    )
                try:
                    event = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise GatewayError(
                        "INVALID_INFERENCE_REQUEST",
                        "transcription event is invalid",
                        422,
                    ) from exc
                if event != {"type": "input.commit"}:
                    raise GatewayError(
                        "INVALID_INFERENCE_REQUEST",
                        "transcription event is invalid",
                        422,
                    )
                break

            stage = "nemotron_transcription_finish"
            transcript = await stream.finish()
            if (
                not isinstance(transcript, str)
                or not transcript
                or len(transcript.encode("utf-8")) > self.max_transcript_bytes
            ):
                raise GatewayError(
                    "UPSTREAM_UNAVAILABLE",
                    "transcription upstream returned an invalid result",
                    503,
                )
            await websocket.send_json(self._event(
                "transcript.final",
                text=transcript,
                nemotron_state=getattr(stream, "nemotron_state", "READY"),
                nemotron_failure_phase=getattr(stream, "nemotron_failure_phase", None),
                recognition_path=getattr(stream, "recognition_path", None),
                transcript_disagreement=bool(
                    getattr(stream, "transcript_disagreement", False)
                ),
                faster_whisper_avg_logprob=getattr(
                    stream, "faster_whisper_avg_logprob", None
                ),
                faster_whisper_no_speech_prob=getattr(
                    stream, "faster_whisper_no_speech_prob", None
                ),
                faster_whisper_confidence=getattr(
                    stream, "faster_whisper_confidence", None
                ),
            ))
            setattr(websocket, "_dimon_outcome_code", "OK")
            await websocket.close(code=1000)
        except WebSocketDisconnect:
            disconnected = True
            setattr(websocket, "_dimon_outcome_code", "CLIENT_DISCONNECTED")
        except GatewayError as exc:
            setattr(websocket, "_dimon_outcome_code", exc.code)
            if exc.status_code >= 500:
                self._log_upstream_failure(websocket, stage, exc)
            await self._send_error(websocket, exc.code)
            await websocket.close(code=1003 if exc.status_code < 500 else 1011)
        except InferenceFailure as exc:
            code = exc.reason_code if exc.reason_code in {
                "UPSTREAM_TIMEOUT",
                "UPSTREAM_UNAVAILABLE",
            } else "UPSTREAM_UNAVAILABLE"
            setattr(websocket, "_dimon_outcome_code", code)
            self._log_upstream_failure(websocket, stage, exc)
            await self._send_error(websocket, code)
            await websocket.close(code=1011)
        except Exception as exc:
            setattr(websocket, "_dimon_outcome_code", "UPSTREAM_UNAVAILABLE")
            self._log_upstream_failure(websocket, stage, exc)
            await self._send_error(websocket, "UPSTREAM_UNAVAILABLE")
            await websocket.close(code=1011)
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    await stream.close()
            self.lease.release()

    @staticmethod
    def _log_upstream_failure(websocket, stage: str, exc: BaseException) -> None:
        logger.warning(json.dumps({
            "event": "transcription_upstream_failed",
            "correlation_id": getattr(websocket, "_dimon_correlation_id", None),
            "stage": stage,
            "upstream_stage": getattr(exc, "failure_stage", ""),
            "error_type": type(exc).__name__,
            "reason_code": getattr(exc, "reason_code", getattr(exc, "code", "UPSTREAM_UNAVAILABLE")),
        }, sort_keys=True, separators=(",", ":")))

    @staticmethod
    def _optional_uuid4(value: object) -> str | None:
        if not isinstance(value, str) or len(value) != 36:
            return None
        try:
            parsed = UUID(value)
        except ValueError:
            return None
        return value if parsed.version == 4 and str(parsed) == value else None

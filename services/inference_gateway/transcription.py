"""Authenticated, bounded project protocol for streaming transcription."""

from __future__ import annotations

import contextlib
import json
import struct
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from starlette.websockets import WebSocketDisconnect

from apps.dimon_live.model_adapter import InferenceFailure
from apps.dimon_live.voice import NemotronClient, VoiceLease

from .auth import AccessTokenVerifier
from .errors import GatewayError
from .limits import SlidingWindowLimiter


class TranscriptionStream(Protocol):
    async def push_audio(self, audio: bytes) -> None: ...
    async def finish(self) -> str: ...
    async def close(self) -> None: ...


class TranscriptionRecognizer(Protocol):
    async def open_stream(
        self,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> TranscriptionStream: ...


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
    def __init__(self, stream: Any):
        self.stream = stream

    async def push_audio(self, audio: bytes) -> None:
        await self.stream.push_audio(pcm16le_to_float32le(audio))

    async def finish(self) -> str:
        return await self.stream.finish()

    async def close(self) -> None:
        await self.stream.close()


class NemotronPcm16Recognizer:
    def __init__(self, url: str):
        self.client = NemotronClient(url)

    async def open_stream(
        self,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> _NemotronPcm16Stream:
        return _NemotronPcm16Stream(await self.client.open_stream(on_partial))


class TranscriptionGateway:
    def __init__(
        self,
        *,
        verifier: AccessTokenVerifier,
        recognizer: TranscriptionRecognizer,
        lease: VoiceLease | None = None,
        limiter: SlidingWindowLimiter | None = None,
        max_audio_bytes: int = 4 * 1_048_576,
        max_duration_seconds: float = 30,
        max_event_bytes: int = 1024,
        max_transcript_bytes: int = 8192,
    ):
        if min(max_audio_bytes, max_event_bytes, max_transcript_bytes) <= 0:
            raise ValueError("transcription byte limits must be positive")
        if max_duration_seconds <= 0:
            raise ValueError("transcription duration must be positive")
        self.verifier = verifier
        self.recognizer = recognizer
        self.lease = lease or VoiceLease()
        self.limiter = limiter or SlidingWindowLimiter(6, 60)
        self.max_audio_bytes = int(max_audio_bytes)
        self.max_duration_seconds = float(max_duration_seconds)
        self.max_event_bytes = int(max_event_bytes)
        self.max_transcript_bytes = int(max_transcript_bytes)

    @staticmethod
    def _event(event_type: str, **fields: object) -> dict[str, object]:
        return {"type": event_type, "protocol_version": 1, **fields}

    async def _send_error(self, websocket, code: str) -> None:
        await websocket.send_json(self._event("error", code=code))

    async def handle(self, websocket) -> None:
        peer = websocket.client.host if websocket.client is not None else None
        try:
            identity = self.verifier.verify(
                websocket.headers.get("cf-access-jwt-assertion"),
                peer,
            )
        except GatewayError as exc:
            await websocket.close(code=4401 if exc.status_code == 401 else 4403)
            return

        if self.limiter.check(identity.subject) is not None:
            await websocket.close(code=4429)
            return
        if not await self.lease.acquire():
            await websocket.accept()
            await self._send_error(websocket, "VOICE_BUSY")
            await websocket.close(code=1013)
            return

        stream: TranscriptionStream | None = None
        disconnected = False
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

            stream = await self.recognizer.open_stream(partial)
            await websocket.send_json(
                self._event("session.ready", sample_rate=16_000, encoding="pcm_s16le")
            )
            total = 0
            while True:
                message = await websocket.receive()
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
                    await stream.push_audio(audio)
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
            await websocket.send_json(self._event("transcript.final", text=transcript))
            await websocket.close(code=1000)
        except WebSocketDisconnect:
            disconnected = True
        except GatewayError as exc:
            await self._send_error(websocket, exc.code)
            await websocket.close(code=1003 if exc.status_code < 500 else 1011)
        except InferenceFailure as exc:
            code = exc.reason_code if exc.reason_code in {
                "UPSTREAM_TIMEOUT",
                "UPSTREAM_UNAVAILABLE",
            } else "UPSTREAM_UNAVAILABLE"
            await self._send_error(websocket, code)
            await websocket.close(code=1011)
        except Exception:
            await self._send_error(websocket, "UPSTREAM_UNAVAILABLE")
            await websocket.close(code=1011)
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    await stream.close()
            self.lease.release()

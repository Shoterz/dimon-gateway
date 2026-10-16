"""Loopback client for the repository-owned Intel Arc Whisper transcription service."""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

from .voice import VoiceError, float32le_to_pcm16


_WHISPER_AVAILABILITY_ERRORS = frozenset(
    {
        "MODEL_UNAVAILABLE",
        "TRANSCRIPTION_UNAVAILABLE",
        "UPSTREAM_UNAVAILABLE",
        "WHISPER_UNAVAILABLE",
    }
)
_WHISPER_REFUSAL_ERRORS = frozenset(
    {
        "AUDIO_LIMIT_EXCEEDED",
        "UTTERANCE_LIMIT_EXCEEDED",
        "MALFORMED_AUDIO",
        "INVALID_REQUEST",
        "INVALID_INFERENCE_REQUEST",
        "POLICY_REJECTED",
        "AUTHENTICATION_FAILED",
    }
)


class WhisperClient:
    def __init__(self, url: str, *, connect_timeout: float = 3.0, result_timeout: float = 30.0):
        self.url = url
        self.connect_timeout = connect_timeout
        self.result_timeout = result_timeout

    async def transcribe(self, audio: bytes, on_partial: Callable[[str], Awaitable[None]]) -> str:
        stream = await self.open_stream(on_partial)
        try:
            if audio:
                await stream.push_audio(audio)
            return await stream.finish()
        except Exception:
            await stream.close()
            raise

    async def open_stream(self, on_partial: Callable[[str], Awaitable[None]]) -> "WhisperStream":
        stream = WhisperStream(self, on_partial)
        await stream.open()
        return stream

    async def _connect(self):
        try:
            from websockets.asyncio.client import connect
            return connect(
                self.url,
                open_timeout=self.connect_timeout,
                close_timeout=0.2,
                ping_interval=None,
                max_size=1_048_576,
                proxy=None,
            )
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError("WHISPER_UNAVAILABLE") from exc

    async def _expect(self, websocket, expected: str) -> None:
        try:
            raw = await asyncio.wait_for(websocket.recv(), self.connect_timeout)
        except Exception as exc:
            raise VoiceError("WHISPER_UNAVAILABLE") from exc
        try:
            event = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise VoiceError("INVALID_MODEL_OUTPUT") from exc
        if not isinstance(event, dict) or event.get("type") != expected:
            raise VoiceError("INVALID_MODEL_OUTPUT")


class FasterWhisperClient:
    """Direct client for the existing Faster-Whisper worker's compact protocol."""

    def __init__(self, url: str, *, connect_timeout: float = 3.0, result_timeout: float = 15.0):
        self.url = url
        self.connect_timeout = connect_timeout
        self.result_timeout = result_timeout

    async def transcribe(self, audio: bytes, on_partial: Callable[[str], Awaitable[None]]) -> str:
        del on_partial
        try:
            from websockets.asyncio.client import connect

            connection = connect(
                self.url,
                open_timeout=self.connect_timeout,
                close_timeout=0.2,
                ping_interval=None,
                max_size=1_048_576,
                proxy=None,
            )
            async with connection as websocket:
                if audio:
                    await websocket.send(audio)
                await websocket.send("done")
                raw = await asyncio.wait_for(websocket.recv(), self.result_timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise VoiceError("WHISPER_UNAVAILABLE") from exc
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError("WHISPER_UNAVAILABLE") from exc
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise VoiceError("INVALID_MODEL_OUTPUT") from exc
        if not isinstance(payload, dict) or set(payload) != {"text", "segments"}:
            raise VoiceError("INVALID_MODEL_OUTPUT")
        text = payload.get("text")
        if not isinstance(text, str):
            raise VoiceError("INVALID_MODEL_OUTPUT")
        return text.replace("\x00", "").strip()


class WhisperStream:
    """A Whisper-backed turn shaped like voice.py's NemotronStream."""

    def __init__(self, client: WhisperClient, on_partial: Callable[[str], Awaitable[None]]):
        self.client = client
        self.on_partial = on_partial
        self.websocket = None
        self.connection = None
        self.receiver: asyncio.Task | None = None
        self.result: asyncio.Future[str] | None = None

    async def open(self) -> None:
        try:
            self.connection = await self.client._connect()
            self.websocket = await self.connection.__aenter__()
            await self.client._expect(self.websocket, "ready")
            self.result = asyncio.get_running_loop().create_future()
            self.receiver = asyncio.create_task(self._receive())
        except VoiceError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise VoiceError("WHISPER_UNAVAILABLE") from exc

    async def push_audio(self, audio: bytes) -> None:
        if self.websocket is None:
            raise VoiceError("WHISPER_UNAVAILABLE")
        try:
            pcm = float32le_to_pcm16(audio)
            if pcm:
                await self.websocket.send(pcm)
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError("WHISPER_UNAVAILABLE") from exc

    async def finish(self) -> str:
        if self.websocket is None or self.result is None:
            raise VoiceError("WHISPER_UNAVAILABLE")
        try:
            await self.websocket.send(json.dumps({"type": "commit"}))
            return await asyncio.wait_for(asyncio.shield(self.result), self.client.result_timeout)
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError("WHISPER_UNAVAILABLE") from exc
        finally:
            await self.close()

    async def close(self) -> None:
        if self.receiver is not None and not self.receiver.done():
            self.receiver.cancel()
        if self.receiver is not None:
            await asyncio.gather(self.receiver, return_exceptions=True)
        self.receiver = None
        if self.connection is not None:
            try:
                await self.connection.__aexit__(None, None, None)
            except Exception:
                pass
        self.connection = None
        self.websocket = None

    async def _receive(self) -> None:
        assert self.websocket is not None
        assert self.result is not None
        try:
            while True:
                event = await self.websocket.recv()
                if isinstance(event, bytes):
                    continue
                try:
                    payload = json.loads(event)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise VoiceError("INVALID_MODEL_OUTPUT") from exc
                if not isinstance(payload, dict):
                    raise VoiceError("INVALID_MODEL_OUTPUT")
                event_type = payload.get("type")
                if event_type == "partial":
                    text = payload.get("text")
                    if not isinstance(text, str):
                        raise VoiceError("INVALID_MODEL_OUTPUT")
                    await self.on_partial(text)
                elif event_type == "final":
                    text = payload.get("text")
                    if not isinstance(text, str) or not text.strip():
                        raise VoiceError("INVALID_MODEL_OUTPUT")
                    if not self.result.done():
                        self.result.set_result(text.strip())
                    return
                elif event_type == "error":
                    reason = payload.get("reason_code")
                    if reason in _WHISPER_AVAILABILITY_ERRORS:
                        raise VoiceError("WHISPER_UNAVAILABLE")
                    if reason in _WHISPER_REFUSAL_ERRORS:
                        raise VoiceError(reason)
                    raise VoiceError("INVALID_MODEL_OUTPUT")
                else:
                    raise VoiceError("INVALID_MODEL_OUTPUT")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.result.done():
                self.result.set_exception(
                    exc if isinstance(exc, VoiceError) else VoiceError("WHISPER_UNAVAILABLE")
                )

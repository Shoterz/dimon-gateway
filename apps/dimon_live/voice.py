"""Bounded push-to-talk bridge between browsers and the private Nemotron service."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import math
import struct
from pathlib import Path
from typing import Any
from typing import Awaitable, Callable

from starlette.websockets import WebSocketDisconnect


class VoiceError(RuntimeError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


class VoiceLimitError(VoiceError):
    pass


class NemotronLifecycleError(RuntimeError):
    pass


class NemotronLifecycleClient:
    """Small client for the existing Jarvis Nemotron on-demand supervisor."""

    def __init__(
        self,
        socket_path: Path,
        *,
        timeout: float = 120.0,
        max_response_bytes: int = 1_048_576,
    ):
        self.socket_path = Path(socket_path)
        if not self.socket_path.is_absolute():
            raise ValueError("Nemotron control socket must be absolute")
        if timeout <= 0 or max_response_bytes <= 0:
            raise ValueError("invalid Nemotron lifecycle limits")
        self.timeout = float(timeout)
        self.max_response_bytes = int(max_response_bytes)
        self._acquired = False

    async def acquire(self) -> dict[str, object]:
        response = await self._request({"op": "acquire"})
        self._acquired = True
        return response

    async def release(self, *, disconnected: bool = False) -> None:
        if not self._acquired:
            return
        request: dict[str, object] = {"op": "release"}
        if disconnected:
            request["disconnected"] = True
        await self._request(request)
        self._acquired = False

    async def _request(self, request: dict[str, object]) -> dict[str, object]:
        payload = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(
                    str(self.socket_path),
                    limit=self.max_response_bytes + 1,
                ),
                timeout=self.timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise NemotronLifecycleError("Nemotron lifecycle is unavailable") from exc
        try:
            writer.write(payload)
            await asyncio.wait_for(writer.drain(), timeout=self.timeout)
            line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
        except (OSError, asyncio.TimeoutError, asyncio.LimitOverrunError) as exc:
            raise NemotronLifecycleError("Nemotron lifecycle request failed") from exc
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
        if not line or len(line) > self.max_response_bytes:
            raise NemotronLifecycleError("Invalid Nemotron lifecycle response")
        try:
            response: Any = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NemotronLifecycleError("Invalid Nemotron lifecycle response") from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise NemotronLifecycleError("Nemotron lifecycle request was rejected")
        return response


class VoiceTurnDetector:
    """Small dependency-free endpoint detector for the public MVP.

    Jarvis uses a warm Silero stream plus the same pause/endpoint concept. The
    Dimon MVP keeps the boundary intentionally lightweight: RMS energy starts
    a turn, then sustained silence ends it. It only decides when to commit
    speech; it never authorizes a banking action.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        speech_threshold: float = 0.015,
        speech_confirmation_seconds: float = 0.12,
        pause_seconds: float = 0.30,
        endpoint_silence_seconds: float = 1.40,
        max_utterance_seconds: float = 30.0,
    ):
        if sample_rate <= 0 or speech_threshold < 0:
            raise ValueError("invalid voice turn detector settings")
        if not 0 < speech_confirmation_seconds <= pause_seconds < endpoint_silence_seconds:
            raise ValueError("voice endpoint timings must be ordered")
        if max_utterance_seconds <= 0:
            raise ValueError("maximum utterance duration must be positive")
        self.sample_rate = sample_rate
        self.speech_threshold = speech_threshold
        self.confirmation_samples = int(sample_rate * speech_confirmation_seconds)
        self.pause_samples = int(sample_rate * pause_seconds)
        self.endpoint_samples = int(sample_rate * endpoint_silence_seconds)
        self.max_samples = int(sample_rate * max_utterance_seconds)
        self.reset()

    def reset(self) -> None:
        self.samples_seen = 0
        self.speech_samples = 0
        self.last_speech_at: int | None = None
        self.active = False
        self.pause_emitted = False
        self.ended = False

    def push(self, chunk: bytes) -> tuple[str, ...]:
        if len(chunk) % 4:
            raise VoiceLimitError("MALFORMED_AUDIO")
        samples = list(struct.iter_unpack("<f", chunk))
        if not samples:
            return ()
        values = [value[0] for value in samples]
        if not all(math.isfinite(value) for value in values):
            raise VoiceLimitError("MALFORMED_AUDIO")
        count = len(values)
        self.samples_seen += count
        rms = math.sqrt(sum(value * value for value in values) / count)
        speech = rms >= self.speech_threshold
        if self.ended:
            return ()
        if speech:
            self.speech_samples += count
            self.last_speech_at = self.samples_seen
            self.pause_emitted = False
            if not self.active and self.speech_samples >= self.confirmation_samples:
                self.active = True
                return ("speech_started",)
            return ()
        if not self.active or self.last_speech_at is None:
            return ()
        silence = self.samples_seen - self.last_speech_at
        events: list[str] = []
        if silence >= self.pause_samples and not self.pause_emitted:
            events.append("pause")
            self.pause_emitted = True
        if silence >= self.endpoint_samples or self.samples_seen >= self.max_samples:
            events.append("endpoint")
            self.active = False
            self.ended = True
        return tuple(events)


class VoiceLease:
    """A process-wide, non-queued lease for the single local speech model."""

    def __init__(self):
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        if self._lock.locked():
            return False
        await self._lock.acquire()
        return True

    def release(self) -> None:
        if self._lock.locked():
            self._lock.release()


def float32le_to_pcm16(audio: bytes) -> bytes:
    if len(audio) % 4:
        raise VoiceLimitError("MALFORMED_AUDIO")
    samples = struct.iter_unpack("<f", audio)
    converted = bytearray()
    for (sample,) in samples:
        if not math.isfinite(sample):
            raise VoiceLimitError("MALFORMED_AUDIO")
        sample = max(-1.0, min(1.0, sample))
        # Match Jarvis' float32_to_pcm16 conversion: +1.0 clips to the
        # signed 16-bit ceiling while -1.0 remains -32768.
        converted.extend(
            struct.pack(
                "<h",
                max(-32768, min(32767, int(sample * 32768))),
            )
        )
    return bytes(converted)


class NemotronClient:
    def __init__(self, url: str, *, connect_timeout: float = 3.0, result_timeout: float = 15.0):
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

    async def open_stream(self, on_partial: Callable[[str], Awaitable[None]]):
        stream = NemotronStream(self, on_partial)
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
            raise VoiceError("NEMOTRON_UNAVAILABLE") from exc

    async def _expect(self, websocket, expected: str) -> None:
        try:
            event = json.loads(await asyncio.wait_for(websocket.recv(), self.connect_timeout))
        except Exception as exc:
            raise VoiceError("NEMOTRON_UNAVAILABLE") from exc
        if event.get("type") != expected:
            raise VoiceError("NEMOTRON_UNAVAILABLE")


class NemotronStream:
    """A Jarvis-style streaming Nemotron turn kept small for Dimon MVP."""

    def __init__(self, client: NemotronClient, on_partial: Callable[[str], Awaitable[None]]):
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
            await self.client._expect(self.websocket, "session.created")
            await self.websocket.send(json.dumps({
                "type": "session.update",
                "session": {"sample_rate": 16_000, "language": "en"},
            }))
            await self.client._expect(self.websocket, "session.updated")
            self.result = asyncio.get_running_loop().create_future()
            self.receiver = asyncio.create_task(self._receive())
        except VoiceError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise VoiceError("NEMOTRON_UNAVAILABLE") from exc

    async def push_audio(self, audio: bytes) -> None:
        if self.websocket is None:
            raise VoiceError("NEMOTRON_UNAVAILABLE")
        try:
            pcm = float32le_to_pcm16(audio)
            if pcm:
                await self.websocket.send(pcm)
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError("NEMOTRON_UNAVAILABLE") from exc

    async def finish(self) -> str:
        if self.websocket is None or self.result is None:
            raise VoiceError("NEMOTRON_UNAVAILABLE")
        try:
            await self.websocket.send(json.dumps({"type": "input_audio_buffer.commit"}))
            return await asyncio.wait_for(asyncio.shield(self.result), self.client.result_timeout)
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError("NEMOTRON_UNAVAILABLE") from exc
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
        transcript = ""
        try:
            while True:
                event = await self.websocket.recv()
                if isinstance(event, bytes):
                    continue
                try:
                    payload = json.loads(event)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise VoiceError("NEMOTRON_UNAVAILABLE") from exc
                event_type = payload.get("type")
                if event_type == "conversation.item.input_audio_transcription.delta":
                    delta = payload.get("delta")
                    if not isinstance(delta, str):
                        raise VoiceError("NEMOTRON_UNAVAILABLE")
                    transcript += delta
                    await self.on_partial(transcript)
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    final = payload.get("transcript")
                    if not isinstance(final, str) or not final.strip():
                        raise VoiceError("NEMOTRON_UNAVAILABLE")
                    if not self.result.done():
                        self.result.set_result(final.strip())
                    return
                elif event_type == "input_audio_buffer.committed":
                    continue
                elif event_type == "error":
                    raise VoiceError("NEMOTRON_UNAVAILABLE")
                else:
                    raise VoiceError("NEMOTRON_UNAVAILABLE")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.result.done():
                self.result.set_exception(exc if isinstance(exc, VoiceError) else VoiceError("NEMOTRON_UNAVAILABLE"))


ActionCallback = Callable[[str], Awaitable[dict] | dict]


class VoiceBridge:
    def __init__(
        self,
        *,
        recognizer: NemotronClient,
        lifecycle: NemotronLifecycleClient | None = None,
        lease: VoiceLease | None = None,
        max_audio_bytes: int = 4_000_000,
        max_utterance_seconds: float = 30.0,
        endpoint_silence_seconds: float = 1.40,
        pause_seconds: float = 0.30,
        speech_threshold: float = 0.015,
    ):
        self.recognizer = recognizer
        self.lifecycle = lifecycle
        self.lease = lease or VoiceLease()
        self.max_audio_bytes = max(4, int(max_audio_bytes))
        self.max_utterance_seconds = float(max_utterance_seconds)
        self.endpoint_silence_seconds = float(endpoint_silence_seconds)
        self.pause_seconds = float(pause_seconds)
        self.speech_threshold = float(speech_threshold)

    def validate_audio(self, chunk: bytes, current_bytes: int = 0) -> None:
        if not isinstance(chunk, bytes) or len(chunk) == 0 or len(chunk) % 4:
            raise VoiceLimitError("MALFORMED_AUDIO")
        total = current_bytes + len(chunk)
        if total > self.max_audio_bytes:
            raise VoiceLimitError("AUDIO_LIMIT_EXCEEDED")
        if total / 4 / 16_000 > self.max_utterance_seconds:
            raise VoiceLimitError("UTTERANCE_LIMIT_EXCEEDED")

    async def handle(self, websocket, context, action_callback: ActionCallback) -> None:
        if not await self.lease.acquire():
            await websocket.accept()
            await websocket.send_json({"type": "error", "reason_code": "VOICE_BUSY"})
            await self._close(websocket, 1013)
            return
        stream = None
        lifecycle_acquired = False
        disconnected = False
        try:
            await websocket.accept()
            chunks: list[bytes] = []
            total = 0
            detector = VoiceTurnDetector(
                pause_seconds=self.pause_seconds,
                endpoint_silence_seconds=self.endpoint_silence_seconds,
                max_utterance_seconds=self.max_utterance_seconds,
                speech_threshold=self.speech_threshold,
            )

            async def partial(text: str) -> None:
                await websocket.send_json({"type": "transcript_partial", "text": text})

            if self.lifecycle is not None:
                try:
                    await self.lifecycle.acquire()
                    lifecycle_acquired = True
                except NemotronLifecycleError as exc:
                    raise VoiceError("NEMOTRON_UNAVAILABLE") from exc
            open_stream = getattr(self.recognizer, "open_stream", None)
            if callable(open_stream):
                stream = await open_stream(partial)
            await websocket.send_json({"type": "ready", "sample_rate": 16_000})
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    disconnected = True
                    return
                if message.get("bytes") is not None:
                    chunk = message["bytes"]
                    self.validate_audio(chunk, total)
                    chunks.append(chunk)
                    total += len(chunk)
                    if stream is not None:
                        await stream.push_audio(chunk)
                    events = detector.push(chunk)
                    if "pause" in events and "endpoint" not in events:
                        await websocket.send_json({"type": "pause"})
                    if "endpoint" in events:
                        await websocket.send_json({
                            "type": "endpoint",
                            "reason": "silence",
                        })
                        break
                    continue
                text = message.get("text")
                if text is None:
                    raise VoiceLimitError("MALFORMED_FRAME")
                try:
                    command = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise VoiceLimitError("MALFORMED_FRAME") from exc
                if command.get("type") == "cancel":
                    await websocket.send_json({"type": "closed", "reason_code": "VOICE_CANCELLED"})
                    return
                if command.get("type") != "stop":
                    raise VoiceLimitError("MALFORMED_FRAME")
                break
            audio = b"".join(chunks)
            if stream is not None:
                transcript = await stream.finish()
            else:
                transcript = await self.recognizer.transcribe(audio, partial)
            await websocket.send_json({"type": "transcript_final", "text": transcript})
            result = action_callback(transcript)
            if inspect.isawaitable(result):
                result = await result
            await websocket.send_json({"type": "action_result", "result": result})
            await self._close(websocket, 1000)
        except WebSocketDisconnect:
            disconnected = True
            return
        except VoiceError as exc:
            await websocket.send_json({"type": "error", "reason_code": exc.reason_code})
            await self._close(websocket, 1003)
        finally:
            if stream is not None:
                close = getattr(stream, "close", None)
                if callable(close):
                    await close()
            if lifecycle_acquired and self.lifecycle is not None:
                with contextlib.suppress(Exception):
                    await self.lifecycle.release(disconnected=disconnected)
            self.lease.release()

    @staticmethod
    async def _close(websocket, code: int) -> None:
        close = getattr(websocket, "close", None)
        if close is not None:
            try:
                await close(code=code)
            except Exception:
                pass

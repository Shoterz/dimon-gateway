"""Bounded push-to-talk bridge between browsers and the private Nemotron service."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import math
import struct
from uuid import uuid4
import wave
from pathlib import Path
from typing import Any
from typing import Awaitable, Callable

from starlette.websockets import WebSocketDisconnect

from .model_adapter import InferenceFailure
from .spoken_normalization import normalize_spoken_request

logger = logging.getLogger(__name__)


class VoiceError(RuntimeError):
    def __init__(self, reason_code: str, failure_stage: str = "voice_pipeline"):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.failure_stage = failure_stage


class VoiceLimitError(VoiceError):
    pass


_NEMOTRON_PHASES = {
    "websocket_connect": "connect",
    "handshake_session_created": "ready_timeout",
    "handshake_session_updated": "ready_timeout",
    "transcription_receive": "stream_closed",
    "transcription_finish": "stream_closed",
    "transcription_event_parse": "protocol",
    "transcription_delta": "protocol",
    "unexpected_transcription_event": "protocol",
    "transcription_final": "empty_final",
    "transcription_error_event": "upstream_error",
}


def nemotron_failure_phase(failure_stage: str) -> str:
    """Return a bounded diagnostic phase that contains no upstream details."""

    return _NEMOTRON_PHASES.get(failure_stage, "protocol")


class NemotronLifecycleError(RuntimeError):
    pass


class NemotronLifecycleLease:
    """One independently releasable Nemotron supervisor lease."""

    def __init__(
        self,
        client: "NemotronLifecycleClient",
        lease_id: str,
        *,
        heartbeat_seconds: float = 10.0,
    ) -> None:
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        self.client = client
        self.lease_id = lease_id
        self.heartbeat_seconds = float(heartbeat_seconds)
        self._released = False
        self._heartbeat_task: asyncio.Task[None] | None = None

    def start(self) -> "NemotronLifecycleLease":
        if self._released:
            raise RuntimeError("cannot start a released Nemotron lease")
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return self

    async def release(self, *, disconnected: bool = False) -> None:
        if self._released:
            return
        self._released = True
        heartbeat_task, self._heartbeat_task = self._heartbeat_task, None
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        await self.client._request(
            {
                "op": "release",
                "lease_id": self.lease_id,
                "disconnected": bool(disconnected),
            }
        )

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_seconds)
                await self.client._request(
                    {"op": "heartbeat", "lease_id": self.lease_id}
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # lease expires safely in the supervisor
            logger.warning(
                "nemotron_lifecycle_heartbeat_failed error_type=%s",
                type(exc).__name__,
            )


class NemotronLifecycleClient:
    """Small client for the existing Jarvis Nemotron on-demand supervisor."""

    def __init__(
        self,
        socket_path: Path,
        *,
        timeout: float = 120.0,
        max_response_bytes: int = 1_048_576,
        heartbeat_seconds: float = 10.0,
    ):
        self.socket_path = Path(socket_path)
        if not self.socket_path.is_absolute():
            raise ValueError("Nemotron control socket must be absolute")
        if timeout <= 0 or max_response_bytes <= 0 or heartbeat_seconds <= 0:
            raise ValueError("invalid Nemotron lifecycle limits")
        self.timeout = float(timeout)
        self.max_response_bytes = int(max_response_bytes)
        self.heartbeat_seconds = float(heartbeat_seconds)

    async def acquire(self) -> NemotronLifecycleLease:
        lease_id = uuid4().hex
        await self._request({"op": "acquire", "lease_id": lease_id})
        return NemotronLifecycleLease(
            self,
            lease_id,
            heartbeat_seconds=self.heartbeat_seconds,
        ).start()

    async def status(self) -> dict[str, object]:
        response = await self._request({"op": "status"})
        status = response.get("status")
        if not isinstance(status, dict):
            raise NemotronLifecycleError("Invalid Nemotron lifecycle status")
        return status

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
    """A non-queued lease on the speech recogniser.

    A local model on one GPU serves one utterance at a time (capacity 1). A
    cloud recogniser such as TRTC scales out, so its capacity is set by
    DIMON_VOICE_CONCURRENCY instead of capping the whole server at one user.
    """

    def __init__(self, capacity: int = 1):
        self.capacity = max(1, int(capacity))
        self._active = 0

    @property
    def active_count(self) -> int:
        return self._active

    async def acquire(self) -> bool:
        if self._active >= self.capacity:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        if self._active > 0:
            self._active -= 1


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
            raise VoiceError("NEMOTRON_UNAVAILABLE", "websocket_connect") from exc

    async def _expect(self, websocket, expected: str) -> None:
        stage = f"handshake_{expected.replace('.', '_')}"
        try:
            event = json.loads(await asyncio.wait_for(websocket.recv(), self.connect_timeout))
        except Exception as exc:
            raise VoiceError("NEMOTRON_UNAVAILABLE", stage) from exc
        if event.get("type") != expected:
            raise VoiceError("NEMOTRON_UNAVAILABLE", stage)


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
            raise VoiceError("NEMOTRON_UNAVAILABLE", "session_update") from exc

    async def push_audio(self, audio: bytes) -> None:
        if self.websocket is None:
            raise VoiceError("NEMOTRON_UNAVAILABLE", "audio_push")
        try:
            pcm = float32le_to_pcm16(audio)
            if pcm:
                await self.websocket.send(pcm)
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError("NEMOTRON_UNAVAILABLE", "audio_push") from exc

    async def finish(self) -> str:
        if self.websocket is None or self.result is None:
            raise VoiceError("NEMOTRON_UNAVAILABLE", "transcription_finish")
        try:
            await self.websocket.send(json.dumps({"type": "input_audio_buffer.commit"}))
            return await asyncio.wait_for(asyncio.shield(self.result), self.client.result_timeout)
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError("NEMOTRON_UNAVAILABLE", "transcription_finish") from exc
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
                    raise VoiceError("NEMOTRON_UNAVAILABLE", "transcription_event_parse") from exc
                event_type = payload.get("type")
                if event_type == "conversation.item.input_audio_transcription.delta":
                    delta = payload.get("delta")
                    if not isinstance(delta, str):
                        raise VoiceError("NEMOTRON_UNAVAILABLE", "transcription_delta")
                    transcript += delta
                    await self.on_partial(transcript)
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    final = payload.get("transcript")
                    if not isinstance(final, str) or not final.strip():
                        raise VoiceError("NEMOTRON_UNAVAILABLE", "transcription_final")
                    if not self.result.done():
                        self.result.set_result(final.strip())
                    return
                elif event_type == "input_audio_buffer.committed":
                    continue
                elif event_type == "error":
                    raise VoiceError("NEMOTRON_UNAVAILABLE", "transcription_error_event")
                else:
                    raise VoiceError("NEMOTRON_UNAVAILABLE", "unexpected_transcription_event")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.result.done():
                self.result.set_exception(
                    exc if isinstance(exc, VoiceError)
                    else VoiceError("NEMOTRON_UNAVAILABLE", "transcription_receive")
                )


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
        evidence_dir: Path | None = None,
    ):
        self.evidence_dir = Path(evidence_dir) if evidence_dir is not None else None
        self.recognizer = recognizer
        self.lifecycle = lifecycle
        self.lease = lease or VoiceLease()
        self.max_audio_bytes = max(4, int(max_audio_bytes))
        self.max_utterance_seconds = float(max_utterance_seconds)
        self.endpoint_silence_seconds = float(endpoint_silence_seconds)
        self.pause_seconds = float(pause_seconds)
        self.speech_threshold = float(speech_threshold)

    def _voice_evidence(self, audio: bytes, transcript: str) -> dict[str, Any]:
        """Audit evidence for one utterance: the raw audio's hash and, when an
        evidence directory is configured, the audio itself as a 16 kHz WAV."""

        evidence: dict[str, Any] = {
            "transcript": transcript,
            "audio_sha256": hashlib.sha256(audio).hexdigest(),
            "audio_seconds": round(len(audio) / 4 / 16_000, 2),
            "encoding": "float32le/16000",
        }
        if self.evidence_dir is not None:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            path = self.evidence_dir / f"{evidence['audio_sha256']}.wav"
            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16_000)
                wav_file.writeframes(float32le_to_pcm16(audio))
            evidence["audio_file"] = path.name
        return evidence

    def validate_audio(self, chunk: bytes, current_bytes: int = 0) -> None:
        if not isinstance(chunk, bytes) or len(chunk) == 0 or len(chunk) % 4:
            raise VoiceLimitError("MALFORMED_AUDIO")
        total = current_bytes + len(chunk)
        if total > self.max_audio_bytes:
            raise VoiceLimitError("AUDIO_LIMIT_EXCEEDED")
        if total / 4 / 16_000 > self.max_utterance_seconds:
            raise VoiceLimitError("UTTERANCE_LIMIT_EXCEEDED")

    async def handle(self, websocket, context, action_callback: ActionCallback) -> None:
        from .speech import transcription_disagreement_message
        from .transcript_arbitration import arbitrate_transcripts

        diagnostic_id = uuid4().hex
        if not await self.lease.acquire():
            logger.warning(json.dumps({
                "event": "voice_pipeline_failed",
                "diagnostic_id": diagnostic_id,
                "stage": "voice_lease_acquire",
                "reason_code": "VOICE_BUSY",
            }, sort_keys=True, separators=(",", ":")))
            await websocket.accept()
            await websocket.send_json({"type": "error", "reason_code": "VOICE_BUSY"})
            await self._close(websocket, 1013)
            return
        stream = None
        lifecycle_lease = None
        disconnected = False
        upstream_disconnected = False
        stage = "voice_session_start"
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

            latest_partial = ""

            async def partial(text: str) -> None:
                nonlocal latest_partial
                if isinstance(text, str) and text.strip():
                    latest_partial = text.strip()
                await websocket.send_json({"type": "transcript_partial", "text": text})

            if self.lifecycle is not None:
                stage = "nemotron_lifecycle_acquire"
                try:
                    lifecycle_lease = await self.lifecycle.acquire()
                except NemotronLifecycleError as exc:
                    raise VoiceError("NEMOTRON_UNAVAILABLE", "lifecycle_acquire") from exc
            open_stream = getattr(self.recognizer, "open_stream", None)
            if callable(open_stream):
                stage = "nemotron_stream_open"
                stream = await open_stream(partial)
            stage = "microphone_audio_receive"
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
                        stage = "nemotron_audio_push"
                        await stream.push_audio(chunk)
                    stage = "microphone_audio_receive"
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
            stage = "nemotron_transcription_finish"
            if stream is not None:
                transcript = await stream.finish()
            else:
                transcript = await self.recognizer.transcribe(audio, partial)
            decision = arbitrate_transcripts(latest_partial, transcript)
            if decision.status == "CONFIRM_LIVE":
                question = transcription_disagreement_message(latest_partial)
                await websocket.send_json({
                    "type": "transcript_conflict",
                    "live_text": latest_partial,
                    "final_text": transcript,
                })
                await websocket.send_json({
                    "type": "action_result",
                    "result": {
                        "outcome": "CLARIFICATION_REQUEST",
                        "reason_code": "TRANSCRIPTION_DISAGREEMENT",
                        "answer": question,
                        "question": question,
                        "transcription_disagreement": {
                            "live_text": latest_partial,
                            "final_text": transcript,
                        },
                    },
                })
                await self._close(websocket, 1000)
                return
            stage = "action_dispatch"
            await websocket.send_json({"type": "transcript_final", "text": transcript})
            normalized_transcript = normalize_spoken_request(transcript)
            evidence = self._voice_evidence(audio, transcript)
            if normalized_transcript != transcript:
                evidence["normalized_transcript"] = normalized_transcript
            if len(inspect.signature(action_callback).parameters) >= 2:
                result = action_callback(normalized_transcript, evidence)
            else:
                result = action_callback(normalized_transcript)
            if inspect.isawaitable(result):
                result = await result
            if (
                isinstance(result, dict)
                and result.get("outcome") == "BLOCKED"
                and result.get("reason_code") == "APP_LOCKED"
            ):
                await websocket.send_json({"type": "error", "reason_code": "APP_LOCKED"})
                await self._close(websocket, 1008)
                return
            await websocket.send_json({"type": "action_result", "result": result})
            await self._close(websocket, 1000)
        except WebSocketDisconnect:
            disconnected = True
            return
        except VoiceError as exc:
            upstream_phase = nemotron_failure_phase(getattr(exc, "failure_stage", ""))
            upstream_disconnected = upstream_phase in {
                "connect", "ready_timeout", "stream_closed", "upstream_error"
            }
            logger.warning(json.dumps({
                "event": "voice_pipeline_failed",
                "diagnostic_id": diagnostic_id,
                "stage": stage,
                "upstream_stage": upstream_phase,
                "reason_code": exc.reason_code,
                "error_type": type(exc).__name__,
            }, sort_keys=True, separators=(",", ":")))
            await websocket.send_json({"type": "error", "reason_code": exc.reason_code})
            await self._close(websocket, 1003)
        except InferenceFailure as exc:
            logger.warning(json.dumps({
                "event": "voice_pipeline_failed",
                "diagnostic_id": diagnostic_id,
                "stage": stage,
                "upstream_stage": getattr(exc, "failure_stage", ""),
                "reason_code": exc.reason_code,
                "error_type": type(exc).__name__,
            }, sort_keys=True, separators=(",", ":")))
            await websocket.send_json({"type": "error", "reason_code": exc.reason_code})
            await self._close(websocket, 1011)
        finally:
            try:
                if stream is not None:
                    close = getattr(stream, "close", None)
                    if callable(close):
                        await close()
            finally:
                try:
                    if lifecycle_lease is not None:
                        with contextlib.suppress(Exception):
                            await lifecycle_lease.release(
                                disconnected=disconnected or upstream_disconnected
                            )
                finally:
                    self.lease.release()

    @staticmethod
    async def _close(websocket, code: int) -> None:
        close = getattr(websocket, "close", None)
        if close is not None:
            try:
                await close(code=code)
            except Exception:
                pass

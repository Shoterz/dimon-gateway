"""Hands-free voice: "Hey Dimon", spoken follow-ups and spoken confirmation.

One WebSocket per open tab streams 16 kHz float32 microphone audio. The
server splits it into utterances by energy, and handles each one by the mode
the browser last asked for:

* asleep (default): the utterance is transcribed and dropped unless it starts
  with "Hey Dimon". Once a voiceprint is registered, the wake phrase must
  also be in the account holder's voice, so a stranger or the TV cannot wake
  it. Dropped audio and text are never stored, logged, or sent back.
* command: the utterance is a new request and goes through the same pipeline
  as typed text (firewall, scam check, parsing, policy, receipts).
* reply: the utterance answers Dimon's question; it is interpreted by fixed
  rules (yes/no, which account, which payee, a name) and only chooses between
  the options Dimon offered.
* approval: the utterance is a spoken confirmation of one approval challenge,
  checked against the one-time phrase and the voiceprint.
* enroll: the utterance is one voiceprint registration sample.

None of these modes approves anything on its own: every payment still needs
its stored receipt and a device signature, and S$200 or more needs the face.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import math
import struct
import time
from typing import Any, Awaitable, Callable

from starlette.websockets import WebSocketDisconnect

from . import voice_dialog
from .voice import VoiceError, VoiceLimitError
from .receipts import DEMO_USER_ID
from .voiceprint import VoiceRejected

SAMPLE_RATE = 16_000
WAKE_SPEAKER_THRESHOLD = 0.45   # wake only; approvals use the stricter voiceprint threshold
FOLLOW_UP_SECONDS = 10.0
MAX_UTTERANCE_SECONDS = 15.0
MAX_SESSION_SECONDS = 30 * 60
PRE_ROLL_SECONDS = 0.3


class Segmenter:
    """Energy-based utterance splitting for a continuous stream."""

    def __init__(self, *, threshold: float = 0.015, start_seconds: float = 0.12,
                 end_silence_seconds: float = 0.9, max_seconds: float = MAX_UTTERANCE_SECONDS):
        self.threshold = threshold
        self.start_samples = int(SAMPLE_RATE * start_seconds)
        self.end_samples = int(SAMPLE_RATE * end_silence_seconds)
        self.max_samples = int(SAMPLE_RATE * max_seconds)
        self.pre_roll = int(SAMPLE_RATE * PRE_ROLL_SECONDS) * 4
        self.reset()

    def reset(self) -> None:
        self.history = bytearray()     # recent audio before speech started
        self.buffer = bytearray()
        self.speech = 0
        self.silence = 0
        self.active = False

    def push(self, chunk: bytes) -> tuple[str | None, bytes | None]:
        """("started" | "utterance" | None, audio of a finished utterance)."""

        if len(chunk) % 4:
            raise VoiceLimitError("MALFORMED_AUDIO")
        count = len(chunk) // 4
        if count == 0:
            return None, None
        values = struct.unpack(f"<{count}f", chunk)
        if not all(math.isfinite(value) for value in values):
            raise VoiceLimitError("MALFORMED_AUDIO")
        loud = math.sqrt(sum(v * v for v in values) / count) >= self.threshold
        if not self.active:
            self.history.extend(chunk)
            del self.history[: max(0, len(self.history) - self.pre_roll)]
            self.speech = self.speech + count if loud else 0
            if self.speech >= self.start_samples:
                self.active = True
                self.buffer = bytearray(self.history)
                self.silence = 0
                return "started", None
            return None, None
        self.buffer.extend(chunk)
        self.silence = 0 if loud else self.silence + count
        if self.silence >= self.end_samples or len(self.buffer) // 4 >= self.max_samples:
            audio = bytes(self.buffer)
            self.reset()
            return "utterance", audio
        return None, None


Dispatch = Callable[[str, dict[str, Any]], Awaitable[dict] | dict]


class HandsFreeBridge:
    def __init__(self, *, recognizer: Any, lease: Any, transcribe_wait: float = 5.0):
        self.recognizer = recognizer
        self.lease = lease
        self.transcribe_wait = transcribe_wait

    async def _transcribe(self, audio: bytes) -> str:
        # The local speech model serves one utterance at a time; wait briefly
        # for it instead of holding it for the whole hands-free session.
        deadline = time.monotonic() + self.transcribe_wait
        while not await self.lease.acquire():
            if time.monotonic() >= deadline:
                raise VoiceError("VOICE_BUSY")
            await asyncio.sleep(0.05)
        try:
            async def ignore(_: str) -> None:
                return None

            return (await self.recognizer.transcribe(audio, ignore)).strip()
        finally:
            self.lease.release()

    async def handle(self, websocket, context, dispatch: Dispatch) -> None:
        database = context.database
        await websocket.accept()
        voice_status = database.voice_status()
        await websocket.send_json({
            "type": "ready",
            "sample_rate": SAMPLE_RATE,
            "wake_phrase": "Hey Dimon",
            "voiceprint": voice_status["voice_registered"],
            "voice_max_minor": voice_status["voice_max_minor"],
        })
        segmenter = Segmenter()
        mode: dict[str, Any] = {"kind": "asleep"}
        started = time.monotonic()
        pending: set[asyncio.Task] = set()

        def awake(kind: str = "command", **extra: Any) -> None:
            mode.clear()
            mode.update({"kind": kind, "until": time.monotonic() + FOLLOW_UP_SECONDS, **extra})

        async def handle_utterance(audio: bytes, current: dict[str, Any]) -> None:
            kind = current.get("kind", "asleep")
            # A reply must start inside the follow-up window; it may take up to
            # one utterance length to finish.
            if kind != "asleep" and time.monotonic() > current.get("until", 0) + MAX_UTTERANCE_SECONDS + 2:
                kind = "asleep"
            try:
                transcript = await self._transcribe(audio)
            except (VoiceError, Exception) as exc:  # noqa: BLE001 - reported, never fatal
                reason = exc.reason_code if isinstance(exc, VoiceError) else "TRANSCRIPTION_FAILED"
                if kind != "asleep":
                    await websocket.send_json({"type": "error", "reason_code": reason})
                return
            if not transcript:
                return
            if kind == "approval":
                await self._approval(websocket, database, current, audio, transcript)
                return
            if kind == "enroll":
                await self._enroll(websocket, database, current, audio, transcript)
                return
            similarity = await asyncio.to_thread(database.voice_store.speaker_similarity, DEMO_USER_ID, audio) \
                if voice_status["voice_registered"] else None
            speaker = "UNKNOWN" if similarity is None else "MATCH" if similarity >= WAKE_SPEAKER_THRESHOLD else "MISMATCH"
            if kind == "asleep":
                woken, command = voice_dialog.wake(transcript)
                if not woken or speaker == "MISMATCH":
                    return    # not for Dimon, or not the account holder: dropped
                if not command:
                    awake("command")
                    await websocket.send_json({"type": "wake", "speaker": speaker})
                    return
                transcript, kind = command, "command"
                await websocket.send_json({"type": "wake", "speaker": speaker, "command": command})
            elif speaker == "MISMATCH":
                await websocket.send_json({"type": "ignored", "reason_code": "SPEAKER_MISMATCH"})
                return
            evidence = {
                "transcript": transcript,
                "speaker": speaker,
                "speaker_similarity": similarity,
                "audio_seconds": round(len(audio) / 4 / SAMPLE_RATE, 2),
                "encoding": "float32le/16000",
                "hands_free": True,
            }
            if kind == "reply":
                value = voice_dialog.interpret(current.get("expect", ""), transcript, current.get("options") or ())
                mode.clear()
                mode["kind"] = "asleep"
                await websocket.send_json({"type": "reply", "text": transcript, "value": value,
                                           "expect": current.get("expect")})
                return
            mode.clear()
            mode["kind"] = "asleep"
            await websocket.send_json({"type": "heard", "text": transcript})
            result = dispatch(transcript, evidence)
            if inspect.isawaitable(result):
                result = await result
            await websocket.send_json({"type": "action_result", "text": transcript, "result": result})

        try:
            while True:
                if time.monotonic() - started > MAX_SESSION_SECONDS:
                    await websocket.send_json({"type": "closed", "reason_code": "SESSION_TIME_LIMIT"})
                    break
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    event, audio = segmenter.push(message["bytes"])
                    if event == "started" and mode["kind"] != "asleep":
                        await websocket.send_json({"type": "speech_started"})
                    if audio is not None:
                        current = dict(mode)
                        if current["kind"] != "asleep":
                            mode.clear()
                            mode["kind"] = "asleep"
                            await websocket.send_json({"type": "processing"})
                        task = asyncio.create_task(handle_utterance(audio, current))
                        pending.add(task)
                        task.add_done_callback(pending.discard)
                    continue
                try:
                    command = json.loads(message.get("text") or "")
                except json.JSONDecodeError:
                    raise VoiceLimitError("MALFORMED_FRAME") from None
                kind = command.get("type")
                if kind == "stop":
                    break
                if kind == "sleep":
                    mode.clear()
                    mode["kind"] = "asleep"
                elif kind == "listen":
                    # Dimon just asked something or finished speaking: the next
                    # utterance needs no wake word.
                    expect = command.get("expect", "command")
                    if expect == "command":
                        awake("command")
                    elif expect in {"confirm", "account", "choice", "name"}:
                        options = [str(option)[:80] for option in (command.get("options") or [])][:6]
                        awake("reply", expect=expect, options=options)
                    elif expect == "approval" and isinstance(command.get("challenge_id"), str):
                        awake("approval", challenge_id=command["challenge_id"])
                    elif expect == "enroll" and isinstance(command.get("enrollment_id"), str):
                        awake("enroll", enrollment_id=command["enrollment_id"])
                    else:
                        raise VoiceLimitError("MALFORMED_FRAME")
                    segmenter.reset()
                    await websocket.send_json({"type": "listening", "expect": expect})
                else:
                    raise VoiceLimitError("MALFORMED_FRAME")
        except WebSocketDisconnect:
            pass
        except VoiceError as exc:
            with contextlib.suppress(Exception):
                await websocket.send_json({"type": "error", "reason_code": exc.reason_code})
        finally:
            for task in pending:
                task.cancel()
            with contextlib.suppress(Exception):
                await websocket.close(code=1000)

    @staticmethod
    async def _approval(websocket, database, current: dict[str, Any], audio: bytes, transcript: str) -> None:
        try:
            result = await asyncio.to_thread(
                database.verify_voice, current["challenge_id"], audio, transcript
            )
        except VoiceRejected as exc:
            await websocket.send_json({"type": "voice_rejected", "reason_code": exc.reason_code,
                                       "heard": transcript})
            return
        await websocket.send_json({"type": "voice_verified", **result})

    @staticmethod
    async def _enroll(websocket, database, current: dict[str, Any], audio: bytes, transcript: str) -> None:
        try:
            result = await asyncio.to_thread(
                database.add_voice_enrollment_sample, current["enrollment_id"], audio
            )
        except VoiceRejected as exc:
            await websocket.send_json({"type": "enroll_rejected", "reason_code": exc.reason_code})
            return
        await websocket.send_json({"type": "enroll_sample", **result})

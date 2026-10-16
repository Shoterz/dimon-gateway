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
import collections
import contextlib
import difflib
import inspect
import json
import logging
import math
import re
import struct
import time
from typing import Any, Awaitable, Callable
from uuid import uuid4

from starlette.websockets import WebSocketDisconnect

from . import voice_dialog
from .live_transcription import LiveTranscriptionSession
from .voice import VoiceError, VoiceLimitError, nemotron_failure_phase
from .voice_trace import VoiceTraceRejected, VoiceTraceStore
from .receipts import DEMO_USER_ID
from .spoken_normalization import normalize_spoken_request
from .voiceprint import ENROLL_PROMPTS, PRIOR_REQUEST_TTL_SECONDS, VoiceRejected

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
WAKE_SPEAKER_THRESHOLD = 0.45   # wake only; approvals use the stricter voiceprint threshold
FOLLOW_UP_SECONDS = 10.0
MAX_UTTERANCE_SECONDS = 15.0
MAX_SESSION_SECONDS = 30 * 60
PRE_ROLL_SECONDS = 0.3
MAX_QUEUED_UTTERANCES = 4
EARLY_WAKE_FINAL_TIMEOUT_SECONDS = 2.0
# Recognisers caption non-speech instead of returning nothing: "(hissing)",
# "[BLANK_AUDIO]", "[Music]", "*coughs*". Those are not words anyone said.
_NON_SPEECH = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|♪+")


ENROLL_MAX_SECONDS = 8.0


def said_an_enroll_prompt(transcript: str) -> bool:
    """A registration sample must be one of the phrases, roughly as heard,
    so Dimon's own voice, a TV or a conversation never becomes the voiceprint."""

    heard = re.findall(r"[a-z']+", transcript.casefold())
    if not heard:
        return False
    for prompt in ENROLL_PROMPTS:
        expected = re.findall(r"[a-z']+", prompt.casefold())
        if len(heard) > len(expected) + 3:
            continue  # the phrase inside other speech is not a clean sample
        if difflib.SequenceMatcher(None, heard, expected).ratio() >= 0.6                 or difflib.SequenceMatcher(None, " ".join(heard), " ".join(expected)).ratio() >= 0.75:
            return True
    return False


def spoken_words(transcript: str) -> str:
    return " ".join(_NON_SPEECH.sub(" ", transcript).split()).strip(" .,!?-")


class WakePartialMatcher:
    """Accept a bounded wake once, after a command partial shows progress."""

    def __init__(self) -> None:
        self._prior: str | None = None
        self._matched = False

    def push(self, text: str, *, final: bool = False) -> tuple[bool, str]:
        if self._matched:
            return False, ""
        cleaned = spoken_words(text)
        woken, command = voice_dialog.live_wake(cleaned)
        if not woken:
            self._prior = None
            return False, ""
        normalized = " ".join(re.findall(r"[a-z0-9']+", cleaned.casefold()))
        if not command:
            self._matched = True
            return True, ""
        progressed = self._prior is not None and normalized.startswith(self._prior + " ")
        self._prior = normalized
        if final or progressed:
            self._matched = True
            return True, command
        return False, ""


class Segmenter:
    """Energy-based utterance splitting for a continuous stream.

    The loudness that counts as speech follows the room: it stays a few times
    above the background (a fan, a TV, traffic), so steady noise neither starts
    an utterance nor keeps one from ending."""

    NOISE_MULTIPLE = 3.0
    MAX_THRESHOLD = 0.08

    def __init__(self, *, threshold: float = 0.015, start_seconds: float = 0.12,
                 end_silence_seconds: float = 0.7, max_seconds: float = MAX_UTTERANCE_SECONDS):
        self.threshold = threshold
        self.start_samples = int(SAMPLE_RATE * start_seconds)
        self.end_samples = int(SAMPLE_RATE * end_silence_seconds)
        self.max_samples = int(SAMPLE_RATE * max_seconds)
        self.pre_roll = int(SAMPLE_RATE * PRE_ROLL_SECONDS) * 4
        # Loudness of recent audio between utterances. The background is the
        # quietest of it, so speech (always louder) never raises the bar.
        self.recent_levels: collections.deque[float] = collections.deque(maxlen=64)
        self.reset()

    def level(self) -> float:
        if not self.recent_levels:
            return self.threshold
        floor = min(self.recent_levels)
        return min(self.MAX_THRESHOLD, max(self.threshold, floor * self.NOISE_MULTIPLE))

    def reset(self) -> None:
        self.history = bytearray()     # recent audio before speech started
        self.buffer = bytearray()
        self.speech = 0
        self.silence = 0
        self.active = False

    def active_audio(self) -> bytes:
        return bytes(self.buffer) if self.active else b""

    def set_max_seconds(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("max utterance seconds must be positive")
        self.max_samples = int(SAMPLE_RATE * seconds)

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
        rms = math.sqrt(sum(v * v for v in values) / count)
        if not self.active:
            self.recent_levels.append(rms)
        loud = rms >= self.level()
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


def enqueue_latest_utterance(
    queue: asyncio.Queue[tuple[bytes, dict[str, Any]]],
    audio: bytes,
    current: dict[str, Any],
) -> list[dict[str, Any]]:
    """Queue active turns losslessly, but retain only the latest idle audio."""

    def drain() -> list[tuple[bytes, dict[str, Any]]]:
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        return items

    discarded_items: list[dict[str, Any]] = []

    def discard(item: tuple[bytes, dict[str, Any]]) -> None:
        state = item[1]
        if state.get("live_session") is not None or state.get("live_state") is not None:
            discarded_items.append({
                "live_session": state.get("live_session"),
                "live_state": state.get("live_state"),
            })

    if current.get("kind", "asleep") == "asleep":
        # CPU Whisper takes a few seconds per pass. Room chatter must never
        # leave the real wake phrase behind a multi-item stale queue.
        for item in drain():
            if item[1].get("kind") != "asleep":
                queue.put_nowait(item)
            else:
                discard(item)
    if queue.qsize() >= MAX_QUEUED_UTTERANCES:
        drained = drain()
        kept = [item for item in drained if item[1].get("kind") != "asleep"]
        for item in drained:
            if item not in kept:
                discard(item)
        for item in kept[-(MAX_QUEUED_UTTERANCES - 1):]:
            queue.put_nowait(item)
    queue.put_nowait((audio, current))
    return discarded_items


class HandsFreeBridge:
    def __init__(self, *, recognizer: Any, lease: Any, transcribe_wait: float = 30.0,
                 wake_recognizer: Any = None, wake_lease: Any = None,
                 fast_wake_recognizer: Any = None, fast_wake_lease: Any = None,
                 wake_speaker_timeout: float = 1.0,
                 wake_finalize_timeout: float = EARLY_WAKE_FINAL_TIMEOUT_SECONDS,
                 trace_store: VoiceTraceStore | None = None):
        if wake_speaker_timeout <= 0 or wake_finalize_timeout <= 0:
            raise ValueError("wake timeouts must be positive")
        self.recognizer = recognizer
        self.lease = lease
        self.transcribe_wait = transcribe_wait
        # Listening for "Hey Dimon" transcribes everything said nearby, and
        # voice registration, spoken approvals and short answers add more. A
        # local recogniser does all of that without spending the main one's
        # budget (a remote gateway allows only a few transcriptions a minute);
        # only requests go to the main recogniser.
        self.wake_recognizer = wake_recognizer
        self.wake_lease = wake_lease
        self.fast_wake_recognizer = fast_wake_recognizer
        self.fast_wake_lease = fast_wake_lease
        self.wake_speaker_timeout = wake_speaker_timeout
        self.wake_finalize_timeout = wake_finalize_timeout
        self.trace_store = trace_store

    async def _transcribe(
        self,
        audio: bytes,
        *,
        local: bool = False,
        on_partial: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        if local and self.wake_recognizer is not None:
            try:
                return await self._transcribe_with(
                    self.wake_recognizer, self.wake_lease, audio, on_partial=on_partial
                )
            except Exception:  # noqa: BLE001 - the main recogniser still works
                logger.warning("wake_recognizer_failed")
        return await self._transcribe_with(
            self.recognizer, self.lease, audio, on_partial=on_partial
        )

    async def _transcribe_with(
        self,
        recognizer: Any,
        lease: Any,
        audio: bytes,
        *,
        on_partial: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        # The local speech model serves one utterance at a time; wait for it
        # (another voice session may be using it) instead of holding it for
        # the whole hands-free session.
        deadline = time.monotonic() + self.transcribe_wait
        while not await lease.acquire():
            if time.monotonic() >= deadline:
                raise VoiceError("VOICE_BUSY")
            await asyncio.sleep(0.05)
        try:
            async def ignore(_: str) -> None:
                return None

            return (await recognizer.transcribe(audio, on_partial or ignore)).strip()
        finally:
            lease.release()

    async def _request_words(self, audio: bytes, heard: str) -> str:
        """The request after "Hey Dimon", from the main recogniser when the
        wake check ran on the local one, so requests keep its accuracy."""

        try:
            woken, command = voice_dialog.wake(spoken_words(await self._transcribe(audio)))
        except Exception:  # noqa: BLE001 - the wake transcript is still usable
            return heard
        return command if woken and command else heard

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
        # The last request spoken in this session: its longer audio backs up
        # the short "Yes Confirm" voice check (voiceprint.CONFIRM_MATCH_THRESHOLD).
        last_request: dict[str, Any] = {}
        # Utterances are transcribed one at a time, in the order they were
        # spoken, so a reply can never overtake the request it answers.
        queue: asyncio.Queue[tuple[bytes, dict[str, Any]]] = asyncio.Queue()
        startup_prebuffer_ms = 0
        utterance_sequence = 0
        conversation_context: dict[str, Any] | None = None
        trace_id: str | None = None
        inflight_state: dict[str, Any] | None = None

        def trace_event(state: dict[str, Any] | None, name: str) -> None:
            if self.trace_store is None or state is None:
                return
            bound_trace_id = state.get("trace_id")
            attempt_id = state.get("attempt_id")
            if not isinstance(bound_trace_id, str):
                return
            seen = state.setdefault("trace_events", set())
            if name in seen:
                return
            try:
                self.trace_store.record_server(
                    bound_trace_id,
                    name,
                    attempt_id=attempt_id if isinstance(attempt_id, str) else None,
                )
                seen.add(name)
            except VoiceTraceRejected:
                logger.warning("voice_trace_event_rejected name=%s", name)

        def trace_finish(
            state: dict[str, Any] | None,
            path: str,
            failure_phase: str | None = None,
        ) -> None:
            if self.trace_store is None or state is None or state.get("trace_finished"):
                return
            bound_trace_id = state.get("trace_id")
            attempt_id = state.get("attempt_id")
            if not isinstance(bound_trace_id, str) or not isinstance(attempt_id, str):
                return
            try:
                self.trace_store.finish_attempt(
                    bound_trace_id,
                    attempt_id,
                    path=path,
                    failure_phase=failure_phase,
                    recognizer_disagreement=bool(
                        state.get("transcript_disagreement", False)
                    ),
                    faster_whisper_confidence=state.get(
                        "faster_whisper_confidence"
                    ),
                    faster_whisper_avg_logprob=state.get(
                        "faster_whisper_avg_logprob"
                    ),
                    faster_whisper_no_speech_prob=state.get(
                        "faster_whisper_no_speech_prob"
                    ),
                )
                state["trace_finished"] = True
            except VoiceTraceRejected:
                logger.warning("voice_trace_finish_rejected path=%s", path)

        def trace_fields(state: dict[str, Any] | None) -> dict[str, str]:
            if state is None:
                return {}
            fields = {}
            if isinstance(state.get("trace_id"), str):
                fields["trace_id"] = state["trace_id"]
            if isinstance(state.get("attempt_id"), str):
                fields["attempt_id"] = state["attempt_id"]
            return fields

        def fallback_phase(exc: BaseException) -> str:
            phase = nemotron_failure_phase(getattr(exc, "failure_stage", ""))
            return f"nemotron_{phase}"

        def awake(kind: str = "command", **extra: Any) -> None:
            mode.clear()
            mode.update({"kind": kind, "until": time.monotonic() + FOLLOW_UP_SECONDS, **extra})

        async def reject_locked_session() -> bool:
            lock_status = getattr(database, "lock_status", None)
            if not callable(lock_status):
                return False
            status = await asyncio.to_thread(lock_status)
            if not status.get("locked"):
                return False
            await websocket.send_json({"type": "error", "reason_code": "APP_LOCKED"})
            await websocket.close(code=1008)
            return True

        async def handle_utterance(audio: bytes, current: dict[str, Any]) -> None:
            if await reject_locked_session():
                return
            kind = current.get("kind", "asleep")
            # A reply must start inside the follow-up window; it may take up to
            # one utterance length to finish.
            if kind != "asleep" and time.monotonic() > current.get("until", 0) + MAX_UTTERANCE_SECONDS + 2:
                kind = "asleep"
            seconds = len(audio) / 4 / SAMPLE_RATE
            wake_started = float(current.get("utterance_ended_at", time.monotonic()))
            prebuffer_ms = int(current.get("prebuffer_ms", 0))
            live_session = current.get("live_session")
            live_state = current.get("live_state") or {}
            fast_wake_sent = bool(live_state.get("wake_emitted"))
            authoritative_started = time.monotonic()
            speaker_task: asyncio.Task[float | None] | None = None
            # A live stream performs its own bounded speaker check as soon as
            # the wake partial arrives. Defer the authoritative full-audio
            # check until its final transcript rather than leaving a detached
            # executor task behind if the browser disconnects after waking.
            if kind == "asleep" and voice_status["voice_registered"] and live_session is None:
                speaker_task = asyncio.create_task(asyncio.to_thread(
                    database.voice_store.speaker_similarity, DEMO_USER_ID, audio
                ))

            async def speaker_result() -> tuple[float | None, str]:
                similarity = await speaker_task if speaker_task is not None else None
                speaker = "UNKNOWN" if similarity is None else (
                    "MATCH" if similarity >= WAKE_SPEAKER_THRESHOLD else "MISMATCH"
                )
                return similarity, speaker

            async def complete_degraded_wake() -> bool:
                nonlocal fast_wake_sent
                if not (fast_wake_sent or live_state.get("wake_emitted")):
                    return False
                fast_wake_sent = True
                trace_event(live_state, "recognition_complete")
                await websocket.send_json({
                    "type": "wake_complete",
                    "speaker": live_state.get("speaker_state", "UNKNOWN"),
                    "path": "nemotron_partial",
                    "degraded_final": True,
                    **trace_fields(live_state),
                })
                trace_finish(
                    live_state,
                    "nemotron_partial",
                    live_state.get("nemotron_failure_phase"),
                )
                return True

            try:
                async def partial(text: str) -> None:
                    cleaned = spoken_words(text)
                    if cleaned:
                        await websocket.send_json({"type": "transcript_partial", "text": cleaned})

                if kind == "asleep" and live_session is not None:
                    try:
                        transcript = spoken_words(await asyncio.wait_for(
                            live_session.finish(), timeout=self.wake_finalize_timeout,
                        ))
                        for field in (
                            "recognition_path",
                            "transcript_disagreement",
                            "faster_whisper_confidence",
                            "faster_whisper_avg_logprob",
                            "faster_whisper_no_speech_prob",
                        ):
                            value = getattr(live_session, field, None)
                            if value is not None:
                                live_state[field] = value
                        if live_session.nemotron_failure_phase is not None:
                            live_state["nemotron_failure_phase"] = (
                                live_session.nemotron_failure_phase
                            )
                            trace_event(live_state, "failure")
                    except Exception as exc:  # noqa: BLE001 - buffered CPU wake remains available
                        logger.warning("live_wake_recognizer_failed", exc_info=True)
                        live_state["nemotron_failure_phase"] = (
                            "nemotron_stream_closed"
                            if isinstance(exc, TimeoutError)
                            else fallback_phase(exc)
                        )
                        trace_event(live_state, "failure")
                        if await complete_degraded_wake():
                            # A confident partial has already passed the wake
                            # and speaker gates. Do not make it wait on another
                            # recognizer: finish the bare-wake handshake now.
                            return
                        transcript = spoken_words(await self._transcribe(audio, local=True))
                    finally:
                        # The caption callback can emit while finish() waits for
                        # the authoritative final. Reconcile after it returns so
                        # that final handling cannot send a duplicate wake.
                        fast_wake_sent = fast_wake_sent or bool(live_state.get("wake_emitted"))
                elif kind == "asleep" and self.fast_wake_recognizer is not None:
                    matcher = WakePartialMatcher()

                    async def wake_partial(text: str) -> None:
                        nonlocal fast_wake_sent
                        if live_state.get("first_partial_at") is None:
                            live_state["first_partial_at"] = time.monotonic()
                            trace_event(live_state, "first_partial")
                        matched, _ = matcher.push(text)
                        if not matched or fast_wake_sent:
                            return
                        trace_event(live_state, "wake_match")
                        match_ms = max(0, round((time.monotonic() - wake_started) * 1000))
                        _, partial_speaker = await speaker_result()
                        trace_event(live_state, "speaker_check")
                        if voice_status["voice_registered"] and partial_speaker != "MATCH":
                            return
                        fast_wake_sent = True
                        live_state["speaker_state"] = partial_speaker
                        awake("command")
                        logger.warning(json.dumps({
                            "event": "hands_free_wake",
                            "wake_path": "nemotron_partial",
                            "speech_end_to_match_ms": match_ms,
                            "speech_end_to_wake_ms": max(0, round((time.monotonic() - wake_started) * 1000)),
                            "prebuffer_ms": prebuffer_ms,
                        }, sort_keys=True, separators=(",", ":")))
                        trace_event(live_state, "wake_sent")
                        await websocket.send_json({
                            "type": "wake", "speaker": partial_speaker,
                            "path": "nemotron_partial",
                            **trace_fields(live_state),
                        })

                    try:
                        transcript = spoken_words(await self._transcribe_with(
                            self.fast_wake_recognizer, self.fast_wake_lease, audio,
                            on_partial=wake_partial,
                        ))
                    except Exception as exc:  # noqa: BLE001 - CPU wake is the outage fallback
                        logger.warning("fast_wake_recognizer_failed")
                        live_state["nemotron_failure_phase"] = fallback_phase(exc)
                        trace_event(live_state, "failure")
                        if await complete_degraded_wake():
                            return
                        transcript = spoken_words(await self._transcribe(audio, local=True))
                else:
                    transcript = spoken_words(
                        await self._transcribe(
                            audio,
                            local=kind in {"asleep", "approval", "enroll"},
                            on_partial=partial if kind in {"reply", "command"} else None,
                        )
                    )
            except (VoiceError, Exception) as exc:  # noqa: BLE001 - reported, never fatal
                reason = exc.reason_code if isinstance(exc, VoiceError) else "TRANSCRIPTION_FAILED"
                logger.warning("hands_free_utterance kind=%s seconds=%.1f failed=%s", kind, seconds, reason)
                trace_event(live_state, "failure")
                trace_finish(live_state, "failed", "faster_whisper_unavailable")
                await websocket.send_json({"type": "error", "reason_code": reason})
                return
            if await reject_locked_session():
                return
            logger.warning(json.dumps({
                "event": "hands_free_transcription_timing",
                "utterance_id": live_state.get("utterance_id"),
                "authoritative_final_ms": max(
                    0, round((time.monotonic() - authoritative_started) * 1000)
                ),
                "endpoint_reason": "cap" if seconds >= 2.45 and kind == "asleep" else "silence_or_finish",
                "selected_path": (
                    "nemotron_partial" if fast_wake_sent else
                    live_state.get("recognition_path", "faster_whisper_final")
                ),
            }, sort_keys=True, separators=(",", ":")))
            trace_event(live_state, "authoritative_final")
            trace_event(live_state, "recognition_complete")
            # Never the words: only what kind of utterance it was and whether
            # anything was heard, so a failure can be traced.
            logger.warning("hands_free_utterance kind=%s seconds=%.1f words=%d woke=%s", kind, seconds,
                           len(transcript.split()), voice_dialog.wake(transcript)[0] if kind == "asleep" else "-")
            if not transcript:
                trace_finish(live_state, "failed", "no_speech")
                if kind == "asleep":
                    await websocket.send_json({"type": "not_woken"})
                return
            if kind == "approval":
                trace_finish(live_state, "faster_whisper_final")
                prior = last_request.get("audio") \
                    if time.monotonic() - last_request.get("at", 0) <= PRIOR_REQUEST_TTL_SECONDS else None
                await self._approval(websocket, database, current, audio, transcript, prior)
                return
            if kind == "enroll":
                trace_finish(live_state, "faster_whisper_final")
                await self._enroll(websocket, database, current, audio, transcript)
                return
            if speaker_task is not None:
                similarity, speaker = await speaker_result()
            else:
                similarity = await asyncio.to_thread(
                    database.voice_store.speaker_similarity, DEMO_USER_ID, audio
                ) if voice_status["voice_registered"] else None
                speaker = "UNKNOWN" if similarity is None else (
                    "MATCH" if similarity >= WAKE_SPEAKER_THRESHOLD else "MISMATCH"
                )
            trace_event(live_state, "speaker_check")
            if kind == "asleep":
                woken, command = voice_dialog.wake(transcript)
                if not woken:
                    trace_finish(live_state, "failed", "wake_not_matched")
                    # Not for Dimon: dropped. Only the fact is reported, never the words.
                    await websocket.send_json({"type": "not_woken"})
                    return
                if not fast_wake_sent:
                    trace_event(live_state, "wake_match")
                if speaker == "MISMATCH":
                    trace_finish(live_state, "failed", "speaker_rejected")
                    await websocket.send_json({"type": "ignored", "reason_code": "SPEAKER_MISMATCH"})
                    return
                if voice_status["voice_registered"] and speaker != "MATCH":
                    trace_finish(live_state, "failed", "speaker_unverified")
                    await websocket.send_json({"type": "ignored", "reason_code": "SPEAKER_UNVERIFIED"})
                    return
                if not command:
                    final_path = live_state.get(
                        "recognition_path", "faster_whisper_final"
                    )
                    if not fast_wake_sent:
                        awake("command")
                        logger.warning(json.dumps({
                            "event": "hands_free_wake",
                            "wake_path": final_path,
                            "speech_end_to_match_ms": max(0, round((time.monotonic() - wake_started) * 1000)),
                            "speech_end_to_wake_ms": max(0, round((time.monotonic() - wake_started) * 1000)),
                            "prebuffer_ms": prebuffer_ms,
                        }, sort_keys=True, separators=(",", ":")))
                        await websocket.send_json({
                            "type": "wake", "speaker": speaker,
                            "path": final_path,
                            **trace_fields(live_state),
                        })
                        trace_event(live_state, "wake_sent")
                    else:
                        # The browser already opened Ask on the partial, but it
                        # kept sending audio so Faster-Whisper could decide
                        # whether a command followed. This final event means it
                        # was a bare wake and the greeting may safely begin.
                        await websocket.send_json({
                            "type": "wake_complete", "speaker": speaker,
                            "path": final_path,
                            **trace_fields(live_state),
                        })
                    trace_finish(
                        live_state,
                        "nemotron_partial" if fast_wake_sent else final_path,
                        None if fast_wake_sent else live_state.get("nemotron_failure_phase"),
                    )
                    return
                if self.wake_recognizer is not None and self.fast_wake_recognizer is None:
                    command = await self._request_words(audio, command)
                transcript, kind = command, "command"
                final_path = live_state.get(
                    "recognition_path", "faster_whisper_final"
                )
                if not fast_wake_sent:
                    logger.warning(json.dumps({
                        "event": "hands_free_wake",
                        "wake_path": final_path,
                        "speech_end_to_match_ms": max(0, round((time.monotonic() - wake_started) * 1000)),
                        "speech_end_to_wake_ms": max(0, round((time.monotonic() - wake_started) * 1000)),
                        "prebuffer_ms": prebuffer_ms,
                    }, sort_keys=True, separators=(",", ":")))
                    await websocket.send_json({
                        "type": "wake", "speaker": speaker, "command": command,
                        "path": final_path,
                        **trace_fields(live_state),
                    })
                    trace_event(live_state, "wake_sent")
                trace_finish(
                    live_state,
                    "nemotron_partial" if fast_wake_sent else final_path,
                    None if fast_wake_sent else live_state.get("nemotron_failure_phase"),
                )
            elif speaker == "MISMATCH":
                trace_finish(live_state, "faster_whisper_final")
                await websocket.send_json({"type": "ignored", "reason_code": "SPEAKER_MISMATCH"})
                return
            else:
                trace_finish(live_state, "faster_whisper_final")
            evidence = {
                "transcript": transcript,
                "speaker": speaker,
                "speaker_similarity": similarity,
                "audio_seconds": round(len(audio) / 4 / SAMPLE_RATE, 2),
                "encoding": "float32le/16000",
                "hands_free": True,
            }
            bound_conversation = current.get("conversation_context")
            if isinstance(bound_conversation, dict):
                evidence["conversation_id"] = bound_conversation["conversation_id"]
                evidence["expected_version"] = bound_conversation["expected_version"]
            if kind == "reply":
                value = voice_dialog.interpret(current.get("expect", ""), transcript, current.get("options") or ())
                mode.clear()
                mode["kind"] = "asleep"
                await websocket.send_json({"type": "reply", "text": transcript, "value": value,
                                           "expect": current.get("expect")})
                return
            mode.clear()
            mode["kind"] = "asleep"
            steer = voice_dialog.control(transcript)
            if steer is not None:
                # "Hey Dimon, cancel" / "Hey Dimon, continue": steers the
                # confirmation on screen; it is never sent as a request.
                await websocket.send_json({"type": "control", "action": steer, "text": transcript})
                return
            original_transcript = transcript
            transcript = normalize_spoken_request(original_transcript)
            if transcript != original_transcript:
                evidence["transcript"] = original_transcript
                evidence["normalized_transcript"] = transcript
            await websocket.send_json({"type": "heard", "text": transcript})
            last_request.update(audio=audio, at=time.monotonic())
            result = dispatch(transcript, evidence)
            if inspect.isawaitable(result):
                result = await result
            if (
                isinstance(result, dict)
                and result.get("outcome") == "BLOCKED"
                and result.get("reason_code") == "APP_LOCKED"
            ):
                await websocket.send_json({"type": "error", "reason_code": "APP_LOCKED"})
                await websocket.close(code=1008)
                return
            await websocket.send_json({"type": "action_result", "text": transcript, "result": result})

        async def worker() -> None:
            nonlocal inflight_state
            while True:
                audio, current = await queue.get()
                inflight_state = current.get("live_state")
                try:
                    await handle_utterance(audio, current)
                except (WebSocketDisconnect, RuntimeError):
                    return
                except Exception:  # noqa: BLE001 - one bad utterance must not end the session
                    with contextlib.suppress(Exception):
                        await websocket.send_json({"type": "error", "reason_code": "UTTERANCE_FAILED"})
                finally:
                    trace_finish(inflight_state, "cancelled", "cancelled")
                    inflight_state = None

        async def enqueue(audio: bytes, current: dict[str, Any]) -> None:
            for discarded in enqueue_latest_utterance(queue, audio, current):
                session = discarded.get("live_session")
                if session is not None:
                    await session.cancel()
                trace_finish(
                    discarded.get("live_state"), "cancelled", "queue_superseded"
                )

        worker_task = asyncio.create_task(worker())
        active_live_session: LiveTranscriptionSession | None = None
        active_live_state: dict[str, Any] | None = None
        try:
            while True:
                if time.monotonic() - started > MAX_SESSION_SECONDS:
                    await websocket.send_json({"type": "closed", "reason_code": "SESSION_TIME_LIMIT"})
                    break
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    chunk = message["bytes"]
                    was_active = segmenter.active
                    if was_active and active_live_state is not None \
                            and active_live_state.get("wake_emitted"):
                        # A recognized wake followed by more words is now a
                        # command turn, not wake-only room audio. Let the user
                        # finish the request instead of cutting it at 2.5 s.
                        segmenter.set_max_seconds(MAX_UTTERANCE_SECONDS)
                    if not was_active:
                        segmenter.set_max_seconds(
                            2.5 if mode.get("kind", "asleep") == "asleep" else MAX_UTTERANCE_SECONDS
                        )
                    event, audio = segmenter.push(chunk)
                    if was_active and active_live_session is not None:
                        if active_live_state is not None:
                            active_live_state["speaker_audio"].extend(chunk)
                        try:
                            await active_live_session.push(chunk)
                        except Exception as exc:  # noqa: BLE001 - final buffered fallback remains available
                            logger.warning("live_wake_audio_push_failed", exc_info=True)
                            await active_live_session.cancel()
                            active_live_session = None
                            if active_live_state is not None:
                                active_live_state["failed"] = True
                                active_live_state["nemotron_failure_phase"] = fallback_phase(exc)
                                trace_event(active_live_state, "failure")
                    if event == "started":
                        utterance_mode = dict(mode)
                        utterance_sequence += 1
                        state = {
                            "mode": utterance_mode,
                            "utterance_id": utterance_sequence,
                            "wake_emitted": False,
                            "started_at": time.monotonic(),
                            "partial_matched_at": None,
                            "first_partial_at": None,
                            "speaker_state": (
                                "PENDING" if voice_status["voice_registered"] else "UNREGISTERED"
                            ),
                            "stream_state": "NOT_STARTED",
                            "speaker_audio": bytearray(segmenter.active_audio()),
                            "cancelled": False,
                            "speaker_check_inflight": False,
                            "trace_id": trace_id,
                            "attempt_id": str(uuid4()) if trace_id is not None else None,
                            "trace_finished": False,
                        }
                        active_live_state = state
                        if self.trace_store is not None and trace_id is not None:
                            try:
                                self.trace_store.start_attempt(trace_id, state["attempt_id"])
                                trace_event(state, "speech_detected")
                            except VoiceTraceRejected:
                                logger.warning("voice_trace_attempt_rejected")
                                state["trace_id"] = None
                                state["attempt_id"] = None
                        await websocket.send_json({
                            "type": "speech_started",
                            **trace_fields(state),
                        })
                        if (
                            utterance_mode.get("kind", "asleep") == "asleep"
                            and self.fast_wake_recognizer is not None
                            and callable(getattr(self.fast_wake_recognizer, "open_stream", None))
                        ):
                            matcher = WakePartialMatcher()
                            state["stream_state"] = "OPENING"

                            async def live_partial(
                                text: str,
                                state: dict[str, Any] = state,
                                matcher: WakePartialMatcher = matcher,
                            ) -> None:
                                if state.get("wake_emitted") or state.get("cancelled"):
                                    return
                                now = time.monotonic()
                                if state.get("first_partial_at") is None:
                                    state["first_partial_at"] = now
                                    trace_event(state, "first_partial")
                                matched, _command = matcher.push(text)
                                if matched and state.get("partial_matched_at") is None:
                                    state["partial_matched_at"] = now
                                    trace_event(state, "wake_match")
                                if state.get("partial_matched_at") is None:
                                    return
                                if state.get("speaker_check_inflight"):
                                    return
                                speaker = "UNKNOWN"
                                speaker_check_started = time.monotonic()
                                if voice_status["voice_registered"]:
                                    state["speaker_check_inflight"] = True
                                    try:
                                        similarity = await asyncio.wait_for(
                                            asyncio.to_thread(
                                                database.voice_store.speaker_similarity,
                                                DEMO_USER_ID,
                                                bytes(state["speaker_audio"]),
                                            ),
                                            timeout=self.wake_speaker_timeout,
                                        )
                                    except asyncio.TimeoutError:
                                        state["speaker_state"] = "UNKNOWN"
                                        return
                                    finally:
                                        state["speaker_check_inflight"] = False
                                    if state.get("cancelled") or state.get("wake_emitted"):
                                        return
                                    speaker = "UNKNOWN" if similarity is None else (
                                        "MATCH" if similarity >= WAKE_SPEAKER_THRESHOLD else "MISMATCH"
                                    )
                                    state["speaker_similarity"] = similarity
                                    state["speaker_state"] = speaker
                                    if speaker != "MATCH":
                                        trace_event(state, "speaker_check")
                                        return
                                trace_event(state, "speaker_check")
                                state["speaker_state"] = speaker
                                state["wake_emitted"] = True
                                awake("command")
                                elapsed_ms = max(0, round((time.monotonic() - state["started_at"]) * 1000))
                                logger.warning(json.dumps({
                                    "event": "hands_free_wake",
                                    "wake_path": "nemotron_partial",
                                    "utterance_start_to_wake_ms": elapsed_ms,
                                    "utterance_start_to_partial_ms": max(0, round(
                                        (state["first_partial_at"] - state["started_at"]) * 1000
                                    )),
                                    "wake_match_ms": max(0, round(
                                        (state["partial_matched_at"] - state["started_at"]) * 1000
                                    )),
                                    "speaker_check_ms": max(0, round(
                                        (time.monotonic() - speaker_check_started) * 1000
                                    )),
                                    "prebuffer_ms": startup_prebuffer_ms,
                                }, sort_keys=True, separators=(",", ":")))
                                trace_event(state, "wake_sent")
                                await websocket.send_json({
                                    "type": "wake", "speaker": speaker, "path": "nemotron_partial",
                                    **trace_fields(state),
                                })

                            async def live_ready(
                                stream: Any,
                                state: dict[str, Any] = state,
                            ) -> None:
                                state["stream_state"] = getattr(
                                    stream, "nemotron_state", "READY"
                                )
                                phase = getattr(stream, "nemotron_failure_phase", None)
                                if isinstance(phase, str):
                                    state["nemotron_failure_phase"] = phase
                                    trace_event(state, "failure")
                                else:
                                    trace_event(state, "nemotron_stream_ready")

                            active_live_session = LiveTranscriptionSession(
                                self.fast_wake_recognizer,
                                self.fast_wake_lease,
                                live_partial,
                                acquire_timeout=self.transcribe_wait,
                                max_pending_bytes=int(SAMPLE_RATE * 4 * 3.0),
                                trace_id=state.get("trace_id"),
                                attempt_id=state.get("attempt_id"),
                                on_stream_ready=live_ready,
                            )
                            try:
                                await active_live_session.start()
                                await active_live_session.push(segmenter.active_audio())
                            except Exception as exc:  # noqa: BLE001 - final buffered fallback remains available
                                logger.warning("live_wake_start_failed", exc_info=True)
                                await active_live_session.cancel()
                                active_live_session = None
                                active_live_state["failed"] = True
                                active_live_state["stream_state"] = "FAILED"
                                active_live_state["nemotron_failure_phase"] = fallback_phase(exc)
                                trace_event(active_live_state, "failure")
                    if audio is not None:
                        utterance_mode = (
                            dict(active_live_state["mode"])
                            if active_live_state is not None
                            else dict(mode)
                        )
                        current = {
                            **utterance_mode,
                            "conversation_context": (
                                dict(conversation_context) if conversation_context else None
                            ),
                            "prebuffer_ms": startup_prebuffer_ms,
                            "utterance_ended_at": time.monotonic(),
                            "live_session": active_live_session,
                            "live_state": active_live_state,
                        }
                        active_live_session = None
                        active_live_state = None
                        startup_prebuffer_ms = 0
                        if current["kind"] != "asleep":
                            mode.clear()
                            mode["kind"] = "asleep"
                        await websocket.send_json({"type": "processing"})
                        await enqueue(audio, current)
                    continue
                try:
                    command = json.loads(message.get("text") or "")
                except json.JSONDecodeError:
                    raise VoiceLimitError("MALFORMED_FRAME") from None
                kind = command.get("type")
                if kind == "trace_start":
                    if set(command) != {"type", "trace_id"} or self.trace_store is None:
                        raise VoiceLimitError("MALFORMED_FRAME")
                    proposed_trace_id = command.get("trace_id")
                    try:
                        self.trace_store.start(proposed_trace_id)
                    except VoiceTraceRejected as exc:
                        if str(exc) != "TRACE_ALREADY_EXISTS":
                            raise VoiceLimitError("MALFORMED_FRAME") from None
                    trace_id = proposed_trace_id
                    await websocket.send_json({"type": "trace_bound", "trace_id": trace_id})
                    continue
                if kind == "trace_event":
                    allowed = {"type", "trace_id", "event", "elapsed_ms", "attempt_id"}
                    if (
                        self.trace_store is None
                        or not set(command).issubset(allowed)
                        or set(command) - {"attempt_id"}
                        != {"type", "trace_id", "event", "elapsed_ms"}
                        or command.get("trace_id") != trace_id
                    ):
                        raise VoiceLimitError("MALFORMED_FRAME")
                    try:
                        self.trace_store.record_browser(
                            trace_id,
                            command.get("event"),
                            command.get("elapsed_ms"),
                            attempt_id=command.get("attempt_id"),
                        )
                    except VoiceTraceRejected:
                        raise VoiceLimitError("MALFORMED_FRAME") from None
                    continue
                if kind == "stop":
                    break
                if kind == "conversation_context":
                    conversation_id = command.get("conversation_id")
                    expected_version = command.get("expected_version")
                    if conversation_id is None and expected_version is None:
                        conversation_context = None
                        await websocket.send_json({
                            "type": "conversation_context", "status": "cleared",
                        })
                    elif (
                        isinstance(conversation_id, str)
                        and 1 <= len(conversation_id.strip()) <= 128
                        and type(expected_version) is int
                        and expected_version >= 1
                    ):
                        conversation_context = {
                            "conversation_id": conversation_id.strip(),
                            "expected_version": expected_version,
                        }
                        await websocket.send_json({
                            "type": "conversation_context", "status": "bound",
                        })
                    else:
                        raise VoiceLimitError("MALFORMED_FRAME")
                    continue
                if kind == "prebuffer_flushed":
                    duration_ms = command.get("duration_ms")
                    if type(duration_ms) is not int or not 0 <= duration_ms <= 1000:
                        raise VoiceLimitError("MALFORMED_FRAME")
                    startup_prebuffer_ms = duration_ms
                    continue
                if kind == "sleep":
                    # An early partial wake makes the browser pause capture to
                    # play the greeting. Abort that in-flight wake utterance so
                    # its recognizer lease and delayed callbacks cannot survive
                    # into the next listening turn.
                    if active_live_state is not None:
                        active_live_state["cancelled"] = True
                        trace_event(active_live_state, "cancelled")
                        trace_finish(active_live_state, "cancelled", "cancelled")
                    if active_live_session is not None:
                        await active_live_session.cancel()
                    active_live_session = None
                    active_live_state = None
                    segmenter.reset()
                    mode.clear()
                    mode["kind"] = "asleep"
                elif kind == "listen":
                    # Dimon just asked something or finished speaking: the next
                    # utterance needs no wake word.
                    expect = command.get("expect", "command")
                    if expect == "command":
                        awake("command")
                    elif expect in {"confirm", "account", "choice", "name", "answer"}:
                        options = [str(option)[:80] for option in (command.get("options") or [])][:6]
                        awake("reply", expect=expect, options=options)
                    elif expect == "approval" and isinstance(command.get("challenge_id"), str):
                        awake("approval", challenge_id=command["challenge_id"])
                    elif expect == "enroll" and isinstance(command.get("enrollment_id"), str):
                        awake("enroll", enrollment_id=command["enrollment_id"])
                    else:
                        raise VoiceLimitError("MALFORMED_FRAME")
                    # An answer already under way (said the moment Dimon
                    # stopped talking) is kept; it takes this new mode when
                    # it ends. Only half-heard noise is dropped.
                    if not segmenter.active:
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
            if active_live_state is not None:
                active_live_state["cancelled"] = True
                trace_event(active_live_state, "cancelled")
                trace_finish(active_live_state, "cancelled", "cancelled")
            if active_live_session is not None:
                await active_live_session.cancel()
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            while not queue.empty():
                _audio, queued = queue.get_nowait()
                trace_finish(queued.get("live_state"), "cancelled", "cancelled")
                queued_session = queued.get("live_session")
                if queued_session is not None:
                    await queued_session.cancel()
            with contextlib.suppress(Exception):
                await websocket.close(code=1000)

    @staticmethod
    async def _approval(websocket, database, current: dict[str, Any], audio: bytes, transcript: str,
                        prior_request_audio: bytes | None = None) -> None:
        try:
            result = await asyncio.to_thread(
                lambda: database.verify_voice(current["challenge_id"], audio, transcript,
                                              prior_request_audio=prior_request_audio)
            )
        except VoiceRejected as exc:
            await websocket.send_json({"type": "voice_rejected", "reason_code": exc.reason_code,
                                       "heard": transcript})
            return
        await websocket.send_json({"type": "voice_verified", **result})

    @staticmethod
    async def _enroll(websocket, database, current: dict[str, Any], audio: bytes, transcript: str) -> None:
        if len(audio) / 4 / SAMPLE_RATE > ENROLL_MAX_SECONDS or not said_an_enroll_prompt(transcript):
            await websocket.send_json({"type": "enroll_rejected", "reason_code": "VOICE_PROMPT_NOT_HEARD"})
            return
        try:
            result = await asyncio.to_thread(
                database.add_voice_enrollment_sample, current["enrollment_id"], audio
            )
        except VoiceRejected as exc:
            await websocket.send_json({"type": "enroll_rejected", "reason_code": exc.reason_code})
            return
        await websocket.send_json({"type": "enroll_sample", **result})

"""Content-free, bounded timing traces for Dimon's voice pipeline."""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Callable
from uuid import UUID


BROWSER_EVENTS = frozenset(
    {
        "d_click",
        "audio_ready",
        "microphone_ready",
        "socket_ready",
        "backend_ready",
        "browser_wake_received",
        "reply_audio_ready",
        "reply_playback_started",
    }
)
SERVER_EVENTS = frozenset(
    {
        "attempt_started",
        "speech_detected",
        "nemotron_stream_ready",
        "first_partial",
        "wake_match",
        "speaker_check",
        "wake_sent",
        "authoritative_final",
        "recognition_complete",
        "attempt_finished",
        "failure",
        "cancelled",
    }
)
RECOGNITION_PATHS = frozenset(
    {"nemotron_partial", "nemotron_final", "faster_whisper_final", "cancelled", "failed"}
)
FAILURE_PHASES = frozenset(
    {
        "nemotron_connect",
        "nemotron_ready_timeout",
        "nemotron_stream_closed",
        "nemotron_protocol",
        "nemotron_empty_final",
        "nemotron_upstream_error",
        "faster_whisper_unavailable",
        "no_speech",
        "wake_not_matched",
        "speaker_rejected",
        "speaker_unverified",
        "queue_superseded",
        "cancelled",
    }
)


class VoiceTraceRejected(ValueError):
    """Raised when trace input does not match the content-free contract."""


def _uuid4(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise VoiceTraceRejected("INVALID_TRACE_ID")
    try:
        parsed = UUID(value)
    except ValueError:
        raise VoiceTraceRejected("INVALID_TRACE_ID") from None
    if parsed.version != 4 or str(parsed) != value:
        raise VoiceTraceRejected("INVALID_TRACE_ID")
    return value


def _elapsed(value: object) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
        or value > 900_000
    ):
        raise VoiceTraceRejected("INVALID_ELAPSED_TIME")
    return round(float(value), 3)


class VoiceTraceStore:
    def __init__(
        self,
        *,
        max_traces: int = 32,
        max_events: int = 64,
        max_attempts: int = 64,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(max_traces, max_events, max_attempts) <= 0:
            raise ValueError("voice trace bounds must be positive")
        self.max_traces = int(max_traces)
        self.max_events = int(max_events)
        self.max_attempts = int(max_attempts)
        self.monotonic = monotonic
        self._traces: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._lock = threading.RLock()

    @property
    def trace_count(self) -> int:
        with self._lock:
            return len(self._traces)

    def start(self, trace_id: str, **extra: object) -> dict[str, object]:
        self._reject_extra(extra)
        trace_id = _uuid4(trace_id)
        with self._lock:
            if trace_id in self._traces:
                raise VoiceTraceRejected("TRACE_ALREADY_EXISTS")
            self._traces[trace_id] = {
                "trace_id": trace_id,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "started_monotonic": float(self.monotonic()),
                "events": deque(maxlen=self.max_events),
                "attempts": OrderedDict(),
            }
            while len(self._traces) > self.max_traces:
                self._traces.popitem(last=False)
            return self._snapshot(self._traces[trace_id])

    def record_browser(
        self,
        trace_id: str,
        event: str,
        elapsed_ms: float,
        *,
        attempt_id: str | None = None,
        **extra: object,
    ) -> None:
        self._reject_extra(extra)
        if event not in BROWSER_EVENTS:
            raise VoiceTraceRejected("INVALID_BROWSER_EVENT")
        if attempt_id is not None:
            attempt_id = _uuid4(attempt_id)
        with self._lock:
            trace = self._trace(trace_id)
            if attempt_id is not None:
                self._attempt(trace, attempt_id)
            self._append_event(
                trace,
                source="browser",
                name=event,
                elapsed_ms=_elapsed(elapsed_ms),
                attempt_id=attempt_id,
            )

    def record_server(
        self,
        trace_id: str,
        event: str,
        *,
        attempt_id: str | None = None,
        **extra: object,
    ) -> None:
        self._reject_extra(extra)
        if event not in SERVER_EVENTS:
            raise VoiceTraceRejected("INVALID_SERVER_EVENT")
        if attempt_id is not None:
            attempt_id = _uuid4(attempt_id)
        with self._lock:
            trace = self._trace(trace_id)
            if attempt_id is not None:
                self._attempt(trace, attempt_id)
            self._append_event(
                trace,
                source="server",
                name=event,
                elapsed_ms=self._server_elapsed(trace),
                attempt_id=attempt_id,
            )

    def start_attempt(
        self,
        trace_id: str,
        attempt_id: str,
        **extra: object,
    ) -> None:
        self._reject_extra(extra)
        attempt_id = _uuid4(attempt_id)
        with self._lock:
            trace = self._trace(trace_id)
            attempts: OrderedDict[str, dict[str, object]] = trace["attempts"]  # type: ignore[assignment]
            if attempt_id in attempts:
                raise VoiceTraceRejected("ATTEMPT_ALREADY_EXISTS")
            if len(attempts) >= self.max_attempts:
                raise VoiceTraceRejected("TOO_MANY_ATTEMPTS")
            attempts[attempt_id] = {
                "attempt_id": attempt_id,
                "path": None,
                "failure_phase": None,
                "recognizer_disagreement": False,
                "faster_whisper_confidence": None,
                "faster_whisper_avg_logprob": None,
                "faster_whisper_no_speech_prob": None,
                "finished": False,
            }
            self._append_event(
                trace,
                source="server",
                name="attempt_started",
                elapsed_ms=self._server_elapsed(trace),
                attempt_id=attempt_id,
            )

    def finish_attempt(
        self,
        trace_id: str,
        attempt_id: str,
        *,
        path: str,
        failure_phase: str | None = None,
        recognizer_disagreement: bool = False,
        faster_whisper_confidence: str | None = None,
        faster_whisper_avg_logprob: float | None = None,
        faster_whisper_no_speech_prob: float | None = None,
        **extra: object,
    ) -> None:
        self._reject_extra(extra)
        if path not in RECOGNITION_PATHS:
            raise VoiceTraceRejected("INVALID_RECOGNITION_PATH")
        if failure_phase is not None and failure_phase not in FAILURE_PHASES:
            raise VoiceTraceRejected("INVALID_FAILURE_PHASE")
        if path == "nemotron_final" and failure_phase is not None:
            raise VoiceTraceRejected("UNEXPECTED_FAILURE_PHASE")
        if path == "nemotron_partial" and failure_phase is not None \
                and not failure_phase.startswith("nemotron_"):
            raise VoiceTraceRejected("UNEXPECTED_FAILURE_PHASE")
        if path == "faster_whisper_final" and failure_phase is not None \
                and not failure_phase.startswith("nemotron_"):
            raise VoiceTraceRejected("UNEXPECTED_FAILURE_PHASE")
        if not isinstance(recognizer_disagreement, bool):
            raise VoiceTraceRejected("INVALID_DISAGREEMENT")
        if faster_whisper_confidence not in {None, "normal", "low"}:
            raise VoiceTraceRejected("INVALID_CONFIDENCE")
        avg_logprob = self._quality(
            faster_whisper_avg_logprob, minimum=-100, maximum=0
        )
        no_speech_prob = self._quality(
            faster_whisper_no_speech_prob, minimum=0, maximum=1
        )
        with self._lock:
            trace = self._trace(trace_id)
            attempt = self._attempt(trace, _uuid4(attempt_id))
            if attempt["finished"]:
                raise VoiceTraceRejected("ATTEMPT_ALREADY_FINISHED")
            attempt["path"] = path
            attempt["failure_phase"] = failure_phase
            attempt["recognizer_disagreement"] = recognizer_disagreement
            attempt["faster_whisper_confidence"] = faster_whisper_confidence
            attempt["faster_whisper_avg_logprob"] = avg_logprob
            attempt["faster_whisper_no_speech_prob"] = no_speech_prob
            attempt["finished"] = True
            self._append_event(
                trace,
                source="server",
                name="attempt_finished",
                elapsed_ms=self._server_elapsed(trace),
                attempt_id=attempt_id,
            )

    def latest(self) -> dict[str, object]:
        with self._lock:
            if not self._traces:
                return {}
            return self._snapshot(next(reversed(self._traces.values())))

    def _trace(self, trace_id: str) -> dict[str, object]:
        trace = self._traces.get(_uuid4(trace_id))
        if trace is None:
            raise VoiceTraceRejected("TRACE_NOT_FOUND")
        return trace

    @staticmethod
    def _attempt(
        trace: dict[str, object], attempt_id: str
    ) -> dict[str, object]:
        attempts: OrderedDict[str, dict[str, object]] = trace["attempts"]  # type: ignore[assignment]
        attempt = attempts.get(attempt_id)
        if attempt is None:
            raise VoiceTraceRejected("ATTEMPT_NOT_FOUND")
        return attempt

    def _server_elapsed(self, trace: dict[str, object]) -> float:
        started = trace["started_monotonic"]
        assert isinstance(started, float)
        return _elapsed((self.monotonic() - started) * 1000)

    @staticmethod
    def _quality(
        value: object, *, minimum: float, maximum: float
    ) -> float | None:
        if value is None:
            return None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not minimum <= float(value) <= maximum
        ):
            raise VoiceTraceRejected("INVALID_RECOGNITION_QUALITY")
        return round(float(value), 4)

    @staticmethod
    def _append_event(
        trace: dict[str, object],
        *,
        source: str,
        name: str,
        elapsed_ms: float,
        attempt_id: str | None,
    ) -> None:
        events: deque[dict[str, object]] = trace["events"]  # type: ignore[assignment]
        event: dict[str, object] = {
            "source": source,
            "name": name,
            "elapsed_ms": elapsed_ms,
        }
        if attempt_id is not None:
            event["attempt_id"] = attempt_id
        events.append(event)

    @staticmethod
    def _reject_extra(extra: dict[str, object]) -> None:
        if extra:
            raise VoiceTraceRejected("TRACE_CONTENT_FORBIDDEN")

    @staticmethod
    def _snapshot(trace: dict[str, object]) -> dict[str, object]:
        events: deque[dict[str, object]] = trace["events"]  # type: ignore[assignment]
        attempts: OrderedDict[str, dict[str, object]] = trace["attempts"]  # type: ignore[assignment]
        return {
            "trace_id": trace["trace_id"],
            "started_at": trace["started_at"],
            "events": [dict(item) for item in events],
            "attempts": [
                {
                    "attempt_id": item["attempt_id"],
                    "path": item["path"],
                    "failure_phase": item["failure_phase"],
                    "recognizer_disagreement": item["recognizer_disagreement"],
                    "faster_whisper_confidence": item["faster_whisper_confidence"],
                    "faster_whisper_avg_logprob": item["faster_whisper_avg_logprob"],
                    "faster_whisper_no_speech_prob": item["faster_whisper_no_speech_prob"],
                }
                for item in attempts.values()
            ],
        }

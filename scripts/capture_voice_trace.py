#!/usr/bin/env python3
"""Capture content-free latency evidence from Dimon's latest voice trace."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import UUID

from apps.dimon_live.voice_trace import (
    BROWSER_EVENTS,
    FAILURE_PHASES,
    RECOGNITION_PATHS,
    SERVER_EVENTS,
)


EXPECTED_PHASES = (
    "d_click",
    "audio_ready",
    "microphone_ready",
    "socket_ready",
    "backend_ready",
    "speech_detected",
    "nemotron_stream_ready",
    "first_partial",
    "wake_match",
    "speaker_check",
    "browser_wake_received",
    "reply_audio_ready",
    "reply_playback_started",
)
WORKER_ORDER = ("qwen", "nemotron", "kokoro", "faster_whisper")
ROOT_KEYS = frozenset({"trace", "workers", "gpu"})
TRACE_KEYS = frozenset({"trace_id", "started_at", "events", "attempts"})
EVENT_KEYS = frozenset({"source", "name", "elapsed_ms", "attempt_id"})
ATTEMPT_KEYS = frozenset({
    "attempt_id",
    "path",
    "failure_phase",
    "recognizer_disagreement",
    "faster_whisper_confidence",
    "faster_whisper_avg_logprob",
    "faster_whisper_no_speech_prob",
})
GPU_KEYS = frozenset({"total_mb", "free_mb", "safety_reserve_mb"})
WORKER_KEYS = frozenset({
    "name",
    "service",
    "profile",
    "state",
    "loaded",
    "worker_pid",
    "active_jobs",
    "heartbeat_age_seconds",
    "budget_mb",
    "observed_vram_mb",
    "model",
    "device",
    "compute_type",
    "context_tokens",
})
REPORT_WORKER_KEYS = (
    "name",
    "profile",
    "state",
    "loaded",
    "worker_pid",
    "active_jobs",
    "heartbeat_age_seconds",
    "budget_mb",
    "observed_vram_mb",
    "model",
    "device",
    "compute_type",
    "context_tokens",
)


class CaptureRejected(ValueError):
    """Raised when endpoint data violates the content-free capture schema."""


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CaptureRejected(f"{label} must be an object")
    return value


def _keys(value: dict[str, object], allowed: frozenset[str], label: str) -> None:
    unexpected = set(value) - allowed
    if unexpected:
        raise CaptureRejected(f"{label} contains forbidden fields: {sorted(unexpected)}")


def _uuid4(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise CaptureRejected(f"{label} must be a UUIDv4")
    try:
        parsed = UUID(value)
    except ValueError:
        raise CaptureRejected(f"{label} must be a UUIDv4") from None
    if parsed.version != 4 or str(parsed) != value:
        raise CaptureRejected(f"{label} must be a UUIDv4")
    return value


def _elapsed(value: object) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
        or value > 900_000
    ):
        raise CaptureRejected("elapsed time must be finite and non-negative")
    return round(float(value), 3)


def _quality(
    value: object, *, minimum: float, maximum: float, label: str
) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not minimum <= float(value) <= maximum
    ):
        raise CaptureRejected(f"{label} is invalid")
    return round(float(value), 4)


def _safe_number(value: object, label: str) -> float | int | None:
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise CaptureRejected(f"{label} must be a non-negative number")
    return value


def _safe_operational_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 160:
        raise CaptureRejected(f"{label} must be bounded text")
    lowered = value.lower()
    if "://" in lowered or value.startswith(("/", "~")) or "\\" in value:
        raise CaptureRejected(f"{label} contains infrastructure location data")
    return value


def _capture_workers(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > 4:
        raise CaptureRejected("workers must contain at most four entries")
    captured = []
    for raw_value in value:
        raw = _mapping(raw_value, "worker")
        _keys(raw, WORKER_KEYS, "worker")
        name = raw.get("name")
        if name not in WORKER_ORDER:
            raise CaptureRejected("worker name is not allowlisted")
        if not isinstance(raw.get("loaded"), bool):
            raise CaptureRejected("worker loaded must be boolean")
        worker: dict[str, object] = {}
        for key in REPORT_WORKER_KEYS:
            item = raw.get(key)
            if key in {"name", "profile", "state", "model", "device", "compute_type"}:
                item = _safe_operational_text(item, f"worker {key}")
            elif key == "loaded":
                item = bool(item)
            else:
                item = _safe_number(item, f"worker {key}")
            worker[key] = item
        captured.append(worker)
    captured.sort(key=lambda item: WORKER_ORDER.index(str(item["name"])))
    return captured


def _capture_gpu(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    raw = _mapping(value, "gpu")
    _keys(raw, GPU_KEYS, "gpu")
    return {key: _safe_number(raw.get(key), f"gpu {key}") for key in sorted(GPU_KEYS)}


def capture_record(payload: object) -> dict[str, object]:
    raw = _mapping(payload, "diagnostics payload")
    _keys(raw, ROOT_KEYS, "diagnostics payload")
    trace = _mapping(raw.get("trace"), "trace")
    _keys(trace, TRACE_KEYS, "trace")
    trace_id = _uuid4(trace.get("trace_id"), "trace_id")
    started_at = trace.get("started_at")
    if not isinstance(started_at, str) or len(started_at) > 64:
        raise CaptureRejected("started_at must be a bounded timestamp")
    try:
        parsed_started_at = datetime.fromisoformat(started_at)
    except ValueError:
        raise CaptureRejected("started_at must be an ISO timestamp") from None
    if parsed_started_at.tzinfo is None:
        raise CaptureRejected("started_at must include a timezone")

    timings: dict[str, dict[str, float]] = {"browser": {}, "server": {}}
    event_rows: list[dict[str, object]] = []
    events = trace.get("events")
    if not isinstance(events, list) or len(events) > 64:
        raise CaptureRejected("events must contain at most 64 entries")
    for raw_value in events:
        event = _mapping(raw_value, "event")
        _keys(event, EVENT_KEYS, "event")
        source = event.get("source")
        name = event.get("name")
        allowed = BROWSER_EVENTS if source == "browser" else (
            SERVER_EVENTS if source == "server" else frozenset()
        )
        if name not in allowed:
            raise CaptureRejected("event source or name is not allowlisted")
        elapsed_ms = _elapsed(event.get("elapsed_ms"))
        attempt_id = event.get("attempt_id")
        if attempt_id is not None:
            attempt_id = _uuid4(attempt_id, "event attempt_id")
        row: dict[str, object] = {
            "source": source,
            "name": name,
            "elapsed_ms": elapsed_ms,
        }
        if attempt_id is not None:
            row["attempt_id"] = attempt_id
        event_rows.append(row)

    attempts_value = trace.get("attempts")
    if not isinstance(attempts_value, list) or len(attempts_value) > 64:
        raise CaptureRejected("attempts must contain at most 64 entries")
    attempts: list[dict[str, object]] = []
    for raw_value in attempts_value:
        attempt = _mapping(raw_value, "attempt")
        _keys(attempt, ATTEMPT_KEYS, "attempt")
        attempt_id = _uuid4(attempt.get("attempt_id"), "attempt_id")
        path = attempt.get("path")
        failure_phase = attempt.get("failure_phase")
        if path is not None and path not in RECOGNITION_PATHS:
            raise CaptureRejected("recognition path is not allowlisted")
        if failure_phase is not None and failure_phase not in FAILURE_PHASES:
            raise CaptureRejected("failure phase is not allowlisted")
        disagreement = attempt.get("recognizer_disagreement", False)
        if not isinstance(disagreement, bool):
            raise CaptureRejected("recognizer disagreement must be boolean")
        confidence = attempt.get("faster_whisper_confidence")
        if confidence not in {None, "normal", "low"}:
            raise CaptureRejected("Faster-Whisper confidence is invalid")
        avg_logprob = _quality(
            attempt.get("faster_whisper_avg_logprob"),
            minimum=-100,
            maximum=0,
            label="Faster-Whisper average log probability",
        )
        no_speech_prob = _quality(
            attempt.get("faster_whisper_no_speech_prob"),
            minimum=0,
            maximum=1,
            label="Faster-Whisper no-speech probability",
        )
        attempts.append({
            "attempt_id": attempt_id,
            "path": path,
            "failure_phase": failure_phase,
            "recognizer_disagreement": disagreement,
            "faster_whisper_confidence": confidence,
            "faster_whisper_avg_logprob": avg_logprob,
            "faster_whisper_no_speech_prob": no_speech_prob,
        })

    final_attempt = next((attempt for attempt in reversed(attempts) if attempt["path"]), None)
    selected_attempt_id = final_attempt["attempt_id"] if final_attempt else None
    event_rows = [
        row for row in event_rows
        if row.get("attempt_id") in {None, selected_attempt_id}
    ]
    for row in event_rows:
        timings[str(row["source"])].setdefault(str(row["name"]), float(row["elapsed_ms"]))
    observed = {row["name"] for row in event_rows}
    missing = [phase for phase in EXPECTED_PHASES if phase not in observed]
    return {
        "trace_id": trace_id,
        "started_at": started_at,
        "completeness": {
            "observed": len(EXPECTED_PHASES) - len(missing),
            "expected": len(EXPECTED_PHASES),
            "ratio": round((len(EXPECTED_PHASES) - len(missing)) / len(EXPECTED_PHASES), 3),
            "missing": missing,
        },
        "timings_ms": timings,
        "events": event_rows,
        "attempt_ids": [attempt["attempt_id"] for attempt in attempts],
        "selected_attempt_id": selected_attempt_id,
        "recognition_path": final_attempt["path"] if final_attempt else None,
        "failure_phase": final_attempt["failure_phase"] if final_attempt else None,
        "recognizer_disagreement": (
            final_attempt["recognizer_disagreement"] if final_attempt else False
        ),
        "faster_whisper_confidence": (
            final_attempt["faster_whisper_confidence"] if final_attempt else None
        ),
        "faster_whisper_avg_logprob": (
            final_attempt["faster_whisper_avg_logprob"] if final_attempt else None
        ),
        "faster_whisper_no_speech_prob": (
            final_attempt["faster_whisper_no_speech_prob"] if final_attempt else None
        ),
        "workers": _capture_workers(raw.get("workers", [])),
        "gpu": _capture_gpu(raw.get("gpu")),
    }


def percentile(values: Iterable[float], percent: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if not 0 <= percent <= 100:
        raise ValueError("percentile must be between 0 and 100")
    position = (len(ordered) - 1) * percent / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


def summarize_records(records: list[dict[str, object]]) -> dict[str, object]:
    path_counts = Counter(
        str(record["recognition_path"])
        for record in records
        if record.get("recognition_path") is not None
    )
    failure_counts = Counter(
        str(record["failure_phase"])
        for record in records
        if record.get("failure_phase") is not None
    )
    confidence_counts = Counter(
        str(record["faster_whisper_confidence"])
        for record in records
        if record.get("faster_whisper_confidence") is not None
    )
    timings: dict[str, dict[str, dict[str, object]]] = {"browser": {}, "server": {}}
    for source in timings:
        names = sorted({
            name
            for record in records
            for name in record.get("timings_ms", {}).get(source, {})
        })
        for name in names:
            values = [
                float(record["timings_ms"][source][name])
                for record in records
                if name in record.get("timings_ms", {}).get(source, {})
            ]
            timings[source][name] = {
                "count": len(values),
                "p50": percentile(values, 50),
                "p95": percentile(values, 95),
                "min": round(min(values), 3),
                "max": round(max(values), 3),
            }
    ratios = [float(record["completeness"]["ratio"]) for record in records]
    return {
        "trials": len(records),
        "complete_trials": sum(ratio == 1.0 for ratio in ratios),
        "completeness_p50": percentile(ratios, 50),
        "recognition_paths": dict(sorted(path_counts.items())),
        "failure_phases": dict(sorted(failure_counts.items())),
        "recognizer_disagreements": sum(
            record.get("recognizer_disagreement") is True for record in records
        ),
        "faster_whisper_confidence": dict(sorted(confidence_counts.items())),
        "timings_ms": timings,
    }


def build_report(records: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summarize_records(records),
        "trials": records,
    }


def _trace_terminal(record: dict[str, object]) -> bool:
    event_names = {event["name"] for event in record["events"]}
    if "reply_playback_started" in event_names:
        return True
    return record.get("recognition_path") in {"failed", "cancelled"}


def collect_trials(
    fetch_latest: Callable[[], object],
    *,
    count: int,
    poll_interval: float,
    trial_timeout: float,
    settle_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[dict[str, object]]:
    baseline_payload = fetch_latest()
    baseline_trace = baseline_payload.get("trace", {}) if isinstance(baseline_payload, dict) else {}
    baseline_id = baseline_trace.get("trace_id") if isinstance(baseline_trace, dict) else None
    records: list[dict[str, object]] = []
    for index in range(count):
        deadline = monotonic() + trial_timeout
        candidate: dict[str, object] | None = None
        fingerprint: tuple[object, ...] | None = None
        unchanged_since = monotonic()
        print(
            f"Trial {index + 1}/{count}: click the red D, wait five seconds, then say Hey Dimon.",
            file=sys.stderr,
            flush=True,
        )
        while monotonic() < deadline:
            payload = fetch_latest()
            trace = payload.get("trace", {}) if isinstance(payload, dict) else {}
            trace_id = trace.get("trace_id") if isinstance(trace, dict) else None
            if trace_id and trace_id != baseline_id:
                candidate = capture_record(payload)
                current_fingerprint = (
                    candidate["trace_id"],
                    len(candidate["events"]),
                    tuple(candidate["attempt_ids"]),
                    candidate["recognition_path"],
                    candidate["failure_phase"],
                )
                if current_fingerprint != fingerprint:
                    fingerprint = current_fingerprint
                    unchanged_since = monotonic()
                if _trace_terminal(candidate) and (
                    "reply_playback_started" in candidate["timings_ms"]["browser"]
                    or monotonic() - unchanged_since >= settle_seconds
                ):
                    records.append(candidate)
                    baseline_id = str(candidate["trace_id"])
                    print(
                        f"Captured {candidate['trace_id']} ({candidate['recognition_path'] or 'partial'}).",
                        file=sys.stderr,
                        flush=True,
                    )
                    break
            sleep(poll_interval)
        else:
            suffix = f" after observing {candidate['trace_id']}" if candidate else ""
            raise TimeoutError(f"trial {index + 1} did not complete within {trial_timeout:g}s{suffix}")
    return records


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture content-free timing evidence for repeated Hey Dimon trials.",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8890")
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--trial-timeout", type=float, default=180)
    parser.add_argument("--settle-seconds", type=float, default=1.0)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.trials <= 0 or args.poll_interval <= 0 or args.trial_timeout <= 0:
        raise SystemExit("trial count and timing options must be positive")
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    endpoint = f"{args.url.rstrip('/')}/api/voice/diagnostics/latest"

    def fetch_latest() -> object:
        request = Request(endpoint, headers={"Accept": "application/json"})
        try:
            with opener.open(request, timeout=10) as response:
                return json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"diagnostics endpoint returned HTTP {error.code}") from error
        except URLError as error:
            raise RuntimeError(f"could not reach diagnostics endpoint: {error.reason}") from error

    records = collect_trials(
        fetch_latest,
        count=args.trials,
        poll_interval=args.poll_interval,
        trial_timeout=args.trial_timeout,
        settle_seconds=args.settle_seconds,
    )
    encoded = json.dumps(build_report(records), indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(f"Saved {len(records)} content-free trials to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Versioned deterministic inference fixtures for teammate development."""

from __future__ import annotations

import json
import io
import math
import re
import struct
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

from .model_adapter import (
    ExtractedIntent,
    FixtureNoMatch,
    InferenceFailure,
    InvalidModelOutput,
    validate_intent,
)


FixtureOutcome = Literal["intent", "invalid_output", "error"]
_CASE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


def normalize_fixture_input(text: str) -> str:
    return " ".join(text.strip().split()).casefold()


@dataclass(frozen=True)
class FixtureCase:
    case_id: str
    input_text: str
    outcome: FixtureOutcome
    raw_output: str | None = None
    intent: dict[str, Any] | None = None
    reason_code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class FixtureCatalog:
    cases_by_input: Mapping[str, FixtureCase]

    @classmethod
    def load(cls, path: Path) -> "FixtureCatalog":
        manifest_path = Path(path).resolve()
        root = manifest_path.parent
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("fixture manifest is unreadable") from exc
        if not isinstance(manifest, dict) or manifest.get("fixture_contract_version") != 1:
            raise ValueError("fixture_contract_version must be 1")
        cases = manifest.get("cases")
        if not isinstance(cases, list):
            raise ValueError("fixture cases must be a list")

        by_input: dict[str, FixtureCase] = {}
        case_ids: set[str] = set()
        for record in cases:
            case = cls._load_case(root, record)
            normalized = normalize_fixture_input(case.input_text)
            if case.case_id in case_ids:
                raise ValueError(f"duplicate fixture case_id: {case.case_id}")
            if normalized in by_input:
                raise ValueError(f"duplicate normalized fixture input: {case.case_id}")
            case_ids.add(case.case_id)
            by_input[normalized] = case
        return cls(MappingProxyType(by_input))

    @staticmethod
    def _load_case(root: Path, record: object) -> FixtureCase:
        if not isinstance(record, dict) or set(record) != {
            "case_id",
            "input",
            "outcome",
            "payload",
        }:
            raise ValueError("fixture case fields are invalid")
        case_id = record["case_id"]
        input_text = record["input"]
        outcome = record["outcome"]
        payload_name = record["payload"]
        if not isinstance(case_id, str) or _CASE_ID.fullmatch(case_id) is None:
            raise ValueError("fixture case_id is invalid")
        if not isinstance(input_text, str) or not normalize_fixture_input(input_text):
            raise ValueError("fixture input is invalid")
        if outcome not in {"intent", "invalid_output", "error"}:
            raise ValueError("fixture outcome is invalid")
        if not isinstance(payload_name, str) or not payload_name:
            raise ValueError("fixture payload is invalid")
        payload_path = (root / payload_name).resolve()
        if not payload_path.is_relative_to(root) or not payload_path.is_file():
            raise ValueError("fixture payload must stay inside the catalog")
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("fixture payload is unreadable") from exc

        if outcome == "intent":
            raw_output = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            try:
                intent = validate_intent(raw_output)
            except InvalidModelOutput as exc:
                raise ValueError(f"invalid fixture intent: {case_id}") from exc
            return FixtureCase(case_id, input_text, outcome, raw_output, intent)
        if outcome == "invalid_output":
            if not isinstance(payload, dict) or not isinstance(payload.get("raw_output"), str):
                raise ValueError("invalid-output fixture payload is invalid")
            return FixtureCase(
                case_id,
                input_text,
                outcome,
                raw_output=payload["raw_output"],
            )
        if not isinstance(payload, dict):
            raise ValueError("error fixture payload is invalid")
        reason_code = payload.get("reason_code")
        message = payload.get("message")
        if not isinstance(reason_code, str) or _REASON_CODE.fullmatch(reason_code) is None:
            raise ValueError("error fixture reason_code is invalid")
        if not isinstance(message, str) or not message:
            raise ValueError("error fixture message is invalid")
        return FixtureCase(
            case_id,
            input_text,
            outcome,
            reason_code=reason_code,
            message=message,
        )


class FixtureExtractor:
    def __init__(self, catalog: FixtureCatalog):
        self.catalog = catalog

    def extract(self, text: str) -> ExtractedIntent:
        case = self.catalog.cases_by_input.get(normalize_fixture_input(text))
        if case is None:
            raise FixtureNoMatch()
        if case.outcome == "invalid_output":
            raise InvalidModelOutput("fixture returned malformed model output")
        if case.outcome == "error":
            raise InferenceFailure(case.reason_code or "UPSTREAM_UNAVAILABLE", case.message or "fixture error")
        if case.intent is None or case.raw_output is None:
            raise InvalidModelOutput("fixture intent is missing")
        return ExtractedIntent(
            intent=dict(case.intent),
            raw_output=case.raw_output,
            latency_ms=0.0,
            fixture_case_id=case.case_id,
        )


class FixtureMultiActionExtractor:
    """Exact-match, read-only multi-action fixtures for scripted demos."""

    def __init__(self, cases: Mapping[str, tuple[str, dict[str, Any]]]):
        self.cases = cases

    @classmethod
    def load(cls, path: Path) -> "FixtureMultiActionExtractor":
        manifest_path = Path(path).resolve()
        root = manifest_path.parent
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        records = manifest.get("multi_action_cases", [])
        if not isinstance(records, list):
            raise ValueError("multi_action_cases must be a list")
        from .multi_action import validate_multi_action_intent

        cases: dict[str, tuple[str, dict[str, Any]]] = {}
        for record in records:
            if not isinstance(record, dict) or set(record) != {"case_id", "input", "payload"}:
                raise ValueError("multi-action fixture fields are invalid")
            payload_path = (root / record["payload"]).resolve()
            if not payload_path.is_relative_to(root) or not payload_path.is_file():
                raise ValueError("multi-action fixture payload is outside the catalog")
            raw = payload_path.read_text(encoding="utf-8")
            intent = validate_multi_action_intent(raw, text=record["input"])
            key = normalize_fixture_input(record["input"])
            if key in cases:
                raise ValueError("duplicate multi-action fixture input")
            cases[key] = (raw, intent)
        return cls(MappingProxyType(cases))

    def extract(self, text: str) -> ExtractedIntent:
        case = self.cases.get(normalize_fixture_input(text))
        if case is None:
            raise FixtureNoMatch()
        raw, intent = case
        return ExtractedIntent(dict(intent), raw, 0.0, "multi-action-fixture")


def _load_named_cases(path: Path) -> Mapping[str, dict[str, Any]]:
    manifest_path = Path(path).resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("fixture manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("fixture_contract_version") != 1:
        raise ValueError("fixture_contract_version must be 1")
    records = manifest.get("cases")
    if not isinstance(records, list):
        raise ValueError("fixture cases must be a list")

    cases: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("fixture case must be an object")
        case_id = record.get("case_id")
        if not isinstance(case_id, str) or _CASE_ID.fullmatch(case_id) is None:
            raise ValueError("fixture case_id is invalid")
        if case_id in cases:
            raise ValueError(f"duplicate fixture case_id: {case_id}")
        cases[case_id] = record
    return MappingProxyType(cases)


def _selected_case(path: Path, case_id: str) -> dict[str, Any]:
    try:
        return _load_named_cases(path)[case_id]
    except KeyError as exc:
        raise ValueError(f"unknown fixture case_id: {case_id}") from exc


def _fixture_error(record: Mapping[str, Any]) -> InferenceFailure:
    reason_code = record.get("reason_code")
    if not isinstance(reason_code, str) or _REASON_CODE.fullmatch(reason_code) is None:
        raise ValueError("fixture reason_code is invalid")
    message = record.get("message", "fixture provider unavailable")
    if not isinstance(message, str) or not message:
        raise ValueError("fixture error message is invalid")
    return InferenceFailure(reason_code, message)


class FixtureRecognizerStream:
    def __init__(
        self,
        record: Mapping[str, Any],
        on_partial: Callable[[str], Awaitable[None]],
        max_audio_bytes: int,
        max_utterance_seconds: float,
    ):
        self._record = record
        self._on_partial = on_partial
        self._max_audio_bytes = max_audio_bytes
        self._max_utterance_seconds = max_utterance_seconds
        self._audio_bytes = 0
        self._closed = False

    async def push_audio(self, audio: bytes) -> None:
        if self._closed:
            raise InferenceFailure("STREAM_CLOSED", "fixture voice stream is closed")
        if not isinstance(audio, bytes) or not audio or len(audio) % 4:
            raise InferenceFailure("MALFORMED_AUDIO", "fixture audio must be float32 PCM")
        self._audio_bytes += len(audio)
        if self._audio_bytes > self._max_audio_bytes:
            raise InferenceFailure(
                "AUDIO_LIMIT_EXCEEDED",
                "fixture audio exceeds the configured byte limit",
            )
        if self._audio_bytes / 4 / 16_000 > self._max_utterance_seconds:
            raise InferenceFailure(
                "UTTERANCE_LIMIT_EXCEEDED",
                "fixture audio exceeds the configured duration limit",
            )

    async def finish(self) -> str:
        if self._closed:
            raise InferenceFailure("STREAM_CLOSED", "fixture voice stream is closed")
        outcome = self._record.get("outcome")
        if outcome == "error":
            raise _fixture_error(self._record)
        if outcome != "transcript":
            raise ValueError("voice fixture outcome is invalid")
        partials = self._record.get("partials")
        transcript = self._record.get("transcript")
        if (
            not isinstance(partials, list)
            or not all(isinstance(item, str) and item for item in partials)
            or not isinstance(transcript, str)
            or not transcript
        ):
            raise ValueError("voice transcript fixture is invalid")
        for partial in partials:
            await self._on_partial(partial)
        return transcript

    async def close(self) -> None:
        self._closed = True


class FixtureRecognizer:
    def __init__(
        self,
        record: Mapping[str, Any],
        max_audio_bytes: int,
        max_utterance_seconds: float,
    ):
        if max_audio_bytes <= 0 or max_utterance_seconds <= 0:
            raise ValueError("fixture audio limits must be positive")
        self._record = record
        self._max_audio_bytes = max_audio_bytes
        self._max_utterance_seconds = max_utterance_seconds

    @classmethod
    def load(
        cls,
        path: Path,
        case_id: str,
        *,
        max_audio_bytes: int = 4_000_000,
        max_utterance_seconds: float = 30.0,
    ) -> "FixtureRecognizer":
        return cls(
            _selected_case(path, case_id),
            max_audio_bytes,
            max_utterance_seconds,
        )

    async def open_stream(
        self,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> FixtureRecognizerStream:
        return FixtureRecognizerStream(
            self._record,
            on_partial,
            self._max_audio_bytes,
            self._max_utterance_seconds,
        )

    async def transcribe(
        self,
        audio: bytes,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> str:
        stream = await self.open_stream(on_partial)
        try:
            await stream.push_audio(audio)
            return await stream.finish()
        finally:
            await stream.close()


class FixtureVoiceLifecycleLease:
    def __init__(self, status: dict[str, object]):
        self.status = status
        self.released = False

    async def release(self, *, disconnected: bool = False) -> None:
        del disconnected
        self.released = True


class FixtureVoiceLifecycle:
    def __init__(self, record: Mapping[str, Any]):
        self._record = record

    @classmethod
    def load(cls, path: Path, case_id: str) -> "FixtureVoiceLifecycle":
        return cls(_selected_case(path, case_id))

    async def acquire(self) -> FixtureVoiceLifecycleLease:
        outcome = self._record.get("lifecycle")
        if outcome == "ready":
            return FixtureVoiceLifecycleLease({"ok": True, "fixture": True})
        if outcome == "error":
            raise _fixture_error(self._record)
        raise ValueError("voice lifecycle fixture is invalid")


class FixtureSynthesizer:
    def __init__(self, record: Mapping[str, Any]):
        self._record = record

    @classmethod
    def load(cls, path: Path, case_id: str) -> "FixtureSynthesizer":
        return cls(_selected_case(path, case_id))

    async def __call__(self, text: str) -> bytes:
        del text
        outcome = self._record.get("outcome")
        if outcome == "error":
            raise _fixture_error(self._record)
        if outcome != "wav":
            raise ValueError("speech fixture outcome is invalid")
        return self._wav()

    async def synthesize_result(self, result: dict[str, Any]) -> bytes:
        from .speech import result_summary

        text = result_summary(result)
        if text is None:
            raise InferenceFailure(
                "INVALID_INFERENCE_REQUEST", "speech requires a committed result"
            )
        return await self(text)

    @staticmethod
    def _wav() -> bytes:
        sample_rate = 16_000
        sample_count = sample_rate // 4
        frames = bytearray()
        for index in range(sample_count):
            envelope = 1.0 - (index / sample_count)
            sample = int(5_000 * envelope * math.sin(2 * math.pi * 440 * index / sample_rate))
            frames.extend(struct.pack("<h", sample))
        output = io.BytesIO()
        with wave.open(output, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            audio.writeframes(frames)
        return output.getvalue()

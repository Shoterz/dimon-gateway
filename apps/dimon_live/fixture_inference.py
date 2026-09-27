"""Versioned deterministic inference fixtures for teammate development."""

from __future__ import annotations

import json
import re
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

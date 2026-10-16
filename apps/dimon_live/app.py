"""FastAPI boundary for the local Dimon Live prototype."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import base64
import binascii
import ipaddress
import math
import os
import logging
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping
from urllib.parse import urlparse, urlsplit
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .approval_gateway import (
    ApprovalContext,
    ApprovalRejected,
    DeviceAssertion,
    b64url_decode,
)
from .database import ClarificationRejected, Database, ReceiptRejected
from .conversation import (
    ConversationInput as CoreConversationInput,
    ConversationRejected,
    ConversationService,
    InputKind,
    build_adapter_registry,
)
from .conversation_store import ConversationStore
from .demo_preparation import DemoPreparationCoordinator
from .face_id import MAX_FRAMES, MIN_FRAMES, FaceRejected
from .gpu_overlord_status import GpuOverlordStatusClient
from . import app_lock, investing, market, singpass, stock_market, trtc
from .payees import PayeeSaveRejected
from .read_back import build_read_back
from .inference import InferenceProviders
from .model_adapter import Extractor
from .intel_voice import FasterWhisperClient, WhisperClient
from .provider_factory import build_inference_providers
from .receipts import DEMO_USER_ID
from .service import ActionService
from .paper_equity import PaperExecutionSnapshot
from .paper_equity_service import PaperEquityService
from .paper_equity_store import PaperEquityStore
from .session_manager import SessionBoundaryError, SessionContext, SessionManager
from .speech import SpeechPromptRejected, SpeechService, render_hands_free_prompt
from .speech_fallback import PrimaryFallbackRecognizer, PrimaryFallbackSynthesizer
from .hands_free import HandsFreeBridge
from .voice import VoiceBridge, VoiceLease
from .voice_trace import (
    BROWSER_EVENTS,
    FAILURE_PHASES,
    RECOGNITION_PATHS,
    SERVER_EVENTS,
    VoiceTraceStore,
)
from .voiceprint import VoiceRejected, speaker_model_health
from .investment_conversation import InvestmentConversationAdapter


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DEFAULT_DB_PATH = APP_DIR / "data" / "demo.sqlite3"

# Local defaults are convenient for the demo, while explicit shell/service
# variables remain authoritative for comparisons and deployment.
load_dotenv(APP_DIR / ".env", override=False)
logger = logging.getLogger(__name__)


def multi_action_status_for_result(result: Mapping[str, Any]) -> str:
    outcome = str(result.get("outcome", ""))
    paper_result = result.get("paper_order_result")
    paper_status = paper_result.get("status") if isinstance(paper_result, Mapping) else None
    if outcome.startswith("EXECUTED_") or paper_status == "FILLED":
        return "APPROVED"
    if outcome in {"BLOCKED", "FAILED"}:
        return "FAILED"
    if outcome in {"CANCELLED", "REJECTED", "PAPER_ORDER_CANCELLED"}:
        return "CANCELLED" if "CANCEL" in outcome else "REJECTED"
    return "AWAITING_APPROVAL"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_PREPARATION_STAGES = (
    "capacity",
    "nemotron",
    "qwen",
    "kokoro",
    "faster_whisper",
)
_PREPARATION_STATES = frozenset(
    {"IDLE", "PREPARING", "READY", "DEGRADED", "FAILED"}
)
_PREPARATION_STAGE_STATES = frozenset(
    {"IDLE", "PENDING", "RUNNING", "READY", "FAILED"}
)
_PREPARATION_REASON_CODES = frozenset(
    {
        "IDLE",
        "READY",
        "READY_WINDOW_EXPIRED",
        "PREPARING",
        "CHECKING",
        "PROBING",
        "DEMO_PREPARATION_DISABLED",
        "GPU_STATUS_UNAVAILABLE",
        "GPU_RESERVE_LOW",
        "WHISPER_BUSY",
        "NEMOTRON_UNAVAILABLE",
        "NEMOTRON_CAPACITY_BUSY",
        "NEMOTRON_OVER_CAPACITY",
        "QWEN_UNAVAILABLE",
        "QWEN_IDENTITY_MISMATCH",
        "KOKORO_UNAVAILABLE",
        "KOKORO_INVALID_AUDIO",
        "FASTER_WHISPER_UNAVAILABLE",
        "FASTER_WHISPER_IDENTITY_MISMATCH",
        "WORKER_MISSING",
        "PROFILE_MISMATCH",
        "WORKER_STATE_INVALID",
        "WORKER_STALE",
        "WORKER_OVER_BUDGET",
        "WORKER_PID_CHANGED",
        "PROBE_FAILED",
        "PREPARATION_TIMEOUT",
        "PREPARATION_CANCELLED",
    }
)
_PREPARATION_WARNING = "Readiness is rechecked while the demo-ready window is active."
_STAGE_FALLBACK_REASONS = {
    "capacity": "GPU_STATUS_UNAVAILABLE",
    "nemotron": "NEMOTRON_UNAVAILABLE",
    "qwen": "QWEN_UNAVAILABLE",
    "kokoro": "KOKORO_UNAVAILABLE",
    "faster_whisper": "FASTER_WHISPER_UNAVAILABLE",
}
_PREPARATION_WORKERS = {
    "qwen": ("llama", "qwen35-9b-abliterated-q5-4k"),
    "nemotron": ("nemotron", "nemotron-en"),
    "kokoro": ("kokoro", "kokoro-82m"),
    "faster_whisper": ("faster_whisper", "faster-whisper-medium-cuda"),
}
_WORKER_TO_STAGE = {
    "qwen": "qwen",
    "nemotron": "nemotron",
    "kokoro": "kokoro",
    "faster_whisper": "faster_whisper",
}


def _parse_local_host(raw_host: str, configured_port: int) -> tuple[str, int] | None:
    if not raw_host or any(character.isspace() for character in raw_host):
        return None
    try:
        parsed = urlsplit(f"//{raw_host}")
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if (
        hostname not in _LOOPBACK_HOSTS
        or port != configured_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return hostname, port


def _is_local_operator_request(request: Request, configured_port: int) -> bool:
    peer = request.client
    try:
        peer_is_loopback = bool(peer and ipaddress.ip_address(peer.host).is_loopback)
    except ValueError:
        peer_is_loopback = False
    if not peer_is_loopback:
        return False
    host_values = [
        value.decode("latin-1")
        for name, value in request.scope.get("headers", [])
        if name.lower() == b"host"
    ]
    if len(host_values) != 1:
        return False
    parsed_host = _parse_local_host(host_values[0], configured_port)
    if parsed_host is None:
        return False
    hostname, port = parsed_host

    raw_origin = request.headers.get("origin")
    if raw_origin is None:
        return True
    try:
        origin = urlparse(raw_origin)
        origin_port = origin.port
    except ValueError:
        return False
    return bool(
        origin.scheme == request.url.scheme
        and origin.hostname
        and origin.hostname.lower() == hostname
        and origin.hostname.lower() in _LOOPBACK_HOSTS
        and origin_port == port
        and not origin.username
        and not origin.password
        and origin.path in {"", "/"}
        and not origin.params
        and not origin.query
        and not origin.fragment
    )


def _safe_preparation_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _bounded_preparation_int(
    value: object,
    *,
    minimum: int = 0,
    maximum: int = 1_000_000,
) -> int | None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        return None
    return value


def _bounded_preparation_float(
    value: object,
    *,
    maximum: float = 3_600,
) -> float | None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
        or value > maximum
    ):
        return None
    return round(float(value), 3)


def _sanitize_preparation_workers(
    value: object,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    candidates = value if isinstance(value, list) else []
    by_name: dict[str, dict[str, object]] = {}
    duplicates: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        name = candidate.get("name")
        if name not in _PREPARATION_WORKERS:
            continue
        if name in by_name:
            duplicates.add(name)
        by_name[name] = candidate

    workers: list[dict[str, object]] = []
    failures: dict[str, str] = {}
    for name, (service, profile) in _PREPARATION_WORKERS.items():
        item = by_name.get(name) if name not in duplicates else None
        state = item.get("state") if item is not None else None
        pid = _bounded_preparation_int(
            item.get("worker_pid") if item is not None else None,
            minimum=1,
            maximum=2_147_483_647,
        )
        active_jobs = _bounded_preparation_int(
            item.get("active_jobs") if item is not None else None,
            maximum=1_024,
        )
        heartbeat = _bounded_preparation_float(
            item.get("heartbeat_age_seconds") if item is not None else None
        )
        budget = _bounded_preparation_int(
            item.get("budget_mb") if item is not None else None,
            minimum=1,
            maximum=131_072,
        )
        observed = _bounded_preparation_int(
            item.get("observed_vram_mb") if item is not None else None,
            maximum=131_072,
        )

        reason = None
        if item is None or pid is None:
            reason = "WORKER_MISSING"
        elif item.get("service") != service or item.get("profile") != profile:
            reason = "PROFILE_MISMATCH"
        elif state not in {"ready", "busy"}:
            reason = "WORKER_STATE_INVALID"
        elif active_jobs is None:
            reason = "WORKER_STATE_INVALID"
        elif heartbeat is None or heartbeat > 5:
            reason = "WORKER_STALE"
        elif budget is None or observed is None or observed > budget:
            reason = "WORKER_OVER_BUDGET"
        elif name == "nemotron" and active_jobs is not None and active_jobs > 1:
            reason = "NEMOTRON_OVER_CAPACITY"

        model = None
        device = None
        compute_type = None
        context_tokens = None
        if name == "qwen":
            if item is not None:
                model = (
                    "qwen35-9b-abliterated-q5"
                    if item.get("model") == "qwen35-9b-abliterated-q5"
                    else None
                )
                device = "cuda" if item.get("device") == "cuda" else None
                compute_type = "Q5_K_M" if item.get("compute_type") == "Q5_K_M" else None
                context_tokens = 4096 if item.get("context_tokens") == 4096 else None
            if None in (model, device, compute_type, context_tokens):
                reason = reason or "QWEN_IDENTITY_MISMATCH"
        elif name == "faster_whisper":
            if item is not None:
                model = "medium" if item.get("model") == "medium" else None
                device = "cuda" if item.get("device") == "cuda" else None
                compute_type = (
                    "int8_float16"
                    if item.get("compute_type") == "int8_float16"
                    else None
                )
            if None in (model, device, compute_type):
                reason = reason or "FASTER_WHISPER_IDENTITY_MISMATCH"

        loaded = reason is None
        if reason is not None:
            failures[name] = reason
        workers.append(
            {
                "name": name,
                "service": service,
                "profile": profile,
                "state": state if loaded else "unavailable",
                "loaded": loaded,
                "worker_pid": pid if loaded else None,
                "active_jobs": active_jobs if loaded else 0,
                "heartbeat_age_seconds": heartbeat if loaded else None,
                "budget_mb": budget if loaded else None,
                "observed_vram_mb": observed if loaded else None,
                "model": model,
                "device": device,
                "compute_type": compute_type,
                "context_tokens": context_tokens,
            }
        )
    return workers, failures


def _sanitize_preparation_gpu(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    total = _bounded_preparation_int(value.get("total_mb"), minimum=1, maximum=131_072)
    free = _bounded_preparation_int(value.get("free_mb"), maximum=131_072)
    reserve = _bounded_preparation_int(
        value.get("safety_reserve_mb"), minimum=512, maximum=131_072
    )
    if (
        total is None
        or free is None
        or reserve is None
        or free > total
        or reserve > total
    ):
        return None
    return {"total_mb": total, "free_mb": free, "safety_reserve_mb": reserve}


def _sanitize_preparation_snapshot(value: object) -> dict[str, object]:
    raw = value if isinstance(value, dict) else {}
    state = raw.get("state")
    if state not in _PREPARATION_STATES:
        state = "FAILED"
    raw_stages = raw.get("stages")
    candidates = raw_stages if isinstance(raw_stages, list) else []
    by_name = {
        item.get("name"): item
        for item in candidates
        if isinstance(item, dict) and item.get("name") in _PREPARATION_STAGES
    }
    stages = []
    for name in _PREPARATION_STAGES:
        item = by_name.get(name, {})
        stage_state = item.get("state")
        if stage_state not in _PREPARATION_STAGE_STATES:
            stage_state = "PENDING"
        latency_value = _bounded_preparation_float(
            item.get("latency_ms"), maximum=900_000
        )
        latency = round(latency_value) if latency_value is not None else 0
        reason = item.get("reason_code")
        if reason not in _PREPARATION_REASON_CODES:
            reason = _STAGE_FALLBACK_REASONS[name] if stage_state == "FAILED" else None
        stages.append(
            {
                "name": name,
                "state": stage_state,
                "latency_ms": latency,
                "reason_code": reason,
            }
        )

    workers, worker_failures = _sanitize_preparation_workers(raw.get("workers"))
    gpu = _sanitize_preparation_gpu(raw.get("gpu"))
    started_at = _safe_preparation_timestamp(raw.get("started_at"))
    completed_at = _safe_preparation_timestamp(raw.get("completed_at"))
    ready_until = _safe_preparation_timestamp(raw.get("ready_until"))
    reason_code = raw.get("reason_code")
    if reason_code not in _PREPARATION_REASON_CODES:
        reason_code = state

    if state == "READY" and ready_until is not None:
        expires_at = datetime.fromisoformat(ready_until)
        if expires_at <= datetime.now(timezone.utc):
            state = "IDLE"
            reason_code = "READY_WINDOW_EXPIRED"

    if state == "IDLE":
        stages = [
            {"name": name, "state": "IDLE", "latency_ms": 0, "reason_code": "IDLE"}
            for name in _PREPARATION_STAGES
        ]
        workers = []
        gpu = None
        ready_until = None
    elif state == "READY":
        invalid_stages = [stage for stage in stages if stage["state"] != "READY"]
        gpu_ready = bool(
            gpu is not None
            and gpu["free_mb"] >= max(512, gpu["safety_reserve_mb"])
        )
        distinct_pids = {
            worker["worker_pid"] for worker in workers if worker["loaded"]
        }
        timestamps_ready = False
        if started_at is not None and completed_at is not None and ready_until is not None:
            started_value = datetime.fromisoformat(started_at)
            completed_value = datetime.fromisoformat(completed_at)
            expires_value = datetime.fromisoformat(ready_until)
            timestamps_ready = started_value <= completed_value < expires_value
        if (
            invalid_stages
            or worker_failures
            or len(distinct_pids) != len(_PREPARATION_WORKERS)
            or not gpu_ready
            or not timestamps_ready
        ):
            state = "DEGRADED"
            if invalid_stages:
                for stage in invalid_stages:
                    stage["state"] = "FAILED"
                    stage["reason_code"] = _STAGE_FALLBACK_REASONS[stage["name"]]
                reason_code = invalid_stages[0]["reason_code"]
            elif worker_failures:
                worker_name, reason_code = next(iter(worker_failures.items()))
                stage = next(
                    item
                    for item in stages
                    if item["name"] == _WORKER_TO_STAGE[worker_name]
                )
                stage["state"] = "FAILED"
                stage["reason_code"] = reason_code
            else:
                reason_code = "GPU_RESERVE_LOW" if gpu is not None else "GPU_STATUS_UNAVAILABLE"
                stages[0]["state"] = "FAILED"
                stages[0]["reason_code"] = reason_code
        else:
            reason_code = "READY"

    return {
        "state": state,
        "reason_code": reason_code,
        "best_effort": False,
        "started_at": started_at,
        "completed_at": completed_at,
        "ready_until": ready_until,
        "stages": stages,
        "workers": workers,
        "gpu": gpu,
        "warning": _PREPARATION_WARNING,
    }


def _safe_uuid4(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return value if parsed.version == 4 and str(parsed) == value else None


def _sanitize_voice_trace_snapshot(value: object) -> dict[str, object]:
    raw = value if isinstance(value, dict) else {}
    trace_id = _safe_uuid4(raw.get("trace_id"))
    started_at = _safe_preparation_timestamp(raw.get("started_at"))
    if trace_id is None or started_at is None:
        return {}

    events = []
    candidates = raw.get("events") if isinstance(raw.get("events"), list) else []
    for candidate in candidates[-64:]:
        if not isinstance(candidate, dict):
            continue
        source = candidate.get("source")
        name = candidate.get("name")
        elapsed_ms = _bounded_preparation_float(
            candidate.get("elapsed_ms"), maximum=900_000
        )
        allowed = BROWSER_EVENTS if source == "browser" else (
            SERVER_EVENTS if source == "server" else frozenset()
        )
        if name not in allowed or elapsed_ms is None:
            continue
        event = {"source": source, "name": name, "elapsed_ms": elapsed_ms}
        attempt_id = _safe_uuid4(candidate.get("attempt_id"))
        if attempt_id is not None:
            event["attempt_id"] = attempt_id
        events.append(event)

    attempts = []
    candidates = raw.get("attempts") if isinstance(raw.get("attempts"), list) else []
    for candidate in candidates[-64:]:
        if not isinstance(candidate, dict):
            continue
        attempt_id = _safe_uuid4(candidate.get("attempt_id"))
        path = candidate.get("path")
        failure_phase = candidate.get("failure_phase")
        if attempt_id is None:
            continue
        if path is not None and path not in RECOGNITION_PATHS:
            continue
        if failure_phase is not None and failure_phase not in FAILURE_PHASES:
            continue
        if path is None and failure_phase is not None:
            continue
        if path == "nemotron_final" and failure_phase is not None:
            continue
        if path == "nemotron_partial" and failure_phase is not None \
                and not failure_phase.startswith("nemotron_"):
            continue
        if path == "faster_whisper_final" and failure_phase is not None \
                and not failure_phase.startswith("nemotron_"):
            continue
        disagreement = candidate.get("recognizer_disagreement")
        confidence = candidate.get("faster_whisper_confidence")
        avg_logprob = candidate.get("faster_whisper_avg_logprob")
        no_speech_prob = candidate.get("faster_whisper_no_speech_prob")
        if not isinstance(disagreement, bool):
            disagreement = False
        if confidence not in {None, "normal", "low"}:
            confidence = None
        if not (
            isinstance(avg_logprob, (int, float))
            and not isinstance(avg_logprob, bool)
            and math.isfinite(avg_logprob)
            and -100 <= float(avg_logprob) <= 0
        ):
            avg_logprob = None
        if not (
            isinstance(no_speech_prob, (int, float))
            and not isinstance(no_speech_prob, bool)
            and math.isfinite(no_speech_prob)
            and 0 <= float(no_speech_prob) <= 1
        ):
            no_speech_prob = None
        sanitized_attempt = {
            "attempt_id": attempt_id,
            "path": path,
            "failure_phase": failure_phase,
        }
        if disagreement:
            sanitized_attempt["recognizer_disagreement"] = True
        if confidence is not None:
            sanitized_attempt["faster_whisper_confidence"] = confidence
        if avg_logprob is not None:
            sanitized_attempt["faster_whisper_avg_logprob"] = float(avg_logprob)
        if no_speech_prob is not None:
            sanitized_attempt["faster_whisper_no_speech_prob"] = float(no_speech_prob)
        attempts.append(sanitized_attempt)

    return {
        "trace_id": trace_id,
        "started_at": started_at,
        "events": events,
        "attempts": attempts,
    }


class ActionRequest(BaseModel):
    request_id: UUID
    text: Annotated[str, Field(max_length=500)]

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("text must not be blank")
        return normalized


class ConversationTextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["TEXT"]
    text: Annotated[str, Field(max_length=500)]

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("text must not be blank")
        return normalized


class ConversationChoiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["CHOICE"]
    choice: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]


ConversationTurnInput = Annotated[
    ConversationTextInput | ConversationChoiceInput,
    Field(discriminator="kind"),
]


class ConversationTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    expected_version: Annotated[int, Field(ge=1)]
    input: ConversationTurnInput


B64Url = Annotated[str, Field(min_length=1, max_length=8192, pattern=r"^[A-Za-z0-9_-]+$")]
ReceiptDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ChallengeId = Annotated[str, Field(pattern=r"^chal_[0-9a-f]{32}$")]


class DeviceAssertionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: ChallengeId
    credential_id: Annotated[str, Field(min_length=1, max_length=1024, pattern=r"^[A-Za-z0-9_-]+$")]
    client_data_json: B64Url
    authenticator_data: B64Url
    signature: B64Url


class ReceiptApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_digest: ReceiptDigest
    assertion: DeviceAssertionPayload
    out_of_band_code: Annotated[str, Field(pattern=r"^\d{6}$")] | None = None
    read_back_played: bool = False
    unusual_amount_confirmed: bool = False
    voice_confirmation: Annotated[str, Field(max_length=32)] | None = None


class ReceiptChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_digest: ReceiptDigest


class PaperEquityOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_digest: ReceiptDigest


class PaperEquityApprovalRequest(BaseModel):
    # Quote, FX, fee, and status fields are intentionally ignored if a client
    # sends them; the provider refresh below is server-owned.
    model_config = ConfigDict(extra="ignore")

    receipt_digest: ReceiptDigest
    assertion: DeviceAssertionPayload


class PaperEquityRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    expected_version: Annotated[int, Field(ge=1)]
    input: ConversationTurnInput


class CredentialRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: ChallengeId
    credential_kind: Literal["WEBAUTHN_PLATFORM", "MOCK_DEVICE_KEY"]
    credential_id: Annotated[str, Field(min_length=1, max_length=1024, pattern=r"^[A-Za-z0-9_-]+$")]
    public_key_spki: B64Url
    algorithm: Literal[-7, -257]
    client_data_json: B64Url
    authenticator_data: B64Url
    signature: B64Url | None = None


class KycCompletionRequest(CredentialRegistrationRequest):
    consent_id: Annotated[str, Field(pattern=r"^consent_[0-9a-f]{32}$")]


class VoiceEnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # This browser's key for signing voice-confirmed approvals without a prompt.
    voice_key: CredentialRegistrationRequest | None = None


# One camera frame, base64 (JPEG from the browser canvas). Frames are analysed
# in memory and discarded; only their hashes are kept.
FaceFrame = Annotated[str, Field(min_length=64, max_length=560_000, pattern=r"^[A-Za-z0-9+/=_-]+$")]
FaceFrames = Annotated[list[FaceFrame], Field(min_length=MIN_FRAMES, max_length=MAX_FRAMES)]


class SingpassRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: Annotated[str, Field(pattern=r"^order_[0-9a-f]{32}$")]
    order_digest: ReceiptDigest


class InvestorAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    horizon: Annotated[int, Field(ge=1, le=4)]
    drop: Annotated[int, Field(ge=1, le=4)]
    experience: Annotated[int, Field(ge=1, le=4)]
    goal: Annotated[int, Field(ge=1, le=4)]


class InvestorProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: InvestorAnswers
    acknowledged: bool


class FaceRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consent_id: Annotated[str, Field(pattern=r"^consent_[0-9a-f]{32}$")]
    frames: FaceFrames


class FaceVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: ChallengeId
    frames: FaceFrames


def _decode_frames(frames: list[str]) -> list[bytes]:
    decoded = []
    for frame in frames:
        normalized = frame.replace("-", "+").replace("_", "/")
        try:
            decoded.append(base64.b64decode(normalized + "=" * (-len(normalized) % 4), validate=True))
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(status_code=400, detail="FACE_FRAME_INVALID") from exc
    return decoded


def _face_error(exc: FaceRejected) -> HTTPException:
    status = {"FACE_ENGINE_UNAVAILABLE": 503, "FACE_LOCKED": 429}.get(exc.reason_code, 400)
    return HTTPException(status_code=status, detail=exc.reason_code)


class UnlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: ChallengeId
    frames: FaceFrames


# What a locked (logged-out) account may still reach: enough to show the lock
# screen and unlock with the registered face, and nothing that reads or moves money.
LOCKED_PATHS = frozenset({
    "/api/session", "/api/session/lock", "/api/session/unlock-challenge", "/api/session/unlock",
    "/api/face", "/api/kyc",
})


class SavePayeeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: Annotated[str, Field(pattern=r"^receipt_[0-9a-f]{32}$")]
    nickname: Annotated[str, Field(min_length=3, max_length=40)]


class ClarificationResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    text: Annotated[str, Field(max_length=100)]

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("text must not be blank")
        return normalized


class VoicePhraseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: ChallengeId


class VoiceClientDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: Literal[
        "audio_context",
        "microphone",
        "worklet_module",
        "worklet_node",
        "socket",
        "reply_audio_fetch",
        "reply_audio_playback",
    ]
    error_name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]
    request_id: UUID | None = None


class SpeechPromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_key: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
    values: dict[str, Any] = Field(default_factory=dict)


def _wake_recognizer(stt_backend: str, inference_mode: str = "local") -> dict[str, Any]:
    """A local whisper.cpp for hearing "Hey Dimon", when one is configured
    (DIMON_WAKE_STT_BACKEND=whisper_cpp) and it isn't already the main one."""

    if inference_mode == "remote":
        return {
            "wake_recognizer": FasterWhisperClient(
                os.environ.get("DIMON_WHISPER_URL", "").strip() or "ws://127.0.0.1:8003/ws",
                connect_timeout=float(os.environ.get("DIMON_WHISPER_CONNECT_TIMEOUT", "3")),
                result_timeout=float(os.environ.get("DIMON_WAKE_RESULT_TIMEOUT", "15")),
            ),
            "wake_lease": VoiceLease(1),
        }
    if os.environ.get("DIMON_WAKE_STT_BACKEND", "").strip().lower() != "whisper_cpp":
        return {}
    if stt_backend == "whisper_cpp":
        return {}
    url = os.environ.get("DIMON_WHISPER_CPP_URL", "").strip() or "ws://127.0.0.1:8006/v1/stream"
    return {
        "wake_recognizer": WhisperClient(
            url,
            connect_timeout=float(os.environ.get("DIMON_WHISPER_CPP_CONNECT_TIMEOUT", "3")),
            result_timeout=float(os.environ.get("DIMON_WAKE_RESULT_TIMEOUT", "15")),
        ),
        "wake_lease": VoiceLease(1),
    }


def create_app(
    db_path: Path | None = None,
    *,
    extractor: Extractor | None = None,
    stock_extractor: Extractor | None = None,
    session_manager: SessionManager | None = None,
    owner_path: Path | None = None,
    visitor_dir: Path | None = None,
    allowed_origins: set[str] | None = None,
    voice_bridge: VoiceBridge | None = None,
    speech_service: SpeechService | None = None,
    providers: InferenceProviders | None = None,
    stock_provider: stock_market.MarketProvider | None = None,
    face_engine: Any = None,
    demo_preparer: DemoPreparationCoordinator | None = None,
    voice_trace_store: VoiceTraceStore | None = None,
    trtc_assistant: trtc.TrtcConversationService | None = None,
    voice_engine: Any = None,
) -> FastAPI:
    if providers is not None and any(
        dependency is not None
        for dependency in (extractor, stock_extractor, voice_bridge, speech_service)
    ):
        raise ValueError("providers cannot be combined with individual inference dependencies")
    selected_providers = providers or build_inference_providers(
        os.environ.get("DIMON_INFERENCE_MODE", "fixture"),
        os.environ,
    )
    legacy_database: Database | None = None
    if db_path is not None:
        legacy_database = Database(Path(db_path), face_engine=face_engine, voice_engine=voice_engine)
        legacy_database.initialize()
    manager = session_manager or (
        SessionManager.from_environment(legacy_database=legacy_database)
        if owner_path is None and visitor_dir is None and allowed_origins is None
        else SessionManager(
            owner_path=owner_path or APP_DIR / "runtime" / "owner.sqlite3",
            visitor_dir=visitor_dir or APP_DIR / "runtime" / "visitors",
            allowed_origins=allowed_origins or set(),
            owner_origin=os.environ.get("DIMON_OWNER_ORIGIN", "https://dimon.stocksuite.app"),
            owner_access_audience=os.environ.get("DIMON_OWNER_ACCESS_AUDIENCE") or None,
            visitor_ttl_seconds=int(os.environ.get("DIMON_VISITOR_TTL_SECONDS", "3600")),
            max_visitor_sessions=int(os.environ.get("DIMON_MAX_VISITOR_SESSIONS", "25")),
        )
    )
    if face_engine is not None:
        manager.face_engine = face_engine
    if voice_engine is not None:
        manager.voice_engine = voice_engine
    selected_extractor = extractor or selected_providers.extractor
    selected_stock_extractor = stock_extractor or selected_providers.stock_extractor
    selected_bill_payment_extractor = selected_providers.bill_payment_extractor
    selected_multi_action_extractor = selected_providers.multi_action_extractor

    async def sweep_expired_ledgers() -> None:
        while True:
            await asyncio.sleep(float(os.environ.get("DIMON_LEDGER_SWEEP_SECONDS", "300")))
            try:
                await asyncio.to_thread(manager.cleanup)
            except Exception:  # noqa: BLE001 - the next sweep tries again
                logger.exception("ledger sweep failed")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        voice_identity_health = await asyncio.to_thread(speaker_model_health)
        app.state.voice_identity_health = voice_identity_health
        if voice_identity_health["state"] == "ready":
            logger.info("voice_identity_model_ready path=%s", voice_identity_health["path"])
        else:
            logger.warning(
                "voice_identity_model_unavailable reason=%s path=%s",
                voice_identity_health["reason"], voice_identity_health["path"],
            )
        preparer = app.state.demo_preparer
        start_task = None
        if preparer is not None:
            async def start_preparation() -> None:
                try:
                    await preparer.start()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("demo_preparation_autostart_failed")

            start_task = asyncio.create_task(
                start_preparation(), name="dimon-demo-preparation-start"
            )
        app.state.demo_preparation_start_task = start_task
        # Expired anonymous ledgers are swept in the background, in batches
        # from the registry index, never by scanning folders per request.
        sweep = asyncio.create_task(sweep_expired_ledgers()) if manager.legacy_database is None else None
        try:
            yield
        finally:
            if sweep is not None:
                sweep.cancel()
                await asyncio.gather(sweep, return_exceptions=True)
            if start_task is not None and not start_task.done():
                start_task.cancel()
                await asyncio.gather(start_task, return_exceptions=True)
            stop = getattr(preparer, "stop", None) if preparer is not None else None
            if callable(stop):
                try:
                    await stop()
                except Exception:
                    logger.warning("demo_preparation_shutdown_failed")
            manager.close()

    application = FastAPI(
        title="Dimon Live", version="0.1.0", lifespan=lifespan
    )
    application.state.database = legacy_database
    application.state.session_manager = manager
    application.state.extractor = selected_extractor
    application.state.stock_extractor = selected_stock_extractor
    application.state.bill_payment_extractor = selected_bill_payment_extractor
    application.state.multi_action_extractor = selected_multi_action_extractor
    application.state.stock_provider = stock_provider or stock_market.YFinanceMarketProvider()
    application.state.conversation_enabled = os.environ.get(
        "DIMON_CONVERSATIONS_ENABLED", "1"
    ) != "0"
    application.state.paper_equity_enabled = os.environ.get(
        "DIMON_REAL_STOCK_EXECUTION_ENABLED", "1"
    ) != "0"
    application.state.demo_exchange_enabled = os.environ.get(
        "DIMON_DEMO_EXCHANGE_ENABLED", "1"
    ) != "0"
    application.state.conversation_adapters = build_adapter_registry(
        (InvestmentConversationAdapter(selected_providers.followup_interpreter),)
    ) if application.state.conversation_enabled else {}
    application.state.inference_mode = selected_providers.mode
    selected_voice_trace_store = (
        voice_trace_store if voice_trace_store is not None else VoiceTraceStore()
    )
    application.state.voice_trace_store = selected_voice_trace_store
    # Push-to-talk and hands-free share one lease. Whisper is always attempted
    # first, so the local concurrency limit remains authoritative even when a
    # scalable TRTC outage fallback is configured.
    stt_backend = os.environ.get("DIMON_STT_BACKEND", "whisper_cpp").strip().lower() or "whisper_cpp"
    voice_lease = VoiceLease(int(os.environ.get(
        "DIMON_VOICE_CONCURRENCY", "1"
    )))
    application.state.hands_free = HandsFreeBridge(
        recognizer=selected_providers.recognizer, lease=voice_lease,
        fast_wake_recognizer=(
            selected_providers.recognizer if selected_providers.mode == "remote" else None
        ),
        fast_wake_lease=(VoiceLease(1) if selected_providers.mode == "remote" else None),
        trace_store=selected_voice_trace_store,
        **_wake_recognizer(stt_backend, selected_providers.mode),
    )
    application.state.voice_bridge = voice_bridge or VoiceBridge(
        recognizer=selected_providers.recognizer,
        lifecycle=selected_providers.voice_lifecycle,
        lease=voice_lease,
        max_audio_bytes=int(os.environ.get("DIMON_MAX_AUDIO_BYTES", "4000000")),
        max_utterance_seconds=float(os.environ.get("DIMON_MAX_UTTERANCE_SECONDS", "30")),
        endpoint_silence_seconds=float(os.environ.get("DIMON_VOICE_ENDPOINT_SILENCE_SECONDS", "1.4")),
        pause_seconds=float(os.environ.get("DIMON_VOICE_PAUSE_SECONDS", "0.3")),
        speech_threshold=float(os.environ.get("DIMON_VOICE_SPEECH_THRESHOLD", "0.015")),
        evidence_dir=Path(os.environ.get("DIMON_VOICE_EVIDENCE_DIR", "runtime/evidence")),
    )
    application.state.speech_service = speech_service or SpeechService(
        base_url=os.environ.get("DIMON_KOKORO_URL", "http://127.0.0.1:3001"),
        audio_dir=Path(os.environ.get("DIMON_AUDIO_DIR", APP_DIR / "runtime" / "audio")),
        synthesizer=selected_providers.synthesizer,
        max_wav_bytes=int(os.environ.get("DIMON_MAX_WAV_BYTES", str(8 * 1_048_576)),),
        ttl_seconds=int(os.environ.get("DIMON_VISITOR_TTL_SECONDS", "3600")),
        read_back_synthesizer=selected_providers.synthesizer,
    )
    try:
        demo_port = int(os.environ.get("DIMON_PORT", "8890"))
    except ValueError:
        demo_port = 0
    preparation_enabled = bool(
        os.environ.get("DIMON_DEMO_PREPARE_ENABLED") == "1"
        and selected_providers.mode == "local"
        and 1 <= demo_port <= 65_535
    )
    selected_demo_preparer = None
    if preparation_enabled:
        runtime_dir = Path(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        )
        socket_path = Path(
            os.environ.get(
                "DIMON_GPU_OVERLORD_SOCKET",
                runtime_dir / f"gpu-overlord-{os.getuid()}.sock",
            )
        )
        selected_demo_preparer = demo_preparer or DemoPreparationCoordinator(
            status_client=GpuOverlordStatusClient(socket_path),
            extractor=selected_extractor,
            recognizer=selected_providers.recognizer,
            lifecycle=selected_providers.voice_lifecycle,
            synthesizer=selected_providers.synthesizer,
            faster_whisper_recognizer=FasterWhisperClient(
                os.environ.get("DIMON_WHISPER_URL", "").strip()
                or "ws://127.0.0.1:8003/ws",
                connect_timeout=float(
                    os.environ.get("DIMON_WHISPER_CONNECT_TIMEOUT", "3")
                ),
                result_timeout=float(
                    os.environ.get("DIMON_WHISPER_RESULT_TIMEOUT", "30")
                ),
            ),
            max_wav_bytes=int(
                os.environ.get("DIMON_MAX_WAV_BYTES", str(8 * 1_048_576))
            ),
        )
    application.state.demo_preparer = selected_demo_preparer
    application.state.demo_preparation_snapshot = _sanitize_preparation_snapshot(
        {"state": "IDLE"}
    )
    remote_preparation_client = (
        getattr(selected_providers.synthesizer, "client", None)
        if selected_providers.mode == "remote"
        else None
    )

    async def diagnostic_preparation_snapshot() -> dict[str, object]:
        if selected_demo_preparer is not None:
            try:
                raw = await selected_demo_preparer.status()
            except Exception:
                logger.warning("voice_diagnostics_preparation_unavailable")
            else:
                application.state.demo_preparation_snapshot = (
                    _sanitize_preparation_snapshot(raw)
                )
        return application.state.demo_preparation_snapshot

    def require_demo_operator(request: Request) -> None:
        if not _is_local_operator_request(request, demo_port):
            raise HTTPException(status_code=404, detail="not found")

    if selected_demo_preparer is not None:
        async def call_demo_preparer(operation: str) -> dict[str, object]:
            try:
                raw = await getattr(selected_demo_preparer, operation)()
            except Exception:
                logger.warning("demo_preparation_operation_failed operation=%s", operation)
                raw = {
                    "state": "FAILED",
                    "stages": [
                        {
                            "name": "capacity",
                            "state": "FAILED",
                            "latency_ms": 0,
                            "reason_code": "GPU_STATUS_UNAVAILABLE",
                        }
                    ],
                }
            result = _sanitize_preparation_snapshot(raw)
            application.state.demo_preparation_snapshot = result
            return result

        @application.get("/api/demo/prepare")
        async def get_demo_preparation(request: Request):
            require_demo_operator(request)
            result = await call_demo_preparer("status")
            return JSONResponse(
                result,
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )

        @application.post("/api/demo/prepare")
        async def post_demo_preparation(request: Request):
            require_demo_operator(request)
            content_type = request.headers.get("content-type", "").partition(";")[0].strip()
            if content_type != "application/json":
                raise HTTPException(status_code=404, detail="not found")
            result = await call_demo_preparer("start")
            return JSONResponse(
                result,
                status_code=202 if result["state"] == "PREPARING" else 200,
                headers={"Cache-Control": "no-store"},
            )
    elif remote_preparation_client is not None:
        @application.get("/api/demo/prepare")
        async def get_remote_demo_preparation(request: Request):
            require_demo_operator(request)
            result = await remote_preparation_client.preparation()
            sanitized = _sanitize_preparation_snapshot(result)
            application.state.demo_preparation_snapshot = sanitized
            return JSONResponse(
                sanitized,
                headers={"Cache-Control": "no-store"},
            )

        @application.post("/api/demo/prepare")
        async def post_remote_demo_preparation(request: Request):
            require_demo_operator(request)
            content_type = request.headers.get("content-type", "").partition(";")[0].strip()
            if content_type != "application/json" or await request.json() != {}:
                raise HTTPException(status_code=400, detail="empty JSON object required")
            result = await remote_preparation_client.preparation(start=True)
            sanitized = _sanitize_preparation_snapshot(result)
            application.state.demo_preparation_snapshot = sanitized
            return JSONResponse(
                sanitized,
                status_code=202 if result.get("state") == "PREPARING" else 200,
                headers={"Cache-Control": "no-store"},
            )
    # The standalone TRTC conversational assistant is not a product mode.
    # Explicit dependency injection remains for its isolated protocol tests.
    application.state.trtc_assistant = trtc_assistant
    application.state.trtc_features = {
        "assistant": application.state.trtc_assistant is not None,
        "speech_recognition": isinstance(selected_providers.recognizer, PrimaryFallbackRecognizer),
        "speech_synthesis": isinstance(selected_providers.synthesizer, PrimaryFallbackSynthesizer),
    }

    def resolve(request: Request, response: Response) -> SessionContext:
        try:
            context = manager.resolve_headers(
                host=request.headers.get("host", "").lower(),
                origin=request.headers.get("origin"),
                cookie=request.cookies.get(manager.cookie_name),
                access_assertion=request.headers.get("cf-access-jwt-assertion"),
            )
        except SessionBoundaryError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.reason_code) from exc
        if request.url.path not in LOCKED_PATHS and context.database.lock_status()["locked"]:
            raise HTTPException(status_code=423, detail="APP_LOCKED")
        if context.role == "visitor":
            response.set_cookie(
                manager.cookie_name,
                context.session_id,
                secure=request.url.scheme == "https",
                httponly=True,
                samesite="lax",
                max_age=manager.visitor_ttl_seconds,
            )
        return context

    @application.get("/api/voice/diagnostics/latest")
    async def latest_voice_diagnostics(request: Request, response: Response):
        resolve(request, response)
        preparation = await diagnostic_preparation_snapshot()
        response.headers["Cache-Control"] = "no-store"
        return {
            "trace": _sanitize_voice_trace_snapshot(
                application.state.voice_trace_store.latest()
            ),
            "workers": preparation.get("workers", []),
            "gpu": preparation.get("gpu"),
        }

    def conversation_service_for(context: SessionContext) -> ConversationService:
        if not application.state.conversation_enabled:
            raise HTTPException(status_code=404, detail="CONVERSATIONS_DISABLED")
        store = ConversationStore(
            context.database.path,
            clock=context.database._clock,
        )
        return ConversationService(
            store,
            build_adapter_registry(
                (
                    InvestmentConversationAdapter(
                        selected_providers.followup_interpreter,
                        stock_provider=application.state.stock_provider,
                        database=context.database,
                    ),
                )
            ),
            context.database._clock,
        )

    def service_for(context: SessionContext) -> ActionService:
        return ActionService(
            context.database,
            selected_extractor,
            stock_provider=application.state.stock_provider,
            stock_extractor=selected_stock_extractor,
            bill_payment_extractor=selected_bill_payment_extractor,
            multi_action_extractor=selected_multi_action_extractor,
            conversation_service=(
                conversation_service_for(context)
                if application.state.conversation_enabled
                else None
            ),
            session_scope=context.session_id,
            user_id=DEMO_USER_ID,
        )

    def paper_equity_service_for(context: SessionContext) -> PaperEquityService:
        return PaperEquityService(
            context.database,
            clock=context.database._clock,
            provider=application.state.stock_provider,
            demo_exchange=application.state.demo_exchange_enabled,
        )

    def require_paper_equity() -> None:
        if not application.state.paper_equity_enabled:
            raise HTTPException(status_code=404, detail="PAPER_EQUITY_DISABLED")

    async def attach_paper_order(
        result: dict[str, Any], context: SessionContext, request_id: str
    ) -> dict[str, Any]:
        if not application.state.paper_equity_enabled:
            return result
        conversation = result.get("conversation") or {}
        if conversation.get("status") not in {"READY_FOR_DRAFT", "CONSUMED"}:
            return result
        try:
            receipt = await asyncio.to_thread(
                paper_equity_service_for(context).create_order,
                conversation_id=str(conversation["conversation_id"]),
                conversation_version=int(conversation["version"]),
                session_scope=context.session_id,
                user_id=DEMO_USER_ID,
                request_id=request_id,
            )
        except ValueError as exc:
            reason = str(exc)
            if reason in {"CONVERSATION_VERSION_CONFLICT", "CONVERSATION_NOT_FOUND"}:
                raise
            question = {
                "PAPER_EQUITY_INSUFFICIENT_FUNDS": "There isn't enough in your savings for this order. Try fewer shares or a smaller amount.",
                "PAPER_EQUITY_NOT_ENOUGH_SHARES": "You don't hold that many shares any more. Check your portfolio and try again.",
                "PAPER_EQUITY_FUNDING_ACCOUNT_INVALID": "Shares can only be paid for from your savings account.",
            }.get(reason, "I couldn't prepare that order. Please start it again.")
            return {
                **result, "outcome": "BLOCKED", "reason_code": reason if reason.isupper() else "PAPER_ORDER_INVALID",
                "answer": question, "question": question,
            }
        selling = receipt.side == "SELL"
        noun = "sale" if selling else "purchase"
        shares = f"{receipt.quantity:,} {'share' if receipt.quantity == 1 else 'shares'}"
        combined = {
            **result,
            "outcome": "ORDER_CONFIRMATION_REQUEST",
            "reason_code": "PAPER_EQUITY_APPROVAL_REQUIRED",
            "answer": (
                f"Your {noun} of {shares} of {receipt.instrument.name} is ready. "
                "Approve it with Singpass, then confirm with your face."
            ),
            "question": f"Approve this {noun} with Singpass and your face?",
            "paper_order": receipt.to_dict(),
            "trace": [*(result.get("trace") or []), {"stage": "PAPER_ORDER_RECEIPT_CREATED"}],
        }
        conversation_id = conversation.get("conversation_id")
        if isinstance(conversation_id, str):
            context.database.bind_multi_action_child_reference(
                "conversation", conversation_id, "paper_order",
                receipt.order_id, result=combined, user_id=DEMO_USER_ID,
            )
        return combined

    async def attach_multi_action_stock_order(
        result: dict[str, Any], context: SessionContext
    ) -> dict[str, Any]:
        if result.get("outcome") != "MULTI_ACTION_CONFIRMATION_REQUEST":
            return result
        refreshed_items = []
        for item in result.get("items", []):
            child_result = item.get("result") or {}
            if (
                item.get("kind") == "STOCK_INVESTMENT"
                and (child_result.get("conversation") or {}).get("status") == "READY_FOR_DRAFT"
            ):
                child_result = await attach_paper_order(
                    child_result, context, str(child_result.get("request_id", result.get("request_id", "")))
                )
                item = {**item, "result": child_result}
                if child_result.get("paper_order"):
                    item["status"] = "AWAITING_APPROVAL"
            refreshed_items.append(item)
        return {**result, "items": refreshed_items}

    async def attach_stock_orders(
        result: dict[str, Any], context: SessionContext, request_id: str
    ) -> dict[str, Any]:
        """Attach every ready stock draft, including children of split requests."""

        if result.get("outcome") == "MULTI_ACTION_CONFIRMATION_REQUEST":
            return await attach_multi_action_stock_order(result, context)
        if result.get("outcome") == "MULTI_REQUEST":
            refreshed_steps = []
            for index, step in enumerate(result.get("steps", [])):
                child = step.get("result") or {}
                child_request_id = str(
                    step.get("request_id")
                    or child.get("request_id")
                    or f"{request_id}:part:{index}"
                )
                child = await attach_stock_orders(child, context, child_request_id)
                refreshed_steps.append({**step, "result": child})
            return {**result, "steps": refreshed_steps}
        return await attach_paper_order(result, context, request_id)

    def conversation_http_error(error: ConversationRejected) -> HTTPException:
        if error.reason_code == "CONVERSATION_EXPIRED":
            status_code = 410
        elif error.reason_code in {
            "CONVERSATION_NOT_FOUND",
            "CONVERSATION_DOMAIN_UNAVAILABLE",
        }:
            status_code = 404
        elif error.reason_code in {
            "CONVERSATION_VERSION_CONFLICT",
            "CONVERSATION_IDEMPOTENCY_CONFLICT",
            "CONVERSATION_TERMINAL",
            "CONVERSATION_HANDLER_VERSION_UNAVAILABLE",
        }:
            status_code = 409
        elif error.reason_code in {
            "CONVERSATION_HANDLER_FAILED",
            "CONVERSATION_STATE_INVALID",
        }:
            status_code = 503
        else:
            status_code = 400
        return HTTPException(status_code=status_code, detail=error.reason_code)

    async def attach_speech(result: dict, context: SessionContext) -> dict:
        artifact_id = application.state.speech_service.queue_result(result, context)
        if artifact_id is None:
            return result
        return {**result, "audio_artifact_id": artifact_id, "audio_pending": True}

    async def attach_action_speech(result: dict, context: SessionContext) -> dict:
        if result.get("outcome") == "MULTI_ACTION_CONFIRMATION_REQUEST":
            items = []
            for item in result.get("items", []):
                child = item.get("result") if isinstance(item, dict) else None
                if isinstance(child, dict):
                    item = {**item, "result": await attach_speech(child, context)}
                items.append(item)
            return {**result, "items": items}
        return await attach_speech(result, context)

    def voice_locked_result(context: SessionContext) -> dict[str, str] | None:
        if not context.database.lock_status()["locked"]:
            return None
        return {"outcome": "BLOCKED", "reason_code": "APP_LOCKED"}

    async def finalize_action_result(
        result: dict[str, Any], context: SessionContext, request_id: str
    ) -> dict[str, Any]:
        result = await attach_stock_orders(result, context, request_id)
        return await attach_action_speech(result, context)

    async def finalize_voice_action_result(
        result: dict[str, Any], context: SessionContext, request_id: str
    ) -> dict[str, Any]:
        locked = voice_locked_result(context)
        if locked is not None:
            return locked
        result = await attach_stock_orders(result, context, request_id)
        locked = voice_locked_result(context)
        if locked is not None:
            return locked
        return await attach_action_speech(result, context)

    @application.get("/api/session")
    async def get_session(request: Request, response: Response):
        context = resolve(request, response)
        return {
            "role": context.role,
            "session_id": "owner" if context.role == "owner" else "visitor",
            "inference_mode": application.state.inference_mode,
            "expires_in_seconds": None
            if context.role == "owner"
            else manager.visitor_ttl_seconds,
            **context.database.lock_status(),
        }

    @application.post("/api/session/lock")
    async def lock_session(request: Request, response: Response):
        context = resolve(request, response)
        try:
            return context.database.lock_app()
        except app_lock.LockRejected as exc:
            raise HTTPException(status_code=409, detail=exc.reason_code) from exc

    @application.post("/api/session/unlock-challenge")
    async def unlock_challenge(request: Request, response: Response):
        context = resolve(request, response)
        try:
            return context.database.issue_unlock_challenge()
        except app_lock.LockRejected as exc:
            raise HTTPException(status_code=409, detail=exc.reason_code) from exc

    @application.post("/api/session/unlock")
    async def unlock_session(request: Request, response: Response, body: UnlockRequest):
        context = resolve(request, response)
        frames = _decode_frames(body.frames)
        try:
            return await asyncio.to_thread(context.database.unlock_with_face, body.challenge_id, frames)
        except app_lock.LockRejected as exc:
            raise HTTPException(status_code=409, detail=exc.reason_code) from exc
        except FaceRejected as exc:
            raise _face_error(exc) from exc

    @application.get("/api/state")
    async def get_state(request: Request, response: Response):
        context = resolve(request, response)
        state = context.database.get_state()
        return {
            **state,
            "demo_mode": True,
            "model": os.environ.get(
                "DIMON_QWEN_MODEL", "qwen35-9b-abliterated-q5"
            ),
        }

    @application.get("/api/multi-actions/{group_id}")
    async def get_multi_action_group(
        request: Request, response: Response, group_id: str
    ):
        context = resolve(request, response)
        group = context.database.get_multi_action_group(
            group_id, user_id=DEMO_USER_ID
        )
        if group is None:
            raise HTTPException(status_code=404, detail="MULTI_ACTION_GROUP_NOT_FOUND")
        return group

    def update_multi_action_status(
        context: SessionContext, child_type: str, child_id: str,
        result: Mapping[str, Any],
    ) -> None:
        status = multi_action_status_for_result(result)
        context.database.update_multi_action_child_status(
            child_type, child_id, status, result, user_id=DEMO_USER_ID
        )

    @application.post("/api/actions")
    async def create_action(request: Request, response: Response, action: ActionRequest):
        context = resolve(request, response)
        request_id = str(action.request_id)
        result = await asyncio.to_thread(
            service_for(context).process,
            request_id,
            action.text,
        )
        return await finalize_action_result(result, context, request_id)

    @application.get("/api/conversations/{conversation_id}")
    async def get_conversation(
        request: Request, response: Response, conversation_id: str
    ):
        context = resolve(request, response)
        try:
            result = await asyncio.to_thread(
                conversation_service_for(context).get,
                conversation_id,
                session_scope=context.session_id,
                user_id=DEMO_USER_ID,
            )
        except ConversationRejected as exc:
            raise conversation_http_error(exc) from exc
        return result

    @application.post("/api/conversations/{conversation_id}/turns")
    async def post_conversation_turn(
        request: Request,
        response: Response,
        conversation_id: str,
        turn: ConversationTurnRequest,
    ):
        context = resolve(request, response)
        if turn.input.kind == "TEXT":
            conversation_input = CoreConversationInput(
                InputKind.TEXT, text=turn.input.text
            )
            source = "typed"
        else:
            conversation_input = CoreConversationInput(
                InputKind.CHOICE, choice=turn.input.choice
            )
            source = "button"
        try:
            result = await asyncio.to_thread(
                conversation_service_for(context).apply_turn,
                conversation_id,
                session_scope=context.session_id,
                user_id=DEMO_USER_ID,
                request_id=str(turn.request_id),
                expected_version=turn.expected_version,
                source=source,
                conversation_input=conversation_input,
            )
        except ConversationRejected as exc:
            raise conversation_http_error(exc) from exc
        result = await attach_paper_order(result, context, str(turn.request_id))
        return await attach_speech(result, context)

    @application.get("/api/paper-equity/orders/{order_id}")
    async def paper_equity_order(
        request: Request, response: Response, order_id: str
    ):
        context = resolve(request, response)
        require_paper_equity()
        try:
            payload = PaperEquityStore(
                context.database.path, clock=context.database._clock
            ).get_order(
                order_id, user_id=DEMO_USER_ID, session_scope=context.session_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        payload.pop("canonical_json", None)
        return {"paper_order": payload}

    @application.post("/api/paper-equity/orders/{order_id}/singpass")
    async def paper_equity_singpass(
        request: Request,
        response: Response,
        order_id: str,
        body: PaperEquityOrderRequest,
    ):
        context = resolve(request, response)
        require_paper_equity()
        try:
            return await asyncio.to_thread(
                paper_equity_service_for(context).request_singpass,
                order_id,
                body.receipt_digest,
                user_id=DEMO_USER_ID,
            )
        except ReceiptRejected as exc:
            raise HTTPException(status_code=409, detail=exc.reason_code) from exc

    @application.post("/api/paper-equity/orders/{order_id}/challenge")
    async def paper_equity_challenge(
        request: Request,
        response: Response,
        order_id: str,
        body: PaperEquityOrderRequest,
    ):
        context = resolve(request, response)
        require_paper_equity()
        try:
            return context.database.issue_paper_equity_challenge(
                order_id,
                body.receipt_digest,
                user_id=DEMO_USER_ID,
                rp_id=approval_context(request).rp_id,
            )
        except ReceiptRejected as exc:
            raise HTTPException(status_code=409, detail=exc.reason_code) from exc

    @application.post("/api/paper-equity/orders/{order_id}/approve")
    async def paper_equity_approve(
        request: Request,
        response: Response,
        order_id: str,
        body: PaperEquityApprovalRequest,
    ):
        context = resolve(request, response)
        require_paper_equity()
        try:
            assertion = DeviceAssertion.from_wire(body.assertion.model_dump())
            execution = await asyncio.to_thread(
                paper_equity_service_for(context).execution_snapshot,
                order_id,
                user_id=DEMO_USER_ID,
                session_scope=context.session_id,
            )
            result = await asyncio.to_thread(
                paper_equity_service_for(context).approve_and_execute,
                order_id,
                body.receipt_digest,
                assertion=assertion,
                context=approval_context(request),
                execution=execution,
                user_id=DEMO_USER_ID,
            )
        except (ApprovalRejected, ReceiptRejected, ValueError) as exc:
            reason = getattr(exc, "reason_code", str(exc))
            status = 503 if reason == "PAPER_EQUITY_PROVIDER_UNAVAILABLE" else 409
            raise HTTPException(status_code=status, detail=reason) from exc
        combined = {"paper_order_result": result, **result}
        update_multi_action_status(context, "paper_order", order_id, combined)
        return await attach_speech(combined, context)

    @application.post("/api/paper-equity/orders/{order_id}/refresh")
    async def paper_equity_refresh(
        request: Request,
        response: Response,
        order_id: str,
        body: PaperEquityOrderRequest,
    ):
        context = resolve(request, response)
        require_paper_equity()
        try:
            execution = await asyncio.to_thread(
                paper_equity_service_for(context).execution_snapshot,
                order_id,
                user_id=DEMO_USER_ID,
                session_scope=context.session_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        store = PaperEquityStore(context.database.path, clock=context.database._clock)
        try:
            stored = store.get_order(
                order_id, user_id=DEMO_USER_ID, session_scope=context.session_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if body.receipt_digest != stored["order_digest"]:
            raise HTTPException(status_code=409, detail="PAPER_EQUITY_DIGEST_MISMATCH")
        if stored["status"] in {"AUTHORIZED", "OPEN", "QUEUED"}:
            try:
                lifecycle = await asyncio.to_thread(
                    store.apply_execution,
                    order_id,
                    body.receipt_digest,
                    execution,
                    user_id=DEMO_USER_ID,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        else:
            lifecycle = {"status": stored["status"], "reason_code": stored.get("reason_code")}
        return {
            "order_id": order_id,
            "receipt_digest": body.receipt_digest,
            "lifecycle": lifecycle,
            "quote": {
                "price_minor": execution.price_minor,
                "market_state": execution.market_state,
                "captured_at": execution.captured_at.isoformat(),
                "source": execution.source,
            },
            "fx": {
                "pair": execution.fx_snapshot.pair,
                "rate": str(execution.fx_snapshot.rate),
                "captured_at": execution.fx_snapshot.captured_at.isoformat(),
                "source": execution.fx_snapshot.source,
            },
        }

    @application.post("/api/paper-equity/orders/{order_id}/cancel")
    async def paper_equity_cancel(
        request: Request,
        response: Response,
        order_id: str,
        body: PaperEquityOrderRequest,
    ):
        context = resolve(request, response)
        require_paper_equity()
        try:
            result = PaperEquityStore(
                context.database.path, clock=context.database._clock
            ).cancel_order(
                order_id,
                body.receipt_digest,
                user_id=DEMO_USER_ID,
                session_scope=context.session_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return result

    def approval_context(request: Request) -> ApprovalContext:
        # The signature must name the exact origin this request came from
        # (already checked against the allow-list by resolve()), and the RP ID
        # is that origin's host. Neither value is taken from the request body.
        origin = request.headers.get("origin")
        if not origin:
            origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
        origin = origin.rstrip("/")
        return ApprovalContext(
            rp_id=urlparse(origin).hostname or "",
            allowed_origins=frozenset({origin}),
        )

    def blocked(reason_code: str, context: SessionContext) -> dict:
        return {
            "outcome": "BLOCKED",
            "reason_code": reason_code,
            "trace": [{"stage": reason_code}],
            "state": context.database.get_state(),
        }

    @application.get("/api/audit")
    async def audit_trail(request: Request, response: Response, limit: int = 100):
        context = resolve(request, response)
        return context.database.audit_overview(limit=max(1, min(limit, 500)))

    @application.get("/api/audit/receipts/{receipt_id}")
    async def audit_receipt_bundle(request: Request, response: Response, receipt_id: str):
        context = resolve(request, response)
        bundle = context.database.audit_bundle(receipt_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="RECEIPT_NOT_FOUND")
        return bundle

    @application.get("/api/profile")
    async def customer_profile(request: Request, response: Response):
        context = resolve(request, response)
        return context.database.customer_profile()

    @application.get("/api/mock-token")
    async def mock_hardware_token(request: Request, response: Response):
        # Simulates the separate hardware token the brief allows to be mocked.
        # A real deployment delivers this code on another device or channel.
        context = resolve(request, response)
        return {
            "simulated_device": "Mock hardware token (stands in for a separate device)",
            "pending": context.database.mock_token_display(),
        }

    @application.get("/mock-token", include_in_schema=False)
    async def mock_hardware_token_page(request: Request):
        if manager.legacy_database is None:
            try:
                manager._assert_origin(
                    request.headers.get("origin"),
                    request.headers.get("host", "").lower(),
                )
            except SessionBoundaryError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.reason_code) from exc
        return HTMLResponse(
            (STATIC_DIR / "mock_token.html").read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )

    # Singpass authorisation for investment transactions. The Singpass app
    # here is a mock service; its decisions are signed with Singpass's own key.
    @application.post("/api/singpass/authorisations")
    async def singpass_request(request: Request, response: Response, body: SingpassRequest):
        context = resolve(request, response)
        try:
            return context.database.request_singpass_authorisation(body.order_id, body.order_digest)
        except ReceiptRejected as exc:
            raise HTTPException(status_code=400, detail=exc.reason_code) from exc

    @application.get("/api/singpass/authorisations/{auth_id}")
    async def singpass_status(request: Request, response: Response, auth_id: str):
        context = resolve(request, response)
        try:
            return context.database.singpass_authorisation(auth_id)
        except singpass.SingpassRejected as exc:
            raise HTTPException(status_code=404, detail=exc.reason_code) from exc

    @application.get("/api/singpass/pending")
    async def singpass_pending(request: Request, response: Response):
        context = resolve(request, response)
        return {"simulated_service": "Mock Singpass app", "pending": context.database.singpass_pending()}

    @application.post("/api/singpass/authorisations/{auth_id}/{decision}")
    async def singpass_decision(request: Request, response: Response, auth_id: str, decision: Literal["approve", "reject"]):
        context = resolve(request, response)
        try:
            return context.database.singpass_decide(auth_id, decision == "approve")
        except singpass.SingpassRejected as exc:
            raise HTTPException(status_code=409, detail=exc.reason_code) from exc

    @application.get("/mock-singpass", include_in_schema=False)
    async def mock_singpass_page(request: Request):
        if manager.legacy_database is None:
            try:
                manager._assert_origin(
                    request.headers.get("origin"),
                    request.headers.get("host", "").lower(),
                )
            except SessionBoundaryError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.reason_code) from exc
        return HTMLResponse(
            (STATIC_DIR / "mock_singpass.html").read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/api/signing/credentials")
    async def list_signing_credentials(request: Request, response: Response):
        context = resolve(request, response)
        signing = approval_context(request)
        return {
            "rp_id": signing.rp_id,
            "credentials": context.database.signing_credentials(signing.rp_id),
        }

    @application.post("/api/signing/registration-challenge")
    async def signing_registration_challenge(request: Request, response: Response):
        context = resolve(request, response)
        signing = approval_context(request)
        challenge = context.database.issue_registration_challenge()
        return {
            **challenge,
            "rp_id": signing.rp_id,
            "user": {"id": DEMO_USER_ID, "name": "Demo User"},
            "existing_credentials": context.database.signing_credentials(signing.rp_id),
        }

    @application.post("/api/signing/credentials")
    async def register_signing_credential(request: Request, response: Response):
        # A device can only be registered as part of KYC onboarding, which
        # binds it as the one device allowed to approve transfers.
        resolve(request, response)
        raise HTTPException(status_code=400, detail="KYC_ONBOARDING_REQUIRED")

    @application.get("/api/kyc")
    async def kyc_status(request: Request, response: Response):
        context = resolve(request, response)
        return context.database.kyc_status()

    @application.post("/api/kyc/myinfo")
    async def kyc_retrieve_myinfo(request: Request, response: Response):
        context = resolve(request, response)
        return context.database.start_myinfo()

    def settle_pending_paper_orders(context: SessionContext) -> list[dict[str, Any]]:
        """Orders queued while the market was closed, or limit orders waiting
        for their price, are checked against the live quote and filled when
        they can be. Each fill re-checks the balance or the holding."""

        if not application.state.paper_equity_enabled:
            return []
        service = paper_equity_service_for(context)
        store = PaperEquityStore(context.database.path, clock=context.database._clock)
        settled = []
        for order_id, digest in store.pending_order_ids(DEMO_USER_ID, context.session_id):
            try:
                execution = service.execution_snapshot(
                    order_id, user_id=DEMO_USER_ID, session_scope=context.session_id
                )
                result = store.apply_execution(order_id, digest, execution, user_id=DEMO_USER_ID)
            except Exception as exc:     # a quote outage leaves the order waiting
                logger.warning("paper order %s not refreshed: %s", order_id, type(exc).__name__)
                continue
            if result.get("status") not in {"QUEUED", "OPEN"}:
                order = store.get_order(order_id, user_id=DEMO_USER_ID)
                settled.append({
                    "order_id": order_id,
                    "side": order.get("side", "BUY"),
                    "instrument": order["instrument"],
                    "quantity": order["quantity"],
                    **result,
                })
        return settled

    @application.get("/api/invest")
    async def invest_overview(request: Request, response: Response):
        context = resolve(request, response)
        settled = await asyncio.to_thread(settle_pending_paper_orders, context)
        overview = await asyncio.to_thread(context.database.investing_overview)
        paper_equity = await asyncio.to_thread(
            PaperEquityStore(
                context.database.path, clock=context.database._clock
            ).portfolio,
            DEMO_USER_ID,
        )
        return {**overview, "paper_equity": paper_equity, "settled_orders": settled}

    @application.post("/api/invest/profile")
    async def invest_profile(request: Request, response: Response, body: InvestorProfileRequest):
        context = resolve(request, response)
        try:
            return context.database.save_investor_profile(body.answers.model_dump(), body.acknowledged)
        except investing.InvestRejected as exc:
            raise HTTPException(status_code=400, detail=exc.reason_code) from exc

    @application.get("/api/invest/trending")
    async def invest_trending(request: Request, response: Response):
        context = resolve(request, response)
        investor = context.database.investor_profile()
        return investing.trending(datetime.now(timezone.utc), investor["risk_score"] if investor else None)

    @application.get("/api/invest/plans")
    async def invest_plans(
        request: Request, response: Response,
        monthly: Annotated[int | None, Query(ge=5_000, le=1_000_000)] = None,
        years: Annotated[int | None, Query(ge=1, le=40)] = None,
    ):
        context = resolve(request, response)
        investor = context.database.investor_profile()
        return investing.plan_suggestions(
            investor["risk_score"] if investor else None,
            investor["horizon_years"] if investor else None, monthly, years,
        )

    @application.get("/api/invest/instruments/{symbol}")
    async def invest_instrument(request: Request, response: Response, symbol: str):
        resolve(request, response)
        if symbol not in market.BY_SYMBOL:
            raise HTTPException(status_code=404, detail="INSTRUMENT_NOT_FOUND")
        now = datetime.now(timezone.utc)
        return {**market.snapshot(symbol, now), "history": market.history(symbol, now), "disclaimer": market.DISCLAIMER}

    @application.post("/api/invest/plans/{plan_id}/cancel")
    async def invest_cancel_plan(request: Request, response: Response, plan_id: str):
        context = resolve(request, response)
        try:
            context.database.cancel_investment_plan(plan_id)
        except investing.InvestRejected as exc:
            raise HTTPException(status_code=404, detail=exc.reason_code) from exc
        return await asyncio.to_thread(context.database.investing_overview)

    @application.post("/api/orders/{order_id}/challenge")
    async def order_challenge(request: Request, response: Response, order_id: str, body: ReceiptChallengeRequest):
        context = resolve(request, response)
        try:
            challenge = context.database.issue_order_challenge(
                order_id, body.receipt_digest, rp_id=approval_context(request).rp_id
            )
        except ReceiptRejected as exc:
            return blocked(exc.reason_code, context)
        queue_text = getattr(application.state.speech_service, "queue_text", None)
        text = challenge["review"]["read_back"]["text"]
        challenge["review"]["read_back"]["audio_artifact_id"] = queue_text(text, context) if callable(queue_text) else None
        return challenge

    @application.post("/api/orders/{order_id}/approve")
    async def approve_order(request: Request, response: Response, order_id: str, approval: ReceiptApprovalRequest):
        context = resolve(request, response)
        try:
            assertion = device_assertion(approval)
        except ApprovalRejected as exc:
            return blocked(exc.reason_code, context)
        result = await asyncio.to_thread(
            service_for(context).approve_order,
            order_id,
            approval.receipt_digest,
            assertion=assertion,
            context=approval_context(request),
            out_of_band_code=approval.out_of_band_code,
            read_back_played=approval.read_back_played,
            risk_acknowledged=approval.unusual_amount_confirmed,
        )
        update_multi_action_status(context, "receipt", receipt_id, result)
        return await attach_speech(result, context)

    @application.post("/api/payee-additions/{addition_id}/challenge")
    async def payee_addition_challenge(
        request: Request, response: Response, addition_id: str, body: ReceiptChallengeRequest
    ):
        context = resolve(request, response)
        try:
            challenge = context.database.issue_payee_addition_challenge(
                addition_id, body.receipt_digest, rp_id=approval_context(request).rp_id
            )
        except ReceiptRejected as exc:
            return blocked(exc.reason_code, context)
        queue_text = getattr(application.state.speech_service, "queue_text", None)
        text = challenge["review"]["read_back"]["text"]
        challenge["review"]["read_back"]["audio_artifact_id"] = queue_text(text, context) if callable(queue_text) else None
        return challenge

    @application.post("/api/payee-additions/{addition_id}/approve")
    async def approve_payee_addition(
        request: Request, response: Response, addition_id: str, approval: ReceiptApprovalRequest
    ):
        context = resolve(request, response)
        try:
            assertion = device_assertion(approval)
        except ApprovalRejected as exc:
            return blocked(exc.reason_code, context)
        return await asyncio.to_thread(
            service_for(context).approve_payee_addition,
            addition_id,
            approval.receipt_digest,
            assertion=assertion,
            context=approval_context(request),
        )

    @application.get("/api/face")
    async def face_status(request: Request, response: Response):
        context = resolve(request, response)
        return context.database.face_store.status(DEMO_USER_ID)

    @application.post("/api/face/register")
    async def face_register(request: Request, response: Response, body: FaceRegistrationRequest):
        context = resolve(request, response)
        frames = _decode_frames(body.frames)
        try:
            return await asyncio.to_thread(context.database.register_face, body.consent_id, frames)
        except FaceRejected as exc:
            raise _face_error(exc) from exc

    @application.post("/api/face/verify")
    async def face_verify(request: Request, response: Response, body: FaceVerificationRequest):
        context = resolve(request, response)
        frames = _decode_frames(body.frames)
        try:
            return await asyncio.to_thread(context.database.verify_face, body.challenge_id, frames)
        except FaceRejected as exc:
            raise _face_error(exc) from exc

    @application.post("/api/kyc/complete")
    async def kyc_complete(request: Request, response: Response, completion: KycCompletionRequest):
        context = resolve(request, response)
        try:
            completed = context.database.complete_kyc_onboarding(
                consent_id=completion.consent_id,
                challenge_id=completion.challenge_id,
                credential_kind=completion.credential_kind,
                credential_id=completion.credential_id,
                public_key_spki=b64url_decode(completion.public_key_spki),
                algorithm=completion.algorithm,
                client_data_json=b64url_decode(completion.client_data_json),
                authenticator_data=b64url_decode(completion.authenticator_data),
                signature=(
                    b64url_decode(completion.signature)
                    if completion.signature is not None
                    else None
                ),
                context=approval_context(request),
            )
        except ApprovalRejected as exc:
            raise HTTPException(status_code=400, detail=exc.reason_code) from exc
        if context.role == "visitor":
            # A verified customer's ledger is kept; only anonymous ones expire.
            manager.mark_registered(context.session_id)
        return completed

    @application.get("/api/payees")
    async def list_saved_payees(request: Request, response: Response):
        context = resolve(request, response)
        # The Pay tab's contact list. Nicknames and payment history ride
        # along so the client can show who "mum" or "Uncle Tan" is.
        history = {
            payee["beneficiary_id"]: payee for payee in context.database.list_payees()
        }
        listed = []
        for payee in context.database.list_saved_beneficiaries():
            extra = history.get(payee["beneficiary_id"], {})
            listed.append({
                "display_name": payee["display_name"],
                "paynow_proxy": payee.get("paynow_proxy"),
                "masked_destination": payee.get("masked_destination"),
                "aliases": extra.get("aliases", list(payee.get("aliases") or ())),
                "last_payment": extra.get("last_payment"),
                "payment_count": extra.get("count", 0),
                "is_new": extra.get("is_new", False),
            })
        return listed

    @application.post("/api/payees")
    async def save_payee(request: Request, response: Response, body: SavePayeeRequest):
        context = resolve(request, response)
        try:
            return context.database.save_payee(body.receipt_id, body.nickname)
        except PayeeSaveRejected as exc:
            raise HTTPException(status_code=400, detail=exc.reason_code) from exc

    async def receipt_challenge(
        kind: str, receipt_id: str, request: Request, response: Response, digest: str
    ) -> dict:
        context = resolve(request, response)
        try:
            challenge = context.database.issue_approval_challenge(
                kind,
                receipt_id,
                digest,
                rp_id=approval_context(request).rp_id,
            )
        except ReceiptRejected as exc:
            return blocked(exc.reason_code, context)
        # Voice out: the read-back is built from the same stored review data
        # the confirmation window shows.
        text = build_read_back(challenge["review"])
        queue_text = getattr(application.state.speech_service, "queue_text", None)
        challenge["review"]["read_back"] = {
            "text": text,
            "audio_artifact_id": queue_text(text, context) if callable(queue_text) else None,
        }
        return challenge

    @application.post("/api/receipts/{receipt_id}/challenge")
    async def single_receipt_challenge(
        request: Request,
        response: Response,
        receipt_id: str,
        body: ReceiptChallengeRequest,
    ):
        return await receipt_challenge("single", receipt_id, request, response, body.receipt_digest)

    @application.post("/api/receipts/{receipt_id}/singpass")
    async def single_receipt_singpass(
        request: Request,
        response: Response,
        receipt_id: str,
        body: ReceiptChallengeRequest,
    ):
        context = resolve(request, response)
        try:
            return context.database.request_receipt_singpass_authorisation(
                "single", receipt_id, body.receipt_digest
            )
        except ReceiptRejected as exc:
            raise HTTPException(status_code=400, detail=exc.reason_code) from exc

    @application.post("/api/batch-receipts/{receipt_id}/challenge")
    async def batch_receipt_challenge(
        request: Request,
        response: Response,
        receipt_id: str,
        body: ReceiptChallengeRequest,
    ):
        return await receipt_challenge("batch", receipt_id, request, response, body.receipt_digest)

    @application.post("/api/batch-receipts/{receipt_id}/singpass")
    async def batch_receipt_singpass(
        request: Request,
        response: Response,
        receipt_id: str,
        body: ReceiptChallengeRequest,
    ):
        context = resolve(request, response)
        try:
            return context.database.request_receipt_singpass_authorisation(
                "batch", receipt_id, body.receipt_digest
            )
        except ReceiptRejected as exc:
            raise HTTPException(status_code=400, detail=exc.reason_code) from exc

    def device_assertion(approval: ReceiptApprovalRequest) -> DeviceAssertion:
        return DeviceAssertion.from_wire(approval.assertion.model_dump())

    @application.post("/api/receipts/{receipt_id}/approve")
    async def approve_receipt(
        request: Request,
        response: Response,
        receipt_id: str,
        approval: ReceiptApprovalRequest,
    ):
        context = resolve(request, response)
        try:
            assertion = device_assertion(approval)
        except ApprovalRejected as exc:
            return blocked(exc.reason_code, context)
        result = service_for(context).approve(
            receipt_id,
            approval.receipt_digest,
            assertion=assertion,
            context=approval_context(request),
            out_of_band_code=approval.out_of_band_code,
            read_back_played=approval.read_back_played,
            unusual_amount_confirmed=approval.unusual_amount_confirmed,
            voice_confirmation=approval.voice_confirmation,
        )
        update_multi_action_status(context, "receipt", receipt_id, result)
        return await attach_speech(result, context)

    @application.post("/api/batch-receipts/{receipt_id}/approve")
    async def approve_batch_receipt(
        request: Request,
        response: Response,
        receipt_id: str,
        approval: ReceiptApprovalRequest,
    ):
        context = resolve(request, response)
        try:
            assertion = device_assertion(approval)
        except ApprovalRejected as exc:
            return blocked(exc.reason_code, context)
        result = service_for(context).approve_batch(
            receipt_id,
            approval.receipt_digest,
            assertion=assertion,
            context=approval_context(request),
            out_of_band_code=approval.out_of_band_code,
            read_back_played=approval.read_back_played,
            unusual_amount_confirmed=approval.unusual_amount_confirmed,
            voice_confirmation=approval.voice_confirmation,
        )
        update_multi_action_status(context, "batch_receipt", receipt_id, result)
        return await attach_speech(result, context)

    @application.post("/api/clarifications/{clarification_id}/respond")
    async def respond_to_clarification(
        request: Request,
        response: Response,
        clarification_id: str,
        clarification: ClarificationResponseRequest,
    ):
        context = resolve(request, response)
        result = service_for(context).respond_to_clarification(
            clarification_id,
            str(clarification.request_id),
            clarification.text,
        )
        return await attach_speech(result, context)

    @application.post("/api/reset")
    async def reset_demo(request: Request, response: Response):
        context = resolve(request, response)
        return context.database.reset()

    @application.websocket("/api/voice/session")
    async def voice_session(websocket: WebSocket):
        try:
            context = manager.resolve_headers(
                host=websocket.headers.get("host", "").lower(),
                origin=websocket.headers.get("origin"),
                cookie=websocket.cookies.get(manager.cookie_name),
                access_assertion=websocket.headers.get("cf-access-jwt-assertion"),
            )
        except SessionBoundaryError as exc:
            await websocket.accept()
            await websocket.send_json({"type": "error", "reason_code": exc.reason_code})
            await websocket.close(code=1008)
            return
        if context.database.lock_status()["locked"]:
            await websocket.accept()
            await websocket.send_json({"type": "error", "reason_code": "APP_LOCKED"})
            await websocket.close(code=1008)
            return

        conversation_id = websocket.query_params.get("conversation_id")
        expected_version_raw = websocket.query_params.get("expected_version")
        clarification_id = websocket.query_params.get("clarification_id")
        if clarification_id is not None and (
            conversation_id is not None or expected_version_raw is not None
        ):
            await websocket.accept()
            await websocket.send_json(
                {"type": "error", "reason_code": "VOICE_CONTEXT_CONFLICT"}
            )
            await websocket.close(code=1008)
            return
        if (conversation_id is None) != (expected_version_raw is None):
            await websocket.accept()
            await websocket.send_json(
                {"type": "error", "reason_code": "CONVERSATION_QUERY_INCOMPLETE"}
            )
            await websocket.close(code=1008)
            return
        expected_version = None
        if expected_version_raw is not None:
            try:
                expected_version = int(expected_version_raw)
            except ValueError:
                expected_version = None
            if expected_version is None or expected_version < 1:
                await websocket.accept()
                await websocket.send_json(
                    {"type": "error", "reason_code": "CONVERSATION_VERSION_INVALID"}
                )
                await websocket.close(code=1008)
                return

        async def dispatch(text: str, evidence: dict):
            locked = voice_locked_result(context)
            if locked is not None:
                return locked
            if clarification_id is not None:
                # Spoken follow-ups often include a short lead-in. Keep the
                # clarification's strict amount parser authoritative while
                # stripping only common, explicit amount-answer phrasing.
                amount_reply = text.strip()
                amount_lead_in = re.match(
                    r"^(?:i want to send|i would like to send|i'd like to send|"
                    r"the amount is|make it|send)\s+(.+?)[.!?]*$",
                    amount_reply,
                    re.IGNORECASE,
                )
                if amount_lead_in:
                    amount_reply = amount_lead_in.group(1).strip()
                try:
                    result = await asyncio.to_thread(
                        service_for(context).respond_to_clarification,
                        clarification_id,
                        str(uuid4()),
                        amount_reply,
                    )
                except ClarificationRejected as exc:
                    return {
                        "outcome": "BLOCKED",
                        "reason_code": exc.reason_code,
                        "trace": [{"stage": exc.reason_code}],
                    }
                locked = voice_locked_result(context)
                return locked if locked is not None else await attach_speech(result, context)
            if conversation_id is not None and expected_version is not None:
                try:
                    result = await asyncio.to_thread(
                        conversation_service_for(context).apply_turn,
                        conversation_id,
                        session_scope=context.session_id,
                        user_id=DEMO_USER_ID,
                        request_id=str(uuid4()),
                        expected_version=expected_version,
                        source="voice",
                        conversation_input=CoreConversationInput(
                            InputKind.TEXT, text=text
                        ),
                    )
                except ConversationRejected as exc:
                    return {
                        "outcome": "BLOCKED",
                        "reason_code": exc.reason_code,
                        "trace": [{"stage": exc.reason_code}],
                    }
                return await finalize_voice_action_result(
                    result, context, str(result.get("request_id") or uuid4())
                )
            action_request_id = str(uuid4())
            result = await asyncio.to_thread(
                service_for(context).process,
                action_request_id,
                text,
                source="voice",
                voice=evidence,
            )
            return await finalize_voice_action_result(
                result, context, action_request_id
            )

        await application.state.voice_bridge.handle(websocket, context, dispatch)

    @application.get("/api/trtc")
    async def trtc_features(request: Request, response: Response):
        resolve(request, response)
        return application.state.trtc_features

    def trtc_assistant_service() -> trtc.TrtcConversationService:
        assistant = application.state.trtc_assistant
        if assistant is None:
            raise HTTPException(status_code=503, detail="TRTC_ASSISTANT_DISABLED")
        return assistant

    def trtc_failure(exc: trtc.TrtcError) -> HTTPException:
        logger.warning("trtc_failure reason=%s request_id=%s", exc.reason_code, exc.request_id)
        return HTTPException(status_code=502, detail=exc.reason_code)

    @application.post("/api/trtc/assistant/join")
    async def trtc_assistant_join(request: Request, response: Response):
        """Room credentials for the browser. Only a UserSig leaves the server."""

        context = resolve(request, response)
        try:
            return await trtc_assistant_service().join(context.session_id)
        except trtc.TrtcError as exc:
            raise trtc_failure(exc) from exc

    @application.post("/api/trtc/assistant/start")
    async def trtc_assistant_start(request: Request, response: Response):
        context = resolve(request, response)
        try:
            return await trtc_assistant_service().start(context.session_id)
        except trtc.TrtcError as exc:
            raise trtc_failure(exc) from exc

    @application.post("/api/trtc/assistant/stop")
    async def trtc_assistant_stop(request: Request, response: Response):
        context = resolve(request, response)
        return {"stopped": await trtc_assistant_service().stop(context.session_id)}

    # ------------------------------------------------------------ voice
    @application.get("/api/voiceprint")
    async def voiceprint_status(request: Request, response: Response):
        context = resolve(request, response)
        return context.database.voice_status()

    @application.post("/api/voiceprint/enrollments")
    async def voiceprint_enrollment(
        request: Request, response: Response, body: VoiceEnrollmentRequest | None = None
    ):
        context = resolve(request, response)
        voice_key = None
        if body is not None and body.voice_key is not None:
            key = body.voice_key
            voice_key = {
                "challenge_id": key.challenge_id,
                "credential_kind": key.credential_kind,
                "credential_id": key.credential_id,
                "public_key_spki": b64url_decode(key.public_key_spki),
                "algorithm": key.algorithm,
                "client_data_json": b64url_decode(key.client_data_json),
                "authenticator_data": b64url_decode(key.authenticator_data),
                "signature": b64url_decode(key.signature) if key.signature is not None else None,
            }
        try:
            return await asyncio.to_thread(
                context.database.start_voice_enrollment,
                voice_key=voice_key, context=approval_context(request),
            )
        except VoiceRejected as exc:
            status = 503 if exc.reason_code == "VOICE_ENGINE_UNAVAILABLE" else 400
            raise HTTPException(status_code=status, detail=exc.reason_code) from exc

    @application.post("/api/voice-enrollments/{enrollment_id}/challenge")
    async def voice_enrollment_challenge(
        request: Request, response: Response, enrollment_id: str, body: ReceiptChallengeRequest
    ):
        context = resolve(request, response)
        try:
            return context.database.issue_voice_enrollment_challenge(
                enrollment_id, body.receipt_digest, rp_id=approval_context(request).rp_id
            )
        except ReceiptRejected as exc:
            return blocked(exc.reason_code, context)

    @application.post("/api/voice-enrollments/{enrollment_id}/approve")
    async def voice_enrollment_approve(
        request: Request, response: Response, enrollment_id: str, approval: ReceiptApprovalRequest
    ):
        context = resolve(request, response)
        try:
            assertion = device_assertion(approval)
        except ApprovalRejected as exc:
            return blocked(exc.reason_code, context)
        try:
            return await asyncio.to_thread(
                context.database.approve_voice_enrollment, enrollment_id, approval.receipt_digest,
                assertion=assertion, context=approval_context(request),
            )
        except ReceiptRejected as exc:
            return blocked(exc.reason_code, context)

    @application.post("/api/voice/phrase")
    async def voice_phrase(request: Request, response: Response, body: VoicePhraseRequest):
        """The confirmation phrase bound to an approval under S$200."""

        context = resolve(request, response)
        try:
            return context.database.issue_voice_phrase(body.challenge_id)
        except VoiceRejected as exc:
            raise HTTPException(status_code=400, detail=exc.reason_code) from exc

    @application.websocket("/api/voice/hands-free")
    async def hands_free(websocket: WebSocket):
        try:
            context = manager.resolve_headers(
                host=websocket.headers.get("host", "").lower(),
                origin=websocket.headers.get("origin"),
                cookie=websocket.cookies.get(manager.cookie_name),
                access_assertion=websocket.headers.get("cf-access-jwt-assertion"),
            )
        except SessionBoundaryError as exc:
            await websocket.accept()
            await websocket.send_json({"type": "error", "reason_code": exc.reason_code})
            await websocket.close(code=1008)
            return
        if context.database.lock_status()["locked"]:
            await websocket.accept()
            await websocket.send_json({"type": "error", "reason_code": "APP_LOCKED"})
            await websocket.close(code=1008)
            return

        async def dispatch(text: str, evidence: dict):
            locked = voice_locked_result(context)
            if locked is not None:
                return locked
            conversation_id = evidence.pop("conversation_id", None)
            expected_version = evidence.pop("expected_version", None)
            if (
                isinstance(conversation_id, str)
                and type(expected_version) is int
                and expected_version >= 1
            ):
                try:
                    result = await asyncio.to_thread(
                        conversation_service_for(context).apply_turn,
                        conversation_id,
                        session_scope=context.session_id,
                        user_id=DEMO_USER_ID,
                        request_id=str(uuid4()),
                        expected_version=expected_version,
                        source="voice",
                        conversation_input=CoreConversationInput(
                            InputKind.TEXT, text=text
                        ),
                    )
                except ConversationRejected as exc:
                    return {
                        "outcome": "BLOCKED",
                        "reason_code": exc.reason_code,
                        "trace": [{"stage": exc.reason_code}],
                    }
                return await finalize_voice_action_result(
                    result, context, str(result.get("request_id") or uuid4())
                )
            action_request_id = str(uuid4())
            result = await asyncio.to_thread(
                service_for(context).process,
                action_request_id,
                text,
                source="voice",
                voice=evidence,
            )
            return await finalize_voice_action_result(
                result, context, action_request_id
            )

        await application.state.hands_free.handle(websocket, context, dispatch)

    @application.post("/api/voice/diagnostics", status_code=204)
    async def voice_client_diagnostic(
        request: Request,
        response: Response,
        diagnostic: VoiceClientDiagnostic,
    ):
        resolve(request, response)
        logger.warning(
            "voice_client_failure request_id=%s stage=%s error_name=%s",
            diagnostic.request_id or "",
            diagnostic.stage,
            diagnostic.error_name,
        )
        response.status_code = 204
        return response

    @application.post("/api/speech/prompts")
    async def speech_prompt(
        request: Request,
        response: Response,
        body: SpeechPromptRequest,
    ):
        context = resolve(request, response)
        try:
            text = render_hands_free_prompt(body.prompt_key, body.values)
        except SpeechPromptRejected as exc:
            raise HTTPException(status_code=400, detail="SPEECH_PROMPT_INVALID") from exc
        artifact_id = application.state.speech_service.queue_text(text, context)
        if not artifact_id:
            raise HTTPException(status_code=503, detail="SPEECH_UNAVAILABLE")
        return {"audio_artifact_id": artifact_id}

    @application.get("/api/audio/{artifact_id}")
    async def get_audio(artifact_id: str, request: Request, response: Response):
        context = resolve(request, response)
        if application.state.speech_service.is_pending(artifact_id, context):
            return Response(
                status_code=202,
                headers={"Retry-After": "0.25", "Cache-Control": "no-store"},
            )
        data = await application.state.speech_service.read(artifact_id, context)
        if data is None:
            raise HTTPException(status_code=404, detail="audio not found")
        return Response(content=data, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @application.get("/api/health")
    async def health(request: Request):
        if os.environ.get("DIMON_LOCAL_HEALTH") != "1":
            raise HTTPException(status_code=404, detail="not found")
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise HTTPException(status_code=404, detail="not found")
        return {
            "status": "ready",
            "demo_mode": True,
            "paper_equity_enabled": application.state.paper_equity_enabled,
            "model_endpoint": os.environ.get(
                "DIMON_QWEN_API_BASE", "http://127.0.0.1:8080/v1"
            ),
        }

    @application.get("/", include_in_schema=False)
    async def root(request: Request):
        if manager.legacy_database is None:
            try:
                manager._assert_origin(
                    request.headers.get("origin"),
                    request.headers.get("host", "").lower(),
                )
            except SessionBoundaryError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.reason_code) from exc
        return HTMLResponse(
            (STATIC_DIR / "index.html").read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/presenter", include_in_schema=False)
    async def presenter(request: Request):
        """Split screen: the phone beside a live, plain-language audit log."""

        if manager.legacy_database is None:
            try:
                manager._assert_origin(
                    request.headers.get("origin"),
                    request.headers.get("host", "").lower(),
                )
            except SessionBoundaryError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.reason_code) from exc
        return HTMLResponse(
            (STATIC_DIR / "presenter.html").read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/favicon.ico", include_in_schema=False)
    @application.get("/favicon.svg", include_in_schema=False)
    async def favicon():
        return Response(
            (STATIC_DIR / "favicon.svg").read_text(encoding="utf-8"),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @application.get("/static/presenter.js", include_in_schema=False)
    async def presenter_script():
        return Response(
            (STATIC_DIR / "presenter.js").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/app.css", include_in_schema=False)
    async def stylesheet():
        return Response(
            (STATIC_DIR / "app.css").read_text(encoding="utf-8"),
            media_type="text/css",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/app.js", include_in_schema=False)
    async def script():
        return Response(
            (STATIC_DIR / "app.js").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/voice_diagnostics.mjs", include_in_schema=False)
    async def voice_diagnostics_script():
        return Response(
            (STATIC_DIR / "voice_diagnostics.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/audio/wake-greeting-v1.wav", include_in_schema=False)
    async def wake_greeting_audio():
        return Response(
            (STATIC_DIR / "audio" / "wake-greeting-v1.wav").read_bytes(),
            media_type="audio/wav",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @application.get("/static/audio/voice-enrollment-confirmation-v1.wav", include_in_schema=False)
    async def voice_enrollment_confirmation_audio():
        return Response(
            (STATIC_DIR / "audio" / "voice-enrollment-confirmation-v1.wav").read_bytes(),
            media_type="audio/wav",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @application.get("/static/transaction_display.mjs", include_in_schema=False)
    async def transaction_display_script():
        return Response(
            (STATIC_DIR / "transaction_display.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/multi_action_review.mjs", include_in_schema=False)
    async def multi_action_review_script():
        return Response(
            (STATIC_DIR / "multi_action_review.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/digiwealth-nav-icon.svg", include_in_schema=False)
    async def digiwealth_nav_icon():
        return Response(
            (STATIC_DIR / "digiwealth-nav-icon.svg").read_text(encoding="utf-8"),
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/receipt_approval.mjs", include_in_schema=False)
    async def receipt_approval_script():
        return Response(
            (STATIC_DIR / "receipt_approval.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
        )

    @application.get("/static/invest_ui.mjs", include_in_schema=False)
    async def invest_ui_script():
        return Response(
            (STATIC_DIR / "invest_ui.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/paper_equity_ui.mjs", include_in_schema=False)
    async def paper_equity_ui_script():
        return Response(
            (STATIC_DIR / "paper_equity_ui.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/face_capture.mjs", include_in_schema=False)
    async def face_capture_script():
        return Response(
            (STATIC_DIR / "face_capture.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/device_approval.mjs", include_in_schema=False)
    async def device_approval_script():
        return Response(
            (STATIC_DIR / "device_approval.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/batch_receipt_approval.mjs", include_in_schema=False)
    async def batch_receipt_approval_script():
        return Response(
            (STATIC_DIR / "batch_receipt_approval.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
        )

    @application.get("/static/clarification_response.mjs", include_in_schema=False)
    async def clarification_response_script():
        return Response(
            (STATIC_DIR / "clarification_response.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
        )

    @application.get("/static/action_request.mjs", include_in_schema=False)
    async def action_request_script():
        return Response(
            (STATIC_DIR / "action_request.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
        )

    @application.get("/static/account_display.mjs", include_in_schema=False)
    async def account_display_script():
        return Response(
            (STATIC_DIR / "account_display.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
        )

    @application.get("/static/banking_shell.mjs", include_in_schema=False)
    async def banking_shell_script():
        return Response(
            (STATIC_DIR / "banking_shell.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/voice_capture.mjs", include_in_schema=False)
    async def voice_capture_script():
        return Response(
            (STATIC_DIR / "voice_capture.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/voice_capture_worklet.js", include_in_schema=False)
    async def voice_capture_worklet():
        return Response(
            (STATIC_DIR / "voice_capture_worklet.js").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/trtc_assistant.mjs", include_in_schema=False)
    async def trtc_assistant_script():
        return Response(
            (STATIC_DIR / "trtc_assistant.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/hands_free.mjs", include_in_schema=False)
    async def hands_free_script():
        return Response(
            (STATIC_DIR / "hands_free.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/multi_request.mjs", include_in_schema=False)
    async def multi_request_script():
        return Response(
            (STATIC_DIR / "multi_request.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/voice_failure.mjs", include_in_schema=False)
    async def voice_failure_script():
        return Response(
            (STATIC_DIR / "voice_failure.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/result_audio.mjs", include_in_schema=False)
    async def result_audio_script():
        return Response(
            (STATIC_DIR / "result_audio.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/identity_feedback.mjs", include_in_schema=False)
    async def identity_feedback_script():
        return Response(
            (STATIC_DIR / "identity_feedback.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/static/audio_prebuffer.mjs", include_in_schema=False)
    async def audio_prebuffer_script():
        return Response(
            (STATIC_DIR / "audio_prebuffer.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    return application


_configured_db = os.environ.get("DIMON_DB_PATH")
app = create_app(Path(_configured_db) if _configured_db else None)

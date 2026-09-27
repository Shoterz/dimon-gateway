"""FastAPI boundary for the local Dimon Live prototype."""

from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .database import Database
from .inference import InferenceProviders
from .model_adapter import Extractor
from .provider_factory import build_inference_providers
from .service import ActionService
from .session_manager import SessionBoundaryError, SessionContext, SessionManager
from .speech import SpeechService
from .voice import VoiceBridge


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DEFAULT_DB_PATH = APP_DIR / "data" / "demo.sqlite3"

# Local defaults are convenient for the demo, while explicit shell/service
# variables remain authoritative for comparisons and deployment.
load_dotenv(APP_DIR / ".env", override=False)
logger = logging.getLogger(__name__)


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


class ReceiptApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


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


class VoiceClientDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: Literal[
        "audio_context",
        "microphone",
        "worklet_module",
        "worklet_node",
        "socket",
    ]
    error_name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]


def create_app(
    db_path: Path | None = None,
    *,
    extractor: Extractor | None = None,
    session_manager: SessionManager | None = None,
    owner_path: Path | None = None,
    visitor_dir: Path | None = None,
    allowed_origins: set[str] | None = None,
    voice_bridge: VoiceBridge | None = None,
    speech_service: SpeechService | None = None,
    providers: InferenceProviders | None = None,
) -> FastAPI:
    if providers is not None and any(
        dependency is not None
        for dependency in (extractor, voice_bridge, speech_service)
    ):
        raise ValueError("providers cannot be combined with individual inference dependencies")
    selected_providers = providers or build_inference_providers(
        os.environ.get("DIMON_INFERENCE_MODE", "fixture"),
        os.environ,
    )
    legacy_database: Database | None = None
    if db_path is not None:
        legacy_database = Database(Path(db_path))
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
    selected_extractor = extractor or selected_providers.extractor
    application = FastAPI(title="Dimon Live", version="0.1.0")
    application.state.database = legacy_database
    application.state.session_manager = manager
    application.state.extractor = selected_extractor
    application.state.inference_mode = selected_providers.mode
    application.state.voice_bridge = voice_bridge or VoiceBridge(
        recognizer=selected_providers.recognizer,
        lifecycle=selected_providers.voice_lifecycle,
        max_audio_bytes=int(os.environ.get("DIMON_MAX_AUDIO_BYTES", "4000000")),
        max_utterance_seconds=float(os.environ.get("DIMON_MAX_UTTERANCE_SECONDS", "30")),
        endpoint_silence_seconds=float(os.environ.get("DIMON_VOICE_ENDPOINT_SILENCE_SECONDS", "1.4")),
        pause_seconds=float(os.environ.get("DIMON_VOICE_PAUSE_SECONDS", "0.3")),
        speech_threshold=float(os.environ.get("DIMON_VOICE_SPEECH_THRESHOLD", "0.015")),
    )
    application.state.speech_service = speech_service or SpeechService(
        base_url=os.environ.get("DIMON_KOKORO_URL", "http://127.0.0.1:3001"),
        audio_dir=Path(os.environ.get("DIMON_AUDIO_DIR", APP_DIR / "runtime" / "audio")),
        synthesizer=selected_providers.synthesizer,
        max_wav_bytes=int(os.environ.get("DIMON_MAX_WAV_BYTES", str(8 * 1_048_576)),),
        ttl_seconds=int(os.environ.get("DIMON_VISITOR_TTL_SECONDS", "3600")),
    )

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

    def service_for(context: SessionContext) -> ActionService:
        return ActionService(context.database, selected_extractor)

    async def attach_speech(result: dict, context: SessionContext) -> dict:
        artifact_id = application.state.speech_service.queue_result(result, context)
        if artifact_id is None:
            return result
        return {**result, "audio_artifact_id": artifact_id, "audio_pending": True}

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
        }

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

    @application.post("/api/actions")
    async def create_action(request: Request, response: Response, action: ActionRequest):
        context = resolve(request, response)
        result = service_for(context).process(str(action.request_id), action.text)
        return await attach_speech(result, context)

    @application.post("/api/receipts/{receipt_id}/approve")
    async def approve_receipt(
        request: Request,
        response: Response,
        receipt_id: str,
        approval: ReceiptApprovalRequest,
    ):
        context = resolve(request, response)
        result = service_for(context).approve(receipt_id, approval.receipt_digest)
        return await attach_speech(result, context)

    @application.post("/api/batch-receipts/{receipt_id}/approve")
    async def approve_batch_receipt(
        request: Request,
        response: Response,
        receipt_id: str,
        approval: ReceiptApprovalRequest,
    ):
        context = resolve(request, response)
        result = service_for(context).approve_batch(receipt_id, approval.receipt_digest)
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

        async def dispatch(text: str):
            result = service_for(context).process(str(uuid4()), text)
            return await attach_speech(result, context)

        await application.state.voice_bridge.handle(websocket, context, dispatch)

    @application.post("/api/voice/diagnostics", status_code=204)
    async def voice_client_diagnostic(
        request: Request,
        response: Response,
        diagnostic: VoiceClientDiagnostic,
    ):
        resolve(request, response)
        user_agent = request.headers.get("user-agent", "unknown")[:256]
        logger.warning(
            "voice_client_failure stage=%s error_name=%s user_agent=%r",
            diagnostic.stage,
            diagnostic.error_name,
            user_agent,
        )
        response.status_code = 204
        return response

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
                    request.headers.get("host", "").split(":", 1)[0],
                )
            except SessionBoundaryError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.reason_code) from exc
        return HTMLResponse(
            (STATIC_DIR / "index.html").read_text(encoding="utf-8"),
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

    @application.get("/static/receipt_approval.mjs", include_in_schema=False)
    async def receipt_approval_script():
        return Response(
            (STATIC_DIR / "receipt_approval.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
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

    @application.get("/static/voice_failure.mjs", include_in_schema=False)
    async def voice_failure_script():
        return Response(
            (STATIC_DIR / "voice_failure.mjs").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    return application


_configured_db = os.environ.get("DIMON_DB_PATH")
app = create_app(Path(_configured_db) if _configured_db else None)

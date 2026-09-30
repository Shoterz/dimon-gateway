"""FastAPI boundary for the local Dimon Live prototype."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import base64
import binascii
import os
import logging
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .approval_gateway import (
    ApprovalContext,
    ApprovalRejected,
    DeviceAssertion,
    b64url_decode,
)
from .database import Database, ReceiptRejected
from .face_id import MAX_FRAMES, MIN_FRAMES, FaceRejected
from . import investing, market, singpass
from .payees import PayeeSaveRejected
from .read_back import build_read_back
from .tencent_tts import read_back_synthesizer_from_env
from .inference import InferenceProviders
from .model_adapter import Extractor
from .provider_factory import build_inference_providers
from .receipts import DEMO_USER_ID
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


class ReceiptChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_digest: ReceiptDigest


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
    face_engine: Any = None,
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
        legacy_database = Database(Path(db_path), face_engine=face_engine)
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
        evidence_dir=Path(os.environ.get("DIMON_VOICE_EVIDENCE_DIR", "runtime/evidence")),
    )
    application.state.speech_service = speech_service or SpeechService(
        base_url=os.environ.get("DIMON_KOKORO_URL", "http://127.0.0.1:3001"),
        audio_dir=Path(os.environ.get("DIMON_AUDIO_DIR", APP_DIR / "runtime" / "audio")),
        synthesizer=selected_providers.synthesizer,
        max_wav_bytes=int(os.environ.get("DIMON_MAX_WAV_BYTES", str(8 * 1_048_576)),),
        ttl_seconds=int(os.environ.get("DIMON_VISITOR_TTL_SECONDS", "3600")),
        read_back_synthesizer=read_back_synthesizer_from_env(
            os.environ, selected_providers.synthesizer
        ),
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

    @application.get("/api/invest")
    async def invest_overview(request: Request, response: Response):
        context = resolve(request, response)
        return await asyncio.to_thread(context.database.investing_overview)

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
        return await attach_speech(result, context)

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
            return context.database.complete_kyc_onboarding(
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

    @application.post("/api/batch-receipts/{receipt_id}/challenge")
    async def batch_receipt_challenge(
        request: Request,
        response: Response,
        receipt_id: str,
        body: ReceiptChallengeRequest,
    ):
        return await receipt_challenge("batch", receipt_id, request, response, body.receipt_digest)

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
        )
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
        )
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

        async def dispatch(text: str, evidence: dict):
            result = service_for(context).process(
                str(uuid4()), text, source="voice", voice=evidence
            )
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

"""FastAPI composition root for the bounded inference gateway."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from apps.dimon_live.gpu_overlord_status import GpuOverlordStatusClient
from apps.dimon_live.model_adapter import InvalidModelOutput, validate_intent
from apps.dimon_live.voice import VoiceLease
from apps.dimon_live.speech import (
    PROMPT_INJECTION_BLOCKED_SPEECH,
    build_order_review_summary,
    build_result_summary,
    build_stock_allocation_summary,
)

from .auth import require_service_key
from .config import GatewayConfig
from .contracts import IntentRequest, QwenTaskRequest, SpeechRequest
from .errors import GatewayError, gateway_error_response
from .idempotency import IdempotencyStore
from .limits import BoundedExecutor, SlidingWindowLimiter
from .observability import (
    GatewayObservability,
    ReadinessProbe,
    ReadinessService,
    default_readiness_probes,
)
from .speech import (
    KokoroGatewaySynthesizer,
    SpeechSynthesizer,
    validate_synthesis,
)
from .preparation import GatewayPreparation
from .gpu_reset import SystemdGpuStackResetter
from .residency import FourWorkerResidencyGate, ProbeEvidence
from .transcription import (
    DualTranscriptionRecognizer,
    FasterWhisperPcm16Recognizer,
    NemotronPcm16Recognizer,
    TranscriptionGateway,
    TranscriptionRecognizer,
)
from .upstreams import IntentUpstream, QwenIntentUpstream, QwenTaskUpstream
from .worker_probes import FasterWhisperRuntimeProbe, QwenRuntimeProbe


async def _read_json(request: Request, *, max_bytes: int = 16_384) -> object:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise GatewayError("INVALID_REQUEST", "request body is too large", 400)
        except ValueError as exc:
            raise GatewayError("INVALID_REQUEST", "content length is invalid", 400) from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise GatewayError("INVALID_REQUEST", "request body is too large", 400)
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GatewayError("INVALID_REQUEST", "request body must be valid JSON", 400) from exc


def _fingerprint(request: IntentRequest) -> str:
    canonical = json.dumps(
        {"contract_version": request.contract_version, "text": request.text},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _qwen_task_fingerprint(request: QwenTaskRequest) -> str:
    canonical = json.dumps(
        {
            "task": request.task,
            "text": request.text,
            "context": request.context.model_dump(mode="json") if request.context else None,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _bounded_text(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def create_gateway_app(
    config: GatewayConfig,
    *,
    intent_upstream: IntentUpstream | None = None,
    intent_executor: BoundedExecutor | None = None,
    intent_limiter: SlidingWindowLimiter | None = None,
    idempotency_store: IdempotencyStore | None = None,
    transcription_recognizer: TranscriptionRecognizer | None = None,
    transcription_lease=None,
    transcription_limiter: SlidingWindowLimiter | None = None,
    speech_synthesizer: SpeechSynthesizer | None = None,
    speech_executor: BoundedExecutor | None = None,
    speech_limiter: SlidingWindowLimiter | None = None,
    readiness_probes: Mapping[str, ReadinessProbe] | None = None,
    readiness_timeout: float = 1.0,
    observability: GatewayObservability | None = None,
    preparation=None,
) -> FastAPI:
    application = FastAPI(
        title="Dimon Inference Gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.config = config
    application.state.intent_upstream = intent_upstream or QwenIntentUpstream(config)
    application.state.qwen_task_upstream = QwenTaskUpstream(config)
    application.state.intent_executor = intent_executor or BoundedExecutor(1, 4, 120)
    application.state.intent_limiter = intent_limiter or SlidingWindowLimiter(12, 60)
    application.state.idempotency_store = idempotency_store or IdempotencyStore()
    if transcription_recognizer is None:
        if config.transcription_backend == "whisper_cpu":
            transcription_recognizer = FasterWhisperPcm16Recognizer(
                config.transcription_url
            )
        elif config.transcription_backend == "dual":
            transcription_recognizer = DualTranscriptionRecognizer(
                fallback=FasterWhisperPcm16Recognizer(config.transcription_url),
                primary=NemotronPcm16Recognizer(
                    config.nemotron_url,
                    control_socket=config.nemotron_control_socket,
                ),
            )
        else:
            transcription_recognizer = NemotronPcm16Recognizer(
                config.nemotron_url,
                control_socket=config.nemotron_control_socket,
            )
    application.state.transcription_gateway = TranscriptionGateway(
        service_key=config.service_key,
        recognizer=transcription_recognizer,
        lease=transcription_lease or VoiceLease(1),
        limiter=transcription_limiter or SlidingWindowLimiter(120, 60),
    )
    application.state.speech_synthesizer = (
        speech_synthesizer or KokoroGatewaySynthesizer(config)
    )
    application.state.speech_executor = speech_executor or BoundedExecutor(1, 4, 30)
    application.state.speech_limiter = speech_limiter or SlidingWindowLimiter(20, 60)
    if preparation is None:
        gpu_status = GpuOverlordStatusClient(config.gpu_overlord_socket)
        gpu_resetter = SystemdGpuStackResetter(
            gpu_status,
            overlord_socket_path=config.gpu_overlord_socket,
            stop_units=config.gpu_reset_units,
        )
        residency_gate = FourWorkerResidencyGate(gpu_status)
        qwen_probe = QwenRuntimeProbe(
            config.qwen_api_base,
            config.qwen_model,
            gpu_status,
        )
        if config.whisper_url is not None:
            faster_whisper_probe = FasterWhisperRuntimeProbe(config.whisper_url)
        else:
            async def faster_whisper_probe() -> ProbeEvidence:
                return ProbeEvidence(
                    "faster_whisper",
                    0,
                    False,
                    "FASTER_WHISPER_UNAVAILABLE",
                )

        preparation = GatewayPreparation(
            recognizer=transcription_recognizer,
            intent_upstream=application.state.intent_upstream,
            speech_synthesizer=application.state.speech_synthesizer,
            residency_gate=residency_gate,
            qwen_probe=qwen_probe,
            faster_whisper_probe=faster_whisper_probe,
            transcription_lease=application.state.transcription_gateway.lease,
            worker_resetter=gpu_resetter.reset,
        )
    application.state.preparation = preparation
    application.state.observability = observability or GatewayObservability()
    application.state.readiness = ReadinessService(
        readiness_probes or default_readiness_probes(config),
        {
            "intent": config.qwen_revision,
            "transcription": config.transcription_revision,
            "speech": config.kokoro_revision,
        },
        timeout=readiness_timeout,
    )

    async def stop_preparation_monitor() -> None:
        await application.state.preparation.stop()

    application.router.add_event_handler("shutdown", stop_preparation_monitor)
    application.add_exception_handler(GatewayError, gateway_error_response)

    @application.get("/v1/prepare")
    async def preparation_status(request: Request):
        await require_service_key(request)
        return await application.state.preparation.status()

    @application.post("/v1/prepare")
    async def start_preparation(request: Request):
        await require_service_key(request)
        body = await _read_json(request, max_bytes=64)
        if body != {}:
            raise GatewayError("INVALID_REQUEST", "preparation request must be empty", 400)
        return JSONResponse(await application.state.preparation.start(), status_code=202)

    @application.middleware("http")
    async def observe_request(request: Request, call_next):
        observed = application.state.observability
        started = observed.clock()
        request.state.correlation_id = observed.correlation_id(
            request.headers.get("x-dimon-correlation-id")
        )
        request.state.outcome_code = "OK"
        request.state.queue_delay_ms = 0.0
        try:
            response = await call_next(request)
        except BaseException:
            request.state.outcome_code = "INTERNAL_ERROR"
            observed.record_http(
                request,
                outcome_code=request.state.outcome_code,
                latency_ms=(observed.clock() - started) * 1000,
            )
            raise
        response.headers["X-Dimon-Correlation-Id"] = request.state.correlation_id
        if response.status_code >= 400 and request.state.outcome_code == "OK":
            request.state.outcome_code = "HTTP_ERROR"
        observed.record_http(
            request,
            outcome_code=request.state.outcome_code,
            latency_ms=(observed.clock() - started) * 1000,
        )
        return response

    @application.get("/health/live")
    async def health_live(request: Request):
        await require_service_key(request)
        return {"status": "live"}

    @application.get("/health/ready")
    async def health_ready(request: Request):
        await require_service_key(request)
        ready, capabilities = await application.state.readiness.check()
        if not ready:
            request.state.outcome_code = "NOT_READY"
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not_ready",
                "capabilities": capabilities,
            },
        )

    @application.post("/v1/intent")
    async def create_intent(request: Request):
        identity_subject = await require_service_key(request)
        try:
            intent_request = IntentRequest.model_validate(await _read_json(request))
        except ValidationError as exc:
            raise GatewayError("INVALID_REQUEST", "intent request is invalid", 400) from exc
        request_id = str(intent_request.request_id)
        fingerprint = _fingerprint(intent_request)
        store = application.state.idempotency_store
        decision = store.begin(identity_subject, request_id, fingerprint)
        if decision.state == "conflict":
            raise GatewayError(
                "IDEMPOTENCY_CONFLICT",
                "request ID was already used with different input",
                409,
                request_id,
            )
        if decision.state == "replay":
            return await decision.resolve()

        retry_after = application.state.intent_limiter.check(identity_subject)
        if retry_after is not None:
            error = GatewayError(
                "RATE_LIMITED",
                "intent rate limit exceeded",
                429,
                request_id,
            )
            store.abort(identity_subject, request_id, fingerprint, error)
            raise error

        async def invoke() -> dict[str, object]:
            try:
                extracted = await application.state.intent_upstream.extract(
                    intent_request.text
                )
                if not isinstance(extracted.raw_output, str):
                    raise TypeError("raw output must be text")
                intent = validate_intent(extracted.raw_output)
                latency_ms = float(extracted.latency_ms)
                if not math.isfinite(latency_ms) or latency_ms < 0:
                    raise ValueError("latency must be finite and non-negative")
            except GatewayError:
                raise
            except Exception as exc:
                raise GatewayError(
                    "UPSTREAM_UNAVAILABLE",
                    "intent upstream did not return a valid response",
                    503,
                    request_id,
                ) from exc
            return {
                "request_id": request_id,
                "intent": intent,
                "raw_output": _bounded_text(
                    extracted.raw_output,
                    config.max_raw_output_bytes,
                ),
                "model": config.qwen_model,
                "revision": config.qwen_revision,
                "latency_ms": latency_ms,
            }

        try:
            response = await application.state.intent_executor.run(
                invoke,
                on_acquired=lambda value: setattr(
                    request.state, "queue_delay_ms", value
                ),
            )
        except BaseException as exc:
            if isinstance(exc, GatewayError) and exc.request_id is None:
                exc.request_id = request_id
            store.abort(identity_subject, request_id, fingerprint, exc)
            raise
        store.succeed(identity_subject, request_id, fingerprint, response)
        return response

    @application.post("/v1/qwen")
    async def run_qwen_task(request: Request):
        identity_subject = await require_service_key(request)
        try:
            task_request = QwenTaskRequest.model_validate(await _read_json(request))
        except ValidationError as exc:
            raise GatewayError("INVALID_REQUEST", "Qwen task request is invalid", 400) from exc
        request_id = str(task_request.request_id)
        fingerprint = _qwen_task_fingerprint(task_request)
        store = application.state.idempotency_store
        decision = store.begin(identity_subject, request_id, fingerprint)
        if decision.state == "conflict":
            raise GatewayError(
                "IDEMPOTENCY_CONFLICT",
                "request ID was already used with different input",
                409,
                request_id,
            )
        if decision.state == "replay":
            return await decision.resolve()
        if application.state.intent_limiter.check(identity_subject) is not None:
            error = GatewayError("RATE_LIMITED", "Qwen rate limit exceeded", 429, request_id)
            store.abort(identity_subject, request_id, fingerprint, error)
            raise error

        async def invoke() -> dict[str, object]:
            context = (
                task_request.context.model_dump(exclude_none=True)
                if task_request.context is not None
                else None
            )
            try:
                raw_output, latency_ms = await application.state.qwen_task_upstream.complete(
                    task_request.task,
                    task_request.text,
                    context,
                )
            except InvalidModelOutput as exc:
                raise GatewayError(
                    "INVALID_MODEL_OUTPUT", "Qwen returned an invalid response", 502, request_id
                ) from exc
            except GatewayError as exc:
                if exc.request_id is None:
                    exc.request_id = request_id
                raise
            return {
                "request_id": request_id,
                "task": task_request.task,
                "raw_output": _bounded_text(raw_output, config.max_raw_output_bytes),
                "model": config.qwen_model,
                "revision": config.qwen_revision,
                "latency_ms": latency_ms,
            }

        try:
            response = await application.state.intent_executor.run(
                invoke,
                on_acquired=lambda value: setattr(request.state, "queue_delay_ms", value),
            )
        except BaseException as exc:
            if isinstance(exc, GatewayError) and exc.request_id is None:
                exc.request_id = request_id
            store.abort(identity_subject, request_id, fingerprint, exc)
            raise
        store.succeed(identity_subject, request_id, fingerprint, response)
        return response

    @application.websocket("/v1/transcription")
    async def transcription(websocket: WebSocket):
        observed = application.state.observability
        started = observed.clock()
        correlation_id = observed.correlation_id(
            websocket.headers.get("x-dimon-correlation-id")
        )
        setattr(websocket, "_dimon_correlation_id", correlation_id)
        setattr(websocket, "_dimon_outcome_code", "INTERNAL_ERROR")
        try:
            await application.state.transcription_gateway.handle(websocket)
        finally:
            observed.record_websocket(
                correlation_id=correlation_id,
                identity=getattr(websocket, "_dimon_identity", None),
                outcome_code=getattr(
                    websocket, "_dimon_outcome_code", "INTERNAL_ERROR"
                ),
                latency_ms=(observed.clock() - started) * 1000,
            )

    @application.post("/v1/speech")
    async def create_speech(request: Request):
        identity_subject = await require_service_key(request)
        try:
            speech_request = SpeechRequest.model_validate(await _read_json(request))
        except ValidationError as exc:
            raise GatewayError("INVALID_REQUEST", "speech request is invalid", 400) from exc
        request_id = str(speech_request.request_id)
        retry_after = application.state.speech_limiter.check(identity_subject)
        if retry_after is not None:
            raise GatewayError(
                "RATE_LIMITED",
                "speech rate limit exceeded",
                429,
                request_id,
            )
        if speech_request.status == "REAL_STOCK_ALLOCATION_REQUIRED":
            summary = build_stock_allocation_summary(speech_request.instrument_label)
        elif speech_request.status == "REAL_STOCK_BRIEF":
            summary = speech_request.speech_text
        elif speech_request.status == "PREPARED_ORDER_REVIEW":
            summary = build_order_review_summary()
        elif speech_request.status == "PROMPT_INJECTION_BLOCKED":
            summary = PROMPT_INJECTION_BLOCKED_SPEECH
        elif speech_request.status == "CONVERSATION_TURN_APPLIED":
            summary = speech_request.speech_text
        else:
            summary = build_result_summary(
                status=speech_request.status,
                action_kind=speech_request.action_kind,
                amount=speech_request.amount,
                currency=speech_request.currency,
                recipient_display=speech_request.recipient_display,
                reference=speech_request.reference,
            )
        if summary is None:
            raise GatewayError(
                "INVALID_REQUEST",
                "speech request is invalid",
                400,
                request_id,
            )

        async def invoke():
            try:
                result = await application.state.speech_synthesizer(summary)
                return validate_synthesis(result)
            except GatewayError:
                raise
            except Exception as exc:
                raise GatewayError(
                    "UPSTREAM_UNAVAILABLE",
                    "speech upstream did not return a valid WAV",
                    503,
                    request_id,
                ) from exc

        try:
            result = await application.state.speech_executor.run(
                invoke,
                on_acquired=lambda value: setattr(
                    request.state, "queue_delay_ms", value
                ),
            )
        except GatewayError as exc:
            if exc.request_id is None:
                exc.request_id = request_id
            raise
        return Response(
            content=result.data,
            media_type="audio/wav",
            headers={
                "X-Dimon-Request-Id": request_id,
                "X-Dimon-Model-Revision": result.revision,
                "X-Dimon-Latency-Ms": str(result.latency_ms),
            },
        )

    return application


def application_factory() -> FastAPI:
    return create_gateway_app(GatewayConfig.from_env(os.environ))

"""FastAPI composition root for the bounded inference gateway."""

from __future__ import annotations

import hashlib
import json
import math
import os

from fastapi import FastAPI, Request
from pydantic import ValidationError

from apps.dimon_live.model_adapter import validate_intent

from .auth import AccessTokenVerifier, require_access_identity
from .config import GatewayConfig
from .contracts import IntentRequest
from .errors import GatewayError, gateway_error_response
from .idempotency import IdempotencyStore
from .limits import BoundedExecutor, SlidingWindowLimiter
from .upstreams import IntentUpstream, QwenIntentUpstream


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


def _bounded_text(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def create_gateway_app(
    config: GatewayConfig,
    *,
    verifier: AccessTokenVerifier | None = None,
    intent_upstream: IntentUpstream | None = None,
    intent_executor: BoundedExecutor | None = None,
    intent_limiter: SlidingWindowLimiter | None = None,
    idempotency_store: IdempotencyStore | None = None,
) -> FastAPI:
    application = FastAPI(
        title="Dimon Inference Gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.config = config
    application.state.access_verifier = verifier or AccessTokenVerifier(config)
    application.state.intent_upstream = intent_upstream or QwenIntentUpstream(config)
    application.state.intent_executor = intent_executor or BoundedExecutor(1, 4, 120)
    application.state.intent_limiter = intent_limiter or SlidingWindowLimiter(12, 60)
    application.state.idempotency_store = idempotency_store or IdempotencyStore()
    application.add_exception_handler(GatewayError, gateway_error_response)

    @application.post("/v1/intent")
    async def create_intent(request: Request):
        identity = await require_access_identity(request)
        try:
            intent_request = IntentRequest.model_validate(await _read_json(request))
        except ValidationError as exc:
            raise GatewayError("INVALID_REQUEST", "intent request is invalid", 400) from exc
        request_id = str(intent_request.request_id)
        fingerprint = _fingerprint(intent_request)
        store = application.state.idempotency_store
        decision = store.begin(identity.subject, request_id, fingerprint)
        if decision.state == "conflict":
            raise GatewayError(
                "IDEMPOTENCY_CONFLICT",
                "request ID was already used with different input",
                409,
                request_id,
            )
        if decision.state == "replay":
            return await decision.resolve()

        retry_after = application.state.intent_limiter.check(identity.subject)
        if retry_after is not None:
            error = GatewayError(
                "RATE_LIMITED",
                "intent rate limit exceeded",
                429,
                request_id,
            )
            store.abort(identity.subject, request_id, fingerprint, error)
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
            response = await application.state.intent_executor.run(invoke)
        except BaseException as exc:
            if isinstance(exc, GatewayError) and exc.request_id is None:
                exc.request_id = request_id
            store.abort(identity.subject, request_id, fingerprint, exc)
            raise
        store.succeed(identity.subject, request_id, fingerprint, response)
        return response

    return application


def application_factory() -> FastAPI:
    return create_gateway_app(GatewayConfig.from_env(os.environ))

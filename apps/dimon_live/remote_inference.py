"""Authenticated, bounded clients for the project inference gateway."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import math
import re
import wave
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import UUID, uuid4

import httpx

from .model_adapter import (
    ExtractedIntent,
    FollowUpIntent,
    InferenceFailure,
    validate_bill_payment_intent,
    validate_follow_up_intent,
    validate_intent,
    validate_stock_intent,
)
from .speech import build_order_review_summary, result_summary, stock_brief_voice_summary
from .voice import float32le_to_pcm16


STABLE_GATEWAY_ERROR_CODES = frozenset(
    {
        "UNAUTHORIZED",
        "INVALID_REQUEST",
        "INVALID_INFERENCE_REQUEST",
        "IDEMPOTENCY_CONFLICT",
        "RATE_LIMITED",
        "CAPACITY_EXCEEDED",
        "UPSTREAM_UNAVAILABLE",
        "UPSTREAM_TIMEOUT",
        "VOICE_BUSY",
        "AUDIO_LIMIT_EXCEEDED",
        "UTTERANCE_LIMIT_EXCEEDED",
        "INVALID_MODEL_OUTPUT",
    }
)
_HEADER_VALUE = re.compile(r"^[\x21-\x7e]{1,256}$")
_SERVICE_KEY_VALUE = re.compile(r"^[\x21-\x7e]{32,256}$")


@dataclass(frozen=True)
class RemoteGatewayConfig:
    base_url: str
    service_key: str = field(repr=False)
    timeout: float = 30.0
    max_response_bytes: int = 1_048_576
    max_wav_bytes: int = 8 * 1_048_576

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "RemoteGatewayConfig":
        base_url = env.get("DIMON_INFERENCE_GATEWAY_URL", "").strip().rstrip("/")
        service_key = env.get("DIMON_INFERENCE_SERVICE_KEY", "").strip()
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("DIMON_INFERENCE_GATEWAY_URL must be an HTTPS origin")
        if not _SERVICE_KEY_VALUE.fullmatch(service_key):
            raise ValueError(
                "DIMON_INFERENCE_SERVICE_KEY is required and must be 32-256 printable ASCII characters"
            )
        try:
            timeout = float(env.get("DIMON_INFERENCE_TIMEOUT_SECONDS", "30"))
            max_response = int(env.get("DIMON_INFERENCE_MAX_RESPONSE_BYTES", "1048576"))
            max_wav = int(env.get("DIMON_MAX_WAV_BYTES", str(8 * 1_048_576)))
        except (TypeError, ValueError) as exc:
            raise ValueError("remote inference limits are invalid") from exc
        if timeout <= 0 or max_response <= 0 or max_wav <= 0:
            raise ValueError("remote inference limits must be positive")
        return cls(base_url, service_key, timeout, max_response, max_wav)

    @property
    def websocket_url(self) -> str:
        parsed = urlparse(self.base_url)
        return urlunparse(("wss", parsed.netloc, f"{parsed.path}/v1/transcription", "", "", ""))


class RemoteGatewayClient:
    """Own the reusable HTTP pools and WebSocket connector for remote providers."""

    def __init__(
        self,
        config: RemoteGatewayConfig,
        *,
        sync_client: httpx.Client | None = None,
        async_client: httpx.AsyncClient | None = None,
        websocket_connect: Callable[..., Any] | None = None,
    ):
        self.config = config
        limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
        self.sync_client = sync_client or httpx.Client(
            timeout=config.timeout,
            trust_env=False,
            limits=limits,
        )
        self.async_client = async_client or httpx.AsyncClient(
            timeout=config.timeout,
            trust_env=False,
            limits=limits,
        )
        self._websocket_connect = websocket_connect

    @property
    def service_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.service_key}"}

    async def preparation(self, *, start: bool = False) -> dict[str, Any]:
        method = "POST" if start else "GET"
        try:
            response = await self.async_client.request(
                method,
                f"{self.config.base_url}/v1/prepare",
                json={} if start else None,
                headers=self.service_headers,
                timeout=130,
            )
        except httpx.TimeoutException as exc:
            raise InferenceFailure("UPSTREAM_TIMEOUT", "preparation timed out") from exc
        except httpx.HTTPError as exc:
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "preparation is unavailable") from exc
        self._raise_gateway_error(response)
        body = response.json()
        if not isinstance(body, dict) or body.get("state") not in {"IDLE", "PREPARING", "READY", "DEGRADED", "FAILED"}:
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "preparation status is invalid")
        return body

    def post_intent(self, payload: Mapping[str, object]) -> dict[str, Any]:
        headers = self._request_headers(payload)
        try:
            with self.sync_client.stream(
                "POST",
                f"{self.config.base_url}/v1/intent",
                json=payload,
                headers=headers,
                timeout=self.config.timeout,
            ) as streamed:
                response = self._read_sync(streamed, self.config.max_response_bytes)
        except httpx.TimeoutException as exc:
            raise InferenceFailure("UPSTREAM_TIMEOUT", "inference gateway timed out") from exc
        except httpx.HTTPError as exc:
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "inference gateway is unavailable") from exc
        self._raise_gateway_error(response)
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway response is invalid") from exc
        if not isinstance(body, dict):
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway response is invalid")
        return body

    def post_qwen_task(
        self,
        task: str,
        text: str,
        *,
        context: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, object] = {
            "request_id": str(uuid4()),
            "task": task,
            "text": text,
        }
        if context is not None:
            payload["context"] = dict(context)
        try:
            with self.sync_client.stream(
                "POST",
                f"{self.config.base_url}/v1/qwen",
                json=payload,
                headers=self._request_headers(payload),
                timeout=120,
            ) as streamed:
                response = self._read_sync(streamed, self.config.max_response_bytes)
        except httpx.TimeoutException as exc:
            raise InferenceFailure("UPSTREAM_TIMEOUT", "inference gateway timed out") from exc
        except httpx.HTTPError as exc:
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "inference gateway is unavailable") from exc
        self._raise_gateway_error(response)
        try:
            body = response.json()
            expected = {"request_id", "task", "raw_output", "model", "revision", "latency_ms"}
            if (
                not isinstance(body, dict)
                or set(body) != expected
                or body["request_id"] != payload["request_id"]
                or body["task"] != task
                or not isinstance(body["raw_output"], str)
                or not isinstance(body["model"], str)
                or not body["model"]
                or not isinstance(body["revision"], str)
                or not _HEADER_VALUE.fullmatch(body["revision"])
            ):
                raise ValueError("Qwen task response is invalid")
            latency_ms = float(body["latency_ms"])
            if not math.isfinite(latency_ms) or latency_ms < 0:
                raise ValueError("Qwen task latency is invalid")
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway response is invalid") from exc
        return body

    async def post_speech(self, payload: Mapping[str, object]) -> httpx.Response:
        headers = self._request_headers(payload)
        try:
            async with self.async_client.stream(
                "POST",
                f"{self.config.base_url}/v1/speech",
                json=payload,
                headers=headers,
                timeout=self.config.timeout,
            ) as streamed:
                response = await self._read_async(streamed, self.config.max_wav_bytes)
        except httpx.TimeoutException as exc:
            raise InferenceFailure("UPSTREAM_TIMEOUT", "inference gateway timed out") from exc
        except httpx.HTTPError as exc:
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "inference gateway is unavailable") from exc
        self._raise_gateway_error(response)
        return response

    def websocket_connection(
        self,
        *,
        trace_id: str | None = None,
        attempt_id: str | None = None,
    ):
        connector = self._websocket_connect
        if connector is None:
            from websockets.asyncio.client import connect

            connector = connect
        valid_trace_id = self._uuid4(trace_id)
        valid_attempt_id = self._uuid4(attempt_id)
        headers = {
            **self.service_headers,
            "X-Dimon-Correlation-Id": valid_attempt_id or str(uuid4()),
        }
        if valid_trace_id is not None:
            headers["X-Dimon-Trace-Id"] = valid_trace_id
        if valid_attempt_id is not None:
            headers["X-Dimon-Attempt-Id"] = valid_attempt_id
        return connector(
            self.config.websocket_url,
            additional_headers=headers,
            open_timeout=self.config.timeout,
            close_timeout=0.5,
            ping_interval=None,
            max_size=self.config.max_response_bytes,
            max_queue=8,
            proxy=None,
        )

    @staticmethod
    def _uuid4(value: object) -> str | None:
        if not isinstance(value, str) or len(value) != 36:
            return None
        try:
            parsed = UUID(value)
        except ValueError:
            return None
        return value if parsed.version == 4 and str(parsed) == value else None

    def _request_headers(self, payload: Mapping[str, object]) -> dict[str, str]:
        proposed = payload.get("request_id")
        try:
            correlation_id = str(UUID(str(proposed)))
        except (ValueError, TypeError, AttributeError):
            correlation_id = str(uuid4())
        return {
            **self.service_headers,
            "X-Dimon-Correlation-Id": correlation_id,
        }

    @staticmethod
    def _bounded_response(response: httpx.Response, data: bytes) -> httpx.Response:
        # data was read with iter_bytes(), so it is already decompressed. Keeping
        # Content-Encoding (Cloudflare gzips gateway replies) would make httpx
        # decompress the plain JSON again and fail.
        headers = httpx.Headers(response.headers)
        for name in ("content-encoding", "content-length", "transfer-encoding"):
            headers.pop(name, None)
        return httpx.Response(
            response.status_code,
            headers=headers,
            content=data,
            request=response.request,
        )

    @classmethod
    def _read_sync(cls, response: httpx.Response, maximum: int) -> httpx.Response:
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > maximum:
                raise InferenceFailure(
                    "INVALID_MODEL_OUTPUT", "gateway response exceeds its limit"
                )
        return cls._bounded_response(response, bytes(data))

    @classmethod
    async def _read_async(cls, response: httpx.Response, maximum: int) -> httpx.Response:
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > maximum:
                raise InferenceFailure(
                    "INVALID_MODEL_OUTPUT", "gateway response exceeds its limit"
                )
        return cls._bounded_response(response, bytes(data))

    @staticmethod
    def _raise_gateway_error(response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        code = None
        try:
            body = response.json()
            proposed = body.get("error", {}).get("code") if isinstance(body, dict) else None
            if proposed in STABLE_GATEWAY_ERROR_CODES:
                code = proposed
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            pass
        if code is None:
            code = {
                401: "UNAUTHORIZED",
                403: "UNAUTHORIZED",
                408: "UPSTREAM_TIMEOUT",
                413: "AUDIO_LIMIT_EXCEEDED",
                429: "RATE_LIMITED",
                504: "UPSTREAM_TIMEOUT",
            }.get(response.status_code)
        if code is None:
            code = (
                "INVALID_INFERENCE_REQUEST"
                if 400 <= response.status_code < 500
                else "UPSTREAM_UNAVAILABLE"
                if response.status_code >= 500
                else "INVALID_MODEL_OUTPUT"
            )
        raise InferenceFailure(code, f"inference gateway rejected the request ({code})")


class RemoteExtractor:
    def __init__(self, client: RemoteGatewayClient):
        self.client = client

    def extract(self, text: str) -> ExtractedIntent:
        request_id = str(uuid4())
        body = self.client.post_intent(
            {"request_id": request_id, "text": text, "contract_version": 4}
        )
        expected = {"request_id", "intent", "raw_output", "model", "revision", "latency_ms"}
        try:
            if set(body) != expected or body["request_id"] != request_id:
                raise ValueError("unexpected response fields")
            raw_output = body["raw_output"]
            if not isinstance(raw_output, str):
                raise TypeError("raw output must be text")
            intent = validate_intent(raw_output)
            if body["intent"] != intent:
                raise ValueError("intent and raw output differ")
            if not all(isinstance(body[key], str) and body[key] for key in ("model", "revision")):
                raise TypeError("model provenance is invalid")
            latency_ms = float(body["latency_ms"])
            if not math.isfinite(latency_ms) or latency_ms < 0:
                raise ValueError("latency is invalid")
        except InferenceFailure:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway response is invalid") from exc
        return ExtractedIntent(intent, raw_output, latency_ms)


class RemoteStockIntentExtractor:
    def __init__(self, client: RemoteGatewayClient):
        self.client = client

    def extract(self, text: str) -> ExtractedIntent:
        body = self.client.post_qwen_task("stock", text)
        raw_output = body["raw_output"]
        intent = validate_stock_intent(raw_output, text=text)
        return ExtractedIntent(intent, raw_output, float(body["latency_ms"]))


class RemoteBillPaymentExtractor:
    def __init__(self, client: RemoteGatewayClient):
        self.client = client

    def extract(self, text: str) -> ExtractedIntent:
        body = self.client.post_qwen_task("bill_payment", text)
        raw_output = body["raw_output"]
        intent = validate_bill_payment_intent(raw_output)
        return ExtractedIntent(intent, raw_output, float(body["latency_ms"]))


class RemoteMultiActionExtractor:
    def __init__(self, client: RemoteGatewayClient):
        self.client = client

    def extract(self, text: str) -> ExtractedIntent:
        from .multi_action import validate_multi_action_intent

        body = self.client.post_qwen_task("multi_action", text)
        raw_output = body["raw_output"]
        intent = validate_multi_action_intent(raw_output, text=text)
        return ExtractedIntent(intent, raw_output, float(body["latency_ms"]))


class RemoteInvestmentFollowUpInterpreter:
    def __init__(self, client: RemoteGatewayClient):
        self.client = client

    def interpret(
        self,
        text: str,
        *,
        phase: str,
        question: str,
        state: Mapping[str, Any],
    ) -> FollowUpIntent:
        context = {
            "phase": phase,
            "question": question,
            "symbol": state.get("symbol"),
            "price": state.get("price"),
            "quote_currency": state.get("quote_currency"),
        }
        body = self.client.post_qwen_task(
            "investment_follow_up", text, context=context
        )
        raw_output = body["raw_output"]
        intent = validate_follow_up_intent(raw_output, text=text, phase=phase)
        return FollowUpIntent(
            tuple(intent["actions"]), raw_output, float(body["latency_ms"]),
            intent["size_kind"], intent["quantity"],
            intent["quantity_evidence"], intent["allocation_evidence"],
        )


class RemoteSynthesizer:
    def __init__(self, client: RemoteGatewayClient):
        self.client = client

    async def __call__(self, _text: str) -> bytes:
        raise InferenceFailure(
            "INVALID_INFERENCE_REQUEST",
            "remote speech requires a committed result",
        )

    async def synthesize_result(self, result: Mapping[str, Any]) -> bytes:
        payload = self._payload(result)
        response = await self.client.post_speech(payload)
        request_id = payload["request_id"]
        try:
            if response.headers.get("content-type", "").split(";", 1)[0] != "audio/wav":
                raise ValueError("wrong media type")
            if response.headers.get("x-dimon-request-id") != request_id:
                raise ValueError("request ID mismatch")
            revision = response.headers.get("x-dimon-model-revision")
            latency = response.headers.get("x-dimon-latency-ms")
            if not revision or not _HEADER_VALUE.fullmatch(revision):
                raise ValueError("revision is invalid")
            latency_ms = float(latency or "")
            if not math.isfinite(latency_ms) or latency_ms < 0:
                raise ValueError("latency is invalid")
            with wave.open(io.BytesIO(response.content), "rb") as wav:
                if (
                    wav.getnchannels() != 1
                    or wav.getsampwidth() != 2
                    or not 8_000 <= wav.getframerate() <= 48_000
                ):
                    raise ValueError("WAV format is invalid")
        except (ValueError, TypeError, wave.Error, EOFError) as exc:
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway speech response is invalid") from exc
        return response.content

    @staticmethod
    def _payload(result: Mapping[str, Any]) -> dict[str, object]:
        status = result.get("outcome")
        if status == "BLOCKED" and result.get("reason_code") == "PROMPT_INJECTION_BLOCKED":
            return {
                "request_id": str(uuid4()),
                "status": "PROMPT_INJECTION_BLOCKED",
            }
        if status == "CLARIFICATION_REQUEST":
            speech_text = result_summary(dict(result))
            if not speech_text:
                raise InferenceFailure(
                    "INVALID_INFERENCE_REQUEST", "clarification speech is invalid"
                )
            return {
                "request_id": str(uuid4()),
                "status": "CONVERSATION_TURN_APPLIED",
                "speech_text": speech_text[:1600],
            }
        if status == "ANSWERED" and result.get("reason_code") == "CONVERSATION_TURN_APPLIED":
            speech_text = result_summary(dict(result))
            if not isinstance(speech_text, str) or not speech_text.strip():
                raise InferenceFailure(
                    "INVALID_INFERENCE_REQUEST", "conversation speech is invalid"
                )
            return {
                "request_id": str(uuid4()),
                "status": "CONVERSATION_TURN_APPLIED",
                "speech_text": speech_text.strip()[:1600],
            }
        if (
            status == "ANSWERED"
            and result.get("reason_code") == "REAL_STOCK_ALLOCATION_REQUIRED"
        ):
            instrument_label = result.get("instrument_label")
            if not isinstance(instrument_label, str) or not instrument_label.strip():
                raise InferenceFailure(
                    "INVALID_INFERENCE_REQUEST", "stock clarification is invalid"
                )
            return {
                "request_id": str(uuid4()),
                "status": "REAL_STOCK_ALLOCATION_REQUIRED",
                "instrument_label": instrument_label,
            }
        if status == "ANSWERED" and result.get("reason_code") == "REAL_STOCK_BRIEF":
            speech_text = stock_brief_voice_summary(dict(result))
            if not speech_text:
                raise InferenceFailure(
                    "INVALID_INFERENCE_REQUEST", "stock brief speech is invalid"
                )
            return {
                "request_id": str(uuid4()),
                "status": "REAL_STOCK_BRIEF",
                "speech_text": speech_text,
            }
        if status == "ORDER_CONFIRMATION_REQUEST" and (
            isinstance(result.get("paper_order"), Mapping)
            or isinstance(result.get("order"), Mapping)
        ):
            return {
                "request_id": str(uuid4()),
                "status": "PREPARED_ORDER_REVIEW",
            }
        amount = result.get("amount_minor")
        currency = result.get("currency", "SGD")
        reference: str | None = None
        validated = result.get("validated_intent")
        if isinstance(validated, Mapping):
            candidate = validated.get("payment_reference")
            if candidate is not None:
                reference = candidate if isinstance(candidate, str) else None
                if reference is None:
                    raise InferenceFailure("INVALID_INFERENCE_REQUEST", "committed result is invalid")

        if status == "EXECUTED_SYNTHETIC_TRANSFER":
            action = result.get("resolved_action")
            if not isinstance(action, Mapping):
                raise InferenceFailure("INVALID_INFERENCE_REQUEST", "committed result is invalid")
            proposed_kind = validated.get("kind") if isinstance(validated, Mapping) else None
            action_kind = proposed_kind or action.get("kind", "BENEFICIARY_TRANSFER")
            recipient = action.get("recipient_display_name")
        elif status == "EXECUTED_SYNTHETIC_BATCH":
            actions = result.get("resolved_actions")
            if not isinstance(actions, list) or not actions:
                raise InferenceFailure("INVALID_INFERENCE_REQUEST", "committed result is invalid")
            action_kind = "BENEFICIARY_TRANSFER_BATCH"
            recipient = f"{len(actions)} recipients"
        else:
            raise InferenceFailure("INVALID_INFERENCE_REQUEST", "speech requires a committed result")
        if (
            action_kind not in {
                "BENEFICIARY_TRANSFER",
                "PAYNOW_MOBILE_TRANSFER",
                "BENEFICIARY_TRANSFER_BATCH",
            }
            or not isinstance(recipient, str)
            or not recipient.strip()
            or not isinstance(amount, int)
            or isinstance(amount, bool)
            or amount <= 0
            or currency != "SGD"
        ):
            raise InferenceFailure("INVALID_INFERENCE_REQUEST", "committed result is invalid")
        return {
            "request_id": str(uuid4()),
            "status": status,
            "action_kind": action_kind,
            "amount": amount,
            "currency": currency,
            "recipient_display": recipient,
            "reference": reference,
        }


class RemoteRecognizer:
    def __init__(self, client: RemoteGatewayClient):
        self.client = client

    async def open_stream(
        self,
        on_partial: Callable[[str], Awaitable[None]],
        *,
        trace_id: str | None = None,
        attempt_id: str | None = None,
    ) -> "RemoteRecognizerStream":
        stream = RemoteRecognizerStream(
            self.client,
            on_partial,
            trace_id=trace_id,
            attempt_id=attempt_id,
        )
        await stream.open()
        return stream

    async def transcribe(
        self,
        audio: bytes,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> str:
        stream = await self.open_stream(on_partial)
        try:
            await stream.push_audio(audio)
            return await stream.finish()
        except BaseException:
            await stream.close()
            raise


class RemoteRecognizerStream:
    def __init__(
        self,
        client: RemoteGatewayClient,
        on_partial: Callable[[str], Awaitable[None]],
        *,
        trace_id: str | None = None,
        attempt_id: str | None = None,
    ):
        self.client = client
        self.on_partial = on_partial
        self.trace_id = trace_id
        self.attempt_id = attempt_id
        self.nemotron_state: str | None = None
        self.nemotron_failure_phase: str | None = None
        self.recognition_path: str | None = None
        self.transcript_disagreement = False
        self.faster_whisper_avg_logprob: float | None = None
        self.faster_whisper_no_speech_prob: float | None = None
        self.faster_whisper_confidence: str | None = None
        self.connection: Any = None
        self.websocket: Any = None
        self.receiver: asyncio.Task[None] | None = None
        self.result: asyncio.Future[str] | None = None
        self.closed = False

    async def open(self) -> None:
        try:
            self.connection = self.client.websocket_connection(
                trace_id=self.trace_id,
                attempt_id=self.attempt_id,
            )
            self.websocket = await self.connection.__aenter__()
            ready = await asyncio.wait_for(self._receive_event(), self.client.config.timeout)
            if ready.get("type") == "error" and ready.get("protocol_version") == 1:
                code = ready.get("code")
                stable = code if code in STABLE_GATEWAY_ERROR_CODES else "INVALID_MODEL_OUTPUT"
                raise InferenceFailure(stable, f"inference gateway voice error ({stable})")
            required = {
                "type": "session.ready",
                "protocol_version": 1,
                "sample_rate": 16_000,
                "encoding": "pcm_s16le",
            }
            if any(ready.get(key) != value for key, value in required.items()) or frozenset(ready) not in {
                frozenset(required),
                frozenset((*required, "nemotron_state", "nemotron_failure_phase")),
            }:
                raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
            self._capture_nemotron_status(ready)
            self.result = asyncio.get_running_loop().create_future()
            self.receiver = asyncio.create_task(self._receive())
        except InferenceFailure:
            await self.close()
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            await self.close()
            raise InferenceFailure("UPSTREAM_TIMEOUT", "inference gateway timed out") from exc
        except Exception as exc:
            await self.close()
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "inference gateway is unavailable") from exc

    async def push_audio(self, audio: bytes) -> None:
        if self.closed or self.websocket is None:
            raise InferenceFailure("STREAM_CLOSED", "voice stream is closed")
        try:
            pcm = float32le_to_pcm16(audio)
            if pcm:
                await self.websocket.send(pcm)
        except InferenceFailure:
            raise
        except Exception as exc:
            reason_code = getattr(exc, "reason_code", None)
            if isinstance(reason_code, str):
                raise InferenceFailure(reason_code, "audio frame is invalid") from exc
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway voice stream failed") from exc

    async def finish(self) -> str:
        if self.closed or self.websocket is None or self.result is None:
            raise InferenceFailure("STREAM_CLOSED", "voice stream is closed")
        try:
            await self.websocket.send(json.dumps({"type": "input.commit"}, separators=(",", ":")))
            return await asyncio.wait_for(asyncio.shield(self.result), self.client.config.timeout)
        except InferenceFailure:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise InferenceFailure("UPSTREAM_TIMEOUT", "inference gateway timed out") from exc
        except Exception as exc:
            if isinstance(exc, InferenceFailure):
                raise
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway voice stream failed") from exc
        finally:
            await self.close()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        current = asyncio.current_task()
        if self.receiver is not None and self.receiver is not current and not self.receiver.done():
            self.receiver.cancel()
            await asyncio.gather(self.receiver, return_exceptions=True)
        self.receiver = None
        if self.result is not None and not self.result.done():
            self.result.cancel()
        if self.connection is not None:
            with contextlib.suppress(Exception):
                await self.connection.__aexit__(None, None, None)
        elif self.websocket is not None:
            with contextlib.suppress(Exception):
                await self.websocket.close()
        self.connection = None
        self.websocket = None

    async def _receive_event(self) -> dict[str, Any]:
        if self.websocket is None:
            raise InferenceFailure("STREAM_CLOSED", "voice stream is closed")
        value = await self.websocket.recv()
        if not isinstance(value, str) or len(value.encode("utf-8")) > self.client.config.max_response_bytes:
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
        try:
            event = json.loads(value)
        except json.JSONDecodeError as exc:
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid") from exc
        if not isinstance(event, dict):
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
        return event

    async def _receive(self) -> None:
        assert self.result is not None
        try:
            while True:
                event = await self._receive_event()
                event_type = event.get("type")
                if event.get("protocol_version") != 1:
                    raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
                if event_type == "transcript.partial":
                    if set(event) != {"type", "protocol_version", "text"}:
                        raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
                    text = event.get("text")
                    if not isinstance(text, str) or not text:
                        raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
                    await self.on_partial(text)
                elif event_type == "transcript.final":
                    legacy_fields = frozenset({
                        "type", "protocol_version", "text",
                        "nemotron_state", "nemotron_failure_phase",
                    })
                    measured_fields = frozenset({
                        *legacy_fields,
                        "recognition_path", "transcript_disagreement",
                        "faster_whisper_avg_logprob",
                        "faster_whisper_no_speech_prob",
                        "faster_whisper_confidence",
                    })
                    if frozenset(event) not in {
                        frozenset({"type", "protocol_version", "text"}),
                        legacy_fields,
                        measured_fields,
                    }:
                        raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
                    self._capture_nemotron_status(event)
                    self._capture_recognition_metadata(event)
                    text = event.get("text")
                    if not isinstance(text, str) or not text.strip():
                        raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
                    if not self.result.done():
                        self.result.set_result(text.strip())
                    return
                elif event_type == "error":
                    if set(event) != {"type", "protocol_version", "code"}:
                        raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
                    code = event.get("code")
                    stable = code if code in STABLE_GATEWAY_ERROR_CODES else "INVALID_MODEL_OUTPUT"
                    raise InferenceFailure(stable, f"inference gateway voice error ({stable})")
                else:
                    raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc if isinstance(exc, InferenceFailure) else InferenceFailure(
                "UPSTREAM_UNAVAILABLE", "gateway voice stream failed"
            )
            if not self.result.done():
                self.result.set_exception(failure)

    def _capture_nemotron_status(self, event: dict[str, Any]) -> None:
        if "nemotron_state" not in event and "nemotron_failure_phase" not in event:
            return
        state = event.get("nemotron_state")
        phase = event.get("nemotron_failure_phase")
        allowed_phases = {
            None,
            "nemotron_connect",
            "nemotron_ready_timeout",
            "nemotron_stream_closed",
            "nemotron_protocol",
            "nemotron_empty_final",
            "nemotron_upstream_error",
        }
        if state not in {"READY", "FAILED"} or phase not in allowed_phases:
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
        if state == "READY" and phase is not None:
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
        if state == "FAILED" and phase is None:
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
        self.nemotron_state = state
        self.nemotron_failure_phase = phase

    def _capture_recognition_metadata(self, event: dict[str, Any]) -> None:
        if "recognition_path" not in event:
            return
        path = event.get("recognition_path")
        disagreement = event.get("transcript_disagreement")
        avg_logprob = event.get("faster_whisper_avg_logprob")
        no_speech_prob = event.get("faster_whisper_no_speech_prob")
        confidence = event.get("faster_whisper_confidence")
        if path not in {None, "nemotron_final", "faster_whisper_final"}:
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
        if not isinstance(disagreement, bool):
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
        if avg_logprob is not None and (
            isinstance(avg_logprob, bool)
            or not isinstance(avg_logprob, (int, float))
            or not math.isfinite(float(avg_logprob))
            or not -100 <= float(avg_logprob) <= 0
        ):
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
        if no_speech_prob is not None and (
            isinstance(no_speech_prob, bool)
            or not isinstance(no_speech_prob, (int, float))
            or not math.isfinite(float(no_speech_prob))
            or not 0 <= float(no_speech_prob) <= 1
        ):
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
        if confidence not in {None, "normal", "low"}:
            raise InferenceFailure("INVALID_MODEL_OUTPUT", "gateway voice protocol is invalid")
        self.recognition_path = path
        self.transcript_disagreement = disagreement
        self.faster_whisper_avg_logprob = (
            float(avg_logprob) if avg_logprob is not None else None
        )
        self.faster_whisper_no_speech_prob = (
            float(no_speech_prob) if no_speech_prob is not None else None
        )
        self.faster_whisper_confidence = confidence

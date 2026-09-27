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
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import httpx

from .model_adapter import ExtractedIntent, InferenceFailure, validate_intent
from .voice import float32le_to_pcm16


STABLE_GATEWAY_ERROR_CODES = frozenset(
    {
        "UNAUTHORIZED",
        "UNTRUSTED_PROXY",
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
_HEADER_VALUE = re.compile(r"^[\x21-\x7e]+$")


@dataclass(frozen=True)
class RemoteGatewayConfig:
    base_url: str
    client_id: str
    client_secret: str
    timeout: float = 30.0
    max_response_bytes: int = 1_048_576
    max_wav_bytes: int = 8 * 1_048_576

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "RemoteGatewayConfig":
        base_url = env.get("DIMON_INFERENCE_GATEWAY_URL", "").strip().rstrip("/")
        client_id = env.get("DIMON_CF_ACCESS_CLIENT_ID", "").strip()
        client_secret = env.get("DIMON_CF_ACCESS_CLIENT_SECRET", "").strip()
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
        if not client_id or not client_secret:
            raise ValueError("Cloudflare Access service-token credentials are required")
        if not _HEADER_VALUE.fullmatch(client_id) or not _HEADER_VALUE.fullmatch(client_secret):
            raise ValueError("Cloudflare Access service-token credentials are invalid")
        try:
            timeout = float(env.get("DIMON_INFERENCE_TIMEOUT_SECONDS", "30"))
            max_response = int(env.get("DIMON_INFERENCE_MAX_RESPONSE_BYTES", "1048576"))
            max_wav = int(env.get("DIMON_MAX_WAV_BYTES", str(8 * 1_048_576)))
        except (TypeError, ValueError) as exc:
            raise ValueError("remote inference limits are invalid") from exc
        if timeout <= 0 or max_response <= 0 or max_wav <= 0:
            raise ValueError("remote inference limits must be positive")
        return cls(base_url, client_id, client_secret, timeout, max_response, max_wav)

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
    def access_headers(self) -> dict[str, str]:
        return {
            "CF-Access-Client-Id": self.config.client_id,
            "CF-Access-Client-Secret": self.config.client_secret,
        }

    def post_intent(self, payload: Mapping[str, object]) -> dict[str, Any]:
        try:
            with self.sync_client.stream(
                "POST",
                f"{self.config.base_url}/v1/intent",
                json=payload,
                headers=self.access_headers,
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
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway response is invalid") from exc
        if not isinstance(body, dict):
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway response is invalid")
        return body

    async def post_speech(self, payload: Mapping[str, object]) -> httpx.Response:
        try:
            async with self.async_client.stream(
                "POST",
                f"{self.config.base_url}/v1/speech",
                json=payload,
                headers=self.access_headers,
                timeout=self.config.timeout,
            ) as streamed:
                response = await self._read_async(streamed, self.config.max_wav_bytes)
        except httpx.TimeoutException as exc:
            raise InferenceFailure("UPSTREAM_TIMEOUT", "inference gateway timed out") from exc
        except httpx.HTTPError as exc:
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "inference gateway is unavailable") from exc
        self._raise_gateway_error(response)
        return response

    def websocket_connection(self):
        connector = self._websocket_connect
        if connector is None:
            from websockets.asyncio.client import connect

            connector = connect
        return connector(
            self.config.websocket_url,
            additional_headers=self.access_headers,
            open_timeout=self.config.timeout,
            close_timeout=0.5,
            ping_interval=None,
            max_size=self.config.max_response_bytes,
            max_queue=8,
            proxy=None,
        )

    @staticmethod
    def _bounded_response(response: httpx.Response, data: bytes) -> httpx.Response:
        return httpx.Response(
            response.status_code,
            headers=response.headers,
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
                    "UPSTREAM_UNAVAILABLE", "gateway response exceeds its limit"
                )
        return cls._bounded_response(response, bytes(data))

    @classmethod
    async def _read_async(cls, response: httpx.Response, maximum: int) -> httpx.Response:
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > maximum:
                raise InferenceFailure(
                    "UPSTREAM_UNAVAILABLE", "gateway response exceeds its limit"
                )
        return cls._bounded_response(response, bytes(data))

    @staticmethod
    def _raise_gateway_error(response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        code = "UPSTREAM_UNAVAILABLE"
        try:
            body = response.json()
            proposed = body.get("error", {}).get("code") if isinstance(body, dict) else None
            if proposed in STABLE_GATEWAY_ERROR_CODES:
                code = proposed
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            pass
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
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway response is invalid") from exc
        return ExtractedIntent(intent, raw_output, latency_ms)


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
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16_000:
                    raise ValueError("WAV format is invalid")
        except (ValueError, TypeError, wave.Error, EOFError) as exc:
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway speech response is invalid") from exc
        return response.content

    @staticmethod
    def _payload(result: Mapping[str, Any]) -> dict[str, object]:
        status = result.get("outcome")
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
    ) -> "RemoteRecognizerStream":
        stream = RemoteRecognizerStream(self.client, on_partial)
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
    ):
        self.client = client
        self.on_partial = on_partial
        self.connection: Any = None
        self.websocket: Any = None
        self.receiver: asyncio.Task[None] | None = None
        self.result: asyncio.Future[str] | None = None
        self.closed = False

    async def open(self) -> None:
        try:
            self.connection = self.client.websocket_connection()
            self.websocket = await self.connection.__aenter__()
            ready = await asyncio.wait_for(self._receive_event(), self.client.config.timeout)
            if ready.get("type") == "error" and ready.get("protocol_version") == 1:
                code = ready.get("code")
                stable = code if code in STABLE_GATEWAY_ERROR_CODES else "UPSTREAM_UNAVAILABLE"
                raise InferenceFailure(stable, f"inference gateway voice error ({stable})")
            if ready != {
                "type": "session.ready",
                "protocol_version": 1,
                "sample_rate": 16_000,
                "encoding": "pcm_s16le",
            }:
                raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway voice protocol is invalid")
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
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway voice protocol is invalid")
        try:
            event = json.loads(value)
        except json.JSONDecodeError as exc:
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway voice protocol is invalid") from exc
        if not isinstance(event, dict):
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway voice protocol is invalid")
        return event

    async def _receive(self) -> None:
        assert self.result is not None
        try:
            while True:
                event = await self._receive_event()
                event_type = event.get("type")
                if event.get("protocol_version") != 1:
                    raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway voice protocol is invalid")
                if event_type == "transcript.partial":
                    if set(event) != {"type", "protocol_version", "text"}:
                        raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway voice protocol is invalid")
                    text = event.get("text")
                    if not isinstance(text, str) or not text:
                        raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway voice protocol is invalid")
                    await self.on_partial(text)
                elif event_type == "transcript.final":
                    if set(event) != {"type", "protocol_version", "text"}:
                        raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway voice protocol is invalid")
                    text = event.get("text")
                    if not isinstance(text, str) or not text.strip():
                        raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway voice protocol is invalid")
                    if not self.result.done():
                        self.result.set_result(text.strip())
                    return
                elif event_type == "error":
                    if set(event) != {"type", "protocol_version", "code"}:
                        raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway voice protocol is invalid")
                    code = event.get("code")
                    stable = code if code in STABLE_GATEWAY_ERROR_CODES else "UPSTREAM_UNAVAILABLE"
                    raise InferenceFailure(stable, f"inference gateway voice error ({stable})")
                else:
                    raise InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway voice protocol is invalid")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc if isinstance(exc, InferenceFailure) else InferenceFailure(
                "UPSTREAM_UNAVAILABLE", "gateway voice stream failed"
            )
            if not self.result.done():
                self.result.set_exception(failure)

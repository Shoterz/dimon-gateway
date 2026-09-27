"""Private loopback adapters used behind project inference contracts."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

import httpx

from apps.dimon_live.model_adapter import (
    ExtractedIntent,
    InvalidModelOutput,
    build_qwen_request,
    validate_intent,
)

from .config import GatewayConfig
from .errors import GatewayError


class IntentUpstream(Protocol):
    async def extract(self, text: str) -> ExtractedIntent: ...


AsyncIntentTransport = Callable[
    [str, Mapping[str, Any], float],
    Awaitable[dict[str, Any]],
]


async def post_json_async(
    url: str,
    payload: Mapping[str, Any],
    timeout: float,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise ValueError("upstream response must be an object")
    return data


class QwenIntentUpstream:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        transport: AsyncIntentTransport = post_json_async,
    ):
        self.api_base = config.qwen_api_base.rstrip("/")
        self.model = config.qwen_model
        self.timeout = 120.0
        self.transport = transport

    async def extract(self, text: str) -> ExtractedIntent:
        payload = build_qwen_request(text, self.model)
        started = time.perf_counter()
        try:
            response = await self.transport(
                f"{self.api_base}/chat/completions",
                payload,
                self.timeout,
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise GatewayError(
                "UPSTREAM_TIMEOUT",
                "intent upstream timed out",
                504,
            ) from exc
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise GatewayError(
                "UPSTREAM_UNAVAILABLE",
                "intent upstream is unavailable",
                503,
            ) from exc
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            choice = response["choices"][0]
            finish_reason = choice.get("finish_reason")
            raw_output = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise InvalidModelOutput("model response envelope is invalid") from exc
        if finish_reason != "stop":
            raise InvalidModelOutput(
                f"model finish_reason must be stop, got {finish_reason!r}"
            )
        intent = validate_intent(raw_output)
        return ExtractedIntent(intent, raw_output, latency_ms)

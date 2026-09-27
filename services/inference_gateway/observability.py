"""Content-free gateway health, correlation, logging, and in-memory metrics."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import UUID, uuid4


SAFE_CAPABILITIES = ("intent", "transcription", "speech")
SAFE_READINESS_CODES = frozenset({"READY", "UPSTREAM_UNAVAILABLE", "UPSTREAM_TIMEOUT"})
LATENCY_BUCKETS_MS = (10, 50, 100, 250, 500, 1_000, 5_000, 30_000, 120_000)


@dataclass(frozen=True)
class CapabilityReadiness:
    ready: bool
    reason_code: str

    def __post_init__(self) -> None:
        if self.reason_code not in SAFE_READINESS_CODES:
            raise ValueError("readiness reason code is not public")
        if self.ready != (self.reason_code == "READY"):
            raise ValueError("readiness state and reason code disagree")


class ReadinessProbe(Protocol):
    async def __call__(self) -> CapabilityReadiness: ...


class EndpointProbe:
    """Bounded TCP readiness check which never exposes its private endpoint."""

    def __init__(self, endpoint: str):
        parsed = urlparse(endpoint)
        if parsed.hostname is None:
            raise ValueError("readiness endpoint is invalid")
        default_port = 443 if parsed.scheme in {"https", "wss"} else 80
        self.host = parsed.hostname
        self.port = parsed.port or default_port

    async def __call__(self) -> CapabilityReadiness:
        try:
            _reader, writer = await asyncio.open_connection(self.host, self.port)
            writer.close()
            await writer.wait_closed()
            return CapabilityReadiness(True, "READY")
        except (OSError, ConnectionError):
            return CapabilityReadiness(False, "UPSTREAM_UNAVAILABLE")


def default_readiness_probes(config) -> dict[str, ReadinessProbe]:
    return {
        "intent": EndpointProbe(config.qwen_api_base),
        "transcription": EndpointProbe(config.nemotron_url),
        "speech": EndpointProbe(config.kokoro_url),
    }


class ReadinessService:
    def __init__(
        self,
        probes: Mapping[str, ReadinessProbe],
        revisions: Mapping[str, str],
        *,
        timeout: float = 1.0,
    ):
        if set(probes) != set(SAFE_CAPABILITIES) or set(revisions) != set(SAFE_CAPABILITIES):
            raise ValueError("readiness requires exactly the named capabilities")
        if timeout <= 0:
            raise ValueError("readiness timeout must be positive")
        self.probes = dict(probes)
        self.revisions = dict(revisions)
        self.timeout = float(timeout)

    async def check(self) -> tuple[bool, dict[str, dict[str, object]]]:
        async def one(name: str) -> tuple[str, CapabilityReadiness]:
            try:
                result = await asyncio.wait_for(self.probes[name](), self.timeout)
                if not isinstance(result, CapabilityReadiness):
                    raise TypeError("probe result is invalid")
                return name, result
            except (asyncio.TimeoutError, TimeoutError):
                return name, CapabilityReadiness(False, "UPSTREAM_TIMEOUT")
            except Exception:
                return name, CapabilityReadiness(False, "UPSTREAM_UNAVAILABLE")

        results = await asyncio.gather(*(one(name) for name in SAFE_CAPABILITIES))
        capabilities = {
            name: {
                "ready": result.ready,
                "reason_code": result.reason_code,
                "revision": self.revisions[name],
            }
            for name, result in results
        }
        return all(value["ready"] for value in capabilities.values()), capabilities


class EventLogger(Protocol):
    def info(self, message: str) -> None: ...


class GatewayObservability:
    def __init__(
        self,
        *,
        logger: EventLogger | None = None,
        clock: Callable[[], float] = time.perf_counter,
        identity_key: bytes | None = None,
    ):
        self.logger = logger or logging.getLogger("dimon.inference_gateway.events")
        self.clock = clock
        self.identity_key = identity_key or secrets.token_bytes(32)
        self._requests: dict[str, Counter[str]] = defaultdict(Counter)
        self._failures: Counter[str] = Counter()
        self._latencies: Counter[str] = Counter()

    @staticmethod
    def correlation_id(proposed: str | None) -> str:
        try:
            parsed = UUID(proposed or "")
            if parsed.version != 4:
                raise ValueError("correlation ID must be UUIDv4")
            return str(parsed)
        except (ValueError, AttributeError):
            return str(uuid4())

    def identity_hash(self, subject: str | None) -> str | None:
        if not subject:
            return None
        return hmac.new(
            self.identity_key,
            subject.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:16]

    def record_http(self, request, *, outcome_code: str, latency_ms: float) -> None:
        route = request.url.path if request.url.path in {
            "/v1/intent",
            "/v1/speech",
            "/health/live",
            "/health/ready",
        } else "OTHER"
        safe_outcome = outcome_code if _safe_code(outcome_code) else "INTERNAL_ERROR"
        identity = getattr(request.state, "access_identity", None)
        subject = getattr(identity, "subject", None)
        event = {
            "event": "gateway_request",
            "correlation_id": request.state.correlation_id,
            "identity_hash": self.identity_hash(subject),
            "method": request.method,
            "route": route,
            "outcome_code": safe_outcome,
            "queue_delay_ms": round(float(getattr(request.state, "queue_delay_ms", 0.0)), 2),
            "latency_ms": round(max(0.0, latency_ms), 2),
        }
        self.logger.info(json.dumps(event, sort_keys=True, separators=(",", ":")))
        self._requests[route][safe_outcome] += 1
        if safe_outcome != "OK":
            self._failures[safe_outcome] += 1
        for bucket in LATENCY_BUCKETS_MS:
            if latency_ms <= bucket:
                self._latencies[str(bucket)] += 1
        self._latencies["+Inf"] += 1

    def record_websocket(
        self,
        *,
        correlation_id: str,
        identity: Any,
        outcome_code: str,
        latency_ms: float,
    ) -> None:
        safe_outcome = outcome_code if _safe_code(outcome_code) else "INTERNAL_ERROR"
        event = {
            "event": "gateway_request",
            "correlation_id": correlation_id,
            "identity_hash": self.identity_hash(getattr(identity, "subject", None)),
            "method": "WEBSOCKET",
            "route": "/v1/transcription",
            "outcome_code": safe_outcome,
            "queue_delay_ms": 0.0,
            "latency_ms": round(max(0.0, latency_ms), 2),
        }
        self.logger.info(json.dumps(event, sort_keys=True, separators=(",", ":")))
        self._requests["/v1/transcription"][safe_outcome] += 1
        if safe_outcome != "OK":
            self._failures[safe_outcome] += 1
        for bucket in LATENCY_BUCKETS_MS:
            if latency_ms <= bucket:
                self._latencies[str(bucket)] += 1
        self._latencies["+Inf"] += 1

    def snapshot(self, app) -> dict[str, Any]:
        capacity: dict[str, dict[str, int]] = {}
        for name, state_name in (
            ("intent", "intent_executor"),
            ("speech", "speech_executor"),
        ):
            executor = getattr(app.state, state_name, None)
            if executor is not None:
                capacity[name] = {
                    "active": int(executor.active_count),
                    "queue_depth": int(executor.queue_depth),
                }
        transcription = getattr(app.state, "transcription_gateway", None)
        if transcription is not None:
            capacity["transcription"] = {
                "active": int(transcription.lease.active_count),
                "queue_depth": 0,
            }
        return {
            "requests": {
                route: dict(sorted(outcomes.items()))
                for route, outcomes in sorted(self._requests.items())
            },
            "failures": dict(sorted(self._failures.items())),
            "capacity": capacity,
            "latency_buckets_ms": dict(self._latencies),
        }


def _safe_code(value: str) -> bool:
    return (
        isinstance(value, str)
        and 2 <= len(value) <= 64
        and all(character == "_" or "A" <= character <= "Z" or character.isdigit() for character in value)
    )

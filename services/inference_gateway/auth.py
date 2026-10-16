"""One shared Bearer-key boundary for gateway HTTP and WebSocket requests."""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Request

from .errors import GatewayError


@dataclass(frozen=True)
class ServiceIdentity:
    """Shared identity used to bound idempotency and rate-limit buckets."""

    subject: str = "shared-service-key"


def authenticate_service_key(authorization: str | None, expected_key: str) -> ServiceIdentity:
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        raise GatewayError("UNAUTHORIZED", "service key is required", 401)
    supplied_key = authorization[7:]
    if not supplied_key or not hmac.compare_digest(
        supplied_key.encode("utf-8"), expected_key.encode("utf-8")
    ):
        raise GatewayError("UNAUTHORIZED", "service key is invalid", 401)
    return ServiceIdentity()


async def require_service_key(request: Request) -> str:
    identity = authenticate_service_key(
        request.headers.get("authorization"),
        request.app.state.config.service_key,
    )
    request.state.service_identity = identity
    return identity.subject

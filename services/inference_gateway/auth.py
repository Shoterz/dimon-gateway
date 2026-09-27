"""Verified Cloudflare Access identity handling."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Literal, Protocol

import jwt
from fastapi import Request

from .config import GatewayConfig
from .errors import GatewayError


class JwksClient(Protocol):
    def get_signing_key_from_jwt(self, token: str): ...


@dataclass(frozen=True)
class AccessIdentity:
    subject: str
    credential_type: Literal["user", "service"]
    email: str | None


class AccessTokenVerifier:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        jwks_client: JwksClient | None = None,
    ):
        self.config = config
        self.jwks_client = jwks_client or jwt.PyJWKClient(
            config.jwks_url,
            cache_keys=True,
            max_cached_keys=16,
            cache_jwk_set=True,
            lifespan=config.jwks_cache_seconds,
            timeout=config.jwks_timeout_seconds,
        )

    def verify(self, assertion: str | None, immediate_peer: str | None) -> AccessIdentity:
        try:
            peer = ipaddress.ip_address(immediate_peer or "")
        except ValueError as exc:
            raise GatewayError("UNTRUSTED_PROXY", "request proxy is not trusted", 403) from exc
        if peer not in self.config.trusted_proxies:
            raise GatewayError("UNTRUSTED_PROXY", "request proxy is not trusted", 403)
        if not assertion:
            raise GatewayError("UNAUTHORIZED", "Cloudflare Access identity is required", 401)

        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(assertion)
            claims = jwt.decode(
                assertion,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.config.access_audience,
                issuer=self.config.access_issuer,
                options={"require": ["exp", "iss", "aud", "type"]},
            )
        except Exception as exc:
            raise GatewayError("UNAUTHORIZED", "Cloudflare Access identity is invalid", 401) from exc

        if claims.get("type") != "app":
            raise GatewayError("UNAUTHORIZED", "Cloudflare Access identity is invalid", 401)
        subject = claims.get("sub")
        email = claims.get("email")
        if email is not None and not isinstance(email, str):
            raise GatewayError("UNAUTHORIZED", "Cloudflare Access identity is invalid", 401)
        if isinstance(subject, str) and subject:
            return AccessIdentity(subject, "user", email)
        common_name = claims.get("common_name")
        if isinstance(subject, str) and not subject and isinstance(common_name, str) and common_name:
            return AccessIdentity(common_name, "service", None)
        raise GatewayError("UNAUTHORIZED", "Cloudflare Access identity is invalid", 401)


async def require_access_identity(request: Request) -> AccessIdentity:
    peer = request.client.host if request.client is not None else None
    identity = request.app.state.access_verifier.verify(
        request.headers.get("cf-access-jwt-assertion"),
        peer,
    )
    request.state.access_identity = identity
    return identity

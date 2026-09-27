"""Strict startup configuration for the loopback inference gateway."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from urllib.parse import urlparse


IPAddress = IPv4Address | IPv6Address


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _loopback_address(value: str, label: str) -> IPAddress:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a loopback IP address") from exc
    if not address.is_loopback:
        raise ValueError(f"{label} must be a loopback IP address")
    return address


def _loopback_url(value: str, label: str, schemes: set[str]) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in schemes
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{label} must be a valid loopback URL")
    _loopback_address(parsed.hostname, label)
    return value.rstrip("/")


@dataclass(frozen=True)
class GatewayConfig:
    bind_host: str
    bind_port: int
    trusted_proxies: frozenset[IPAddress]
    access_audience: str
    access_issuer: str
    jwks_url: str
    jwks_cache_seconds: float
    jwks_timeout_seconds: float
    qwen_api_base: str
    nemotron_url: str
    kokoro_url: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "GatewayConfig":
        bind_host = env.get("DIMON_GATEWAY_HOST", "127.0.0.1").strip()
        _loopback_address(bind_host, "DIMON_GATEWAY_HOST")
        try:
            bind_port = int(env.get("DIMON_GATEWAY_PORT", "8891"))
        except ValueError as exc:
            raise ValueError("DIMON_GATEWAY_PORT must be an integer") from exc
        if not 1 <= bind_port <= 65535:
            raise ValueError("DIMON_GATEWAY_PORT must be between 1 and 65535")

        proxy_values = _required(env, "DIMON_GATEWAY_TRUSTED_PROXIES").split(",")
        trusted_proxies = frozenset(
            _loopback_address(value.strip(), "DIMON_GATEWAY_TRUSTED_PROXIES")
            for value in proxy_values
            if value.strip()
        )
        if not trusted_proxies:
            raise ValueError("DIMON_GATEWAY_TRUSTED_PROXIES is required")

        audience = _required(env, "DIMON_CF_ACCESS_AUDIENCE")
        issuer = _required(env, "DIMON_CF_ACCESS_ISSUER").rstrip("/")
        parsed_issuer = urlparse(issuer)
        if (
            parsed_issuer.scheme != "https"
            or parsed_issuer.hostname is None
            or not parsed_issuer.hostname.endswith(".cloudflareaccess.com")
            or parsed_issuer.path not in {"", "/"}
            or parsed_issuer.params
            or parsed_issuer.query
            or parsed_issuer.fragment
            or parsed_issuer.username is not None
        ):
            raise ValueError("DIMON_CF_ACCESS_ISSUER must be an HTTPS Cloudflare Access origin")

        try:
            cache_seconds = float(env.get("DIMON_ACCESS_JWKS_CACHE_SECONDS", "300"))
            timeout_seconds = float(env.get("DIMON_ACCESS_JWKS_TIMEOUT_SECONDS", "3"))
        except ValueError as exc:
            raise ValueError("JWKS cache and timeout values must be numeric") from exc
        if not 1 <= cache_seconds <= 3600 or not 0.1 <= timeout_seconds <= 30:
            raise ValueError("JWKS cache or timeout is outside its safe range")

        qwen = _loopback_url(
            env.get("DIMON_QWEN_API_BASE", "http://127.0.0.1:8080/v1").strip(),
            "DIMON_QWEN_API_BASE",
            {"http", "https"},
        )
        nemotron = _loopback_url(
            env.get("DIMON_NEMOTRON_URL", "ws://127.0.0.1:8004/v1/realtime").strip(),
            "DIMON_NEMOTRON_URL",
            {"ws", "wss"},
        )
        kokoro = _loopback_url(
            env.get("DIMON_KOKORO_URL", "http://127.0.0.1:3001").strip(),
            "DIMON_KOKORO_URL",
            {"http", "https"},
        )
        return cls(
            bind_host=bind_host,
            bind_port=bind_port,
            trusted_proxies=trusted_proxies,
            access_audience=audience,
            access_issuer=issuer,
            jwks_url=f"{issuer}/cdn-cgi/access/certs",
            jwks_cache_seconds=cache_seconds,
            jwks_timeout_seconds=timeout_seconds,
            qwen_api_base=qwen,
            nemotron_url=nemotron,
            kokoro_url=kokoro,
        )

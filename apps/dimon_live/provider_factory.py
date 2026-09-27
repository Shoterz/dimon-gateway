"""Construct inference providers from validated startup configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

from .inference import (
    InferenceProviders,
    ProviderConfigurationError,
    parse_inference_mode,
)
from .model_adapter import QwenTransferExtractor
from .speech import KokoroSynthesizer
from .voice import NemotronClient, NemotronLifecycleClient


_LOCAL_DEFAULTS = {
    "DIMON_QWEN_API_BASE": "http://127.0.0.1:8080/v1",
    "DIMON_QWEN_MODEL": "qwen35-9b-abliterated-q5",
    "DIMON_NEMOTRON_URL": "ws://127.0.0.1:8004/v1/realtime",
    "DIMON_KOKORO_URL": "http://127.0.0.1:3001",
}


def _value(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, _LOCAL_DEFAULTS[key]).strip()
    if not value:
        raise ProviderConfigurationError(
            "INVALID_LOCAL_INFERENCE_CONFIG",
            f"{key} must not be blank in local mode",
        )
    return value


def _endpoint(env: Mapping[str, str], key: str, schemes: set[str]) -> str:
    value = _value(env, key)
    parsed = urlparse(value)
    if parsed.scheme not in schemes or not parsed.netloc:
        raise ProviderConfigurationError(
            "INVALID_LOCAL_INFERENCE_CONFIG",
            f"{key} has an invalid endpoint",
        )
    return value


def build_inference_providers(
    mode: str,
    env: Mapping[str, str],
) -> InferenceProviders:
    selected_mode = parse_inference_mode(mode)
    if selected_mode == "fixture":
        raise ProviderConfigurationError(
            "FIXTURE_PROVIDER_UNAVAILABLE",
            "fixture providers are not installed yet",
        )
    if selected_mode == "remote":
        raise ProviderConfigurationError(
            "REMOTE_PROVIDER_UNAVAILABLE",
            "remote providers require the inference gateway client",
        )

    try:
        qwen_url = _endpoint(env, "DIMON_QWEN_API_BASE", {"http", "https"})
        qwen_model = _value(env, "DIMON_QWEN_MODEL")
        nemotron_url = _endpoint(env, "DIMON_NEMOTRON_URL", {"ws", "wss"})
        kokoro_url = _endpoint(env, "DIMON_KOKORO_URL", {"http", "https"})
        connect_timeout = float(env.get("DIMON_NEMOTRON_CONNECT_TIMEOUT", "3"))
        result_timeout = float(env.get("DIMON_NEMOTRON_RESULT_TIMEOUT", "15"))
        start_timeout = float(env.get("DIMON_NEMOTRON_START_TIMEOUT", "120"))
        runtime_dir = Path(
            env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        )
        lifecycle_path = Path(
            env.get(
                "DIMON_NEMOTRON_CONTROL_SOCKET",
                str(runtime_dir / "jarvis-nemotron.ctl.sock"),
            )
        )
        if not lifecycle_path.is_absolute():
            raise ValueError("control socket must be absolute")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ProviderConfigurationError):
            raise
        raise ProviderConfigurationError(
            "INVALID_LOCAL_INFERENCE_CONFIG",
            "local inference settings are invalid",
        ) from exc

    return InferenceProviders(
        mode="local",
        extractor=QwenTransferExtractor(api_base=qwen_url, model=qwen_model),
        recognizer=NemotronClient(
            nemotron_url,
            connect_timeout=connect_timeout,
            result_timeout=result_timeout,
        ),
        voice_lifecycle=NemotronLifecycleClient(
            lifecycle_path,
            timeout=start_timeout,
        ),
        synthesizer=KokoroSynthesizer(kokoro_url),
    )

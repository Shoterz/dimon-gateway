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
from .fixture_inference import (
    FixtureCatalog,
    FixtureExtractor,
    FixtureRecognizer,
    FixtureSynthesizer,
    FixtureVoiceLifecycle,
)
from .model_adapter import QwenTransferExtractor
from .speech import KokoroSynthesizer
from .remote_inference import (
    RemoteExtractor,
    RemoteGatewayClient,
    RemoteGatewayConfig,
    RemoteRecognizer,
    RemoteSynthesizer,
)
from .voice import NemotronClient, NemotronLifecycleClient
from .intel_voice import WhisperClient


_LOCAL_DEFAULTS = {
    "DIMON_QWEN_API_BASE": "http://127.0.0.1:8080/v1",
    "DIMON_QWEN_MODEL": "qwen35-9b-abliterated-q5",
    "DIMON_NEMOTRON_URL": "ws://127.0.0.1:8004/v1/realtime",
    "DIMON_KOKORO_URL": "http://127.0.0.1:3001",
    "DIMON_WHISPER_URL": "ws://127.0.0.1:8005/v1/stream",
    "DIMON_WHISPER_CPP_URL": "ws://127.0.0.1:8006/v1/stream",
}

_STT_BACKENDS = {"nemotron", "whisper_intel", "whisper_cpp"}


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
        fixture_root = Path(__file__).resolve().parents[2] / "fixtures" / "inference"
        voice_case = env.get("DIMON_FIXTURE_VOICE_CASE", "normal").strip() or "normal"
        speech_case = env.get("DIMON_FIXTURE_SPEECH_CASE", "normal").strip() or "normal"
        return InferenceProviders(
            mode="fixture",
            extractor=FixtureExtractor(FixtureCatalog.load(fixture_root / "manifest.json")),
            recognizer=FixtureRecognizer.load(
                fixture_root / "voice" / "manifest.json",
                voice_case,
            ),
            voice_lifecycle=FixtureVoiceLifecycle.load(
                fixture_root / "voice" / "manifest.json",
                voice_case,
            ),
            synthesizer=FixtureSynthesizer.load(
                fixture_root / "speech" / "manifest.json",
                speech_case,
            ),
        )
    if selected_mode == "remote":
        try:
            remote_client = RemoteGatewayClient(RemoteGatewayConfig.from_env(env))
        except (TypeError, ValueError) as exc:
            raise ProviderConfigurationError(
                "INVALID_REMOTE_INFERENCE_CONFIG",
                "remote inference settings are invalid",
            ) from exc
        return InferenceProviders(
            mode="remote",
            extractor=RemoteExtractor(remote_client),
            recognizer=RemoteRecognizer(remote_client),
            voice_lifecycle=None,
            synthesizer=RemoteSynthesizer(remote_client),
        )

    stt_backend = env.get("DIMON_STT_BACKEND", "nemotron").strip().lower() or "nemotron"
    if stt_backend not in _STT_BACKENDS:
        raise ProviderConfigurationError(
            "INVALID_LOCAL_INFERENCE_CONFIG",
            "DIMON_STT_BACKEND must be nemotron, whisper_intel, or whisper_cpp",
        )

    try:
        qwen_url = _endpoint(env, "DIMON_QWEN_API_BASE", {"http", "https"})
        qwen_model = _value(env, "DIMON_QWEN_MODEL")
        kokoro_url = _endpoint(env, "DIMON_KOKORO_URL", {"http", "https"})
        if stt_backend == "whisper_intel":
            whisper_url = _endpoint(env, "DIMON_WHISPER_URL", {"ws", "wss"})
            whisper_connect_timeout = float(env.get("DIMON_WHISPER_CONNECT_TIMEOUT", "3"))
            whisper_result_timeout = float(env.get("DIMON_WHISPER_RESULT_TIMEOUT", "30"))
        elif stt_backend == "whisper_cpp":
            whisper_url = _endpoint(env, "DIMON_WHISPER_CPP_URL", {"ws", "wss"})
            whisper_connect_timeout = float(env.get("DIMON_WHISPER_CPP_CONNECT_TIMEOUT", "3"))
            whisper_result_timeout = float(env.get("DIMON_WHISPER_CPP_RESULT_TIMEOUT", "90"))
        else:
            nemotron_url = _endpoint(env, "DIMON_NEMOTRON_URL", {"ws", "wss"})
            connect_timeout = float(env.get("DIMON_NEMOTRON_CONNECT_TIMEOUT", "3"))
            result_timeout = float(env.get("DIMON_NEMOTRON_RESULT_TIMEOUT", "15"))
            start_timeout = float(env.get("DIMON_NEMOTRON_START_TIMEOUT", "120"))
            # os.getuid is POSIX-only; Windows hosts must set
            # XDG_RUNTIME_DIR or DIMON_NEMOTRON_CONTROL_SOCKET explicitly.
            uid = os.getuid() if hasattr(os, "getuid") else "unknown"
            runtime_dir = Path(env.get("XDG_RUNTIME_DIR", f"/run/user/{uid}"))
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

    if stt_backend in {"whisper_intel", "whisper_cpp"}:
        recognizer = WhisperClient(
            whisper_url,
            connect_timeout=whisper_connect_timeout,
            result_timeout=whisper_result_timeout,
        )
        voice_lifecycle = None
    else:
        recognizer = NemotronClient(
            nemotron_url,
            connect_timeout=connect_timeout,
            result_timeout=result_timeout,
        )
        voice_lifecycle = NemotronLifecycleClient(
            lifecycle_path,
            timeout=start_timeout,
        )

    return InferenceProviders(
        mode="local",
        extractor=QwenTransferExtractor(api_base=qwen_url, model=qwen_model),
        recognizer=recognizer,
        voice_lifecycle=voice_lifecycle,
        synthesizer=KokoroSynthesizer(kokoro_url),
    )

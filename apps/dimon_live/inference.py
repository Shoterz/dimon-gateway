"""Shared inference-provider contracts for every Dimon runtime mode."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from .model_adapter import Extractor


InferenceMode = Literal["fixture", "remote", "local"]
Synthesizer = Callable[[str], Awaitable[bytes]]


class ProviderConfigurationError(ValueError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


class Recognizer(Protocol):
    async def open_stream(self, on_partial: Callable[[str], Awaitable[None]]) -> Any: ...

    async def transcribe(
        self,
        audio: bytes,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> str: ...


class VoiceLifecycle(Protocol):
    async def acquire(self) -> dict[str, object]: ...

    async def release(self, *, disconnected: bool = False) -> None: ...


@dataclass(frozen=True)
class InferenceProviders:
    mode: InferenceMode
    extractor: Extractor
    recognizer: Recognizer
    voice_lifecycle: VoiceLifecycle | None
    synthesizer: Synthesizer


def parse_inference_mode(value: str) -> InferenceMode:
    normalized = value.strip().lower()
    if normalized not in {"fixture", "remote", "local"}:
        raise ProviderConfigurationError(
            "INVALID_INFERENCE_MODE",
            "DIMON_INFERENCE_MODE must be fixture, remote, or local",
        )
    return cast(InferenceMode, normalized)

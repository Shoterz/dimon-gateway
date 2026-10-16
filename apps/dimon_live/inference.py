"""Shared inference-provider contracts for every Dimon runtime mode."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from .model_adapter import Extractor


InferenceMode = Literal["fixture", "remote", "local"]


class Synthesizer(Protocol):
    async def __call__(self, text: str) -> bytes: ...

    async def synthesize_result(self, result: dict[str, Any]) -> bytes: ...


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


class VoiceLifecycleLease(Protocol):
    async def release(self, *, disconnected: bool = False) -> None: ...


class VoiceLifecycle(Protocol):
    async def acquire(self) -> VoiceLifecycleLease: ...


@dataclass(frozen=True)
class InferenceProviders:
    mode: InferenceMode
    extractor: Extractor
    recognizer: Recognizer
    voice_lifecycle: VoiceLifecycle | None
    synthesizer: Synthesizer
    stock_extractor: Extractor | None = None
    followup_interpreter: Any | None = None
    bill_payment_extractor: Extractor | None = None
    multi_action_extractor: Extractor | None = None


def parse_inference_mode(value: str) -> InferenceMode:
    normalized = value.strip().lower()
    if normalized not in {"fixture", "remote", "local"}:
        raise ProviderConfigurationError(
            "INVALID_INFERENCE_MODE",
            "DIMON_INFERENCE_MODE must be fixture, remote, or local",
        )
    return cast(InferenceMode, normalized)

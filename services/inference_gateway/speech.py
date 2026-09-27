"""Result-bound Kokoro adapter and strict WAV validation."""

from __future__ import annotations

import io
import math
import time
import wave
from dataclasses import dataclass
from typing import Protocol

from apps.dimon_live.speech import KokoroSynthesizer

from .config import GatewayConfig


@dataclass(frozen=True)
class SpeechSynthesis:
    data: bytes
    media_type: str
    revision: str
    latency_ms: float


class SpeechSynthesizer(Protocol):
    async def __call__(self, text: str) -> SpeechSynthesis: ...


class KokoroGatewaySynthesizer:
    def __init__(self, config: GatewayConfig):
        self.synthesizer = KokoroSynthesizer(config.kokoro_url, timeout=30)
        self.revision = config.kokoro_revision

    async def __call__(self, text: str) -> SpeechSynthesis:
        started = time.perf_counter()
        data = await self.synthesizer(text)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        return SpeechSynthesis(data, "audio/wav", self.revision, latency_ms)


def validate_synthesis(
    result: SpeechSynthesis,
    *,
    max_wav_bytes: int = 8 * 1_048_576,
) -> SpeechSynthesis:
    if (
        not isinstance(result.data, bytes)
        or len(result.data) > max_wav_bytes
        or result.media_type != "audio/wav"
        or not isinstance(result.revision, str)
        or not result.revision
        or any(ord(character) < 33 or ord(character) > 126 for character in result.revision)
        or not isinstance(result.latency_ms, (int, float))
        or not math.isfinite(result.latency_ms)
        or result.latency_ms < 0
    ):
        raise ValueError("speech synthesis metadata is invalid")
    try:
        with wave.open(io.BytesIO(result.data), "rb") as audio:
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or not 8_000 <= audio.getframerate() <= 48_000
                or audio.getnframes() <= 0
            ):
                raise ValueError("speech WAV format is invalid")
            frames = audio.readframes(audio.getnframes())
            if len(frames) != audio.getnframes() * audio.getsampwidth():
                raise ValueError("speech WAV data is truncated")
    except (EOFError, wave.Error) as exc:
        raise ValueError("speech WAV is invalid") from exc
    return result

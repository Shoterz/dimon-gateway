"""Bounded audio framing shared by the CUDA child worker."""

from __future__ import annotations

import numpy as np


def decode_f32le_audio(payload: bytes) -> np.ndarray:
    if not payload:
        return np.empty(0, dtype=np.float32)
    if len(payload) % 4:
        raise ValueError("audio payload is not aligned float32 data")
    audio = np.frombuffer(payload, dtype="<f4")
    if not np.all(np.isfinite(audio)):
        raise ValueError("audio payload contains non-finite samples")
    return audio.astype(np.float32, copy=False)


class AudioAccumulator:
    def __init__(self, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._maximum = max_bytes
        self._payload = bytearray()

    def append(self, chunk: bytes) -> None:
        if len(self._payload) + len(chunk) > self._maximum:
            raise OverflowError("audio payload exceeds the configured limit")
        self._payload.extend(chunk)

    def decode(self) -> np.ndarray:
        return decode_f32le_audio(bytes(self._payload))

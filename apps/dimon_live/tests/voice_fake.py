"""A stand-in speaker engine and synthetic "voices" for tests.

Each speaker is a tone at its own pitch; the fake engine's embedding is the
spectrum around the dominant frequency, so the same speaker always matches
and different speakers never do. Every clip has fresh noise, so no two clips
are byte-identical unless a test replays one on purpose.
"""

from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16_000
PITCH = {"account-holder": 180.0, "stranger": 310.0, "tv": 520.0}


def clip(speaker: str = "account-holder", seconds: float = 1.6, *, silence_after: float = 0.0,
         seed: int | None = None) -> bytes:
    rng = np.random.default_rng(seed)
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    voice = 0.3 * np.sin(2 * np.pi * PITCH[speaker] * t) + 0.01 * rng.standard_normal(t.size)
    audio = np.concatenate([voice, np.zeros(int(SAMPLE_RATE * silence_after))])
    return audio.astype("<f4").tobytes()


class FakeSpeakerEngine:
    version = "fake-speaker-1"

    def embed(self, pcm: np.ndarray) -> np.ndarray:
        spectrum = np.abs(np.fft.rfft(np.asarray(pcm, dtype=np.float64)))
        frequencies = np.fft.rfftfreq(len(pcm), 1 / SAMPLE_RATE)
        peak = frequencies[int(np.argmax(spectrum))]
        centres = np.arange(100, 800, 10.0)
        vector = np.exp(-((centres - peak) ** 2) / (2 * 15.0 ** 2))
        return vector / np.linalg.norm(vector)

"""A stand-in face engine for tests: each "frame" is JSON describing who is in
front of the camera and how far their head is turned. Everything else (the
face database, liveness, matching, replay and challenge binding) is the real
code path."""

from __future__ import annotations

import hashlib
import json
import math
import random
import secrets

from apps.dimon_live.face_id import FrameAnalysis


def _identity(person: str) -> list[float]:
    rng = random.Random(hashlib.sha256(person.encode()).digest())
    vector = [rng.gauss(0, 1) for _ in range(128)]
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


class FakeFaceEngine:
    version = "test-face-engine-1"

    def analyze(self, frame: bytes) -> FrameAnalysis:
        data = json.loads(frame)
        faces = data.get("faces", 1)
        if faces != 1:
            return FrameAnalysis(face_count=faces)
        base = _identity(data["person"])
        rng = random.Random(data["nonce"])
        noisy = [v + rng.gauss(0, 0.02) for v in base]
        norm = math.sqrt(sum(v * v for v in noisy))
        return FrameAnalysis(face_count=1, embedding=tuple(v / norm for v in noisy), yaw=data["yaw"])


def live_frames(person: str = "account-holder", *, yaws=(0.0, 0.05, 0.18, 0.32, 0.15, 0.02), faces: int = 1) -> list[bytes]:
    """A head-turn sequence of fresh frames (never seen before)."""

    return [
        json.dumps({"person": person, "yaw": yaw, "faces": faces, "nonce": secrets.token_hex(8)}).encode()
        for yaw in yaws
    ]

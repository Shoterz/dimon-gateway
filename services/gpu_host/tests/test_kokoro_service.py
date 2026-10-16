from __future__ import annotations

import asyncio
import unittest

import httpx

from services.kokoro import server as kokoro_server
from services.kokoro.server import create_app


WAV = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 28


class FakeEngine:
    def __init__(self, *, error: bool = False):
        self.error = error
        self.calls: list[str] = []

    def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        if self.error:
            raise RuntimeError("private engine detail")
        return WAV


class KokoroServiceTests(unittest.TestCase):
    def test_all_speech_uses_the_single_authoritative_speed(self) -> None:
        self.assertEqual(getattr(kokoro_server, "KOKORO_SPEED", None), 1.20)

    async def request(self, app, method: str, path: str, **kwargs):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://kokoro.test"
        ) as client:
            return await client.request(method, path, **kwargs)

    def test_health_and_bounded_speech_contract(self) -> None:
        engine = FakeEngine()
        app = create_app(engine)
        health = asyncio.run(self.request(app, "GET", "/health"))
        speech = asyncio.run(
            self.request(app, "POST", "/v1/speech", json={"text": "Synthetic result."})
        )
        extra = asyncio.run(
            self.request(app, "POST", "/v1/speech", json={"text": "hello", "voice": "other"})
        )
        oversized = asyncio.run(
            self.request(app, "POST", "/v1/speech", json={"text": "x" * 1001})
        )
        self.assertEqual(health.json(), {"status": "ready"})
        self.assertEqual(speech.status_code, 200)
        self.assertEqual(speech.headers["content-type"], "audio/wav")
        self.assertEqual(speech.content, WAV)
        self.assertEqual(engine.calls, ["Synthetic result."])
        self.assertEqual(extra.status_code, 422)
        self.assertEqual(oversized.status_code, 422)

    def test_engine_failure_is_content_free(self) -> None:
        response = asyncio.run(
            self.request(
                create_app(FakeEngine(error=True)),
                "POST",
                "/v1/speech",
                json={"text": "safe"},
            )
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private engine detail", response.text)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest

from starlette.testclient import TestClient

from services.whisper_intel.server import create_app


class FakeEngine:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[int] = []

    def transcribe(self, pcm16_audio: bytes) -> str:
        self.calls.append(len(pcm16_audio))
        return self.responses.pop(0)


class WhisperIntelServiceTests(unittest.TestCase):
    def test_health_route(self) -> None:
        app = create_app(FakeEngine([]))
        client = TestClient(app)
        response = client.get("/health")
        self.assertEqual(response.json(), {"status": "ready"})

    def test_stream_sends_partial_after_threshold_then_final_on_commit(self) -> None:
        engine = FakeEngine(["send John", "send John Smith S$5"])
        app = create_app(engine)
        client = TestClient(app)
        with client.websocket_connect("/v1/stream") as websocket:
            ready = websocket.receive_json()
            self.assertEqual(ready, {"type": "ready", "sample_rate": 16000})

            # 48000 bytes == 1.5s of 16kHz mono 16-bit PCM, the partial threshold.
            websocket.send_bytes(b"\x00\x01" * 24000)
            partial = websocket.receive_json()
            self.assertEqual(partial, {"type": "partial", "text": "send John"})

            websocket.send_text(json.dumps({"type": "commit"}))
            final = websocket.receive_json()
            self.assertEqual(final, {"type": "final", "text": "send John Smith S$5"})

        self.assertEqual(engine.calls, [48000, 48000])

    def test_stream_reports_error_without_engine_exception_text(self) -> None:
        class FailingEngine:
            def transcribe(self, pcm16_audio: bytes) -> str:
                raise RuntimeError("private engine detail")

        app = create_app(FailingEngine())
        client = TestClient(app)
        with client.websocket_connect("/v1/stream") as websocket:
            websocket.receive_json()  # ready
            websocket.send_bytes(b"\x00\x01" * 24000)
            error = websocket.receive_json()
            self.assertEqual(error, {"type": "error", "reason_code": "TRANSCRIPTION_UNAVAILABLE"})
            self.assertNotIn("private engine detail", json.dumps(error))


if __name__ == "__main__":
    unittest.main()

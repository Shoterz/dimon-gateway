from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from services.whisper_cpp.server import WhisperEngine, WhisperPaths, build_engine, create_app


class FakeEngine:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[int] = []

    def transcribe(self, pcm16_audio: bytes, *, max_new_tokens: int = 256) -> str:
        self.calls.append(len(pcm16_audio))
        return self.responses.pop(0)


class WhisperCppServiceTests(unittest.TestCase):
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

            # 32000 bytes == 1.0s of 16kHz mono 16-bit PCM, the partial threshold.
            websocket.send_bytes(b"\x00\x01" * 16000)
            partial = websocket.receive_json()
            self.assertEqual(partial, {"type": "partial", "text": "send John"})

            websocket.send_text(json.dumps({"type": "commit"}))
            final = websocket.receive_json()
            self.assertEqual(final, {"type": "final", "text": "send John Smith S$5"})

        self.assertEqual(engine.calls, [32000, 32000])

    def test_slow_partial_decode_keeps_reading_and_skips_overlapping_windows(self) -> None:
        release = threading.Event()

        class SlowEngine(FakeEngine):
            def transcribe(self, pcm16_audio: bytes, *, max_new_tokens: int = 256) -> str:
                if not self.calls:
                    release.wait(5)
                return super().transcribe(pcm16_audio, max_new_tokens=max_new_tokens)

        engine = SlowEngine(["send", "send John Smith"])
        client = TestClient(create_app(engine))
        with client.websocket_connect("/v1/stream") as websocket:
            websocket.receive_json()  # ready
            # Three seconds of audio arrive while the first partial is decoding.
            for _ in range(3):
                websocket.send_bytes(b"\x00\x01" * 16000)
            release.set()
            websocket.send_text(json.dumps({"type": "commit"}))
            self.assertEqual(websocket.receive_json(), {"type": "partial", "text": "send"})
            self.assertEqual(
                websocket.receive_json(), {"type": "final", "text": "send John Smith"}
            )

        # One partial for the first second, then the final over all audio;
        # the windows that arrived mid-decode were skipped, not queued.
        self.assertEqual(engine.calls, [32000, 96000])

    def test_stream_reports_error_without_engine_exception_text(self) -> None:
        class FailingEngine:
            def transcribe(self, pcm16_audio: bytes, *, max_new_tokens: int = 256) -> str:
                raise RuntimeError("private engine detail")

        app = create_app(FailingEngine())
        client = TestClient(app)
        with client.websocket_connect("/v1/stream") as websocket:
            websocket.receive_json()  # ready
            websocket.send_bytes(b"\x00\x01" * 16000)
            error = websocket.receive_json()
            self.assertEqual(error, {"type": "error", "reason_code": "TRANSCRIPTION_UNAVAILABLE"})
            self.assertNotIn("private engine detail", json.dumps(error))


class WhisperEngineTests(unittest.TestCase):
    def test_transcribe_returns_empty_string_for_empty_audio(self) -> None:
        engine = WhisperEngine(WhisperPaths(binary="/fake/whisper-cli", model="/fake/ggml-small.bin"))
        self.assertEqual(engine.transcribe(b""), "")

    def test_transcribe_invokes_binary_and_returns_joined_stdout(self) -> None:
        engine = WhisperEngine(WhisperPaths(binary="/fake/whisper-cli", model="/fake/ggml-small.bin"))
        fake_result = MagicMock(returncode=0, stdout=" send John Smith S$5 \n\n", stderr="")
        with patch("services.whisper_cpp.server.subprocess.run", return_value=fake_result) as mock_run:
            text = engine.transcribe(b"\x00\x01" * 16000)

        self.assertEqual(text, "send John Smith S$5")
        called_args = mock_run.call_args.args[0]
        self.assertEqual(called_args[0], "/fake/whisper-cli")
        self.assertIn("-m", called_args)
        self.assertIn("/fake/ggml-small.bin", called_args)
        self.assertIn("-f", called_args)
        self.assertIn("-nt", called_args)

    def test_transcribe_raises_when_binary_exits_nonzero(self) -> None:
        engine = WhisperEngine(WhisperPaths(binary="/fake/whisper-cli", model="/fake/ggml-small.bin"))
        fake_result = MagicMock(returncode=1, stdout="", stderr="model load failed")
        with patch("services.whisper_cpp.server.subprocess.run", return_value=fake_result):
            with self.assertRaises(RuntimeError):
                engine.transcribe(b"\x00\x01" * 16000)


class BuildEngineTests(unittest.TestCase):
    def test_falls_back_to_the_cli_without_a_server_binary(self):
        engine = build_engine("/fake/whisper-cli", "/fake/ggml-small.bin")
        self.assertIsInstance(engine, WhisperEngine)


if __name__ == "__main__":
    unittest.main()

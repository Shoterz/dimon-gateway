import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from apps.dimon_live.database import Database
from apps.dimon_live.session_manager import SessionContext
from apps.dimon_live.speech import KokoroSynthesizer, SpeechService


WAV = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 28
REAL_ASYNC_CLIENT = httpx.AsyncClient


class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        database = Database(Path(self.temp.name) / "demo.sqlite3")
        database.initialize()
        self.context = SessionContext("visitor-1", "visitor", database)

    def tearDown(self):
        self.temp.cleanup()

    def test_committed_result_creates_opaque_session_audio_artifact(self):
        async def synthesize(text):
            self.assertIn("John Smith", text)
            return WAV

        service = SpeechService(
            base_url="http://kokoro.test",
            audio_dir=Path(self.temp.name) / "audio",
            synthesizer=synthesize,
        )
        result = asyncio.run(
            service.speak_result(
                {
                    "outcome": "EXECUTED_SYNTHETIC_TRANSFER",
                    "amount_minor": 500,
                    "resolved_action": {"recipient_display_name": "John Smith"},
                },
                self.context,
            )
        )
        self.assertIsNotNone(result)
        self.assertNotIn("John Smith", result.artifact_id)
        self.assertEqual(asyncio.run(service.read(result.artifact_id, self.context)), WAV)

    def test_kokoro_failure_returns_none_and_does_not_change_result(self):
        async def fail(text):
            raise RuntimeError("offline")

        service = SpeechService(
            base_url="http://kokoro.test",
            audio_dir=Path(self.temp.name) / "audio",
            synthesizer=fail,
        )
        result = asyncio.run(
            service.speak_result(
                {
                    "outcome": "EXECUTED_SYNTHETIC_TRANSFER",
                    "amount_minor": 500,
                    "resolved_action": {"recipient_display_name": "John Smith"},
                },
                self.context,
            )
        )
        self.assertIsNone(result)

    def test_kokoro_client_uses_bounded_project_service_contract(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, headers={"content-type": "audio/wav"}, content=WAV)

        def client_factory(**kwargs):
            return REAL_ASYNC_CLIENT(
                base_url=kwargs["base_url"], transport=httpx.MockTransport(handler)
            )

        with patch("apps.dimon_live.speech.httpx.AsyncClient", side_effect=client_factory):
            audio = asyncio.run(KokoroSynthesizer("http://kokoro.test")("Safe result."))

        self.assertEqual(audio, WAV)
        self.assertEqual(requests[0].url.path, "/v1/speech")
        self.assertEqual(requests[0].read(), b'{"text":"Safe result."}')

    def test_kokoro_client_rejects_non_wav_response(self):
        def client_factory(**kwargs):
            return REAL_ASYNC_CLIENT(
                base_url=kwargs["base_url"],
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, headers={"content-type": "application/json"}, json={}
                    )
                ),
            )

        with patch("apps.dimon_live.speech.httpx.AsyncClient", side_effect=client_factory):
            with self.assertRaises(RuntimeError):
                asyncio.run(KokoroSynthesizer("http://kokoro.test")("Safe result."))

    def test_kokoro_client_supports_bounded_gradio_shared_service(self):
        paths = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/v1/speech":
                return httpx.Response(404)
            if request.method == "POST":
                return httpx.Response(200, json={"event_id": "event-1"})
            if request.url.path.endswith("/event-1"):
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    text='event: complete\ndata: [{"url":"http://kokoro.test/gradio_a/gradio_api/file=out.wav"}]\n\n',
                )
            return httpx.Response(200, content=WAV)

        def client_factory(**kwargs):
            return REAL_ASYNC_CLIENT(
                base_url=kwargs["base_url"], transport=httpx.MockTransport(handler)
            )

        with patch("apps.dimon_live.speech.httpx.AsyncClient", side_effect=client_factory):
            audio = asyncio.run(KokoroSynthesizer("http://kokoro.test")("Safe result."))

        self.assertEqual(audio, WAV)
        self.assertEqual(
            paths,
            [
                "/v1/speech",
                "/gradio_api/call/KOKORO_TTS_API",
                "/gradio_api/call/KOKORO_TTS_API/event-1",
                "/gradio_api/file=out.wav",
            ],
        )

    def test_non_committed_result_is_not_sent_to_kokoro(self):
        called = []

        async def synthesize(text):
            called.append(text)
            return WAV

        service = SpeechService(
            base_url="http://kokoro.test",
            audio_dir=Path(self.temp.name) / "audio",
            synthesizer=synthesize,
        )
        result = asyncio.run(service.speak_result({"outcome": "BLOCKED"}, self.context))
        self.assertIsNone(result)
        self.assertEqual(called, [])

    def test_structured_synthesizer_receives_the_committed_result(self):
        class StructuredSynthesizer:
            def __init__(self):
                self.result = None

            async def synthesize_result(self, result):
                self.result = result
                return WAV

            async def __call__(self, _text):
                raise AssertionError("structured synthesizer must not receive free-form text")

        synthesizer = StructuredSynthesizer()
        service = SpeechService(
            base_url="http://kokoro.test",
            audio_dir=Path(self.temp.name) / "audio",
            synthesizer=synthesizer,
        )
        committed = {
            "outcome": "EXECUTED_SYNTHETIC_TRANSFER",
            "amount_minor": 500,
            "resolved_action": {"recipient_display_name": "John Smith"},
        }

        artifact = asyncio.run(service.speak_result(committed, self.context))

        self.assertIsNotNone(artifact)
        self.assertEqual(synthesizer.result, committed)

    def test_queued_result_can_be_returned_before_kokoro_finishes(self):
        async def scenario():
            started = asyncio.Event()
            release = asyncio.Event()

            async def synthesize(text):
                started.set()
                await release.wait()
                return WAV

            service = SpeechService(
                base_url="http://kokoro.test",
                audio_dir=Path(self.temp.name) / "audio",
                synthesizer=synthesize,
            )
            artifact_id = service.queue_result(
                {
                    "outcome": "EXECUTED_SYNTHETIC_TRANSFER",
                    "amount_minor": 500,
                    "resolved_action": {"recipient_display_name": "John Smith"},
                },
                self.context,
            )
            self.assertIsInstance(artifact_id, str)
            self.assertTrue(service.is_pending(artifact_id, self.context))
            await asyncio.wait_for(started.wait(), 0.5)
            release.set()
            for _ in range(20):
                if not service.is_pending(artifact_id, self.context):
                    break
                await asyncio.sleep(0.01)
            self.assertFalse(service.is_pending(artifact_id, self.context))
            artifact = await service.read(artifact_id, self.context)
            self.assertEqual(artifact, WAV)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

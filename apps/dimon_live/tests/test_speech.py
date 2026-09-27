import asyncio
import tempfile
import unittest
from pathlib import Path

from apps.dimon_live.database import Database
from apps.dimon_live.session_manager import SessionContext
from apps.dimon_live.speech import SpeechService


WAV = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 28


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

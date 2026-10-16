from __future__ import annotations

import asyncio
import io
import unittest
import wave
from pathlib import Path

from apps.dimon_live.fixture_inference import (
    FixtureRecognizer,
    FixtureSynthesizer,
    FixtureVoiceLifecycle,
)
from apps.dimon_live.model_adapter import InferenceFailure
from apps.dimon_live.provider_factory import build_inference_providers


ROOT = Path(__file__).parents[3]
VOICE_MANIFEST = ROOT / "fixtures/inference/voice/manifest.json"
SPEECH_MANIFEST = ROOT / "fixtures/inference/speech/manifest.json"


class FixtureVoiceTests(unittest.TestCase):
    def test_recognizer_emits_partials_before_the_final_transcript(self) -> None:
        events: list[tuple[str, str]] = []
        recognizer = FixtureRecognizer.load(VOICE_MANIFEST, "normal")

        async def scenario() -> str:
            async def partial(text: str) -> None:
                events.append(("partial", text))

            transcript = await recognizer.transcribe(b"\0" * 128, partial)
            events.append(("final", transcript))
            return transcript

        transcript = asyncio.run(scenario())

        self.assertEqual(transcript, "Send John Smith S$50 from savings.")
        self.assertEqual([kind for kind, _ in events], ["partial", "partial", "final"])

    def test_stream_rejects_audio_over_the_live_limit(self) -> None:
        recognizer = FixtureRecognizer.load(VOICE_MANIFEST, "normal", max_audio_bytes=16)

        async def scenario() -> None:
            stream = await recognizer.open_stream(lambda _text: asyncio.sleep(0))
            await stream.push_audio(b"\0" * 20)

        with self.assertRaises(InferenceFailure) as raised:
            asyncio.run(scenario())
        self.assertEqual(raised.exception.reason_code, "AUDIO_LIMIT_EXCEEDED")

    def test_stream_enforces_live_audio_shape_and_duration(self) -> None:
        malformed = FixtureRecognizer.load(VOICE_MANIFEST, "normal")

        async def push(recognizer: FixtureRecognizer, audio: bytes) -> None:
            stream = await recognizer.open_stream(lambda _text: asyncio.sleep(0))
            await stream.push_audio(audio)

        with self.assertRaises(InferenceFailure) as malformed_error:
            asyncio.run(push(malformed, b"bad"))
        self.assertEqual(malformed_error.exception.reason_code, "MALFORMED_AUDIO")

        too_long = FixtureRecognizer.load(
            VOICE_MANIFEST,
            "normal",
            max_audio_bytes=1_000,
            max_utterance_seconds=0.001,
        )
        with self.assertRaises(InferenceFailure) as duration_error:
            asyncio.run(push(too_long, b"\0" * 128))
        self.assertEqual(duration_error.exception.reason_code, "UTTERANCE_LIMIT_EXCEEDED")

    def test_voice_lifecycle_supports_normal_unavailable_and_busy(self) -> None:
        normal = FixtureVoiceLifecycle.load(VOICE_MANIFEST, "normal")
        lease = asyncio.run(normal.acquire())
        self.assertEqual(lease.status, {"ok": True, "fixture": True})
        asyncio.run(lease.release())
        asyncio.run(lease.release())

        expected = {
            "nemotron-unavailable": "UPSTREAM_UNAVAILABLE",
            "voice-busy": "VOICE_BUSY",
        }
        for case_id, reason_code in expected.items():
            with self.subTest(case_id=case_id):
                lifecycle = FixtureVoiceLifecycle.load(VOICE_MANIFEST, case_id)
                with self.assertRaises(InferenceFailure) as raised:
                    asyncio.run(lifecycle.acquire())
                self.assertEqual(raised.exception.reason_code, reason_code)


class FixtureSpeechTests(unittest.TestCase):
    def test_synthesizer_returns_deterministic_bounded_mono_wav(self) -> None:
        synth = FixtureSynthesizer.load(SPEECH_MANIFEST, "normal")

        first = asyncio.run(synth("Synthetic transfer complete."))
        second = asyncio.run(synth("Different server-owned result."))

        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 8 * 1_048_576)
        with wave.open(io.BytesIO(first), "rb") as audio:
            self.assertEqual(audio.getnchannels(), 1)
            self.assertEqual(audio.getsampwidth(), 2)
            self.assertEqual(audio.getframerate(), 16_000)

    def test_synthesizer_can_report_kokoro_unavailable(self) -> None:
        synth = FixtureSynthesizer.load(SPEECH_MANIFEST, "kokoro-unavailable")

        with self.assertRaises(InferenceFailure) as raised:
            asyncio.run(synth("Synthetic transfer complete."))

        self.assertEqual(raised.exception.reason_code, "UPSTREAM_UNAVAILABLE")

    def test_fixture_factory_uses_named_voice_and_speech_cases(self) -> None:
        providers = build_inference_providers(
            "fixture",
            {
                "DIMON_FIXTURE_VOICE_CASE": "voice-busy",
                "DIMON_FIXTURE_SPEECH_CASE": "kokoro-unavailable",
            },
        )

        self.assertEqual(providers.mode, "fixture")
        with self.assertRaises(InferenceFailure) as voice_error:
            asyncio.run(providers.voice_lifecycle.acquire())
        self.assertEqual(voice_error.exception.reason_code, "VOICE_BUSY")
        with self.assertRaises(InferenceFailure) as speech_error:
            asyncio.run(providers.synthesizer("result"))
        self.assertEqual(speech_error.exception.reason_code, "UPSTREAM_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()

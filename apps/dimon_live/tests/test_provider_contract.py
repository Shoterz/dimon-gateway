from __future__ import annotations

import asyncio
import io
import json
import struct
import unittest
import wave
from collections.abc import Awaitable, Callable

from apps.dimon_live.inference import InferenceProviders
from apps.dimon_live.model_adapter import (
    ExtractedIntent,
    InferenceFailure,
    InvalidModelOutput,
)
from apps.dimon_live.provider_factory import build_inference_providers


EXACT_PROMPT = "Send John Smith S$50 from savings."
MALFORMED_PROMPT = "Fixture: return malformed model output."
TIMEOUT_PROMPT = "Fixture: simulate Qwen timeout."


class ProviderContractMixin:
    expected_mode: str

    def make_providers(self, scenario: str = "normal") -> InferenceProviders:
        raise NotImplementedError

    def test_bundle_reports_the_selected_mode(self) -> None:
        self.assertEqual(self.make_providers().mode, self.expected_mode)

    def test_intent_returns_the_shared_bounded_shape(self) -> None:
        extracted = self.make_providers().extractor.extract(EXACT_PROMPT)

        self.assertEqual(extracted.intent["contract_version"], 3)
        self.assertEqual(extracted.intent["kind"], "BENEFICIARY_TRANSFER")
        self.assertEqual(extracted.intent["recipient_label"], "John Smith")

    def test_voice_emits_partials_before_final_text(self) -> None:
        events: list[str] = []
        recognizer = self.make_providers().recognizer

        async def scenario() -> None:
            async def partial(text: str) -> None:
                events.append(f"partial:{text}")

            stream = await recognizer.open_stream(partial)
            await stream.push_audio(b"\0" * 128)
            events.append(f"final:{await stream.finish()}")

        asyncio.run(scenario())

        self.assertGreaterEqual(len(events), 2)
        self.assertTrue(events[0].startswith("partial:"))
        self.assertEqual(events[-1], f"final:{EXACT_PROMPT}")

    def test_speech_returns_a_valid_bounded_wav(self) -> None:
        audio = asyncio.run(self.make_providers().synthesizer("result"))

        self.assertLessEqual(len(audio), 8 * 1_048_576)
        with wave.open(io.BytesIO(audio), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), 16_000)

    def test_invalid_output_has_a_stable_reason_code(self) -> None:
        with self.assertRaises(InferenceFailure) as raised:
            self.make_providers().extractor.extract(MALFORMED_PROMPT)
        self.assertEqual(raised.exception.reason_code, "INVALID_MODEL_OUTPUT")

    def test_timeout_has_a_stable_reason_code(self) -> None:
        with self.assertRaises(InferenceFailure) as raised:
            self.make_providers("timeout").extractor.extract(TIMEOUT_PROMPT)
        self.assertEqual(raised.exception.reason_code, "UPSTREAM_TIMEOUT")

    def test_voice_and_speech_unavailable_have_stable_reason_codes(self) -> None:
        voice = self.make_providers("voice-unavailable")
        speech = self.make_providers("speech-unavailable")

        with self.assertRaises(InferenceFailure) as voice_error:
            asyncio.run(voice.voice_lifecycle.acquire())
        self.assertEqual(voice_error.exception.reason_code, "UPSTREAM_UNAVAILABLE")
        with self.assertRaises(InferenceFailure) as speech_error:
            asyncio.run(speech.synthesizer("result"))
        self.assertEqual(speech_error.exception.reason_code, "UPSTREAM_UNAVAILABLE")

    def test_closed_voice_stream_rejects_late_audio(self) -> None:
        recognizer = self.make_providers().recognizer

        async def scenario() -> None:
            stream = await recognizer.open_stream(lambda _text: asyncio.sleep(0))
            await stream.close()
            await stream.push_audio(b"\0" * 128)

        with self.assertRaises(InferenceFailure) as raised:
            asyncio.run(scenario())
        self.assertEqual(raised.exception.reason_code, "STREAM_CLOSED")


class FixtureProviderContractTests(ProviderContractMixin, unittest.TestCase):
    expected_mode = "fixture"

    def make_providers(self, scenario: str = "normal") -> InferenceProviders:
        env: dict[str, str] = {}
        if scenario == "voice-unavailable":
            env["DIMON_FIXTURE_VOICE_CASE"] = "nemotron-unavailable"
        if scenario == "speech-unavailable":
            env["DIMON_FIXTURE_SPEECH_CASE"] = "kokoro-unavailable"
        return build_inference_providers("fixture", env)


class LocalFakeExtractor:
    def extract(self, text: str) -> ExtractedIntent:
        if text == MALFORMED_PROMPT:
            raise InvalidModelOutput("fake local adapter returned malformed output")
        if text == TIMEOUT_PROMPT:
            raise InferenceFailure("UPSTREAM_TIMEOUT", "fake local deadline")
        intent = {
            "contract_version": 3,
            "kind": "BENEFICIARY_TRANSFER",
            "source_account_ref": "savings",
            "source_evidence": "savings",
            "recipient_label": "John Smith",
            "recipient_evidence": "John Smith",
            "currency": "SGD",
            "payment_reference": None,
            "reference_evidence": None,
            "requested_execution_date": None,
        }
        return ExtractedIntent(intent, json.dumps(intent), 1.0)


class LocalFakeStream:
    def __init__(self, on_partial: Callable[[str], Awaitable[None]]):
        self.on_partial = on_partial
        self.closed = False

    async def push_audio(self, audio: bytes) -> None:
        del audio
        if self.closed:
            raise InferenceFailure("STREAM_CLOSED", "fake local stream is closed")

    async def finish(self) -> str:
        await self.on_partial("Send John")
        return EXACT_PROMPT

    async def close(self) -> None:
        self.closed = True


class LocalFakeRecognizer:
    async def open_stream(
        self,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> LocalFakeStream:
        return LocalFakeStream(on_partial)

    async def transcribe(
        self,
        audio: bytes,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> str:
        stream = await self.open_stream(on_partial)
        await stream.push_audio(audio)
        return await stream.finish()


class LocalFakeLifecycle:
    def __init__(self, unavailable: bool = False):
        self.unavailable = unavailable

    async def acquire(self) -> dict[str, object]:
        if self.unavailable:
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "fake local voice unavailable")
        return {"ok": True}

    async def release(self, *, disconnected: bool = False) -> None:
        del disconnected


class LocalFakeSynthesizer:
    def __init__(self, unavailable: bool = False):
        self.unavailable = unavailable

    async def __call__(self, text: str) -> bytes:
        del text
        if self.unavailable:
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "fake local speech unavailable")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16_000)
            wav.writeframes(struct.pack("<h", 0) * 160)
        return output.getvalue()


class LocalFakeProviderContractTests(ProviderContractMixin, unittest.TestCase):
    expected_mode = "local"

    def make_providers(self, scenario: str = "normal") -> InferenceProviders:
        return InferenceProviders(
            mode="local",
            extractor=LocalFakeExtractor(),
            recognizer=LocalFakeRecognizer(),
            voice_lifecycle=LocalFakeLifecycle(scenario == "voice-unavailable"),
            synthesizer=LocalFakeSynthesizer(scenario == "speech-unavailable"),
        )


if __name__ == "__main__":
    unittest.main()

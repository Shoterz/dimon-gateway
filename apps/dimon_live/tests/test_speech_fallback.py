import asyncio
import json
import unittest
from unittest.mock import patch

from websockets.asyncio.server import serve

from apps.dimon_live.intel_voice import WhisperClient
from apps.dimon_live.model_adapter import InferenceFailure
from apps.dimon_live.provider_factory import build_inference_providers
from apps.dimon_live.speech import result_summary
from apps.dimon_live.speech_fallback import (
    PrimaryFallbackRecognizer,
    PrimaryFallbackSynthesizer,
    is_provider_unavailable,
)
from apps.dimon_live.voice import VoiceError, VoiceLimitError
from apps.dimon_live.tencent_tts import read_back_synthesizer_from_env


async def ignore_partial(_text: str) -> None:
    return None


class FakeRecognizer:
    def __init__(self, *, transcript="primary", failure=None, stream=None):
        self.transcript = transcript
        self.failure = failure
        self.stream = stream
        self.calls = []

    async def transcribe(self, audio, on_partial):
        self.calls.append(bytes(audio))
        if self.failure is not None:
            raise self.failure
        await on_partial(self.transcript)
        return self.transcript

    async def open_stream(self, on_partial):
        if self.failure is not None:
            raise self.failure
        if self.stream is None:
            self.stream = FakeStream(on_partial=on_partial, transcript=self.transcript)
        return self.stream


class FakeStream:
    def __init__(self, *, on_partial=ignore_partial, transcript="primary", fail_on_push=None):
        self.on_partial = on_partial
        self.transcript = transcript
        self.fail_on_push = fail_on_push
        self.frames = []
        self.closed = False

    async def push_audio(self, audio):
        self.frames.append(bytes(audio))
        if self.fail_on_push is not None and len(self.frames) == self.fail_on_push:
            raise InferenceFailure("UPSTREAM_UNAVAILABLE", "primary stream disconnected")

    async def finish(self):
        await self.on_partial(self.transcript)
        return self.transcript

    async def close(self):
        self.closed = True


class FakeSynthesizer:
    def __init__(self, *, data=b"wav", failure=None):
        self.data = data
        self.failure = failure
        self.calls = []
        self.result_calls = []

    async def __call__(self, text):
        self.calls.append(text)
        if self.failure is not None:
            raise self.failure
        return self.data

    async def synthesize_result(self, result):
        self.result_calls.append(result)
        if self.failure is not None:
            raise self.failure
        return self.data


class SpeechFallbackTests(unittest.TestCase):
    def test_actual_whisper_refusal_never_calls_backup(self):
        backup = FakeRecognizer(transcript="from trtc")

        async def handler(websocket):
            await websocket.send(json.dumps({"type": "ready", "sample_rate": 16000}))
            await websocket.recv()
            await websocket.recv()
            await websocket.send(
                json.dumps({"type": "error", "reason_code": "AUDIO_LIMIT_EXCEEDED"})
            )

        async def scenario():
            server = await serve(handler, "127.0.0.1", 0)
            address = server.sockets[0].getsockname()
            primary = WhisperClient(f"ws://{address[0]}:{address[1]}")
            recognizer = PrimaryFallbackRecognizer(primary, backup, max_audio_bytes=64)
            try:
                await recognizer.transcribe(b"\x00\x00\x00\x00", ignore_partial)
            finally:
                server.close()
                await server.wait_closed()

        with self.assertRaises(VoiceError) as raised:
            asyncio.run(scenario())
        self.assertEqual(raised.exception.reason_code, "AUDIO_LIMIT_EXCEEDED")
        self.assertEqual(backup.calls, [])

    def test_actual_whisper_engine_outage_calls_backup_once(self):
        backup = FakeRecognizer(transcript="from trtc")

        async def handler(websocket):
            await websocket.send(json.dumps({"type": "ready", "sample_rate": 16000}))
            await websocket.recv()
            await websocket.recv()
            await websocket.send(
                json.dumps({"type": "error", "reason_code": "TRANSCRIPTION_UNAVAILABLE"})
            )

        async def scenario():
            server = await serve(handler, "127.0.0.1", 0)
            address = server.sockets[0].getsockname()
            primary = WhisperClient(f"ws://{address[0]}:{address[1]}")
            recognizer = PrimaryFallbackRecognizer(primary, backup, max_audio_bytes=64)
            try:
                return await recognizer.transcribe(b"\x00\x00\x00\x00", ignore_partial)
            finally:
                server.close()
                await server.wait_closed()

        self.assertEqual(asyncio.run(scenario()), "from trtc")
        self.assertEqual(backup.calls, [b"\x00\x00\x00\x00"])

    def test_primary_recognition_success_never_calls_backup(self):
        primary = FakeRecognizer(transcript="from whisper")
        backup = FakeRecognizer(transcript="from trtc")
        recognizer = PrimaryFallbackRecognizer(primary, backup, max_audio_bytes=64)

        transcript = asyncio.run(recognizer.transcribe(b"audio", ignore_partial))

        self.assertEqual(transcript, "from whisper")
        self.assertEqual(primary.calls, [b"audio"])
        self.assertEqual(backup.calls, [])

    def test_allowed_recognition_failures_call_backup_once(self):
        for reason in ("UPSTREAM_TIMEOUT", "UPSTREAM_UNAVAILABLE", "WHISPER_UNAVAILABLE"):
            with self.subTest(reason=reason):
                primary = FakeRecognizer(failure=InferenceFailure(reason, "offline"))
                backup = FakeRecognizer(transcript="from trtc")
                recognizer = PrimaryFallbackRecognizer(primary, backup, max_audio_bytes=64)

                self.assertEqual(
                    asyncio.run(recognizer.transcribe(b"audio", ignore_partial)),
                    "from trtc",
                )
                self.assertEqual(backup.calls, [b"audio"])

    def test_refusal_failures_do_not_contact_backup(self):
        for reason in (
            "INVALID_INFERENCE_REQUEST",
            "AUDIO_LIMIT_EXCEEDED",
            "MALFORMED_AUDIO",
            "POLICY_REJECTED",
            "AUTHENTICATION_FAILED",
            "SCHEMA_INVALID",
        ):
            with self.subTest(reason=reason):
                failure = InferenceFailure(reason, "refused")
                primary = FakeRecognizer(failure=failure)
                backup = FakeRecognizer(transcript="from trtc")
                recognizer = PrimaryFallbackRecognizer(primary, backup, max_audio_bytes=64)

                with self.assertRaises(InferenceFailure) as raised:
                    asyncio.run(recognizer.transcribe(b"audio", ignore_partial))
                self.assertIs(raised.exception, failure)
                self.assertEqual(backup.calls, [])

    def test_midstream_primary_failure_replays_bounded_audio_once(self):
        primary_stream = FakeStream(transcript="unused", fail_on_push=2)
        primary = FakeRecognizer(stream=primary_stream)
        backup = FakeRecognizer(transcript="from trtc")
        recognizer = PrimaryFallbackRecognizer(primary, backup, max_audio_bytes=8)

        async def run():
            stream = await recognizer.open_stream(ignore_partial)
            await stream.push_audio(b"one")
            await stream.push_audio(b"two")
            return await stream.finish()

        self.assertEqual(asyncio.run(run()), "from trtc")
        self.assertEqual(backup.stream.frames, [b"onetwo"])
        self.assertTrue(primary_stream.closed)

    def test_stream_buffer_limit_is_not_an_availability_fallback(self):
        primary = FakeRecognizer(stream=FakeStream())
        backup = FakeRecognizer(transcript="from trtc")
        recognizer = PrimaryFallbackRecognizer(primary, backup, max_audio_bytes=3)

        async def run():
            stream = await recognizer.open_stream(ignore_partial)
            await stream.push_audio(b"four")

        with self.assertRaises(VoiceLimitError) as raised:
            asyncio.run(run())
        self.assertEqual(raised.exception.reason_code, "AUDIO_LIMIT_EXCEEDED")
        self.assertIsNone(backup.stream)

    def test_primary_and_backup_failure_preserves_primary_reason(self):
        primary_failure = InferenceFailure("UPSTREAM_TIMEOUT", "primary timeout")
        primary = FakeRecognizer(failure=primary_failure)
        backup = FakeRecognizer(failure=VoiceError("TRTC_ASR_UNAVAILABLE"))
        recognizer = PrimaryFallbackRecognizer(primary, backup, max_audio_bytes=64)

        with self.assertRaises(InferenceFailure) as raised:
            asyncio.run(recognizer.transcribe(b"audio", ignore_partial))
        self.assertIs(raised.exception, primary_failure)
        self.assertEqual(backup.calls, [b"audio"])

    def test_synthesis_is_primary_first_and_falls_back_only_for_availability(self):
        primary = FakeSynthesizer(data=b"kokoro")
        backup = FakeSynthesizer(data=b"trtc")
        synth = PrimaryFallbackSynthesizer(primary, backup)
        self.assertEqual(asyncio.run(synth("hello")), b"kokoro")
        self.assertEqual(backup.calls, [])

        primary.failure = InferenceFailure("UPSTREAM_TIMEOUT", "timeout")
        self.assertEqual(asyncio.run(synth("retry")), b"trtc")
        self.assertEqual(backup.calls, ["retry"])

    def test_structured_result_uses_primary_contract_and_bounded_backup_text(self):
        failure = InferenceFailure("UPSTREAM_UNAVAILABLE", "offline")
        primary = FakeSynthesizer(failure=failure)
        backup = FakeSynthesizer(data=b"trtc")
        synth = PrimaryFallbackSynthesizer(primary, backup)
        result = {"outcome": "ANSWERED", "reason_code": "GREETING"}

        self.assertEqual(asyncio.run(synth.synthesize_result(result)), b"trtc")
        self.assertEqual(primary.result_calls, [result])
        self.assertEqual(backup.calls, [result_summary(result)])
        self.assertEqual(backup.result_calls, [])

    def test_invalid_synthesis_request_never_calls_backup(self):
        failure = InferenceFailure("INVALID_INFERENCE_REQUEST", "invalid")
        primary = FakeSynthesizer(failure=failure)
        backup = FakeSynthesizer(data=b"trtc")
        synth = PrimaryFallbackSynthesizer(primary, backup)

        with self.assertRaises(InferenceFailure) as raised:
            asyncio.run(synth("unsafe"))
        self.assertIs(raised.exception, failure)
        self.assertEqual(backup.calls, [])

    def test_primary_and_backup_synthesis_failure_preserves_primary_reason(self):
        primary_failure = InferenceFailure("UPSTREAM_UNAVAILABLE", "gateway offline")
        primary = FakeSynthesizer(failure=primary_failure)
        backup = FakeSynthesizer(failure=VoiceError("TRTC_TTS_UNAVAILABLE"))
        synth = PrimaryFallbackSynthesizer(primary, backup)

        with self.assertRaises(InferenceFailure) as raised:
            asyncio.run(synth("hello"))
        self.assertIs(raised.exception, primary_failure)
        self.assertEqual(backup.calls, ["hello"])

    def test_fixture_mode_never_builds_or_wraps_trtc(self):
        env = {
            "DIMON_TRTC_SDK_APP_ID": "1400000001",
            "DIMON_TRTC_SDK_SECRET_KEY": "sdk-secret",
            "DIMON_TRTC_SECRET_ID": "cloud-id",
            "DIMON_TRTC_SECRET_KEY": "cloud-secret",
        }
        with (
            patch("apps.dimon_live.provider_factory.trtc.asr_from_env") as asr_factory,
            patch("apps.dimon_live.provider_factory.trtc.tts_from_env") as tts_factory,
        ):
            providers = build_inference_providers("fixture", env)

        asr_factory.assert_not_called()
        tts_factory.assert_not_called()
        self.assertNotIsInstance(providers.recognizer, PrimaryFallbackRecognizer)
        self.assertNotIsInstance(providers.synthesizer, PrimaryFallbackSynthesizer)

    def test_local_defaults_to_whisper_cpp_and_kokoro_without_backup_credentials(self):
        providers = build_inference_providers("local", {})

        self.assertEqual(providers.recognizer.url, "ws://127.0.0.1:8006/v1/stream")
        self.assertEqual(providers.synthesizer.base_url, "http://127.0.0.1:3001")

    def test_non_fixture_providers_wrap_complete_trtc_backups(self):
        backup_recognizer = FakeRecognizer(transcript="from trtc")
        backup_synthesizer = FakeSynthesizer(data=b"trtc")
        env = {
            "DIMON_TRTC_SDK_APP_ID": "1400000001",
            "DIMON_TRTC_SDK_SECRET_KEY": "sdk-secret",
            "DIMON_TRTC_SECRET_ID": "cloud-id",
            "DIMON_TRTC_SECRET_KEY": "cloud-secret",
            "DIMON_MAX_AUDIO_BYTES": "1234",
        }
        with (
            patch(
                "apps.dimon_live.provider_factory.trtc.asr_from_env",
                return_value=backup_recognizer,
            ) as asr_factory,
            patch(
                "apps.dimon_live.provider_factory.trtc.tts_from_env",
                return_value=backup_synthesizer,
            ) as tts_factory,
        ):
            providers = build_inference_providers("local", env)

        asr_factory.assert_called_once_with(env)
        tts_factory.assert_called_once_with(env)
        self.assertIsInstance(providers.recognizer, PrimaryFallbackRecognizer)
        self.assertEqual(providers.recognizer.primary.url, "ws://127.0.0.1:8006/v1/stream")
        self.assertIs(providers.recognizer.backup, backup_recognizer)
        self.assertEqual(providers.recognizer.max_audio_bytes, 1234)
        self.assertIsInstance(providers.synthesizer, PrimaryFallbackSynthesizer)
        self.assertEqual(providers.synthesizer.primary.base_url, "http://127.0.0.1:3001")
        self.assertIs(providers.synthesizer.backup, backup_synthesizer)

    def test_remote_gateway_remains_primary_when_trtc_backup_is_configured(self):
        backup_recognizer = FakeRecognizer(transcript="from trtc")
        backup_synthesizer = FakeSynthesizer(data=b"trtc")
        env = {
            "DIMON_INFERENCE_GATEWAY_URL": "https://gateway.example.test",
            "DIMON_INFERENCE_SERVICE_KEY": "shared-test-key-0123456789abcdef",
            "DIMON_TRTC_SDK_APP_ID": "1400000001",
            "DIMON_TRTC_SDK_SECRET_KEY": "sdk-secret",
            "DIMON_TRTC_SECRET_ID": "cloud-id",
            "DIMON_TRTC_SECRET_KEY": "cloud-secret",
        }
        with (
            patch(
                "apps.dimon_live.provider_factory.trtc.asr_from_env",
                return_value=backup_recognizer,
            ),
            patch(
                "apps.dimon_live.provider_factory.trtc.tts_from_env",
                return_value=backup_synthesizer,
            ),
        ):
            providers = build_inference_providers("remote", env)

        self.assertIsInstance(providers.recognizer, PrimaryFallbackRecognizer)
        self.assertEqual(
            providers.recognizer.primary.client.config.base_url,
            "https://gateway.example.test",
        )
        self.assertIsInstance(providers.synthesizer, PrimaryFallbackSynthesizer)
        self.assertIs(providers.synthesizer.backup, backup_synthesizer)

    def test_partial_trtc_credentials_do_not_create_a_backup(self):
        with (
            patch("apps.dimon_live.provider_factory.trtc.asr_from_env") as asr_factory,
            patch("apps.dimon_live.provider_factory.trtc.tts_from_env") as tts_factory,
        ):
            providers = build_inference_providers(
                "local",
                {"DIMON_TRTC_SDK_APP_ID": "1400000001"},
            )

        asr_factory.assert_not_called()
        tts_factory.assert_not_called()
        self.assertNotIsInstance(providers.recognizer, PrimaryFallbackRecognizer)
        self.assertNotIsInstance(providers.synthesizer, PrimaryFallbackSynthesizer)

    def test_legacy_tts_selectors_cannot_reverse_the_primary_order(self):
        primary = FakeSynthesizer(data=b"kokoro")

        for provider in ("trtc", "tencent"):
            with self.subTest(provider=provider):
                selected = read_back_synthesizer_from_env(
                    {"DIMON_TTS_PROVIDER": provider},
                    primary,
                )
                self.assertIs(selected, primary)

    def test_availability_classifier_is_explicit(self):
        self.assertTrue(is_provider_unavailable(InferenceFailure("UPSTREAM_TIMEOUT", "timeout")))
        self.assertTrue(is_provider_unavailable(VoiceError("WHISPER_UNAVAILABLE")))
        self.assertFalse(is_provider_unavailable(InferenceFailure("INVALID_INFERENCE_REQUEST", "bad")))


if __name__ == "__main__":
    unittest.main()

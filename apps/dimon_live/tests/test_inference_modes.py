from __future__ import annotations

import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from apps.dimon_live.database import Database
from apps.dimon_live.inference import InferenceProviders, parse_inference_mode
from apps.dimon_live.model_adapter import FixtureNoMatch
from apps.dimon_live.provider_factory import (
    ProviderConfigurationError,
    build_inference_providers,
)
from apps.dimon_live.service import ActionService


class StubExtractor:
    def extract(self, text: str):
        raise AssertionError("not called")


class StubRecognizer:
    async def open_stream(self, on_partial):
        raise AssertionError("not called")

    async def transcribe(self, audio, on_partial):
        raise AssertionError("not called")


class StubLifecycle:
    async def acquire(self):
        return {"ok": True}

    async def release(self, *, disconnected: bool = False):
        return None


async def stub_synthesizer(text: str) -> bytes:
    return b""


class InferenceModeTests(unittest.TestCase):
    def test_parse_accepts_the_three_public_modes(self) -> None:
        self.assertEqual(
            [parse_inference_mode(value) for value in ("fixture", "remote", "local")],
            ["fixture", "remote", "local"],
        )

    def test_parse_rejects_an_unknown_mode(self) -> None:
        with self.assertRaises(ProviderConfigurationError) as raised:
            parse_inference_mode("automatic")

        self.assertEqual(raised.exception.reason_code, "INVALID_INFERENCE_MODE")

    def test_local_factory_builds_all_current_adapters(self) -> None:
        providers = build_inference_providers("local", {})

        self.assertEqual(providers.mode, "local")
        self.assertEqual(providers.extractor.api_base, "http://127.0.0.1:8080/v1")
        self.assertEqual(providers.recognizer.url, "ws://127.0.0.1:8004/v1/realtime")
        self.assertEqual(providers.synthesizer.base_url, "http://127.0.0.1:3001")

    def test_local_factory_rejects_an_explicitly_empty_endpoint(self) -> None:
        with self.assertRaises(ProviderConfigurationError) as raised:
            build_inference_providers("local", {"DIMON_QWEN_API_BASE": ""})

        self.assertEqual(raised.exception.reason_code, "INVALID_LOCAL_INFERENCE_CONFIG")

    def test_remote_factory_requires_service_token_configuration(self) -> None:
        with self.assertRaises(ProviderConfigurationError) as raised:
            build_inference_providers("remote", {})
        self.assertEqual(raised.exception.reason_code, "INVALID_REMOTE_INFERENCE_CONFIG")

    def test_remote_factory_builds_gateway_backed_providers(self) -> None:
        providers = build_inference_providers(
            "remote",
            {
                "DIMON_INFERENCE_GATEWAY_URL": "https://gateway.example.test",
                "DIMON_CF_ACCESS_CLIENT_ID": "client-id",
                "DIMON_CF_ACCESS_CLIENT_SECRET": "client-secret",
            },
        )

        self.assertEqual(providers.mode, "remote")
        self.assertIsNone(providers.voice_lifecycle)
        self.assertEqual(
            providers.extractor.client,
            providers.recognizer.client,
        )
        self.assertEqual(
            providers.extractor.client,
            providers.synthesizer.client,
        )

    def test_provider_bundle_is_immutable(self) -> None:
        providers = InferenceProviders(
            mode="fixture",
            extractor=StubExtractor(),
            recognizer=StubRecognizer(),
            voice_lifecycle=StubLifecycle(),
            synthesizer=stub_synthesizer,
        )

        with self.assertRaises(FrozenInstanceError):
            providers.mode = "local"  # type: ignore[misc]

    def test_create_app_rejects_bundle_and_individual_injection(self) -> None:
        from apps.dimon_live.app import create_app

        providers = InferenceProviders(
            mode="fixture",
            extractor=StubExtractor(),
            recognizer=StubRecognizer(),
            voice_lifecycle=StubLifecycle(),
            synthesizer=stub_synthesizer,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "providers"):
                create_app(
                    Path(temp_dir) / "demo.sqlite3",
                    providers=providers,
                    extractor=StubExtractor(),
                )

    def test_action_service_returns_stable_failure_without_exception_text(self) -> None:
        class MissingFixtureExtractor:
            def extract(self, text: str):
                raise FixtureNoMatch("private fixture diagnostic")

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "demo.sqlite3")
            database.initialize()
            result = ActionService(database, MissingFixtureExtractor()).process(
                "00000000-0000-4000-8000-000000000077",
                "unknown fixture prompt",
            )

        self.assertEqual(result["reason_code"], "FIXTURE_NO_MATCH")
        self.assertNotIn("detail", result)
        self.assertNotIn("private fixture diagnostic", str(result))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx

from apps.dimon_live.inference import InferenceProviders
from apps.dimon_live.tests.test_demo_preparation_api import (
    FakePreparer,
    StubExtractor,
    StubLifecycle,
    StubRecognizer,
    StubSynthesizer,
    ready_snapshot,
    snapshot,
)
from apps.dimon_live.voice_trace import VoiceTraceStore


class VoiceDiagnosticsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from apps.dimon_live.app import create_app

        self.create_app = create_app
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def app(self, trace_store, preparation=None):
        environment = {
            "DIMON_DEMO_PREPARE_ENABLED": "1",
            "DIMON_INFERENCE_MODE": "local",
            "DIMON_PORT": "8890",
        }
        providers = InferenceProviders(
            mode="local",
            extractor=StubExtractor(),
            recognizer=StubRecognizer(),
            voice_lifecycle=StubLifecycle(),
            synthesizer=StubSynthesizer(),
        )
        with patch.dict(os.environ, environment, clear=False):
            return self.create_app(
                providers=providers,
                owner_path=self.root / "owner.sqlite3",
                visitor_dir=self.root / "visitors",
                allowed_origins=set(),
                demo_preparer=preparation or FakePreparer(current=snapshot()),
                voice_trace_store=trace_store,
            )

    @staticmethod
    def request(app):
        async def send():
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8890",
            ) as client:
                return await client.get(
                    "/api/voice/diagnostics/latest",
                    headers={"host": "127.0.0.1:8890"},
                )

        return asyncio.run(send())

    def test_empty_state_is_bounded_and_never_cacheable(self) -> None:
        response = self.request(self.app(VoiceTraceStore()))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), {"trace": {}, "workers": [], "gpu": None})

    def test_partial_trace_keeps_only_allowlisted_timing_fields(self) -> None:
        store = VoiceTraceStore()
        trace_id = str(uuid4())
        store.start(trace_id)
        store.record_browser(trace_id, "d_click", 0)
        store.record_browser(trace_id, "audio_ready", 8.125)

        body = self.request(self.app(store)).json()

        self.assertEqual(set(body), {"trace", "workers", "gpu"})
        self.assertEqual(body["trace"]["trace_id"], trace_id)
        self.assertEqual(body["trace"]["attempts"], [])
        self.assertEqual(
            [event["name"] for event in body["trace"]["events"]],
            ["d_click", "audio_ready"],
        )

    def test_cuda_fallback_reason_and_four_loaded_workers_are_exposed(self) -> None:
        store = VoiceTraceStore()
        trace_id = str(uuid4())
        attempt_id = str(uuid4())
        store.start(trace_id)
        store.start_attempt(trace_id, attempt_id)
        store.record_server(trace_id, "speech_detected", attempt_id=attempt_id)
        store.finish_attempt(
            trace_id,
            attempt_id,
            path="faster_whisper_final",
            failure_phase="nemotron_connect",
        )

        body = self.request(
            self.app(store, FakePreparer(current=ready_snapshot()))
        ).json()

        self.assertEqual(body["trace"]["attempts"], [{
            "attempt_id": attempt_id,
            "path": "faster_whisper_final",
            "failure_phase": "nemotron_connect",
        }])
        self.assertEqual(
            [worker["name"] for worker in body["workers"]],
            ["qwen", "nemotron", "kokoro", "faster_whisper"],
        )
        self.assertTrue(all(worker["loaded"] for worker in body["workers"]))
        self.assertEqual(body["gpu"], {
            "total_mb": 12282,
            "free_mb": 2213,
            "safety_reserve_mb": 512,
        })

    def test_endpoint_resanitizes_malicious_dependencies_recursively(self) -> None:
        class MaliciousTraceStore:
            def latest(self):
                return {
                    "trace_id": str(uuid4()),
                    "started_at": "2026-10-15T01:00:00+00:00",
                    "events": [{
                        "source": "browser",
                        "name": "d_click",
                        "elapsed_ms": 0,
                        "text": "send Mallory money",
                        "socket": "/run/private.sock",
                    } for _index in range(70)],
                    "attempts": [{
                        "attempt_id": str(uuid4()),
                        "path": None,
                        "failure_phase": None,
                    } for _index in range(70)],
                    "secret": "https://private.example/token",
                }

        raw = ready_snapshot(
            secret="private-token",
            socket_path="/run/private.sock",
        )
        raw["workers"][0].update({
            "command": "serve --api-key secret",
            "url": "ws://127.0.0.1/private",
            "filesystem": "/opt/dimon/model.gguf",
        })

        body = self.request(
            self.app(MaliciousTraceStore(), FakePreparer(current=raw))
        ).json()
        encoded = repr(body).lower()
        for forbidden in (
            "mallory", "private-token", "private.sock", "api-key",
            "ws://", "https://", "/opt/", "model.gguf",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(set(body), {"trace", "workers", "gpu"})
        self.assertEqual(
            set(body["trace"]), {"trace_id", "started_at", "events", "attempts"}
        )
        self.assertEqual(
            set(body["trace"]["events"][0]), {"source", "name", "elapsed_ms"}
        )
        self.assertEqual(len(body["trace"]["events"]), 64)
        self.assertEqual(len(body["trace"]["attempts"]), 64)
        expected_worker_keys = {
            "name", "service", "profile", "state", "loaded", "worker_pid",
            "active_jobs", "heartbeat_age_seconds", "budget_mb", "observed_vram_mb",
            "model", "device", "compute_type", "context_tokens",
        }
        self.assertTrue(all(set(worker) == expected_worker_keys for worker in body["workers"]))


if __name__ == "__main__":
    unittest.main()

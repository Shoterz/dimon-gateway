import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from apps.dimon_live.inference import InferenceProviders
from apps.dimon_live.model_adapter import ExtractedIntent


class StubExtractor:
    def extract(self, text):
        return ExtractedIntent({}, "{}", 1.0)


class StubRecognizer:
    async def open_stream(self, on_partial):
        raise AssertionError("not called")

    async def transcribe(self, audio, on_partial):
        raise AssertionError("not called")


class StubLifecycle:
    async def acquire(self):
        raise AssertionError("not called")

    async def release(self, *, disconnected=False):
        raise AssertionError("not called")


class StubSynthesizer:
    def __init__(self, client=None):
        self.client = client

    async def __call__(self, text):
        raise AssertionError("not called")

    async def synthesize_result(self, result):
        raise AssertionError("not called")


def snapshot(state="IDLE", **extra):
    return {
        "state": state,
        "best_effort": True,
        "started_at": None,
        "completed_at": None,
        "ready_until": None,
        "stages": [
            {
                "name": name,
                "state": "PENDING",
                "latency_ms": 0,
                "reason_code": None,
            }
            for name in (
                "capacity",
                "nemotron",
                "qwen",
                "kokoro",
                "faster_whisper",
            )
        ],
        "warning": "Other GPU requests can make prepared services cold again.",
        **extra,
    }


def ready_snapshot(**extra):
    value = snapshot(
        "READY",
        started_at="2099-10-15T01:00:00+00:00",
        completed_at="2099-10-15T01:00:04+00:00",
        ready_until="2099-10-15T01:05:04+00:00",
        workers=[
            {
                "name": "qwen",
                "service": "llama",
                "state": "ready",
                "worker_pid": 101,
                "profile": "qwen35-9b-abliterated-q5-4k",
                "active_jobs": 0,
                "heartbeat_age_seconds": 0.1,
                "budget_mb": 6528,
                "observed_vram_mb": 5852,
                "model": "qwen35-9b-abliterated-q5",
                "device": "cuda",
                "compute_type": "Q5_K_M",
                "context_tokens": 4096,
            },
            {
                "name": "nemotron",
                "service": "nemotron",
                "state": "ready",
                "worker_pid": 102,
                "profile": "nemotron-en",
                "active_jobs": 0,
                "heartbeat_age_seconds": 0.1,
                "budget_mb": 1536,
                "observed_vram_mb": 1196,
            },
            {
                "name": "kokoro",
                "service": "kokoro",
                "state": "ready",
                "worker_pid": 103,
                "profile": "kokoro-82m",
                "active_jobs": 0,
                "heartbeat_age_seconds": 0.1,
                "budget_mb": 1280,
                "observed_vram_mb": 860,
            },
            {
                "name": "faster_whisper",
                "service": "faster_whisper",
                "state": "ready",
                "worker_pid": 104,
                "profile": "faster-whisper-medium-cuda",
                "active_jobs": 0,
                "heartbeat_age_seconds": 0.1,
                "budget_mb": 1664,
                "observed_vram_mb": 1324,
                "model": "medium",
                "device": "cuda",
                "compute_type": "int8_float16",
            },
        ],
        gpu={"total_mb": 12282, "free_mb": 2213, "safety_reserve_mb": 512},
    )
    for stage in value["stages"]:
        stage.update(state="READY", latency_ms=12, reason_code="READY")
    value.update(extra)
    return value


class FakePreparer:
    def __init__(self, *, current=None, started=None):
        self.current = current or snapshot()
        self.started = started or snapshot("PREPARING", started_at="2026-10-03T01:02:03+00:00")
        self.start_calls = 0
        self.status_calls = 0

    async def start(self):
        self.start_calls += 1
        return self.started

    async def status(self):
        self.status_calls += 1
        return self.current


class FakeRemotePreparationClient:
    def __init__(self):
        self.calls: list[bool] = []

    async def preparation(self, *, start=False):
        self.calls.append(start)
        return snapshot("PREPARING" if start else "IDLE")


class DemoPreparationApiTests(unittest.TestCase):
    def setUp(self):
        from apps.dimon_live.app import create_app

        self.create_app = create_app
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def providers(self, mode="local", *, remote_client=None):
        return InferenceProviders(
            mode=mode,
            extractor=StubExtractor(),
            recognizer=StubRecognizer(),
            voice_lifecycle=StubLifecycle(),
            synthesizer=StubSynthesizer(remote_client),
        )

    def app(self, preparer, *, enabled="1", mode="local", remote_client=None):
        environment = {
            "DIMON_DEMO_PREPARE_ENABLED": enabled,
            "DIMON_INFERENCE_MODE": mode,
            "DIMON_PORT": "8890",
        }
        with patch.dict(os.environ, environment, clear=False):
            return self.create_app(
                providers=self.providers(mode, remote_client=remote_client),
                owner_path=self.root / f"owner-{enabled}-{mode}.sqlite3",
                visitor_dir=self.root / f"visitors-{enabled}-{mode}",
                allowed_origins=set(),
                demo_preparer=preparer,
            )

    @staticmethod
    def request(
        app,
        method,
        path,
        *,
        host="127.0.0.1:8890",
        origin=None,
        client_host="127.0.0.1",
        **kwargs,
    ):
        async def send():
            headers = dict(kwargs.pop("headers", {}))
            headers["host"] = host
            if origin is not None:
                headers["origin"] = origin
            transport = httpx.ASGITransport(app=app, client=(client_host, 50000))
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8890",
            ) as client:
                return await client.request(method, path, headers=headers, **kwargs)

        return asyncio.run(send())

    def test_prepare_routes_are_absent_when_disabled_or_not_local_mode(self):
        for enabled, mode in (("0", "local"), ("1", "fixture")):
            with self.subTest(enabled=enabled, mode=mode):
                app = self.app(FakePreparer(), enabled=enabled, mode=mode)
                get_response = self.request(app, "GET", "/api/demo/prepare")
                post_response = self.request(
                    app, "POST", "/api/demo/prepare", json={}
                )
                self.assertEqual(get_response.status_code, 404)
                self.assertEqual(post_response.status_code, 404)

    def test_enabled_local_preparation_starts_in_background_and_stops_on_shutdown(self):
        class BlockingPreparer(FakePreparer):
            def __init__(self):
                super().__init__()
                self.entered = asyncio.Event()
                self.release = asyncio.Event()
                self.stop_calls = 0

            async def start(self):
                self.start_calls += 1
                self.entered.set()
                await self.release.wait()
                return self.started

            async def stop(self):
                self.stop_calls += 1

        preparer = BlockingPreparer()
        app = self.app(preparer)

        async def exercise():
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(preparer.entered.wait(), timeout=0.1)
                self.assertEqual(preparer.start_calls, 1)
                self.assertFalse(preparer.release.is_set())

        asyncio.run(exercise())
        self.assertEqual(preparer.stop_calls, 1)

    def test_disabled_or_fixture_preparation_does_not_autostart(self):
        async def check(preparer, *, enabled, mode):
            app = self.app(preparer, enabled=enabled, mode=mode)
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)
                self.assertIsNone(app.state.demo_preparation_start_task)

        for enabled, mode in (("0", "local"), ("1", "fixture")):
            with self.subTest(enabled=enabled, mode=mode):
                preparer = FakePreparer()
                asyncio.run(check(preparer, enabled=enabled, mode=mode))
                self.assertEqual(preparer.start_calls, 0)

    def test_prepare_routes_reject_public_host_and_nonmatching_origin(self):
        app = self.app(FakePreparer())

        forged = self.request(
            app,
            "GET",
            "/api/demo/prepare",
            host="public.example",
            headers={"x-forwarded-host": "127.0.0.1:8890"},
        )
        mismatched_origin = self.request(
            app,
            "GET",
            "/api/demo/prepare",
            host="127.0.0.1:8890",
            origin="http://localhost:8890",
        )
        wrong_port = self.request(
            app,
            "GET",
            "/api/demo/prepare",
            host="127.0.0.1:9999",
        )

        self.assertEqual(forged.status_code, 404)
        self.assertEqual(mismatched_origin.status_code, 404)
        self.assertEqual(wrong_port.status_code, 404)
        self.assertNotIn("DEMO_PREPARATION", forged.text)

    def test_remote_prepare_proxy_is_local_operator_only(self):
        remote = FakeRemotePreparationClient()
        app = self.app(None, mode="remote", remote_client=remote)

        blocked_get = self.request(
            app, "GET", "/api/demo/prepare", host="public.example"
        )
        blocked_post = self.request(
            app, "POST", "/api/demo/prepare", host="public.example", json={}
        )
        spoofed_loopback_host = self.request(
            app,
            "POST",
            "/api/demo/prepare",
            host="127.0.0.1:8890",
            client_host="203.0.113.8",
            json={},
        )
        allowed = self.request(
            app,
            "POST",
            "/api/demo/prepare",
            origin="http://127.0.0.1:8890",
            json={},
        )

        self.assertEqual(blocked_get.status_code, 404)
        self.assertEqual(blocked_post.status_code, 404)
        self.assertEqual(spoofed_loopback_host.status_code, 404)
        self.assertEqual(allowed.status_code, 202)
        self.assertEqual(remote.calls, [True])

    def test_prepare_routes_accept_ipv4_localhost_and_ipv6_loopback_with_ports(self):
        for host, origin in (
            ("127.0.0.1:8890", "http://127.0.0.1:8890"),
            ("localhost:8890", "http://localhost:8890"),
            ("[::1]:8890", "http://[::1]:8890"),
        ):
            with self.subTest(host=host):
                preparer = FakePreparer()
                app = self.app(preparer)
                response = self.request(
                    app,
                    "GET",
                    "/api/demo/prepare",
                    host=host,
                    origin=origin,
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["state"], "IDLE")
                self.assertNotIn("set-cookie", response.headers)

    def test_prepare_post_starts_one_run_and_returns_202_while_active(self):
        preparer = FakePreparer()
        app = self.app(preparer)

        response = self.request(
            app,
            "POST",
            "/api/demo/prepare",
            origin="http://127.0.0.1:8890",
            json={},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["state"], "PREPARING")
        self.assertEqual(preparer.start_calls, 1)
        self.assertNotIn("set-cookie", response.headers)

    def test_prepare_status_never_exposes_internal_exception_or_socket_path(self):
        private_snapshot = snapshot(
            "FAILED",
            internal_exception="token=secret",
            socket_path="/run/user/1000/gpu-overlord.sock",
        )
        private_snapshot["stages"][0]["reason_code"] = "/private/reason"
        preparer = FakePreparer(current=private_snapshot)
        app = self.app(preparer)

        response = self.request(app, "GET", "/api/demo/prepare")
        encoded = json.dumps(response.json())

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("socket", encoded)
        self.assertNotIn("private", encoded)
        self.assertEqual(
            set(response.json()),
            {
                "state",
                "reason_code",
                "best_effort",
                "started_at",
                "completed_at",
                "ready_until",
                "stages",
                "workers",
                "gpu",
                "warning",
            },
        )

    def test_ready_requires_five_ready_stages_and_four_verified_workers(self):
        app = self.app(FakePreparer(current=ready_snapshot()))

        response = self.request(app, "GET", "/api/demo/prepare")
        payload = response.json()

        self.assertEqual(payload["state"], "READY")
        self.assertFalse(payload["best_effort"])
        self.assertEqual(
            [stage["name"] for stage in payload["stages"]],
            ["capacity", "nemotron", "qwen", "kokoro", "faster_whisper"],
        )
        self.assertEqual({worker["name"] for worker in payload["workers"]}, {
            "qwen", "nemotron", "kokoro", "faster_whisper",
        })
        self.assertTrue(all(worker["loaded"] for worker in payload["workers"]))
        self.assertEqual(payload["gpu"]["free_mb"], 2213)

    def test_claimed_ready_fails_closed_for_missing_medium_or_pending_capacity(self):
        missing_medium = ready_snapshot()
        missing_medium["stages"] = missing_medium["stages"][:-1]
        pending_capacity = ready_snapshot()
        pending_capacity["stages"][0]["state"] = "PENDING"

        for raw in (missing_medium, pending_capacity):
            with self.subTest(raw=raw["stages"]):
                payload = self.request(
                    self.app(FakePreparer(current=raw)),
                    "GET",
                    "/api/demo/prepare",
                ).json()
                self.assertEqual(payload["state"], "DEGRADED")
                self.assertFalse(all(
                    stage["state"] == "READY" for stage in payload["stages"]
                ))

    def test_expired_ready_snapshot_is_cold_and_clears_residency(self):
        raw = ready_snapshot(ready_until="2020-01-01T00:00:00+00:00")

        payload = self.request(
            self.app(FakePreparer(current=raw)), "GET", "/api/demo/prepare"
        ).json()

        self.assertEqual(payload["state"], "IDLE")
        self.assertTrue(all(stage["state"] == "IDLE" for stage in payload["stages"]))
        self.assertEqual(payload["workers"], [])
        self.assertIsNone(payload["gpu"])

    def test_worker_output_is_allowlisted_and_identity_mismatch_degrades(self):
        raw = ready_snapshot()
        raw["workers"][0].update(
            profile="wrong-profile",
            command="/private/start-qwen --token secret",
            endpoint="http://127.0.0.1:8080",
        )

        payload = self.request(
            self.app(FakePreparer(current=raw)), "GET", "/api/demo/prepare"
        ).json()
        encoded = json.dumps(payload)

        self.assertEqual(payload["state"], "DEGRADED")
        qwen = next(worker for worker in payload["workers"] if worker["name"] == "qwen")
        self.assertFalse(qwen["loaded"])
        self.assertEqual(qwen["profile"], "qwen35-9b-abliterated-q5-4k")
        self.assertNotIn("private", encoded)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("endpoint", encoded)


if __name__ == "__main__":
    unittest.main()

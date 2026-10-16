from __future__ import annotations

import json
import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from services.faster_whisper_cuda.app import build_managed_service, create_frontend_app, main
from services.faster_whisper_cuda.install import RuntimeLayout


class FakeManager:
    def __init__(self) -> None:
        self.loaded = False
        self.active_jobs = 0
        self.events: list[str] = []
        self.fail_begin: Exception | None = None

    def snapshot(self) -> dict[str, object]:
        return {
            "status": "ready" if self.loaded else "idle",
            "ready": self.loaded,
            "loaded": self.loaded,
            "frontend_pid": 11,
            "worker_pid": 22 if self.loaded else None,
            "model": "medium" if self.loaded else None,
            "device": "cuda" if self.loaded else None,
            "compute_type": "int8_float16" if self.loaded else None,
            "active_jobs": self.active_jobs,
            "last_probe_at": 101.0 if self.loaded else None,
        }

    def ensure_loaded(self) -> dict[str, object]:
        self.events.append("warm")
        self.loaded = True
        return self.snapshot()

    def run_probe(self) -> dict[str, object]:
        self.events.append("probe")
        self.loaded = True
        return self.snapshot()

    def begin_job(self) -> dict[str, object]:
        if self.fail_begin:
            raise self.fail_begin
        self.events.append("begin")
        self.loaded = True
        self.active_jobs = 1
        return self.snapshot()

    def end_job(self) -> None:
        self.events.append("end")
        self.active_jobs = 0


class FakeChildWebSocket:
    def __init__(self, *, fail_send: bool = False) -> None:
        self.sent: list[bytes | str] = []
        self.fail_send = fail_send

    async def send(self, value: bytes | str) -> None:
        if self.fail_send:
            raise ConnectionError("child failed with secret content")
        self.sent.append(value)

    async def recv(self) -> str:
        return json.dumps(
            {
                "text": "hello",
                "segments": [{"avg_logprob": -0.2, "no_speech_prob": 0.1}],
            }
        )


class FakeConnection:
    def __init__(self, websocket: FakeChildWebSocket) -> None:
        self.websocket = websocket
        self.closed = False

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, *_args):
        self.closed = True


class RecordingConnector:
    def __init__(self, websocket: FakeChildWebSocket | None = None) -> None:
        self.websocket = websocket or FakeChildWebSocket()
        self.connections: list[FakeConnection] = []

    def __call__(self, url: str, **kwargs):
        self.url = url
        self.kwargs = kwargs
        connection = FakeConnection(self.websocket)
        self.connections.append(connection)
        return connection


class FrontendTests(unittest.TestCase):
    def test_managed_service_preserves_venv_interpreter_path(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        layout = RuntimeLayout.for_repository(repository)

        class FakeClient:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

        module = types.SimpleNamespace(ManagedServiceClient=FakeClient)
        environment = {
            "DIMON_FASTER_WHISPER_PYTHON": str(layout.python),
            "DIMON_FASTER_WHISPER_MODEL_PATH": str(layout.model),
            "DIMON_GPU_OVERLORD_CLIENT_DIR": str(repository),
        }
        with patch.dict(sys.modules, {"gpu_overlord_client": module}):
            manager, _broker = build_managed_service(environment)

        self.assertEqual(manager._worker_command[0], str(layout.python))

    def test_main_registers_shutdown_before_serving(self) -> None:
        manager = Mock()
        broker = Mock()
        captured: dict[str, object] = {}

        def fake_run(app, **kwargs) -> None:
            captured["app"] = app
            captured["kwargs"] = kwargs

        with patch(
            "services.faster_whisper_cuda.app.build_managed_service",
            return_value=(manager, broker),
        ), patch("uvicorn.run", side_effect=fake_run):
            main()

        broker.start.assert_called_once_with()
        app = captured["app"]
        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                pass

        asyncio.run(run_lifespan())
        manager.close.assert_called_once_with()
        broker.close.assert_called_once_with()

    def test_frontend_without_child_is_not_ready(self) -> None:
        manager = FakeManager()
        app = create_frontend_app(manager, websocket_connect=RecordingConnector())

        with TestClient(app) as client:
            body = client.get("/health").json()

        self.assertFalse(body["ready"])
        self.assertFalse(body["loaded"])
        self.assertIsNone(body["worker_pid"])

    def test_warm_and_probe_return_verified_worker_status(self) -> None:
        manager = FakeManager()
        app = create_frontend_app(manager, websocket_connect=RecordingConnector())

        with TestClient(app) as client:
            warm = client.post("/warm").json()
            probe = client.post("/probe").json()

        self.assertTrue(warm["ready"])
        self.assertEqual(warm["worker_pid"], 22)
        self.assertTrue(probe["ready"])
        self.assertEqual(manager.events, ["warm", "probe"])

    def test_websocket_proxy_tracks_one_job_and_preserves_protocol(self) -> None:
        manager = FakeManager()
        connector = RecordingConnector()
        app = create_frontend_app(manager, websocket_connect=connector)
        audio = b"\0\0\0\0"

        with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
            websocket.send_bytes(audio)
            websocket.send_text("done")
            body = websocket.receive_json()

        self.assertEqual(body["text"], "hello")
        self.assertEqual(connector.websocket.sent, [audio, "done"])
        self.assertEqual(manager.events, ["begin", "end"])
        self.assertEqual(manager.active_jobs, 0)
        self.assertTrue(connector.connections[0].closed)

    def test_disconnect_cancel_and_proxy_failure_return_active_jobs_to_zero(self) -> None:
        manager = FakeManager()
        connector = RecordingConnector(FakeChildWebSocket(fail_send=True))
        app = create_frontend_app(manager, websocket_connect=connector)

        with TestClient(app) as client:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_bytes(b"\0\0\0\0")
                websocket.receive()

        self.assertEqual(manager.active_jobs, 0)
        self.assertEqual(manager.events[-1], "end")
        self.assertTrue(connector.connections[0].closed)

        disconnected = FakeManager()
        app = create_frontend_app(disconnected, websocket_connect=RecordingConnector())
        with TestClient(app) as client:
            with client.websocket_connect("/ws"):
                pass
        self.assertEqual(disconnected.active_jobs, 0)
        self.assertEqual(disconnected.events[-1], "end")


if __name__ == "__main__":
    unittest.main()

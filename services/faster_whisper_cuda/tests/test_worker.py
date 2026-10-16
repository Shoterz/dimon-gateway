from __future__ import annotations

import math
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from services.faster_whisper_cuda.config import WorkerSettings
from services.faster_whisper_cuda.protocol import decode_f32le_audio
from services.faster_whisper_cuda.worker import (
    CudaMediumEngine,
    TranscriptionResult,
    TranscriptionSegment,
    WorkerState,
    create_worker_app,
)


@dataclass
class FakeSegment:
    text: str
    avg_logprob: float = -0.25
    no_speech_prob: float = 0.05


class FakeBackend:
    def __init__(
        self,
        *,
        device: str = "cuda",
        compute_type: str = "int8_float16",
        transcript: str = "hello world",
        error: Exception | None = None,
    ) -> None:
        self.model = SimpleNamespace(device=device, compute_type=compute_type)
        self.transcript = transcript
        self.error = error
        self.calls: list[np.ndarray] = []
        self.transcribe_options: list[dict[str, object]] = []

    def transcribe(self, audio: np.ndarray, **kwargs: object):
        self.calls.append(audio.copy())
        self.transcribe_options.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        return iter((FakeSegment(self.transcript),)), object()


class RecordingFactory:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> FakeBackend:
        self.calls.append((args, kwargs))
        return self.backend


class RecordingTranscriber:
    identity = {
        "model": "medium",
        "device": "cuda",
        "compute_type": "int8_float16",
    }

    def __init__(
        self,
        *,
        result: TranscriptionResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or TranscriptionResult(
            text="hello world",
            segments=(TranscriptionSegment(-0.25, 0.05),),
        )
        self.error = error
        self.received: list[np.ndarray] = []
        self.last_probe_at = 123.0

    def transcribe(self, audio: np.ndarray) -> TranscriptionResult:
        self.received.append(audio.copy())
        if self.error is not None:
            raise self.error
        return self.result

    def probe(self) -> None:
        self.last_probe_at += 1


class CudaMediumEngineTests(unittest.TestCase):
    def model_directory(self, root: Path) -> Path:
        model = root / "model"
        model.mkdir()
        (model / "model.bin").write_bytes(b"model")
        return model

    def test_worker_constructs_medium_on_cuda_int8_float16_with_one_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            model = self.model_directory(Path(temp))
            factory = RecordingFactory(FakeBackend())

            engine = CudaMediumEngine(model, model_factory=factory)

        args, kwargs = factory.calls[0]
        self.assertEqual(args, (str(model),))
        self.assertEqual(kwargs["device"], "cuda")
        self.assertEqual(kwargs["compute_type"], "int8_float16")
        self.assertEqual(kwargs["num_workers"], 1)
        self.assertTrue(kwargs["local_files_only"])
        self.assertEqual(engine.identity["model"], "medium")

    def test_health_reports_exact_effective_identity_and_process_pid(self) -> None:
        transcriber = RecordingTranscriber()
        app = create_worker_app(transcriber)

        with TestClient(app) as client:
            body = client.get("/health").json()

        self.assertEqual(
            {key: body[key] for key in ("model", "device", "compute_type")},
            transcriber.identity,
        )
        self.assertEqual(body["pid"], os.getpid())
        self.assertTrue(body["ready"])
        self.assertTrue(body["loaded"])
        self.assertEqual(body["active_jobs"], 0)

    def test_wrong_effective_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            model = self.model_directory(Path(temp))
            for field, value in (
                ("device", "cpu"),
                ("compute_type", "int8"),
            ):
                kwargs = {field: value}
                with self.subTest(field=field), self.assertRaisesRegex(
                    RuntimeError, "WORKER_IDENTITY_MISMATCH"
                ):
                    CudaMediumEngine(
                        model,
                        model_factory=RecordingFactory(FakeBackend(**kwargs)),
                    )

    def test_transcription_guides_financial_and_product_vocabulary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            model = self.model_directory(Path(temp))
            backend = FakeBackend()
            engine = CudaMediumEngine(
                model,
                model_factory=RecordingFactory(backend),
                startup_probe=False,
            )

            engine.transcribe(np.zeros(16_000, dtype=np.float32))

        options = backend.transcribe_options[0]
        self.assertIn("NVIDIA", options["hotwords"])
        self.assertIn("Dimon", options["hotwords"])
        self.assertIn("PayNow", options["hotwords"])


class WorkerProtocolTests(unittest.TestCase):
    def test_websocket_accepts_float32_chunks_and_returns_existing_schema(self) -> None:
        transcriber = RecordingTranscriber()
        app = create_worker_app(transcriber)
        audio = np.asarray([0.0, 0.25, -0.5], dtype="<f4")

        with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
            websocket.send_bytes(audio.tobytes())
            websocket.send_text("done")
            body = websocket.receive_json()

        self.assertEqual(body["text"], "hello world")
        self.assertEqual(
            body["segments"],
            [{"avg_logprob": -0.25, "no_speech_prob": 0.05}],
        )
        np.testing.assert_array_equal(transcriber.received[0], audio)

    def test_malformed_or_oversized_audio_is_rejected_without_transcription(self) -> None:
        for payload, maximum in ((b"abc", 32), (b"\0" * 12, 8)):
            transcriber = RecordingTranscriber()
            app = create_worker_app(transcriber, max_audio_bytes=maximum)
            with self.subTest(payload=len(payload), maximum=maximum), TestClient(
                app
            ) as client, client.websocket_connect("/ws") as websocket:
                websocket.send_bytes(payload)
                websocket.send_text("done")
                with self.assertRaises(WebSocketDisconnect):
                    websocket.receive_json()
            self.assertEqual(transcriber.received, [])

        with self.assertRaisesRegex(ValueError, "non-finite"):
            decode_f32le_audio(np.asarray([math.nan], dtype="<f4").tobytes())

    def test_only_one_transcription_job_is_admitted(self) -> None:
        state = WorkerState()

        self.assertTrue(state.try_begin())
        self.assertFalse(state.try_begin())
        self.assertEqual(state.snapshot()["active_jobs"], 1)
        state.finish()
        self.assertTrue(state.try_begin())
        state.finish()

    def test_success_failure_disconnect_and_cancel_return_active_jobs_to_zero(self) -> None:
        cases = (
            RecordingTranscriber(),
            RecordingTranscriber(error=RuntimeError("secret transcript failed")),
        )
        for transcriber in cases:
            state = WorkerState()
            app = create_worker_app(transcriber, state=state)
            with self.subTest(error=transcriber.error), TestClient(app) as client:
                try:
                    with client.websocket_connect("/ws") as websocket:
                        websocket.send_bytes(np.zeros(160, dtype="<f4").tobytes())
                        websocket.send_text("done")
                        websocket.receive()
                except WebSocketDisconnect:
                    pass
            self.assertEqual(state.snapshot()["active_jobs"], 0)

        disconnected_state = WorkerState()
        app = create_worker_app(RecordingTranscriber(), state=disconnected_state)
        with TestClient(app) as client:
            with client.websocket_connect("/ws"):
                pass
        self.assertEqual(disconnected_state.snapshot()["active_jobs"], 0)

    def test_status_and_logs_never_include_audio_or_transcript(self) -> None:
        secret = "send private payee ten dollars"
        transcriber = RecordingTranscriber(
            result=TranscriptionResult(secret, (TranscriptionSegment(-0.1, 0.0),))
        )
        app = create_worker_app(transcriber)

        with TestClient(app) as client:
            health_before = client.get("/health").text
            with client.websocket_connect("/ws") as websocket:
                websocket.send_bytes(np.zeros(8, dtype="<f4").tobytes())
                websocket.send_text("done")
                self.assertEqual(websocket.receive_json()["text"], secret)
            health_after = client.get("/health").text

        self.assertNotIn(secret, health_before)
        self.assertNotIn(secret, health_after)
        self.assertNotIn("audio", health_after.lower())


class WorkerSettingsTests(unittest.TestCase):
    def test_settings_reject_public_bind_or_wrong_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            model = Path(temp)
            base = {
                "DIMON_FASTER_WHISPER_MODEL_PATH": str(model),
                "DIMON_FASTER_WHISPER_MODEL": "medium",
                "DIMON_FASTER_WHISPER_DEVICE": "cuda",
                "DIMON_FASTER_WHISPER_COMPUTE_TYPE": "int8_float16",
            }
            for key, value in (
                ("DIMON_FASTER_WHISPER_WORKER_HOST", "0.0.0.0"),
                ("DIMON_FASTER_WHISPER_MODEL", "turbo"),
                ("DIMON_FASTER_WHISPER_DEVICE", "cpu"),
                ("DIMON_FASTER_WHISPER_COMPUTE_TYPE", "int8"),
            ):
                environment = dict(base)
                environment[key] = value
                with self.subTest(key=key), self.assertRaises(ValueError):
                    WorkerSettings.from_env(environment)


if __name__ == "__main__":
    unittest.main()

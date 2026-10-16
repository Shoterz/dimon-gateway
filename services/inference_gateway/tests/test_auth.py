from __future__ import annotations

import asyncio
import unittest

import httpx
from fastapi import Request

from services.inference_gateway.app import create_gateway_app
from services.inference_gateway.auth import authenticate_service_key, require_service_key
from services.inference_gateway.cli import main as gateway_main
from services.inference_gateway.config import GatewayConfig
from services.inference_gateway.errors import GatewayError


def valid_env() -> dict[str, str]:
    return {
        "DIMON_GATEWAY_HOST": "127.0.0.1",
        "DIMON_GATEWAY_PORT": "8891",
        "DIMON_INFERENCE_SERVICE_KEY": "shared-test-key-0123456789abcdef",
        "DIMON_QWEN_API_BASE": "http://127.0.0.1:8080/v1",
        "DIMON_QWEN_REVISION": "test-revision",
        "DIMON_NEMOTRON_URL": "ws://127.0.0.1:8004/v1/realtime",
        "DIMON_NEMOTRON_REVISION": "test-nemotron-revision",
        "DIMON_KOKORO_URL": "http://127.0.0.1:3001",
        "DIMON_KOKORO_REVISION": "test-kokoro-revision",
        "DIMON_GPU_OVERLORD_SOCKET": "/run/user/1000/gpu-overlord-1000.sock",
    }


class GatewayConfigTests(unittest.TestCase):
    def test_valid_config_uses_loopback_and_shared_key(self) -> None:
        config = GatewayConfig.from_env(valid_env())

        self.assertEqual(config.bind_host, "127.0.0.1")
        self.assertEqual(config.bind_port, 8891)
        self.assertEqual(config.service_key, "shared-test-key-0123456789abcdef")
        self.assertEqual(
            config.gpu_overlord_socket,
            __import__("pathlib").Path("/run/user/1000/gpu-overlord-1000.sock"),
        )
        self.assertEqual(
            config.gpu_reset_units,
            (
                "dimon-faster-whisper-medium.service",
                "jarvis-nemotron.service",
                "gpu-overlord-stack.service",
            ),
        )

    def test_gpu_reset_units_are_explicit_and_validated(self) -> None:
        env = valid_env()
        env["DIMON_GPU_RESET_UNITS"] = "worker-a.service,worker-b.service"
        self.assertEqual(
            GatewayConfig.from_env(env).gpu_reset_units,
            ("worker-a.service", "worker-b.service"),
        )

        for value in ("", "worker.service,worker.service", "../../worker.service"):
            with self.subTest(value=value):
                env["DIMON_GPU_RESET_UNITS"] = value
                with self.assertRaisesRegex(ValueError, "DIMON_GPU_RESET_UNITS"):
                    GatewayConfig.from_env(env)

    def test_default_gateway_transcription_capacity_is_one(self) -> None:
        app = create_gateway_app(GatewayConfig.from_env(valid_env()))

        self.assertEqual(app.state.transcription_gateway.lease.capacity, 1)

    def test_whisper_cpu_backend_drives_gateway_composition_and_public_revision(self) -> None:
        env = valid_env()
        env.update(
            {
                "DIMON_GATEWAY_STT_BACKEND": "whisper_cpu",
                "DIMON_WHISPER_URL": "ws://127.0.0.1:8003/ws",
                "DIMON_WHISPER_REVISION": "faster-whisper-small-en",
            }
        )

        config = GatewayConfig.from_env(env)
        app = create_gateway_app(config)

        self.assertEqual(
            type(app.state.transcription_gateway.recognizer).__name__,
            "FasterWhisperPcm16Recognizer",
        )
        self.assertEqual(config.transcription_revision, "faster-whisper-small-en")

    def test_dual_backend_uses_nemotron_primary_and_whisper_fallback(self) -> None:
        env = valid_env()
        env.update(
            {
                "DIMON_GATEWAY_STT_BACKEND": "dual",
                "DIMON_WHISPER_URL": "ws://127.0.0.1:8003/ws",
                "DIMON_WHISPER_REVISION": "faster-whisper-small-en",
            }
        )

        config = GatewayConfig.from_env(env)
        app = create_gateway_app(config)
        recognizer = app.state.transcription_gateway.recognizer

        self.assertEqual(type(recognizer).__name__, "DualTranscriptionRecognizer")
        self.assertEqual(type(recognizer.fallback).__name__, "FasterWhisperPcm16Recognizer")
        self.assertEqual(type(recognizer.primary).__name__, "NemotronPcm16Recognizer")
        self.assertEqual(config.transcription_url, "ws://127.0.0.1:8003/ws")
        self.assertEqual(config.transcription_revision, "faster-whisper-small-en")

    def test_dual_backend_accepts_cuda_medium_as_authoritative_final(self) -> None:
        env = valid_env()
        env.update(
            {
                "DIMON_GATEWAY_STT_BACKEND": "dual",
                "DIMON_WHISPER_URL": "ws://127.0.0.1:8007/ws",
                "DIMON_WHISPER_REVISION": "faster-whisper-medium-cuda",
            }
        )

        config = GatewayConfig.from_env(env)
        app = create_gateway_app(config)
        recognizer = app.state.transcription_gateway.recognizer

        self.assertEqual(type(recognizer).__name__, "DualTranscriptionRecognizer")
        self.assertEqual(config.transcription_url, "ws://127.0.0.1:8007/ws")
        self.assertEqual(config.transcription_revision, "faster-whisper-medium-cuda")

    def test_service_key_and_model_revisions_are_required(self) -> None:
        for key in (
            "DIMON_INFERENCE_SERVICE_KEY",
            "DIMON_QWEN_REVISION",
            "DIMON_NEMOTRON_REVISION",
            "DIMON_KOKORO_REVISION",
            "DIMON_GPU_OVERLORD_SOCKET",
        ):
            with self.subTest(key=key):
                env = valid_env()
                env[key] = ""
                with self.assertRaisesRegex(ValueError, key):
                    GatewayConfig.from_env(env)

    def test_gpu_overlord_socket_must_be_absolute(self) -> None:
        env = valid_env()
        env["DIMON_GPU_OVERLORD_SOCKET"] = "relative.sock"

        with self.assertRaisesRegex(ValueError, "DIMON_GPU_OVERLORD_SOCKET"):
            GatewayConfig.from_env(env)

    def test_bind_and_all_upstream_endpoints_must_be_loopback(self) -> None:
        cases = {
            "DIMON_GATEWAY_HOST": "0.0.0.0",
            "DIMON_QWEN_API_BASE": "https://models.example/v1",
            "DIMON_NEMOTRON_URL": "wss://speech.example/v1/realtime",
            "DIMON_KOKORO_URL": "http://192.168.1.10:3001",
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                env = valid_env()
                env[key] = value
                with self.assertRaisesRegex(ValueError, "loopback"):
                    GatewayConfig.from_env(env)

    def test_invalid_port_and_key_header_values_are_rejected(self) -> None:
        for key, value in (
            ("DIMON_GATEWAY_PORT", "70000"),
            ("DIMON_INFERENCE_SERVICE_KEY", "key with spaces"),
            ("DIMON_INFERENCE_SERVICE_KEY", "too-short"),
        ):
            with self.subTest(key=key):
                env = valid_env()
                env[key] = value
                with self.assertRaises(ValueError):
                    GatewayConfig.from_env(env)

    def test_public_revision_identifiers_cannot_contain_paths_or_controls(self) -> None:
        for key in (
            "DIMON_QWEN_REVISION",
            "DIMON_NEMOTRON_REVISION",
            "DIMON_KOKORO_REVISION",
        ):
            for value in ("nested/model/path", "revision with spaces", "r1\r\nX:evil"):
                with self.subTest(key=key, value=value):
                    env = valid_env()
                    env[key] = value
                    with self.assertRaisesRegex(ValueError, key):
                        GatewayConfig.from_env(env)


class ServiceKeyTests(unittest.TestCase):
    def test_exact_bearer_key_is_accepted(self) -> None:
        identity = authenticate_service_key(
            "Bearer shared-test-key-0123456789abcdef",
            "shared-test-key-0123456789abcdef",
        )
        self.assertEqual(identity.subject, "shared-service-key")

    def test_missing_or_incorrect_bearer_key_is_rejected(self) -> None:
        for authorization in (None, "Basic shared-test-key", "Bearer wrong"):
            with self.subTest(authorization=authorization):
                with self.assertRaises(GatewayError) as raised:
                    authenticate_service_key(
                        authorization, "shared-test-key-0123456789abcdef"
                    )
                self.assertEqual(raised.exception.code, "UNAUTHORIZED")
                self.assertEqual(raised.exception.status_code, 401)

    def test_request_dependency_uses_configured_service_key(self) -> None:
        config = GatewayConfig.from_env(valid_env())
        app = create_gateway_app(config)

        @app.get("/probe")
        async def probe(request: Request):
            return {"subject": await require_service_key(request)}

        async def send():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://gateway.test"
            ) as client:
                return await client.get(
                    "/probe",
                    headers={
                        "Authorization": "Bearer shared-test-key-0123456789abcdef"
                    },
                )

        response = asyncio.run(send())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"subject": "shared-service-key"})

    def test_gateway_error_envelope_does_not_expose_exception_details(self) -> None:
        config = GatewayConfig.from_env(valid_env())
        app = create_gateway_app(config)

        @app.get("/probe")
        async def probe():
            raise GatewayError("SAFE_CODE", "safe public message", 429, "request-123")

        async def send():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
                return await client.get("/probe")

        response = asyncio.run(send())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            response.json(),
            {"error": {"code": "SAFE_CODE", "message": "safe public message", "request_id": "request-123"}},
        )


class GatewayCliTests(unittest.TestCase):
    def test_cli_uses_only_validated_loopback_bind(self) -> None:
        calls = []

        def runner(*args, **kwargs):
            calls.append((args, kwargs))

        gateway_main(environ=valid_env(), runner=runner)

        self.assertEqual(calls[0][0], ("services.inference_gateway.app:application_factory",))
        self.assertEqual(
            calls[0][1],
            {"factory": True, "host": "127.0.0.1", "port": 8891},
        )


if __name__ == "__main__":
    unittest.main()

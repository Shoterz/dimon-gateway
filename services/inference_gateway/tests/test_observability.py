from __future__ import annotations

import asyncio
import json
import unittest
from uuid import UUID, uuid4

import httpx

from services.inference_gateway.app import create_gateway_app
from services.inference_gateway.observability import (
    CapabilityReadiness,
    GatewayObservability,
)
from services.inference_gateway.tests.test_auth import valid_env
from services.inference_gateway.tests.test_intent_endpoint import FakeIntentUpstream
from services.inference_gateway.tests.test_transcription_endpoint import (
    FakeRecognizer,
    FakeWebSocket,
)


class MemoryLogger:
    def __init__(self):
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)


class FakeProbe:
    def __init__(self, result: CapabilityReadiness, *, delay: float = 0):
        self.result = result
        self.delay = delay
        self.calls = 0

    async def __call__(self) -> CapabilityReadiness:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


class ObservabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = __import__(
            "services.inference_gateway.config", fromlist=["GatewayConfig"]
        ).GatewayConfig.from_env(valid_env())

    def token(self, *, subject: str = "service-client") -> str:
        del subject
        return self.config.service_key

    def app(self, *, probes=None, timeout=0.1, logger=None, upstream=None):
        observability = GatewayObservability(logger=logger or MemoryLogger())
        app = create_gateway_app(
            self.config,
            intent_upstream=upstream or FakeIntentUpstream(),
            readiness_probes=probes,
            readiness_timeout=timeout,
            observability=observability,
        )
        return app, observability

    async def request(self, app, path: str, *, token=True, headers=None, json_body=None):
        selected_headers = dict(headers or {})
        if token:
            selected_headers["authorization"] = f"Bearer {self.token()}"
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
            if json_body is None:
                return await client.get(path, headers=selected_headers)
            return await client.post(path, headers=selected_headers, json=json_body)

    def test_health_requires_identity_before_reporting_liveness(self) -> None:
        app, _ = self.app()

        unauthorized = asyncio.run(self.request(app, "/health/live", token=False))
        healthy = asyncio.run(self.request(app, "/health/live"))

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(healthy.status_code, 200)
        self.assertEqual(healthy.json(), {"status": "live"})

    def test_readiness_reports_only_named_capabilities_revisions_and_safe_codes(self) -> None:
        probes = {
            "intent": FakeProbe(CapabilityReadiness(True, "READY")),
            "transcription": FakeProbe(CapabilityReadiness(False, "UPSTREAM_UNAVAILABLE")),
            "speech": FakeProbe(CapabilityReadiness(True, "READY")),
        }
        app, _ = self.app(probes=probes)

        response = asyncio.run(self.request(app, "/health/ready"))

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["status"], "not_ready")
        self.assertEqual(set(body["capabilities"]), set(probes))
        self.assertEqual(body["capabilities"]["intent"]["revision"], self.config.qwen_revision)
        self.assertEqual(
            body["capabilities"]["transcription"]["revision"],
            self.config.nemotron_revision,
        )
        self.assertEqual(body["capabilities"]["speech"]["revision"], self.config.kokoro_revision)
        serialized = json.dumps(body)
        self.assertNotIn("127.0.0.1", serialized)
        self.assertNotIn("client-secret", serialized)
        self.assertNotIn("/home/", serialized)

    def test_readiness_probes_have_independent_deadlines(self) -> None:
        probes = {
            "intent": FakeProbe(CapabilityReadiness(True, "READY")),
            "transcription": FakeProbe(CapabilityReadiness(True, "READY"), delay=1),
            "speech": FakeProbe(CapabilityReadiness(True, "READY")),
        }
        app, _ = self.app(probes=probes, timeout=0.01)

        response = asyncio.run(self.request(app, "/health/ready"))

        self.assertEqual(response.status_code, 503)
        capabilities = response.json()["capabilities"]
        self.assertEqual(capabilities["intent"]["reason_code"], "READY")
        self.assertEqual(capabilities["transcription"]["reason_code"], "UPSTREAM_TIMEOUT")
        self.assertEqual(capabilities["speech"]["reason_code"], "READY")

    def test_correlation_id_is_validated_generated_and_returned_on_errors(self) -> None:
        app, _ = self.app()
        supplied = str(uuid4())

        accepted = asyncio.run(
            self.request(app, "/health/live", headers={"x-dimon-correlation-id": supplied})
        )
        generated = asyncio.run(
            self.request(app, "/health/live", headers={"x-dimon-correlation-id": "not-a-uuid"})
        )
        rejected = asyncio.run(self.request(app, "/health/live", token=False))

        self.assertEqual(accepted.headers["x-dimon-correlation-id"], supplied)
        self.assertNotEqual(generated.headers["x-dimon-correlation-id"], "not-a-uuid")
        UUID(generated.headers["x-dimon-correlation-id"])
        UUID(rejected.headers["x-dimon-correlation-id"])

    def test_structured_logs_and_metrics_never_include_request_content_or_tokens(self) -> None:
        logger = MemoryLogger()
        app, observability = self.app(logger=logger)
        secret_prompt = "Send John Smith S$50 from savings. PRIVATE-PROMPT-731"
        request_id = str(uuid4())
        token = self.token()

        response = asyncio.run(
            self.request(
                app,
                "/v1/intent",
                headers={"authorization": f"Bearer {token}"},
                token=False,
                json_body={
                    "request_id": request_id,
                    "text": secret_prompt,
                    "contract_version": 4,
                },
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(logger.messages), 1)
        serialized = "\n".join(logger.messages)
        self.assertNotIn(secret_prompt, serialized)
        self.assertNotIn(token, serialized)
        self.assertNotIn("John Smith", serialized)
        event = json.loads(logger.messages[-1])
        self.assertEqual(
            set(event),
            {
                "event",
                "correlation_id",
                "identity_hash",
                "method",
                "route",
                "outcome_code",
                "queue_delay_ms",
                "latency_ms",
            },
        )
        self.assertEqual(event["route"], "/v1/intent")
        self.assertEqual(event["outcome_code"], "OK")
        self.assertNotEqual(event["identity_hash"], "service-client")

        metrics = observability.snapshot(app)
        self.assertEqual(metrics["requests"]["/v1/intent"]["OK"], 1)
        self.assertEqual(metrics["failures"], {})
        self.assertIn("intent", metrics["capacity"])
        self.assertIn("latency_buckets_ms", metrics)

    def test_websocket_session_uses_correlation_and_content_free_logging(self) -> None:
        logger = MemoryLogger()
        app, observability = self.app(logger=logger)
        app.state.transcription_gateway.recognizer = FakeRecognizer()
        token = self.token()
        correlation_id = str(uuid4())
        socket = FakeWebSocket(
            [
                {
                    "type": "websocket.receive",
                    "text": json.dumps({"type": "input.commit"}),
                }
            ],
            token,
        )
        socket.headers["x-dimon-correlation-id"] = correlation_id
        endpoint = next(
            route.endpoint for route in app.routes if route.path == "/v1/transcription"
        )

        asyncio.run(endpoint(socket))

        event = json.loads(logger.messages[-1])
        self.assertEqual(event["correlation_id"], correlation_id)
        self.assertEqual(event["method"], "WEBSOCKET")
        self.assertEqual(event["route"], "/v1/transcription")
        self.assertEqual(event["outcome_code"], "OK")
        serialized = logger.messages[-1]
        self.assertNotIn(token, serialized)
        self.assertNotIn("Send John", serialized)
        metrics = observability.snapshot(app)
        self.assertEqual(metrics["requests"]["/v1/transcription"]["OK"], 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import json
import time
import unittest
from dataclasses import replace
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from apps.dimon_live.model_adapter import ExtractedIntent
from services.inference_gateway.app import create_gateway_app
from services.inference_gateway.auth import AccessTokenVerifier
from services.inference_gateway.config import GatewayConfig
from services.inference_gateway.idempotency import IdempotencyStore
from services.inference_gateway.limits import BoundedExecutor, SlidingWindowLimiter
from services.inference_gateway.tests.test_auth import StaticJwksClient, valid_env
from services.inference_gateway.upstreams import QwenIntentUpstream


SINGLE_INTENT = {
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
BATCH_INTENT = {
    "contract_version": 4,
    "kind": "BENEFICIARY_TRANSFER_BATCH",
    "source_account_ref": None,
    "source_evidence": None,
    "items": [
        {"recipient_label": "John Smith", "recipient_evidence": "John Smith"},
        {"recipient_label": "Alicia Powell", "recipient_evidence": "Alicia Powell"},
    ],
    "currency": "SGD",
    "requested_execution_date": None,
}


class FakeIntentUpstream:
    def __init__(self, intent=None, *, error: BaseException | None = None):
        self.intent = SINGLE_INTENT if intent is None else intent
        self.error = error
        self.calls: list[str] = []
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def extract(self, text: str) -> ExtractedIntent:
        self.calls.append(text)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        raw = self.intent if isinstance(self.intent, str) else json.dumps(self.intent)
        parsed = self.intent if isinstance(self.intent, dict) else {}
        return ExtractedIntent(dict(parsed), raw, 4.25)


class IntentEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.config = GatewayConfig.from_env(valid_env())

    def token(self, *, subject: str = "user-123") -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "type": "app",
                "iss": self.config.access_issuer,
                "aud": [self.config.access_audience],
                "iat": now - 1,
                "nbf": now - 1,
                "exp": now + 60,
                "sub": subject,
                "email": "friend@example.com",
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )

    def make_app(
        self,
        upstream: FakeIntentUpstream | None = None,
        *,
        executor: BoundedExecutor | None = None,
        limiter: SlidingWindowLimiter | None = None,
        store: IdempotencyStore | None = None,
        config: GatewayConfig | None = None,
    ):
        selected_config = config or self.config
        verifier = AccessTokenVerifier(
            selected_config,
            jwks_client=StaticJwksClient(self.private_key.public_key()),
        )
        return create_gateway_app(
            selected_config,
            verifier=verifier,
            intent_upstream=upstream or FakeIntentUpstream(),
            intent_executor=executor,
            intent_limiter=limiter,
            idempotency_store=store,
        )

    async def request(self, app, *, payload=None, content=None, token: str | None = "default"):
        headers = {}
        if token is not None:
            headers["cf-access-jwt-assertion"] = self.token() if token == "default" else token
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
            return await client.post(
                "/v1/intent",
                json=payload,
                content=content,
                headers=headers,
            )

    @staticmethod
    def payload(text: str = "Send John Smith S$50 from savings.", request_id=None) -> dict:
        return {
            "request_id": str(request_id or uuid4()),
            "text": text,
            "contract_version": 4,
        }

    def test_authentication_happens_before_body_parsing(self) -> None:
        response = asyncio.run(self.request(self.make_app(), content=b"{broken", token=None))

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "UNAUTHORIZED")

    def test_utf8_byte_limit_and_contract_version_are_strict(self) -> None:
        app = self.make_app()
        oversized = asyncio.run(self.request(app, payload=self.payload("é" * 4097)))
        wrong_version = self.payload()
        wrong_version["contract_version"] = 3
        unsupported = asyncio.run(self.request(app, payload=wrong_version))

        self.assertEqual(oversized.status_code, 400)
        self.assertEqual(oversized.json()["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(unsupported.status_code, 400)
        self.assertEqual(unsupported.json()["error"]["code"], "INVALID_REQUEST")

    def test_success_supports_single_and_batch_contract_family(self) -> None:
        for intent in (SINGLE_INTENT, BATCH_INTENT):
            with self.subTest(kind=intent["kind"]):
                upstream = FakeIntentUpstream(intent)
                request_id = uuid4()
                response = asyncio.run(
                    self.request(self.make_app(upstream), payload=self.payload(request_id=request_id))
                )
                body = response.json()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(body["request_id"], str(request_id))
                self.assertEqual(body["intent"], intent)
                self.assertEqual(body["model"], "qwen35-9b-abliterated-q5")
                self.assertEqual(body["revision"], "test-revision")
                self.assertEqual(body["latency_ms"], 4.25)

    def test_valid_raw_output_is_truncated_to_the_host_bound(self) -> None:
        intent = {
            **SINGLE_INTENT,
            "payment_reference": "x" * 500,
            "reference_evidence": "x" * 500,
        }
        config = replace(self.config, max_raw_output_bytes=256)

        response = asyncio.run(
            self.request(
                self.make_app(FakeIntentUpstream(intent), config=config),
                payload=self.payload(),
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.json()["raw_output"].encode("utf-8")), 256)

    def test_malformed_output_and_upstream_outage_are_safe(self) -> None:
        cases = (
            FakeIntentUpstream("{not json"),
            FakeIntentUpstream(error=OSError("private upstream detail")),
        )
        for upstream in cases:
            with self.subTest(error=upstream.error):
                response = asyncio.run(self.request(self.make_app(upstream), payload=self.payload()))
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error"]["code"], "UPSTREAM_UNAVAILABLE")
                self.assertNotIn("private upstream detail", response.text)

    def test_timeout_maps_to_stable_gateway_error(self) -> None:
        upstream = FakeIntentUpstream()
        upstream.release = asyncio.Event()
        app = self.make_app(
            upstream,
            executor=BoundedExecutor(1, 4, 0.01),
        )

        response = asyncio.run(self.request(app, payload=self.payload()))

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"]["code"], "UPSTREAM_TIMEOUT")

    def test_rate_limit_is_per_identity_and_replay_does_not_consume_it(self) -> None:
        now = [100.0]
        upstream = FakeIntentUpstream()
        app = self.make_app(
            upstream,
            limiter=SlidingWindowLimiter(1, 60, clock=lambda: now[0]),
        )
        request_id = uuid4()
        first_payload = self.payload(request_id=request_id)

        first = asyncio.run(self.request(app, payload=first_payload))
        replay = asyncio.run(self.request(app, payload=first_payload))
        limited = asyncio.run(self.request(app, payload=self.payload("Send John S$2.50.")))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["error"]["code"], "RATE_LIMITED")
        self.assertEqual(len(upstream.calls), 1)

    def test_capacity_rejects_work_beyond_the_configured_queue(self) -> None:
        async def scenario() -> None:
            upstream = FakeIntentUpstream()
            upstream.release = asyncio.Event()
            app = self.make_app(upstream, executor=BoundedExecutor(1, 0, 2))
            first = asyncio.create_task(self.request(app, payload=self.payload()))
            await upstream.started.wait()

            excess = await self.request(app, payload=self.payload("Send John S$2.50."))
            self.assertEqual(excess.status_code, 429)
            self.assertEqual(excess.json()["error"]["code"], "CAPACITY_EXCEEDED")
            upstream.release.set()
            self.assertEqual((await first).status_code, 200)

        asyncio.run(scenario())

    def test_idempotent_replay_and_conflict_are_identity_scoped(self) -> None:
        upstream = FakeIntentUpstream()
        app = self.make_app(upstream)
        request_id = uuid4()
        original = self.payload(request_id=request_id)

        first = asyncio.run(self.request(app, payload=original))
        replay = asyncio.run(self.request(app, payload=original))
        conflict = asyncio.run(
            self.request(
                app,
                payload=self.payload("Send John S$2.50.", request_id=request_id),
            )
        )

        self.assertEqual(first.json(), replay.json())
        self.assertEqual(len(upstream.calls), 1)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")


class QwenIntentUpstreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = GatewayConfig.from_env(valid_env())

    def test_request_shape_is_entirely_host_owned(self) -> None:
        captured = {}

        async def transport(url, payload, timeout):
            captured.update(url=url, payload=payload, timeout=timeout)
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(SINGLE_INTENT)},
                    }
                ]
            }

        extracted = asyncio.run(
            QwenIntentUpstream(self.config, transport=transport).extract(
                "Send John Smith S$50 from savings."
            )
        )

        self.assertEqual(captured["url"], "http://127.0.0.1:8080/v1/chat/completions")
        self.assertEqual(captured["timeout"], 120)
        self.assertEqual(captured["payload"]["model"], self.config.qwen_model)
        self.assertEqual(captured["payload"]["temperature"], 0)
        self.assertEqual(captured["payload"]["messages"][1]["role"], "user")
        self.assertEqual(extracted.intent, SINGLE_INTENT)

    def test_transport_timeout_maps_without_leaking_details(self) -> None:
        async def timeout(*_args):
            raise TimeoutError("private socket detail")

        with self.assertRaisesRegex(Exception, "intent upstream timed out") as raised:
            asyncio.run(QwenIntentUpstream(self.config, transport=timeout).extract("request"))
        self.assertEqual(raised.exception.code, "UPSTREAM_TIMEOUT")
        self.assertNotIn("private socket detail", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

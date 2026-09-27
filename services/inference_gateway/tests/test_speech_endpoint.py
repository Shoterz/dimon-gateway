from __future__ import annotations

import asyncio
import io
import struct
import time
import unittest
import wave
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from services.inference_gateway.app import create_gateway_app
from services.inference_gateway.auth import AccessTokenVerifier
from services.inference_gateway.config import GatewayConfig
from services.inference_gateway.limits import BoundedExecutor, SlidingWindowLimiter
from services.inference_gateway.speech import SpeechSynthesis
from services.inference_gateway.tests.test_auth import StaticJwksClient, valid_env


def valid_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(struct.pack("<h", 0) * 160)
    return output.getvalue()


class FakeSpeechSynthesizer:
    def __init__(
        self,
        result: SpeechSynthesis | None = None,
        *,
        error: BaseException | None = None,
    ):
        self.result = result or SpeechSynthesis(
            data=valid_wav(),
            media_type="audio/wav",
            revision="kokoro-fixture-r1",
            latency_ms=3.5,
        )
        self.error = error
        self.calls: list[str] = []
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def __call__(self, text: str) -> SpeechSynthesis:
        self.calls.append(text)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.result


class SpeechEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.config = GatewayConfig.from_env(valid_env())

    def token(self, subject: str = "user-123") -> str:
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
        synthesizer: FakeSpeechSynthesizer | None = None,
        *,
        executor: BoundedExecutor | None = None,
        limiter: SlidingWindowLimiter | None = None,
    ):
        verifier = AccessTokenVerifier(
            self.config,
            jwks_client=StaticJwksClient(self.private_key.public_key()),
        )
        return create_gateway_app(
            self.config,
            verifier=verifier,
            speech_synthesizer=synthesizer or FakeSpeechSynthesizer(),
            speech_executor=executor,
            speech_limiter=limiter,
        )

    @staticmethod
    def payload(**overrides) -> dict:
        value = {
            "request_id": str(uuid4()),
            "status": "EXECUTED_SYNTHETIC_TRANSFER",
            "action_kind": "BENEFICIARY_TRANSFER",
            "amount": 5000,
            "currency": "SGD",
            "recipient_display": "John Smith",
            "reference": "rent",
        }
        value.update(overrides)
        return value

    async def request(self, app, payload: dict, *, subject: str = "user-123"):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
            return await client.post(
                "/v1/speech",
                json=payload,
                headers={"cf-access-jwt-assertion": self.token(subject)},
            )

    def test_success_constructs_text_and_returns_validated_wav_headers(self) -> None:
        synthesizer = FakeSpeechSynthesizer()
        request_id = uuid4()
        response = asyncio.run(
            self.request(
                self.make_app(synthesizer),
                self.payload(request_id=str(request_id)),
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, valid_wav())
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertEqual(response.headers["x-dimon-request-id"], str(request_id))
        self.assertEqual(response.headers["x-dimon-model-revision"], "kokoro-fixture-r1")
        self.assertEqual(response.headers["x-dimon-latency-ms"], "3.5")
        self.assertEqual(
            synthesizer.calls,
            [
                "Synthetic transfer complete. 50 dollars and 00 cents moved to "
                "John Smith. Reference rent."
            ],
        )

    def test_arbitrary_text_extra_fields_and_invalid_values_are_rejected(self) -> None:
        cases = (
            self.payload(text="speak anything"),
            self.payload(amount=-1),
            self.payload(amount=1_000_000_001),
            self.payload(status="PENDING"),
            self.payload(action_kind="SHELL_COMMAND"),
            self.payload(
                status="EXECUTED_SYNTHETIC_BATCH",
                action_kind="BENEFICIARY_TRANSFER",
            ),
            self.payload(recipient_display="John\r\nX-Evil: yes"),
            self.payload(reference="rent\0hidden"),
        )
        app = self.make_app()
        for payload in cases:
            with self.subTest(payload=payload):
                response = asyncio.run(self.request(app, payload))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")

    def test_oversized_wrong_media_and_invalid_wav_are_rejected(self) -> None:
        cases = (
            SpeechSynthesis(b"R" * (8 * 1_048_576 + 1), "audio/wav", "r1", 1.0),
            SpeechSynthesis(valid_wav(), "audio/mpeg", "r1", 1.0),
            SpeechSynthesis(b"RIFFxxxxWAVEnot-valid", "audio/wav", "r1", 1.0),
            SpeechSynthesis(valid_wav(), "audio/wav", "r1\r\nX-Evil: yes", 1.0),
        )
        for result in cases:
            with self.subTest(media_type=result.media_type, size=len(result.data)):
                response = asyncio.run(
                    self.request(self.make_app(FakeSpeechSynthesizer(result)), self.payload())
                )
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error"]["code"], "UPSTREAM_UNAVAILABLE")

    def test_outage_and_timeout_are_stable(self) -> None:
        outage = asyncio.run(
            self.request(
                self.make_app(FakeSpeechSynthesizer(error=OSError("private detail"))),
                self.payload(),
            )
        )
        slow = FakeSpeechSynthesizer()
        slow.release = asyncio.Event()
        timeout = asyncio.run(
            self.request(
                self.make_app(slow, executor=BoundedExecutor(1, 4, 0.01)),
                self.payload(),
            )
        )

        self.assertEqual(outage.status_code, 503)
        self.assertEqual(outage.json()["error"]["code"], "UPSTREAM_UNAVAILABLE")
        self.assertNotIn("private detail", outage.text)
        self.assertEqual(timeout.status_code, 504)
        self.assertEqual(timeout.json()["error"]["code"], "UPSTREAM_TIMEOUT")

    def test_capacity_and_rate_limits_are_enforced(self) -> None:
        async def capacity_scenario() -> None:
            synth = FakeSpeechSynthesizer()
            synth.release = asyncio.Event()
            app = self.make_app(synth, executor=BoundedExecutor(1, 0, 2))
            first = asyncio.create_task(self.request(app, self.payload()))
            await synth.started.wait()
            excess = await self.request(app, self.payload())
            self.assertEqual(excess.status_code, 429)
            self.assertEqual(excess.json()["error"]["code"], "CAPACITY_EXCEEDED")
            synth.release.set()
            self.assertEqual((await first).status_code, 200)

        asyncio.run(capacity_scenario())

        limiter = SlidingWindowLimiter(1, 60, clock=lambda: 100.0)
        app = self.make_app(limiter=limiter)
        self.assertEqual(asyncio.run(self.request(app, self.payload(), subject="alice")).status_code, 200)
        limited = asyncio.run(self.request(app, self.payload(), subject="alice"))
        other = asyncio.run(self.request(app, self.payload(), subject="bob"))
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["error"]["code"], "RATE_LIMITED")
        self.assertEqual(other.status_code, 200)


if __name__ == "__main__":
    unittest.main()

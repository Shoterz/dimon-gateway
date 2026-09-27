from __future__ import annotations

import asyncio
import json
import struct
import time
import unittest
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from apps.dimon_live.model_adapter import InferenceFailure
from apps.dimon_live.voice import VoiceLease, float32le_to_pcm16
from services.inference_gateway.app import create_gateway_app
from services.inference_gateway.auth import AccessTokenVerifier
from services.inference_gateway.config import GatewayConfig
from services.inference_gateway.limits import SlidingWindowLimiter
from services.inference_gateway.tests.test_auth import StaticJwksClient, valid_env
from services.inference_gateway.transcription import (
    TranscriptionGateway,
    pcm16le_to_float32le,
)


class FakeWebSocket:
    def __init__(self, messages, token: str | None, *, peer: str = "127.0.0.1"):
        self.messages = list(messages)
        self.headers = {"cf-access-jwt-assertion": token} if token else {}
        self.client = SimpleNamespace(host=peer)
        self.accepted = False
        self.sent = []
        self.close_code = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, event: dict) -> None:
        self.sent.append(event)

    async def receive(self) -> dict:
        if not self.messages:
            return {"type": "websocket.disconnect"}
        return self.messages.pop(0)

    async def close(self, code: int) -> None:
        self.close_code = code


class FakeStream:
    def __init__(self, on_partial, *, failure: BaseException | None = None):
        self.on_partial = on_partial
        self.failure = failure
        self.audio = bytearray()
        self.closed = False

    async def push_audio(self, audio: bytes) -> None:
        if self.failure:
            raise self.failure
        self.audio.extend(audio)

    async def finish(self) -> str:
        if self.failure:
            raise self.failure
        await self.on_partial("Send John")
        await self.on_partial("Send John Smith S$50")
        return "Send John Smith S$50 from savings."

    async def close(self) -> None:
        self.closed = True


class FakeRecognizer:
    def __init__(self, *, failure: BaseException | None = None):
        self.failure = failure
        self.streams: list[FakeStream] = []

    async def open_stream(self, on_partial):
        if self.failure:
            raise self.failure
        stream = FakeStream(on_partial)
        self.streams.append(stream)
        return stream


class TranscriptionEndpointTests(unittest.TestCase):
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

    def verifier(self) -> AccessTokenVerifier:
        return AccessTokenVerifier(
            self.config,
            jwks_client=StaticJwksClient(self.private_key.public_key()),
        )

    def gateway(self, recognizer=None, **kwargs) -> TranscriptionGateway:
        return TranscriptionGateway(
            verifier=self.verifier(),
            recognizer=recognizer or FakeRecognizer(),
            **kwargs,
        )

    @staticmethod
    def commit() -> dict:
        return {
            "type": "websocket.receive",
            "text": json.dumps({"type": "input.commit"}),
        }

    def test_app_exposes_the_versioned_websocket_route(self) -> None:
        app = create_gateway_app(
            self.config,
            verifier=self.verifier(),
            transcription_recognizer=FakeRecognizer(),
        )
        paths = {route.path for route in app.routes}

        self.assertIn("/v1/transcription", paths)

    def test_pcm16_adapter_preserves_samples_for_the_existing_nemotron_client(self) -> None:
        pcm = struct.pack("<hhhhh", -32768, -1, 0, 1, 32767)

        self.assertEqual(float32le_to_pcm16(pcm16le_to_float32le(pcm)), pcm)

    def test_authenticated_session_orders_ready_partials_and_final(self) -> None:
        recognizer = FakeRecognizer()
        socket = FakeWebSocket(
            [
                {"type": "websocket.receive", "bytes": b"\0\0" * 320},
                self.commit(),
            ],
            self.token(),
        )

        asyncio.run(self.gateway(recognizer).handle(socket))

        self.assertTrue(socket.accepted)
        self.assertEqual(
            [event["type"] for event in socket.sent],
            ["session.ready", "transcript.partial", "transcript.partial", "transcript.final"],
        )
        self.assertTrue(all(event["protocol_version"] == 1 for event in socket.sent))
        self.assertEqual(bytes(recognizer.streams[0].audio), b"\0\0" * 320)
        self.assertTrue(recognizer.streams[0].closed)
        self.assertEqual(socket.close_code, 1000)

    def test_missing_identity_is_rejected_before_accept(self) -> None:
        socket = FakeWebSocket([], None)

        asyncio.run(self.gateway().handle(socket))

        self.assertFalse(socket.accepted)
        self.assertEqual(socket.sent, [])
        self.assertEqual(socket.close_code, 4401)

    def test_second_session_receives_voice_busy_without_opening_upstream(self) -> None:
        async def scenario() -> None:
            lease = VoiceLease()
            self.assertTrue(await lease.acquire())
            recognizer = FakeRecognizer()
            socket = FakeWebSocket([], self.token())
            try:
                await self.gateway(recognizer, lease=lease).handle(socket)
            finally:
                lease.release()
            self.assertTrue(socket.accepted)
            self.assertEqual(socket.sent[0]["code"], "VOICE_BUSY")
            self.assertEqual(recognizer.streams, [])
            self.assertEqual(socket.close_code, 1013)

        asyncio.run(scenario())

    def test_binary_is_never_consumed_before_ready(self) -> None:
        socket = FakeWebSocket(
            [{"type": "websocket.receive", "bytes": b"\0\0" * 16}, self.commit()],
            self.token(),
        )

        asyncio.run(self.gateway().handle(socket))

        self.assertEqual(socket.sent[0]["type"], "session.ready")

    def test_invalid_json_and_wrong_event_type_fail_closed(self) -> None:
        messages = (
            {"type": "websocket.receive", "text": "{broken"},
            {"type": "websocket.receive", "text": json.dumps({"type": "cancel"})},
        )
        for message in messages:
            with self.subTest(message=message):
                socket = FakeWebSocket([message], self.token())
                asyncio.run(self.gateway().handle(socket))
                self.assertEqual(socket.sent[-1]["type"], "error")
                self.assertEqual(socket.sent[-1]["code"], "INVALID_INFERENCE_REQUEST")

    def test_audio_byte_and_duration_limits_are_independent(self) -> None:
        cases = (
            (
                self.gateway(max_audio_bytes=4, max_duration_seconds=30),
                b"\0\0" * 3,
                "AUDIO_LIMIT_EXCEEDED",
            ),
            (
                self.gateway(max_audio_bytes=1000, max_duration_seconds=0.001),
                b"\0\0" * 33,
                "UTTERANCE_LIMIT_EXCEEDED",
            ),
        )
        for gateway, audio, code in cases:
            with self.subTest(code=code):
                socket = FakeWebSocket(
                    [{"type": "websocket.receive", "bytes": audio}],
                    self.token(),
                )
                asyncio.run(gateway.handle(socket))
                self.assertEqual(socket.sent[-1]["code"], code)

    def test_upstream_failure_is_translated_and_lease_is_released(self) -> None:
        async def scenario() -> None:
            lease = VoiceLease()
            recognizer = FakeRecognizer(
                failure=InferenceFailure("UPSTREAM_UNAVAILABLE", "private detail")
            )
            first = FakeWebSocket([], self.token())
            await self.gateway(recognizer, lease=lease).handle(first)
            self.assertEqual(first.sent[-1]["code"], "UPSTREAM_UNAVAILABLE")
            self.assertNotIn("private detail", str(first.sent))

            healthy = FakeWebSocket([self.commit()], self.token())
            await self.gateway(FakeRecognizer(), lease=lease).handle(healthy)
            self.assertEqual(healthy.sent[-1]["type"], "transcript.final")

        asyncio.run(scenario())

    def test_client_disconnect_closes_upstream_and_releases_lease(self) -> None:
        async def scenario() -> None:
            lease = VoiceLease()
            recognizer = FakeRecognizer()
            disconnected = FakeWebSocket([], self.token())
            await self.gateway(recognizer, lease=lease).handle(disconnected)
            self.assertTrue(recognizer.streams[0].closed)

            next_socket = FakeWebSocket([self.commit()], self.token())
            await self.gateway(FakeRecognizer(), lease=lease).handle(next_socket)
            self.assertEqual(next_socket.sent[-1]["type"], "transcript.final")

        asyncio.run(scenario())

    def test_rate_limit_is_per_verified_identity(self) -> None:
        now = [100.0]
        limiter = SlidingWindowLimiter(1, 60, clock=lambda: now[0])
        gateway = self.gateway(limiter=limiter)
        first = FakeWebSocket([self.commit()], self.token("alice"))
        limited = FakeWebSocket([], self.token("alice"))
        other = FakeWebSocket([self.commit()], self.token("bob"))

        asyncio.run(gateway.handle(first))
        asyncio.run(gateway.handle(limited))
        asyncio.run(gateway.handle(other))

        self.assertEqual(limited.close_code, 4429)
        self.assertFalse(limited.accepted)
        self.assertEqual(other.sent[-1]["type"], "transcript.final")


if __name__ == "__main__":
    unittest.main()

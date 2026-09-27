from __future__ import annotations

import asyncio
import io
import json
import struct
import unittest
import wave
from collections.abc import Awaitable, Callable
from uuid import UUID

import httpx

from apps.dimon_live.model_adapter import InferenceFailure
from apps.dimon_live.remote_inference import (
    RemoteExtractor,
    RemoteGatewayClient,
    RemoteGatewayConfig,
    RemoteRecognizer,
    RemoteSynthesizer,
    STABLE_GATEWAY_ERROR_CODES,
)


PROMPT = "Send John Smith S$50 from savings."
INTENT = {
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


def wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(struct.pack("<h", 0) * 160)
    return output.getvalue()


def config() -> RemoteGatewayConfig:
    return RemoteGatewayConfig.from_env(
        {
            "DIMON_INFERENCE_GATEWAY_URL": "https://gateway.example.test",
            "DIMON_CF_ACCESS_CLIENT_ID": "client-id",
            "DIMON_CF_ACCESS_CLIENT_SECRET": "client-secret",
        }
    )


class RemoteConfigurationTests(unittest.TestCase):
    def test_remote_configuration_requires_https_and_all_credentials(self) -> None:
        cases = (
            {},
            {"DIMON_INFERENCE_GATEWAY_URL": "http://gateway.test"},
            {
                "DIMON_INFERENCE_GATEWAY_URL": "https://gateway.test",
                "DIMON_CF_ACCESS_CLIENT_ID": "id",
            },
            {
                "DIMON_INFERENCE_GATEWAY_URL": "https://user:pass@gateway.test",
                "DIMON_CF_ACCESS_CLIENT_ID": "id",
                "DIMON_CF_ACCESS_CLIENT_SECRET": "secret",
            },
        )
        for env in cases:
            with self.subTest(env=env), self.assertRaises(ValueError):
                RemoteGatewayConfig.from_env(env)


class RemoteHttpTests(unittest.TestCase):
    def make_client(self, handler) -> tuple[RemoteGatewayClient, list[httpx.Request]]:
        requests: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return handler(request)

        transport = httpx.MockTransport(record)
        return (
            RemoteGatewayClient(
                config(),
                sync_client=httpx.Client(transport=transport),
                async_client=httpx.AsyncClient(transport=transport),
            ),
            requests,
        )

    def test_extractor_sends_service_token_and_parses_validated_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "request_id": payload["request_id"],
                    "intent": INTENT,
                    "raw_output": json.dumps(INTENT),
                    "model": "qwen-fixture",
                    "revision": "qwen-r1",
                    "latency_ms": 4.25,
                },
            )

        client, requests = self.make_client(handler)
        extracted = RemoteExtractor(client).extract(PROMPT)

        self.assertEqual(extracted.intent, INTENT)
        self.assertEqual(extracted.latency_ms, 4.25)
        request = requests[0]
        payload = json.loads(request.content)
        self.assertEqual(request.url.path, "/v1/intent")
        self.assertEqual(payload["text"], PROMPT)
        self.assertEqual(payload["contract_version"], 4)
        self.assertRegex(payload["request_id"], r"^[0-9a-f-]{36}$")
        self.assertEqual(request.headers["cf-access-client-id"], "client-id")
        self.assertEqual(request.headers["cf-access-client-secret"], "client-secret")
        self.assertEqual(request.headers["x-dimon-correlation-id"], payload["request_id"])

    def test_every_stable_gateway_error_is_preserved_without_its_message(self) -> None:
        for code in STABLE_GATEWAY_ERROR_CODES:
            with self.subTest(code=code):
                response = httpx.Response(
                    503,
                    json={
                        "error": {
                            "code": code,
                            "message": "private gateway detail",
                            "request_id": None,
                        }
                    },
                )
                client, _ = self.make_client(lambda _request, value=response: value)
                with self.assertRaises(InferenceFailure) as raised:
                    RemoteExtractor(client).extract(PROMPT)
                self.assertEqual(raised.exception.reason_code, code)
                self.assertNotIn("private gateway detail", str(raised.exception))

    def test_invalid_and_oversized_success_responses_fail_closed(self) -> None:
        responses = (
            httpx.Response(200, json={"request_id": "wrong"}),
            httpx.Response(200, content=b"x" * (1_048_576 + 1)),
        )
        for response in responses:
            with self.subTest(size=len(response.content)):
                client, _ = self.make_client(lambda _request, value=response: value)
                with self.assertRaises(InferenceFailure) as raised:
                    RemoteExtractor(client).extract(PROMPT)
                self.assertEqual(raised.exception.reason_code, "UPSTREAM_UNAVAILABLE")

    def test_timeout_is_reported_with_stable_code(self) -> None:
        client, _ = self.make_client(
            lambda _request: (_ for _ in ()).throw(httpx.ReadTimeout("private"))
        )
        with self.assertRaises(InferenceFailure) as raised:
            RemoteExtractor(client).extract(PROMPT)
        self.assertEqual(raised.exception.reason_code, "UPSTREAM_TIMEOUT")
        self.assertNotIn("private", str(raised.exception))

    def test_speech_sends_only_committed_fields_and_validates_wav(self) -> None:
        audio = wav_bytes()

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                content=audio,
                headers={
                    "content-type": "audio/wav",
                    "x-dimon-request-id": payload["request_id"],
                    "x-dimon-model-revision": "kokoro-r1",
                    "x-dimon-latency-ms": "2.5",
                },
            )

        client, requests = self.make_client(handler)
        result = {
            "outcome": "EXECUTED_SYNTHETIC_TRANSFER",
            "amount_minor": 5000,
            "currency": "SGD",
            "validated_intent": {"payment_reference": "rent"},
            "resolved_action": {
                "kind": "BENEFICIARY_TRANSFER",
                "recipient_display_name": "John Smith",
            },
            "state": {"must": "not leave Dimon"},
        }

        received = asyncio.run(RemoteSynthesizer(client).synthesize_result(result))

        self.assertEqual(received, audio)
        payload = json.loads(requests[0].content)
        self.assertEqual(
            set(payload),
            {
                "request_id",
                "status",
                "action_kind",
                "amount",
                "currency",
                "recipient_display",
                "reference",
            },
        )
        self.assertEqual(payload["reference"], "rent")
        self.assertNotIn("state", payload)

    def test_speech_rejects_noncommitted_results_and_bad_response_fields(self) -> None:
        client, requests = self.make_client(
            lambda _request: httpx.Response(200, content=b"not wav", headers={"content-type": "audio/wav"})
        )
        synthesizer = RemoteSynthesizer(client)
        with self.assertRaises(InferenceFailure) as raised:
            asyncio.run(synthesizer.synthesize_result({"outcome": "BLOCKED"}))
        self.assertEqual(raised.exception.reason_code, "INVALID_INFERENCE_REQUEST")
        self.assertEqual(requests, [])

        committed = {
            "outcome": "EXECUTED_SYNTHETIC_BATCH",
            "amount_minor": 1000,
            "currency": "SGD",
            "resolved_actions": [{}, {}],
        }
        with self.assertRaises(InferenceFailure) as raised:
            asyncio.run(synthesizer.synthesize_result(committed))
        self.assertEqual(raised.exception.reason_code, "UPSTREAM_UNAVAILABLE")


class FakeWebSocket:
    def __init__(self, events: list[str | bytes]):
        self.events = list(events)
        self.sent: list[str | bytes] = []
        self.closed = False

    async def recv(self):
        if not self.events:
            await asyncio.Future()
        return self.events.pop(0)

    async def send(self, value):
        self.sent.append(value)

    async def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, socket: FakeWebSocket):
        self.socket = socket
        self.exited = False

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *_args):
        self.exited = True
        await self.socket.close()


class RemoteVoiceTests(unittest.TestCase):
    def test_stream_authenticates_converts_audio_and_forwards_partials(self) -> None:
        events = [
            json.dumps(
                {
                    "type": "session.ready",
                    "protocol_version": 1,
                    "sample_rate": 16_000,
                    "encoding": "pcm_s16le",
                }
            ),
            json.dumps({"type": "transcript.partial", "protocol_version": 1, "text": "Send John"}),
            json.dumps({"type": "transcript.final", "protocol_version": 1, "text": PROMPT}),
        ]
        socket = FakeWebSocket(events)
        connection = FakeConnection(socket)
        calls: list[tuple[str, dict]] = []

        def connect(url: str, **kwargs):
            calls.append((url, kwargs))
            return connection

        partials: list[str] = []

        async def scenario() -> str:
            client = RemoteGatewayClient(config(), websocket_connect=connect)
            stream = await RemoteRecognizer(client).open_stream(
                lambda text: self._append(partials, text)
            )
            await stream.push_audio(struct.pack("<ff", 1.0, -1.0))
            return await stream.finish()

        final = asyncio.run(scenario())

        self.assertEqual(final, PROMPT)
        self.assertEqual(partials, ["Send John"])
        self.assertEqual(socket.sent[0], struct.pack("<hh", 32767, -32768))
        self.assertEqual(json.loads(socket.sent[1]), {"type": "input.commit"})
        self.assertEqual(calls[0][0], "wss://gateway.example.test/v1/transcription")
        self.assertEqual(calls[0][1]["additional_headers"]["CF-Access-Client-Id"], "client-id")
        UUID(calls[0][1]["additional_headers"]["X-Dimon-Correlation-Id"])
        self.assertLessEqual(calls[0][1]["max_size"], 1_048_576)
        self.assertTrue(connection.exited)

    @staticmethod
    async def _append(items: list[str], value: str) -> None:
        items.append(value)

    def test_voice_gateway_error_cancel_and_late_audio_are_bounded(self) -> None:
        async def scenario() -> None:
            socket = FakeWebSocket(
                [
                    json.dumps(
                        {
                            "type": "session.ready",
                            "protocol_version": 1,
                            "sample_rate": 16_000,
                            "encoding": "pcm_s16le",
                        }
                    ),
                    json.dumps({"type": "error", "protocol_version": 1, "code": "VOICE_BUSY"}),
                ]
            )
            client = RemoteGatewayClient(
                config(), websocket_connect=lambda *_args, **_kwargs: FakeConnection(socket)
            )
            stream = await RemoteRecognizer(client).open_stream(lambda _text: asyncio.sleep(0))
            with self.assertRaises(InferenceFailure) as raised:
                await stream.finish()
            self.assertEqual(raised.exception.reason_code, "VOICE_BUSY")
            await stream.close()
            with self.assertRaises(InferenceFailure) as closed:
                await stream.push_audio(struct.pack("<f", 0.0))
            self.assertEqual(closed.exception.reason_code, "STREAM_CLOSED")

        asyncio.run(scenario())

    def test_voice_busy_can_be_the_first_gateway_event(self) -> None:
        async def scenario() -> None:
            socket = FakeWebSocket(
                [json.dumps({"type": "error", "protocol_version": 1, "code": "VOICE_BUSY"})]
            )
            client = RemoteGatewayClient(
                config(), websocket_connect=lambda *_args, **_kwargs: FakeConnection(socket)
            )
            with self.assertRaises(InferenceFailure) as raised:
                await RemoteRecognizer(client).open_stream(lambda _text: asyncio.sleep(0))
            self.assertEqual(raised.exception.reason_code, "VOICE_BUSY")
            self.assertTrue(socket.closed)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

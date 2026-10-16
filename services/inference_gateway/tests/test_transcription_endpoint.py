from __future__ import annotations

import asyncio
import json
import struct
import unittest
from types import SimpleNamespace
from uuid import uuid4


from apps.dimon_live.model_adapter import InferenceFailure
from apps.dimon_live.voice import VoiceLease, float32le_to_pcm16
from services.inference_gateway.app import create_gateway_app
from services.inference_gateway.config import GatewayConfig
from services.inference_gateway.limits import SlidingWindowLimiter
from services.inference_gateway.tests.test_auth import valid_env
from services.inference_gateway.transcription import (
    DualTranscriptionRecognizer,
    FasterWhisperPcm16Recognizer,
    NemotronPcm16Recognizer,
    TranscriptionGateway,
    _NemotronPcm16Stream,
    pcm16le_to_float32le,
)


class RecordingStream:
    def __init__(self, on_partial, *, final="final", fail_at=None, partial=None):
        self.on_partial = on_partial
        self.final = final
        self.fail_at = fail_at
        self.partial = partial
        self.audio = []
        self.closed = False

    async def push_audio(self, audio):
        if self.fail_at == "push":
            raise RuntimeError("caption push failed")
        self.audio.append(audio)
        if self.partial:
            await self.on_partial(self.partial)

    async def finish(self):
        if self.fail_at == "finish":
            raise RuntimeError("caption finish failed")
        return self.final

    async def close(self):
        self.closed = True


class RecordingRecognizer:
    def __init__(self, **stream_options):
        self.stream_options = stream_options
        self.stream = None

    async def open_stream(self, on_partial):
        if self.stream_options.get("fail_at") == "open":
            raise RuntimeError("caption open failed")
        self.stream = RecordingStream(on_partial, **self.stream_options)
        return self.stream


class FlakyRecordingRecognizer(RecordingRecognizer):
    def __init__(self, failures):
        super().__init__()
        self.failures = failures
        self.opens = 0

    async def open_stream(self, on_partial):
        self.opens += 1
        if self.opens <= self.failures:
            raise RuntimeError("caption open failed")
        return await super().open_stream(on_partial)


class FakeWebSocket:
    def __init__(self, messages, token: str | None, *, peer: str = "127.0.0.1"):
        self.messages = list(messages)
        self.headers = {"authorization": f"Bearer {token}"} if token else {}
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


class RecordingLifecycleLease:
    def __init__(self, lifecycle, number):
        self.lifecycle = lifecycle
        self.number = number
        self.releases = []
        self.released = False

    async def release(self, *, disconnected=False):
        self.releases.append(disconnected)
        if self.released:
            return
        self.released = True
        self.lifecycle.active.discard(self.number)


class RecordingLifecycle:
    def __init__(self):
        self.handles = []
        self.active = set()

    async def acquire(self):
        number = len(self.handles) + 1
        handle = RecordingLifecycleLease(self, number)
        self.handles.append(handle)
        self.active.add(number)
        return handle


class BlockingOpenClient:
    def __init__(self):
        self.entered = asyncio.Event()

    async def open_stream(self, _on_partial):
        self.entered.set()
        await asyncio.Event().wait()


class BlockingReceiveWebSocket(FakeWebSocket):
    def __init__(self, token):
        super().__init__([], token)
        self.receiving = asyncio.Event()

    async def receive(self):
        self.receiving.set()
        await asyncio.Event().wait()


class FakeWhisperWorkerSocket:
    def __init__(self, response: dict[str, object]):
        self.response = response
        self.sent: list[bytes | str] = []

    async def send(self, payload: bytes | str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        return json.dumps(self.response)


class FakeWhisperConnection:
    def __init__(self, websocket: FakeWhisperWorkerSocket):
        self.websocket = websocket
        self.closed = False

    async def __aenter__(self) -> FakeWhisperWorkerSocket:
        return self.websocket

    async def __aexit__(self, *_args) -> None:
        self.closed = True


class TranscriptionEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = GatewayConfig.from_env(valid_env())

    def token(self, subject: str = "user-123") -> str:
        del subject
        return self.config.service_key

    def gateway(self, recognizer=None, **kwargs) -> TranscriptionGateway:
        return TranscriptionGateway(
            service_key=self.config.service_key,
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
            transcription_recognizer=FakeRecognizer(),
        )
        paths = {route.path for route in app.routes}

        self.assertIn("/v1/transcription", paths)

    def test_pcm16_adapter_preserves_samples_for_the_existing_nemotron_client(self) -> None:
        pcm = struct.pack("<hhhhh", -32768, -1, 0, 1, 32767)

        self.assertEqual(float32le_to_pcm16(pcm16le_to_float32le(pcm)), pcm)

    def test_nemotron_probe_close_is_clean_but_stream_failure_requests_recovery(self) -> None:
        class Lifecycle:
            def __init__(self):
                self.releases = []

            async def release(self, *, disconnected=False):
                self.releases.append(disconnected)

        class Stream:
            def __init__(self, fail=False):
                self.fail = fail

            async def push_audio(self, _audio):
                if self.fail:
                    raise RuntimeError("closed")

            async def close(self):
                return None

        async def scenario():
            clean_lifecycle = Lifecycle()
            clean = _NemotronPcm16Stream(Stream(), clean_lifecycle)
            await clean.close()

            failed_lifecycle = Lifecycle()
            failed = _NemotronPcm16Stream(Stream(fail=True), failed_lifecycle)
            with self.assertRaisesRegex(RuntimeError, "closed"):
                await failed.push_audio(b"\0\0")
            await failed.close()
            return clean_lifecycle.releases, failed_lifecycle.releases

        clean, failed = asyncio.run(scenario())
        self.assertEqual(clean, [False])
        self.assertEqual(failed, [True])

    def test_each_nemotron_stream_owns_a_distinct_lifecycle_lease(self) -> None:
        async def scenario():
            lifecycle = RecordingLifecycle()
            recognizer = NemotronPcm16Recognizer("ws://127.0.0.1:8004/v1/realtime")
            recognizer.lifecycle = lifecycle
            recognizer.client = RecordingRecognizer()

            first = await recognizer.open_stream(lambda _text: asyncio.sleep(0))
            second = await recognizer.open_stream(lambda _text: asyncio.sleep(0))
            await first.close()
            await second.close()
            return lifecycle

        lifecycle = asyncio.run(scenario())
        self.assertEqual([handle.number for handle in lifecycle.handles], [1, 2])
        self.assertEqual(
            [handle.releases for handle in lifecycle.handles],
            [[False], [False]],
        )
        self.assertEqual(lifecycle.active, set())

    def test_stream_close_releases_exactly_once(self) -> None:
        async def scenario():
            lifecycle = RecordingLifecycle()
            lease = await lifecycle.acquire()
            stream = RecordingStream(lambda _text: asyncio.sleep(0))
            wrapped = _NemotronPcm16Stream(stream, lease)
            await wrapped.close()
            await wrapped.close()
            return stream, lease, lifecycle

        stream, lease, lifecycle = asyncio.run(scenario())
        self.assertTrue(stream.closed)
        self.assertEqual(lease.releases, [False])
        self.assertEqual(lifecycle.active, set())

    def test_stream_push_and_finish_failure_release_with_disconnect_hint(self) -> None:
        async def scenario(fail_at):
            lifecycle = RecordingLifecycle()
            lease = await lifecycle.acquire()
            wrapped = _NemotronPcm16Stream(
                RecordingStream(lambda _text: asyncio.sleep(0), fail_at=fail_at),
                lease,
            )
            with self.assertRaisesRegex(RuntimeError, f"caption {fail_at} failed"):
                if fail_at == "push":
                    await wrapped.push_audio(b"\0\0")
                else:
                    await wrapped.finish()
            await wrapped.close()
            return lease.releases, lifecycle.active

        for fail_at in ("push", "finish"):
            with self.subTest(fail_at=fail_at):
                releases, active = asyncio.run(scenario(fail_at))
                self.assertEqual(releases, [True])
                self.assertEqual(active, set())

    def test_cancel_during_nemotron_open_releases_acquired_handle(self) -> None:
        async def scenario():
            lifecycle = RecordingLifecycle()
            recognizer = NemotronPcm16Recognizer("ws://127.0.0.1:8004/v1/realtime")
            recognizer.lifecycle = lifecycle
            recognizer.client = BlockingOpenClient()
            task = asyncio.create_task(
                recognizer.open_stream(lambda _text: asyncio.sleep(0))
            )
            await recognizer.client.entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return lifecycle

        lifecycle = asyncio.run(scenario())
        self.assertEqual(lifecycle.handles[0].releases, [True])
        self.assertEqual(lifecycle.active, set())

    def test_cancel_during_gateway_stream_releases_handle_and_capacity(self) -> None:
        async def scenario():
            lifecycle = RecordingLifecycle()
            recognizer = NemotronPcm16Recognizer("ws://127.0.0.1:8004/v1/realtime")
            recognizer.lifecycle = lifecycle
            recognizer.client = RecordingRecognizer()
            capacity = VoiceLease(1)
            socket = BlockingReceiveWebSocket(self.token())
            task = asyncio.create_task(
                self.gateway(recognizer, lease=capacity).handle(socket)
            )
            await socket.receiving.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            capacity_available = await capacity.acquire()
            if capacity_available:
                capacity.release()
            return lifecycle, capacity_available

        lifecycle, capacity_available = asyncio.run(scenario())
        self.assertTrue(capacity_available)
        self.assertEqual(lifecycle.handles[0].releases, [False])
        self.assertEqual(lifecycle.active, set())

    def test_gateway_handler_shutdown_cancellation_releases_handle(self) -> None:
        async def scenario():
            lifecycle = RecordingLifecycle()
            recognizer = NemotronPcm16Recognizer("ws://127.0.0.1:8004/v1/realtime")
            recognizer.lifecycle = lifecycle
            recognizer.client = RecordingRecognizer()
            socket = BlockingReceiveWebSocket(self.token())
            task = asyncio.create_task(self.gateway(recognizer).handle(socket))
            await socket.receiving.wait()
            task.cancel("gateway shutdown")
            with self.assertRaises(asyncio.CancelledError):
                await task
            return lifecycle

        lifecycle = asyncio.run(scenario())
        self.assertEqual(lifecycle.handles[0].releases, [False])
        self.assertEqual(lifecycle.active, set())

    def test_one_hundred_open_close_and_cancel_cycles_leave_zero_leases(self) -> None:
        async def scenario():
            lifecycle = RecordingLifecycle()
            recognizer = NemotronPcm16Recognizer("ws://127.0.0.1:8004/v1/realtime")
            recognizer.lifecycle = lifecycle
            for cycle in range(100):
                if cycle % 10 == 0:
                    blocker = BlockingOpenClient()
                    recognizer.client = blocker
                    task = asyncio.create_task(
                        recognizer.open_stream(lambda _text: asyncio.sleep(0))
                    )
                    await blocker.entered.wait()
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    recognizer.client = RecordingRecognizer()
                    stream = await recognizer.open_stream(
                        lambda _text: asyncio.sleep(0)
                    )
                    await stream.close()
            return lifecycle

        lifecycle = asyncio.run(scenario())
        self.assertEqual(len(lifecycle.handles), 100)
        self.assertTrue(all(len(handle.releases) == 1 for handle in lifecycle.handles))
        self.assertEqual(lifecycle.active, set())

    def test_faster_whisper_adapter_uses_the_existing_worker_protocol(self) -> None:
        pcm = struct.pack("<hhhhh", -32768, -1, 0, 1, 32767)
        worker = FakeWhisperWorkerSocket(
            {
                "text": "Send John Smith S$50 from savings.",
                "segments": [
                    {"avg_logprob": -0.1, "no_speech_prob": 0.02},
                ],
            }
        )
        connection = FakeWhisperConnection(worker)
        recognizer = FasterWhisperPcm16Recognizer(
            "ws://127.0.0.1:8003/ws",
            websocket_connect=lambda *_args, **_kwargs: connection,
        )
        socket = FakeWebSocket(
            [
                {"type": "websocket.receive", "bytes": pcm},
                self.commit(),
            ],
            self.token(),
        )

        asyncio.run(self.gateway(recognizer).handle(socket))

        self.assertEqual(
            [event["type"] for event in socket.sent],
            ["session.ready", "transcript.final"],
        )
        self.assertEqual(
            socket.sent[-1]["text"],
            "Send John Smith S$50 from savings.",
        )
        self.assertEqual(worker.sent, [pcm16le_to_float32le(pcm), "done"])
        self.assertTrue(connection.closed)

    def test_dual_recognizer_emits_partial_and_returns_nemotron_final(self) -> None:
        fallback = RecordingRecognizer(final="fallback whisper text")
        primary = RecordingRecognizer(final="NVIDIA help me buy one share in it", partial="live words")
        recognizer = DualTranscriptionRecognizer(fallback, primary)
        events = []

        async def scenario():
            async def on_partial(text):
                events.append(text)

            stream = await recognizer.open_stream(on_partial)
            await stream.push_audio(b"\0\0")
            result = await stream.finish()
            await stream.close()
            return result, stream

        result, stream = asyncio.run(scenario())

        self.assertEqual(events, ["live words"])
        self.assertEqual(result, "NVIDIA help me buy one share in it")
        self.assertEqual(stream.recognition_path, "nemotron_final")
        self.assertTrue(stream.transcript_disagreement)
        self.assertEqual(fallback.stream.audio, [b"\0\0"])
        self.assertEqual(primary.stream.audio, [b"\0\0"])
        self.assertTrue(fallback.stream.closed)
        self.assertTrue(primary.stream.closed)

    def test_dual_recognizer_reports_bounded_nemotron_open_failure_for_cuda_fallback(self) -> None:
        recognizer = DualTranscriptionRecognizer(
            RecordingRecognizer(final="authoritative whisper text"),
            RecordingRecognizer(fail_at="open"),
            caption_open_attempts=1,
            caption_retry_delay=0,
        )

        async def scenario():
            stream = await recognizer.open_stream(lambda _text: asyncio.sleep(0))
            result = await stream.finish()
            return (
                result,
                stream.nemotron_state,
                stream.nemotron_failure_phase,
                stream.recognition_path,
            )

        result, state, phase, path = asyncio.run(scenario())
        self.assertEqual(result, "authoritative whisper text")
        self.assertEqual(state, "FAILED")
        self.assertEqual(phase, "nemotron_protocol")
        self.assertEqual(path, "faster_whisper_final")

    def test_gateway_binds_trace_headers_and_reports_nemotron_state(self) -> None:
        trace_id = str(uuid4())
        attempt_id = str(uuid4())
        recognizer = DualTranscriptionRecognizer(
            RecordingRecognizer(final="authoritative"),
            RecordingRecognizer(fail_at="open"),
            caption_open_attempts=1,
            caption_retry_delay=0,
        )
        socket = FakeWebSocket([self.commit()], self.token())
        socket.headers.update({
            "x-dimon-trace-id": trace_id,
            "x-dimon-attempt-id": attempt_id,
        })

        asyncio.run(self.gateway(recognizer).handle(socket))

        self.assertEqual(socket._dimon_trace_id, trace_id)
        self.assertEqual(socket._dimon_attempt_id, attempt_id)
        self.assertEqual(socket.sent[0]["nemotron_state"], "FAILED")
        self.assertEqual(socket.sent[0]["nemotron_failure_phase"], "nemotron_protocol")
        self.assertEqual(socket.sent[-1]["nemotron_state"], "FAILED")
        self.assertEqual(socket.sent[-1]["recognition_path"], "faster_whisper_final")
        self.assertIn("transcript_disagreement", socket.sent[-1])

    def test_dual_recognizer_retries_caption_open_without_reopening_authority(self) -> None:
        authoritative = RecordingRecognizer(final="authoritative")
        captions = FlakyRecordingRecognizer(failures=1)
        recognizer = DualTranscriptionRecognizer(
            authoritative, captions, caption_open_attempts=2, caption_retry_delay=0,
        )

        async def scenario():
            stream = await recognizer.open_stream(lambda _text: asyncio.sleep(0))
            await stream.push_audio(b"\0\0")
            return await stream.finish()

        self.assertEqual(asyncio.run(scenario()), "final")
        self.assertEqual(captions.opens, 2)
        self.assertEqual(authoritative.stream.audio, [b"\0\0"])

    def test_caption_retry_budget_is_fresh_for_every_utterance(self) -> None:
        class PerUtteranceCaptions:
            def __init__(self):
                self.opens = 0

            async def open_stream(self, on_partial):
                self.opens += 1
                raise RuntimeError("still unavailable")

        captions = PerUtteranceCaptions()
        recognizer = DualTranscriptionRecognizer(
            RecordingRecognizer(final="authoritative"), captions,
            caption_open_attempts=2, caption_retry_delay=0,
        )

        async def scenario():
            first = await recognizer.open_stream(lambda _text: asyncio.sleep(0))
            self.assertEqual(await first.finish(), "authoritative")
            recognizer.fallback = RecordingRecognizer(final="authoritative again")
            second = await recognizer.open_stream(lambda _text: asyncio.sleep(0))
            self.assertEqual(await second.finish(), "authoritative again")

        asyncio.run(scenario())
        self.assertEqual(captions.opens, 4)

    def test_cancelling_caption_retry_closes_authoritative_stream(self) -> None:
        authoritative = RecordingRecognizer(final="authoritative")
        captions = FlakyRecordingRecognizer(failures=99)
        recognizer = DualTranscriptionRecognizer(
            authoritative, captions, caption_open_attempts=2, caption_retry_delay=30,
        )

        async def scenario():
            task = asyncio.create_task(
                recognizer.open_stream(lambda _text: asyncio.sleep(0))
            )
            while captions.opens < 1:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        self.assertTrue(authoritative.stream.closed)

    def test_dual_recognizer_tolerates_every_caption_failure_stage(self) -> None:
        for fail_at in ("open", "push", "finish"):
            with self.subTest(fail_at=fail_at):
                authoritative = RecordingRecognizer(final="authoritative")
                captions = RecordingRecognizer(fail_at=fail_at)
                recognizer = DualTranscriptionRecognizer(authoritative, captions)

                async def scenario():
                    stream = await recognizer.open_stream(lambda _text: asyncio.sleep(0))
                    await stream.push_audio(b"\0\0")
                    result = await stream.finish()
                    await stream.close()
                    return result

                self.assertEqual(asyncio.run(scenario()), "authoritative")
                self.assertTrue(authoritative.stream.closed)
                if captions.stream is not None:
                    self.assertTrue(captions.stream.closed)

    def test_dual_recognizer_drops_hung_whisper_push_without_blocking_nemotron(self) -> None:
        class HangingPushStream(RecordingStream):
            async def push_audio(self, _audio):
                await asyncio.Event().wait()

        class HangingPushRecognizer:
            def __init__(self):
                self.stream = None

            async def open_stream(self, on_partial):
                self.stream = HangingPushStream(on_partial)
                return self.stream

        fallback = HangingPushRecognizer()
        primary = RecordingRecognizer(final="NVIDIA")
        recognizer = DualTranscriptionRecognizer(
            fallback,
            primary,
            stream_push_timeout=0.01,
        )

        async def scenario():
            stream = await recognizer.open_stream(lambda _text: asyncio.sleep(0))
            await asyncio.wait_for(stream.push_audio(b"\0\0"), timeout=0.2)
            return await stream.finish()

        self.assertEqual(asyncio.run(scenario()), "NVIDIA")
        self.assertTrue(fallback.stream.closed)

    def test_nemotron_final_does_not_wait_for_hung_whisper_comparison(self) -> None:
        class HangingFinishStream(RecordingStream):
            async def finish(self):
                await asyncio.Event().wait()

        class HangingFinishRecognizer:
            def __init__(self):
                self.stream = None

            async def open_stream(self, on_partial):
                self.stream = HangingFinishStream(on_partial)
                return self.stream

        fallback = HangingFinishRecognizer()
        recognizer = DualTranscriptionRecognizer(
            fallback,
            RecordingRecognizer(final="NVIDIA"),
            comparison_grace_timeout=0.01,
        )

        async def scenario():
            stream = await recognizer.open_stream(lambda _text: asyncio.sleep(0))
            result = await asyncio.wait_for(stream.finish(), timeout=0.2)
            return result, stream.recognition_path

        self.assertEqual(
            asyncio.run(scenario()),
            ("NVIDIA", "nemotron_final"),
        )
        self.assertTrue(fallback.stream.closed)

    def test_dual_recognizer_uses_nemotron_when_whisper_fallback_fails(self) -> None:
        fallback = RecordingRecognizer(fail_at="finish")
        primary = RecordingRecognizer(final="safe nemotron final")
        recognizer = DualTranscriptionRecognizer(fallback, primary)

        async def scenario():
            stream = await recognizer.open_stream(lambda _text: asyncio.sleep(0))
            try:
                return await stream.finish(), stream.recognition_path
            finally:
                await stream.close()

        self.assertEqual(asyncio.run(scenario()), ("safe nemotron final", "nemotron_final"))

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

    def test_missing_service_key_is_rejected_before_accept(self) -> None:
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

    def test_idle_client_deadline_closes_upstream_and_releases_lease(self) -> None:
        async def scenario() -> tuple[FakeWebSocket, FakeRecognizer, bool]:
            lease = VoiceLease()
            recognizer = FakeRecognizer()
            stalled = BlockingReceiveWebSocket(self.token())
            await asyncio.wait_for(
                self.gateway(
                    recognizer,
                    lease=lease,
                    client_idle_timeout_seconds=0.01,
                ).handle(stalled),
                timeout=0.2,
            )
            capacity_available = await lease.acquire()
            if capacity_available:
                lease.release()
            return stalled, recognizer, capacity_available

        stalled, recognizer, capacity_available = asyncio.run(scenario())

        self.assertEqual(stalled.sent[-1]["code"], "UPSTREAM_TIMEOUT")
        self.assertEqual(stalled.close_code, 1011)
        self.assertTrue(recognizer.streams[0].closed)
        self.assertTrue(capacity_available)

    def test_rate_limit_is_shared_by_all_callers(self) -> None:
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
        self.assertEqual(other.close_code, 4429)


if __name__ == "__main__":
    unittest.main()

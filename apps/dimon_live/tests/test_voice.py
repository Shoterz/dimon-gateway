import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from starlette.websockets import WebSocketDisconnect

from apps.dimon_live import voice as voice_module
from apps.dimon_live.session_manager import SessionContext
from apps.dimon_live.voice import (
    NemotronLifecycleClient,
    VoiceBridge,
    VoiceLease,
    VoiceLimitError,
    nemotron_failure_phase,
)
from apps.dimon_live.database import Database


class FakeSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.accepted = False

    async def accept(self, **kwargs):
        self.accepted = True

    async def send_json(self, value):
        self.sent.append(value)

    async def receive(self):
        if not self.messages:
            raise WebSocketDisconnect(code=1000)
        return self.messages.pop(0)


class FakeRecognizer:
    async def transcribe(self, audio, on_partial):
        await on_partial("send John")
        await on_partial("send John Smith S$5")
        return "send John Smith S$5"


def float32_frame(value: float, samples: int = 320) -> bytes:
    import struct

    return struct.pack("<" + "f" * samples, *([value] * samples))


class VoiceTests(unittest.TestCase):
    def test_lifecycle_acquire_returns_distinct_lease_handles(self):
        async def scenario():
            requests = []
            client = NemotronLifecycleClient(Path("/tmp/test-nemotron-control.sock"))

            async def request(payload):
                requests.append(dict(payload))
                return {"ok": True, "status": {"active_sessions": 0}}

            client._request = request
            first = await client.acquire()
            second = await client.acquire()
            await first.release()
            await second.release()
            return client, first, second, requests

        client, first, second, requests = asyncio.run(scenario())
        acquire_ids = [item["lease_id"] for item in requests if item["op"] == "acquire"]
        release_ids = [item["lease_id"] for item in requests if item["op"] == "release"]
        self.assertEqual(len(acquire_ids), 2)
        self.assertNotEqual(first.lease_id, second.lease_id)
        self.assertEqual(acquire_ids, [first.lease_id, second.lease_id])
        self.assertEqual(release_ids, [first.lease_id, second.lease_id])
        self.assertTrue(all(len(value) == 32 for value in acquire_ids))
        self.assertFalse(hasattr(client, "_acquired"))

    def test_lease_heartbeats_with_its_own_id(self):
        async def scenario():
            requests = []

            class Client:
                async def _request(self, payload):
                    requests.append(dict(payload))
                    return {"ok": True, "status": {"active_sessions": 1}}

            lease = voice_module.NemotronLifecycleLease(
                Client(), "a" * 32, heartbeat_seconds=0.001
            )
            lease.start()
            await asyncio.sleep(0.01)
            await lease.release()
            return requests

        requests = asyncio.run(scenario())
        heartbeats = [item for item in requests if item["op"] == "heartbeat"]
        self.assertGreaterEqual(len(heartbeats), 1)
        self.assertTrue(all(item["lease_id"] == "a" * 32 for item in heartbeats))

    def test_lease_release_is_idempotent_and_cancels_heartbeat(self):
        async def scenario():
            requests = []

            class Client:
                async def _request(self, payload):
                    requests.append(dict(payload))
                    return {"ok": True, "status": {"active_sessions": 0}}

            lease = voice_module.NemotronLifecycleLease(
                Client(), "b" * 32, heartbeat_seconds=0.001
            )
            lease.start()
            await lease.release(disconnected=True)
            await lease.release(disconnected=False)
            await asyncio.sleep(0.005)
            return lease, requests

        lease, requests = asyncio.run(scenario())
        releases = [item for item in requests if item["op"] == "release"]
        self.assertEqual(
            releases,
            [{"op": "release", "lease_id": "b" * 32, "disconnected": True}],
        )
        self.assertTrue(lease._heartbeat_task is None or lease._heartbeat_task.done())

    def test_release_failure_is_bounded_and_orphan_can_expire(self):
        async def scenario():
            requests = []

            class Client:
                async def _request(self, payload):
                    requests.append(dict(payload))
                    if payload["op"] == "release":
                        raise RuntimeError("control socket failed")
                    return {"ok": True, "status": {"active_sessions": 1}}

            lease = voice_module.NemotronLifecycleLease(
                Client(), "c" * 32, heartbeat_seconds=0.001
            )
            lease.start()
            with self.assertRaisesRegex(RuntimeError, "control socket"):
                await lease.release()
            count_after_failure = len(requests)
            await lease.release()
            await asyncio.sleep(0.005)
            return lease, requests, count_after_failure

        lease, requests, count_after_failure = asyncio.run(scenario())
        self.assertEqual(len(requests), count_after_failure)
        self.assertEqual(sum(item["op"] == "release" for item in requests), 1)
        self.assertTrue(lease._heartbeat_task is None or lease._heartbeat_task.done())

    def test_cancel_during_acquire_or_release_leaves_no_client_task(self):
        async def scenario():
            acquire_entered = asyncio.Event()
            release_entered = asyncio.Event()

            client = NemotronLifecycleClient(Path("/tmp/test-nemotron-control.sock"))

            async def blocked_acquire(payload):
                acquire_entered.set()
                await asyncio.Event().wait()

            client._request = blocked_acquire
            acquire_task = asyncio.create_task(client.acquire())
            await acquire_entered.wait()
            acquire_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await acquire_task

            async def request(payload):
                if payload["op"] == "release":
                    release_entered.set()
                    await asyncio.Event().wait()
                return {"ok": True, "status": {"active_sessions": 1}}

            client._request = request
            lease = await client.acquire()
            release_task = asyncio.create_task(lease.release())
            await release_entered.wait()
            release_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await release_task
            await asyncio.sleep(0)
            return lease

        lease = asyncio.run(scenario())
        self.assertTrue(lease._heartbeat_task is None or lease._heartbeat_task.done())

    def test_material_partial_final_disagreement_is_not_dispatched(self):
        class DisagreeingRecognizer:
            async def transcribe(self, _audio, on_partial):
                await on_partial("pay my water bill")
                return "PMI Worth Review."

        bridge = VoiceBridge(recognizer=DisagreeingRecognizer(), max_audio_bytes=1024)
        temp = tempfile.TemporaryDirectory()
        dispatched = []
        try:
            database = Database(Path(temp.name) / "demo.sqlite3")
            database.initialize()
            context = SessionContext("visitor", "visitor", database)
            socket = FakeSocket([
                {"type": "websocket.receive", "bytes": b"\0" * 128},
                {"type": "websocket.receive", "text": '{"type":"stop"}'},
            ])

            asyncio.run(
                bridge.handle(
                    socket,
                    context,
                    lambda text: dispatched.append(text) or {"outcome": "ANSWERED"},
                )
            )

            self.assertEqual(dispatched, [])
            conflict = next(event for event in socket.sent if event["type"] == "transcript_conflict")
            self.assertEqual(conflict["live_text"], "pay my water bill")
            self.assertEqual(conflict["final_text"], "PMI Worth Review.")
            result = next(event["result"] for event in socket.sent if event["type"] == "action_result")
            self.assertEqual(result["reason_code"], "TRANSCRIPTION_DISAGREEMENT")
        finally:
            temp.cleanup()

    def test_nemotron_failure_phases_are_stable_and_privacy_safe(self):
        cases = {
            "websocket_connect": "connect",
            "handshake_session_created": "ready_timeout",
            "transcription_receive": "stream_closed",
            "unexpected_transcription_event": "protocol",
            "transcription_final": "empty_final",
            "transcription_error_event": "upstream_error",
        }
        for internal, public in cases.items():
            with self.subTest(internal=internal):
                self.assertEqual(nemotron_failure_phase(internal), public)

    def test_lock_detected_during_dispatch_invalidates_the_socket(self):
        bridge = VoiceBridge(recognizer=FakeRecognizer(), max_audio_bytes=1024)
        temp = tempfile.TemporaryDirectory()
        try:
            database = Database(Path(temp.name) / "demo.sqlite3")
            database.initialize()
            context = SessionContext("visitor", "visitor", database)
            socket = FakeSocket([
                {"type": "websocket.receive", "bytes": float32_frame(0.1, 32)},
                {"type": "websocket.receive", "text": '{"type":"stop"}'},
            ])

            asyncio.run(
                bridge.handle(
                    socket,
                    context,
                    lambda _text: {"outcome": "BLOCKED", "reason_code": "APP_LOCKED"},
                )
            )

            self.assertEqual(socket.sent[-1], {"type": "error", "reason_code": "APP_LOCKED"})
            self.assertFalse(any(message.get("type") == "action_result" for message in socket.sent))
        finally:
            temp.cleanup()

    def test_bridge_releases_the_handle_returned_by_its_acquire(self):
        events = []

        class LifecycleLease:
            async def release(self, *, disconnected=False):
                events.append(f"lifecycle.release:{disconnected}")

        class Lifecycle:
            async def acquire(self):
                events.append("lifecycle.acquire")
                return LifecycleLease()

        class Stream:
            async def push_audio(self, _chunk):
                pass

            async def finish(self):
                return "send John Smith S$5"

            async def close(self):
                pass

        class StreamingRecognizer:
            async def open_stream(self, _on_partial):
                events.append("recognizer.open")
                return Stream()

        bridge = VoiceBridge(
            recognizer=StreamingRecognizer(),
            lifecycle=Lifecycle(),
            max_audio_bytes=1024,
        )
        temp = tempfile.TemporaryDirectory()
        try:
            database = Database(Path(temp.name) / "demo.sqlite3")
            database.initialize()
            context = SessionContext("visitor", "visitor", database)
            socket = FakeSocket([
                {"type": "websocket.receive", "bytes": float32_frame(0.1, 32)},
                {"type": "websocket.receive", "text": '{"type":"stop"}'},
            ])

            asyncio.run(bridge.handle(socket, context, lambda _text: {"outcome": "BLOCKED"}))

            self.assertEqual(
                events,
                ["lifecycle.acquire", "recognizer.open", "lifecycle.release:False"],
            )
        finally:
            temp.cleanup()

    def test_bridge_releases_nemotron_with_disconnect_hint(self):
        releases = []

        class LifecycleLease:
            async def release(self, *, disconnected=False):
                releases.append(disconnected)

        class Lifecycle:
            async def acquire(self):
                return LifecycleLease()

        class Stream:
            async def close(self):
                pass

        class StreamingRecognizer:
            async def open_stream(self, _on_partial):
                return Stream()

        bridge = VoiceBridge(recognizer=StreamingRecognizer(), lifecycle=Lifecycle())
        temp = tempfile.TemporaryDirectory()
        try:
            database = Database(Path(temp.name) / "demo.sqlite3")
            database.initialize()
            context = SessionContext("visitor", "visitor", database)

            asyncio.run(bridge.handle(FakeSocket([]), context, lambda _text: {}))

            self.assertEqual(releases, [True])
        finally:
            temp.cleanup()

    def test_bridge_close_failure_still_releases_lifecycle_and_capacity(self):
        releases = []
        capacity = VoiceLease(1)

        class LifecycleLease:
            async def release(self, *, disconnected=False):
                releases.append(disconnected)

        class Lifecycle:
            async def acquire(self):
                return LifecycleLease()

        class BrokenCloseStream:
            async def close(self):
                raise RuntimeError("upstream close failed")

        class StreamingRecognizer:
            async def open_stream(self, _on_partial):
                return BrokenCloseStream()

        bridge = VoiceBridge(
            recognizer=StreamingRecognizer(),
            lifecycle=Lifecycle(),
            lease=capacity,
        )

        async def scenario():
            with tempfile.TemporaryDirectory() as temporary_directory:
                database = Database(Path(temporary_directory) / "demo.sqlite3")
                database.initialize()
                context = SessionContext("visitor", "visitor", database)
                with self.assertRaisesRegex(RuntimeError, "upstream close failed"):
                    await bridge.handle(FakeSocket([]), context, lambda _text: {})
            available = await capacity.acquire()
            if available:
                capacity.release()
            return available

        self.assertTrue(asyncio.run(scenario()))
        self.assertEqual(releases, [True])

    def test_lease_allows_one_owner_and_releases(self):
        lease = VoiceLease()
        self.assertTrue(asyncio.run(lease.acquire()))
        self.assertFalse(asyncio.run(lease.acquire()))
        lease.release()
        self.assertTrue(asyncio.run(lease.acquire()))

    def test_bridge_rejects_non_float32_audio_without_action(self):
        bridge = VoiceBridge(recognizer=FakeRecognizer(), max_audio_bytes=1024)
        with self.assertRaises(VoiceLimitError):
            bridge.validate_audio(b"bad")

    def test_bridge_emits_final_transcript_and_action_result(self):
        temp = tempfile.TemporaryDirectory()
        try:
            database = Database(Path(temp.name) / "demo.sqlite3")
            database.initialize()
            context = SessionContext("visitor", "visitor", database)
            bridge = VoiceBridge(recognizer=FakeRecognizer(), max_audio_bytes=1024)
            socket = FakeSocket(
                [
                    {"type": "websocket.receive", "bytes": b"\0" * 128},
                    {"type": "websocket.receive", "text": '{"type":"stop"}'},
                ]
            )

            async def action(text):
                return {"outcome": "BLOCKED", "request_id": "voice-request"}

            asyncio.run(bridge.handle(socket, context, action))
            kinds = [event["type"] for event in socket.sent]
            self.assertEqual(kinds[:2], ["ready", "transcript_partial"])
            self.assertIn("transcript_final", kinds)
            self.assertIn("action_result", kinds)
        finally:
            temp.cleanup()

    def test_bridge_normalizes_a_transaction_homophone_before_dispatch(self):
        class MisheardRecognizer:
            async def transcribe(self, _audio, _on_partial):
                return "Let's by Apple shares"

        bridge = VoiceBridge(recognizer=MisheardRecognizer(), max_audio_bytes=1024)
        temp = tempfile.TemporaryDirectory()
        dispatched = []
        try:
            database = Database(Path(temp.name) / "demo.sqlite3")
            database.initialize()
            context = SessionContext("visitor", "visitor", database)
            socket = FakeSocket([
                {"type": "websocket.receive", "bytes": b"\0" * 128},
                {"type": "websocket.receive", "text": '{"type":"stop"}'},
            ])

            def action(text, evidence):
                dispatched.append((text, evidence))
                return {"outcome": "ANSWERED"}

            asyncio.run(bridge.handle(socket, context, action))

            self.assertEqual(dispatched[0][0], "Let's buy Apple shares")
            self.assertEqual(dispatched[0][1]["transcript"], "Let's by Apple shares")
            self.assertEqual(dispatched[0][1]["normalized_transcript"], "Let's buy Apple shares")
        finally:
            temp.cleanup()

    def test_second_socket_gets_busy_without_action(self):
        bridge = VoiceBridge(recognizer=FakeRecognizer(), max_audio_bytes=1024)
        first = asyncio.run(bridge.lease.acquire())
        self.assertTrue(first)
        second = FakeSocket([])
        temp = tempfile.TemporaryDirectory()
        try:
            database = Database(Path(temp.name) / "demo.sqlite3")
            database.initialize()
            context = SessionContext("visitor", "visitor", database)
            asyncio.run(bridge.handle(second, context, lambda text: None))
            self.assertEqual(second.sent[0]["reason_code"], "VOICE_BUSY")
        finally:
            bridge.lease.release()
            temp.cleanup()

    def test_bridge_auto_commits_after_endpoint_silence(self):
        class RecordingRecognizer(FakeRecognizer):
            def __init__(self):
                self.audio = b""

            async def transcribe(self, audio, on_partial):
                self.audio = audio
                return await super().transcribe(audio, on_partial)

        recognizer = RecordingRecognizer()
        bridge = VoiceBridge(
            recognizer=recognizer,
            max_audio_bytes=100_000,
            endpoint_silence_seconds=1.4,
        )
        temp = tempfile.TemporaryDirectory()
        try:
            database = Database(Path(temp.name) / "demo.sqlite3")
            database.initialize()
            context = SessionContext("visitor", "visitor", database)
            socket = FakeSocket(
                [
                    {"type": "websocket.receive", "bytes": float32_frame(0.1)}
                    for _ in range(8)
                ]
                + [
                    {"type": "websocket.receive", "bytes": float32_frame(0.0)}
                    for _ in range(70)
                ]
            )

            async def action(text):
                return {"outcome": "BLOCKED", "request_id": "voice-request"}

            asyncio.run(bridge.handle(socket, context, action))
            kinds = [event["type"] for event in socket.sent]
            self.assertIn("endpoint", kinds)
            self.assertIn("transcript_final", kinds)
            self.assertIn("action_result", kinds)
            self.assertGreater(len(recognizer.audio), 0)
        finally:
            temp.cleanup()

    def test_bridge_streams_audio_and_finishes_recognition_at_endpoint(self):
        class Stream:
            def __init__(self):
                self.chunks = []
                self.finished = False

            async def push_audio(self, chunk):
                self.chunks.append(chunk)

            async def finish(self):
                self.finished = True
                return "send John Smith S$5"

        class StreamingRecognizer:
            def __init__(self):
                self.stream = Stream()

            async def open_stream(self, on_partial):
                return self.stream

        recognizer = StreamingRecognizer()
        bridge = VoiceBridge(
            recognizer=recognizer,
            max_audio_bytes=100_000,
            endpoint_silence_seconds=1.4,
        )
        temp = tempfile.TemporaryDirectory()
        try:
            database = Database(Path(temp.name) / "demo.sqlite3")
            database.initialize()
            context = SessionContext("visitor", "visitor", database)
            socket = FakeSocket(
                [
                    {"type": "websocket.receive", "bytes": float32_frame(0.1)}
                    for _ in range(8)
                ]
                + [
                    {"type": "websocket.receive", "bytes": float32_frame(0.0)}
                    for _ in range(70)
                ]
            )

            async def action(text):
                return {"outcome": "BLOCKED", "request_id": "voice-request"}

            asyncio.run(bridge.handle(socket, context, action))
            self.assertTrue(recognizer.stream.chunks)
            self.assertTrue(recognizer.stream.finished)
            self.assertIn("action_result", [event["type"] for event in socket.sent])
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()

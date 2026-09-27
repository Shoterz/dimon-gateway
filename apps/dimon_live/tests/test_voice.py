import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from starlette.websockets import WebSocketDisconnect

from apps.dimon_live.session_manager import SessionContext
from apps.dimon_live.voice import VoiceBridge, VoiceLease, VoiceLimitError
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
    def test_bridge_acquires_nemotron_before_stream_and_releases_afterward(self):
        events = []

        class Lifecycle:
            async def acquire(self):
                events.append("lifecycle.acquire")

            async def release(self, *, disconnected=False):
                events.append(f"lifecycle.release:{disconnected}")

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

        class Lifecycle:
            async def acquire(self):
                pass

            async def release(self, *, disconnected=False):
                releases.append(disconnected)

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

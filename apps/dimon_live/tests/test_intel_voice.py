from __future__ import annotations

import asyncio
import json
import unittest

from websockets.asyncio.server import serve

from apps.dimon_live.intel_voice import WhisperClient
from apps.dimon_live.voice import VoiceError


async def _run_fake_server(handler):
    server = await serve(handler, "127.0.0.1", 0)
    address = server.sockets[0].getsockname()
    return server, f"ws://{address[0]}:{address[1]}"


class WhisperClientTests(unittest.TestCase):
    def test_transcribe_reports_partial_then_returns_final(self):
        partials_received = []

        async def handler(websocket):
            await websocket.send(json.dumps({"type": "ready", "sample_rate": 16000}))
            await websocket.recv()  # audio bytes pushed by the client
            await websocket.send(json.dumps({"type": "partial", "text": "send John"}))
            message = json.loads(await websocket.recv())
            assert message["type"] == "commit"
            await websocket.send(
                json.dumps({"type": "final", "text": "send John Smith S$5"})
            )

        async def scenario():
            server, url = await _run_fake_server(handler)
            try:
                client = WhisperClient(url)

                async def on_partial(text: str) -> None:
                    partials_received.append(text)

                return await client.transcribe(b"\x00\x00\x00\x00" * 8, on_partial)
            finally:
                server.close()
                await server.wait_closed()

        result = asyncio.run(scenario())
        self.assertEqual(result, "send John Smith S$5")
        self.assertEqual(partials_received, ["send John"])

    def test_transcribe_raises_when_server_reports_error(self):
        async def handler(websocket):
            await websocket.send(json.dumps({"type": "ready", "sample_rate": 16000}))
            await websocket.recv()
            await websocket.recv()  # commit
            await websocket.send(
                json.dumps({"type": "error", "reason_code": "MODEL_UNAVAILABLE"})
            )

        async def scenario():
            server, url = await _run_fake_server(handler)
            try:
                client = WhisperClient(url)

                async def on_partial(_text: str) -> None:
                    pass

                await client.transcribe(b"\x00\x00\x00\x00", on_partial)
            finally:
                server.close()
                await server.wait_closed()

        with self.assertRaises(VoiceError) as raised:
            asyncio.run(scenario())
        self.assertEqual(str(raised.exception), "WHISPER_UNAVAILABLE")

    def test_transcribe_raises_when_server_is_unreachable(self):
        async def scenario():
            client = WhisperClient("ws://127.0.0.1:65501")

            async def on_partial(_text: str) -> None:
                pass

            await client.transcribe(b"\x00\x00\x00\x00", on_partial)

        with self.assertRaises(VoiceError) as raised:
            asyncio.run(scenario())
        self.assertEqual(str(raised.exception), "WHISPER_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()

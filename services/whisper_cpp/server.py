"""Minimal loopback-only whisper.cpp transcription service owned by this repository.

Unlike services/whisper_intel (which loads OpenVINO in-process), this service
shells out to a downloaded whisper.cpp CLI binary per redecode window.
whisper.cpp's Vulkan backend runs on any GPU vendor with a working Vulkan
driver (NVIDIA, AMD, Intel), and falls back to CPU automatically if none is
available — so this one binary/service works regardless of GPU vendor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from fastapi import FastAPI, WebSocket

SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2  # 16-bit PCM
PARTIAL_INTERVAL_BYTES = int(1.0 * SAMPLE_RATE * BYTES_PER_SAMPLE)  # ~1.0s of new audio
WINDOW_BYTES = 8 * SAMPLE_RATE * BYTES_PER_SAMPLE  # redecode at most the last ~8s
PARTIAL_MAX_NEW_TOKENS = 96
FINAL_MAX_NEW_TOKENS = 256


@dataclass(frozen=True)
class WhisperPaths:
    binary: str
    model: str


class WhisperEngine:
    def __init__(self, paths: WhisperPaths):
        self.paths = paths

    def transcribe(self, pcm16_audio: bytes, *, max_new_tokens: int = FINAL_MAX_NEW_TOKENS) -> str:
        # whisper.cpp's CLI has no per-call token-count control the way the
        # OpenVINO engine does; max_new_tokens is accepted for call-site
        # parity with services/whisper_intel/server.py's WhisperEngine and is
        # currently unused.
        del max_new_tokens
        if not pcm16_audio:
            return ""
        handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_path = handle.name
        handle.close()
        try:
            with wave.open(tmp_path, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(BYTES_PER_SAMPLE)
                wav_file.setframerate(SAMPLE_RATE)
                wav_file.writeframes(pcm16_audio)
            result = subprocess.run(
                [self.paths.binary, "-m", self.paths.model, "-f", tmp_path, "-nt", "-l", "en"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        finally:
            os.unlink(tmp_path)
        if result.returncode != 0:
            raise RuntimeError("whisper.cpp exited non-zero")
        return " ".join(line.strip() for line in result.stdout.splitlines() if line.strip())


def create_app(engine):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return {"status": "ready"}

    @app.websocket("/v1/stream")
    async def stream(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text(json.dumps({"type": "ready", "sample_rate": SAMPLE_RATE}))
        buffer = bytearray()
        since_partial = 0
        # A CPU decode takes seconds; running it inline would stop reading
        # audio, back up the socket, and starve keepalive pings. Decode
        # partials in the background instead, one at a time, and skip
        # intervals that arrive while one is still running.
        partial_task: asyncio.Task | None = None

        async def send_partial(window: bytes) -> None:
            try:
                text = await asyncio.to_thread(
                    engine.transcribe, window, max_new_tokens=PARTIAL_MAX_NEW_TOKENS
                )
            except Exception:
                await websocket.send_text(json.dumps(
                    {"type": "error", "reason_code": "TRANSCRIPTION_UNAVAILABLE"}
                ))
                await websocket.close()
                return
            await websocket.send_text(json.dumps({"type": "partial", "text": text}))

        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    chunk = message["bytes"]
                    buffer.extend(chunk)
                    since_partial += len(chunk)
                    if since_partial >= PARTIAL_INTERVAL_BYTES and (
                        partial_task is None or partial_task.done()
                    ):
                        since_partial = 0
                        partial_task = asyncio.create_task(
                            send_partial(bytes(buffer[-WINDOW_BYTES:]))
                        )
                    continue
                if partial_task is not None:
                    await partial_task
                raw_text = message.get("text")
                if raw_text is None:
                    await websocket.send_text(json.dumps({"type": "error", "reason_code": "MALFORMED_FRAME"}))
                    return
                try:
                    command = json.loads(raw_text)
                except json.JSONDecodeError:
                    await websocket.send_text(json.dumps({"type": "error", "reason_code": "MALFORMED_FRAME"}))
                    return
                if command.get("type") != "commit":
                    await websocket.send_text(json.dumps({"type": "error", "reason_code": "MALFORMED_FRAME"}))
                    return
                try:
                    final_text = await asyncio.to_thread(engine.transcribe, bytes(buffer))
                except Exception:
                    await websocket.send_text(json.dumps(
                        {"type": "error", "reason_code": "TRANSCRIPTION_UNAVAILABLE"}
                    ))
                    return
                await websocket.send_text(json.dumps({"type": "final", "text": final_text}))
                return
        except Exception:
            return
        finally:
            if partial_task is not None and not partial_task.done():
                partial_task.cancel()

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", choices=("127.0.0.1",), required=True)
    # Loopback-only is the boundary; the port may move off a busy default.
    parser.add_argument("--port", type=int, choices=range(1024, 65536), metavar="PORT", required=True)
    args = parser.parse_args()
    engine = WhisperEngine(WhisperPaths(binary=args.binary, model=args.model))
    import uvicorn

    uvicorn.run(create_app(engine), host=args.host, port=args.port)


if __name__ == "__main__":
    main()

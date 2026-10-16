"""Minimal loopback-only whisper.cpp transcription service owned by this repository.

Unlike services/whisper_intel (which loads OpenVINO in-process), this service
runs the downloaded whisper.cpp binaries. When whisper-server sits next to the
CLI it is started once and keeps the model loaded, so each utterance costs
only the decode; otherwise the CLI is run per redecode window, which reloads
the model every time (several seconds on a CPU).
whisper.cpp's Vulkan backend runs on any GPU vendor with a working Vulkan
driver (NVIDIA, AMD, Intel), and falls back to CPU automatically if none is
available — so this one binary/service works regardless of GPU vendor.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import io
import json
import os
import socket
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx
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


def _wav(pcm16_audio: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(BYTES_PER_SAMPLE)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(pcm16_audio)
    return output.getvalue()


def _free_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ResidentWhisperEngine:
    """whisper-server started once on a private loopback port, model kept loaded."""

    def __init__(self, server_binary: str, model: str, *, threads: int | None = None,
                 audio_ctx: int = 0, startup_timeout: float = 120.0):
        self.port = _free_loopback_port()
        threads = threads or max(1, min(8, (os.cpu_count() or 4) // 2))
        # A smaller audio context decodes short clips several times faster
        # (384 covers ~7.7s), at the cost of ignoring anything longer.
        extra = ["-ac", str(audio_ctx)] if audio_ctx > 0 else []
        self.process = subprocess.Popen(
            [server_binary, "-m", model, "-l", "en", "-nt", "-t", str(threads), *extra,
             "--host", "127.0.0.1", "--port", str(self.port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        atexit.register(self.close)
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=60)
        deadline = time.monotonic() + startup_timeout
        while True:
            if self.process.poll() is not None:
                raise RuntimeError("whisper-server exited while loading the model")
            try:
                self.client.get("/")
                break
            except httpx.TransportError:
                if time.monotonic() >= deadline:
                    self.close()
                    raise RuntimeError("whisper-server did not start") from None
                time.sleep(0.25)

    def transcribe(self, pcm16_audio: bytes, *, max_new_tokens: int = FINAL_MAX_NEW_TOKENS) -> str:
        del max_new_tokens
        if not pcm16_audio:
            return ""
        response = self.client.post(
            "/inference",
            files={"file": ("audio.wav", _wav(pcm16_audio), "audio/wav")},
            data={"response_format": "json", "temperature": "0.0"},
        )
        response.raise_for_status()
        return " ".join(response.json().get("text", "").split())

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


def build_engine(binary: str, model: str, *, audio_ctx: int = 0):
    """The resident server when it ships beside the CLI, else the CLI per call."""

    cli = Path(binary)
    server = cli.with_name(cli.name.replace("whisper-cli", "whisper-server"))
    if server != cli and server.is_file():
        try:
            return ResidentWhisperEngine(str(server), model, audio_ctx=audio_ctx)
        except RuntimeError:
            pass
    return WhisperEngine(WhisperPaths(binary=binary, model=model))


def create_app(engine, *, partials: bool = True):
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
                    # Wake-word listening needs only the final text; partials
                    # would just queue in front of it on the one decoder.
                    if partials and since_partial >= PARTIAL_INTERVAL_BYTES and (
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
    parser.add_argument("--audio-ctx", type=int, default=0)
    parser.add_argument("--no-partials", action="store_true")
    args = parser.parse_args()
    engine = build_engine(args.binary, args.model, audio_ctx=max(0, args.audio_ctx))
    import uvicorn

    uvicorn.run(create_app(engine, partials=not args.no_partials), host=args.host, port=args.port)


if __name__ == "__main__":
    main()

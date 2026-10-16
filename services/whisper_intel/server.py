"""Minimal loopback-only Intel Arc Whisper transcription service owned by this repository."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from fastapi import FastAPI, WebSocket

SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2  # 16-bit PCM
PARTIAL_INTERVAL_BYTES = int(1.5 * SAMPLE_RATE * BYTES_PER_SAMPLE)  # ~1.5s of new audio
WINDOW_BYTES = 20 * SAMPLE_RATE * BYTES_PER_SAMPLE  # redecode at most the last ~20s


@dataclass(frozen=True)
class WhisperPaths:
    model: str


class WhisperEngine:
    def __init__(self, paths: WhisperPaths, device: str):
        from optimum.intel.openvino import OVModelForSpeechSeq2Seq
        from transformers import AutoProcessor

        self.processor = AutoProcessor.from_pretrained(paths.model)
        self.model = OVModelForSpeechSeq2Seq.from_pretrained(paths.model, device=device)

    def transcribe(self, pcm16_audio: bytes) -> str:
        import numpy as np

        if not pcm16_audio:
            return ""
        samples = np.frombuffer(pcm16_audio, dtype="<i2").astype(np.float32) / 32768.0
        inputs = self.processor(samples, sampling_rate=SAMPLE_RATE, return_tensors="pt")
        generated_ids = self.model.generate(inputs["input_features"])
        text = self.processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
        return text.strip()


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
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    chunk = message["bytes"]
                    buffer.extend(chunk)
                    since_partial += len(chunk)
                    if since_partial >= PARTIAL_INTERVAL_BYTES:
                        since_partial = 0
                        window = bytes(buffer[-WINDOW_BYTES:])
                        try:
                            text = await asyncio.to_thread(engine.transcribe, window)
                        except Exception:
                            await websocket.send_text(json.dumps(
                                {"type": "error", "reason_code": "TRANSCRIPTION_UNAVAILABLE"}
                            ))
                            return
                        await websocket.send_text(json.dumps({"type": "partial", "text": text}))
                    continue
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

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", default="GPU")
    parser.add_argument("--host", choices=("127.0.0.1",), required=True)
    parser.add_argument("--port", type=int, choices=(8005,), required=True)
    args = parser.parse_args()
    engine = WhisperEngine(WhisperPaths(args.model), args.device)
    import uvicorn

    uvicorn.run(create_app(engine), host=args.host, port=args.port)


if __name__ == "__main__":
    main()

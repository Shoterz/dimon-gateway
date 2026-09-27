"""Minimal loopback-only Kokoro synthesis service owned by this repository."""

from __future__ import annotations

import argparse
import io
import wave
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1_000)


@dataclass(frozen=True)
class KokoroPaths:
    model: str
    config: str
    voice: str


class KokoroEngine:
    def __init__(self, paths: KokoroPaths):
        import torch
        from kokoro import KModel, KPipeline

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = KModel(config=paths.config, model=paths.model).to(device).eval()
        pipeline = KPipeline(
            lang_code="a",
            repo_id="hexgrad/Kokoro-82M",
            model=model,
            device=device,
        )
        pipeline.voices["af_heart"] = torch.load(
            paths.voice, map_location="cpu", weights_only=True
        )
        self.pipeline = pipeline

    def synthesize(self, text: str) -> bytes:
        import numpy as np

        segments = []
        for result in self.pipeline(text, voice="af_heart", speed=1.15):
            if result.audio is not None:
                segments.append(result.audio.detach().cpu().numpy())
        if not segments:
            raise RuntimeError("KOKORO_EMPTY_OUTPUT")
        audio = np.clip(np.concatenate(segments), -1.0, 1.0)
        pcm16 = (audio * 32767).astype("<i2", copy=False).tobytes()
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24_000)
            wav.writeframes(pcm16)
        return output.getvalue()


def create_app(engine: KokoroEngine):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import Response
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return {"status": "ready"}

    @app.post("/v1/speech")
    async def speech(request: SpeechRequest):
        try:
            # The gateway admits one synthesis at a time, so this dedicated
            # process deliberately performs the model call synchronously.
            wav = engine.synthesize(request.text)
        except Exception as error:
            raise HTTPException(status_code=503, detail="SYNTHESIS_UNAVAILABLE") from error
        return Response(wav, media_type="audio/wav")

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--voice", required=True)
    parser.add_argument("--host", choices=("127.0.0.1",), required=True)
    parser.add_argument("--port", type=int, choices=(3001,), required=True)
    args = parser.parse_args()
    engine = KokoroEngine(KokoroPaths(args.model, args.config, args.voice))
    import uvicorn

    uvicorn.run(create_app(engine), host=args.host, port=args.port)


if __name__ == "__main__":
    main()

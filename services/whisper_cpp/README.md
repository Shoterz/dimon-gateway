# Whisper (vendor-agnostic, whisper.cpp) service

This repository-owned FastAPI service shells out to a downloaded whisper.cpp
Vulkan-build CLI binary and exposes only a bounded transcription WebSocket and
a health route on `127.0.0.1:8006`. It exists as a drop-in replacement for the
Nemotron recognizer when `DIMON_STT_BACKEND=whisper_cpp`, for developers
running local mode with any GPU vendor (NVIDIA, AMD, Intel) or no GPU at all —
whisper.cpp's Vulkan backend auto-selects a GPU when a working Vulkan driver
is present and falls back to CPU otherwise.

Set `DIMON_WHISPER_CPP_MODEL_PATH` to a whisper.cpp `ggml-*.bin` model file
(e.g. `ggml-small.bin` from the `ggml-org/whisper.cpp` releases — not the
OpenVINO IR files used by `services/whisper_intel`) and
`DIMON_WHISPER_CPP_BINARY_PATH` to the `whisper-cli` executable from a
downloaded whisper.cpp release. Unlike `services/whisper_intel`, no separate
Python environment is required — this service only needs `fastapi` and
`uvicorn`, already present in the repo's own `uv`-managed environment, and
runs via `uv run python`. `apps/dimon_live/provider_factory.py` connects to
this service via `DIMON_WHISPER_CPP_URL` (default
`ws://127.0.0.1:8006/v1/stream`); its client-side connect and result timeouts
are configurable via `DIMON_WHISPER_CPP_CONNECT_TIMEOUT` (default `3`
seconds) and `DIMON_WHISPER_CPP_RESULT_TIMEOUT` (default `90` seconds, kept
comfortably above the server's 60-second CLI subprocess timeout so a slow
CPU-fallback decode isn't mistaken for an unavailable service).

`./run.sh --check` (or `.\run.ps1 -Check` on Windows) validates the model and
binary paths without transcribing anything. The model and binary remain
outside Git, matching every other model service in this repo.

The WebSocket protocol at `/v1/stream` is identical to
`services/whisper_intel`'s (owned entirely by this repository, not tied to
either engine): the server sends `{"type": "ready", "sample_rate": 16000}`,
the client streams binary 16 kHz mono PCM16 frames, the server periodically
sends `{"type": "partial", "text": "..."}` as it redecodes the buffered
audio, the client sends `{"type": "commit"}`, and the server replies with
`{"type": "final", "text": "..."}` before closing.

# Whisper (Intel Arc) service

This repository-owned FastAPI service loads an OpenVINO Whisper model and exposes only a bounded transcription WebSocket and a health route on `127.0.0.1:8005`. It exists as a drop-in replacement for the Nemotron recognizer when `DIMON_STT_BACKEND=whisper_intel`, for developers running local mode on an Intel Arc GPU instead of an NVIDIA host.

Set `DIMON_WHISPER_MODEL_PATH` to a local OpenVINO Whisper model directory (an `optimum-intel`-exported model, e.g. `whisper-small`) and `DIMON_WHISPER_PYTHON` to an external Python environment containing `optimum[openvino]`, `transformers`, `fastapi`, `uvicorn`, and `numpy`. `DIMON_WHISPER_DEVICE` defaults to `GPU` and accepts any OpenVINO device string (`GPU`, `CPU`).

`./run.sh --check` validates the model path and runtime executable without loading the model. The model, config, and Python environment remain outside Git, matching the Qwen and Kokoro services.

The WebSocket protocol at `/v1/stream` is owned entirely by this repository (not a copy of Nemotron's wire format): the server sends `{"type": "ready", "sample_rate": 16000}`, the client streams binary 16 kHz mono PCM16 frames, the server periodically sends `{"type": "partial", "text": "..."}` as it redecodes the buffered audio, the client sends `{"type": "commit"}`, and the server replies with `{"type": "final", "text": "..."}` before closing.

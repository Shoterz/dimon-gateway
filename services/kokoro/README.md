# Kokoro service

This launcher wraps a compatible Kokoro application and forces its Gradio API to `127.0.0.1:3001`. Set `DIMON_KOKORO_MODEL_PATH` and `DIMON_KOKORO_VOICE_PATH` to the verified model and `af_heart.pt`, `DIMON_KOKORO_APP_DIR` to the compatible application checkout, and `DIMON_KOKORO_PYTHON` to that environment's Python executable.

The model and voice are checked before startup and remain outside Git. `./run.sh --check` validates the runtime without importing the model or allocating GPU memory.

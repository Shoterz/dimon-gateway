# Kokoro service

This repository-owned FastAPI service loads Kokoro and exposes only bounded synthesis and health routes on `127.0.0.1:3001`. Set `DIMON_KOKORO_MODEL_PATH`, `DIMON_KOKORO_CONFIG_PATH`, and `DIMON_KOKORO_VOICE_PATH` to the pinned model, config, and `af_heart.pt`. Set `DIMON_KOKORO_PYTHON` to an external Python environment containing Kokoro, PyTorch, FastAPI, Uvicorn, and NumPy.

The model, config, voice, Python environment, and generated audio remain outside Git. `./run.sh --check` validates the files and executable without importing the model or allocating GPU memory. The service does not import code or configuration from the personal Jarvis, llama, or Kokoro projects.

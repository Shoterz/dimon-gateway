# Qwen service

This launcher starts the pinned Qwen GGUF through an externally installed `llama-server`. It accepts no runtime argument passthrough and always binds to `127.0.0.1:8080` with one slot and a 16K context, a conservative profile for the shared GPU.

Set `DIMON_QWEN_MODEL_PATH` to the verified GGUF and `DIMON_LLAMA_SERVER_PATH` to the executable. Run `./run.sh --check` before starting it. The launcher never downloads or copies the model.

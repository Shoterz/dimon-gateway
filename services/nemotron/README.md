# Nemotron service

This launcher starts NVIDIA NeMo-Speech.cpp with the pinned `nemotron-en` artifact on `127.0.0.1:8004`. Set `DIMON_NEMOTRON_MODEL_PATH` to the verified GGUF, `DIMON_NEMO_SPEECH_PATH` to the pinned runtime executable, and `DIMON_NEMO_SPEECH_MODEL_DIR` to its model store. `./run.sh --check` validates these inputs without loading CUDA.

The checked runtime revision is NeMo-Speech.cpp v0.1.0. The model store and executable remain outside Git and are shared with any existing service when using shared mode.

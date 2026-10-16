# Model provenance

Dimon stores model weights outside Git and refers to each artifact through an environment variable. `config/models.example.yaml` records the upstream source, immutable revision, license identifier, exact byte size, SHA-256 digest, and loopback port for the GPU host's single-file artifacts. Copy it to the ignored `config/models.yaml` for a GPU host; do not replace revisions with branch names.

| Capability | Upstream artifact | Revision | License | Bytes | SHA-256 | Local path variable |
| --- | --- | --- | --- | ---: | --- | --- |
| Intent | `Huihui-Qwen3.5-9B-abliterated.Q5_K_M.gguf` | `9f646d7eda193ddf2348134f3bff3d49eed7a2c6` | Apache-2.0 | 6,522,004,864 | `946072b16f5d672e60357410f900888f6522b3ae49f241b5bf2142cb89637fb6` | `DIMON_QWEN_MODEL_PATH` |
| Streaming speech recognition | `nemotron-speech-streaming-en-0.6b.q8_0.gguf` | `ebe59e5a817142986528bbbee5dba8db7b38ed50` | NVIDIA Open Model License | 699,872,960 | `d9a01898d2a611c8764e23a1c2f45e70bbd5a425dc4de93692ac951dd603812d` | `DIMON_NEMOTRON_MODEL_PATH` |
| Speech synthesis | `kokoro-v1_0.pth` | `f3ff3571791e39611d31c381e3a41a3af07b4987` | Apache-2.0 | 327,212,226 | `496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4` | `DIMON_KOKORO_MODEL_PATH` |

The repository does not redistribute these artifacts. The host operator obtains them from the recorded upstream, accepts the applicable license, and stores one copy anywhere outside the repository. Shared and standalone modes use the same path variables, so changing modes does not duplicate weights.

Faster-Whisper Medium is a multi-file snapshot rather than one artifact. Its
immutable upstream revision is recorded in
`services/faster_whisper_cuda/artifacts.json`, and its Python/CUDA dependency
graph is hash-locked in `services/faster_whisper_cuda/requirements.lock`.
Windows face, voiceprint, Qwen, whisper.cpp, and Kokoro downloads are pinned by
URL, byte size, and SHA-256 in `scripts/windows/artifacts.psd1`.

Verify a configured host with:

```bash
cp config/models.example.yaml config/models.yaml
scripts/check-model-manifest --manifest config/models.yaml --json
```

The verifier streams each file through SHA-256 and never includes configured paths in its output. Updating a model requires a new immutable revision, measured byte size, and digest in the example manifest and this table.

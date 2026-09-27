# Third-party notices

The MIT license in this repository applies only to repository-owned code and
documentation. External models, model weights, runtimes, and libraries retain
their own copyright and license terms. Model weights and compiled runtimes are
not redistributed in this repository.

Before downloading or deploying an artifact, verify the license attached to
the exact revision. The project model manifest will record immutable revisions,
checksums, byte sizes, and license sources during the GPU-host checkpoint.

## Models and inference runtimes

| Component | Upstream source | License source |
| --- | --- | --- |
| Qwen model family | [QwenLM](https://github.com/QwenLM) | Verify the model card for the selected Qwen revision. |
| Huihui Qwen3.5 GGUF used by the current host | [model repository](https://huggingface.co/mradermacher/Huihui-Qwen3.5-9B-abliterated-GGUF) | Verify the repository model card and the base-model terms. |
| NVIDIA Nemotron/NeMo speech runtime | [NVIDIA NeMo](https://github.com/NVIDIA-NeMo/NeMo) | See the repository license and the selected speech-model card. |
| Kokoro 82M | [hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) | See the model card and files for the pinned revision. |
| llama.cpp | [ggerganov/llama.cpp](https://github.com/ggml-org/llama.cpp) | See the upstream `LICENSE`. |

## Python dependencies

Runtime dependencies are resolved exactly in `uv.lock`. Their upstream license
files remain authoritative:

- [FastAPI](https://github.com/fastapi/fastapi)
- [HTTPX](https://github.com/encode/httpx)
- [Pydantic](https://github.com/pydantic/pydantic)
- [python-dotenv](https://github.com/theskumar/python-dotenv)
- [Uvicorn](https://github.com/encode/uvicorn)
- [websockets](https://github.com/python-websockets/websockets)

Development and transitive dependencies are listed in `uv.lock`. Distribution
of a release should include any notices required by the exact dependency set.
# Model artifacts and GPU runtimes

Dimon does not redistribute model weights or compiled GPU runtimes. Optional host-side components are obtained separately under their upstream terms:

- Huihui Qwen3.5 9B abliterated GGUF, Apache-2.0.
- NVIDIA Nemotron Speech Streaming 0.6B, NVIDIA Open Model License.
- Kokoro-82M, Apache-2.0.
- llama.cpp, MIT License.
- NVIDIA NeMo-Speech.cpp, Apache-2.0, with NVIDIA runtime dependencies subject to their own terms.

Exact artifact sources and immutable revisions are listed in `docs/model-provenance.md`.

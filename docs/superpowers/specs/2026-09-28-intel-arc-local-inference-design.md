# Intel Arc local inference

**Date:** 2026-09-28
**Status:** proposed design for review
**Branch:** `backend-edits`

## Purpose

Let a developer whose laptop has an Intel Arc iGPU (no NVIDIA/CUDA) run all
three inference capabilities — intent extraction, transcription, and speech —
locally, instead of being limited to `fixture` mode or a teammate's `remote`
gateway. The existing NVIDIA-host `local` path, `remote` mode, `fixture` mode,
and the shared GPU host supervisor are unaffected; this is additive.

## Background

`apps/dimon_live/provider_factory.py` already treats `local` mode as "call
whatever URLs are configured" — it does not depend on `services/gpu_host`'s
capacity broker or process supervisor. That broker calls `nvidia-smi` directly
(`services/gpu_host/capacity.py`) and has no Intel equivalent readily
available, especially under WSL2. Because `local` mode never touches it, an
Intel-only local stack is reachable without going anywhere near that code.

Of the three model services:

- **Qwen** (`services/qwen`) is served by `llama-server`, whose
  `/v1/chat/completions` endpoint `model_adapter.py` calls. llama.cpp has
  Vulkan and SYCL backends that run on Intel Arc; `--n-gpu-layers auto` and the
  rest of `run.sh`'s flags are backend-agnostic. This needs a different
  `llama-server` binary, not different code.
- **Kokoro** (`services/kokoro`) is a repo-owned FastAPI service wrapping
  plain PyTorch. It already runs fine on CPU; no GPU dependency to fix.
- **Nemotron** (`services/nemotron`) wraps NVIDIA's NeMo-Speech.cpp — a
  CUDA-only streaming ASR runtime with no Intel Arc build. This is the one
  component that must be replaced, not ported.

`apps/dimon_live/inference.py`'s `Recognizer` Protocol only requires
`transcribe(audio, on_partial) -> str`; `open_stream` is optional and probed
with `getattr(..., None)` in `voice.py`'s `VoiceBridge.handle`. A replacement
recognizer does not need to speak Nemotron's WebSocket wire protocol — it only
needs to satisfy this Python-level contract.

## Selected approach

Add one new repo-owned service and one new client module, wired in through a
single additive environment variable. Nothing about `fixture`, `remote`, or
the existing NVIDIA `local` path changes.

### Qwen — binary swap only

No code changes. Point `DIMON_LLAMA_SERVER_PATH` at a Vulkan- or SYCL-built
`llama-server` and `DIMON_QWEN_MODEL_PATH` at the same pinned GGUF.
`services/qwen/run.sh` and `model_adapter.py` are unchanged.

### Kokoro — no changes

Runs as-is on CPU.

### `services/whisper_intel/` (new)

A small FastAPI service, structurally the sibling of
`services/kokoro/server.py`:

- Loads an OpenVINO INT8 Whisper model onto the Arc GPU at startup. Default
  size is `whisper-small` (good accuracy/latency balance for the Q5 Qwen
  model's memory footprint sharing the same 16 GiB unified memory); the
  model path is configurable via `DIMON_WHISPER_MODEL_PATH` for anyone who
  wants to trade down to `whisper-base` for speed.
- Exposes one WebSocket endpoint speaking a minimal protocol we define and
  control on both ends:
  1. server sends `{"type": "ready", "sample_rate": 16000}`
  2. client streams binary 16 kHz mono PCM16 frames
  3. server periodically sends `{"type": "partial", "text": "..."}` as it
     redecodes the growing buffer
  4. client sends `{"type": "commit"}`
  5. server sends `{"type": "final", "text": "..."}` and closes
- `run.sh --check` validates the model path and OpenVINO device availability
  without loading the model, following the same convention as
  `services/qwen/run.sh --check` and `services/nemotron/run.sh --check`.
- `README.md` documents required env vars, following the sibling services'
  terse style.

### `apps/dimon_live/intel_voice.py` (new)

`WhisperClient` / `WhisperStream`, structurally a near-clone of
`voice.py`'s `NemotronClient` / `NemotronStream` (same `push_audio` /
`finish` / `close` shape), so it satisfies the existing `Recognizer` Protocol
with zero changes to `voice.py` or `VoiceBridge`. No `VoiceLifecycle`
implementation is needed — like Kokoro, the service just stays loaded.

**Partial-caption strategy:** `WhisperStream.push_audio` buffers incoming
audio. Roughly every ~1.5s of new audio, it redecodes the buffered audio
(windowed to the last ~20s) in a background thread via `asyncio.to_thread`
and calls `on_partial` with the updated text — the standard
redecode-the-buffer approach for Whisper-family live captions, since Whisper
is not natively a token-streaming model the way Nemotron was. `finish()` does
one final full decode over the complete buffer for the returned transcript.

### `provider_factory.py` — one additive branch

A new env var, `DIMON_STT_BACKEND`, defaulting to `nemotron`, is consulted
only inside the existing `local` branch. When it's `whisper_intel`, build
`WhisperClient` (pointed at `DIMON_WHISPER_URL`, default
`ws://127.0.0.1:8005/v1/stream`) instead of `NemotronClient`, and pass
`voice_lifecycle=None`. Qwen and Kokoro construction in `local` mode is
unchanged either way. `fixture` and `remote` modes are untouched.

## Error handling

Mirrors the existing Nemotron path exactly: service unreachable or
model-load failure raises `VoiceError("WHISPER_UNAVAILABLE")`, the same shape
`NEMOTRON_UNAVAILABLE` already uses today. Malformed audio frames are already
caught upstream by `VoiceBridge.validate_audio`; no change needed there.

## Testing

- `WhisperClient` / `WhisperStream` get contract tests using a fake transport,
  mirroring the existing `NemotronClient` test pattern — no real OpenVINO or
  GPU required in CI.
- `services/whisper_intel` gets its own test suite following
  `services/kokoro/tests`' pattern: `--check` validates paths and device
  without loading the model.
- `provider_factory.py`'s new branch gets a focused test asserting
  `DIMON_STT_BACKEND=whisper_intel` constructs a `WhisperClient` with
  `voice_lifecycle=None`, and that the default (`nemotron`, or the variable
  unset) is unchanged from current behavior.

## Explicitly out of scope

- `services/gpu_host`'s supervisor, `capacity.py`'s `nvidia-smi` telemetry,
  `install_units.py` systemd units, and `model_manifest.py` verification are
  not touched. The three local services are started by hand, the same way the
  README already documents running each launcher yourself.
- No changes to `fixture` or `remote` inference modes.
- No changes to the NVIDIA-host `local` path's default behavior
  (`DIMON_STT_BACKEND` unset behaves exactly as today).
- Live word-level streaming equivalent to Nemotron's delta events is not
  attempted; partial captions are buffer-redecode approximations.

## Open follow-ups (not blocking this branch)

- Whether to eventually add an Intel Arc profile to `services/gpu_host` for
  parity with the shared NVIDIA host is left for a future design once there's
  a concrete Intel VRAM/telemetry story on WSL2.
- Whether `config/models.example.yaml` should list the Whisper artifact for
  documentation purposes, even though `model_manifest.py` verification is out
  of scope here.

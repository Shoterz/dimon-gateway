# GPU Residency Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the live CPU `small.en` fallback with repository-local Faster-Whisper Medium on CUDA, run Qwen Q5 with a 4,096-token context, and prove with real worker PIDs and measured VRAM that Qwen, Nemotron, Kokoro, and Faster-Whisper can remain resident together.

**Architecture:** A loopback-only Faster-Whisper frontend is managed by systemd but does not count as a loaded worker. It acquires a distinct GPU Overlord lease and launches a child process from a repository-local virtual environment; only that child imports CTranslate2, loads the pinned Medium model on CUDA with `int8_float16`, and holds VRAM. The frontend proxies the existing binary WebSocket protocol, reports the child PID and exact model identity, prevents concurrent jobs, and can evict the child only while idle. Qwen remains Q5 and uses the existing optimized runner; only the external Llama supervisor selects a new 4K profile. A calibration tool samples per-PID VRAM through load and representative inference, then the live Overlord budgets are updated from measurements before the four-worker acceptance run.

**Tech Stack:** Python 3.12, FastAPI/Starlette WebSockets, faster-whisper/CTranslate2 CUDA, uv-managed isolated runtime, systemd user services, GPU Overlord, llama.cpp, pytest/unittest, `nvidia-smi`.

**Spec:** `docs/superpowers/specs/2026-10-14-dimon-resident-voice-pipeline-design.md`

## Global Constraints

- The Faster-Whisper runtime, Python packages, model, download caches, and generated state must live under `<repo>/.runtime/`; installation must not write to `~/.local`, a system Python, or the project `.venv`.
- The required worker identity is exactly model `medium`, device `cuda`, compute type `int8_float16`, and one active transcription job. CPU execution, another model, or another compute type is a hard readiness failure.
- Keep the current CPU `small.en` service on port `8003` only as an explicitly named emergency rollback target. It must not satisfy the normal warm or ready contract.
- Use service name `faster_whisper`, profile `faster-whisper-medium-cuda`, public loopback port `8007`, and private child port `8008`.
- Qwen remains `qwen35-9b-abliterated-q5`; the effective context becomes exactly `4096`. Preserve its existing flash-attention, Q4 KV-cache, one-slot, and quantization settings.
- Do not edit `<llama-runtime>/run-qwen35-9b-abliterated.sh`. If an unexpected blocker later requires editing it, first back up the file and verify its original SHA-256 is `89d6038c57c7b84adafeae92fa0ece3335843911bb60793a0330a20fb3bf2cda`.
- Before editing the non-versioned GPU-manager files, create `<gpu-manager>/backups/pre-faster-whisper-medium-qwen4k-20261014/` and preserve the four files and hashes listed in Task 5.
- Final profile budgets are measurement-derived. For each worker, use the greater of loaded-idle and representative-inference peak, add `max(256 MiB, 10%)`, then round upward to the next 128 MiB. Retain the global 512 MiB safety reserve.
- A systemd-active frontend, open TCP port, or successful import is not proof that a model is resident. The loaded child PID, its exact identity, a successful inference probe, and live GPU process telemetry are required.
- Preserve the user’s unrelated dirty changes in `apps/dimon_live/static/app.js`, `apps/dimon_live/static/hands_free.mjs`, `apps/dimon_live/tests/test_ui_client.py`, and `tmp/`.
- No raw audio, transcript, prompt, payee, or payment content may be written by the service status, calibration report, or logs.

## Review Focus

- Every path used by the installer and launcher must remain under the repository after symlink resolution; environment overrides cannot escape into user or system directories.
- The CUDA worker must never silently fall back to CPU. Startup and health checks compare requested and effective model/device/compute identity.
- GPU admission happens before child launch. Failed load, failed health, worker crash, cancellation, eviction, and shutdown all terminate the child and release the Overlord lease exactly once.
- The frontend owns at most one active WebSocket job. Eviction is rejected while that job is live, and a disconnect returns the job count to zero.
- The live gateway and direct wake fallback must both point to port `8007` before CPU `small.en` is considered out of the normal path.
- The final acceptance must show four distinct non-supervisor worker PIDs resident at the same instant and at least 512 MiB free after representative inference.

---

### Task 1: Pin the Project-Local Runtime and Artifact Contract

**Files:**
- Modify: `.gitignore`
- Create: `services/faster_whisper_cuda/__init__.py`
- Create: `services/faster_whisper_cuda/artifacts.json`
- Create: `services/faster_whisper_cuda/requirements.lock`
- Create: `services/faster_whisper_cuda/install.py`
- Create: `scripts/install-faster-whisper-medium`
- Create: `services/faster_whisper_cuda/tests/test_install.py`

**Interfaces:**
- Produces fixed locations `.runtime/faster-whisper-medium/venv`, `.runtime/models/faster-whisper-medium`, `.runtime/cache/pip`, `.runtime/cache/huggingface`, and `.runtime/cache/xdg`.
- Produces an artifact manifest containing the exact faster-whisper version, CTranslate2 version, model repository, immutable model revision, and expected worker identity.
- Produces `scripts/install-faster-whisper-medium --check` and `--install`; neither mode accepts an arbitrary destination.

- [ ] **Step 1: Write failing containment and pinning tests**

Add tests named:

```python
test_all_runtime_paths_are_descendants_of_repository_runtime
test_symlink_escape_is_rejected
test_install_environment_redirects_every_package_and_model_cache
test_install_never_uses_user_site_or_project_venv
test_requirements_and_model_revision_are_exactly_pinned
test_check_rejects_wrong_model_device_or_compute_identity
```

Use a fake command runner and temporary repository root; no test downloads packages or model weights.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `.venv/bin/pytest services/faster_whisper_cuda/tests/test_install.py -q`

Expected: FAIL because the installer and manifest do not exist.

- [ ] **Step 3: Add fixed runtime paths and ignored local state**

Add `/.runtime/` to `.gitignore`. Implement canonical-path containment that rejects symlinks or overrides resolving outside `<repo>/.runtime`. Export `UV_CACHE_DIR`, `PIP_CACHE_DIR`, `HF_HOME`, `HUGGINGFACE_HUB_CACHE`, `TRANSFORMERS_CACHE`, and `XDG_CACHE_HOME` beneath that root and set `PYTHONNOUSERSITE=1`.

- [ ] **Step 4: Pin packages and the Medium model**

Populate `requirements.lock` with exact tested versions and hashes. Populate `artifacts.json` with `Systran/faster-whisper-medium` at an immutable Hugging Face revision and the required identity `medium/cuda/int8_float16`. The installer creates only the nested virtual environment, installs into it with `uv pip --python <nested-python>`, and downloads with `local_dir` set to the fixed model directory.

- [ ] **Step 5: Implement install and check modes**

`--install` creates the directories, environment, packages, and model. `--check` performs no network access and verifies executable/import locations, model files, manifest revision marker, CUDA availability through CTranslate2, and absence of imported packages from `.local` or the project `.venv`.

- [ ] **Step 6: Run focused tests**

Run: `.venv/bin/pytest services/faster_whisper_cuda/tests/test_install.py -q`

Expected: all pass without creating `.runtime` in the real repository.

- [ ] **Step 7: Commit the local-runtime contract**

```bash
git add .gitignore services/faster_whisper_cuda/__init__.py services/faster_whisper_cuda/artifacts.json services/faster_whisper_cuda/requirements.lock services/faster_whisper_cuda/install.py services/faster_whisper_cuda/tests/test_install.py scripts/install-faster-whisper-medium
git commit -m "build: isolate Faster-Whisper CUDA runtime"
```

### Task 2: Implement the CUDA Medium Child Worker

**Files:**
- Create: `services/faster_whisper_cuda/config.py`
- Create: `services/faster_whisper_cuda/protocol.py`
- Create: `services/faster_whisper_cuda/worker.py`
- Create: `services/faster_whisper_cuda/tests/test_worker.py`

**Interfaces:**
- Serves private loopback `GET /health` and `WS /ws` on port `8008`.
- Receives little-endian float32 mono 16 kHz binary chunks followed by text `done`.
- Returns `{"text": str, "segments": [{"avg_logprob": float, "no_speech_prob": float}]}` to preserve the current gateway/client protocol.
- Health returns only bounded operational fields: `ready`, `loaded`, `model`, `device`, `compute_type`, `pid`, `active_jobs`, `ready_at`, and `last_probe_at`.

- [ ] **Step 1: Write failing worker identity, protocol, and cleanup tests**

Add tests named:

```python
test_worker_constructs_medium_on_cuda_int8_float16_with_one_worker
test_health_reports_exact_effective_identity_and_process_pid
test_wrong_effective_identity_fails_closed
test_websocket_accepts_float32_chunks_and_returns_existing_schema
test_malformed_or_oversized_audio_is_rejected_without_transcription
test_only_one_transcription_job_is_admitted
test_success_failure_disconnect_and_cancel_return_active_jobs_to_zero
test_status_and_logs_never_include_audio_or_transcript
```

Inject a fake model factory so tests require no model or GPU.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.venv/bin/pytest services/faster_whisper_cuda/tests/test_worker.py -q`

Expected: FAIL because the worker modules do not exist.

- [ ] **Step 3: Implement strict configuration and audio framing**

Accept only host `127.0.0.1`, the fixed private port, the canonical repository model path, and the manifest identity. Reject misaligned float32 payloads, non-finite samples, input above 8 MiB, commands other than `done`, and a second concurrent job.

- [ ] **Step 4: Implement the injectable Medium engine**

The production factory imports `faster_whisper.WhisperModel` only inside the nested runtime and constructs:

```python
WhisperModel(
    model_path,
    device="cuda",
    compute_type="int8_float16",
    num_workers=1,
    local_files_only=True,
)
```

Materialize and sanitize segments, aggregate text in memory, and expose no content through health or ordinary logs. A startup probe transcribes a bounded generated silence buffer and records only success and timestamp.

- [ ] **Step 5: Implement the private FastAPI worker**

Load the model before declaring health ready, use one admission lock, guarantee job decrement in `finally`, and exit non-zero on CUDA/model/identity failure rather than selecting CPU.

- [ ] **Step 6: Run focused tests**

Run: `.venv/bin/pytest services/faster_whisper_cuda/tests/test_worker.py -q`

Expected: all pass with the fake engine.

- [ ] **Step 7: Commit the child worker**

```bash
git add services/faster_whisper_cuda/config.py services/faster_whisper_cuda/protocol.py services/faster_whisper_cuda/worker.py services/faster_whisper_cuda/tests/test_worker.py
git commit -m "feat: add CUDA Medium transcription worker"
```

### Task 3: Add the GPU-Managed Resident Frontend

**Files:**
- Create: `services/faster_whisper_cuda/managed.py`
- Create: `services/faster_whisper_cuda/app.py`
- Create: `services/faster_whisper_cuda/run.sh`
- Create: `services/faster_whisper_cuda/tests/test_managed.py`
- Create: `services/faster_whisper_cuda/tests/test_frontend.py`
- Modify: `services/gpu_host/tests/test_launchers.py`

**Interfaces:**
- Registers `faster_whisper` with the existing `ManagedServiceClient` and acquires `faster-whisper-medium-cuda` before launching the child.
- Serves public loopback `GET /health`, `POST /warm`, `POST /probe`, and `WS /ws` on port `8007`.
- `POST /warm` starts and validates the child; `POST /probe` runs a real bounded inference and refreshes `last_probe_at`.
- Reports the child model PID to GPU Overlord only after exact health identity and probe success.

- [ ] **Step 1: Write failing lifecycle and proxy tests**

Add tests named:

```python
test_gpu_lease_is_acquired_before_child_launch
test_child_pid_is_published_only_after_identity_and_probe
test_load_failure_terminates_child_and_releases_once
test_unexpected_child_exit_releases_and_clears_loaded_status
test_idle_eviction_stops_child_and_releases
test_busy_eviction_is_rejected
test_websocket_proxy_tracks_one_job_and_preserves_protocol
test_disconnect_cancel_and_proxy_failure_return_active_jobs_to_zero
test_frontend_without_child_is_not_ready
test_warm_is_idempotent_for_same_live_child
```

Use fake process, broker, health client, and WebSocket peers.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.venv/bin/pytest services/faster_whisper_cuda/tests/test_managed.py services/faster_whisper_cuda/tests/test_frontend.py services/gpu_host/tests/test_launchers.py -q`

Expected: FAIL because the managed service and launcher do not exist.

- [ ] **Step 3: Implement the child lifecycle state machine**

Use states `idle`, `loading`, `ready`, `busy`, `draining`, and `degraded` under one lock/condition. Acquire GPU capacity before process creation, wait boundedly for exact private health plus probe, then call `worker_started(child.pid)`. On any failure or child death, stop/kill boundedly, clear identity, set active jobs to zero, and release exactly once.

- [ ] **Step 4: Implement safe eviction and shutdown**

The Overlord callback returns `False` while loading, draining, or busy. When idle, it terminates only the child, waits for VRAM release, clears loaded health, and returns `True` so `ManagedServiceClient` releases its lease. Frontend shutdown follows the same idempotent path and closes the broker client.

- [ ] **Step 5: Implement public health, warm, probe, and proxy routes**

The public health response mirrors verified child identity and includes `frontend_pid` separately from `worker_pid`. WebSocket open ensures the worker is loaded, atomically claims the single job, updates GPU Overlord `active_jobs`, proxies binary frames and final JSON, then decrements in `finally`. Never expose child exceptions or content verbatim.

- [ ] **Step 6: Add a strict launcher**

`run.sh` validates the main project Python, nested worker Python, model directory, external GPU Overlord client directory, and fixed runtime containment. `--check` validates without starting or importing the model. Extend launcher tests for `faster_whisper_cuda`, port `8007`, fixed environment variables, executable bit, and rejection of unknown arguments.

- [ ] **Step 7: Run focused tests**

Run: `.venv/bin/pytest services/faster_whisper_cuda/tests services/gpu_host/tests/test_launchers.py -q`

Expected: all pass and every injected termination path records exactly one release.

- [ ] **Step 8: Commit the managed service**

```bash
git add services/faster_whisper_cuda/managed.py services/faster_whisper_cuda/app.py services/faster_whisper_cuda/run.sh services/faster_whisper_cuda/tests services/gpu_host/tests/test_launchers.py
git commit -m "feat: manage resident Faster-Whisper worker"
```

### Task 4: Add Portable Service Installation and Configuration

**Files:**
- Create: `deploy/systemd/dimon-faster-whisper-medium.service`
- Modify: `services/gpu_host/install_units.py`
- Modify: `services/gpu_host/tests/test_systemd_units.py`
- Modify: `deploy/systemd/README.md`
- Modify: `.env.example`
- Modify: `docs/inference-gateway.md`
- Modify: `services/inference_gateway/tests/test_auth.py`

**Interfaces:**
- Renders the new user unit with absolute repository and protected host-environment paths, but does not enable or start it.
- Documents `DIMON_WHISPER_URL=ws://127.0.0.1:8007/ws` and `DIMON_WHISPER_REVISION=faster-whisper-medium-cuda` as the normal dual-backend authoritative final.

- [ ] **Step 1: Write failing service-template and configuration tests**

Assert the unit uses loopback, the project launcher, bounded restart policy, the protected environment file, and no home-directory literal or credential. Assert installer output contains three units. Add a gateway config case proving the dual backend accepts the Medium CUDA revision at port `8007`.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.venv/bin/pytest services/gpu_host/tests/test_systemd_units.py services/inference_gateway/tests/test_auth.py -q`

Expected: FAIL because the unit and rendered service are absent.

- [ ] **Step 3: Add the unit and installer entry**

Order the Medium service after `gpu-overlord-stack.service`; use `Restart=on-failure`, `RestartSec=5`, `StartLimitBurst=3`, `TimeoutStopSec=30`, `UMask=0077`, and `NoNewPrivileges=true`. Do not order the gateway after the worker because the gateway must report a missing worker rather than being prevented from starting.

- [ ] **Step 4: Document the host variables and rollback endpoint**

Document the nested runtime/model paths, GPU-manager client directory, `8007` endpoint, Medium revision, local cache guarantees, and the emergency CPU endpoint on `8003`. Do not commit live `.env.host` or secrets.

- [ ] **Step 5: Run focused tests**

Run: `.venv/bin/pytest services/gpu_host/tests/test_systemd_units.py services/inference_gateway/tests/test_auth.py -q`

Expected: all pass.

- [ ] **Step 6: Commit the portable service wiring**

```bash
git add deploy/systemd/dimon-faster-whisper-medium.service services/gpu_host/install_units.py services/gpu_host/tests/test_systemd_units.py deploy/systemd/README.md .env.example docs/inference-gateway.md services/inference_gateway/tests/test_auth.py
git commit -m "ops: add CUDA Medium user service"
```

### Task 5: Back Up and Change Qwen to the 4K Profile

**Files:**
- Backup: `<gpu-manager>/backups/pre-faster-whisper-medium-qwen4k-20261014/overlord_llama_supervisor.py`
- Backup: `<gpu-manager>/backups/pre-faster-whisper-medium-qwen4k-20261014/test_overlord_llama.py`
- Backup: `<gpu-manager>/backups/pre-faster-whisper-medium-qwen4k-20261014/gpu_overlord_config.json`
- Backup: `<gpu-manager>/backups/pre-faster-whisper-medium-qwen4k-20261014/test_gpu_overlord.py`
- Modify: `<gpu-manager>/overlord_llama_supervisor.py`
- Modify: `<gpu-manager>/tests/test_overlord_llama.py`
- Modify: `<gpu-manager>/gpu_overlord_config.json`
- Modify: `<gpu-manager>/tests/test_gpu_overlord.py`

**Original SHA-256 values:**

```text
ee6eea0bf16bb5c5779b7b53394aa795da9c3def315309a3c20384a2805d49ff  overlord_llama_supervisor.py
f53345ca4ba8c06f4118ed05fd01f7308b41d91b402fa0d7a9a8da43aa454ac9  test_overlord_llama.py
25c406d709142f7f77fedec489fd0959181f48d3a95e355fbc035498026e54d8  gpu_overlord_config.json
65d8c1b99ca31725061be1d8485fb9100269d8c9b6e574f77ed7db48acbc1068  test_gpu_overlord.py
```

**Interfaces:**
- Qwen selection produces profile `qwen35-9b-abliterated-q5-4k` and environment `{"CTX_SIZE": "4096"}` regardless of Nemotron state.
- GPU Overlord adds distinct service/profile `faster_whisper/faster-whisper-medium-cuda` and retains the old Qwen 8K profile for explicit rollback only.

- [ ] **Step 1: Create immutable backups and verify hashes**

Create the fixed backup directory only if absent, copy all four files without overwriting existing backups, and run `sha256sum`. Abort if any original or existing backup hash differs from the values above.

- [ ] **Step 2: Change the Qwen tests first**

Rename the three current Qwen35 8K tests to assert 4K for idle Nemotron, active Nemotron, and unavailable status. Assert the runner command remains unchanged and only `CTX_SIZE` is overridden.

- [ ] **Step 3: Add failing profile and simultaneous-admission tests**

In `test_gpu_overlord.py`, require the distinct Faster-Whisper service/profile and Qwen 4K profile. Add a telemetry-driven test representing the measured four-worker state plus 512 MiB reserve. Initially mark the exact final budgets as calibration-pending constants used only for the first load.

- [ ] **Step 4: Run external tests and confirm failure**

Run: `cd <gpu-manager> && python3 -m unittest tests.test_overlord_llama tests.test_gpu_overlord -v`

Expected: Qwen tests see the 8K profile and the Faster-Whisper profile is absent.

- [ ] **Step 5: Change only the supervisor mapping and configuration**

Change Qwen35 selection to `qwen35-9b-abliterated-q5-4k` with `CTX_SIZE=4096`. Add the new profile keys with conservative bootstrap budgets sufficient for initial calibration; do not delete rollback profiles. Do not modify the Qwen runner.

- [ ] **Step 6: Run external tests**

Run: `cd <gpu-manager> && python3 -m unittest tests.test_overlord_llama tests.test_gpu_overlord -v`

Expected: all pass with Qwen fixed at 4K and separate CUDA Medium admission.

### Task 6: Install and Prove the Local CUDA Runtime

**Files:**
- Generate only: `.runtime/faster-whisper-medium/venv/`
- Generate only: `.runtime/models/faster-whisper-medium/`
- Generate only: `.runtime/cache/`
- Create: `docs/runbooks/faster-whisper-medium-cuda.md`

- [ ] **Step 1: Run the offline preflight**

Run: `scripts/install-faster-whisper-medium --check`

Expected before installation: a precise `RUNTIME_MISSING` or `MODEL_MISSING`, never an attempted CPU fallback.

- [ ] **Step 2: Install with project-local caches**

Run: `scripts/install-faster-whisper-medium --install`

Expected: all writes remain under `.runtime`; network access is used only for pinned packages and the pinned model revision.

- [ ] **Step 3: Verify import and model containment**

Run the installer `--check`, inspect `sys.prefix`, `sys.path`, module `__file__` values for faster-whisper/CTranslate2, and the resolved model path. Assert none contains `/.local/`, the project `.venv`, or a system site-packages directory.

- [ ] **Step 4: Run one real worker probe**

Start only the child with the nested Python, call private health and probe, and inspect `nvidia-smi` for the reported PID. Assert model `medium`, device `cuda`, compute `int8_float16`, and GPU residency. Stop it and confirm its PID and VRAM disappear.

- [ ] **Step 5: Write the operational runbook**

Document install, offline check, health/probe fields, logs, endpoint cutover, rollback to port `8003`, and complete cleanup by deleting only `.runtime/faster-whisper-medium`, `.runtime/models/faster-whisper-medium`, and `.runtime/cache` after stopping the service.

- [ ] **Step 6: Commit the runbook**

```bash
git add docs/runbooks/faster-whisper-medium-cuda.md
git commit -m "docs: add CUDA Medium operations runbook"
```

### Task 7: Measure and Calibrate Real VRAM Profiles

**Files:**
- Create: `scripts/calibrate_resident_vram.py`
- Create: `apps/dimon_live/tests/test_resident_vram_calibration.py`
- Modify after measurement: `<gpu-manager>/gpu_overlord_config.json`
- Modify after measurement: `<gpu-manager>/tests/test_gpu_overlord.py`
- Create: `docs/benchmarks/2026-10-14-resident-worker-vram.json`

**Interfaces:**
- Produces a content-free JSON report with GPU total/free memory, service, verified worker PID, loaded-idle MiB, inference-peak MiB, calculated budget MiB, exact identity, probe result, and timestamp.
- Samples `nvidia-smi` at 100 ms while each representative probe runs and again while all four workers are resident.

- [ ] **Step 1: Write failing parsing, sampling, and budget tests**

Add tests named:

```python
test_process_samples_are_bound_to_verified_worker_pids
test_missing_or_reused_pid_fails_calibration
test_budget_uses_peak_plus_headroom_rounded_to_128_mib
test_report_rejects_wrong_worker_identity
test_report_contains_no_request_or_transcript_content
test_four_worker_acceptance_requires_512_mib_free
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.venv/bin/pytest apps/dimon_live/tests/test_resident_vram_calibration.py -q`

Expected: FAIL because the calibration module does not exist.

- [ ] **Step 3: Implement the bounded calibration tool**

Resolve worker PIDs from verified service health/Overlord status, sample only those PIDs, and run bounded representative probes for Qwen, Nemotron, Kokoro, and CUDA Medium. Store identity and numeric timings/memory only. Fail on a missing PID, identity mismatch, probe failure, worker replacement, less than 512 MiB final reserve, or a non-zero idle Nemotron job count.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest apps/dimon_live/tests/test_resident_vram_calibration.py -q`

Expected: all pass with fake telemetry and probes.

- [ ] **Step 5: Start the managed stack and collect real measurements**

Render the new unit, reload systemd, start GPU Overlord and the four worker frontends, warm each actual model, and run the calibration tool. Repeat representative inference three times; use the maximum observed value rather than the median.

- [ ] **Step 6: Replace bootstrap budgets with calculated budgets**

Update the Qwen 4K and Faster-Whisper profile values—and any existing worker profile that the same run proves is under-budgeted—to the formula in Global Constraints. Update the external tests with the measured values and verify the combined admission/reserve case.

- [ ] **Step 7: Run external and calibration tests**

Run:

```bash
cd <gpu-manager> && python3 -m unittest tests.test_overlord_llama tests.test_gpu_overlord -v
cd <repo> && .venv/bin/pytest apps/dimon_live/tests/test_resident_vram_calibration.py -q
```

Expected: all pass; the saved report shows four distinct resident worker PIDs and at least 512 MiB free.

- [ ] **Step 8: Commit the calibration tool and evidence**

```bash
git add scripts/calibrate_resident_vram.py apps/dimon_live/tests/test_resident_vram_calibration.py docs/benchmarks/2026-10-14-resident-worker-vram.json
git commit -m "test: measure resident voice worker VRAM"
```

### Task 8: Cut Over the Live Authoritative Fallback and Verify Residency

**Files:**
- Modify locally, do not commit: `.env.host`
- Render locally: user unit `dimon-faster-whisper-medium.service`
- Modify the live `dimon.service` environment source only if inspection proves it does not inherit `.env.host`
- Test: existing gateway, hands-free, provider, and service suites

- [ ] **Step 1: Run the complete pre-cutover regression suite**

Run:

```bash
.venv/bin/pytest services/faster_whisper_cuda/tests services/gpu_host/tests/test_systemd_units.py services/gpu_host/tests/test_launchers.py services/inference_gateway/tests/test_auth.py services/inference_gateway/tests/test_transcription_endpoint.py apps/dimon_live/tests/test_intel_voice.py apps/dimon_live/tests/test_speech_fallback.py apps/dimon_live/tests/test_inference_modes.py apps/dimon_live/tests/test_resident_vram_calibration.py -q
```

Expected: all pass. Record any known unrelated pre-existing failure separately; do not waive a failure in these new paths.

- [ ] **Step 2: Update private live endpoint configuration**

Back up `.env.host` locally, then set:

```text
DIMON_WHISPER_URL=ws://127.0.0.1:8007/ws
DIMON_WHISPER_REVISION=faster-whisper-medium-cuda
```

Set the fixed nested runtime/model/cache and GPU-manager client paths. Inspect the effective `dimon.service` environment; update its private environment source so direct wake fallback also uses port `8007`. Do not expose or commit secrets.

- [ ] **Step 3: Render and start in dependency order**

Render units, run `systemctl --user daemon-reload`, then restart in this order: GPU Overlord, Llama supervisor, Medium service, inference gateway, Dimon app. Leave `jarvis-whisper-cpu.service` running or stopped according to rollback preference, but prove no normal endpoint references port `8003`.

- [ ] **Step 4: Prove exact live identities**

Check the Medium frontend/child health, Llama `/props` or slots metadata, Nemotron status, Kokoro health, GPU Overlord status, service process trees, and `nvidia-smi`. Require Qwen Q5/4096, Nemotron worker, Kokoro worker, and Medium/CUDA/int8_float16 to each have a distinct live worker PID.

- [ ] **Step 5: Run real protocol and failure checks**

Send a real Faster-Whisper WebSocket request, then disconnect/cancel one request and inject one worker failure. Each case must return active jobs to zero, release capacity after child failure, and never select CPU silently. Confirm Nemotron remains at zero idle leases.

- [ ] **Step 6: Run simultaneous-residency acceptance**

Warm all four, run representative Qwen, Nemotron, Kokoro, and Medium requests while keeping every worker resident, and rerun the VRAM calibration. Acceptance requires four verified worker PIDs, successful probes, zero idle Nemotron jobs, no eviction, and at least 512 MiB free after the peak.

- [ ] **Step 7: Verify rollback without performing it**

Confirm the backup hashes, old Qwen 8K profile, CPU port `8003`, and prior private `.env.host` copy are available. Document that rollback restores the supervisor mapping and endpoint, stops/disables the Medium unit, and may delete only the project `.runtime` trees.

- [ ] **Step 8: Record live results**

Append the service identity, PIDs, measured VRAM, reserve, test commands, and timestamps to `docs/benchmarks/2026-10-14-resident-worker-vram.json`; do not record content or secrets.

## Completion Gate

This subproject is complete only when:

1. The CUDA Medium runtime and model are wholly repository-local and reproducibly pinned.
2. The live normal endpoint is port `8007`; no normal path references CPU `small.en` on `8003`.
3. Qwen is still Q5 and proves an effective 4,096-token context without editing its runner.
4. Qwen, Nemotron, Kokoro, and Faster-Whisper each show a distinct loaded worker PID and successful probe.
5. Measured simultaneous VRAM retains at least 512 MiB free and the Overlord profiles match the recorded peaks plus headroom.
6. Failure, cancellation, disconnect, eviction, and shutdown tests return active jobs/leases to zero.
7. The external backups and rollback procedure are verified.

Only after this gate passes should implementation proceed to the separate truthful four-worker READY plan, followed by the cross-component trace and in-app diagnostics panel plan.

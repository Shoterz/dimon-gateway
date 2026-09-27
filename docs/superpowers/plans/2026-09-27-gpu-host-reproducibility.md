# GPU Host Reproducibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Qwen, Nemotron, Kokoro, and inference-gateway host reproducible from repository-owned configuration and launchers while storing every heavyweight artifact exactly once outside Git.

**Architecture:** The repository owns manifests, adapters, process definitions, readiness probes, and a small capacity supervisor. Paths to weights and compiled runtimes come from the host environment. `shared` mode points at already-running personal services; `standalone` mode launches project-scoped processes from the same artifact paths. Neither mode copies weights into the repository or changes personal hostnames.

**Tech Stack:** Python 3.12, uv, Bash, systemd user services, llama.cpp server, NVIDIA NeMo Speech runtime, Kokoro FastAPI runtime, NVIDIA CLI probes.

**Spec:** [Team repository and inference architecture design](../specs/2026-09-27-team-repository-and-inference-architecture-design.md)

## Global Constraints

- This plan depends on the bounded inference gateway.
- Model weights, model caches, CUDA toolkits, virtual environments, compiled binaries, and generated audio remain outside Git.
- `shared` mode may reuse processes on `127.0.0.1:8080`, `127.0.0.1:8004`, and `127.0.0.1:3001`; it must not stop, reconfigure, or duplicate them.
- `standalone` mode must be runnable without reading launcher code or configuration from the personal Jarvis, llama, or GPU-manager repositories.
- All raw services and the inference gateway bind to loopback.
- Downloading artifacts is an explicit opt-in command with license and disk-space prompts; normal setup never downloads models.

## Review Focus

- Two repository modes must refer to the same external artifact paths, so switching modes does not consume a second copy of each model.
- Every artifact must have upstream source, immutable revision, license, expected size, and checksum or immutable content digest.
- Process cleanup must affect only project-owned PIDs.
- Readiness must verify actual capability, not just an open port.
- A failed service must produce one bounded typed readiness failure and must not expose host paths.

---

### Task 1: Define and validate the model manifest

**Files:**
- Create: `config/models.example.yaml`
- Create: `services/gpu_host/__init__.py`
- Create: `services/gpu_host/tests/__init__.py`
- Create: `services/gpu_host/model_manifest.py`
- Create: `scripts/check-model-manifest`
- Create: `services/gpu_host/tests/test_model_manifest.py`
- Create: `docs/model-provenance.md`
- Modify: `THIRD_PARTY_NOTICES.md`

**Interfaces:**
- `ModelManifest.load(path)` validates Qwen, Nemotron, and Kokoro entries with `source`, `revision`, `license`, `artifact`, `expected_bytes`, `sha256`, and environment-variable path names.
- `scripts/check-model-manifest --manifest config/models.yaml` checks local files without printing absolute paths and exits nonzero for missing, wrong-size, or wrong-checksum artifacts.
- `config/models.example.yaml` contains provenance and variable names; host-specific `config/models.yaml` is ignored.

- [ ] Write failing tests for missing fields, mutable revisions such as `main`, malformed hashes, duplicate ports, path literals inside the checked-in example, missing files, and checksum mismatch.
- [ ] Run the focused test and observe failures.
- [ ] Query each official upstream API during implementation, record immutable revisions and license identifiers, and calculate SHA-256 for the exact artifacts already on the GPU host before writing the example manifest.
- [ ] Implement streaming checksum validation and disk-size checks without loading large files into memory.
- [ ] Document provenance, redistribution status, local path variables, and the command used to verify each artifact.
- [ ] Run the manifest tests and validate the host's configured manifest.
- [ ] Commit provenance as `feat: add verified model manifest`.

### Task 2: Add project-owned raw-service launchers

**Files:**
- Create: `services/qwen/run.sh`
- Create: `services/qwen/README.md`
- Create: `services/nemotron/run.sh`
- Create: `services/nemotron/README.md`
- Create: `services/kokoro/run.sh`
- Create: `services/kokoro/README.md`
- Create: `services/gpu_host/tests/test_launchers.py`
- Modify: `.env.example`

**Interfaces:**
- Each launcher requires its artifact/runtime path variables, uses `exec`, binds to the approved loopback port, and accepts no arbitrary shell fragment.
- Qwen: OpenAI-compatible llama-server on `127.0.0.1:8080` with one pinned model path and bounded context/output settings.
- Nemotron: project-compatible streaming service on `127.0.0.1:8004` with pinned Parakeet/Nemotron model and runtime revisions.
- Kokoro: project-compatible synthesis service on `127.0.0.1:3001` with a pinned voice/model configuration.

- [ ] Write failing static tests for `set -euo pipefail`, required-variable checks, loopback binds, approved ports, `exec`, fixed model selection, argument quoting, and absence of personal paths or public binds.
- [ ] Run the launcher tests and observe failures.
- [ ] Reimplement the minimal launch commands from upstream documentation and the existing working services; copy no unrelated personal supervisor code or secrets.
- [ ] Add `--check` mode to each script so teammates can validate executables, versions, artifacts, and ports without starting the process.
- [ ] Run `bash -n` plus the static launcher tests.
- [ ] Commit launchers as `feat: add project inference launchers`.

### Task 3: Implement project-owned capacity and process supervision

**Files:**
- Create: `services/gpu_host/config.py`
- Create: `services/gpu_host/capacity.py`
- Create: `services/gpu_host/processes.py`
- Create: `services/gpu_host/supervisor.py`
- Create: `services/gpu_host/tests/test_capacity.py`
- Create: `services/gpu_host/tests/test_processes.py`
- Create: `config/gpu-host.example.yaml`

**Interfaces:**
- `CapacityProfile(name, reserved_vram_mb, startup_timeout_seconds, idle_timeout_seconds)`.
- `GpuCapacityBroker.acquire(service, profile, timeout) -> Lease` and `release(lease)` serialize incompatible starts and reserve configured VRAM conservatively.
- `ManagedProcess.start()`, `ready()`, and `stop()` track one project-owned process group and never signal an unrecorded PID.
- `HostSupervisor.ensure(capability)` starts only the configured standalone dependency and returns a typed readiness result.

- [ ] Write failing tests for compatible/incompatible reservations, insufficient memory, FIFO waiters, cancellation, stale PID records, process-group cleanup, readiness timeout, crash backoff, and refusal to signal foreign PIDs.
- [ ] Run the focused tests and observe failures.
- [ ] Implement capacity decisions using injected GPU telemetry; parse `nvidia-smi` only in the production adapter.
- [ ] Implement project PID/state files under `${XDG_RUNTIME_DIR}/dimon-gateway/`, command allow-listing from validated configuration, exponential crash backoff, and bounded logs.
- [ ] Configure conservative profiles from measured host usage and document that the gateway's application-level queues remain the final request limit.
- [ ] Run the focused tests with fake telemetry and fake child processes.
- [ ] Commit lifecycle control as `feat: add GPU host supervisor`.

### Task 4: Add shared and standalone host orchestration

**Files:**
- Create: `scripts/setup-host`
- Create: `scripts/start-host`
- Create: `scripts/stop-host`
- Create: `scripts/healthcheck`
- Create: `services/gpu_host/tests/test_host_scripts.py`
- Create: `docs/gpu-host-setup.md`

**Interfaces:**
- `scripts/setup-host --mode shared|standalone` validates Python, uv, NVIDIA driver, runtime binaries, manifest, ports, and free disk space; it does not download by default.
- `scripts/start-host --mode shared` validates the three existing loopback services and starts only the project gateway.
- `scripts/start-host --mode standalone` starts the project supervisor plus gateway using external artifacts.
- `scripts/stop-host` stops only PIDs recorded under the project runtime directory.
- `scripts/healthcheck --json` reports Qwen, Nemotron, Kokoro, gateway, GPU capacity, revision agreement, and a single safe reason code per failure.

- [ ] Write failing script-contract tests for both modes, missing artifacts, occupied ports, wrong upstream model revision, explicit download gating, loopback binds, and project-only cleanup.
- [ ] Run the focused tests and observe failures.
- [ ] Implement shell entry points as thin wrappers around typed Python modules; avoid duplicating configuration parsing in Bash.
- [ ] Add an explicit `setup-host --download` path that shows licenses, expected bytes, destination, and checksum before fetching from official sources.
- [ ] Document exact first-run, restart, log, health, upgrade, and rollback commands for both modes.
- [ ] Run static tests and a `--check` rehearsal without changing running personal services.
- [ ] Commit orchestration as `feat: add shared and standalone host modes`.

### Task 5: Add user-service definitions and restart recovery

**Files:**
- Create: `deploy/systemd/dimon-gpu-host.service`
- Create: `deploy/systemd/dimon-inference-gateway.service`
- Create: `deploy/systemd/README.md`
- Create: `services/gpu_host/tests/test_systemd_units.py`

**Interfaces:**
- User units load secrets from an ignored host environment file, use the repository scripts, restart on bounded failure, and write no credentials into the unit files.
- The gateway starts after the host supervisor and exposes only `127.0.0.1:8891`.

- [ ] Write failing tests for loopback-only commands, environment-file permissions/documentation, restart bounds, startup ordering, working directory placeholders, and absence of credentials.
- [ ] Run the focused tests and observe failures.
- [ ] Add portable template units plus an installer command that renders absolute repository paths into `${XDG_CONFIG_HOME}/systemd/user/`.
- [ ] Document daemon reload, enable/start, status, logs, and rollback without enabling units automatically.
- [ ] Test rendered units with `systemd-analyze --user verify` when available.
- [ ] Commit service templates as `ops: add GPU host user services`.

### Task 6: Perform host capability and no-duplication rehearsal

**Files:**
- Modify: `docs/gpu-host-setup.md`
- Create: `docs/evidence/gpu-host-rehearsal.md`

**Interfaces:**
- Rehearsal evidence records artifact digests, service revisions, readiness results, peak VRAM, and latency without storing local paths, prompts, audio, tokens, or personal service configuration.

- [ ] Run shared mode and confirm the gateway reuses existing Qwen/Nemotron/Kokoro PIDs and artifact inodes rather than spawning or copying them.
- [ ] Stop only project-owned processes, then run standalone mode against the same configured artifact files.
- [ ] Exercise one intent, one short transcription, one speech result, one capacity rejection, and one upstream restart.
- [ ] Confirm `llama.stocksuite.app` and `jarvis.stocksuite.app` remain reachable and unchanged throughout the rehearsal.
- [ ] Record sanitized evidence and commit the checkpoint after boundary checks pass.

### Verification Contract

Before reporting this checkpoint complete, run:

```bash
uv run python -m unittest discover -s services/gpu_host/tests -p 'test_*.py'
bash -n services/qwen/run.sh services/nemotron/run.sh services/kokoro/run.sh
scripts/check-model-manifest --manifest config/models.yaml
scripts/setup-host --mode shared --check
scripts/healthcheck --json
uv run python -m unittest discover -s services/inference_gateway/tests -p 'test_*.py'
scripts/check-repository-boundary
git diff --check
```

The checkpoint is complete only after both host modes use the same verified artifact files, only the intended project processes are created or stopped, all raw listeners are loopback-only, and the personal Jarvis and llama services remain unchanged.

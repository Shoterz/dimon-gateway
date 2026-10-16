# Truthful Four-Worker Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the red-D READY state prove that Qwen Q5/4K, Nemotron, Kokoro, and Faster-Whisper Medium CUDA are simultaneously resident, recently probed, and above the 512 MiB GPU reserve.

**Architecture:** The inference gateway owns warm/probe orchestration and a strict residency gate backed by GPU Overlord status. The public Dimon app only forwards a sanitized snapshot; the browser combines that backend proof with its existing audio/microphone/socket readiness. A bounded monitor repeats real probes within the five-minute ready window so idle eviction immediately degrades readiness instead of leaving stale green stages.

**Tech Stack:** Python 3.12, FastAPI, asyncio, httpx, pytest/unittest, GPU Overlord Unix-socket status protocol, vanilla JavaScript.

**Spec:** `docs/superpowers/specs/2026-10-14-dimon-resident-voice-pipeline-design.md`

## Global Constraints

- READY requires four non-supervisor worker PIDs with exact profiles: `qwen35-9b-abliterated-q5-4k`, `nemotron-en`, `kokoro-82m`, and `faster-whisper-medium-cuda`.
- Qwen must prove effective context `4096`; Faster-Whisper must prove `medium`, `cuda`, `int8_float16` through its real `/probe` path.
- Managed states `ready` and `busy` may pass; stale heartbeat, wrong profile, missing PID, failed probe, over-budget VRAM, or free VRAM below 512 MiB must fail closed.
- The ready lifetime is five minutes, with refresh probes occurring before the existing 75-second worker idle eviction.
- Preparation performs no banking write and exposes no endpoint, credential, prompt, transcript, audio, or filesystem path.
- Existing user changes in `apps/dimon_live/static/app.js`, `apps/dimon_live/static/hands_free.mjs`, face, and voiceprint files must be preserved.

## Review Focus

- A worker switches PID or profile between probe and final status: return DEGRADED, never READY; Task 1 tests this through strict evidence matching.
- Nemotron is legitimately busy with the one admitted live stream during preparation: accept the live busy proof without opening a competing stream, but reject over-capacity or stale state; Task 3 tests both cases.
- A monitor probe fails after initial READY: clear current readiness on the next monitor cycle while retaining only labeled historical latency; Task 3 tests this transition.
- The ready window expires: overall state and every stage become IDLE rather than preserving stale READY values; Task 3 tests expiry.
- Malformed or oversized GPU/status data: sanitize to bounded reason codes and fail closed without leaking infrastructure details; Tasks 1 and 4 test this.

---

### Task 1: Strict GPU residency evidence

**Files:**
- Modify: `apps/dimon_live/gpu_overlord_status.py`
- Create: `services/inference_gateway/residency.py`
- Modify: `apps/dimon_live/tests/test_gpu_overlord_status.py`
- Create: `services/inference_gateway/tests/test_residency.py`

**Interfaces:**
- Consumes: GPU Overlord `{status:{gpu,services}}` response.
- Produces: `ResidentWorkerEvidence`, `ResidencySnapshot`, and `FourWorkerResidencyGate.check(recent_probes) -> ResidencySnapshot`.

- [ ] **Step 1: Write failing parser and gate tests** for exact PID/profile/state/heartbeat/budget/VRAM fields; missing PID, wrong profile, heartbeat over 5 seconds, observed VRAM over budget, PID changed since probe, and free VRAM under 512 must each fail with a bounded reason code.
- [ ] **Step 2: Run tests to verify RED**

Run: `.venv/bin/python -m pytest apps/dimon_live/tests/test_gpu_overlord_status.py services/inference_gateway/tests/test_residency.py -q`

Expected: FAIL because the enriched evidence and gate do not exist.

- [ ] **Step 3: Implement the enriched parser and strict gate** without exposing control sockets, commands, or paths.
- [ ] **Step 4: Run tests to verify GREEN** with all cases passing.
- [ ] **Step 5: Commit**

Commit: `feat: verify four resident GPU workers`

### Task 2: Real Qwen and Faster-Whisper probes

**Files:**
- Modify: `services/inference_gateway/config.py`
- Create: `services/inference_gateway/worker_probes.py`
- Create: `services/inference_gateway/tests/test_worker_probes.py`
- Modify: `services/inference_gateway/tests/test_auth.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: Qwen model-specific `/props?model=qwen35-9b-abliterated-q5`, Faster-Whisper `/probe`, and `DIMON_GPU_OVERLORD_SOCKET`.
- Produces: `QwenRuntimeProbe.__call__() -> ProbeEvidence` and `FasterWhisperRuntimeProbe.__call__() -> ProbeEvidence`, each carrying worker PID and bounded identity only.

- [ ] **Step 1: Write failing probe tests** proving Qwen requires `n_ctx == 4096` and Q5 model identity, Medium requires `ready/loaded`, `medium/cuda/int8_float16`, a positive worker PID, and malformed/timeout responses fail closed.
- [ ] **Step 2: Run tests to verify RED**

Run: `.venv/bin/python -m pytest services/inference_gateway/tests/test_worker_probes.py services/inference_gateway/tests/test_auth.py -q`

Expected: FAIL because runtime probes and socket configuration are absent.

- [ ] **Step 3: Implement bounded loopback probes and configuration**; no response body field outside the allowlist enters readiness output.
- [ ] **Step 4: Run tests to verify GREEN**.
- [ ] **Step 5: Commit**

Commit: `feat: prove Qwen 4K and CUDA Medium identity`

### Task 3: Five-stage preparation and retention monitor

**Files:**
- Modify: `services/inference_gateway/preparation.py`
- Modify: `services/inference_gateway/app.py`
- Modify: `services/inference_gateway/tests/test_preparation.py`
- Modify: `services/inference_gateway/tests/test_auth.py`

**Interfaces:**
- Consumes: Task 1 `FourWorkerResidencyGate`, Task 2 probes, existing Qwen/Kokoro/Nemotron production adapters, and the gateway transcription lease.
- Produces: preparation snapshots with ordered stages `capacity`, `nemotron`, `qwen`, `kokoro`, `faster_whisper`, plus sanitized `workers` and `gpu` evidence.

- [ ] **Step 1: Write failing orchestration tests** for four concurrent warm probes, exact final residency validation, legitimately busy Nemotron, over-capacity rejection, monitor degradation/recovery, and five-minute expiry clearing all stages to IDLE.
- [ ] **Step 2: Run tests to verify RED**

Run: `.venv/bin/python -m pytest services/inference_gateway/tests/test_preparation.py -q`

Expected: FAIL because preparation has only three stages and no residency gate.

- [ ] **Step 3: Implement the five-stage coordinator** with one in-flight start task, a refresh interval no longer than 45 seconds, and final evidence captured only after all probes complete.
- [ ] **Step 4: Run tests to verify GREEN**.
- [ ] **Step 5: Commit**

Commit: `feat: keep four Dimon workers truthfully ready`

### Task 4: Public sanitization and red-D presentation

**Files:**
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/hands_free.mjs`
- Modify: `apps/dimon_live/tests/test_demo_preparation_api.py`
- Modify: `apps/dimon_live/tests/test_ui_client.py`
- Modify: `apps/dimon_live/tests/test_hands_free.py`

**Interfaces:**
- Consumes: Task 3 preparation snapshot.
- Produces: `/api/demo/prepare` sanitized five-stage response and green browser presentation only when backend plus audio/microphone/socket are ready.

- [ ] **Step 1: Write failing API/UI tests** proving Faster-Whisper is required, worker diagnostic fields are bounded, PENDING capacity cannot coexist with READY, expired/current-state mismatch is rejected, and hidden endpoints/secrets/paths never serialize.
- [ ] **Step 2: Run tests to verify RED**

Run: `.venv/bin/python -m pytest apps/dimon_live/tests/test_demo_preparation_api.py apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_hands_free.py -q`

Expected: FAIL because public sanitization knows only four old stages and the UI does not require Medium.

- [ ] **Step 3: Implement the sanitizer and presentation update** while preserving the user's current UI changes.
- [ ] **Step 4: Run tests to verify GREEN**.
- [ ] **Step 5: Commit**

Commit: `feat: make Dimon ready badge truthful`

### Task 5: Deploy and prove live four-worker readiness

**Files:**
- Modify: `docs/runbooks/faster-whisper-medium-cuda.md`
- Create: `docs/benchmarks/2026-10-15-four-worker-readiness.json`

**Interfaces:**
- Consumes: Tasks 1–4 and the calibrated GPU budgets.
- Produces: live acceptance evidence with four distinct PIDs, exact profiles, Qwen 4096 context, Medium CUDA identity, and at least 512 MiB free.

- [ ] **Step 1: Run the full focused suite**

Run: `.venv/bin/python -m pytest services/inference_gateway/tests apps/dimon_live/tests/test_gpu_overlord_status.py apps/dimon_live/tests/test_demo_preparation.py apps/dimon_live/tests/test_demo_preparation_api.py apps/dimon_live/tests/test_hands_free.py apps/dimon_live/tests/test_ui_client.py services/faster_whisper_cuda/tests services/gpu_host/tests -q`

Expected: PASS, apart from any explicitly documented pre-existing unrelated test.

- [ ] **Step 2: Restart the GPU stack, Medium service, gateway, and Dimon app** so the calibrated 1,664 MiB Medium budget and new code are active.
- [ ] **Step 3: Warm once and capture content-free acceptance JSON**; assert state READY, all five stages non-pending, four distinct worker PIDs, exact profiles, active Nemotron sessions at or below one, and free VRAM at least 512 MiB.
- [ ] **Step 4: Wait beyond the old 75-second idle timeout** and prove the retention monitor keeps all four workers resident during the five-minute window.
- [ ] **Step 5: Commit**

Commit: `test: prove truthful four-worker readiness`

# Shared Qwen 8K and Concurrent Kokoro Residency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the shared Qwen3.5 service at an 8K context and have DIMON warm Nemotron, Qwen, and Kokoro concurrently and verify the trio before declaring preparation ready.

**Architecture:** Keep the existing shared Qwen endpoint and GPU Overlord. Add a measured 8K Qwen profile, make the supervisor always select 8192 for Qwen3.5, and change DIMON's best-effort preparation to auto-start in the background when enabled, warming Qwen and Kokoro concurrently after Nemotron. Readiness still comes from successful probes plus the Overlord's resident-worker snapshot.

**Tech Stack:** Python, FastAPI, asyncio, llama.cpp OpenAI-compatible server, GPU Overlord managed leases, unittest.

**Spec:** `docs/superpowers/specs/2026-10-06-shared-qwen8k-concurrent-kokoro-design.md`

## Global Constraints

- Shared `qwen35-9b-abliterated-q5` launches with `CTX_SIZE=8192`, including for `llama.stocksuite.app`; the user accepts this effect.
- Nemotron, Qwen, and Kokoro must be observed resident before DIMON preparation reports READY.
- Qwen and Kokoro warm probes must overlap; Nemotron is prepared first.
- GPU Overlord remains the sole VRAM admission/eviction authority; never evict a busy service or lower its 512 MiB safety reserve to force admission.
- Calibrate the 8K Qwen profile from observed host VRAM use; do not derive it by linearly scaling a larger-context profile.
- Keep existing model behavior, endpoints, business logic, database, credentials, and non-Qwen model profiles unchanged.
- Preserve bounded timeouts, sanitized failure reporting, and current normal text/TTS fallback if optional preparation is not ready.

## Review Focus

- Qwen context must remain exactly 8192 when Nemotron status is busy, idle, missing, or unavailable; test all four cases.
- Concurrent Qwen/Kokoro startup must not claim READY until both probes and the final resident-worker check succeed; test with event gates and an evicted-worker snapshot.
- Failed/cancelled warm-up must not leak a managed lease, hang HTTP startup, or expose raw exception details; test each stage failure and cancellation.
- GPU accounting must reserve capacity for all three services plus the safety reserve; test sufficient/insufficient admission and a busy-worker denial.
- Existing manual preparation endpoints and non-Qwen model launch configurations must remain compatible.

---

### Task 1: Add the shared 8K Qwen supervisor profile

**Files:**
- Modify: the external GPU process manager's `overlord_llama_supervisor.py`
- Modify: the external GPU process manager's `gpu_overlord_config.json`
- Test: the external GPU process manager's `tests/test_overlord_llama.py`

**Interfaces:**
- Consumes: Existing `ManagedModelSupervisor.resolve_runtime(model_name)` and `CapacityBroker.status()`.
- Produces: Qwen3.5 resolution to profile `qwen35-9b-abliterated-q5-8k` and launch environment `{"CTX_SIZE": "8192"}` regardless of Nemotron status.

- [x] **Step 1: Add failing context-selection tests**

Add tests for Nemotron active, inactive, missing, and status-error conditions, plus a test that another configured model's resolution is unchanged.

- [x] **Step 2: Run the focused tests and verify the new cases fail**

Run: `pytest -q tests/test_overlord_llama.py`
Expected: New 8K expectations fail against the current 16K/32K selection; existing unrelated tests pass.

- [x] **Step 3: Implement the 8K Qwen profile selection**

In `overlord_llama_supervisor.py`, select `qwen35-9b-abliterated-q5-8k` and `CTX_SIZE=8192` for Qwen3.5, without changing other models. Add the corresponding Qwen profile entry in `gpu_overlord_config.json`, initially marked for host calibration rather than guessed from context scaling.

- [ ] **Step 4: Calibrate and test the profile budget on the host**

Measure cold start and representative generation peak for Qwen3.5 at 8K with current Nemotron and Kokoro loads. Set the profile budget to cover measured peak plus allocator overhead. Test broker admission with the configured 8K Qwen + Nemotron + Kokoro budgets and 512 MiB reserve; prove admission succeeds under the measured safe snapshot and fails closed below it.

Run: `pytest -q tests/test_overlord_llama.py tests/test_gpu_overlord.py`
Expected: Focused supervisor and broker tests pass; the recorded host measurement stays within the configured lease budget and reserve.

### Task 2: Warm Qwen and Kokoro concurrently in DIMON preparation

**Files:**
- Modify: `apps/dimon_live/demo_preparation.py`
- Test: `apps/dimon_live/tests/test_demo_preparation.py`

**Interfaces:**
- Consumes: Existing extractor, Kokoro synthesizer, Nemotron lifecycle, and `GpuOverlordStatusClient` interfaces.
- Produces: Existing preparation snapshot shape, with Qwen and Kokoro stages RUNNING concurrently and READY only after both probes plus a final resident-worker snapshot.

- [x] **Step 1: Add a gate-based overlap test**

Use controlled fake Qwen and Kokoro operations. Assert both have started before either gate is released, and assert preparation remains PREPARING until both gates complete and status confirms the trio.

- [x] **Step 2: Run the focused test and verify it fails**

Run: `pytest -q apps/dimon_live/tests/test_demo_preparation.py`
Expected: The overlap assertion fails because current implementation awaits Qwen and then Kokoro sequentially.

- [x] **Step 3: Implement concurrent probes and final residency validation**

After existing capacity preflight and Nemotron warm-up, run Qwen and Kokoro probes concurrently with bounded stage-specific timeouts. Preserve stable stage failure codes, cancellation propagation, RIFF/WAVE validation, and sanitize errors. Re-read Overlord status after both probes and mark READY only when Nemotron, Qwen, and Kokoro workers are all live and non-error.

- [x] **Step 4: Add failure, cancellation, eviction, and capacity tests**

Cover Qwen failure, Kokoro failure, each timeout, cancellation, malformed WAV, missing worker in final status, and a busy Whisper/broker-capacity denial. Assert no false READY and no leaked Nemotron lifecycle lease.

- [x] **Step 5: Run focused preparation tests**

Run: `pytest -q apps/dimon_live/tests/test_demo_preparation.py apps/dimon_live/tests/test_demo_preparation_api.py`
Expected: All preparation and API tests pass; endpoint status remains sanitized and compatible.

### Task 3: Automatically start preparation without blocking DIMON startup

**Files:**
- Modify: `apps/dimon_live/app.py`
- Test: `apps/dimon_live/tests/test_demo_preparation_api.py`

**Interfaces:**
- Consumes: Existing optional `application.state.demo_preparer`, local inference mode, and `DIMON_DEMO_PREPARE_ENABLED` gate.
- Produces: Background preparation task started during app lifespan only when local preparation is enabled; current GET status and POST start/retry endpoints remain available.

- [x] **Step 1: Add failing app-lifespan tests**

Verify enabled local mode schedules preparation without awaiting its completion; disabled or fixture mode does not schedule it; existing GET/POST endpoints continue to work.

- [x] **Step 2: Run the focused API tests and verify the autostart case fails**

Run: `pytest -q apps/dimon_live/tests/test_demo_preparation_api.py`
Expected: The new enabled-mode autostart assertion fails, while existing route tests pass.

- [x] **Step 3: Add lifespan-managed background start**

Use FastAPI lifespan or the project's existing lifecycle pattern. Store/track the task so shutdown cancels/awaits it safely. Do not block HTTP startup on model loads, do not start in fixture/remote mode, and preserve local-operator endpoint restrictions.

- [x] **Step 4: Run API and app tests**

Run: `pytest -q apps/dimon_live/tests/test_demo_preparation_api.py apps/dimon_live/tests/test_api.py`
Expected: All tests pass and startup remains non-blocking.

### Task 4: Integrate the shared GPU stack and verify end-to-end

**Files:**
- Operational configuration: the external GPU process manager's `gpu_overlord_config.json` and the existing GPU stack service environment.
- DIMON runtime configuration: its existing service environment for `DIMON_DEMO_PREPARE_ENABLED=1`.
- No change to the Qwen API endpoint or llama.stocksuite.app routing.

**Interfaces:**
- Consumes: Tasks 1–3; current managed GPU stack and live DIMON service.
- Produces: A live status showing Nemotron, Qwen 8K, and Kokoro resident together and preparation READY; subsequent Qwen response hands off to already-warm Kokoro.

- [x] **Step 1: Review the exact live service unit and environment before editing**

Confirm the managed stack and DIMON service target files and current process ownership. Preserve any local user changes and do not overwrite unrelated environment entries.

- [x] **Step 2: Enable automatic preparation for the intended DIMON local service**

Set only the existing enable flag for the target DIMON local service; do not enable it in fixtures or other apps. Restart only the managed GPU stack and DIMON service needed to load the approved Qwen profile and app code, after configuration validation.

- [x] **Step 3: Verify Qwen's actual launch context and managed leases**

Inspect supervisor status/logs and process arguments/environment to confirm `CTX_SIZE=8192`; inspect GPU Overlord status to confirm Nemotron, Qwen, and Kokoro are all resident and no busy worker was evicted.

- [x] **Step 4: Exercise one text request through Qwen and Kokoro**

Run DIMON preparation to READY, send one supported Qwen request, confirm the response is spoken by Kokoro without a cold-load interval, and confirm all three workers remain healthy through the turn. Record before/during/after GPU measurements.

- [x] **Step 5: Run regression suites**

Run: `pytest -q apps/dimon_live/tests`
Run in GPU manager directory: `pytest -q tests/test_overlord_llama.py tests/test_gpu_overlord.py tests/test_overlord_kokoro.py tests/test_overlord_nemotron.py`
Expected: All focused and relevant regression tests pass; report any unrelated pre-existing failure by exact name.

### Live validation notes — 2026-10-06

- The local DIMON `.env` already had `DIMON_INFERENCE_MODE=local` and `DIMON_DEMO_PREPARE_ENABLED=1`; no unrelated systemd environment was changed.
- After confirming zero active jobs, the GPU Overlord stack was restarted to load the new supervisor. DIMON's local operator warm-up reached `READY` with Nemotron, Qwen, and Kokoro all present. Qwen launched with `--ctx-size 8192`; the selected profile is `qwen35-9b-abliterated-q5-8k`.
- At the successful trio snapshot: Qwen observed 5,906 MiB under a 7,168 MiB lease, Nemotron 1,184/1,536 MiB, Kokoro 860/1,280 MiB, and device use 8,624 MiB of 12,282 MiB. Keep the 512 MiB reserve unchanged. A short Qwen completion returned in about 0.31 seconds; DIMON's Kokoro adapter produced valid RIFF/WAVE audio.
- A 7,000-token stress request timed out after 90 seconds; peak sampled device use during that attempt was 7,853 MiB. It did not leave an active Qwen job. This remains a Qwen long-prompt performance issue to diagnose; it is not evidence to reduce the configured lease budget.
- During live checks a separate Whisper ASR request became busy; the Overlord correctly preserved it and evicted idle Qwen/Nemotron leases. DIMON changed from READY to DEGRADED, and after the request naturally ended a warm-up again reached READY. Residency is intentionally best-effort under GPU pressure, not pinned.
- Regression evidence: GPU manager suites passed (58 tests). Focused DIMON warm-up/lifecycle tests passed (20 tests and 11 subtests). The full DIMON suite ran 649 tests: 633 passed, 16 failed in the pre-existing dirty banking/investing/UI worktree (static-root/voice expectations, Straits Telco ambiguity assumptions, and microphone-reopen string expectation). No preparation test failed.

## Execution Notes

- The GPU manager directory is outside this repository's normal writable root. Before implementation, obtain the runtime's approved write access for the requested supervisor/config/test files; do not write there using an unapproved workaround.
- Starting or restarting GPU services changes live external process state. Do this only after code/config tests pass and only for the identified managed stack and DIMON service.
- Do not commit automatically; preserve the user's current branch and unrelated dirty work.

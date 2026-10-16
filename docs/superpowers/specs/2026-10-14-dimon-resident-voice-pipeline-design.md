# Dimon Resident Voice Pipeline, Truthful Readiness, and Trace Diagnostics

## Status

Approved architecture for implementation. This design supersedes conflicting readiness, Qwen-context, and speech-fallback details in the earlier Dimon warm-up and shared-Qwen specifications. It does not change banking authorization or transaction approval rules.

## Goals

1. Eliminate leaked Nemotron session counts and make the supervisor self-heal if a client disappears.
2. Keep the actual Qwen, Nemotron, Kokoro, and Faster-Whisper workers loaded together during the advertised ready window.
3. Replace the normal CPU `small.en` speech fallback with project-local Faster-Whisper Medium on CUDA.
4. Keep Qwen Q5 while reducing its context from 8,192 to 4,096 tokens.
5. Make `READY` mean that the required model workers—not only their supervisors—are resident, healthy, and recently proven usable.
6. Add an optional in-app diagnostics panel that measures the complete red-D-to-reply path without recording sensitive content.

## Confirmed current behavior

### Nemotron lease leak

`NemotronLifecycleClient` stores one shared `_acquired` boolean. A shared `NemotronPcm16Recognizer` then hands that lifecycle client to multiple streams. Each stream can send `acquire`, but the first closing stream clears the boolean, causing later streams to skip `release`. The supervisor counts anonymous increments and decrements, so it cannot identify duplicate releases or expire an orphaned acquisition.

The gateway currently permits four concurrent transcription sessions even though the local Nemotron worker has not been established as a four-stream server. During live preparation/capacity checks, its reported active count rose from 72 to 74 after work had ended. This is a lifecycle-accounting defect, not valid activity.

Recent gateway failures also include `NEMOTRON_UNAVAILABLE` and upstream timeouts. The lease defect is proven, but it must not be claimed as the sole cause of every recognition failure. Stream capacity, connection, readiness, protocol, cancellation, and timeout failures need distinct measurement.

### Misleading preparation status

The preparation coordinator changes the overall state to `IDLE` when its ready lifetime expires but leaves prior per-stage `READY` values intact. It actively monitors only Nemotron after warm-up. Qwen and Kokoro can be unloaded while their old stages still say ready.

The preparation wrapper also exposes a pending capacity stage and zero stage latencies even after the operation completed. As a result, the current badge is proof that probes once returned—not proof that all model workers are resident now.

### Speech fallback

The fallback used by the hands-free wake path is a separate CPU Faster-Whisper service running CTranslate2 `small.en`, `int8`, with ten CPU threads. It is not the GPU OpenAI Whisper Turbo service. Across the observed two-hour sample, all 185 recorded transcription attempts selected this final fallback:

- median recognition: 5.0 seconds;
- 95th percentile: 16.3 seconds;
- worst: 72.4 seconds;
- successful wake events arrived 2.6–5.6 seconds after speech ended.

The target replacement is specifically Faster-Whisper Medium on CUDA. Whisper Turbo is not part of this design.

### Measured GPU capacity

The machine has an RTX 4070 with 12,282 MiB total VRAM. Live measurements showed:

| Resident worker | Observed VRAM |
| --- | ---: |
| Qwen3.5 9B Q5, 8K context | 5,906 MiB |
| Nemotron | 1,226 MiB |
| Kokoro | 810 MiB |
| Faster-Whisper Distil-Large-v3, CUDA `int8_float16` | 1,114 MiB |
| Desktop/display processes | 278 MiB |
| **Observed total** | **9,696 MiB** |

This left 2,170 MiB free, or about 1,658 MiB beyond the current 512 MiB safety reserve. It proves that the current three workers and a substantial CUDA CTranslate2 recognizer can coexist. Faster-Whisper Medium itself is not installed yet, so its real loaded and inference-peak memory must be measured before its final Overlord budget is accepted.

Changing Qwen Q5 from 8K to 4K saved only about 60 MiB in a temporary launch (5,906 to 5,846 MiB); reducing batch sizes saved only about another 8 MiB. The weights dominate this hybrid model's footprint. The 4K change is still suitable for Dimon because observed prompts were roughly 300–620 tokens and route output limits were 128–512 tokens, but it is not the main VRAM solution.

## Architecture

### 1. Lease-safe Nemotron lifecycle

Every Nemotron stream owns an independent lease object with a unique opaque lease ID. A stream can acquire once and release once. Release is idempotent, so duplicate cleanup is harmless. No stream consults or mutates another stream's acquired state.

The lease object is created for the utterance and is closed from one `finally` boundary covering successful finalization, upstream errors, client disconnect, cancellation, and timeout. A failed acquire never creates a live lease. A partially opened stream still releases any lease it did acquire.

The Nemotron supervisor replaces its anonymous integer counter with a set or map of live lease records:

- `acquire(lease_id)` inserts or refreshes one record instead of blindly incrementing;
- `release(lease_id)` removes that record if present and otherwise succeeds as a no-op;
- `active_jobs` is derived from the number of live lease records;
- each record has creation and last-heartbeat times;
- a bounded maximum lease age reaps an orphan left by a crashed client;
- closing the owning connection or receiving an explicit cancellation releases the lease promptly when the protocol can identify ownership;
- status exposes only bounded operational metadata, never audio or transcript text.

The gateway initially limits local Nemotron recognition to one active stream. Additional attempts wait in a bounded queue or fail quickly into the defined CUDA Faster-Whisper fallback. Concurrency can be raised later only after an explicit multi-stream correctness and latency benchmark.

After deploying the new protocol, restart the Nemotron supervisor/worker once to clear the existing corrupt count. A startup migration must not reinterpret the old anonymous count as live work. From then on, an expired or disconnected lease is self-healed rather than remaining `busy` indefinitely.

A Nemotron worker is degraded when its active count exceeds configured capacity, a lease exceeds its maximum age, or a health probe cannot acquire, stream, finalize, and return the count to its starting value. Preparation must not report it ready in those states.

### 2. Project-local Faster-Whisper Medium on CUDA

Install the Faster-Whisper runtime and Medium model beneath this repository at these project-owned paths:

- `.runtime/faster-whisper-medium/venv` for the isolated Python environment;
- `.runtime/models/faster-whisper-medium` for model artifacts and caches;
- `.runtime/cache/pip` and `.runtime/cache/huggingface` for installer/runtime caches;
- a versioned project-owned runner/configuration for its loopback service.

The runtime directory is ignored by Git. Installation must not use user-site packages, `~/.local`, a system Python environment, or a package/model cache outside the repository. `PIP_CACHE_DIR`, `HF_HOME`, `HUGGINGFACE_HUB_CACHE`, and `XDG_CACHE_HOME` point into `.runtime` during installation and service startup so cleanup is a bounded stop-and-delete operation. Persistent service management may reference the project runner, but it must not copy the Python runtime, dependencies, model, or caches outside this repository; any small external service-registration file must be documented in the cleanup command.

The service loads Faster-Whisper `medium` on `cuda` with `int8_float16` compute and one worker initially. Startup fails closed if CUDA is unavailable, a different model is loaded, or the service silently falls back to CPU. Its health/status response must report a bounded model identity, compute device/type, process ID, and readiness timestamp.

GPU Overlord gains a distinct Faster-Whisper service/profile for this worker. Its memory budget is based on measured load plus representative inference peak and allocator headroom. It is not aliased to the existing OpenAI Whisper Turbo profile.

The hands-free fallback route points to this CUDA Medium service. The old CPU Small service may remain as a final emergency fallback during rollout, but it is not considered warm, does not satisfy `READY`, and must be identified explicitly if selected. Once the CUDA path passes soak testing, disabling the CPU fallback can be a separate operational choice.

### 3. Qwen Q5 at a fixed 4K context

The managed Qwen profile `qwen35-9b-abliterated-q5` launches with `CTX_SIZE=4096` regardless of Nemotron state. Q5 quantization, the model file, endpoint, GPU layer behavior, and existing route-specific output limits stay unchanged.

The current managed supervisor already supplies the context through an environment override, so the preferred change is its Qwen profile mapping rather than the model runner script. If `<llama-runtime>/run-qwen35-9b-abliterated.sh` must be edited, create a restorable timestamped backup before the first edit and record its original SHA-256 (`89d6038c57c7b84adafeae92fa0ece3335843911bb60793a0330a20fb3bf2cda`). Do not overwrite that backup during later iterations.

The final launch status must prove the effective context is 4,096. Passing an environment variable without observing the launched worker configuration is insufficient.

### 4. Truthful four-worker `READY`

The red-D preparation operation warms four actual workers concurrently where admission permits:

1. Qwen3.5 9B Q5 with an effective 4K context;
2. Nemotron;
3. Kokoro;
4. Faster-Whisper Medium on CUDA.

Each stage performs a small real probe through its production path. Process-list presence or an HTTP supervisor response alone is insufficient. The final readiness gate obtains a fresh Overlord snapshot and requires, for every worker:

- an actual non-supervisor worker PID;
- the expected model/profile identity;
- a ready or legitimately busy managed state;
- a fresh heartbeat;
- a successful recent real probe;
- no expired, over-capacity, or stuck leases;
- observed VRAM within its calibrated budget.

The gate also requires measured free VRAM to remain at or above the existing 512 MiB safety reserve after all four workers are loaded and have completed a representative warm inference. A profile that only fits while idle does not pass.

Preparation pins or refreshes the four workers for the advertised ready lifetime, currently five minutes. Worker idle-retention and readiness expiry use the same deadline. If any worker exits, changes profile, becomes stale, breaches a health guard, or is evicted, readiness drops on the next bounded poll rather than remaining green.

When readiness expires, both the overall state and all per-worker stages are reset to cold/idle. Old success timestamps and latencies may remain only as labeled historical observations; they cannot be rendered as current readiness. Capacity must be `READY` or `DEGRADED`, never `PENDING`, when the overall operation says `READY`.

The combined browser badge is green only when backend four-worker readiness and browser audio readiness are both true. Backend readiness comprises model residency; browser readiness comprises an unlocked audio context, an initialized microphone/worklet, and a hands-free WebSocket that acknowledged `ready`.

### 5. End-to-end trace identity and timing

Clicking the red D creates a random, non-sensitive preparation/interaction trace ID. The ID is propagated through the browser, Dimon hands-free WebSocket, inference gateway, and bounded upstream requests. An utterance receives its own attempt ID linked to the parent trace.

Every component uses a monotonic clock for durations within that process. Wall-clock timestamps are used only to correlate cross-process events; durations are not calculated by subtracting unsynchronized clocks. Server receipt events bridge the timing domains.

The latest trace records these milestones when applicable:

1. red D clicked;
2. browser audio context ready;
3. microphone/worklet ready;
4. hands-free socket ready;
5. backend four-worker ready;
6. speech detected;
7. Nemotron stream ready;
8. first Nemotron partial;
9. wake phrase matched;
10. speaker verification completed;
11. browser received wake;
12. reply audio ready;
13. reply playback started.

It also records utterance endpoint time, authoritative transcription completion, selected recognition path, and a bounded failure phase/reason. Failure reasons distinguish connection, acquire, queue, readiness, stream, protocol, no-speech, timeout, cancellation, speaker decision, and fallback failure. They do not collapse every upstream failure into `NEMOTRON_UNAVAILABLE`.

Tracing stores no raw audio, transcript text, voice embeddings, prompt text, recipient/payment details, or generated reply content. Keep only a bounded in-memory ring buffer plus structured operational logs, with short retention controlled by existing logging policy. Trace propagation must never become authorization data.

### 6. In-app diagnostics panel

Add a compact, collapsible diagnostics panel for development and demo preparation. It shows the latest trace as a simple timing waterfall and displays, for each required worker:

- model/profile name;
- loaded/unloaded state;
- actual worker PID;
- heartbeat age;
- current VRAM;
- active jobs or live leases;
- last real-probe result.

It also shows total GPU used/free memory, reserve headroom, selected recognition path, and the bounded failed phase when a fallback occurs.

A `Hide diagnostics` control hides the panel for recordings without disabling measurement. The preference is stored locally in the browser and can be restored with a small settings/control affordance. Hiding the panel does not alter readiness, worker residency, or tracing. The panel consumes a sanitized latest-trace/status endpoint and never exposes service credentials or unrestricted infrastructure controls.

## Request flow

1. The user clicks the red D.
2. The browser immediately unlocks audio and starts microphone/socket readiness while requesting backend preparation.
3. The gateway obtains a four-worker capacity admission and warms the actual workers through real probes.
4. The final gate verifies identities, PIDs, heartbeats, lease health, effective Qwen context, CUDA Medium identity, and post-inference VRAM reserve.
5. Only then can backend status become `READY`; only after browser readiness can the UI say `Ready — say “Hey Dimon”`.
6. Speech starts an utterance-scoped Nemotron lease immediately and streams audio live.
7. A valid partial can wake Dimon after the existing speaker rule passes. The complete request still waits for an authoritative Faster-Whisper final.
8. If Nemotron fails, the retained bounded utterance audio goes to CUDA Faster-Whisper Medium and the trace preserves the exact failed phase.
9. Every success, error, disconnect, cancellation, and timeout closes the utterance lease exactly once.
10. Reply generation and Kokoro playback add their milestones to the same trace.

## Failure behavior

- A worker failing its real probe makes preparation `DEGRADED`; the badge cannot remain green.
- Insufficient measured VRAM blocks four-worker readiness rather than overcommitting the GPU or lowering the safety reserve silently.
- A Nemotron queue timeout falls back predictably and cannot create a lease that survives the request.
- A gateway/browser disconnect cancels owned work and releases its lease.
- A client crash is healed by the supervisor lease expiry.
- A Faster-Whisper CUDA load failure cannot masquerade as Medium-ready by using CPU or another Whisper model.
- A diagnostics endpoint failure affects only the panel, not banking operation; readiness still fails closed if its underlying worker evidence is unavailable.
- No warm-up or trace path performs a banking write or executes a financial intent.

## Verification and acceptance

### Nemotron lifecycle

- Concurrent stream tests prove N successful acquisitions produce N independent releases.
- Duplicate release is idempotent.
- Acquisition failure leaves no lease.
- Success, upstream error, timeout, task cancellation, browser disconnect, and gateway shutdown all release.
- A killed client leaves an orphan that the supervisor reaps within the configured lease deadline.
- A 100-cycle acquire/stream/finalize/cancel soak test ends at `active_jobs == 0` with no monotonic count growth.
- Configured active work never exceeds one until a later benchmark changes the capacity.
- Preparation probes return the active count to its pre-probe value.

### Faster-Whisper CUDA Medium

- Runtime, dependencies, model, and caches all resolve under the repository `.runtime` tree; user-site and system environments are unchanged.
- Status and process inspection prove model `medium`, device `cuda`, and compute type `int8_float16`.
- A real inference establishes loaded and peak VRAM; Overlord's profile covers the peak plus headroom.
- All four workers remain simultaneously resident above the 512 MiB reserve while each performs a representative request.
- Wake-phrase and request audio tests report accuracy and p50/p95 latency; no fixed speed claim is made before measurement.
- Injected Nemotron failure selects CUDA Medium and records the precise fallback reason. CPU Small, if retained, appears only under its explicitly tested emergency condition.

### Qwen 4K

- Worker launch proves Q5 and an effective 4,096-token context.
- Existing banking intent, multi-action, stock, bill, and payment golden cases pass without truncation.
- Route output limits and transaction safeguards remain unchanged.
- If the external runner is edited, the original backup exists and matches the recorded checksum.

### Truthful readiness

- A live supervisor without its model worker cannot satisfy readiness.
- Wrong profile, stale heartbeat, failed probe, low reserve, expired retention, or stuck Nemotron lease forces `DEGRADED`/`IDLE`.
- `READY` exposes four verified worker PIDs and no pending capacity stage.
- The workers remain resident for the full advertised lifetime under normal load.
- Expiry and eviction clear current stage readiness instead of showing stale green entries.

### Diagnostics

- Tests correlate the complete D-click-to-playback sequence under one trace and preserve utterance IDs across fallback.
- Phase durations are non-negative and locally monotonic; cross-host data is clearly separated by receipt boundaries.
- The panel shows worker residency and timing, collapses cleanly on desktop/mobile, and its hide preference survives reload.
- API, logs, and UI contain no raw audio, transcript, embedding, prompt, payee, or payment content.

### Live acceptance

After unit/integration tests and a controlled service restart:

1. Reset the inherited Nemotron leak and verify `active_jobs == 0` while idle.
2. Warm once and prove all four model worker PIDs are resident simultaneously.
3. Run at least 20 “Hey Dimon” trials from a five-second post-click wait.
4. Report detection, first partial, wake match, speaker check, browser wake, and playback p50/p95 values.
5. Confirm the Nemotron live path is normally selected and fallback occurs only for injected or precisely classified failure.
6. Confirm `active_jobs` returns to zero after every completed/cancelled test and remains zero after the soak window.

If a latency target is missed, the measured phase identifies the remaining bottleneck; success is not inferred from service status alone.

## Rollout and rollback

Roll out in this order: lease protocol and concurrency guard; controlled Nemotron reset; project-local CUDA Medium installation and measured profile; Qwen 4K profile; truthful preparation gate; diagnostics panel; live soak test. Do not expose green readiness until every earlier dependency is deployed.

Rollback keeps schema and banking behavior intact: restore the prior Qwen supervisor mapping, stop the project-local Medium service, point fallback to the prior endpoint if necessary, and disable the new UI panel. Do not roll back to anonymous Nemotron counting after the new supervisor has accepted lease IDs. Preserve any required protocol compatibility during a rolling restart.

Project-local Faster-Whisper cleanup consists of stopping/disabling its service, removing its project-owned runner configuration if desired, and deleting the repository `.runtime/faster-whisper-medium` and `.runtime/models/faster-whisper-medium` trees. No user-site or system Python cleanup should be necessary.

## Out of scope

- Changing payment confirmation, approval, account, or ledger behavior.
- Using Nemotron partial text to execute or populate a financial action.
- Switching to Whisper Turbo.
- Changing Qwen quantization away from Q5.
- Hiding a worker failure to preserve a green demo badge.
- Persisting voice traces or content to the banking database.
- Promising a latency percentile before the instrumented live acceptance run.

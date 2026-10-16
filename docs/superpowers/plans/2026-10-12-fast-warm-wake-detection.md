# Fast Warm Wake Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open Dimon’s Ask experience within 1.3–2.0 seconds of a completed “Hey Dimon” wake phrase while preserving authoritative Faster-Whisper request transcription and the existing voice-identity boundary.

**Architecture:** Add a one-second in-memory browser audio ring buffer for connection startup. Route wake-sized utterances through the existing remote dual recognizer so warm Nemotron partials can trigger a bounded early wake while Faster-Whisper completes authoritative transcription; retain the dedicated CPU recognizer as fallback and prevent duplicate wake events.

**Tech Stack:** Python 3.12, asyncio, FastAPI WebSockets, browser ES modules, Web Audio API, Nemotron, Faster-Whisper, pytest, Node.js module tests.

**Spec:** `docs/superpowers/specs/2026-10-12-fast-warm-wake-detection-design.md`

## Global Constraints

- Target median warm-path wake event: no more than 2.0 seconds after speech ends.
- Nemotron partial text may detect a wake phrase but must never execute, approve, or fill a financial action.
- Faster-Whisper remains authoritative for complete request text.
- Registered voice identity must pass before an early wake event is emitted.
- CPU Faster-Whisper wake recognition remains the outage fallback.
- Browser pre-buffer holds at most one second, is never persisted, and excludes Dimon’s speech plus the echo-suppression tail.
- Existing public WebSocket message shapes remain compatible.

## Review Focus

- A partial briefly contains “Hey Dimon” and is then corrected: require a stable bounded match before waking.
- Nemotron succeeds after the CPU fallback has begun: emit only one wake event.
- The speaker check finishes after the partial match: wait for a match; never bypass identity.
- A reconnect occurs with old buffered frames: discard frames belonging to the replaced socket.
- Dimon begins speaking while startup frames are buffered: clear the buffer and suppress self-audio.

---

### Task 1: One-second browser startup pre-buffer

**Files:**
- Create: `apps/dimon_live/static/audio_prebuffer.mjs`
- Modify: `apps/dimon_live/static/hands_free.mjs`
- Modify: `apps/dimon_live/app.py`
- Test: `apps/dimon_live/tests/test_ui_client.py`
- Test: `apps/dimon_live/tests/test_api.py`

**Interfaces:**
- Produces: `RollingAudioPrebuffer(maxBytes: number)` with `push(frame: ArrayBuffer): void`, `drain(): ArrayBuffer[]`, `clear(): void`, and readonly `byteLength: number`; the client sends bounded control metadata `{type: "prebuffer_flushed", duration_ms: 0..1000}` before drained frames.
- Consumes: 16 kHz Float32 frames emitted by the existing `VoiceFrameResampler`; one second is exactly `16_000 * 4` bytes.

- [ ] **Step 1: Write failing unit tests for the bounded FIFO**

Add Node-driven tests named `test_audio_prebuffer_retains_only_latest_second_in_order` and `test_audio_prebuffer_drain_and_clear_remove_old_frames`. Assert a 64,000-byte cap, chronological drain order, zero bytes after drain, and no stale frames after clear.

- [ ] **Step 2: Run the FIFO tests and verify RED**

Run: `UV_CACHE_DIR=/tmp/dimon-uv-cache uv run pytest apps/dimon_live/tests/test_ui_client.py -k audio_prebuffer -q`

Expected: FAIL because `audio_prebuffer.mjs` does not exist.

- [ ] **Step 3: Implement `RollingAudioPrebuffer`**

Create the module with copied frame ownership, FIFO trimming from the oldest edge, and no timers or persistence.

- [ ] **Step 4: Write failing integration tests for startup, reconnect, and audible suppression**

Add static/client-harness assertions that `hands_free.mjs` buffers resampled frames while its current socket is connecting, drains only after that socket’s `ready` message, clears on socket replacement/stop, and clears without sending when `speaking`, `hooks.isDimonAudible()`, or the 350 ms echo tail is active.

- [ ] **Step 5: Run the integration tests and verify RED**

Run: `UV_CACHE_DIR=/tmp/dimon-uv-cache uv run pytest apps/dimon_live/tests/test_ui_client.py -k 'prebuffer or audible' -q`

Expected: FAIL because `hands_free.mjs` does not yet integrate the buffer.

- [ ] **Step 6: Integrate the pre-buffer and same-origin route**

Import `RollingAudioPrebuffer`, create one 64,000-byte buffer per active socket generation, send bounded flush metadata and then drain frames only after the matching server `ready`, and clear it at every stop/reconnect/turn reset and audible suppression boundary. Add `/static/audio_prebuffer.mjs` with JavaScript content type and `Cache-Control: no-store`; accept the metadata as non-operative telemetry in the hands-free server.

- [ ] **Step 7: Run Task 1 tests and verify GREEN**

Run: `UV_CACHE_DIR=/tmp/dimon-uv-cache uv run pytest apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_api.py -q`

Expected: PASS.

- [ ] **Step 8: Commit Task 1**

```bash
git add apps/dimon_live/static/audio_prebuffer.mjs apps/dimon_live/static/hands_free.mjs apps/dimon_live/app.py apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_api.py
git commit -m "fix: preserve microphone audio during voice startup"
```

### Task 2: Stable Nemotron partial wake matcher

**Files:**
- Modify: `apps/dimon_live/hands_free.py`
- Test: `apps/dimon_live/tests/test_hands_free.py`

**Interfaces:**
- Produces: `WakePartialMatcher.push(text: str) -> tuple[bool, str]`. An exact standalone accepted wake phrase may match once; phonetic/fuzzy variants or partials containing command words require the same normalized wake result in two consecutive non-empty updates.
- Consumes: existing `spoken_words(text)` and `voice_dialog.wake(text)` normalization.

- [ ] **Step 1: Write failing matcher tests**

Add tests covering two stable “Hey Dimon” partials, accepted phonetic variants, a corrected/transient match, “the demon in the film,” and a wake phrase followed by command words. Assert transient/non-wake inputs return `(False, "")`.

- [ ] **Step 2: Run matcher tests and verify RED**

Run: `UV_CACHE_DIR=/tmp/dimon-uv-cache uv run pytest apps/dimon_live/tests/test_hands_free.py -k partial_matcher -q`

Expected: FAIL because `WakePartialMatcher` does not exist.

- [ ] **Step 3: Implement `WakePartialMatcher`**

Keep the matcher deterministic and stateful only within one utterance. Reset stability whenever a non-empty partial normalizes to a different wake result or no accepted wake. Never expose partial command text as authoritative.

- [ ] **Step 4: Run matcher tests and verify GREEN**

Run: `UV_CACHE_DIR=/tmp/dimon-uv-cache uv run pytest apps/dimon_live/tests/test_hands_free.py -k partial_matcher -q`

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add apps/dimon_live/hands_free.py apps/dimon_live/tests/test_hands_free.py
git commit -m "feat: detect stable Nemotron wake partials"
```

### Task 3: Fast wake orchestration with authoritative final and fallback

**Files:**
- Modify: `apps/dimon_live/hands_free.py`
- Modify: `apps/dimon_live/app.py`
- Test: `apps/dimon_live/tests/test_hands_free.py`
- Test: `apps/dimon_live/tests/test_inference_modes.py`

**Interfaces:**
- Consumes: `WakePartialMatcher.push(text)` from Task 2; `selected_providers.recognizer` as the dual remote recognizer; existing `wake_recognizer` as CPU fallback.
- Produces: `HandsFreeBridge(..., fast_wake_recognizer: Any | None = None, fast_wake_lease: VoiceLease | None = None)` and one `wake` event per utterance with optional `path: "nemotron_partial" | "faster_whisper_final"` metadata.

- [ ] **Step 1: Write failing fast-path behavior tests**

Add async/WebSocket tests that assert: a stable partial emits `wake` before the recognizer final resolves; a non-wake partial does not; an authoritative final cannot emit a duplicate; a partial wake plus command does not dispatch the partial command; and final Faster-Whisper text supplies the command.

- [ ] **Step 2: Add failing identity and race tests**

Assert a registered speaker mismatch never receives early wake, a delayed successful speaker check releases a pending stable match, and simultaneous fast/fallback completion emits only one event.

- [ ] **Step 3: Add failing fallback/configuration tests**

Assert remote mode injects `selected_providers.recognizer` as `fast_wake_recognizer`, local/fixture modes preserve current behavior, and a fast recognizer exception falls back to the existing CPU wake recognizer.

- [ ] **Step 4: Run Task 3 tests and verify RED**

Run: `UV_CACHE_DIR=/tmp/dimon-uv-cache uv run pytest apps/dimon_live/tests/test_hands_free.py apps/dimon_live/tests/test_inference_modes.py -k 'wake or remote' -q`

Expected: FAIL because fast wake orchestration is absent.

- [ ] **Step 5: Implement fast wake orchestration**

For asleep utterances, run speaker similarity and dual remote transcription concurrently. Feed partials only into `WakePartialMatcher`; once stable and identity-safe, send one early wake with no executable command. Await the authoritative final for command text. On remote/Nemotron failure, run the existing CPU wake recognizer. Use an utterance-scoped sent flag to arbitrate all completion races.

- [ ] **Step 6: Run Task 3 tests and verify GREEN**

Run: `UV_CACHE_DIR=/tmp/dimon-uv-cache uv run pytest apps/dimon_live/tests/test_hands_free.py apps/dimon_live/tests/test_inference_modes.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add apps/dimon_live/hands_free.py apps/dimon_live/app.py apps/dimon_live/tests/test_hands_free.py apps/dimon_live/tests/test_inference_modes.py
git commit -m "feat: wake Dimon from warm Nemotron partials"
```

### Task 4: Bounded latency telemetry and deployment verification

**Files:**
- Modify: `apps/dimon_live/hands_free.py`
- Test: `apps/dimon_live/tests/test_hands_free.py`

**Interfaces:**
- Consumes: fast/fallback path selection from Task 3.
- Produces: content-free structured log fields `wake_path`, `speech_end_to_match_ms`, `speech_end_to_wake_ms`, and `prebuffer_ms`; no transcript or raw audio.

- [ ] **Step 1: Write failing telemetry tests**

Capture logs for fast and fallback wake cases. Assert the bounded path/timing field names are present and transcript text is absent.

- [ ] **Step 2: Run telemetry tests and verify RED**

Run: `UV_CACHE_DIR=/tmp/dimon-uv-cache uv run pytest apps/dimon_live/tests/test_hands_free.py -k wake_latency -q`

Expected: FAIL because timing telemetry is absent.

- [ ] **Step 3: Implement content-free timing logs**

Use `time.monotonic()` at utterance completion, stable partial match, and wake emission. Clamp recorded millisecond values to non-negative integers and log only bounded metadata.

- [ ] **Step 4: Run focused and full verification**

Run:

```bash
node --check apps/dimon_live/static/hands_free.mjs
node --check apps/dimon_live/static/audio_prebuffer.mjs
UV_CACHE_DIR=/tmp/dimon-uv-cache uv run pytest apps/dimon_live/tests/test_hands_free.py apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_api.py apps/dimon_live/tests/test_inference_modes.py apps/dimon_live/tests/test_remote_inference.py services/inference_gateway/tests/test_transcription_endpoint.py -q
```

Expected: PASS with no failures.

- [ ] **Step 5: Commit telemetry**

```bash
git add apps/dimon_live/hands_free.py apps/dimon_live/tests/test_hands_free.py
git commit -m "chore: measure bounded wake latency"
```

- [ ] **Step 6: Deploy and verify the warm path**

Restart `dimon.service` and `dimon-inference-gateway.service`, warm services through the existing preparation endpoint/logo action, verify all public JavaScript modules return 200, then perform at least five real “Hey Dimon” trials. Record median `speech_end_to_wake_ms`; accept deployment only when the warm-path median is at most 2,000 ms, no duplicate wake appears, and immediate post-greeting words remain intact.

- [ ] **Step 7: Push `develop`**

Push the verified commits to `origin/develop` without rewriting history.

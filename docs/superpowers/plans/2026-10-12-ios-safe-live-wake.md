# iOS-Safe Speech Playback and Live Wake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make blocked iOS speech playback recoverable and make the first warm “Hey Dimon” use a resilient, genuinely live Nemotron stream with a two-second median target.

**Architecture:** The existing visible reply player becomes the single owner of hands-free audio and waits for a real `ended` event. Browser readiness combines backend preparation, AudioContext, microphone/worklet, and WebSocket state. On the server, an utterance-scoped dual-recognition stream opens at VAD start, receives chunks during capture, uses bounded Nemotron partials only to wake the UI, and returns Faster-Whisper’s final transcript as authoritative; an actual-stream keepalive and bounded retries make Nemotron failures visible and self-recovering.

**Tech Stack:** FastAPI/Starlette WebSockets, Python asyncio, browser ES modules, Web Audio API/AudioWorklet, HTMLMediaElement, Faster-Whisper, Nemotron, unittest/pytest, Node-based ES-module tests.

**Spec:** `docs/superpowers/specs/2026-10-12-ios-safe-live-wake-design.md`

## Global Constraints

- Faster-Whisper remains authoritative for complete requests; Nemotron partial text may only wake the UI and render captions.
- Registered voice identity requires a positive `MATCH`; mismatch, unknown, insufficient audio, and timeout must fail closed.
- No browser-native speech recognition or speech synthesis may be introduced.
- The existing one-second microphone pre-buffer remains bounded and excludes Dimon playback and its echo-suppression tail.
- Wake-only capture is capped at approximately 2.5 seconds; active request capture keeps its existing limits.
- A blocked autoplay attempt must remain pending until the visible player reaches `ended` or the operation is explicitly cancelled.
- Telemetry must not contain transcript text, raw audio, or voice embeddings.

## Review Focus

- Autoplay rejects after the player source is loaded: show a paused player, keep the mic suppressed, and resume only after manual playback reaches `ended` (Task 1 tests).
- The D button is pressed while iOS AudioContext is suspended or permission is denied: unlock synchronously, never show false green-ready, and give a recoverable status (Task 2 tests).
- Nemotron opens slowly while audio arrives: preserve ordered pre-roll/live chunks without duplication or unbounded buffering (Task 3 tests).
- A progressive partial changes from “Hey Diamond” to “Hey Diamond send…” while speaker verification is still pending: wake exactly once only after a registered voice matches (Task 4 tests).
- Nemotron disconnects after preparation or during a stream: retry/probe it, preserve Faster-Whisper service, surface a precise phase, and attempt Nemotron again on the next utterance (Task 5 tests).

---

### Task 1: One completion-aware visible speech player

**Files:**
- Modify: `apps/dimon_live/static/result_audio.mjs`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/hands_free.mjs`
- Test: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Produces: `playAudioToCompletion(audioElement, {playImpl, onBlocked, onFailure, signal}) -> Promise<"ended" | "cancelled" | "failed">` in `result_audio.mjs`.
- Produces: `hooks.playSpeechBlob(blob) -> Promise<"ended" | "cancelled" | "failed">` supplied by `app.js` to `initHandsFree`.
- Consumes: the existing `#reply-audio-player`, `#result-audio`, `syncReplyAudioPlayer()`, and `clearResultAudio()`.

- [ ] **Step 1: Write failing result-player state tests**

Add Node-backed tests named `test_hands_free_audio_autoplay_resolves_only_on_ended`, `test_hands_free_audio_blocked_autoplay_waits_for_manual_ended`, and `test_hands_free_audio_abort_resolves_cancelled`. Assert that a rejected `play()` calls `onBlocked`, does not settle the promise, and settles only after the same audio element emits `ended`; abort must remove listeners and settle `cancelled`.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run pytest apps/dimon_live/tests/test_ui_client.py -k 'hands_free_audio' -q`

Expected: FAIL because `playAudioToCompletion` is not exported.

- [ ] **Step 3: Implement `playAudioToCompletion`**

Add the exact export to `result_audio.mjs`. Set playback rate to `AGENT_VOICE_PLAYBACK_RATE`, attach one-shot completion/error listeners before calling `playImpl`, treat `NotAllowedError` as blocked rather than complete, and keep waiting for manual playback. Only an `ended`, media error, or abort may settle it.

- [ ] **Step 4: Route hands-free blobs through the visible player**

In `app.js`, implement the `playSpeechBlob` hook: clear/revoke the old object URL, attach the new blob to `ui.resultAudio`, show/sync the existing player, report blocked status as `Tap play to hear Dimon`, and return the completion result. In `hands_free.mjs`, replace `new Audio(...)` with this hook and retain `speaking=true` plus microphone suppression until the hook returns `ended`; explicit stop uses an `AbortController`.

- [ ] **Step 5: Run focused browser tests**

Run: `uv run pytest apps/dimon_live/tests/test_ui_client.py -k 'hands_free_audio or result_audio' -q`

Expected: PASS; blocked autoplay does not reopen the microphone.

- [ ] **Step 6: Commit**

```bash
git add apps/dimon_live/static/result_audio.mjs apps/dimon_live/static/app.js apps/dimon_live/static/hands_free.mjs apps/dimon_live/tests/test_ui_client.py
git commit -m "fix: keep blocked speech in visible player"
```

### Task 2: Honest first-wake readiness from the D control

**Files:**
- Modify: `apps/dimon_live/static/hands_free.mjs`
- Modify: `apps/dimon_live/static/app.js`
- Test: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Produces: `wakeWord.prepareFromGesture() -> Promise<HandsFreeReadiness>` where `HandsFreeReadiness` has booleans `audio`, `microphone`, and `socket` plus `state` in `STARTING | READY | FAILED`.
- Produces: `hooks.onReadiness(readiness)` notifications whenever AudioContext, mic/worklet, or socket readiness changes.
- Consumes: Task 1’s speech suppression; `refreshPreparation(start)` continues to own backend preparation.

- [ ] **Step 1: Write failing readiness tests**

Add Node-backed tests named `test_d_warm_unlocks_audio_before_fetch_await`, `test_green_ready_requires_backend_audio_microphone_and_socket`, and `test_first_frames_before_socket_ready_are_prebuffered`. Assert call ordering (`context.resume` begins before the preparation fetch), that any false component prevents green-ready, and that the buffer flush remains at most 1,000 ms.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `uv run pytest apps/dimon_live/tests/test_ui_client.py -k 'warm_unlocks or green_ready or first_frames' -q`

Expected: FAIL because the preparation button does not coordinate with hands-free readiness.

- [ ] **Step 3: Expose explicit hands-free readiness**

In `hands_free.mjs`, split audio unlock from asynchronous microphone construction, implement `prepareFromGesture()`, and publish readiness changes. A `ready` socket message sets only `socket=true`; `AudioContext.state === "running"` and successful worklet/microphone creation set their own flags. Reconnect or track loss clears the affected flags immediately.

- [ ] **Step 4: Combine browser and backend readiness in `app.js`**

Change the D click listener to call `wakeWord.prepareFromGesture()` synchronously inside the click task before awaiting `refreshPreparation(true)`. Render `Warming services`, then `Starting microphone`, then `Ready — say “Hey Dimon”`. Set `data-state="ready"` only when the backend snapshot is `READY` and all three browser flags are true; permission denial or socket failure renders `degraded` with a specific recovery message.

- [ ] **Step 5: Run readiness and pre-buffer regression tests**

Run: `uv run pytest apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_hands_free.py -k 'readiness or prebuffer or green_ready or warm_unlocks' -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/dimon_live/static/hands_free.mjs apps/dimon_live/static/app.js apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_hands_free.py
git commit -m "fix: gate warm status on voice readiness"
```

### Task 3: Open and feed the authoritative dual stream at VAD start

**Files:**
- Create: `apps/dimon_live/live_transcription.py`
- Modify: `apps/dimon_live/hands_free.py`
- Test: `apps/dimon_live/tests/test_hands_free.py`

**Interfaces:**
- Produces: `LiveTranscriptionSession(recognizer, lease, on_partial, acquire_timeout, max_pending_bytes)`.
- Produces methods: `start() -> None`, `push(audio: bytes) -> None`, `finish() -> str`, and `cancel() -> None`, all async.
- Produces: `Segmenter.active_audio() -> bytes` for the VAD pre-roll/current audio at the `started` transition.
- Consumes: the recognizer `open_stream(on_partial)`, whose dual gateway stream emits Nemotron partials and returns Faster-Whisper final text.

- [ ] **Step 1: Write failing live-session unit tests**

Add tests `test_live_session_opens_at_speech_start`, `test_live_session_queues_chunks_in_order_while_opening`, `test_live_session_never_duplicates_start_chunk`, `test_live_session_bounds_pending_audio`, and `test_live_session_releases_lease_on_finish_and_cancel`. Use a gated fake stream and assert the authoritative final is returned.

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest apps/dimon_live/tests/test_hands_free.py -k 'live_session' -q`

Expected: FAIL because `live_transcription.py` and `Segmenter.active_audio()` do not exist.

- [ ] **Step 3: Implement `LiveTranscriptionSession`**

Acquire the lease with the existing bounded wait behavior, open one stream, queue only up to `max_pending_bytes` while it connects, flush in order, and guarantee close plus lease release exactly once on finish, cancellation, or failure. Do not retry inside this class; Task 5 owns Nemotron/gateway recovery.

- [ ] **Step 4: Expose active VAD audio and the wake-only cap**

Add `Segmenter.active_audio()` and allow `set_max_seconds(seconds: float)` or an equivalent exact mode switch. Use `2.5` seconds while the mode is `asleep`; restore `MAX_UTTERANCE_SECONDS` for command, reply, approval, and enrollment modes. Keep the existing 0.3-second pre-roll and 0.7-second silence endpoint.

- [ ] **Step 5: Integrate the live session into `HandsFreeBridge.handle`**

At the `started` event in asleep mode, create the session immediately and send `active_audio()` once. Forward every subsequent active chunk before endpoint processing. At `utterance`, attach the session to the queued utterance job and call `finish()` in the worker instead of reopening and replaying the complete audio. If startup/push fails, cancel the session and retain the completed VAD audio for the existing local/Faster-Whisper fallback. Non-asleep turns retain current ordering and authoritative semantics.

- [ ] **Step 6: Run server wake regressions**

Run: `uv run pytest apps/dimon_live/tests/test_hands_free.py -q`

Expected: PASS; tests prove chunks reach the stream before endpoint and wake-only noise stops around 2.5 seconds.

- [ ] **Step 7: Commit**

```bash
git add apps/dimon_live/live_transcription.py apps/dimon_live/hands_free.py apps/dimon_live/tests/test_hands_free.py
git commit -m "feat: stream wake audio during capture"
```

### Task 4: Progressive wake matching, speaker gate, and exactly-once arbitration

**Files:**
- Modify: `apps/dimon_live/hands_free.py`
- Modify: `apps/dimon_live/voice_dialog.py`
- Test: `apps/dimon_live/tests/test_hands_free.py`
- Test: `apps/dimon_live/tests/test_voice_dialog.py`

**Interfaces:**
- Produces: `WakePartialMatcher.push(text: str, *, final: bool = False) -> tuple[bool, str]`.
- Produces: one utterance-scoped arbitration record containing `utterance_id`, `wake_emitted`, `partial_matched_at`, `speaker_state`, and `stream_state`.
- Consumes: Task 3’s live partial callback and completed utterance audio.

- [ ] **Step 1: Replace repeated-string expectations with progressive tests**

Test exact and progressive sequences: `Hey` → `Hey Diamond` → `Hey Diamond send`, `Hi Diamon`, and `Hey Dimon`. Assert “demon,” incidental mentions, and incomplete prefixes do not match. Assert later final results cannot produce a second wake.

- [ ] **Step 2: Add registered-speaker race tests**

Add `test_partial_waits_for_registered_speaker_match`, `test_partial_never_wakes_on_speaker_unknown_or_timeout`, and `test_unregistered_partial_wakes_without_voice_check`. Verify that no request is dispatched from partial text and that a later Faster-Whisper final is the only command evidence.

- [ ] **Step 3: Run focused tests and verify failure**

Run: `uv run pytest apps/dimon_live/tests/test_hands_free.py apps/dimon_live/tests/test_voice_dialog.py -k 'partial or speaker or duplicate' -q`

Expected: FAIL under the identical-consecutive-partial matcher and post-endpoint speaker flow.

- [ ] **Step 4: Implement progressive matching and arbitration**

Normalize only bounded `Hey|Hi` plus `Dimon|Diamon|Diamond` variants. A standalone complete wake may match immediately; a wake prefix with following text must remain present across two forward-progressing partials or be confirmed final. Start speaker similarity from the current accumulated audio once a partial matches; emit only after `MATCH` when registered. Use `wake_emitted` to reconcile Nemotron partial, Nemotron final, Faster-Whisper final, fallback, reconnect, and cancellation.

- [ ] **Step 5: Preserve authoritative command handling**

Allow an early partial to send only `{type:"wake", path:"nemotron_partial"}`. Do not attach or dispatch its command. When Task 3’s stream finishes, parse and dispatch only the Faster-Whisper final; if it contains only the wake phrase, open Ask without a command.

- [ ] **Step 6: Run full hands-free and dialog tests**

Run: `uv run pytest apps/dimon_live/tests/test_hands_free.py apps/dimon_live/tests/test_voice_dialog.py -q`

Expected: PASS with exactly one wake per utterance and all speaker checks fail-closed.

- [ ] **Step 7: Commit**

```bash
git add apps/dimon_live/hands_free.py apps/dimon_live/voice_dialog.py apps/dimon_live/tests/test_hands_free.py apps/dimon_live/tests/test_voice_dialog.py
git commit -m "fix: arbitrate progressive wake partials safely"
```

### Task 5: Nemotron self-recovery, actual-stream health, and precise telemetry

**Files:**
- Modify: `services/inference_gateway/transcription.py`
- Modify: `services/inference_gateway/preparation.py`
- Modify: `apps/dimon_live/voice.py`
- Modify: `apps/dimon_live/hands_free.py`
- Test: `services/inference_gateway/tests/test_transcription_endpoint.py`
- Test: `services/inference_gateway/tests/test_preparation.py`
- Test: `apps/dimon_live/tests/test_voice.py`
- Test: `apps/dimon_live/tests/test_hands_free.py`

**Interfaces:**
- Produces: `DualTranscriptionRecognizer(..., caption_open_attempts=2, caption_retry_delay=0.2)`.
- Produces: `GatewayPreparation(..., health_interval_seconds=15.0)` with a background actual-stream probe while its ready window remains active.
- Produces internal Nemotron phase codes: `connect`, `ready_timeout`, `stream_closed`, `protocol`, `empty_final`, and `upstream_error`; public responses remain bounded stable error codes.
- Consumes: existing Nemotron lifecycle `acquire()`/`release(disconnected=True)` so a failed connection requests supervisor recovery.

- [ ] **Step 1: Write failing gateway recovery tests**

Add tests asserting a failed first caption open is retried once, the authoritative stream remains usable when both caption attempts fail, a later utterance attempts captions again, and no caption final can replace the authoritative final.

- [ ] **Step 2: Write failing preparation keepalive tests**

Use a short injected interval and fake clock/gates to assert that READY performs a real Nemotron stream open/close probe, a failed probe changes only the Nemotron stage to `FAILED`/overall `DEGRADED`, the next probe retries, and recovery restores `READY`. Assert `stop()` cancels the monitor and closes any probe stream.

- [ ] **Step 3: Write failing error-classification tests**

Cover connection refusal, session-ready timeout, premature close, malformed event, empty final, and upstream error event. Assert logs/diagnostic stage distinguish them while responses expose no URL, transcript, exception text, or credentials.

- [ ] **Step 4: Run focused tests and verify failure**

Run: `uv run pytest services/inference_gateway/tests/test_transcription_endpoint.py services/inference_gateway/tests/test_preparation.py apps/dimon_live/tests/test_voice.py apps/dimon_live/tests/test_hands_free.py -k 'retry or keepalive or nemotron or caption' -q`

Expected: FAIL because caption opening is single-attempt, preparation has no monitor, and Nemotron uses one broad failure bucket.

- [ ] **Step 5: Add bounded caption recovery**

In `DualTranscriptionRecognizer.open_stream`, keep the already-open authoritative stream, retry only the caption stream up to `caption_open_attempts`, delay `caption_retry_delay`, and log a phase/code without transcript data. If all attempts fail, return the authoritative-only stream. Every new utterance starts with a fresh retry budget.

- [ ] **Step 6: Add preparation actual-stream keepalive**

After preparation becomes READY, start one monitor task that probes the Nemotron caption target every `health_interval_seconds` until `ready_until` or `stop()`. A probe failure marks Nemotron degraded but does not stop Faster-Whisper/Qwen/Kokoro; subsequent probes attempt recovery and restore READY only after a real stream opens and closes successfully.

- [ ] **Step 7: Classify Nemotron failures precisely**

Map each internal phase in `voice.py` and hands-free telemetry without changing privacy rules. On disconnect-class failures, call lifecycle release with `disconnected=True` so the supervisor can restart/reload the model. Log `utterance_start_to_partial_ms`, `wake_match_ms`, endpoint reason/cap, speaker-check duration, authoritative-final duration, and selected path.

- [ ] **Step 8: Run resilience suites**

Run: `uv run pytest services/inference_gateway/tests/test_transcription_endpoint.py services/inference_gateway/tests/test_preparation.py apps/dimon_live/tests/test_voice.py apps/dimon_live/tests/test_hands_free.py -q`

Expected: PASS; Nemotron can degrade and recover without losing authoritative transcription.

- [ ] **Step 9: Commit**

```bash
git add services/inference_gateway/transcription.py services/inference_gateway/preparation.py apps/dimon_live/voice.py apps/dimon_live/hands_free.py services/inference_gateway/tests/test_transcription_endpoint.py services/inference_gateway/tests/test_preparation.py apps/dimon_live/tests/test_voice.py apps/dimon_live/tests/test_hands_free.py
git commit -m "fix: monitor and recover Nemotron streams"
```

### Task 6: Integrated verification and deployment

**Files:**
- Modify: `apps/dimon_live/README.md`
- Modify: `docs/superpowers/specs/2026-10-12-ios-safe-live-wake-design.md` only if measured behavior requires a documented correction
- Test: existing suites and production endpoints

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: deployed build plus a measured verification record in the final handoff; no unmeasured latency claim.

- [ ] **Step 1: Update operational documentation**

Document the combined readiness definition, iOS blocked-autoplay recovery, Nemotron keepalive/retry behavior, Faster-Whisper authority, precise diagnostic stages, and the physical-device validation procedure.

- [ ] **Step 2: Run syntax and focused cross-stack suites**

Run: `node --check apps/dimon_live/static/hands_free.mjs && node --check apps/dimon_live/static/app.js && UV_CACHE_DIR=/tmp/dimon-uv-cache uv run pytest apps/dimon_live/tests/test_speech.py apps/dimon_live/tests/test_remote_inference.py apps/dimon_live/tests/test_hands_free.py apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_demo_preparation.py services/inference_gateway/tests/test_transcription_endpoint.py services/inference_gateway/tests/test_preparation.py -q`

Expected: PASS.

- [ ] **Step 3: Run the complete relevant application and gateway suites**

Run: `UV_CACHE_DIR=/tmp/dimon-uv-cache uv run pytest apps/dimon_live/tests services/inference_gateway/tests -q`

Expected: PASS with no new warnings indicating leaked tasks, unhandled futures, or unreleased streams.

- [ ] **Step 4: Review the complete branch diff**

Run: `git diff --check && git status --short && git log --oneline --decorate -8`

Expected: no whitespace errors; only intended files are changed or committed.

- [ ] **Step 5: Deploy through the repository’s existing Dimon deployment procedure**

Restart only the services required by changed files, verify the inference gateway and Dimon app readiness endpoints, and confirm public JavaScript modules return HTTP 200. Do not alter Cloudflare routing unless endpoint verification proves routing is wrong.

- [ ] **Step 6: Perform measured device acceptance**

On iPhone Safari, verify blocked autoplay displays a paused player and the mic resumes only after `ended`. After the D control says ready, perform five first-wake trials and record phrase-end-to-Ask timings; target median is at most 2.0 seconds. Repeat registered-voice positive and mismatched-speaker trials. Confirm telemetry identifies any outlier phase without transcript content.

- [ ] **Step 7: Commit documentation and deployment notes**

```bash
git add apps/dimon_live/README.md docs/superpowers/specs/2026-10-12-ios-safe-live-wake-design.md
git commit -m "docs: record live wake operations"
```

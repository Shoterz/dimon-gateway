# Voice Trace Diagnostics Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a hideable in-app diagnostics panel that correlates red-D click, browser readiness, speech/Nemotron/fallback milestones, and reply playback without retaining audio or business content.

**Architecture:** A random trace ID links a red-D preparation run to utterance-scoped attempt IDs. Browser and server each record monotonic durations in their own timing domains; the server keeps a bounded in-memory ring of allowlisted events and exposes only the latest sanitized trace plus Task 1's worker residency snapshot. A compact `<details>` panel renders a timing waterfall and four worker cards, while a localStorage preference hides presentation without disabling measurement.

**Tech Stack:** Python 3.12, FastAPI/WebSocket, dataclasses/deque, vanilla ES modules, HTML/CSS, Node syntax checks, pytest/unittest.

**Spec:** `docs/superpowers/specs/2026-10-14-dimon-resident-voice-pipeline-design.md`

## Global Constraints

- Trace IDs and attempt IDs are UUIDv4 values and never authorization data.
- Store no audio, transcript, embedding, prompt, payee, payment, reply, or generated-content field.
- Cross-process durations are never computed by subtracting unrelated monotonic clocks; server receipt events bridge timing domains.
- The panel is optional and hideable for recording; hiding it does not disable tracing or readiness.
- Only the latest bounded trace and four-worker operational evidence are exposed; credentials, URLs, sockets, commands, and filesystem paths are forbidden.
- Existing user changes in the Dimon UI and voice files must be preserved.

## Review Focus

- Malicious client event names or IDs: reject rather than store/display them; Task 1 tests allowlists and UUID validation.
- Reconnect/fallback creates multiple attempts: keep unique attempt IDs linked to one trace without mixing milestones; Task 2 tests correlation.
- Browser reload with diagnostics hidden: measurement continues and local preference remains hidden; Task 4 tests this.
- Missing milestones and failures: render partial waterfalls with a bounded failure phase, never negative or fabricated durations; Tasks 1 and 4 test this.
- Trace endpoint serialization: recursively prove forbidden content and infrastructure strings cannot appear; Tasks 3 and 5 test this.

---

### Task 1: Privacy-safe trace model and ring buffer

**Files:**
- Create: `apps/dimon_live/voice_trace.py`
- Create: `apps/dimon_live/tests/test_voice_trace.py`

**Interfaces:**
- Produces: `VoiceTraceStore.start(trace_id)`, `record_browser(...)`, `record_server(...)`, `start_attempt(...)`, `finish_attempt(...)`, and `latest() -> dict[str, object]`.

- [ ] Write failing tests for UUIDv4 validation, event allowlists, non-negative local elapsed time, maximum 32 traces/64 events, attempt isolation, and rejection of content-like extra fields.
- [ ] Run `.venv/bin/python -m pytest apps/dimon_live/tests/test_voice_trace.py -q` and verify RED.
- [ ] Implement the bounded in-memory trace model using server monotonic time and sanitized enums only.
- [ ] Run the test and verify GREEN.
- [ ] Commit as `feat: add privacy-safe voice trace store`.

### Task 2: Trace propagation through hands-free and gateway

**Files:**
- Modify: `apps/dimon_live/static/hands_free.mjs`
- Modify: `apps/dimon_live/hands_free.py`
- Modify: `apps/dimon_live/remote_inference.py`
- Modify: `services/inference_gateway/transcription.py`
- Modify: `apps/dimon_live/tests/test_hands_free.py`
- Modify: `services/inference_gateway/tests/test_transcription_endpoint.py`

**Interfaces:**
- Consumes: Task 1 trace store.
- Produces: allowlisted milestones for socket ready, speech detected, Nemotron stream ready, first partial, wake match, speaker check, browser wake receipt, authoritative final, recognition path, and bounded failure phase.

- [ ] Write failing correlation tests covering normal Nemotron partial wake, CUDA Medium final, injected Nemotron failure, disconnect, and cancellation.
- [ ] Run the two named test files and verify RED.
- [ ] Add UUID trace/attempt fields to existing handshake/control envelopes and gateway correlation headers without placing them in authorization decisions.
- [ ] Run tests and verify GREEN.
- [ ] Commit as `feat: trace hands-free recognition phases`.

### Task 3: Sanitized diagnostics endpoint

**Files:**
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/tests/test_api.py`
- Create: `apps/dimon_live/tests/test_voice_diagnostics_api.py`

**Interfaces:**
- Consumes: latest trace from Task 1 and the sanitized four-worker preparation snapshot from the readiness plan.
- Produces: `GET /api/voice/diagnostics/latest` with `{trace, workers, gpu}` and no controls.

- [ ] Write failing endpoint tests for bounded schema, no-store response, empty state, partial trace, fallback reason, and recursive forbidden-field/string rejection.
- [ ] Run the diagnostics tests and verify RED.
- [ ] Implement the read-only endpoint and sanitization.
- [ ] Run tests and verify GREEN.
- [ ] Commit as `feat: expose sanitized voice diagnostics`.

### Task 4: Collapsible diagnostics panel and hide preference

**Files:**
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/app.css`
- Create: `apps/dimon_live/static/voice_diagnostics.mjs`
- Modify: `apps/dimon_live/static/app.js`
- Create: `apps/dimon_live/tests/test_voice_diagnostics_ui.py`
- Modify: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Consumes: Task 3 endpoint and browser milestone events from Task 2.
- Produces: compact latest-run waterfall, four worker cards, GPU reserve, recognition path/failure phase, and persistent `Hide diagnostics` control.

- [ ] Write failing DOM/source-contract tests for every required milestone/worker, mobile collapse, escaped text, hidden-by-preference reload, and measurement continuing while hidden.
- [ ] Run UI tests and `node --check` to verify RED.
- [ ] Implement the panel as a focused module and minimal integration hooks in the existing UI.
- [ ] Run tests and syntax checks to verify GREEN.
- [ ] Commit as `feat: add hideable voice diagnostics panel`.

### Task 5: End-to-end timing acceptance harness

**Files:**
- Create: `scripts/capture_voice_trace.py`
- Create: `apps/dimon_live/tests/test_voice_trace_capture.py`
- Modify: `docs/runbooks/faster-whisper-medium-cuda.md`

**Interfaces:**
- Consumes: Task 3 latest-trace endpoint.
- Produces: content-free JSON summaries and p50/p95 phase metrics for repeated manual “Hey Dimon” trials.

- [ ] Write failing tests for trace completeness scoring, per-domain non-negative durations, percentile calculation, fallback grouping, and refusal to serialize content fields.
- [ ] Run the harness tests and verify RED.
- [ ] Implement a read-only capture command that waits for new trace IDs and emits timings only.
- [ ] Run tests and verify GREEN.
- [ ] Commit as `test: measure the Hey Dimon pipeline`.

### Task 6: Live 20-trial acceptance

**Files:**
- Create: `docs/benchmarks/2026-10-15-hey-dimon-latency.json`

**Interfaces:**
- Consumes: Tasks 1–5 and twenty user-spoken wake trials performed five seconds after red-D click.
- Produces: content-free detection/partial/wake/speaker/browser/playback p50/p95 metrics and exact recognition-path counts.

- [ ] Run the focused Python and JavaScript suites plus privacy scans; all must pass.
- [ ] Restart Dimon services and prove diagnostics can be hidden/re-shown without changing measurement.
- [ ] Capture twenty trials, checking Nemotron active sessions return to zero after every completed/cancelled attempt.
- [ ] Save only IDs, timestamps/durations, bounded phases, path labels, and worker/GPU evidence.
- [ ] Commit as `test: record Hey Dimon latency acceptance`.

# Dimon One-Click Warm-Up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the header D warm Nemotron, Qwen, and Kokoro on demand, and restore Kokoro playback for all trusted clarification replies.

**Architecture:** Generalize the existing preparation coordinator around fixed async stage operations, host it in the inference gateway, and expose it through an authenticated gateway endpoint plus a same-origin application proxy. The browser renders only sanitized readiness state; trusted result mapping remains the sole source of clarification speech.

**Tech Stack:** Python 3.12, asyncio, FastAPI/httpx, vanilla JavaScript/CSS, pytest/unittest

**Spec:** `docs/superpowers/specs/2026-10-11-dimon-one-click-warmup-design.md`

## Global Constraints

- The browser must never receive the inference service key or arbitrary model controls.
- Warm-up uses fixed probes only and never writes banking data or retains generated audio.
- Concurrent starts are idempotent and status contains only safe fields/reason codes.
- Existing local preparation and single-service failure behavior remain supported.
- Clarification speech is derived only from trusted committed results through `result_summary()`.

## Review Focus

- A public client cannot select prompts/models through preparation: Task 2 endpoint contract test.
- Repeated D clicks cannot launch duplicate preparation: Task 3 client test and coordinator regression suite.
- Remote proxy never reflects the service key: Task 2 API test.
- Expired readiness removes the green state: Task 3 client test.
- Non-clarification arbitrary text remains rejected for remote synthesis: Task 1 mapping test.

---

### Task 1: Restore clarification speech

**Files:**
- Modify: `apps/dimon_live/remote_inference.py`
- Test: `apps/dimon_live/tests/test_remote_inference.py`
- Test: `apps/dimon_live/tests/test_speech.py`

**Interfaces:**
- Consumes: trusted committed result dictionaries and `result_summary(result)`
- Produces: `RemoteSynthesizer._payload()` mapping every `CLARIFICATION_REQUEST` to bounded `CONVERSATION_TURN_APPLIED` speech

- [ ] **Step 1: Write failing tests** for missing-amount clarification and rejection of arbitrary unsupported outcomes.
- [ ] **Step 2: Run focused tests**; expected FAIL because missing-amount clarification raises `INVALID_INFERENCE_REQUEST`.
- [ ] **Step 3: Generalize the clarification branch** while retaining the existing trusted summary and 1,600-character bound.
- [ ] **Step 4: Run focused speech and remote-inference tests**; expected PASS.

### Task 2: Gateway preparation and application proxy

**Files:**
- Modify: `apps/dimon_live/demo_preparation.py`
- Modify: `services/inference_gateway/app.py`
- Modify: `apps/dimon_live/remote_inference.py`
- Modify: `apps/dimon_live/app.py`
- Test: `apps/dimon_live/tests/test_demo_preparation.py`
- Test: `services/inference_gateway/tests/test_auth.py`
- Test: `apps/dimon_live/tests/test_demo_preparation_api.py`

**Interfaces:**
- Consumes: fixed gateway-owned Nemotron/Qwen/Kokoro providers and existing `DemoPreparationCoordinator.start/status/stop`
- Produces: authenticated `GET/POST /v1/prepare`, `RemoteGatewayClient.get_preparation/start_preparation`, and same-origin `GET/POST /api/demo/prepare`

- [ ] **Step 1: Write failing coordinator tests** for self-managed Nemotron lifecycle and existing idempotency.
- [ ] **Step 2: Run focused coordinator tests**; expected FAIL on the new self-managed lifecycle path.
- [ ] **Step 3: Refactor the coordinator minimally** to accept fixed async warm operations without changing sanitized snapshots.
- [ ] **Step 4: Write failing gateway endpoint tests** for authentication, empty fixed request body, sanitized status, and shared concurrent start.
- [ ] **Step 5: Add gateway composition and lifecycle shutdown** using the existing GPU status socket and fixed provider calls.
- [ ] **Step 6: Write failing remote-client/application proxy tests** proving server-held authentication and public same-origin access.
- [ ] **Step 7: Implement bounded client methods and proxy routes** without exposing credentials or arbitrary request fields.
- [ ] **Step 8: Run preparation, gateway, and application API tests**; expected PASS.

### Task 3: D-logo readiness interaction

**Files:**
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/app.css`
- Test: `apps/dimon_live/tests/test_ui_client.py`
- Test: `apps/dimon_live/tests/test_fixture_ui.py`

**Interfaces:**
- Consumes: same-origin `GET/POST /api/demo/prepare` snapshots
- Produces: accessible `#prepare-dimon-button`, retained Home link, visual `cold|preparing|ready|degraded` states, and compact live status

- [ ] **Step 1: Write failing static-contract tests** for button/link separation, live region, labels, and state CSS.
- [ ] **Step 2: Write failing JavaScript behavior tests** for polling, duplicate-click suppression, expiry/degraded transition, and safe failure copy.
- [ ] **Step 3: Implement the semantic header markup and compact responsive styles.**
- [ ] **Step 4: Implement preparation fetch/poll state management** with no layout shift and no credential handling.
- [ ] **Step 5: Run UI client and fixture UI tests**; expected PASS.

### Task 4: Integrated verification and deployment

**Files:**
- Modify: `docs/inference-gateway.md`
- Modify: `.env.host` only if a preparation setting is required

**Interfaces:**
- Consumes: Tasks 1–3
- Produces: documented, deployed one-click warm-up and audible clarification flow

- [ ] **Step 1: Document the fixed preparation endpoints, lifecycle window, and D-button states.**
- [ ] **Step 2: Run the complete relevant Python and frontend test suites plus `git diff --check`; expected PASS.**
- [ ] **Step 3: Restart gateway and Dimon services, then verify authenticated preparation reaches `READY`.**
- [ ] **Step 4: Verify the public D control and a missing-amount voice response produce a playable audio artifact.**
- [ ] **Step 5: Commit and push `develop` after all verification is green.**

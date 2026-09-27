# Dimon public live MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Expose the existing synthetic Dimon ledger through one session-isolated FastAPI gateway that can safely serve both the owner hostname and a temporary ngrok URL while sharing the existing Qwen, Nemotron, and Kokoro services.

**Architecture:** Keep the current `ActionService` and SQLite receipt contracts as the banking source of truth. Add a request-scoped session manager that chooses either the protected persistent owner database or an ephemeral per-visitor database, then route all existing endpoints through that session. Add a server-owned push-to-talk WebSocket that translates bounded browser PCM to Nemotron transcripts and submits the final transcript through the same text action endpoint; generate Kokoro audio only from committed server-owned result fields.

**Tech Stack:** Python 3, FastAPI, Starlette WebSockets, SQLite, httpx, existing Qwen/Nemotron/Kokoro HTTP/WebSocket services, browser AudioWorklet.

**Spec:** `docs/superpowers/specs/2026-09-25-dimon-public-mvp-design.md`

## Global Constraints

- Only the Dimon gateway is public; Qwen, Nemotron, Kokoro, SQLite paths, health, metrics, and debug details stay private.
- Visitor state is stored in `runtime/ledgers/visitors/<opaque-session-id>.sqlite3`; owner state is `runtime/ledgers/owner.sqlite3` and requires a trusted Cloudflare Access assertion.
- The browser cannot choose a database path, account ID, owner/visitor mode, amount, or receipt facts.
- Voice is push-to-talk, mono 16 kHz float32 PCM, bounded by configured byte and duration limits, with one global lease.
- Kokoro failure never rolls back a committed ledger result.
- No real banking, PayNow, KYC, biometrics, arbitrary shell, or second model stack is added.

## Review Focus

- An invalid or missing owner assertion must never open the owner database; test before any state access.
- Two visitor cookies must receive independent seeded files and balances; test cross-session mutation and reset.
- A second voice WebSocket must receive `VOICE_BUSY`, while disconnect/error releases the lease.
- Malformed/oversized voice frames must fail closed without calling the action service.
- Kokoro outages must leave a committed text result intact and return an audio-unavailable response.

---

### Task 1: Session-isolated ledger manager and ingress policy

**Files:**
- Create: `apps/dimon_live/session_manager.py`
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/.env.example`
- Test: `apps/dimon_live/tests/test_sessions.py`

**Interfaces:**
- Produces `SessionContext(session_id: str, role: Literal["owner", "visitor"], database: Database)` and `SessionManager.resolve(request)`, `SessionManager.reset(context)`, `SessionManager.cleanup()`.
- `create_app` accepts optional `session_manager`, `voice_bridge`, and `speech_service` dependencies for tests.

- [ ] Write failing tests for two visitor cookies, owner assertion rejection, ngrok visitor-only behavior, isolated reset, and invalid origin rejection.
- [ ] Run `python -m unittest apps.dimon_live.tests.test_sessions -v`; confirm the new session APIs fail.
- [ ] Implement opaque Secure/HttpOnly/SameSite cookie issuance, visitor DB seeding from the existing demo DB, owner assertion validation using a configured Access audience/email claim, origin allow-list checks, 60-minute inactivity expiry, active-session cap, and session-scoped reset.
- [ ] Route `/api/state`, `/api/actions`, receipt approvals, batch approvals, clarifications, and reset through the resolved session database.
- [ ] Run the focused tests, then the full existing `python -m unittest discover -s apps/dimon_live/tests -v` suite.

### Task 2: Public-safe FastAPI boundary and configuration

**Files:**
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/app.css`
- Test: `apps/dimon_live/tests/test_public_boundary.py`

**Interfaces:**
- Produces `GET /api/session`, `GET /api/state`, and existing session-scoped action routes with bounded JSON responses.
- Public requests do not expose `/api/health`; local health remains available only when `DIMON_LOCAL_HEALTH=1` and the request is loopback.

- [ ] Write failing tests for configured-origin success, unconfigured-origin rejection, bounded request sizes, session cookie issuance, and absence of public health/debug routes.
- [ ] Run the focused tests and observe failures.
- [ ] Implement middleware for Origin/Host policy, a session bootstrap endpoint, strict public response shaping, environment-driven paths/limits, and explicit `OPTIONS` handling.
- [ ] Add a visible visitor/owner session badge and typed fallback copy without exposing paths or model endpoints.
- [ ] Run focused and full tests.

### Task 3: Nemotron voice bridge and global lease

**Files:**
- Create: `apps/dimon_live/voice.py`
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/app.css`
- Test: `apps/dimon_live/tests/test_voice.py`

**Interfaces:**
- Produces `VoiceLease`, `NemotronClient`, and `VoiceBridge.handle(websocket, session_context)`.
- WebSocket route: `/api/voice/session`; events are JSON `ready`, `transcript_partial`, `transcript_final`, `error`, `closed`.

- [ ] Write failing tests for one accepted socket/one `VOICE_BUSY`, malformed frame, byte/duration limits, disconnect lease release, and final transcript dispatch through the action service.
- [ ] Run focused tests and observe failures.
- [ ] Implement an asyncio global lease, bounded binary frame validation for mono 16 kHz float32, Nemotron protocol translation to `audio_append`/commit and transcript events, timeout/cancellation cleanup, and typed fallback errors.
- [ ] Add push-to-talk UI using an AudioWorklet that sends bounded PCM chunks and submits the final transcript through the existing action form; never connect the browser directly to Nemotron.
- [ ] Run focused and full tests.

### Task 4: Kokoro result audio

**Files:**
- Create: `apps/dimon_live/speech.py`
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/app.js`
- Test: `apps/dimon_live/tests/test_speech.py`

**Interfaces:**
- Produces `SpeechService.speak_result(result, session_context) -> SpeechArtifact | None` and `GET /api/audio/{artifact_id}`.

- [ ] Write failing tests for server-owned summary construction, bounded audio storage, opaque artifact retrieval, and Kokoro failure preserving a committed result.
- [ ] Run focused tests and observe failures.
- [ ] Implement private Kokoro HTTP invocation, WAV size/type validation, per-session ephemeral audio storage, expiry cleanup, and opaque artifact authorization.
- [ ] Add a play-audio control shown only when the gateway returns an artifact ID.
- [ ] Run focused and full tests.

### Task 5: Launcher, deployment docs, and rehearsal checks

**Files:**
- Create: `apps/dimon_live/run_public_mvp.sh`
- Modify: `apps/dimon_live/README.md`
- Modify: `apps/dimon_live/.env.example`
- Create: `docs/public-mvp-runbook.md`
- Test: `apps/dimon_live/tests/test_runbook_contract.py`

**Interfaces:**
- Launcher starts only `uvicorn apps.dimon_live.app:app --host 127.0.0.1 --port 8890` and prints the two expected ingress URLs without starting model processes.

- [ ] Write failing tests for launcher safety and required environment defaults.
- [ ] Run focused tests and observe failures.
- [ ] Implement a strict launcher with `exec`, loopback binding, required dependency checks, and no public model-port forwarding.
- [ ] Document owner Cloudflare Access, ngrok forwarding to `127.0.0.1:8890`, per-visitor isolation, busy voice behavior, and cleanup.
- [ ] Run the full test suite and a localhost HTTP rehearsal covering owner/visitor isolation, receipt approval, reset, and voice lease behavior.

### Verification contract

Before reporting completion, run:

```bash
python -m unittest discover -s apps/dimon_live/tests -v
python -m compileall -q apps/dimon_live
```

Then manually verify the app on `http://127.0.0.1:8890`, confirming one owner route, one visitor route, one blocked/ambiguous request, one receipt approval, one isolated reset, and one voice-busy case. Record any unavailable local inference service as a non-mutating dependency warning rather than claiming an end-to-end voice success.

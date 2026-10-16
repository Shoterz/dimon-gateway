# Dimon Dual Streaming STT Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show live Nemotron captions while using only Faster-Whisper's final transcript for Dimon's financial intent.

**Architecture:** Compose an authoritative Faster-Whisper stream with a best-effort Nemotron caption stream behind the existing recognizer protocol. Add a `dual` configuration mode whose health contract follows the authoritative engine.

**Tech Stack:** Python 3, asyncio, FastAPI/WebSocket, unittest

**Spec:** `docs/superpowers/specs/2026-10-11-dimon-dual-streaming-stt-design.md`

## Global Constraints

- Each PCM16 chunk is offered to both recognizers during the same utterance.
- Only Faster-Whisper final text may become `transcript.final`.
- Nemotron failure must not fail or delay authoritative completion.
- Both streams close on success, failure, and disconnect.
- Existing single-engine modes and Kokoro behavior remain unchanged.

## Review Focus

- Caption open failure still yields the authoritative final: Task 1 test.
- Caption push failure still yields the authoritative final: Task 1 test.
- Caption final never replaces Whisper final: Task 1 test.
- Authoritative failure remains fatal: Task 1 test.
- Dual configuration requires Whisper URL/revision and exposes Whisper readiness: Task 2 test.

---

### Task 1: Dual recognizer composition

**Files:**
- Modify: `services/inference_gateway/transcription.py`
- Test: `services/inference_gateway/tests/test_transcription_endpoint.py`

**Interfaces:**
- Consumes: existing `TranscriptionRecognizer.open_stream(on_partial)` protocol
- Produces: `DualTranscriptionRecognizer(authoritative, captions)` implementing that protocol

- [ ] **Step 1: Write failing tests** for caption partials plus authoritative final, caption open/push/finish failure, authoritative failure, and cleanup.
- [ ] **Step 2: Run the focused tests**; expected failure is missing `DualTranscriptionRecognizer`.
- [ ] **Step 3: Implement the dual recognizer and stream** with isolated best-effort caption handling and authoritative final ownership.
- [ ] **Step 4: Run transcription endpoint tests**; expected PASS.

### Task 2: Configuration and application composition

**Files:**
- Modify: `services/inference_gateway/config.py`
- Modify: `services/inference_gateway/app.py`
- Test: `services/inference_gateway/tests/test_auth.py`

**Interfaces:**
- Consumes: `DualTranscriptionRecognizer(authoritative, captions)` from Task 1
- Produces: `DIMON_GATEWAY_STT_BACKEND=dual` composition and Whisper-owned readiness/revision

- [ ] **Step 1: Write a failing configuration/composition test** asserting dual composition and authoritative revision/URL.
- [ ] **Step 2: Run the focused test**; expected FAIL because `dual` is rejected.
- [ ] **Step 3: Add dual validation and app construction** while preserving both single-engine modes.
- [ ] **Step 4: Run inference-gateway tests**; expected PASS.

### Task 3: Host selection and documentation

**Files:**
- Modify: `.env.host`
- Modify: `docs/inference-gateway.md`

**Interfaces:**
- Consumes: `dual` configuration from Task 2
- Produces: deployed host selection and operator documentation

- [ ] **Step 1: Set the host backend to `dual` and document partial/final ownership and fallback behavior.**
- [ ] **Step 2: Run the full relevant test suite and syntax checks**; expected PASS.
- [ ] **Step 3: Commit, restart the gateway, and verify local/public health and a voice session.**

# Bounded Inference Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose authenticated, bounded Qwen, Nemotron, and Kokoro capabilities through one project-owned API while keeping raw model ports private and business authority inside Dimon.

**Architecture:** Run a FastAPI inference gateway on `127.0.0.1:8891`. The gateway trusts identity headers only from the loopback Cloudflare Tunnel origin, validates every request, enforces per-capability concurrency/rate/timeout limits, and calls raw inference services over loopback. Dimon's remote provider speaks this project protocol and still runs grounding, policy, approvals, receipts, and SQLite locally.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, httpx, websockets, asyncio, unittest.

**Spec:** [Team repository and inference architecture design](../specs/2026-09-27-team-repository-and-inference-architecture-design.md)

**Platform references:** [Cloudflare Access JWT validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/) and [service-token authentication](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/).

## Global Constraints

- This plan depends on repository foundation and provider/fixture contracts.
- Bind the gateway to loopback. Cloudflare Access is the public authentication layer; the app accepts its assertion only from a configured trusted proxy address.
- Never expose arbitrary model names, prompts, sampling settings, file paths, raw Kokoro text, shell commands, or raw upstream proxy routes.
- The gateway has no database access and no power to approve or execute a transfer.
- Qwen allows one active request and four queued requests; Nemotron allows one active session and no queue; Kokoro allows one active request and four queued requests.
- Preserve stable machine-readable error codes and avoid returning upstream exception strings.

## Review Focus

- Identity must be established before rate limiting, idempotency, or upstream work.
- Queue cancellation and timeouts must release all permits.
- Reusing a request ID with a different payload must return `409 IDEMPOTENCY_CONFLICT`.
- TTS input must be constructed from bounded committed-result fields, never arbitrary client text.
- A remote-mode app must pass the same provider contract tests as fixture and local fakes.

---

### Task 1: Scaffold the loopback-only gateway and identity boundary

**Files:**
- Create: `services/__init__.py`
- Create: `services/inference_gateway/__init__.py`
- Create: `services/inference_gateway/config.py`
- Create: `services/inference_gateway/auth.py`
- Create: `services/inference_gateway/errors.py`
- Create: `services/inference_gateway/app.py`
- Create: `services/inference_gateway/cli.py`
- Create: `services/inference_gateway/tests/__init__.py`
- Create: `services/inference_gateway/tests/test_auth.py`
- Modify: `pyproject.toml` (add `PyJWT[crypto]` for verified Access JWTs)
- Modify: `.env.example`

**Interfaces:**
- Console command: `dimon-inference-gateway`.
- `GatewayConfig.from_env(env)` requires loopback bind, upstream loopback URLs, accepted Cloudflare Access audience, and trusted proxy addresses.
- `AccessIdentity(subject, credential_type, email)` is derived from a verified Cloudflare Access assertion delivered by the trusted tunnel path; user identities use `sub`, while service identities use `common_name` because service-token assertions may have an empty `sub`.
- JSON error envelope: `{"error":{"code":str,"message":str,"request_id":str|null}}`.

- [ ] Write failing tests for missing assertions, malformed assertions, wrong audience, expired claims, non-loopback clients, untrusted forwarded headers, and accepted user/service identities.
- [ ] Run `uv run python -m unittest services.inference_gateway.tests.test_auth -v`; confirm the package is absent.
- [ ] Add strict settings validation and a gateway factory with no model routes yet.
- [ ] Validate Access JWT signature, issuer, audience, and expiry using Cloudflare's configured JWKS endpoint with a bounded cache; do not merely decode unverified claims.
- [ ] Require the immediate peer to be in the trusted-proxy allow-list before honoring identity headers.
- [ ] Add the loopback CLI, stable error mapper, and safe environment examples.
- [ ] Run the focused tests and repository-boundary checker.
- [ ] Commit the gateway shell as `feat: add authenticated inference gateway`.

### Task 2: Implement shared capacity, rate-limit, timeout, and idempotency primitives

**Files:**
- Create: `services/inference_gateway/limits.py`
- Create: `services/inference_gateway/idempotency.py`
- Create: `services/inference_gateway/tests/test_limits.py`
- Create: `services/inference_gateway/tests/test_idempotency.py`

**Interfaces:**
- `BoundedExecutor(active_limit, queue_limit, timeout_seconds).run(awaitable_factory)` rejects excess work with `CAPACITY_EXCEEDED` and releases permits on completion, timeout, or cancellation.
- `SlidingWindowLimiter(limit, window_seconds).check(identity_key)` returns a retry delay or records an accepted attempt.
- `IdempotencyStore(ttl_seconds=600, max_entries=512).begin(identity, request_id, fingerprint)` returns new, replay, or conflict state and stores only successful bounded responses.

- [ ] Write failing async tests for active/queued limits, FIFO admission, queue rejection, task cancellation, upstream timeout, per-identity isolation, rate-window expiry, replay, conflict, and bounded eviction.
- [ ] Run both focused modules and observe failures.
- [ ] Implement the primitives with monotonic time and injected clocks for deterministic tests.
- [ ] Ensure permits are released in `finally` blocks and disconnected requests cancel pending upstream work.
- [ ] Run focused tests repeatedly to detect ordering or cleanup flakes.
- [ ] Commit the shared controls as `feat: bound inference gateway capacity`.

### Task 3: Add the structured-intent endpoint

**Files:**
- Create: `services/inference_gateway/contracts.py`
- Create: `services/inference_gateway/upstreams.py`
- Modify: `services/inference_gateway/app.py`
- Create: `services/inference_gateway/tests/test_intent_endpoint.py`

**Interfaces:**
- `POST /v1/intent` accepts `IntentRequest(request_id: UUID, text: str[1..8192 bytes], contract_version: Literal[4])`.
- Version `4` means the current transfer-contract family; single actions may return the existing version-3 single-action schema while batches return version 4.
- The gateway fixes model name, prompt, schema, temperature, and output bounds from host configuration.
- Success returns `request_id`, validated `intent`, bounded `raw_output`, `model`, `revision`, and `latency_ms`.
- Limits: one active, four queued, 120-second total timeout, 12 accepted starts per identity per minute, 10-minute idempotency.

- [ ] Write failing endpoint tests for auth-before-body handling, UTF-8 byte limits, unsupported contract version, successful single/batch intent, malformed upstream JSON, upstream outage, timeout, rate limit, capacity, replay, and idempotency conflict.
- [ ] Run the focused test and observe failures.
- [ ] Wrap the existing Qwen extractor behind an upstream adapter whose model/prompt/settings come only from host configuration.
- [ ] Validate the response using the existing action-intent validator and truncate stored raw output to the configured bound.
- [ ] Add stable HTTP mappings: `400 INVALID_REQUEST`, `401 UNAUTHORIZED`, `409 IDEMPOTENCY_CONFLICT`, `429 RATE_LIMITED`, `429 CAPACITY_EXCEEDED`, `503 UPSTREAM_UNAVAILABLE`, and `504 UPSTREAM_TIMEOUT`.
- [ ] Run the endpoint, model-adapter, adversarial, and idempotency tests.
- [ ] Commit the endpoint as `feat: add bounded intent inference`.

### Task 4: Add bounded transcription streaming

**Files:**
- Create: `services/inference_gateway/transcription.py`
- Modify: `services/inference_gateway/app.py`
- Create: `services/inference_gateway/tests/test_transcription_endpoint.py`

**Interfaces:**
- WebSocket: `/v1/transcription`.
- Server events: `session.ready`, `transcript.partial`, `transcript.final`, and `error`; every event includes `protocol_version: 1`.
- Client sends binary PCM16 mono 16 kHz frames and one JSON `{"type":"input.commit"}` event.
- Limits: 4 MiB and 30 seconds per session, one active session, no queue, six accepted starts per identity per minute.

- [ ] Write failing tests for authenticated upgrade, rejected missing identity, second-session `VOICE_BUSY`, binary-before-ready, invalid JSON, wrong event type, oversized audio, excessive duration, partial/final ordering, upstream disconnect, client disconnect, and lease release.
- [ ] Run the focused test and observe failures.
- [ ] Authenticate before accepting the socket and acquire the no-queue lease before emitting `session.ready`.
- [ ] Translate the project protocol to the current Nemotron client internally; never expose the raw Nemotron protocol to the browser or teammate app.
- [ ] Track decoded PCM duration, bound event sizes, cancel upstream tasks on disconnect, and release the lease in all paths.
- [ ] Run the focused test repeatedly plus the existing voice suite.
- [ ] Commit the stream protocol as `feat: add bounded transcription gateway`.

### Task 5: Add result-bound speech synthesis

**Files:**
- Create: `services/inference_gateway/speech.py`
- Modify: `services/inference_gateway/contracts.py`
- Modify: `services/inference_gateway/app.py`
- Modify: `apps/dimon_live/speech.py`
- Create: `services/inference_gateway/tests/test_speech_endpoint.py`

**Interfaces:**
- `POST /v1/speech` accepts `SpeechRequest(request_id, status, action_kind, amount, currency, recipient_display, reference)` with strict lengths and enumerated status/action values.
- `build_result_summary(request) -> str` constructs the only text sent to Kokoro.
- Success returns `audio/wav`, `X-Dimon-Request-Id`, model revision, and bounded latency headers.
- Limits: one active, four queued, 30-second timeout, 20 accepted starts per identity per minute, WAV response at most 8 MiB.

- [ ] Write failing tests that reject arbitrary `text`, extra fields, negative/oversized amounts, invalid status/action, header injection, oversized WAV, wrong media type, outage, timeout, capacity, and rate limit.
- [ ] Run the focused test and observe failures.
- [ ] Refactor the existing server-owned committed-result summary into a public pure function shared by local and gateway speech paths.
- [ ] Implement the Kokoro adapter and WAV validation without persisting gateway-side artifacts.
- [ ] Run focused tests and the existing speech/API tests.
- [ ] Commit the endpoint as `feat: add result-bound speech gateway`.

### Task 6: Implement the Dimon remote provider

**Files:**
- Create: `apps/dimon_live/remote_inference.py`
- Modify: `apps/dimon_live/provider_factory.py`
- Modify: `.env.example`
- Create: `apps/dimon_live/tests/test_remote_inference.py`
- Modify: `apps/dimon_live/tests/test_provider_contract.py`

**Interfaces:**
- `RemoteExtractor`, `RemoteRecognizer`, and `RemoteSynthesizer` implement the existing provider protocols.
- Configuration requires `DIMON_INFERENCE_GATEWAY_URL`, `DIMON_CF_ACCESS_CLIENT_ID`, and `DIMON_CF_ACCESS_CLIENT_SECRET` in remote mode.
- HTTP and WebSocket requests send Cloudflare Access service-token headers, UUID request IDs, and the bounded project contracts.

- [ ] Write failing tests for configuration, request shapes, identity headers, response parsing, streaming order, cancellation, every stable gateway error code, and missing/invalid response fields.
- [ ] Run the focused test and observe failures.
- [ ] Implement HTTP clients with one shared connection pool and a WebSocket client with bounded frame/event parsing.
- [ ] Map gateway errors to existing `InferenceFailure` codes and preserve `VOICE_BUSY` behavior.
- [ ] Add the remote provider to the factory and provider contract suite.
- [ ] Run the full provider contract and application suite with mocked transport only.
- [ ] Commit remote mode as `feat: connect Dimon to remote inference`.

### Task 7: Add health, readiness, correlation, and safe metrics

**Files:**
- Create: `services/inference_gateway/observability.py`
- Modify: `services/inference_gateway/app.py`
- Create: `services/inference_gateway/tests/test_observability.py`
- Create: `docs/inference-gateway.md`

**Interfaces:**
- `GET /health/live` reports process liveness after authentication.
- `GET /health/ready` reports only named capability readiness and configured revisions, with no filesystem paths or exception details.
- Every request uses or creates a UUID correlation ID and emits structured logs with identity hash, route, outcome code, queue delay, and latency; no transcript, prompt, intent, token, or audio content is logged.

- [ ] Write failing tests for authenticated health, per-upstream readiness, safe fields, correlation propagation, and log redaction.
- [ ] Implement bounded readiness probes with independent timeouts and no public raw-port details.
- [ ] Add structured logs and in-memory counters for request counts, failure codes, active work, queue depth, and latency buckets.
- [ ] Document the public contract, limits, error codes, and curl/WebSocket examples using placeholders.
- [ ] Run all gateway and application tests, then commit the checkpoint after the repository audit passes.

### Verification Contract

Before reporting this checkpoint complete, run:

```bash
uv run python -m unittest discover -s services/inference_gateway/tests -p 'test_*.py'
uv run python -m unittest apps.dimon_live.tests.test_provider_contract -v
uv run python -m unittest apps.dimon_live.tests.test_remote_inference -v
uv run python -m unittest discover -s apps/dimon_live/tests -p 'test_*.py'
uv run python -m compileall -q apps services tests
scripts/check-repository-boundary
git diff --check
```

Run the gateway on `127.0.0.1:8891` with fake upstream adapters. Verify unauthorized HTTP and WebSocket requests fail, authenticated intent/speech/transcription flows pass, a sixth concurrent intent is rejected, a second voice session receives `VOICE_BUSY`, and no response or log contains a secret, prompt, transcript, or host path.

# Shared Inference Gateway Service Key Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let branch collaborators use the owner's hosted Qwen, Nemotron, and Kokoro services through one endpoint and one shared key.

**Architecture:** Reuse the existing loopback inference gateway on port `8891`, replacing Cloudflare Access JWT checks with one Bearer key for HTTP and WebSocket routes. Configure remote-mode app clients with that same key and the shared endpoint; keep raw model upstreams loopback-only.

**Tech Stack:** Python 3.12, FastAPI, httpx, websockets, dotenv, pytest.

**Spec:** `docs/superpowers/specs/2026-10-07-shared-inference-service-key-design.md`

## Global Constraints

- One HTTP/WebSocket origin, `https://tencentservices.stocksuite.app`, carries all Qwen, Nemotron, and Kokoro inference traffic.
- The gateway runs on the GPU host and Cloudflare Tunnel forwards the hostname to `http://127.0.0.1:8891`.
- Qwen, Nemotron, and Kokoro remain private host services; the gateway relays only the project's bounded inference contracts to their configured upstreams.
- Remove Cloudflare Access identity verification and all Cloudflare Access service-token headers from gateway and project inference clients.
- Use one shared service key for HTTP and WebSocket authentication.
- The owner will supply the key value later; missing/blank keys fail startup in gateway and remote mode.
- The key is intentionally committed in the tracked teammate runtime environment file; anyone with branch access can copy it and use the endpoint.
- Never put the key in browser JavaScript or responses, log it, or forward it to model upstreams.
- Preserve existing payload validation, request limits, idempotency, error contracts, and GPU-host supervision.

## Review Focus

- WebSocket authentication must reject absent and wrong Bearer keys before starting Nemotron; cover valid, absent, and wrong keys in `services/inference_gateway/tests/test_transcription_endpoint.py`.
- Host and client must use the same exact key header without retaining Access headers; cover header construction and absence of Access headers in `apps/dimon_live/tests/test_remote_inference.py`.
- The tracked app `.env` exception must not unignore other `.env` files; cover the intended path and unrelated secrets in `tests/test_clean_clone.py` or a focused config test.
- Gateway and remote startup must reject a blank service key while fixture/local app modes remain unaffected; cover in gateway auth/config and provider configuration tests.
- The published origin must remain `http://127.0.0.1:8891`, with model upstreams loopback-only; cover config and documentation assertions.

---

### Task 1: Replace Access identity with unified service-key auth

**Files:**
- Modify: `services/inference_gateway/config.py`
- Modify: `services/inference_gateway/auth.py`
- Modify: `services/inference_gateway/app.py`
- Modify: `services/inference_gateway/contracts.py`
- Modify: `services/inference_gateway/upstreams.py`
- Modify: `services/inference_gateway/transcription.py`
- Modify: `services/inference_gateway/tests/test_auth.py`
- Modify: `services/inference_gateway/tests/test_transcription_endpoint.py`

**Interfaces:**
- Produces: `GatewayConfig.service_key: str` and `require_service_key(request) -> str`; `TranscriptionGateway` authenticates `Authorization: Bearer <key>` on the WebSocket handshake; `POST /v1/qwen` accepts only fixed transfer, stock, bill-payment, and investment-follow-up tasks and builds prompts server-side.
- Removes: Access verifier/JWKS configuration, trusted-proxy identity requirements, and identity-based limiter keys. Retain existing route contracts and use one shared-key identity bucket for rate limiting.

- [ ] Add tests for gateway startup requiring `DIMON_INFERENCE_SERVICE_KEY`, exact Bearer acceptance, missing/wrong-key HTTP rejection, and WebSocket valid/missing/wrong-key behavior.
- [ ] Run focused gateway auth tests and confirm they fail against the Access-based implementation.
- [ ] Implement constant-time key comparison and use the shared key for HTTP and WebSocket authentication; remove Access JWT/JWKS and proxy verification code and settings.
- [ ] Run focused gateway auth and transcription endpoint tests; confirm all pass.

### Task 2: Send one key from all remote inference clients

**Files:**
- Modify: `apps/dimon_live/remote_inference.py`
- Modify: `apps/dimon_live/model_adapter.py`
- Modify: `apps/dimon_live/provider_factory.py`
- Modify: `apps/dimon_live/tests/test_remote_inference.py`
- Modify: `apps/dimon_live/tests/test_provider_contract.py`

**Interfaces:**
- Consumes: `DIMON_INFERENCE_SERVICE_KEY` from remote-mode configuration.
- Produces: `RemoteGatewayConfig.service_key: str`; `RemoteGatewayClient` sends `Authorization: Bearer <key>` on intent, fixed Qwen task, speech, and transcription requests. Remote provider factory wires stock, bill-payment, and investment-follow-up Qwen tasks to `/v1/qwen`.

- [ ] Add tests that remote config requires a nonblank key and HTTP/WebSocket requests send the Bearer key with no Cloudflare Access headers.
- [ ] Run focused remote inference tests and confirm they fail against the current service-token client.
- [ ] Implement key loading and shared HTTP/WebSocket header construction; remove all Access service-token configuration and header code.
- [ ] Run focused remote inference and provider contract tests; confirm they pass.

### Task 3: Make the teammate clone use the hosted gateway

**Files:**
- Create: `apps/dimon_live/.env` (tracked teammate runtime config with owner-supplied key placeholder)
- Modify: `.gitignore`
- Modify: `.env.example`
- Modify: `apps/dimon_live/.env.example`
- Modify: `apps/dimon_live/provider_factory.py` only if remote startup validation needs adjustment
- Modify: `tests/test_clean_clone.py`

**Interfaces:**
- Produces: tracked teammate defaults `DIMON_INFERENCE_MODE=remote`, `DIMON_INFERENCE_GATEWAY_URL=https://tencentservices.stocksuite.app`, and `DIMON_INFERENCE_SERVICE_KEY=` for the owner to fill before publishing the usable branch.
- The exact `apps/dimon_live/.env` path is unignored; all other `.env` and host environment files remain ignored.

- [ ] Add a clean-clone/config test proving the teammate file is tracked by the ignore rules, uses remote mode and the hosted URL, and contains no Access service-token variables.
- [ ] Confirm the test fails before the ignore/config changes.
- [ ] Add only the narrow `.gitignore` exception and update canonical/app templates and teammate config to remove Access settings and use the shared-key variable.
- [ ] Run the focused clean-clone and provider configuration tests; confirm they pass.

### Task 4: Document the tunnel and host setup

**Files:**
- Modify: `docs/inference-gateway.md`
- Modify: `docs/gpu-host-setup.md`

**Interfaces:**
- Documents exact Cloudflare Tunnel origin `http://127.0.0.1:8891`, hostname `tencentservices.stocksuite.app`, host-only key configuration, tracked teammate config, and the shared-key visibility/revocation behavior.

- [ ] Update docs to remove Access app, audience, issuer, and service-token setup; explain that the committed key is copyable by branch readers and that retiring the hostname removes the public route.
- [ ] Check docs and environment templates contain no stale Access instructions or old `dimon-inference.stocksuite.app` teammate URL.

### Task 5: End-to-end contract check

**Files:**
- Modify only the relevant tests from Tasks 1–3 if integration gaps appear.

**Interfaces:**
- Uses the service-key gateway and remote-client contracts defined in Tasks 1 and 2.

- [ ] Run the existing gateway, remote inference, provider contract, and clean-clone test modules together.
- [ ] Confirm no Access headers, JWT requirements, or non-loopback model upstreams remain in the shared inference path.
- [ ] Confirm gateway port remains `8891` and report that as the Cloudflare Tunnel destination.

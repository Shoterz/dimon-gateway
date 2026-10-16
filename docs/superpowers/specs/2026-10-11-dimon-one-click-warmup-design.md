# Dimon One-Click Warm-Up and Clarification Speech Design

## Purpose

Make Dimon demonstrably ready before a live interaction. Clicking the red D in the public header warms the speech-recognition, reasoning, and speech-output services together, while preserving the gateway as the only public model boundary. Separately, every safe server-generated clarification shown as Dimon's reply must be eligible for Kokoro playback.

## Existing behavior and root cause

Nemotron, Qwen, and Kokoro are managed on demand and can all be cold before the first interaction. The application already contains a local-only demo preparation coordinator, but production uses remote inference, so its `/api/demo/prepare` route is not enabled and cannot warm the gateway host.

The missing spoken reply is independent of cold start. `RemoteSynthesizer._payload()` accepts only three recipient-clarification reason codes. A missing-amount clarification such as “How much would you like to send?” has a different reason code and is rejected locally with `INVALID_INFERENCE_REQUEST`; the reserved audio artifact then disappears and the browser receives 404.

## Architecture

### Gateway warm-up boundary

Add authenticated `POST /v1/prepare` and `GET /v1/prepare` endpoints to the inference gateway. They expose only a fixed warm-up operation and bounded status; callers cannot choose models, prompts, or synthesized text.

The preparation coordinator performs these fixed probes:

1. Nemotron: acquire its existing lifecycle lease, open a stream, close it, and release the lease. The supervisor's idle-retention policy keeps the loaded worker warm.
2. Qwen: execute the existing fixed, schema-bound intent probe.
3. Kokoro: synthesize the fixed phrase `Dimon is ready.` and validate the returned WAV without publishing it.

Qwen and Kokoro may warm concurrently after the initial capacity check. Preparation is idempotent: concurrent clicks share one in-progress operation. Status reports only `COLD`, `PREPARING`, `READY`, or `DEGRADED`, per-stage state, safe reason codes, timestamps, and the existing warning that another GPU request can evict a warm service. No endpoint, process details, prompts, or model output are returned.

The gateway must use the existing GPU-overlord status socket and lifecycle clients. The operation is best-effort and never executes financial intent, writes banking data, or stores generated audio.

### Public application proxy

Enable same-origin `GET/POST /api/demo/prepare` in remote mode by proxying to the gateway through `RemoteGatewayClient` with the server-held service key. The browser never receives that key and cannot call arbitrary gateway operations through this proxy. Existing local-mode preparation behavior remains supported.

### Header interaction

Split the current brand anchor into:

- A red D button that starts preparation.
- A “Dimon” text link that retains navigation to Home.

The D button has explicit accessible and visual states:

- Cold: normal red D; label `Warm Dimon services`.
- Preparing: amber/pulsing treatment; disabled against duplicate submission; label `Dimon is warming up`.
- Ready: red D with a small green readiness dot; label `Dimon services ready`.
- Degraded: red D with a small amber warning dot; label `Some Dimon services are unavailable`.

A compact live-status message announces `Warming Nemotron, Qwen, and Kokoro…`, `Dimon is ready`, or a safe failed-stage summary. It must not shift the header layout materially on mobile. The client polls while preparation is active and refreshes status periodically so stale readiness is removed.

### Clarification speech correction

For any committed result with `outcome == "CLARIFICATION_REQUEST"`, derive speech from the existing trusted `result_summary()` function and send it as gateway status `CONVERSATION_TURN_APPLIED`. Do not accept browser-provided arbitrary speech text. Existing specialized result mappings remain unchanged.

This makes missing-amount, missing-account, recipient-disambiguation, and other server-authored clarification replies audible through Kokoro.

## Failure behavior

- A failed warm-up reports the failed stage and leaves ordinary cold-start behavior available; it never blocks normal app use.
- Repeated clicks are idempotent and do not start duplicate model loads.
- If readiness expires or a required managed worker disappears, the UI returns to degraded/cold rather than claiming readiness.
- Kokoro generation failure leaves the text reply visible and reports the existing voice-reply failure state.
- Authentication failures and malformed preparation requests return the gateway's existing safe error envelope.

## Verification

- Unit tests cover generalized clarification mapping and prove arbitrary non-clarification text is still rejected.
- Gateway tests cover authentication, fixed warm-up behavior, idempotency, sanitized status, and partial-stage failure.
- Application API tests cover remote proxying without exposing the service key.
- Browser/UI tests cover D-button state transitions, duplicate-click suppression, Home-link preservation, and accessible labels.
- Deployment verification confirms all three services ready, the public UI reaches ready state, and a missing-amount clarification produces and autoplays a Kokoro artifact.

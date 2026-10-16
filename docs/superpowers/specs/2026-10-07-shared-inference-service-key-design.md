# Shared Inference Gateway Service Key

## Goal

Let a teammate who clones the `backend-edits` branch run the project against the owner's hosted Qwen, Nemotron, and Kokoro services through one public gateway, without installing the models or requesting a credential separately.

## Approved scope and constraints

- One HTTP/WebSocket origin, `https://tencentservices.stocksuite.app`, carries all Qwen, Nemotron, and Kokoro inference traffic.
- The gateway runs on the GPU host and Cloudflare Tunnel forwards the hostname to `http://127.0.0.1:8891`.
- Qwen, Nemotron, and Kokoro remain private host services; the gateway relays only the project's bounded inference contracts to their configured upstreams.
- Remove Cloudflare Access identity verification and all Cloudflare Access service-token headers from gateway and project inference clients.
- Use one shared service key for HTTP and WebSocket authentication. Store the same key in the host gateway environment and in the branch's tracked teammate environment file so a clone receives it without a separate request.
- The owner will supply the key value later. The implementation must make the setting explicit and must reject missing/blank keys at gateway startup and remote-client startup.
- The owner knowingly wants the key committed with the branch. Treat it as a shared repository credential: every person with branch access can copy it and use the endpoint. Retiring the public hostname is the owner's requested access-revocation mechanism.
- Do not put the key in browser JavaScript or responses, log it, or forward it to model upstreams.
- Preserve existing inference payload validation, request limits, idempotency, error contracts, and GPU-host supervision.

## Current architecture and evidence

- `services/inference_gateway` already exposes bounded intent (`POST /v1/intent`), transcription (`WS /v1/transcription`), speech (`POST /v1/speech`), and health routes.
- It listens on loopback port `8891`; `docs/gpu-host-setup.md` and `services/gpu_host/host_cli.py` use the same port. Qwen, Nemotron, and Kokoro run on loopback ports `8080`, `8004`, and `3001` respectively.
- The gateway currently verifies Cloudflare Access JWTs and the remote client currently sends Access service-token headers. The gateway configuration requires Access audience, issuer, and trusted-proxy settings.
- The remote inference provider already multiplexes its HTTP speech/intent and WebSocket transcription through one HTTPS origin. However, the clean-clone example currently selects fixture mode and has blank Access credentials.
- The root `.gitignore` excludes `.env` files except examples, so the intended teammate runtime environment must receive an explicit tracked exception rather than broadly unignoring all local credential files.
- Existing uncommitted changes are present in application and gateway source files; implementation must preserve and integrate them rather than reset or replace them.

## Design

### 1. Unified service-key authentication

Add a single required `DIMON_INFERENCE_SERVICE_KEY` setting to gateway configuration and remote-client configuration. HTTP requests send it in `Authorization: Bearer <key>`. WebSocket requests send the same header during the handshake. Compare credentials with a constant-time comparison, return the existing stable unauthorized error for missing or invalid keys, and never include credential material in logs or error responses.

Remove Cloudflare Access JWT/JWKS verification, proxy identity requirements, Access audience/issuer configuration, and `CF-Access-Client-Id`, `CF-Access-Client-Secret`, and `CF-Access-Jwt-Assertion` handling. Rate limiting remains global/per-service as currently implemented; do not invent per-person identities from a shared key.

### 2. One gateway origin and existing bounded routes

Keep the existing gateway service on `127.0.0.1:8891` and publish only that listener via the Cloudflare Tunnel origin. Keep the public URL configurable, with `https://tencentservices.stocksuite.app` as the teammate branch default. Route Qwen intent requests to the GPU-host Qwen endpoint managed by the existing host supervisor, Nemotron transcription WebSockets to its realtime endpoint, and Kokoro synthesis to its HTTP service. Do not expose upstream URLs to clients or create separate public routes for those services.

All application-side Qwen, Nemotron, and Kokoro requests in remote mode must use the existing remote gateway client. Preserve the current structured request contracts and result-bound speech behavior; extend those existing contracts only as required for already-supported application Qwen workflows.

### 3. Clone-ready environment configuration

Provide a tracked teammate runtime environment file containing `DIMON_INFERENCE_MODE=remote`, `DIMON_INFERENCE_GATEWAY_URL=https://tencentservices.stocksuite.app`, and `DIMON_INFERENCE_SERVICE_KEY=<owner-supplied-value>`. Add a narrow `.gitignore` exception for that exact file while continuing to ignore other `.env` files and host-only credentials. Keep the key absent from `.env.example` if that example is intended for generic local use; document where the committed teammate config is and how the owner updates the shared key.

The host's ignored environment retains a separate `DIMON_INFERENCE_SERVICE_KEY` with the same value plus private loopback upstream URLs and model revisions. No Cloudflare Access values are needed. The existing tunnel should target port `8891`.

## Failure behavior

- Gateway startup fails clearly when its service key is missing or blank.
- Remote-mode application startup fails clearly when the shared key is missing or blank; fixture/local modes do not require it.
- Requests with absent or incorrect keys are rejected before inference work begins. HTTP and WebSocket errors remain bounded and do not expose secrets.
- Existing upstream timeouts, capacity limits, invalid model response handling, and service-unavailable behavior remain unchanged.
- Requests to the public URL fail when the owner retires that hostname/tunnel route. Updating the key requires updating both the host gateway environment and the tracked teammate environment file.

## Acceptance criteria

- The gateway binds to `127.0.0.1:8891`, with documentation naming `8891` as the Cloudflare Tunnel origin port.
- Valid shared-key HTTP requests can call intent and speech; valid shared-key WebSocket requests can call transcription.
- Missing and incorrect keys are rejected for all three routes; no Access headers or JWT assertions are required or used.
- Gateway configuration has no Access issuer, audience, JWKS, or trusted-proxy requirements.
- Remote-mode clients attach the same Bearer key on HTTP and WebSocket requests and send no Access service-token headers.
- Teammate clone configuration selects remote mode and the one hosted origin, with an explicitly tracked runtime environment file and the shared key setting.
- The existing model upstreams remain loopback-only and are reached through the GPU host's managed services.
- Existing payload, rate, size, idempotency, and speech result-bound contracts remain covered by the test suite.
- Documentation states plainly that a key committed to Git is available to all branch readers and that retiring the hostname removes the public route.

## Alternatives considered

1. **One shared Bearer key on the existing bounded gateway (selected).** Smallest change and matches clone-ready access; all three capabilities stay behind one loopback listener and the existing project contracts.
2. **Cloudflare Access service tokens plus a shared key.** Adds no value for the requested clone-without-separate-credential flow and retains the headers the user wants removed.
3. **Generic reverse proxy for raw model APIs.** Exposes broader, less constrained upstream interfaces and duplicates protocol handling already implemented by the project gateway.

## Out of scope

- Creating or changing the Cloudflare Tunnel, DNS record, or endpoint deployment from this repository.
- Rotating or choosing the service-key value; the owner will set it in the host and tracked teammate environment before publishing the usable branch.
- Publishing raw Qwen, Nemotron, or Kokoro service ports.
- Per-teammate identity, quotas, or credential revocation while all users share one key.

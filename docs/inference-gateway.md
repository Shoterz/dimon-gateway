# Inference gateway

The inference gateway is Dimon's only team-facing model boundary. It runs on
the GPU host at `127.0.0.1:8891`; the Cloudflare Tunnel hostname
`tencentservices.stocksuite.app` targets `http://127.0.0.1:8891`. Qwen, Nemotron,
and Kokoro remain on private loopback ports. Qwen intent requests go to the
loopback API managed by the GPU host's existing model supervisor.

The gateway exposes bounded Qwen tasks (`POST /v1/intent` and `POST /v1/qwen`),
transcription (`WS /v1/transcription`), and result speech (`POST /v1/speech`). It cannot read
Dimon's SQLite databases, approve a receipt, execute a transfer, select
arbitrary models, accept arbitrary prompts, or synthesize arbitrary text.
Grounding, policy, receipts, approval, and ledger writes remain in each Dimon
application process.

## Host configuration

The GPU host's ignored `.env.host` supplies the gateway settings. Use the same
rotated `DIMON_INFERENCE_SERVICE_KEY` as each authorized teammate's ignored
local environment:

```dotenv
DIMON_GATEWAY_HOST=127.0.0.1
DIMON_GATEWAY_PORT=8891
DIMON_INFERENCE_SERVICE_KEY=<shared-key>

DIMON_QWEN_API_BASE=http://127.0.0.1:8080/v1
DIMON_QWEN_MODEL=<configured-model-alias>
DIMON_QWEN_REVISION=<immutable-safe-revision>
DIMON_NEMOTRON_URL=ws://127.0.0.1:8004/v1/realtime
DIMON_NEMOTRON_CONTROL_SOCKET=<absolute-path-to-supervisor-control-socket>
DIMON_NEMOTRON_REVISION=<immutable-safe-revision>
DIMON_GATEWAY_STT_BACKEND=dual
DIMON_WHISPER_URL=ws://127.0.0.1:8007/ws
DIMON_WHISPER_REVISION=faster-whisper-medium-cuda
DIMON_KOKORO_URL=http://127.0.0.1:3001
DIMON_KOKORO_REVISION=<immutable-safe-revision>
```

When Nemotron runs under the Jarvis on-demand supervisor, set the control socket
path (for example `/run/user/1000/jarvis-nemotron.ctl.sock`). The gateway
acquires the supervisor before opening the WebSocket and releases it when the
session ends. Readiness checks the control socket, so an idle, unloaded Nemotron
model still reports as available.

In `dual` mode, every microphone chunk goes to both engines. Nemotron supplies
live `transcript.partial` captions and the primary final transcript used for
financial intent. Project-local Faster-Whisper Medium on CUDA runs in parallel
for disagreement diagnostics and bounded fallback when Nemotron cannot return
a final transcript. Its exact required identity is `medium/cuda/int8_float16`;
a CPU or differently sized worker fails readiness. Both workers are required
for the demo-ready four-worker profile even though one successful recognizer
can complete an individual utterance. The older CPU `small.en` service on port
`8003` is retained only as an explicit emergency rollback endpoint and does
not satisfy normal readiness.

The revision fields use short safe identifiers containing letters, digits,
`.`, `_`, `:`, `+`, `@`, or `-`. Start the gateway with
`uv run dimon-inference-gateway` or the configured systemd unit.

Set the Cloudflare Tunnel service for `tencentservices.stocksuite.app` to the
origin `http://127.0.0.1:8891`. This is the only public inference origin; never
publish the Qwen, Nemotron, or Kokoro ports.

## Teammate configuration

Copy `apps/dimon_live/.env.example` to the ignored `apps/dimon_live/.env`, then
configure remote mode and this origin:

```dotenv
DIMON_INFERENCE_MODE=remote
DIMON_INFERENCE_GATEWAY_URL=https://tencentservices.stocksuite.app
DIMON_INFERENCE_SERVICE_KEY=<shared-key>
```

The owner sets the key in the host `.env.host` and distributes it out of band
to authorized teammates. The same Bearer key authenticates HTTP and WebSocket
requests; it is sent only by
the Dimon backend, never browser JavaScript. Retiring the tunnel hostname
removes the public route.

## Public contracts

HTTP and WebSocket requests send `Authorization: Bearer <shared-key>`. HTTP
requests may also send a UUIDv4 in `X-Dimon-Correlation-Id`; the gateway
creates one when missing or invalid and returns the accepted value in the
response header. Rate limits apply to the shared key as one caller bucket.

`POST /v1/intent` accepts exactly:

```json
{
  "request_id": "00000000-0000-4000-8000-000000000001",
  "text": "Send John Smith S$50 from savings.",
  "contract_version": 4
}
```

Text is limited to 8 KiB. The gateway fixes the model, system prompt, schema,
and sampling policy. One Qwen request may run, four may wait, and a request has
a 120-second deadline. The shared key may submit 12 intent requests per minute.
Successful responses include validated intent, bounded raw output, model alias,
immutable revision, and model latency.

`POST /v1/qwen` accepts one of the fixed tasks `transfer`, `stock`,
`bill_payment`, or `investment_follow_up`, plus bounded text and a request ID.
Only investment follow-up accepts bounded phase context. The gateway selects
the Qwen model, system prompt, JSON schema, and sampling settings itself; callers
cannot send arbitrary model names or prompts. Responses include the task, raw
model output validated against that task's schema, model revision, and latency.
These calls share the intent worker and the 12-request-per-minute key limit.

`WS /v1/transcription` accepts binary 16 kHz mono PCM16 frames followed by
`{"type":"input.commit"}`. Server events are `session.ready`,
`transcript.partial`, `transcript.final`, and `error`, each with
`protocol_version: 1`. Audio is limited to 4 MiB and 30 seconds. One voice
session may run and none wait; a competing session receives `VOICE_BUSY`. The
shared key may start six sessions per minute.

`POST /v1/speech` accepts only fields derived from a committed synthetic result.
The gateway constructs the sentence and returns a validated mono 16-bit WAV
no larger than 8 MiB. One synthesis may run, four may wait, and a request has
a 30-second deadline. The shared key may submit 20 speech requests per minute.

## Health, errors, and observability

Both health routes require the shared key. `GET /health/live` checks gateway
liveness. `GET /health/ready` performs bounded probes for intent,
transcription, and speech, returning HTTP 503 if any capability is unavailable.
The response contains only capability names, readiness, safe reason codes, and
safe revision identifiers.

HTTP failures use this envelope:

```json
{
  "error": {
    "code": "UPSTREAM_UNAVAILABLE",
    "message": "safe public description",
    "request_id": "00000000-0000-4000-8000-000000000001"
  }
}
```

Stable codes include `UNAUTHORIZED`, `INVALID_REQUEST`,
`INVALID_INFERENCE_REQUEST`, `IDEMPOTENCY_CONFLICT`, `RATE_LIMITED`,
`CAPACITY_EXCEEDED`, `VOICE_BUSY`, `AUDIO_LIMIT_EXCEEDED`,
`UTTERANCE_LIMIT_EXCEEDED`, `UPSTREAM_TIMEOUT`, and `UPSTREAM_UNAVAILABLE`.
Logs contain a process-keyed shared-caller hash, correlation ID, fixed route,
outcome, queue delay, and latency; they never contain the key or inference
content.

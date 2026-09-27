# Inference gateway

The inference gateway is Dimon's only team-facing model boundary. It runs on the
GPU host at `127.0.0.1:8891`; Cloudflare Tunnel publishes that single loopback
service as `dimon-inference.stocksuite.app`. Qwen, Nemotron, and Kokoro continue
to listen only on loopback and are never direct public routes.

The gateway can propose an intent, transcribe bounded audio, or speak a
committed synthetic result. It cannot read Dimon's SQLite databases, approve a
receipt, execute a transfer, select arbitrary models, accept arbitrary prompts,
or synthesize arbitrary text. Grounding, policy, receipts, approval, and ledger
writes remain in each Dimon application process.

## Host configuration

Copy `.env.example` to an ignored environment file and set these values on the
GPU host:

```dotenv
DIMON_GATEWAY_HOST=127.0.0.1
DIMON_GATEWAY_PORT=8891
DIMON_GATEWAY_TRUSTED_PROXIES=127.0.0.1,::1
DIMON_CF_ACCESS_AUDIENCE=<access-application-audience>
DIMON_CF_ACCESS_ISSUER=https://<team-name>.cloudflareaccess.com

DIMON_QWEN_API_BASE=http://127.0.0.1:8080/v1
DIMON_QWEN_MODEL=<configured-model-alias>
DIMON_QWEN_REVISION=<immutable-safe-revision>
DIMON_NEMOTRON_URL=ws://127.0.0.1:8004/v1/realtime
DIMON_NEMOTRON_REVISION=<immutable-safe-revision>
DIMON_KOKORO_URL=http://127.0.0.1:3001
DIMON_KOKORO_REVISION=<immutable-safe-revision>
```

Revision values are returned by readiness and model responses, so they accept
only short identifiers containing letters, digits, `.`, `_`, `:`, `+`, `@`, or
`-`. Do not put paths, credentials, or mutable labels in revision fields.

Start the gateway with the locked project environment:

```bash
uv run dimon-inference-gateway
```

Configure the tunnel origin as `http://127.0.0.1:8891`. Protect the hostname
with a Cloudflare Access application whose audience matches
`DIMON_CF_ACCESS_AUDIENCE`. Create separate service-token credentials for each
team or CI client and keep them in ignored local environment files or the CI
secret store.

## Teammate remote mode

A teammate selects the remote provider without installing CUDA or model
weights:

```dotenv
DIMON_INFERENCE_MODE=remote
DIMON_INFERENCE_GATEWAY_URL=https://dimon-inference.stocksuite.app
DIMON_CF_ACCESS_CLIENT_ID=<service-token-client-id>
DIMON_CF_ACCESS_CLIENT_SECRET=<service-token-client-secret>
```

The three values are required together. The gateway URL must be an HTTPS
origin. Service-token headers are sent only from the teammate's Dimon backend;
they must never be placed in frontend JavaScript or committed files.

## Public contracts

All HTTP requests send Cloudflare's `CF-Access-Client-Id` and
`CF-Access-Client-Secret` headers. They may also send a UUIDv4 in
`X-Dimon-Correlation-Id`; the gateway creates one when it is missing or invalid
and returns the accepted value in the same response header.

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
a 120-second deadline. Each verified identity may submit 12 intent requests per
minute. Successful responses include the validated intent, bounded raw output,
model alias, immutable revision, and model latency.

`WS /v1/transcription` accepts binary 16 kHz mono PCM16 frames followed by:

```json
{"type":"input.commit"}
```

Server events are `session.ready`, `transcript.partial`, `transcript.final`, and
`error`, each with `protocol_version: 1`. Audio is limited to 4 MiB and 30
seconds. One voice session may run and none wait; a competing session receives
`VOICE_BUSY`. Each identity may start six sessions per minute.

`POST /v1/speech` accepts only fields derived from a committed synthetic result:

```json
{
  "request_id": "00000000-0000-4000-8000-000000000002",
  "status": "EXECUTED_SYNTHETIC_TRANSFER",
  "action_kind": "BENEFICIARY_TRANSFER",
  "amount": 5000,
  "currency": "SGD",
  "recipient_display": "John Smith",
  "reference": "rent"
}
```

The gateway constructs the sentence. It returns a validated mono 16-bit 16 kHz
WAV no larger than 8 MiB. One synthesis may run, four may wait, and a request
has a 30-second deadline. Each identity may submit 20 speech requests per
minute.

## Health and readiness

Both health routes require Access authentication:

- `GET /health/live` proves the gateway process can authenticate and respond.
- `GET /health/ready` performs independent, bounded reachability probes for
  `intent`, `transcription`, and `speech` and returns their configured
  revisions. It returns HTTP 503 when any capability is unavailable or times
  out.

The readiness response contains only capability names, `ready`, `reason_code`,
and safe revision identifiers. It never returns raw ports, model paths, process
arguments, credentials, GPU details, or exception strings.

Example using placeholder credentials:

```bash
curl --fail-with-body \
  -H 'CF-Access-Client-Id: <service-token-client-id>' \
  -H 'CF-Access-Client-Secret: <service-token-client-secret>' \
  https://dimon-inference.stocksuite.app/health/ready
```

## Stable failures and observability

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

Stable codes include `UNAUTHORIZED`, `UNTRUSTED_PROXY`, `INVALID_REQUEST`,
`INVALID_INFERENCE_REQUEST`, `IDEMPOTENCY_CONFLICT`, `RATE_LIMITED`,
`CAPACITY_EXCEEDED`, `VOICE_BUSY`, `AUDIO_LIMIT_EXCEEDED`,
`UTTERANCE_LIMIT_EXCEEDED`, `UPSTREAM_TIMEOUT`, and `UPSTREAM_UNAVAILABLE`.
Upstream exception text is never returned.

The gateway emits one JSON event per HTTP request and transcription session.
Events contain the correlation ID, a process-keyed identity hash, method, fixed
route name, stable outcome code, queue delay, and total latency. They never
contain an Access token, prompt, transcript, intent, audio, recipient, amount,
or upstream exception. Bounded in-memory counters track request outcomes,
failure codes, latency buckets, active work, and queue depth. There is no public
metrics route in this MVP.

## Verification

The default test suite uses fake upstream adapters and requires no GPU:

```bash
uv run python -m unittest discover -s services/inference_gateway/tests -p 'test_*.py'
uv run python -m unittest apps.dimon_live.tests.test_provider_contract -v
uv run python -m unittest apps.dimon_live.tests.test_remote_inference -v
```

Run production-credential integration checks only from a trusted machine. Do
not expose service-token credentials to pull requests from forks or to browser
code.

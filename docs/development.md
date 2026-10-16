# Development modes

Dimon keeps the application and inference processes separate. Every developer
runs the FastAPI application, browser UI, deterministic grounding and policy,
receipt approval flow, session isolation, and a local SQLite ledger. The
selected inference mode supplies only intent extraction, transcription, and
result speech.

## First run

Use Python 3.12 and the committed `uv.lock`:

```bash
uv sync --locked --group dev
uv run dimon-serve
```

Open <http://127.0.0.1:8890>. Fixture mode is the credential-free default.
Remote mode uses an ignored local `apps/dimon_live/.env` copied from the
example and a service key supplied by the host operator. Model weights and CUDA
are not required on teammate machines.

Docker is intentionally optional for this checkpoint. The laptop application
has one Python environment and local SQLite files, so `uv` is the smaller daily
workflow. Containers can be added for deployment or host orchestration without
changing the provider contracts.

## Selecting a mode

| Mode | Use it for | Command | Current status |
| --- | --- | --- | --- |
| `fixture` | Daily feature work, UI work, CI, and deterministic demos | `uv run dimon-dev` | Implemented and the default |
| `remote` | Using the hosted team inference services | `uv run dimon-serve` with an ignored local `.env` | Implemented; requires a rotated service key |
| `local` | GPU-host staging and the judge-facing deployment | `DIMON_INFERENCE_MODE=local uv run dimon-serve` | Implemented; requires the three loopback model services |

There is no automatic fallback between modes. A fixture mismatch cannot call a
live model, and a failed live provider cannot silently return fixture output.
The browser reads the public mode name from `/api/session` and displays
`Fixture inference` while fixtures are active. Upstream URLs and credentials
are never returned by that endpoint.

## Fixture mode

Intent fixtures are declared in `fixtures/inference/manifest.json`. Matching is
case-insensitive and collapses whitespace, but is otherwise exact. The exact
prompt list is in the repository README and the application's test playbook.

Voice and speech default to their `normal` cases. Select a failure case before
starting the application when testing fallback UI:

```bash
DIMON_FIXTURE_VOICE_CASE=voice-busy uv run dimon-dev
DIMON_FIXTURE_VOICE_CASE=nemotron-unavailable uv run dimon-dev
DIMON_FIXTURE_SPEECH_CASE=kokoro-unavailable uv run dimon-dev
```

The committed fixture selector values are:

| Variable | Values | Default |
| --- | --- | --- |
| `DIMON_FIXTURE_VOICE_CASE` | `normal`, `voice-busy`, `nemotron-unavailable` | `normal` |
| `DIMON_FIXTURE_SPEECH_CASE` | `normal`, `kokoro-unavailable` | `normal` |

## Local GPU-host mode

Local mode connects only to model processes configured by these variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DIMON_QWEN_API_BASE` | `http://127.0.0.1:8080/v1` | llama-server OpenAI-compatible API |
| `DIMON_QWEN_MODEL` | `qwen35-9b-abliterated-q5` | Server model identifier |
| `DIMON_NEMOTRON_URL` | `ws://127.0.0.1:8004/v1/realtime` | Streaming transcription socket |
| `DIMON_NEMOTRON_CONTROL_SOCKET` | `$XDG_RUNTIME_DIR/jarvis-nemotron.ctl.sock` | Host lifecycle control socket |
| `DIMON_KOKORO_URL` | `http://127.0.0.1:3001` | Result-speech service |
| `DIMON_NEMOTRON_CONNECT_TIMEOUT` | `3` | Connection deadline in seconds |
| `DIMON_NEMOTRON_RESULT_TIMEOUT` | `15` | Final transcript deadline in seconds |
| `DIMON_NEMOTRON_START_TIMEOUT` | `120` | Lifecycle startup deadline in seconds |

The Qwen, Nemotron, and Kokoro weights stay outside Git and are stored once on
the GPU host. Do not point browser code at these ports. The FastAPI application
is their only application-facing caller.

## Remote mode configuration

The gateway checkpoint will consume these server-side variables:

| Variable | Purpose |
| --- | --- |
| `DIMON_INFERENCE_GATEWAY_URL` | Bounded gateway origin, `https://tencentservices.stocksuite.app` for teammate mode |
| `DIMON_INFERENCE_SERVICE_KEY` | Shared Bearer key used by the remote client and host gateway |

The teammate key belongs only in the ignored local `apps/dimon_live/.env`.
The GPU host keeps the same value in its ignored `.env.host`. Never place the
key in Git, browser JavaScript, fixture manifests, logs, or model upstream requests.

## Shared limits and failures

These settings apply at the application boundary in every mode:

| Variable | Default |
| --- | --- |
| `DIMON_MAX_AUDIO_BYTES` | `4000000` |
| `DIMON_MAX_UTTERANCE_SECONDS` | `30` |
| `DIMON_MAX_WAV_BYTES` | `8388608` |
| `DIMON_VISITOR_TTL_SECONDS` | `3600` |
| `DIMON_MAX_VISITOR_SESSIONS` | `25` |

Provider failures use stable reason codes so UI and API behavior do not depend
on private exception text:

| Reason code | Meaning | Expected behavior |
| --- | --- | --- |
| `FIXTURE_NO_MATCH` | No exact intent fixture exists | Return a failed action and leave SQLite unchanged |
| `INVALID_MODEL_OUTPUT` | Intent output violates the bounded schema | Reject it before policy or execution |
| `UPSTREAM_TIMEOUT` | A fixture or gateway deadline expired | Fail closed; a retry is a new explicit action |
| `UPSTREAM_UNAVAILABLE` | Voice or speech provider is unavailable | Keep typed interaction available |
| `VOICE_BUSY` | The single voice lease is occupied | Reject the second voice turn without queuing it |
| `MALFORMED_AUDIO` | Audio is empty or not float32 frame-aligned | Close the voice turn without calling an action |
| `AUDIO_LIMIT_EXCEEDED` | Audio exceeds 4 MiB | Close the voice turn |
| `UTTERANCE_LIMIT_EXCEEDED` | Audio exceeds 30 seconds | Close the voice turn |
| `STREAM_CLOSED` | Audio arrived after cancellation or close | Reject the late frame |
| `REMOTE_PROVIDER_UNAVAILABLE` | Remote client is not present in this checkpoint | Stop startup with an explicit configuration error |

Kokoro failure after a committed transfer never rolls back or changes the text
result. Inference failures before approval or execution never mutate SQLite.

## Verification

Run the provider parity contract without any live model services:

```bash
uv run python -m unittest apps.dimon_live.tests.test_provider_contract -v
```

Run the complete repository checks before sharing a branch:

```bash
uv lock --check
DIMON_INFERENCE_MODE=fixture uv run pytest -q
scripts/check-repository-boundary
git diff --check
```

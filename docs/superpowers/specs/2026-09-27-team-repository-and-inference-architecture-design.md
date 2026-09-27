# Dimon team repository and inference architecture

**Date:** 2026-09-27
**Status:** proposed design for team review
**Working repository name:** `dimon-gateway`

## Purpose

Create one team-owned repository that contains the Dimon application, its
deterministic execution boundary, inference contracts, development fixtures,
host launch definitions, tests, and operating documentation. Teammates must be
able to clone the repository and develop without CUDA or model downloads. The
shared GPU host supplies live Qwen, Nemotron, and Kokoro inference through one
authenticated project gateway.

The repository will not contain model weights, compiled model runtimes,
generated audio, active databases, secrets, tunnel credentials, or personal
machine paths. The existing Jarvis and Llama projects remain available and
unchanged.

## Success criteria

1. A clean laptop can install the locked Python environment and start a visibly
   labelled fixture-backed application with `uv`.
2. An authorized teammate can switch the same application to remote inference
   without changing application code.
3. The GPU host runs the public demo and project inference gateway while its
   raw Qwen, Nemotron, and Kokoro ports remain bound to loopback.
4. The repository records exact model/runtime provenance and can reconstruct a
   standalone GPU host without copying artifacts from personal projects.
5. The initial Git upload contains no active secrets, model weights, generated
   runtime data, or personal absolute paths.
6. `llama.stocksuite.app` and `jarvis.stocksuite.app` keep their current
   purposes and configurations.
7. Git history contains no Codex or ChatGPT contributor trailers. CodeBuddy is
   credited only for work completed in a genuine CodeBuddy session.

## Selected approach

The project uses central GPU inference with local application development.
Every teammate runs the real FastAPI application, deterministic contracts,
policy, receipts, execution code, and an isolated SQLite ledger. Daily work
uses deterministic inference fixtures. Integration work calls the project
inference gateway on the owner's GPU PC.

```text
                          GPU HOST
┌───────────────────────────────────────────────────────────┐
│ Qwen 3.5 / llama-server             127.0.0.1:8080        │
│ Nemotron / NeMo-Speech              127.0.0.1:8004        │
│ Kokoro                              127.0.0.1:3001        │
│ GPU lifecycle and capacity manager                         │
│                                                           │
│ Dimon inference gateway                                  │
│   /v1/intent        -> bounded Qwen extraction            │
│   /v1/transcription -> bounded Nemotron WebSocket         │
│   /v1/speech        -> bounded Kokoro result speech       │
└────────────────────────────┬──────────────────────────────┘
                             │ Cloudflare Tunnel and Access
                             ▼
                dimon-inference.stocksuite.app
                             ▲
                             │ authenticated server calls
┌────────────────────────────┴──────────────────────────────┐
│ TEAMMATE LAPTOP                                          │
│ uv-managed FastAPI app + frontend + isolated SQLite      │
│                                                         │
│ fixture mode -> deterministic local inference fixtures   │
│ remote mode  -> authenticated Dimon inference gateway    │
└───────────────────────────────────────────────────────────┘
```

### Alternatives not selected

**Full local inference for every teammate:** setup requires compatible NVIDIA
hardware, CUDA, native runtimes, and several gigabytes of weights. It would
block ordinary frontend and backend work.

**Raw public model endpoints:** publishing `llama-server`, Nemotron, or Kokoro
would expose broad model capabilities and couple clients to internal ports and
protocols. The project gateway will expose bounded Dimon operations instead.

**Docker-first development:** the current laptop application needs one Python
process, browser assets, and SQLite. Docker would add networking and filesystem
work before it solves a demonstrated environment problem. The team can add a
container later without changing the service contracts.

## Runtime modes

The application selects one inference provider at startup. The mode appears in
the health response and in a visible UI badge.

| Mode | Intended user | Inference source | Failure behavior |
| --- | --- | --- | --- |
| `fixture` | Daily teammate development and CI | Versioned deterministic fixtures | Unknown fixture case returns `FIXTURE_NO_MATCH`; it does not call a live model. |
| `remote` | Teammate integration testing | `https://dimon-inference.stocksuite.app` | Authentication, capacity, timeout, and upstream failures remain explicit. No fixture fallback occurs. |
| `local` | GPU-host staging and judge demo | Loopback Qwen, Nemotron, and Kokoro adapters | Existing fail-closed model and voice errors remain visible. No fixture fallback occurs. |

The application keeps policy and execution local in every mode. The remote
gateway returns inference results and audio only. It never receives SQLite
paths, ledger credentials, receipt approval authority, or a general Python,
shell, SQL, or tool-execution interface.

## Public hostnames

| Hostname | Access | Purpose | Origin |
| --- | --- | --- | --- |
| `dimon.stocksuite.app` | Public judge/demo access | Complete Dimon application with per-visitor synthetic ledgers | `http://127.0.0.1:8890` |
| `dimon-inference.stocksuite.app` | Cloudflare Access; teammates and CI only | Bounded Qwen, STT, and TTS gateway | `http://127.0.0.1:8891` |
| `dimon-staging.stocksuite.app` | Cloudflare Access; team only | Shared integration build and operational checks | `http://127.0.0.1:8892` with separate data paths |
| `llama.stocksuite.app` | Existing policy | Personal model service | Unchanged |
| `jarvis.stocksuite.app` | Existing policy | Personal Jarvis service | Unchanged |

Cloudflare Tunnel publishes only the three Dimon origins. It does not route
ports 8080, 8004, or 3001. Human staging access uses identity-based Access.
Local backends and CI use individually revocable service credentials stored in
ignored environment files or the CI secret store. Browser JavaScript never
receives inference credentials.

For remote HTTP calls, the provider sends its Access service credentials in
request headers. For transcription, it sends the same credentials during the
WebSocket upgrade. Cloudflare Access validates the credentials before
forwarding the request, and the gateway maps the validated Access identity to
its rate-limit key.

The public demo does not select a persistent owner ledger. Each public session
receives an isolated synthetic visitor ledger. Any owner-only operational view
must use the Access-protected staging hostname.

## Inference gateway contracts

The inference gateway is a small FastAPI service under
`services/inference_gateway/`. It owns authentication mapping, request limits,
upstream adapters, concurrency, timeouts, and safe error translation.

### Intent extraction

`POST /v1/intent`

Request fields:

- `request_id`: UUID used for correlation and idempotent retry;
- `text`: UTF-8 request text, 8 KiB maximum;
- `contract_version`: one of the versions supported by the repository.

The gateway chooses the configured Qwen model, system prompt, JSON Schema, and
generation settings. It returns the validated intent, bounded raw model output
for audit, model identifier, model revision, and latency. Clients cannot submit
an arbitrary prompt, model name, response schema, sampling configuration, or
tool definition.

Qwen permits one active request and four queued requests. A full queue returns
HTTP 429 with `INFERENCE_CAPACITY`. Each request has a 120-second deadline.
Each Access identity may start at most 12 intent requests per minute.

The gateway caches a completed response for ten minutes under the Access
identity and request ID. An identical retry returns the cached response. Reuse
of that request ID with different text or contract version returns HTTP 409
with `IDEMPOTENCY_CONFLICT`.

### Streaming transcription

`WS /v1/transcription`

The authenticated server client sends 16 kHz mono PCM16 frames and a commit
event. The gateway starts or acquires Nemotron on the GPU host, forwards bounded
audio, and emits partial and final transcript events using a versioned project
protocol. The gateway accepts at most 4 MiB or 30 seconds of audio per turn.

Nemotron permits one active voice turn and does not queue callers. A second
caller receives `VOICE_BUSY`. Disconnect, timeout, malformed audio, and upstream
failure release the lease.
Each Access identity may start at most six transcription sessions per minute.

### Result speech

`POST /v1/speech`

The request contains a supported committed outcome and its bounded display
fields: request ID, recipient display name, integer minor-unit amount, currency,
and optional batch count. The gateway constructs the spoken sentence. It does
not accept arbitrary text, account IDs, receipt secrets, or model output.

Kokoro permits one active synthesis and four queued requests. Each request has
a 30-second deadline. The response is a validated WAV payload no larger than
8 MiB.
Each Access identity may start at most 20 speech requests per minute.

### Health

- `GET /health/live` proves the gateway process can serve requests.
- `GET /health/ready` reports only stable dependency state and reason codes.
- Detailed model paths, process arguments, credentials, GPU state, and logs
  remain available only on the host.

The remote health routes require Access. The public Dimon application exposes
no inference health route.

## Error contract

| Condition | HTTP or WebSocket result | Stable reason |
| --- | --- | --- |
| Missing or invalid Access identity | 401 or 403 | `INFERENCE_UNAUTHORIZED` |
| Per-client rate exceeded | 429 | `INFERENCE_RATE_LIMITED` |
| Global bounded queue full | 429 | `INFERENCE_CAPACITY` |
| Request ID reused with different input | 409 | `IDEMPOTENCY_CONFLICT` |
| Voice turn already active | WebSocket close with application error | `VOICE_BUSY` |
| Invalid request or audio | 422 or WebSocket close | `INVALID_INFERENCE_REQUEST` |
| Upstream unavailable | 503 | Existing model-specific unavailable reason |
| Upstream deadline exceeded | 504 | `INFERENCE_TIMEOUT` |
| Fixture has no declared case | 422 | `FIXTURE_NO_MATCH` |

All inference failures leave the local ledger unchanged. Kokoro failure after a
committed transaction preserves the text result and ledger outcome.

## Repository structure

```text
dimon-gateway/
├── apps/
│   └── dimon_live/                 Existing web app and execution boundary
├── services/
│   ├── inference_gateway/          Authenticated bounded host API
│   ├── qwen/                       Launcher, checks, and provenance adapter
│   ├── nemotron/                   Launcher, checks, and lifecycle adapter
│   └── kokoro/                     Launcher, checks, and provenance adapter
├── fixtures/
│   └── inference/                  Named deterministic development cases
├── config/
│   └── models.example.yaml         Revisions, checksums, licenses, path variables
├── scripts/
│   ├── setup-host                  Validate or acquire external artifacts
│   ├── start-host                  Start the project-owned host mode
│   ├── start-dev                   Start fixture or remote laptop mode
│   └── healthcheck                 Verify the selected mode
├── tests/                          Cross-component contract and smoke tests
├── docs/
│   ├── architecture.md             User-facing architecture and trust boundaries
│   ├── development.md              Laptop setup and feedback loop
│   ├── gpu-host-setup.md           Shared and standalone host modes
│   ├── model-provenance.md         Model/runtime sources and license notes
│   └── runtime-service-catalog.md  Living component inventory
├── pyproject.toml                  Python 3.12 application and tool configuration
├── uv.lock                         Committed cross-platform dependency lock
├── .env.example                    Non-secret mode and endpoint template
├── .gitignore                      Secrets, weights, runtimes, and generated data
├── LICENSE
└── THIRD_PARTY_NOTICES.md
```

## Python environment

The project targets Python 3.12. `pyproject.toml` owns application and
development dependencies. `uv.lock` is committed. A fresh contributor uses:

```bash
uv sync --locked
uv run dimon-dev
uv run pytest
```

`dimon-dev` defaults to fixture mode and binds only to loopback. Remote mode
requires an explicit environment setting and Access credentials. The repository
does not activate remote inference during tests unless a separately named
integration command is invoked.

Docker remains optional. If deployment later requires an image, the Dockerfile
will use the same `pyproject.toml` and `uv.lock`. It will not contain weights or
host credentials.

## GPU-host reuse and independence

The owner will not clone Qwen weights or `llama.cpp` into the project directory.
Host configuration points to one canonical model file and one existing runtime
binary through ignored environment variables. Repository launchers accept those
paths and never copy the artifacts.

The host supports two configurations:

1. **Shared-host mode:** use the owner's existing model processes and capacity
   manager. This is the normal development and demonstration configuration.
2. **Standalone-host mode:** use repository-owned launch definitions against
   downloaded artifacts on another compatible Linux/NVIDIA machine. Only one
   configuration may bind the project ports at a time.

The repository must contain enough configuration to reproduce standalone-host
mode without reading files from the personal Jarvis, Llama, Kokoro, or GPU
manager repositories. It may use the same upstream projects and model files,
subject to their licenses. Imported source retains required copyright and
license notices.

`config/models.example.yaml` records for each artifact:

- upstream project or model repository;
- exact revision or release;
- expected filename and byte size;
- SHA-256 checksum;
- license name and source URL;
- environment variable that supplies its local path; and
- compatible launcher/runtime version.

## Fixture mode

Fixture mode uses the same provider interfaces as live inference. Fixtures are
named, versioned, and inspectable. The first set covers:

- exact beneficiary transfer;
- ambiguous recipient;
- missing amount and amount-only continuation;
- exact and unknown synthetic PayNow proxy;
- atomic saved-beneficiary batch;
- prompt-injection attempt;
- malformed model response;
- Qwen timeout;
- Nemotron unavailable and voice busy; and
- Kokoro unavailable after a committed result.

The UI displays `FIXTURE MODE` on every page. Fixture responses include their
case ID. Unknown text returns `FIXTURE_NO_MATCH`; fixture mode does not guess or
contact the live gateway.

## Git publication boundary

The current directory is not a valid Git repository. The migration creates a
fresh repository with `main` as the initial branch. The working name is
`dimon-gateway`; the team can rename it before remote creation.

The first upload includes application source, static assets, schemas, tests,
the source, tests, schemas, and saved result JSON under
`experiments/local-structured-action/`, visualizations, design documents, and
runbooks. It excludes:

- `apps/dimon_live/.env` and every active secret file;
- `runtime/`, generated audio, visitor ledgers, and demo databases;
- `.venv/`, bytecode, caches, editor state, and `.superpowers/` scratch data;
- GGUF, SafeTensors, ONNX, PyTorch, and other model weight formats;
- downloaded or compiled Nemotron, llama.cpp, and Kokoro runtimes;
- Cloudflare tunnel credentials, Access secrets, certificates, and tokens;
- personal absolute paths such as `/home/<user>/...` and user-specific runtime
  socket paths; and
- the supplied competition PDF by default. Replace it with a citation to the
  official source; add the file only if the team confirms public redistribution
  is permitted.

Before the first commit, the migration replaces personal paths with environment
variables, expands `.gitignore`, scans filenames and content for secrets, and
checks the complete staged file list. The repository starts private during this
audit. It becomes public only after the team reviews the staged inventory,
attribution, third-party notices, fixture labelling, and clean-clone result.

Git commits use each contributor's configured name and email. Commits created
during this work contain no Codex or ChatGPT `Co-authored-by` trailers. The team
must not add CodeBuddy attribution to work CodeBuddy did not perform. Genuine
CodeBuddy changes keep their real commits and the evidence required by the
competition.

## Development and integration flow

1. A teammate clones the repository and runs the fixture-backed app with `uv`.
2. They modify frontend, API, contracts, policy, or execution code and run local
   tests against an isolated temporary ledger.
3. When model behavior matters, they add ignored Access credentials and select
   remote mode.
4. Their local FastAPI process calls the bounded inference gateway while policy,
   receipts, approvals, and ledger writes remain local.
5. They open `dimon-staging.stocksuite.app` for the shared integrated build.
6. A reviewed change enters `main`; CI repeats the locked fixture-mode checks.
7. The GPU host deploys the reviewed main revision to the public demo.

## Testing and evidence

CI requires no GPU and performs:

- dependency lock verification;
- unit and API tests;
- browser module tests;
- fixture-mode end-to-end smoke tests;
- schema and gateway contract tests;
- secret and prohibited-artifact checks; and
- documentation link and configuration-example checks.

Named, opt-in integration checks run against the Access-protected gateway. They
cover one Qwen extraction, one Nemotron transcript, one Kokoro result, auth
rejection, capacity rejection, timeout mapping, and correlation IDs. These
checks never run from an untrusted pull request with production credentials.

The public deployment is accepted only after a localhost rehearsal and a
Cloudflare rehearsal confirm visitor isolation, typed fallback, voice-busy
behavior, immutable receipt approval, replay safety, and no public inference or
health endpoint.

## Workstream decomposition

This architecture is too large for one implementation plan. Implement it as
ordered, independently reviewable workstreams:

1. **Repository foundation:** sanitize the tree, add `pyproject.toml`, `uv.lock`,
   entry points, ignored artifacts, notices, and clean-clone checks.
2. **Provider boundary and fixture mode:** define shared inference interfaces,
   adapt the current local clients, build labelled fixtures, and make fixture
   mode the laptop default.
3. **Inference gateway:** add the bounded HTTP/WebSocket contracts,
   authentication mapping, capacity limits, health, and remote provider.
4. **GPU-host reproducibility:** add launch definitions, model manifest,
   shared-host configuration, standalone-host configuration, and health checks.
5. **Cloudflare environments:** configure the inference and staging hostnames,
   Access policies, service credentials, WebSocket verification, and public
   demo boundary while preserving personal routes.
6. **GitHub publication and CI:** initialize the fresh history, verify the staged
   inventory, create the private remote, enable CI and review rules, then make
   the repository public after the publication audit.

Each workstream must leave a usable system. Workstreams 1 and 2 give teammates
a complete offline feedback loop before any external route changes. Workstream
3 enables live model feedback. Workstreams 4 through 6 make the demo
reproducible and publishable.

## Acceptance checklist

- A clean clone starts fixture mode using the committed lockfile.
- Fixture mode is visibly labelled and never calls the network.
- Remote mode reaches only the bounded project inference gateway.
- Unauthenticated inference requests fail before reaching a model.
- Raw model ports remain loopback-only.
- Personal Jarvis and Llama routes still work unchanged.
- Teammate ledgers remain local and isolated from staging and demo ledgers.
- Live inference failure never triggers a hidden fixture response.
- No secret, active database, generated audio, model weight, compiled runtime,
  personal absolute path, or tunnel credential appears in the staged file list.
- Model and runtime revisions, checksums, licenses, and launch versions are
  recorded.
- CI passes without GPU access.
- Contributor metadata contains no false CodeBuddy attribution and no Codex or
  ChatGPT contributor trailer.

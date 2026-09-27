# Dimon runtime and service catalog

**Status:** living document. Update this catalog whenever the team adds,
removes, replaces, or changes the deployment boundary of a component.

This catalog tracks every part required to develop, run, demonstrate, and
reproduce Dimon. A component does not need to become a network service. Keep
code inside the Dimon FastAPI process unless it needs an independent hardware
allocation, process lifecycle, security boundary, or scaling policy.

## Status labels

| Label | Meaning |
| --- | --- |
| Working | The repository contains the integration and the current demo uses it. |
| Host dependency | The integration lives here, but the running dependency and its large artifacts live on the GPU host. |
| Partial | Some behavior exists, but the listed completion evidence is missing. |
| Planned | Product work is described but has not been implemented. |
| Missing | The team needs this for reproducible development or deployment. |

## Deployment boundaries

| Boundary | Runs on teammate laptops | Runs on the shared GPU host | Stored in Git |
| --- | --- | --- | --- |
| Browser interface | Yes | Yes | Source and static assets |
| Dimon FastAPI application | Yes | Yes | Source, configuration template, and tests |
| Deterministic policy and execution | Yes | Yes | Source, schemas, migrations, and tests |
| SQLite ledger | One disposable ledger per developer | Separate staging/demo ledgers | Schema and seed logic only |
| Inference fixtures | Yes | Optional | Fixture definitions and fixture service |
| Qwen, Nemotron, and Kokoro inference | Remote or replaced by fixtures | Yes | Adapters, launch definitions, and provenance only |
| Model weights and generated runtime data | No | Yes | No |

The shared inference route must use project-specific authentication. Personal
`llama.stocksuite.app` and `jarvis.stocksuite.app` routes remain outside the
Dimon deployment.

## Current runtime components

| Component | Type and boundary | Status | Current implementation | Next completion evidence |
| --- | --- | --- | --- | --- |
| Browser microphone capture and resampling | Browser module | Working | `apps/dimon_live/static/voice_capture.mjs` and `voice_capture_worklet.js` capture device audio and resample it to 16 kHz. | Cross-browser microphone rehearsal and measured audio failures. |
| Voice activity and endpoint detection | Browser plus FastAPI module | Working | Browser controls the turn while `VoiceTurnDetector` applies bounded server-side speech and silence rules. | Noisy-room and long-utterance evaluation. |
| Nemotron streaming transcription | Host inference service with a FastAPI adapter | Host dependency | The authenticated PCM16 gateway, pinned manifest, loopback launcher, capacity profile, and shared/standalone host checks are repository-owned. | Live rehearsal after the compatible host service is running. |
| Qwen structured intent extraction | Host inference service with an in-process adapter | Host dependency | The authenticated bounded gateway, pinned manifest, fixed llama-server launcher, and shared/standalone host checks are repository-owned. | Authenticated remote-host latency rehearsal. |
| Deterministic grounding and policy | In-process Python module | Working | Python derives amounts, binds evidence, resolves recipients, applies policy, and treats model output as untrusted. | Extend the same boundary to the planned action types. |
| Receipt and approval contracts | In-process Python module plus JSON Schema | Working | Server-created digests bind stored single and batch receipts to approval. | Receipt versioning, correction supersession, and stronger user authorization. |
| Python and SQLite execution gateway | In-process Python module | Working | `ActionService` and `Database` execute synthetic transfers atomically with replay protection. | Mock KYC, ownership, value, and velocity rules. |
| Session and visitor isolation | FastAPI module plus per-session SQLite files | Working | `SessionManager` separates owner and visitor state and bounds visitor lifetime. | Staging tests for expiry, cleanup, and concurrent visitors. |
| Kokoro spoken results | Host inference service with an in-process adapter | Host dependency | The committed-result gateway, pinned manifest, repository-owned minimal FastAPI service, fixed loopback launcher, and bounded WAV validation are repository-owned. | Reconfigure the current personal listener to loopback, then measure live synthesis latency. |
| Audio artifact storage and expiry | In-process module plus runtime directory | Working | Opaque, session-scoped WAV artifacts expire from `runtime/audio`. | Restart cleanup and disk-usage limits. |
| GPU lifecycle and capacity control | Host operations service | Working | A repository-owned FIFO capacity broker, conservative profiles, PID identity checks, crash backoff, shared/standalone orchestration, health report, and user-service templates manage only project processes. | Complete the gated live standalone rehearsal after owner-managed ports are free. |
| Inference authentication and rate limiting | Project inference gateway | Working | Cloudflare Access assertions establish user or service identity before bounded Qwen, STT, or TTS work; per-identity rate limits and global capacity limits have contract tests. | Deploy the Access application, issue revocable teammate credentials, and rehearse from outside the host. |
| Health and readiness reporting | FastAPI and host operations | Working | Authenticated liveness and bounded per-capability readiness report only safe reason codes and immutable revisions. | Connect the checks to project-owned host launch definitions and rehearse model restart failures. |
| Request correlation and latency metrics | Cross-cutting in-process concern | Partial | The inference gateway propagates UUID correlation IDs and records content-free structured events, latency buckets, active work, queue depth, and stable failures. | Carry the same correlation ID through browser capture, local policy, ledger result, and speech artifact delivery. |
| Model provenance, revisions, checksums, and licenses | Repository metadata | Working | The machine-readable manifest pins all three models by immutable revision, byte size, SHA-256, license, and external path variable; the host artifacts were verified in place. | Record native-runtime digests in the same manifest when those runtimes are upgraded. |
| Local fixture mode | Local development service | Working | `uv run dimon-dev` starts labelled deterministic Qwen, Nemotron, and Kokoro fixtures without CUDA, model weights, Docker, or network access. | Extend labelled cases as product contracts grow and keep the fixture smoke test in CI. |
| Dependency locking and reproducible setup | Repository tooling | Working | The root `pyproject.toml`, `uv.lock`, Python version, console commands, and clean-boundary tests are committed. | Rehearse the locked setup on a clean teammate machine and in CI. |
| CI and contract tests | Repository automation | Partial | Python and browser contract tests exist, but the repository has no visible CI workflow. | CI runs locked Python tests, browser module tests, static checks, and fixture-mode smoke tests. |
| Cloudflare routing, CORS, and WebSocket behavior | Deployment configuration | Partial | The application supports explicit origins and public WebSockets through the current tunnel. Routing still depends on owner-managed configuration. | Project hostnames, Access policy, checked-in non-secret route documentation, and remote WebSocket rehearsal. |
| Typed fallback when speech is unavailable | Browser and FastAPI behavior | Working | Voice failures return the interface to typed input without changing ledger state. | Staging checks for cold start, disconnect, timeout, and busy voice service. |

## Planned product components

| Component | Intended boundary | Status | Completion evidence |
| --- | --- | --- | --- |
| Independent read-only validation agent | Start as an in-process adapter to an independently configured model call | Planned | Critical fields record `MATCH`; mismatch, unknown, malformed output, and timeout block approval with zero ledger writes. |
| Correction and receipt versioning | In-process contracts and database records | Planned | A correction creates a new version and makes every prior receipt impossible to approve. |
| Strong approval or mock biometric boundary | Separate approval interface backed by server-owned challenges | Planned | Altered, expired, wrong-user, and replayed approvals fail without a debit. |
| Mock KYC, ownership, value, and velocity controls | Deterministic in-process policy | Planned | Direct tests prove each rule blocks execution before any ledger write. |
| Judge-facing evaluation and audit evidence | Evaluation tooling and durable reports | Planned | Held-out results include field accuracy, false approvals/refusals, replay rejection, and stage latency without invented measurements. |

## Development environment decision

Use `uv` as the default team setup. The application currently needs one Python
environment, static browser files, and SQLite. `uv sync` can create the local
environment from the committed lockfile, and `uv run` can keep it synchronized
while running the app or tests.

Target developer commands:

```bash
uv sync --locked
uv run dimon-dev
uv run pytest
```

Docker is deferred. Add it when one of these conditions becomes true:

- contributors repeatedly hit operating-system or native-library differences;
- local development requires two or more real network services instead of one
  application plus fixtures;
- the deployment target accepts an image and benefits from the same tested
  artifact used in CI; or
- the team moves the GPU stack to another host and needs a repeatable host
  installation.

Do not add Docker to solve model size or GPU availability. A container still
needs the model weights, host NVIDIA driver, compatible GPU runtime, and enough
VRAM. If the team later containerizes inference, mount each existing model file
read-only instead of copying it into an image.

## Update checklist

When a component changes:

1. Update its status, boundary, implementation location, and completion
   evidence in this catalog.
2. Add or update its interface contract before another component depends on
   it.
3. Record new environment variables in `.env.example` without secrets or
   machine-specific paths.
4. Record model/runtime provenance when inference behavior changes.
5. Add a runbook check and a failure-path test.
6. Decide whether the component still belongs in-process using the hardware,
   lifecycle, security, and scaling criteria at the top of this document.

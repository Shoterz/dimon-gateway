# Dimon public live MVP design

## Status

Implemented MVP baseline. The gateway, session isolation, voice bridge, and
Kokoro result-audio path now live under `apps/dimon_live/`; deployment still
requires the operator to configure Cloudflare Access and the two HTTPS tunnels.

## Goal

Expose the working Dimon synthetic banking flow through one FastAPI application
that can be opened at both `dimon.stocksuite.app` and a temporary HTTPS ngrok
URL. A visitor can type or speak a request, see the exact transcript and
receipt, approve the stored receipt, observe an isolated SQLite ledger change,
and hear a Kokoro response. The expensive Qwen, Nemotron, and Kokoro runtimes
remain shared single-instance local services.

## Scope

The first public MVP includes:

- the existing Dimon ledger interface and receipt flow;
- typed text fallback;
- browser microphone capture with push-to-talk fallback and automatic
  endpointing after conversational silence;
- Jarvis-shaped Nemotron streaming (audio is forwarded while the user speaks)
  with partial and final transcript delivery;
- the existing Qwen proposal path and deterministic Python gateway;
- Kokoro speech generated only from a committed gateway result, off the ledger
  response path so a slow TTS service cannot delay balance updates;
- one active voice session globally;
- a separate SQLite file for every visitor session;
- a persistent owner ledger selected only after external Cloudflare Access
  authentication;
- two HTTPS ingress routes forwarding to the same local Dimon port;
- rate, size, duration, and concurrency limits suitable for a small friend
  demo.

The MVP does not include real banking, PayNow, real KYC, real biometrics,
public model endpoints, arbitrary shell access, multi-region deployment,
multi-instance inference, or a login form inside Dimon.

## Architecture

```text
https://dimon.stocksuite.app  -- Cloudflare Access/Tunnel --┐
                                                            ├─> Dimon FastAPI :8890
https://<temporary>.ngrok.app -- ngrok HTTPS tunnel -------┘       |
                                                                    |-- session/ledger manager
                                                                    |-- banking policy/receipts
                                                                    |-- Qwen :8080 (loopback)
                                                                    |-- Nemotron :8004 (loopback)
                                                                    `-- Kokoro :3001 (loopback)
```

Dimon owns the browser interface, session selection, SQLite path, banking
policy, receipts, approval, and ledger execution. It calls shared inference
services over their local interfaces. It does not import or serve the Jarvis
browser UI and does not start a second Qwen, Nemotron, or Kokoro process.

The existing GPU Overlord remains responsible for model lifecycle and VRAM
admission. The Dimon launcher checks dependency health and starts only the
Dimon application. The public tunnels forward only to port `8890`.

## Ingress and owner boundary

The application accepts only configured origins from `DIMON_ALLOWED_ORIGINS`.
The initial values are the permanent owner origin and the current ngrok origin.
The ngrok origin is supplied at startup because free ngrok URLs change.

Owner mode is opt-in. When `DIMON_OWNER_ACCESS_AUDIENCE` is configured, the
owner origin must be placed behind Cloudflare Access. Dimon has no login
screen. When the owner route is used, the server accepts the owner ledger only
after validating the trusted Cloudflare Access assertion supplied by the
configured tunnel. If the assertion is missing or invalid, the request fails
closed; it must not silently receive owner data. If the audience is blank, the
stable hostname safely receives a visitor ledger instead, which keeps the demo
usable before Access is configured.

The ngrok route never selects the owner ledger. It creates or resumes only a
visitor session. Host and origin values are checked at the application boundary
and are not accepted as a substitute for the Access assertion.

## Session and ledger isolation

The application creates a random, opaque session identifier and stores it in a
Secure, HttpOnly, SameSite cookie. The client cannot choose a filesystem path,
account ID, or owner/visitor mode.

```text
runtime/ledgers/
├── owner.sqlite3
└── visitors/<opaque-session-id>.sqlite3
```

Each visitor database is seeded from the existing synthetic demo state. All
existing receipt, clarification, idempotency, and ledger behavior runs against
that session's database. Reset replaces only the current visitor database.

Visitor sessions expire after 60 minutes of inactivity in the MVP. A cleanup
job removes expired visitor database and audio files. The server caps active
visitor sessions and rejects new sessions when the configured cap is reached.

The owner database is persistent but remains synthetic. It is never returned by
the ngrok route and is not deleted by a visitor reset.

## Text flow

The existing `/api/actions`, receipt approval, batch approval, clarification,
and state routes remain the source of truth. Each request resolves its session
database server-side. Responses include the session-scoped balances, receipt
state, transaction outcome, and a bounded human-readable result.

The browser never submits amount, account ID, receipt facts, database path, or
authorization facts as execution authority. Existing digest-bound approval and
exactly-once behavior remains unchanged.

## Voice flow

The browser adds a push-to-talk control. It captures mono 16 kHz float32 PCM in
an AudioWorklet and sends bounded binary chunks over a Dimon WebSocket. The
browser does not connect directly to Nemotron.

```text
browser AudioWorklet
  → Dimon /api/voice/session (streaming float32 chunks)
  → global voice lease + lightweight pause/endpoint detector
  → Nemotron ws://127.0.0.1:8004/v1/realtime (concurrent send/receive)
  → transcript_partial / transcript_final
  → existing /api/actions path
```

The server owns the Nemotron protocol translation, transcript length limit,
audio byte limit, turn timeout, and cancellation. The final transcript is
displayed before it is submitted to the normal action path. A voice transcript
is not an authorization signal; receipt approval remains explicit.

If another browser owns the lease, the WebSocket returns a stable `VOICE_BUSY`
error and the UI offers typed input. Disconnect, timeout, malformed audio,
oversized audio, and unavailable Nemotron all release the lease and leave the
ledger unchanged.

The endpoint detector is intentionally a small RMS-based MVP port of the Jarvis
timing contract: a short pause is surfaced to the UI and approximately 1.4 s of
silence commits the turn automatically. The user can still release the button
to send immediately. It decides only when to submit audio; it never authorizes
an action. The production Jarvis Silero VAD remains out of scope for this
temporary public demo.

## Kokoro flow

After a committed gateway result, Dimon constructs a short spoken summary from
server-owned result fields. It calls the private Kokoro HTTP endpoint and stores
the bounded WAV response in the current session's ephemeral audio directory.
The browser receives an opaque audio response identifier and can fetch the WAV
only through Dimon. Failed or blocked actions may receive a spoken explanation;
Kokoro never receives hidden model reasoning or raw account data.

Audio files are bounded by size and retention. They are deleted with the visitor
session and are not committed to the repository.

## Failure and safety behavior

- Qwen unavailable: return `MODEL_UNAVAILABLE`; no receipt or ledger mutation.
- Nemotron unavailable or busy: show a voice error; typed input remains usable.
- Kokoro unavailable: preserve the committed ledger result and show a text
  result without audio.
- Owner assertion invalid: reject owner access; never fall back to owner data.
- Visitor cookie invalid or expired: issue a new seeded visitor ledger.
- Session cap reached: reject new visitor sessions with a retry message.
- Request, transcript, audio, or response limit exceeded: fail closed with a
  stable reason code.
- Tunnel disconnect: release the voice lease and leave any pending receipt
  unchanged.
- Public requests never expose `/health`, `/metrics`, model ports, debug logs,
  SQLite paths, or internal service credentials.

## Configuration

The Dimon process reads these deployment values from environment variables:

```text
DIMON_DB_OWNER_PATH
DIMON_VISITOR_DB_DIR
DIMON_VISITOR_TTL_SECONDS=3600
DIMON_MAX_VISITOR_SESSIONS
DIMON_ALLOWED_ORIGINS
DIMON_OWNER_ORIGIN=https://dimon.stocksuite.app
DIMON_OWNER_ACCESS_AUDIENCE
DIMON_NEMOTRON_URL=ws://127.0.0.1:8004/v1/realtime
DIMON_KOKORO_URL=http://127.0.0.1:3001
DIMON_QWEN_API_BASE=http://127.0.0.1:8080/v1
DIMON_MAX_AUDIO_BYTES
DIMON_MAX_UTTERANCE_SECONDS
DIMON_VOICE_PAUSE_SECONDS=0.3
DIMON_VOICE_ENDPOINT_SILENCE_SECONDS=1.4
DIMON_VOICE_SPEECH_THRESHOLD=0.015
DIMON_MAX_ACTIONS_PER_MINUTE
```

The temporary ngrok origin is supplied when the tunnel starts. No model URL or
credential is sent to the browser.

## Verification evidence

Before calling the MVP ready to share, tests and a local rehearsal must prove:

1. Two independent visitor cookies produce two independent seeded SQLite files.
2. A transfer in visitor A does not change visitor B or the owner ledger.
3. Visitor reset does not change any other ledger.
4. Missing/invalid owner authentication never opens `owner.sqlite3`.
5. Two voice WebSockets produce one accepted session and one `VOICE_BUSY` result.
6. Audio limits, malformed PCM, disconnect, timeout, and Nemotron failure release
   the lease with no ledger write.
7. A final transcript enters the same deterministic action path as typed text.
8. Kokoro failure does not roll back an already committed ledger result.
9. Both configured HTTPS origins load the UI and reject an unconfigured Origin.
10. A localhost rehearsal through both tunnels shows one owner flow, one visitor
    flow, one busy voice flow, and one blocked/ambiguous transaction.

## Non-goals after the MVP

Do not add real payment connectivity, real biometric authentication, arbitrary
phonetic authorization, public database access, a second model stack, or a
general-purpose multi-tenant platform until this single-machine public demo has
measured latency, resource use, session isolation, and unauthorized-write
results.

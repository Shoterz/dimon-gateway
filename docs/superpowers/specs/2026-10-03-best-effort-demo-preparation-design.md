# Best-Effort Demo Preparation Design

**Date:** 2026-10-03
**Status:** Proposed for review
**Scope:** Dimon Live local mode only

## Purpose

Dimon's first voice request after an idle period can be slow because Nemotron,
Qwen, and Kokoro may need to acquire GPU capacity and load their workers. The
demo operator needs one deliberate preparation action before presenting, with
clear evidence that the required services are ready.

The implementation will add a local Presenter-view control that warms Dimon's
existing inference providers. It will rely on GPU Overlord's existing hard
admission behavior to evict an idle Whisper worker when capacity is needed. It
will not modify GPU Overlord, stop systemd services, kill processes, or promise
exclusive GPU capacity.

## Current constraints

- The host GPU has approximately 12 GB of VRAM.
- GPU Overlord currently budgets 7,168 MB for the co-resident Qwen profile,
  1,536 MB for Nemotron, 1,280 MB for Kokoro, and 6,144 MB for Whisper.
- Whisper plus the complete Dimon set does not fit within the available VRAM
  and safety reserve.
- GPU Overlord may evict idle managed workers for a hard capacity request. It
  does not evict a worker with an active job.
- A later Whisper hotkey request may evict an idle Dimon worker. This design is
  therefore best effort, not a reservation.
- Nemotron currently has a separate stale-registration fault. Preparation must
  expose that failure; it must not restart or repair system services.

## Goals

- Provide one **Prepare demo** button in the local Presenter view.
- Warm Nemotron, Qwen, and Kokoro through their existing application adapters.
- Refuse to begin when GPU Overlord reports an active Whisper job.
- Show per-stage readiness, failure reason, and measured duration.
- Detect when a later GPU request makes a prepared service cold again.
- Avoid receipts, ledger writes, audit entries, and generated speech artifacts.
- Keep every preparation operation bounded, single-flight, and safe to retry.

## Non-goals

- No guaranteed or exclusive Dimon reservation.
- No change to GPU Overlord or its service profiles.
- No direct eviction, process termination, or `systemctl` invocation from Dimon.
- No automatic repair of stale supervisor registration.
- No remote or public administrative control.
- No change to the production voice authorization boundary.
- No endpoint-silence default change in this feature. Silence thresholds will
  be evaluated separately using recorded and live utterances.

## Operator boundary

Demo preparation is disabled unless all of these conditions hold:

1. `DIMON_INFERENCE_MODE=local`.
2. `DIMON_DEMO_PREPARE_ENABLED=1`.
3. The HTTP `Host` is a loopback host: `127.0.0.1`, `localhost`, or `[::1]`,
   with the configured Dimon port permitted.
4. The browser `Origin`, when present, is the matching loopback origin.

Requests outside this boundary receive `404` so the public application does
not advertise an administrative capability. The endpoint accepts same-origin
JSON `POST` requests only. It returns bounded reason codes rather than paths,
tokens, prompts, transcripts, or upstream exception text.

This is a local hackathon control, not a general production administration
interface. Public tunnel hosts must never pass the operator boundary.

## Components

### Demo preparation coordinator

Create a focused coordinator in `apps/dimon_live/demo_preparation.py`. It owns:

- one process-local preparation task;
- an asynchronous lock preventing concurrent preparations;
- stage states and monotonic durations;
- bounded timeouts;
- the optimistic readiness expiry; and
- sanitized failure mapping.

The coordinator receives provider callables or small protocols rather than
constructing model clients itself. Tests can therefore supply deterministic
fakes without GPU services.

### GPU Overlord status client

Add a bounded, read-only Unix-socket client which sends the existing
`{"op":"status"}` request. The socket path comes from
`DIMON_GPU_OVERLORD_SOCKET`, defaulting to the current per-user Overlord socket.
The client enforces connection, response-size, and response-time limits.

The status client never registers a service, acquires a lease, releases a
lease, or sends an eviction request. Its response is reduced to the service
name, state, active-job count, worker presence, declared budget, observed VRAM,
and total/free GPU memory.

If status cannot be read, preparation fails closed because Dimon cannot prove
that Whisper is idle.

### Application API

Add two local-only routes:

- `POST /api/demo/prepare` starts preparation or returns the existing active
  run. It responds with `202` while work continues.
- `GET /api/demo/prepare` returns the current sanitized snapshot and refreshes
  live provider state where possible.

The snapshot has this conceptual shape:

```json
{
  "state": "IDLE|PREPARING|READY|DEGRADED|FAILED",
  "best_effort": true,
  "started_at": "timestamp or null",
  "completed_at": "timestamp or null",
  "ready_until": "timestamp or null",
  "stages": [
    {
      "name": "capacity|nemotron|qwen|kokoro",
      "state": "PENDING|RUNNING|READY|FAILED",
      "latency_ms": 0,
      "reason_code": "bounded code or null"
    }
  ],
  "warning": "Other GPU requests can make prepared services cold again."
}
```

Repeated button presses while preparation is active return the same run. A new
run may be started after completion or failure.

## Preparation flow

Preparation is sequential so the GPU broker observes stable service state.

1. **Capacity preflight**
   - Read GPU Overlord status.
   - If Whisper reports active jobs, or is starting or draining, fail with
     `WHISPER_BUSY` without asking any model to load.
   - An idle or ready Whisper worker is allowed. GPU Overlord may evict it later
     through its existing hard-admission path.

2. **Nemotron warm-up**
   - Acquire the existing Nemotron lifecycle once.
   - Open and close a recognizer stream to verify the worker protocol, without
     sending audio or creating a transcript.
   - Release the lifecycle normally so its existing idle timer remains
     authoritative.
   - Map lifecycle and protocol failures to stable codes such as
     `NEMOTRON_UNAVAILABLE`.

3. **Qwen warm-up**
   - Run the existing constrained extractor with one repository-owned benign
     transfer sentence.
   - Validate the returned intent, then discard it.
   - Do not call `ActionService`, create a receipt, write audit data, or touch a
     session database.

4. **Kokoro warm-up**
   - Synthesize one short fixed readiness phrase through the selected local
     synthesizer.
   - Validate the bounded WAV response in memory and discard it.
   - Do not enqueue or persist an audio artifact.

5. **Ready state**
   - Mark preparation ready only when every stage succeeds.
   - Set `ready_until` to 60 seconds after completion. This is shorter than the
     current 75-second Nemotron idle window.
   - Continue labeling the state as best effort.

If a stage fails, later stages do not run. Resources acquired by the failed
stage are released in `finally` blocks. Preparation never retries endlessly.

## Detecting lost warmth

The Presenter view polls preparation status while visible. The backend compares
the optimistic ready state with current GPU Overlord worker state. If a required
worker disappears, enters an error state, or the 60-second window expires, the
state becomes `DEGRADED`.

A Whisper hotkey request is allowed to use normal GPU Overlord behavior. It may
evict idle Qwen or another Dimon worker. The Presenter view then stops claiming
that the demo is warm and offers **Prepare again**. Dimon does not block, delay,
or modify the Whisper request.

## Presenter experience

Add a compact preparation control to the existing Presenter tools area:

- **Prepare demo** while idle, degraded, or failed;
- **Preparing…** with the current stage while active;
- **Prepared · best effort** with the remaining warm window when ready; and
- a concise stable reason when preparation fails.

The public Presenter page initially hides the control. It becomes visible only
after the local status endpoint confirms that demo preparation is enabled.
Screen-reader status updates use an `aria-live` region. Scenario buttons remain
usable after a preparation failure.

## Timeouts and errors

Each stage has an explicit deadline. Initial limits are:

- GPU status: 2 seconds;
- Nemotron lifecycle and readiness: 120 seconds;
- Qwen extraction: 120 seconds; and
- Kokoro synthesis: 30 seconds.

The overall run cannot exceed the sum of its stage deadlines plus small cleanup
allowances. Browser polling may stop without cancelling server cleanup.

Stable client-visible failures include:

- `DEMO_PREPARATION_DISABLED`
- `GPU_STATUS_UNAVAILABLE`
- `WHISPER_BUSY`
- `NEMOTRON_UNAVAILABLE`
- `QWEN_UNAVAILABLE`
- `KOKORO_UNAVAILABLE`
- `PREPARATION_TIMEOUT`

Full internal exceptions may be logged locally with correlation identifiers,
but logs must not contain audio, transcripts, prompts, tokens, or secrets.

## Endpoint-silence experiment

The preparation feature preserves the current 1.4-second default. A separate
evaluation will compare 1.4, 1.0, 0.8, 0.6, and 0.4 seconds using identical
recordings plus live hesitant and Singlish requests. It will record premature
cutoffs, critical-field transcription errors, endpoint delay, transcription
finalization time, Qwen time, and total voice-to-result time.

The default changes only after the lowest safe value is supported by evidence.
Manual microphone stop remains the immediate-commit path.

## Testing

### Coordinator tests

- Stages execute strictly in capacity, Nemotron, Qwen, Kokoro order.
- An active Whisper job fails before any warm-up call.
- An idle Whisper worker is not commanded directly.
- Concurrent starts share one preparation run.
- Each timeout maps to its stable reason code.
- Nemotron is released after success, failure, and cancellation.
- Qwen warm-up creates no database, receipt, or audit mutation.
- Kokoro output is discarded rather than persisted.
- A missing prepared worker changes `READY` to `DEGRADED`.

### API boundary tests

- The feature is absent outside local inference mode.
- The feature is absent when its environment flag is disabled.
- Public hosts and non-loopback origins receive `404`.
- Loopback `POST` starts one bounded run.
- Status responses contain no paths, tokens, prompts, transcripts, or raw
  exception messages.

### Browser contract tests

- The control stays hidden when the endpoint is unavailable.
- Progress and per-stage results render correctly.
- Repeated clicks do not start duplicate runs.
- A degraded state offers preparation again.
- Existing Presenter scenario buttons continue to work.

### Live verification

On the GPU host:

1. Start with an idle warm Whisper worker.
2. Press **Prepare demo** and record GPU Overlord state and stage timings.
3. Confirm Whisper's worker unloads only when capacity requires it.
4. Confirm Nemotron, Qwen, and Kokoro are ready without ledger mutation.
5. Run one voice request and record endpoint, transcription, and model latency.
6. Press the Whisper hotkey after preparation and confirm normal best-effort
   eviction behavior.
7. Confirm the Presenter state becomes degraded if a Dimon worker was evicted.

## Repository and handoff

All product changes remain in the Dimon repository and the current feature
branch. No GPU Overlord source or systemd unit is changed by this feature.

The pull-request or handoff summary must list:

- the new local environment flag and socket setting;
- routes and UI files changed;
- measured warm-up and voice latency;
- whether Whisper was evicted during live verification;
- the best-effort limitation; and
- the separate Nemotron registration issue if it remains unresolved.

# Nemotron Lifecycle Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace anonymous Nemotron session counting with bounded, per-stream leases that return to zero after success, failure, cancellation, disconnect, or client death.

**Architecture:** The Nemotron supervisor owns a capacity-one registry keyed by opaque lease IDs and reaps leases that miss heartbeats or exceed a hard lifetime. Dimon clients receive an independent lease handle from every acquisition, and each gateway stream owns and idempotently closes exactly one such handle. Deployment rejects the legacy anonymous protocol, resets the inherited corrupt count with one controlled restart, and verifies zero idle leases with both unit and live soak checks.

**Tech Stack:** Python 3.12, asyncio, Unix-domain newline-delimited JSON control protocol, FastAPI/Starlette WebSockets, unittest/pytest, GPU Overlord.

**Spec:** `docs/superpowers/specs/2026-10-14-dimon-resident-voice-pipeline-design.md`

## Global Constraints

- Nemotron capacity is one live stream until a separate concurrency benchmark approves a higher value.
- Lease heartbeat interval is 10 seconds, missed-heartbeat timeout is 30 seconds, hard lease lifetime is 90 seconds, and supervisor reap interval is 5 seconds.
- `active_sessions` and GPU Overlord `active_jobs` are always derived from the live lease registry; no anonymous counter remains.
- Acquire and release are idempotent for the same lease ID; a late or duplicate release cannot affect another lease.
- Lease IDs are lowercase 32-character hexadecimal UUID values and are never exposed in public status, browser messages, or ordinary logs.
- No raw audio, transcript, payment data, or voice identity data enters lifecycle status or logging.
- The existing corrupt live count is cleared only by a controlled `jarvis-nemotron.service` restart after both sides of protocol v2 are installed.
- Preserve all unrelated dirty workspace changes; implementation uses an isolated worktree for this repository.
- The external supervisor directory is not a Git repository. Before editing it, create `<gpu-manager>/backups/pre-nemotron-lease-v2-20261014/` and preserve the original supervisor and test files with SHA-256 values `6e55d0cfa27397a7740c730a4ff5a75f2cd765cad3f717848ec1aa20aa2d73d9` and `9e06a9b34f6819f96462afd3409e044cfe8e22ba1e17105569e887a320aed27f` respectively.

## Review Focus

- Missing, malformed, or oversized lease IDs must be rejected without starting the worker or changing `active_jobs`; Task 1 pins this with protocol-validation tests.
- A duplicate acquire, heartbeat, release, or late release must never double-count or release a different session; Task 1 pins every idempotency case.
- Cancellation during client acquire, stream open, push, finish, and close must leave zero client and supervisor leases; Tasks 2 and 3 inject cancellation at each boundary.
- A live but hung client must not hold the GPU forever: missed-heartbeat and hard-age expiry both reap it and schedule idle shutdown; Task 1 uses a fake clock for both paths.
- Worker crash, supervisor shutdown, and gateway shutdown must publish `active_jobs == 0` even if leases existed; Tasks 1 and 3 exercise all three termination paths.

---

### Task 1: Supervisor Lease-ID Registry and Orphan Reaper

**Files:**
- Backup: `<gpu-manager>/backups/pre-nemotron-lease-v2-20261014/overlord_nemotron_supervisor.py`
- Backup: `<gpu-manager>/backups/pre-nemotron-lease-v2-20261014/test_overlord_nemotron.py`
- Modify: `<gpu-manager>/overlord_nemotron_supervisor.py`
- Modify: `<gpu-manager>/tests/test_overlord_nemotron.py`

**Interfaces:**
- Consumes: `CapacityBroker.set_active_jobs(active_jobs: int)` and the existing worker start/stop lifecycle.
- Produces: `NemotronController.acquire_session(lease_id: str) -> dict[str, object]`, `heartbeat_session(lease_id: str) -> dict[str, object]`, `release_session(lease_id: str, *, disconnected: bool = False) -> dict[str, object]`, and status fields `active_sessions`, `capacity`, `oldest_lease_age_seconds`, `lease_timeout_seconds`, and `max_lease_seconds`.
- Produces control operations: `{"op":"acquire","lease_id":"<32 hex>"}`, `{"op":"heartbeat","lease_id":"<32 hex>"}`, and `{"op":"release","lease_id":"<32 hex>","disconnected":false}`.

- [ ] **Step 1: Preserve and verify the two non-versioned source files**

Create the fixed backup directory only if absent, copy the supervisor and its test into it, and run `sha256sum` on the backup copies.

Expected: checksums match the two values in Global Constraints; abort rather than overwrite if the backup path already contains different files.

- [ ] **Step 2: Write failing registry and protocol tests**

Add tests named:

```python
test_same_lease_acquire_is_idempotent
test_second_lease_is_rejected_at_capacity_one
test_heartbeat_refreshes_only_its_lease
test_duplicate_and_late_release_are_no_ops
test_missing_and_malformed_lease_ids_are_rejected_without_starting
test_missed_heartbeat_reaps_lease_and_publishes_zero
test_hard_age_reaps_even_a_heartbeat_renewed_lease
test_worker_crash_and_shutdown_clear_leases_and_publish_zero
test_one_hundred_acquire_release_cycles_finish_at_zero
```

Assert that `active_sessions == len(live registry)`, broker `active_jobs` events never exceed `1`, duplicate operations keep the count unchanged, invalid requests leave `starts == []`, and the final broker event in every reset path is `("active_jobs", 0)`.

- [ ] **Step 3: Run the supervisor tests and confirm the legacy counter fails them**

Run: `cd <gpu-manager> && python3 -m unittest tests.test_overlord_nemotron -v`

Expected: new tests fail because lease-ID methods, capacity enforcement, status fields, and reaping do not exist.

- [ ] **Step 4: Implement `_SessionLease` and lease-aware controller methods**

In `overlord_nemotron_supervisor.py`, add:

```python
@dataclass
class _SessionLease:
    created_at: float
    last_seen_at: float

async def acquire_session(self, lease_id: str) -> dict[str, object]: ...
async def heartbeat_session(self, lease_id: str) -> dict[str, object]: ...
async def release_session(
    self, lease_id: str, *, disconnected: bool = False
) -> dict[str, object]: ...
async def _reap_expired_sessions(self) -> int: ...
```

Replace `_active_sessions` with `_session_leases: dict[str, _SessionLease]`. Validate IDs before `ensure_started()`. Enforce `max_sessions=1`; reacquiring the same ID refreshes it without changing the count. Publish `len(_session_leases)` to the broker after every actual registry change.

- [ ] **Step 5: Add bounded reaper lifecycle and status**

Extend `NemotronController.__init__` with keyword parameters `max_sessions=1`, `heartbeat_timeout_seconds=30.0`, `max_lease_seconds=90.0`, and `reaper_interval_seconds=5.0`. Start one reaper task with the first accepted lease, reap when either age bound is exceeded, schedule the disconnect idle timeout after the final reap, and cancel the reaper on shutdown.

Expose only aggregate ages/counts in `_status_locked()`; never expose lease IDs. On process crash and shutdown, clear the registry and call `broker.set_active_jobs(0)` before releasing the GPU lease.

- [ ] **Step 6: Make protocol v2 mandatory and configure exact defaults**

Update `handle_request()` so `acquire`, `heartbeat`, and `release` require a valid `lease_id`. Keep `status` and `shutdown` unchanged. In `_run_service()`, read `NEMOTRON_MAX_SESSIONS` default `1`, `NEMOTRON_LEASE_HEARTBEAT_TIMEOUT_SECONDS` default `30`, `NEMOTRON_LEASE_MAX_SECONDS` default `90`, and `NEMOTRON_LEASE_REAPER_INTERVAL_SECONDS` default `5`.

- [ ] **Step 7: Run supervisor tests**

Run: `cd <gpu-manager> && python3 -m unittest tests.test_overlord_nemotron -v`

Expected: all Nemotron supervisor tests pass and the 100-cycle test ends with `active_sessions == 0`.

### Task 2: Independent Client Lease Handles

**Files:**
- Modify: `apps/dimon_live/inference.py`
- Modify: `apps/dimon_live/voice.py`
- Modify: `apps/dimon_live/demo_preparation.py`
- Modify: `apps/dimon_live/fixture_inference.py`
- Modify: `apps/dimon_live/tests/test_voice.py`
- Modify: `apps/dimon_live/tests/test_demo_preparation.py`
- Modify: `apps/dimon_live/tests/test_fixture_voice_speech.py`
- Modify: `apps/dimon_live/tests/test_provider_contract.py`

**Interfaces:**
- Consumes: Task 1 protocol v2 control operations.
- Produces: `VoiceLifecycle.acquire() -> VoiceLifecycleLease`, `VoiceLifecycleLease.release(*, disconnected: bool = False) -> None`, `NemotronLifecycleClient.acquire() -> NemotronLifecycleLease`, and `NemotronLifecycleClient.status() -> dict[str, object]`.
- Produces: every local voice bridge and preparation operation stores and releases its own returned handle.

- [ ] **Step 1: Write failing client-handle tests**

Add tests named:

```python
test_lifecycle_acquire_returns_distinct_lease_handles
test_lease_heartbeats_with_its_own_id
test_lease_release_is_idempotent_and_cancels_heartbeat
test_release_failure_is_bounded_and_orphan_can_expire
test_cancel_during_acquire_or_release_leaves_no_client_task
test_bridge_releases_the_handle_returned_by_its_acquire
test_preparation_releases_its_handle_on_success_failure_and_cancel
```

Patch `_request` with a recording async fake. Assert two acquires use two distinct valid IDs, each release sends its own ID exactly once, heartbeat tasks are absent after close, and no client `_acquired` boolean exists.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `uv run pytest apps/dimon_live/tests/test_voice.py apps/dimon_live/tests/test_demo_preparation.py apps/dimon_live/tests/test_fixture_voice_speech.py apps/dimon_live/tests/test_provider_contract.py -q`

Expected: new handle assertions fail against the shared boolean API.

- [ ] **Step 3: Define the lifecycle protocols**

In `apps/dimon_live/inference.py`, replace client-level `release()` with:

```python
class VoiceLifecycleLease(Protocol):
    async def release(self, *, disconnected: bool = False) -> None: ...

class VoiceLifecycle(Protocol):
    async def acquire(self) -> VoiceLifecycleLease: ...
```

- [ ] **Step 4: Implement `NemotronLifecycleLease`**

In `apps/dimon_live/voice.py`, add `NemotronLifecycleLease(client, lease_id, heartbeat_seconds=10.0)`. It owns one heartbeat task, sends `heartbeat` with its ID, and marks itself released before attempting its single release request. `release()` cancels and awaits the heartbeat task, then sends the ID-bearing release; repeated calls return without I/O.

Make `NemotronLifecycleClient` stateless across sessions: remove `_acquired`, generate `uuid4().hex` inside `acquire()`, send the ID-bearing acquire, and return a new started lease handle. Add `status()` for sanitized operational verification.

- [ ] **Step 5: Migrate local consumers and fixtures**

In `VoiceBridge.handle()`, replace `lifecycle_acquired: bool` with `lifecycle_lease: VoiceLifecycleLease | None` and release that handle in `finally`. In `DemoPreparationCoordinator._warm_nemotron()`, store and release the returned handle. Update `FixtureVoiceLifecycle` and test doubles so `acquire()` returns a fixture lease object with idempotent `release()`.

- [ ] **Step 6: Run focused and contract tests**

Run: `uv run pytest apps/dimon_live/tests/test_voice.py apps/dimon_live/tests/test_demo_preparation.py apps/dimon_live/tests/test_fixture_voice_speech.py apps/dimon_live/tests/test_provider_contract.py apps/dimon_live/tests/test_inference_modes.py -q`

Expected: all pass; cancellation tests report no pending heartbeat tasks.

- [ ] **Step 7: Commit the project-side lifecycle API**

```bash
git add apps/dimon_live/inference.py apps/dimon_live/voice.py apps/dimon_live/demo_preparation.py apps/dimon_live/fixture_inference.py apps/dimon_live/tests/test_voice.py apps/dimon_live/tests/test_demo_preparation.py apps/dimon_live/tests/test_fixture_voice_speech.py apps/dimon_live/tests/test_provider_contract.py
git commit -m "fix: isolate Nemotron lifecycle leases"
```

### Task 3: Gateway Per-Stream Ownership and Capacity-One Guard

**Files:**
- Modify: `services/inference_gateway/transcription.py`
- Modify: `services/inference_gateway/app.py`
- Modify: `services/inference_gateway/tests/test_transcription_endpoint.py`
- Modify: `services/inference_gateway/tests/test_auth.py`
- Modify: `services/inference_gateway/tests/test_preparation.py`

**Interfaces:**
- Consumes: `NemotronLifecycleClient.acquire() -> NemotronLifecycleLease` from Task 2.
- Produces: `_NemotronPcm16Stream(stream, lifecycle_lease)` with idempotent `close()`, and a gateway-wide `VoiceLease(1)` admission limit.

- [ ] **Step 1: Write failing stream-ownership and cancellation tests**

Add tests named:

```python
test_each_nemotron_stream_owns_a_distinct_lifecycle_lease
test_stream_close_releases_exactly_once
test_stream_push_and_finish_failure_release_with_disconnect_hint
test_cancel_during_nemotron_open_releases_acquired_handle
test_cancel_during_gateway_stream_releases_handle_and_capacity
test_gateway_handler_shutdown_cancellation_releases_handle
test_one_hundred_open_close_and_cancel_cycles_leave_zero_leases
test_default_gateway_transcription_capacity_is_one
```

Use a fake lifecycle whose `acquire()` returns numbered handle objects. Assert every stream closes the matching handle, no handle receives two release calls, the 100-cycle set is empty, and the gateway can accept a new session after every injected cancellation.

- [ ] **Step 2: Run gateway tests and confirm failure**

Run: `uv run pytest services/inference_gateway/tests/test_transcription_endpoint.py services/inference_gateway/tests/test_auth.py services/inference_gateway/tests/test_preparation.py -q`

Expected: ownership tests expose the shared lifecycle object and the capacity test sees `4` instead of `1`.

- [ ] **Step 3: Move lease ownership into `_NemotronPcm16Stream`**

In `NemotronPcm16Recognizer.open_stream()`, store `lifecycle_lease = await self.lifecycle.acquire()`, release that exact handle if upstream opening fails or is cancelled, and pass it to the returned stream. `_NemotronPcm16Stream.close()` sets a `closed` guard before closing upstream and releases its handle in `finally` with the disconnect hint.

- [ ] **Step 4: Enforce one admitted transcription session**

In `create_gateway_app()`, change the default transcription lease to `VoiceLease(1)`. Preserve explicit injected leases in tests and callers. Update the configuration assertion in `test_auth.py`.

- [ ] **Step 5: Run gateway and hands-free regression tests**

Run: `uv run pytest services/inference_gateway/tests/test_transcription_endpoint.py services/inference_gateway/tests/test_auth.py services/inference_gateway/tests/test_preparation.py apps/dimon_live/tests/test_hands_free.py apps/dimon_live/tests/test_remote_inference.py -q`

Expected: all pass, including 100-cycle zero-leak coverage and existing authoritative Faster-Whisper behavior.

- [ ] **Step 6: Commit the gateway ownership fix**

```bash
git add services/inference_gateway/transcription.py services/inference_gateway/app.py services/inference_gateway/tests/test_transcription_endpoint.py services/inference_gateway/tests/test_auth.py services/inference_gateway/tests/test_preparation.py
git commit -m "fix: bind Nemotron leases to streams"
```

### Task 4: Controlled Cutover and Zero-Leak Acceptance

**Files:**
- Create: `scripts/verify_nemotron_lifecycle.py`
- Create: `docs/runbooks/nemotron-lifecycle-v2.md`
- Test: `apps/dimon_live/tests/test_nemotron_lifecycle_verifier.py`

**Interfaces:**
- Consumes: `NemotronLifecycleClient.acquire()` and `status()` from Task 2 and the aggregate supervisor fields from Task 1.
- Produces: `python scripts/verify_nemotron_lifecycle.py --socket PATH --cycles 100` returning exit code zero only when every cycle releases and final `active_sessions == 0`.

- [ ] **Step 1: Write the failing verifier tests**

Cover a 100-cycle success, non-zero final count, interrupted cycle cleanup, malformed status, and lease acquire/release failure. Assert output contains only cycle counts and aggregate status—not lease IDs or upstream details.

- [ ] **Step 2: Run the verifier tests and confirm failure**

Run: `uv run pytest apps/dimon_live/tests/test_nemotron_lifecycle_verifier.py -q`

Expected: FAIL because the verifier module does not exist.

- [ ] **Step 3: Implement the bounded verifier**

Add:

```python
async def verify_lifecycle(
    client: NemotronLifecycleClient, *, cycles: int = 100
) -> dict[str, int]: ...
```

Validate `1 <= cycles <= 1000`; use `try/finally` around each acquired handle; poll sanitized status after each release and at the end; fail if a count does not return to zero within a bounded five-second wait.

- [ ] **Step 4: Write the cutover and rollback runbook**

Document this exact order: verify backups; install both code sides; run all tests; restart `jarvis-nemotron.service`; restart `dimon-inference-gateway.service`; confirm idle zero; run the 100-cycle verifier; open and cancel a real transcription stream; confirm zero again; then run one real warm-up. Rollback may restore the project commit and external backup only before protocol-v2 traffic resumes; never restore anonymous counting while new clients remain active.

- [ ] **Step 5: Run all lifecycle and regression tests**

Run:

```bash
cd <gpu-manager> && python3 -m unittest tests.test_overlord_nemotron -v
cd <repo> && uv run pytest apps/dimon_live/tests/test_voice.py apps/dimon_live/tests/test_demo_preparation.py apps/dimon_live/tests/test_fixture_voice_speech.py apps/dimon_live/tests/test_provider_contract.py apps/dimon_live/tests/test_hands_free.py apps/dimon_live/tests/test_remote_inference.py apps/dimon_live/tests/test_nemotron_lifecycle_verifier.py services/inference_gateway/tests/test_transcription_endpoint.py services/inference_gateway/tests/test_auth.py services/inference_gateway/tests/test_preparation.py -q
```

Expected: all pass with no pending-task warnings.

- [ ] **Step 6: Commit the verifier and runbook**

```bash
git add scripts/verify_nemotron_lifecycle.py docs/runbooks/nemotron-lifecycle-v2.md apps/dimon_live/tests/test_nemotron_lifecycle_verifier.py
git commit -m "test: verify zero Nemotron lease leaks"
```

- [ ] **Step 7: Perform the controlled restart and live acceptance**

Restart the two user services in runbook order, run the verifier for 100 cycles, then exercise successful finalization, explicit cancellation, browser disconnect, and an injected upstream failure. After each case, query status and GPU Overlord.

Expected: `active_sessions == 0` and Nemotron `active_jobs == 0` after every completed case; neither value grows across the soak; the worker may remain `ready` during its warm retention window but never remains falsely `busy`.

- [ ] **Step 8: Record measured acceptance evidence**

Add the test totals, restart timestamp, pre/post aggregate counts, and any classified failures to the runbook's acceptance record. Do not include transcripts, audio, lease IDs, secrets, or payment content.

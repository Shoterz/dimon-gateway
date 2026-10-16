# Best-Effort Demo Preparation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local Presenter control that safely checks GPU capacity, warms Nemotron, Qwen, and Kokoro in order, and reports when that best-effort readiness is lost.

**Architecture:** Dimon reads GPU Overlord's existing status protocol without acquiring or evicting anything directly. A process-local coordinator performs one sequential, time-bounded warm-up run, while loopback-only FastAPI routes expose sanitized progress to a small Presenter UI module.

**Tech Stack:** Python 3.12, asyncio, FastAPI, Pydantic-free dataclasses for internal state, browser JavaScript modules, Node-based client tests, unittest/pytest through `uv`.

**Spec:** `docs/superpowers/specs/2026-10-03-best-effort-demo-preparation-design.md`

## Global Constraints

- Product changes remain in the Dimon repository and `test/backend-edits` branch; do not modify GPU Overlord source or systemd units.
- Enable preparation only when `DIMON_INFERENCE_MODE=local`, `DIMON_DEMO_PREPARE_ENABLED=1`, and request host/origin are loopback.
- Never invoke `systemctl`, kill a process, acquire an Overlord lease, release a lease, or send an eviction request from the new code.
- Warm in strict order: capacity, Nemotron, Qwen, Kokoro.
- Use stage deadlines of 2, 120, 120, and 30 seconds respectively.
- Never create a receipt, database mutation, audit entry, persisted WAV, transcript, or speech artifact during preparation.
- Responses and logs must not expose paths, tokens, prompts, transcripts, audio, or raw upstream exceptions.
- Preserve the 1.4-second voice endpoint default; threshold evaluation is separate.
- Mark readiness as best effort and expire it after 60 seconds.

## Review Focus

- **Truncated, oversized, or malformed Overlord status:** reject it as `GPU_STATUS_UNAVAILABLE`; Task 1 tests all three cases.
- **Whisper changes state between preflight and warm-up:** never command Whisper directly, and report later worker loss as degraded; Tasks 2 and 3 pin the behavior.
- **Duplicate preparation clicks and task cancellation:** share one run and release Nemotron in every exit path; Task 2 tests concurrency and cleanup.
- **Spoofed local access through public host/origin headers:** return `404` unless both host and any supplied origin are matching loopback values; Task 3 tests mismatches and IPv6/port parsing.
- **Prepared state becomes stale after hotkey GPU use:** poll live worker status, move to `DEGRADED`, and offer preparation again; Tasks 2 and 4 test backend and UI behavior.

---

## File Structure

- `apps/dimon_live/gpu_overlord_status.py` — strict read-only parser and bounded Unix-socket status client.
- `apps/dimon_live/demo_preparation.py` — preparation state, sequential coordinator, timeouts, failure mapping, and warm-state refresh.
- `apps/dimon_live/app.py` — dependency wiring, local operator boundary, preparation routes, and static module route.
- `apps/dimon_live/static/demo_preparation.mjs` — pure API/formatting helpers for the Presenter control.
- `apps/dimon_live/static/presenter.js` — DOM wiring and polling only.
- `apps/dimon_live/static/presenter.html` — hidden control and live status container.
- `apps/dimon_live/tests/test_gpu_overlord_status.py` — protocol validation tests.
- `apps/dimon_live/tests/test_demo_preparation.py` — coordinator ordering, timeout, concurrency, cleanup, and degradation tests.
- `apps/dimon_live/tests/test_demo_preparation_api.py` — route gating and response-boundary tests.
- `apps/dimon_live/tests/test_ui_client.py` — Node tests for Presenter preparation state and requests.
- `apps/dimon_live/.env.example` and `apps/dimon_live/README.md` — local feature configuration and best-effort limitation.

### Task 1: Strict read-only GPU Overlord status client

**Files:**
- Create: `apps/dimon_live/gpu_overlord_status.py`
- Create: `apps/dimon_live/tests/test_gpu_overlord_status.py`

**Interfaces:**
- Consumes: Existing newline-delimited `{"op":"status"}` Unix-socket response from GPU Overlord.
- Produces: `GpuServiceStatus`, `GpuStatusSnapshot`, `GpuStatusError`, and `GpuOverlordStatusClient.read() -> Awaitable[GpuStatusSnapshot]` for Task 2.

- [ ] **Step 1: Write failing parser tests**

Add tests named:

- `test_status_client_returns_only_bounded_service_and_gpu_fields`
- `test_status_client_rejects_malformed_or_missing_response_fields`
- `test_status_client_rejects_oversized_and_truncated_lines`
- `test_status_client_maps_connect_and_timeout_failures`

Assert that valid input becomes immutable typed records containing only service, state, active jobs, worker presence, budget, observed VRAM, and total/free/safety-reserve GPU memory. Assert every invalid transport or envelope raises `GpuStatusError("GPU_STATUS_UNAVAILABLE")` without copying raw response data into its public message.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run python -m unittest apps.dimon_live.tests.test_gpu_overlord_status -v`

Expected: FAIL because `apps.dimon_live.gpu_overlord_status` does not exist.

- [ ] **Step 3: Implement typed status parsing**

Create:

```python
@dataclass(frozen=True)
class GpuServiceStatus:
    service: str
    state: str
    active_jobs: int
    worker_present: bool
    budget_mb: int
    observed_vram_mb: int

@dataclass(frozen=True)
class GpuStatusSnapshot:
    total_mb: int
    free_mb: int
    safety_reserve_mb: int
    services: tuple[GpuServiceStatus, ...]

    def service(self, name: str) -> GpuServiceStatus | None: ...

class GpuStatusError(RuntimeError):
    reason_code = "GPU_STATUS_UNAVAILABLE"

def parse_gpu_status(response: object) -> GpuStatusSnapshot: ...
```

Accept only GPU Overlord's declared states and non-negative integer fields. Treat booleans as invalid integers. Ignore unneeded keys rather than storing them.

- [ ] **Step 4: Implement the bounded Unix-socket client**

Create:

```python
class GpuOverlordStatusClient:
    def __init__(
        self,
        socket_path: Path,
        *,
        timeout_seconds: float = 2.0,
        max_response_bytes: int = 65_536,
    ): ...

    async def read(self) -> GpuStatusSnapshot: ...
```

Send exactly one newline-delimited status request. Bound connection, drain, and read operations with the same two-second deadline, require one complete line within 65,536 bytes, close the writer in `finally`, then call `parse_gpu_status`.

- [ ] **Step 5: Run focused tests**

Run: `uv run python -m unittest apps.dimon_live.tests.test_gpu_overlord_status -v`

Expected: all tests PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add apps/dimon_live/gpu_overlord_status.py apps/dimon_live/tests/test_gpu_overlord_status.py
git commit -m "feat: read bounded GPU Overlord status"
```

### Task 2: Single-flight preparation coordinator

**Files:**
- Create: `apps/dimon_live/demo_preparation.py`
- Create: `apps/dimon_live/tests/test_demo_preparation.py`

**Interfaces:**
- Consumes: `GpuOverlordStatusClient.read()`, existing extractor `extract(text)`, recognizer `open_stream(on_partial)`, optional lifecycle `acquire()/release()`, and async synthesizer `__call__(text)`.
- Produces: `DemoPreparationCoordinator.start()`, `.status()`, `.wait()`, and JSON-safe snapshots used by Task 3.

- [ ] **Step 1: Write failing state and ordering tests**

Add tests named:

- `test_preparation_runs_capacity_nemotron_qwen_kokoro_in_order`
- `test_active_start_returns_the_same_run_without_duplicate_calls`
- `test_whisper_busy_fails_before_warming_any_provider`
- `test_missing_whisper_record_allows_preparation`

Use fakes that append exact operation names. Assert the successful sequence is `status`, `lifecycle.acquire`, `recognizer.open`, `stream.close`, `lifecycle.release`, `extractor.extract`, `synthesizer`, and that the result is `READY`, `best_effort=true`, with a 60-second readiness window.

- [ ] **Step 2: Run the first coordinator tests and verify failure**

Run: `uv run python -m unittest apps.dimon_live.tests.test_demo_preparation -v`

Expected: FAIL because `apps.dimon_live.demo_preparation` does not exist.

- [ ] **Step 3: Implement preparation state and single-flight execution**

Create:

```python
StageName = Literal["capacity", "nemotron", "qwen", "kokoro"]
StageState = Literal["PENDING", "RUNNING", "READY", "FAILED"]
PreparationState = Literal["IDLE", "PREPARING", "READY", "DEGRADED", "FAILED"]

class DemoPreparationCoordinator:
    async def start(self) -> dict[str, object]: ...
    async def status(self) -> dict[str, object]: ...
    async def wait(self) -> dict[str, object]: ...
```

Use one `asyncio.Lock` and one owned task. `start()` creates a run only when no run is active. Snapshots expose ISO timestamps, ordered stages, rounded non-negative `latency_ms`, stable reason codes, and the fixed best-effort warning.

- [ ] **Step 4: Implement sequential warm-up**

Use `asyncio.wait_for` for each exact deadline. Run synchronous `extractor.extract("Send John Smith S$5 from savings")` through `asyncio.to_thread`. Open and close Nemotron without audio or transcript callbacks. Call the synthesizer with `"Dimon is ready."`; accept only an in-memory RIFF/WAVE payload within the configured WAV limit, then discard it.

- [ ] **Step 5: Write failing timeout, cleanup, mutation, and degradation tests**

Add tests named:

- `test_each_stage_timeout_maps_to_its_stable_reason_code`
- `test_nemotron_releases_after_success_failure_and_cancellation`
- `test_qwen_and_kokoro_warmup_do_not_touch_database_or_files`
- `test_missing_prepared_worker_changes_ready_to_degraded`
- `test_raw_upstream_exception_text_is_not_exposed`

Use temporary directories and assert they remain empty. After a ready run, return a status snapshot with Qwen's worker missing and assert the public state becomes `DEGRADED`.

- [ ] **Step 6: Implement cleanup, error mapping, and live refresh**

Map capacity, Nemotron, Qwen, Kokoro, and generic timeout errors to the spec's stable codes. Release the lifecycle in `finally` only when acquisition succeeded. While status is ready, refresh Overlord state and degrade when the `nemotron`, `llama`, or `kokoro` managed worker is absent/error or when `ready_until` expires.

- [ ] **Step 7: Run coordinator tests**

Run: `uv run python -m unittest apps.dimon_live.tests.test_demo_preparation -v`

Expected: all tests PASS.

- [ ] **Step 8: Commit Task 2**

```bash
git add apps/dimon_live/demo_preparation.py apps/dimon_live/tests/test_demo_preparation.py
git commit -m "feat: coordinate best-effort demo warming"
```

### Task 3: Local-only FastAPI preparation boundary

**Files:**
- Modify: `apps/dimon_live/app.py`
- Create: `apps/dimon_live/tests/test_demo_preparation_api.py`

**Interfaces:**
- Consumes: `DemoPreparationCoordinator.start()/status()` from Task 2 and the selected local `InferenceProviders`.
- Produces: local-only `POST /api/demo/prepare`, `GET /api/demo/prepare`, injected `demo_preparer` support in `create_app`, and `/static/demo_preparation.mjs` serving for Task 4.

- [ ] **Step 1: Write failing operator-boundary tests**

Add tests named:

- `test_prepare_routes_are_absent_when_disabled_or_not_local_mode`
- `test_prepare_routes_reject_public_host_and_nonmatching_origin`
- `test_prepare_routes_accept_ipv4_localhost_and_ipv6_loopback_with_ports`
- `test_prepare_post_starts_one_run_and_returns_202_while_active`
- `test_prepare_status_never_exposes_internal_exception_or_socket_path`

Use injected fake coordinators and ASGI transports. Assert denied requests return `404`, not `401` or capability details. Include a public Host with a forged loopback `X-Forwarded-Host` and assert it remains denied.

- [ ] **Step 2: Run API tests and verify failure**

Run: `uv run python -m unittest apps.dimon_live.tests.test_demo_preparation_api -v`

Expected: FAIL because the routes do not exist.

- [ ] **Step 3: Add dependency wiring and operator guard**

Extend `create_app` with:

```python
demo_preparer: DemoPreparationCoordinator | None = None
```

Construct a default coordinator only in enabled local mode. Resolve the Overlord socket from `DIMON_GPU_OVERLORD_SOCKET` or `<XDG_RUNTIME_DIR>/gpu-overlord-<uid>.sock`. Implement a helper that parses the actual `Host` and optional `Origin` without trusting forwarding headers and requires matching loopback hostnames.

- [ ] **Step 4: Add sanitized routes**

Add `GET` and `POST /api/demo/prepare`. Return `404` outside the operator boundary. Return `202` from POST while `PREPARING` and `200` for terminal snapshots. Do not resolve or create a visitor session for these operator routes.

- [ ] **Step 5: Run focused and neighboring API tests**

Run:

```bash
uv run python -m unittest apps.dimon_live.tests.test_demo_preparation_api -v
uv run python -m unittest apps.dimon_live.tests.test_api apps.dimon_live.tests.test_sessions -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit Task 3**

```bash
git add apps/dimon_live/app.py apps/dimon_live/tests/test_demo_preparation_api.py
git commit -m "feat: expose local demo preparation API"
```

### Task 4: Presenter preparation control

**Files:**
- Create: `apps/dimon_live/static/demo_preparation.mjs`
- Modify: `apps/dimon_live/static/presenter.html`
- Modify: `apps/dimon_live/static/presenter.js`
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Consumes: Task 3's GET/POST preparation snapshot.
- Produces: `fetchDemoPreparationStatus`, `startDemoPreparation`, and `describeDemoPreparation` pure module exports plus Presenter DOM behavior.

- [ ] **Step 1: Write failing JavaScript contract tests**

Add `DEMO_PREPARATION_MODULE` to `test_ui_client.py` and tests named:

- `test_demo_preparation_client_uses_same_origin_get_and_post`
- `test_demo_preparation_formatter_covers_hidden_preparing_ready_failed_and_degraded`
- `test_demo_preparation_ready_countdown_never_goes_negative`
- `test_demo_preparation_public_404_keeps_control_hidden`

Run Node with injected `fetchImpl` and a fixed clock. Assert exact labels `Prepare demo`, `Preparing…`, `Prepared · best effort`, and `Prepare again`.

- [ ] **Step 2: Run client tests and verify failure**

Run: `uv run python -m unittest apps.dimon_live.tests.test_ui_client -v`

Expected: FAIL because `demo_preparation.mjs` does not exist.

- [ ] **Step 3: Implement the pure client module**

Create:

```javascript
export async function fetchDemoPreparationStatus(fetchImpl = fetch) { ... }
export async function startDemoPreparation(fetchImpl = fetch) { ... }
export function describeDemoPreparation(snapshot, nowMs = Date.now()) { ... }
```

Treat `404` as disabled/hidden, throw only bounded client errors for other non-OK responses, and return presentation data without mutating the DOM.

- [ ] **Step 4: Add hidden accessible Presenter controls**

Add a hidden button and `aria-live="polite"` status element to the tools area. Import the module from `presenter.js`, reveal only after a successful local GET, disable while preparing, poll every 500 ms while active and every two seconds while ready, and preserve all existing scenario-button behavior.

- [ ] **Step 5: Serve the static module and run UI/API tests**

Add the no-store `/static/demo_preparation.mjs` route beside existing static routes.

Run:

```bash
uv run python -m unittest apps.dimon_live.tests.test_ui_client -v
uv run python -m unittest apps.dimon_live.tests.test_demo_preparation_api -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit Task 4**

```bash
git add apps/dimon_live/static/demo_preparation.mjs apps/dimon_live/static/presenter.html apps/dimon_live/static/presenter.js apps/dimon_live/app.py apps/dimon_live/tests/test_ui_client.py
git commit -m "feat: add Presenter demo preparation control"
```

### Task 5: Configuration, documentation, and complete verification

**Files:**
- Modify: `apps/dimon_live/.env.example`
- Modify: `apps/dimon_live/README.md`
- Modify: `apps/dimon_live/tests/test_runbook_contract.py`

**Interfaces:**
- Consumes: Tasks 1-4 complete feature.
- Produces: documented local configuration, operator procedure, limitations, and verified branch.

- [ ] **Step 1: Write failing documentation contract tests**

Add assertions that `.env.example` and the README name:

- `DIMON_DEMO_PREPARE_ENABLED=0` as the safe default;
- `DIMON_GPU_OVERLORD_SOCKET`;
- the loopback-only Presenter URL;
- the best-effort limitation and Whisper hotkey behavior; and
- the fact that no GPU Overlord or systemd service is directly controlled.

- [ ] **Step 2: Run documentation tests and verify failure**

Run: `uv run python -m unittest apps.dimon_live.tests.test_runbook_contract -v`

Expected: FAIL because the configuration and procedure are undocumented.

- [ ] **Step 3: Document configuration and operation**

Update the example environment and README with enablement, preparation stages, expected ready/degraded states, retry behavior, and the warning that a later Whisper hotkey request can make Dimon cold again. Keep the existing 1.4-second endpoint default documented as unchanged.

- [ ] **Step 4: Run the focused feature suite**

Run:

```bash
uv run python -m unittest \
  apps.dimon_live.tests.test_gpu_overlord_status \
  apps.dimon_live.tests.test_demo_preparation \
  apps.dimon_live.tests.test_demo_preparation_api \
  apps.dimon_live.tests.test_ui_client \
  apps.dimon_live.tests.test_runbook_contract -v
```

Expected: all tests PASS.

- [ ] **Step 5: Run the complete repository verification**

Run:

```bash
uv lock --check
uv run python -m unittest discover -s apps/dimon_live/tests -p 'test_*.py'
uv run python -m unittest discover -s tests -p 'test_*.py'
uv run python -m compileall -q apps tests
scripts/check-repository-boundary
```

Expected: every command exits zero with no test failures or boundary violations.

- [ ] **Step 6: Perform non-mutating live preflight**

With the production services untouched, verify the current app, Overlord socket, registered service states, and Presenter route. Record that the known stale Nemotron registration prevents a successful warm-up until separately repaired; do not restart it as part of this feature.

- [ ] **Step 7: Commit Task 5**

```bash
git add apps/dimon_live/.env.example apps/dimon_live/README.md apps/dimon_live/tests/test_runbook_contract.py
git commit -m "docs: explain best-effort demo preparation"
```

- [ ] **Step 8: Review the complete branch diff**

Compare every changed file with the design, confirm no GPU Overlord or systemd files changed, and confirm all task commits are present. Use the verification-before-completion workflow before reporting success.

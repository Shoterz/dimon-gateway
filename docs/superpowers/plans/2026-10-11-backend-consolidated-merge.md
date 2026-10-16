# Backend-on-Consolidated Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge the teammate backend branch into the safety-first consolidated UI while preserving both feature sets and enforcing server-managed Whisper/Kokoro speech with TRTC as an outage-only backup.

**Architecture:** Start from the current `ui/safety-first-integration` tip, which contains the approved design and the same product tree as `origin/consolidated-ui`. Merge `origin/backend-on-consolidated` in an isolated worktree and resolve the six textual conflicts with the design's hybrid rules. Add provider wrappers around the existing recognizer and synthesizer contracts, then adapt hands-free narration to the server audio-artifact path and reconcile the client state machines.

**Tech Stack:** Python 3.12, FastAPI, SQLite, asyncio, vanilla JavaScript modules, WebSockets, pytest, Node syntax checks

**Spec:** `docs/superpowers/specs/2026-10-11-backend-consolidated-merge-design.md`

## Global Constraints

- Preserve both branch histories through a merge commit.
- Keep the consolidated safety-first UI as the presentation foundation.
- Keep the teammate's app lock, hands-free, voiceprint, multi-intent, payee-addition, stock-rule, pending-order, and session-scale behavior.
- Use the configured remote/local Whisper path and Kokoro as the primary speech providers.
- Call TRTC once only after an eligible primary connection, timeout, or unavailable failure.
- Do not call TRTC for malformed audio, bounds, policy, authentication, validation, or schema failures.
- Do not ship `window.speechSynthesis`, `speechSynthesis`, or `SpeechSynthesisUtterance`.
- Keep fixture mode deterministic and cloud-free.
- Keep every financial action behind server validation, immutable receipts, and signed approval.
- Do not expose the standalone TRTC assistant as an alternate product mode.

## Review Focus

- A primary stream that fails after receiving audio must replay the bounded utterance to TRTC once; Task 2 adds a mid-stream failure test.
- A failure code outside the availability allowlist must remain unchanged and must not contact TRTC; Task 2 adds parameterized refusal tests.
- Hands-free and push-to-talk must not own the microphone together or capture Dimon's playback; Task 3 adds controller-state tests.
- Requests containing both synthetic catalog terms and real-stock words must retain synthetic precedence; Task 1 adds routing tests.
- A locked session must expose only lock-state and unlock operations while account and action routes refuse access; Task 4 runs and extends boundary tests.

---

### Task 1: Merge Branches and Resolve the Six Conflicts

**Files:**
- Modify: `apps/dimon_live/service.py`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/invest_ui.mjs`
- Modify: `apps/dimon_live/static/paper_equity_ui.mjs`
- Modify: `apps/dimon_live/tests/test_database.py`
- Modify: `apps/dimon_live/tests/test_service.py`
- Modify: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Consumes: both branch trees at the SHAs recorded in the spec.
- Produces: one conflict-free merge commit whose service routing, DOM shell, investment rendering, and seed data contain both branches' behavior.

- [ ] **Step 1: Create and verify the isolated integration worktree**

Run:

```bash
git worktree add .worktrees/backend-on-consolidated-merge -b merge/backend-on-consolidated ui/safety-first-integration
uv run pytest -q
```

Expected: the worktree starts from the design commit and the baseline suite exits 0.

- [ ] **Step 2: Start the merge without committing**

Run:

```bash
git merge --no-commit --no-ff origin/backend-on-consolidated
git diff --name-only --diff-filter=U
```

Expected: Git lists only the six conflicts named in the spec.

- [ ] **Step 3: Add hybrid routing and seed assertions**

Extend `test_service.py` with tests named
`test_synthetic_catalog_precedes_real_stock_rules` and
`test_rule_stock_sell_precedes_buy_only_model`. Extend
`test_database.py::test_initialize_and_reset_restore_seed_balances` to assert
the union of utility settlement accounts and `MXXX LXX`.

- [ ] **Step 4: Resolve backend conflicts**

In `service.py`, retain synthetic-investment precedence and integrate
`stock_request.parse`, `_real_stock_sell`, rule-backed research/purchase,
`payee_additions.understand`, and the fixed transfer parser. In
`test_database.py`, retain the S$2,000 savings seed and include every account
from both branches.

- [ ] **Step 5: Resolve client conflicts**

Use the consolidated imports, evidence lifecycle, startup watchdog, formatting
helpers, and diagnostic behavior. Add the teammate imports and behavior for
multi-request, hands-free, lock, payee additions, sales, pending orders, and
settlement notices. Keep the synthetic marker and add the top-bar actions.
Combine accessible sector styling with share counts and Sell controls. Combine
trusted currency/source formatting with sale proceeds and cancellation.

- [ ] **Step 6: Verify the resolved integration surface**

Run:

```bash
uv run pytest -q apps/dimon_live/tests/test_service.py apps/dimon_live/tests/test_database.py apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_investing.py apps/dimon_live/tests/test_paper_equity_ui.py
node --check apps/dimon_live/static/app.js
node --check apps/dimon_live/static/invest_ui.mjs
node --check apps/dimon_live/static/paper_equity_ui.mjs
git diff --check
rg -n '^<<<<<<<|^=======|^>>>>>>>' apps services tests
```

Expected: tests and syntax checks pass, `git diff --check` reports nothing,
and `rg` finds no conflict markers.

- [ ] **Step 7: Commit the merge**

```bash
git add -A
git commit -m "merge: integrate backend-on-consolidated"
```

### Task 2: Add Primary-to-TRTC Speech Fallback

**Files:**
- Create: `apps/dimon_live/speech_fallback.py`
- Create: `apps/dimon_live/tests/test_speech_fallback.py`
- Modify: `apps/dimon_live/provider_factory.py`
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/tencent_tts.py`
- Modify: `.env.example`
- Modify: `README.md`

**Interfaces:**
- Consumes: `Recognizer.open_stream(on_partial)`, `Recognizer.transcribe(audio, on_partial)`, synthesizer `__call__(text)`, optional `synthesize_result(result)`, and TRTC factories from `trtc.py`.
- Produces: `PrimaryFallbackRecognizer(primary, backup, max_audio_bytes)`, `PrimaryFallbackSynthesizer(primary, backup)`, and `is_provider_unavailable(exc) -> bool`.

- [ ] **Step 1: Write failing provider fallback tests**

Add tests that assert:

```python
primary_success -> backup.calls == 0
primary_UPSTREAM_TIMEOUT -> backup.calls == 1
primary_WHISPER_UNAVAILABLE -> backup.calls == 1
primary_INVALID_INFERENCE_REQUEST -> backup.calls == 0
primary_AUDIO_LIMIT_EXCEEDED -> backup.calls == 0
primary_and_backup_fail -> primary stable failure is raised
midstream_primary_failure -> buffered audio is replayed once
fixture_mode -> no TRTC factory call
```

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```bash
uv run pytest -q apps/dimon_live/tests/test_speech_fallback.py
```

Expected: collection or assertions fail because fallback adapters do not exist.

- [ ] **Step 3: Implement fallback adapters**

Implement `is_provider_unavailable(exc) -> bool` with an explicit allowlist.
Implement `PrimaryFallbackRecognizer.open_stream()` with a wrapper stream that
buffers no more than `max_audio_bytes`, forwards to the primary, and replays to
the backup only after an allowed failure. Implement `transcribe()` with the
same one-attempt rule. Implement `PrimaryFallbackSynthesizer.__call__()` and
`synthesize_result()` so the primary receives structured results and the
backup receives only `speech.result_summary(result)` text.

- [ ] **Step 4: Wire runtime configuration**

Build the current remote or local providers first. In non-fixture modes, create
TRTC backup clients only when the required credentials exist. Wrap the primary
recognizer and synthesizer with those clients. Change the unset local STT
default to `whisper_cpp`; retain explicit `whisper_intel`. Remove the reversed
TRTC-first selection from `DIMON_STT_BACKEND` and `DIMON_TTS_PROVIDER`.

- [ ] **Step 5: Verify GREEN and provider regressions**

Run:

```bash
uv run pytest -q apps/dimon_live/tests/test_speech_fallback.py apps/dimon_live/tests/test_inference_modes.py apps/dimon_live/tests/test_remote_inference.py apps/dimon_live/tests/test_speech.py apps/dimon_live/tests/test_trtc.py
```

Expected: all tests pass and the fallback tests prove primary-first behavior.

- [ ] **Step 6: Commit**

```bash
git add apps/dimon_live/speech_fallback.py apps/dimon_live/tests/test_speech_fallback.py apps/dimon_live/provider_factory.py apps/dimon_live/app.py apps/dimon_live/tencent_tts.py .env.example README.md
git commit -m "feat: add TRTC speech outage fallback"
```

### Task 3: Replace Browser Narration with Server Audio

**Files:**
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/speech.py`
- Modify: `apps/dimon_live/static/hands_free.mjs`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/tests/test_api.py`
- Modify: `apps/dimon_live/tests/test_ui_client.py`
- Modify: `apps/dimon_live/tests/test_hands_free.py`

**Interfaces:**
- Consumes: `SpeechService.queue_text(text, context)` and `/api/audio/{artifact_id}`.
- Produces: `POST /api/speech/prompts` with `{prompt_key, values}` where the server validates typed values and renders a bounded template, returning `{audio_artifact_id}`; plus `handsFree.say(promptKey, values, textForDisplay)` that plays server audio. Action results reuse their existing `audio_artifact_id` instead of resubmitting result text.

- [ ] **Step 1: Write failing API and client tests**

Add an API test that accepts only enumerated hands-free prompt keys and typed
template values, returns an artifact owned by the current session, and rejects
arbitrary text or unknown value fields. Add client tests that scan every
shipped `.js` and `.mjs` file for browser synthesis APIs and assert
`hands_free.mjs` requests and plays a server audio artifact.

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```bash
uv run pytest -q apps/dimon_live/tests/test_api.py apps/dimon_live/tests/test_ui_client.py -k 'speech_prompt or browser_speech or hands_free'
```

Expected: the prompt endpoint is missing and `hands_free.mjs` contains browser synthesis calls.

- [ ] **Step 3: Add bounded prompt synthesis**

Define the hands-free prompt catalog in `speech.py`. Add the authenticated
prompt endpoint in `app.py`, queue through `SpeechService`, and return only the
artifact identifier. Do not accept client-provided prose for synthesis.

- [ ] **Step 4: Replace browser speech calls**

Rewrite `say()` and stop/is-speaking hooks in `hands_free.mjs` to fetch the
prompt artifact, poll `/api/audio/{id}`, and play an `Audio` object. Reuse the
existing playback ownership callback so microphone frames remain muted during
Dimon audio. Remove all browser synthesis calls and fallback comments.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
uv run pytest -q apps/dimon_live/tests/test_api.py apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_hands_free.py apps/dimon_live/tests/test_speech.py
node --check apps/dimon_live/static/hands_free.mjs
node --check apps/dimon_live/static/app.js
rg -n 'speechSynthesis|SpeechSynthesisUtterance|SpeechRecognition|webkitSpeechRecognition' apps/dimon_live/static
```

Expected: tests and syntax checks pass, and `rg` returns no shipped browser
speech API usage.

- [ ] **Step 6: Commit**

```bash
git add apps/dimon_live/app.py apps/dimon_live/speech.py apps/dimon_live/static/hands_free.mjs apps/dimon_live/static/app.js apps/dimon_live/tests/test_api.py apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_hands_free.py
git commit -m "feat: route hands-free speech through server audio"
```

### Task 4: Reconcile Client State, Locking, and Multi-Request Behavior

**Files:**
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/app.css`
- Modify: `apps/dimon_live/static/hands_free.mjs`
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/session_manager.py`
- Modify: `apps/dimon_live/tests/test_ui_client.py`
- Modify: `apps/dimon_live/tests/test_app_lock.py`
- Modify: `apps/dimon_live/tests/test_sessions.py`
- Modify: `apps/dimon_live/tests/test_multi_intent.py`

**Interfaces:**
- Consumes: the merged lock endpoints, hands-free controller hooks, existing push-to-talk functions, and `multiRequest.observe(result)`.
- Produces: one microphone owner, one playback owner, lock-first startup, and evidence-aware multi-request rendering.

- [ ] **Step 1: Write failing state-ownership tests**

Add tests named `test_hands_free_pauses_push_to_talk_and_result_playback`,
`test_lock_startup_completes_watchdog_without_loading_accounts`, and
`test_multi_request_updates_plan_bar_and_evidence_lifecycle`.

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```bash
uv run pytest -q apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_app_lock.py apps/dimon_live/tests/test_sessions.py apps/dimon_live/tests/test_multi_intent.py
```

Expected: at least the new integration assertions fail.

- [ ] **Step 3: Implement one voice and startup state owner**

Make push-to-talk pause the hands-free controller before opening its socket and
resume it after action completion. Make hands-free playback participate in the
same audible-state checks as result and read-back audio. Load the session first;
for a locked session, render the lock screen and dispatch boot completion
without reading state or payees. After unlock, load the remaining shell once.

- [ ] **Step 4: Preserve evidence while integrating multi-request state**

Call both `multiRequest.observe(result)` and the consolidated evidence lifecycle
for each result. Keep exact-retry coordinates stable. Place plan-bar state in
the Ask flow without moving technical diagnostics out of Evidence.

- [ ] **Step 5: Verify GREEN**

Run the Step 2 command again, then:

```bash
node --check apps/dimon_live/static/app.js
node --check apps/dimon_live/static/hands_free.mjs
git diff --check
```

Expected: all tests and checks pass.

- [ ] **Step 6: Commit**

```bash
git add apps/dimon_live/static/app.js apps/dimon_live/static/index.html apps/dimon_live/static/app.css apps/dimon_live/static/hands_free.mjs apps/dimon_live/app.py apps/dimon_live/session_manager.py apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_app_lock.py apps/dimon_live/tests/test_sessions.py apps/dimon_live/tests/test_multi_intent.py
git commit -m "fix: reconcile merged voice and session state"
```

### Task 5: Verify Auto-Merged Backend Features as One Product

**Files:**
- Modify as failures require: `apps/dimon_live/app.py`
- Modify as failures require: `apps/dimon_live/database.py`
- Modify as failures require: `apps/dimon_live/paper_equity.py`
- Modify as failures require: `apps/dimon_live/paper_equity_store.py`
- Modify as failures require: `apps/dimon_live/voiceprint.py`
- Modify as failures require: `apps/dimon_live/hands_free.py`
- Modify as failures require: focused tests under `apps/dimon_live/tests/`

**Interfaces:**
- Consumes: every merged API, database migration, receipt, voiceprint, and stock-order contract.
- Produces: a focused integration suite with no failures across either branch's added behavior.

- [ ] **Step 1: Run the backend feature suites**

Run:

```bash
uv run pytest -q apps/dimon_live/tests/test_app_lock.py apps/dimon_live/tests/test_hands_free.py apps/dimon_live/tests/test_voiceprint.py apps/dimon_live/tests/test_multi_intent.py apps/dimon_live/tests/test_stock_orders_by_rule.py apps/dimon_live/tests/test_trtc.py apps/dimon_live/tests/test_scale.py apps/dimon_live/tests/test_sessions.py apps/dimon_live/tests/test_api.py apps/dimon_live/tests/test_database.py apps/dimon_live/tests/test_service.py
```

Expected: all tests pass. Treat each failure as an integration defect.

- [ ] **Step 2: For each failure, add or tighten the smallest reproducing test**

Run the single failing test and confirm the expected RED result before changing
production code. Record any necessary deviation from the spec as a ledger
ruling.

- [ ] **Step 3: Fix the production contract and rerun focused tests**

Make the smallest change that satisfies the design and both branch contracts.
Run the individual reproducer, then the Step 1 command.

- [ ] **Step 4: Commit integration repairs**

```bash
git add -A
git commit -m "fix: preserve merged backend contracts"
```

Expected: skip this commit if Step 1 passes without changes.

### Task 6: Full Verification and Browser Smoke Test

**Files:**
- Modify only if a failing verification exposes a defect.

**Interfaces:**
- Consumes: the complete merged product.
- Produces: fresh test, syntax, conflict-marker, and browser evidence for handoff.

- [ ] **Step 1: Run the complete automated suite**

Run:

```bash
uv run pytest -q
```

Expected: exit 0 with no failures.

- [ ] **Step 2: Run static integrity checks**

Run:

```bash
git diff --check ui/safety-first-integration...HEAD
find apps/dimon_live/static -type f \( -name '*.js' -o -name '*.mjs' \) -print0 | xargs -0 -n1 node --check
rg -n '^<<<<<<<|^=======|^>>>>>>>' apps services tests
rg -n 'speechSynthesis|SpeechSynthesisUtterance|SpeechRecognition|webkitSpeechRecognition' apps/dimon_live/static
```

Expected: all syntax checks pass; the searches find no conflict markers or
browser speech APIs.

- [ ] **Step 3: Start fixture mode and smoke-test the UI**

Run the app in fixture mode. Verify Home, Ask, Wealth, Safety, and Evidence;
lock and face-unlock behavior; typed fallback after unavailable speech;
hands-free controls; multi-request review; stock sale and cancellation; and
mobile-width keyboard/focus behavior.

- [ ] **Step 4: Run the final branch review and repair important findings**

Generate the review package against the integration base. Review the merge
against the spec, the Review Focus list, and all ledger rulings. Fix Critical
or Important findings with a failing test first, then rerun the full suite.

- [ ] **Step 5: Record the verified integration head**

Run:

```bash
git status --short --branch
git log --oneline --decorate -8
git diff --stat ui/safety-first-integration...HEAD
```

Expected: the worktree is clean, the history contains the backend merge parent,
and the final diff contains the combined product.

# Dimon Phase 1 Initialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the deployed Dimon frontend initialize reliably, explain startup failure, distinguish Invest from digiWealth, and restore release-clean UI contracts.

**Architecture:** Keep FastAPI's explicit same-origin static routes, but protect them with a transitive-import contract test. Add a dependency-free classic watchdog before the module entry point, with `app.js` dispatching a completion event only after required startup loads succeed. Preserve the existing HTML/CSS/ES-module structure and reconcile tests against the approved banking-shell behavior.

**Tech Stack:** Python 3.12, FastAPI/Starlette TestClient, unittest/pytest, browser-native ES modules, HTML, CSS, inline SVG.

**Spec:** `docs/superpowers/specs/2026-10-10-dimon-release-repair-design.md`

## Global Constraints

- Keep every balance, transfer, and investment explicitly synthetic.
- Do not change banking-domain rules or enable real payment rails.
- Preserve the existing visual identity and frontend architecture.
- Use test-first changes and preserve unrelated worktree edits.
- Customer-visible controls and failure states must remain keyboard and screen-reader accessible.

## Review Focus

- A missing nested import must make the smoke test fail with the missing `/static/...` path, not silently stop traversal; Task 1 tests it through the currently missing module.
- A successful boot must cancel the watchdog before its timeout and leave real balance content intact; Task 2 asserts the event contract and completion ordering.
- A module-load failure occurs before `app.js` can update the DOM; Task 2 asserts the watchdog contains no module imports and owns its reload control.
- Icon meaning must remain understandable without color or SVG accessibility noise; Task 3 asserts distinct paths, visible text labels, and decorative SVG treatment.
- Contract updates must not normalize unsafe transaction wording; Task 4 retains immutable-receipt, honest queued/filled, and microphone-continuation assertions.

---

### Task 1: Transitive static-module delivery

**Files:**
- Modify: `apps/dimon_live/tests/test_api.py`
- Modify: `apps/dimon_live/app.py`

**Interfaces:**
- Consumes: `create_app()` and its existing explicit `/static/*.mjs` responses.
- Produces: `/static/transaction_display.mjs` with JavaScript media type; a smoke test that recursively resolves relative `import ... from` specifiers from `/static/app.js`.

- [ ] **Step 1: Write the failing deployment tests**

Add `test_transaction_display_module_is_served_as_javascript` and `test_app_module_graph_is_fully_served`. The graph test tracks visited paths, extracts static relative imports, requests each resolved `/static/...` path, and asserts HTTP 200 plus JavaScript content type.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest apps/dimon_live/tests/test_api.py -k 'transaction_display_module or app_module_graph' -q`

Expected: FAIL naming `/static/transaction_display.mjs` with HTTP 404.

- [ ] **Step 3: Expose the missing module**

Add an explicit `transaction_display_script()` route beside the other client-module routes in `apps/dimon_live/app.py`, returning the file as `application/javascript` with `Cache-Control: no-store`.

- [ ] **Step 4: Verify GREEN**

Run: `uv run pytest apps/dimon_live/tests/test_api.py -k 'transaction_display_module or app_module_graph' -q`

Expected: both tests PASS.

- [ ] **Step 5: Commit**

Commit only `apps/dimon_live/app.py` and the Task 1 portions of `apps/dimon_live/tests/test_api.py` with message `fix(dimon): serve complete frontend module graph`.

### Task 2: Independent startup watchdog and explicit balance state

**Files:**
- Modify: `apps/dimon_live/tests/test_ui_client.py`
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/app.css`

**Interfaces:**
- Consumes: existing `Promise.all([loadSession(), loadState(), loadPayees()])` startup sequence and balance element IDs.
- Produces: `dimon:boot-complete` document event; `window.__dimonBootWatchdog` timer contract; accessible `Unavailable` balance and reload recovery after timeout.

- [ ] **Step 1: Write failing watchdog contract tests**

Add source-contract tests asserting that the classic watchdog appears before the module script, has no `type="module"` or imports, listens for `dimon:boot-complete`, changes both dash balances to `Unavailable`, exposes a `Reload Dimon` button, and that `app.js` dispatches completion only in the fulfilled startup branch.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest apps/dimon_live/tests/test_ui_client.py -k 'startup_watchdog or boot_complete or unexplained_balance' -q`

Expected: FAIL because the watchdog and event do not exist.

- [ ] **Step 3: Implement the watchdog and completion event**

Add a small non-module head script that starts an 8-second timeout, clears it on `dimon:boot-complete`, renders `Unavailable` in `#home-total-balance` and `#home-wealth-balance`, replaces the account skeleton with a role-alert failure message, and provides a native reload button. Change the startup promise to dispatch completion only after all required loads resolve; retain the existing in-module error handling for failures after module evaluation.

- [ ] **Step 4: Add focused accessible styling**

Style the startup failure within the existing banking tokens, keeping text at least 4.5:1, control boundary/focus at least 3:1, and a minimum 44px reload target.

- [ ] **Step 5: Verify GREEN**

Run: `uv run pytest apps/dimon_live/tests/test_ui_client.py -k 'startup_watchdog or boot_complete or unexplained_balance' -q`

Expected: all watchdog contract tests PASS.

- [ ] **Step 6: Commit**

Commit the Task 2 files with message `fix(dimon): explain frontend startup failures`.

### Task 3: Distinct Invest and digiWealth iconography

**Files:**
- Modify: `apps/dimon_live/tests/test_ui_client.py`
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/app.css`

**Interfaces:**
- Consumes: existing text-labelled shortcut buttons and `currentColor` SVG styling.
- Produces: a candlestick Invest icon and a portfolio/managed-growth digiWealth icon, both decorative and visually distinct.

- [ ] **Step 1: Write the failing icon-identity test**

Add `test_invest_and_digiwealth_shortcuts_have_distinct_decorative_icons`, parse the two shortcut buttons, and assert each SVG has `aria-hidden="true"`, distinct path markup, and the existing visible labels.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest apps/dimon_live/tests/test_ui_client.py::UiClientContractTests::test_invest_and_digiwealth_shortcuts_have_distinct_decorative_icons -q`

Expected: FAIL because both shortcuts currently share the same bar-chart path and omit decorative semantics.

- [ ] **Step 3: Replace the two SVG glyphs**

Use simple stroke-based 24×24 SVGs consistent with adjacent shortcut icons: vertical price wicks and candle bodies for Invest; a contained allocation/leaf-growth mark for digiWealth. Add `aria-hidden="true"` and `focusable="false"`; do not add icon-only labels because visible button text already names each action.

- [ ] **Step 4: Verify GREEN**

Run: the Task 3 test command.

Expected: PASS.

- [ ] **Step 5: Commit**

Commit the Task 3 files with message `fix(dimon): distinguish investment shortcuts`.

### Task 4: Reconcile release UI contracts

**Files:**
- Modify as required: `apps/dimon_live/static/app.js`
- Modify as required: `apps/dimon_live/static/index.html`
- Modify as required: `apps/dimon_live/static/app.css`
- Modify as required: `apps/dimon_live/static/invest_ui.mjs`
- Modify: `apps/dimon_live/tests/test_ui_client.py`
- Modify: `apps/dimon_live/tests/test_paper_equity_ui.py`

**Interfaces:**
- Consumes: approved banking-shell markup and behavior from the spec plus existing immutable receipt/action modules.
- Produces: release-clean frontend contracts without weakening transaction-state or voice-failure honesty.

- [ ] **Step 1: Capture the current failures**

Run: `uv run pytest apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_paper_equity_ui.py -q`

Expected: the reported seven failures (or the current exact failure set) are recorded before edits.

- [ ] **Step 2: Classify each failure**

For each failing assertion, record whether production violates the approved design or the assertion describes superseded markup/copy. Preserve these immutable requirements: reply-audio failures remain user-safe, starter and investment actions enter the assistant flow, receipt review prevents premature microphone continuation, and filled/queued order copy accurately states settlement.

- [ ] **Step 3: Add or correct one failing assertion per regression**

Before each production correction, make the narrow test express the approved observable behavior and run it alone to confirm RED. For obsolete structural selectors, update only the selector while retaining the behavioral assertion.

- [ ] **Step 4: Make the minimum production corrections**

Change only behavior proven incorrect by Step 3. Do not refactor unrelated parts of the large `app.js` file.

- [ ] **Step 5: Verify the targeted suite**

Run: `uv run pytest apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_paper_equity_ui.py -q`

Expected: all tests PASS.

- [ ] **Step 6: Verify Phase 1 regression scope**

Run: `uv run pytest apps/dimon_live/tests/test_api.py apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_paper_equity_ui.py -q`

Expected: all tests PASS; any unrelated pre-existing failure is named explicitly and left untouched.

- [ ] **Step 7: Commit**

Commit only Phase 1 reconciliation files with message `fix(dimon): reconcile frontend release contracts`.

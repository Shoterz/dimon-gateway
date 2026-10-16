# Dimon Phase 2 Trustworthy Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give uncertain transactions an executable identical-request recovery path, keep implementation details out of customer copy, and make Evidence readable and decision-first.

**Architecture:** Preserve request coordinates in the existing pending-action state and render recovery controls through one focused helper in `app.js`. Keep customer-safe copy in the conversation while recording bounded technical context in existing Evidence diagnostics. Restructure Evidence with semantic native disclosure and a coherent light token layer rather than adding another visual system.

**Tech Stack:** Browser-native JavaScript modules, HTML, CSS, Python unittest/pytest source and Node behavior contracts.

**Spec:** `docs/superpowers/specs/2026-10-10-dimon-release-repair-design.md`

## Global Constraints

- Retrying must reuse the original request UUID and exact text.
- Discarding must warn that the previous outcome remains unknown before clearing coordinates.
- Customer copy describes outcome, possible money movement, and recovery; raw codes and service details stay in Evidence.
- Evidence normal text meets 4.5:1 contrast; controls, focus, and meaningful icons meet 3:1.
- State is never communicated through color alone.
- Preserve synthetic-demo language and existing banking-domain behavior.

## Review Focus

- Repeated Retry clicks while the request is in flight must not create parallel submissions; Task 1 asserts the busy guard.
- Discard cancellation must preserve the UUID and exact text; Task 1 asserts confirmation precedes reset.
- “Check settlement evidence” must retain the uncertain request context even without a receipt; Task 1 asserts Evidence opens before any state is cleared.
- Network exceptions and voice errors must not leak exception messages or reason codes into customer live regions; Task 2 covers both paths.
- Evidence must remain keyboard navigable with diagnostics collapsed by default and current-run proof first; Task 3 asserts DOM order and native disclosure.

---

### Task 1: Executable identical-request recovery

**Files:**
- Modify: `apps/dimon_live/tests/test_ui_client.py`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/app.css`

**Interfaces:**
- Consumes: `pendingActionRequest: {requestId, text} | null`, `requestAction(coordinates)`, `openEvidence(document, context)`, and `resetCommandMode()`.
- Produces: `renderUnknownActionRecovery(actionResponse, started)`, recovery buttons identified by `data-retry-same-request`, `data-check-settlement-evidence`, and `data-discard-unknown-request`.

- [ ] **Step 1: Write failing recovery-contract tests**

Add tests asserting the unknown-action branch renders all three controls, retains `pendingActionRequest`, retry calls `requestAction(pendingActionRequest)` without generating a UUID, retry disables its controls while in flight, Evidence opens without clearing state, and discard calls `window.confirm` before `resetCommandMode()`.

- [ ] **Step 2: Verify RED**

Run: `env UV_CACHE_DIR=/tmp/dimon-phase2-uv-cache uv run pytest apps/dimon_live/tests/test_ui_client.py -k 'unknown_action_recovery or identical_request_retry or discard_unknown' -q`

Expected: FAIL because the controls and helper do not exist.

- [ ] **Step 3: Implement the recovery helper**

Extract the unknown-action rendering into `renderUnknownActionRecovery(actionResponse, started)`. Render three native buttons in the candidate area. The retry handler resubmits the retained object through the same result path, disables all recovery controls until completion, and preserves coordinates again if the outcome remains unknown. The Evidence handler sets an unknown-outcome context and calls `openEvidence`; discard confirms explicit uncertainty, then resets.

- [ ] **Step 4: Style the recovery action group**

Use the existing button vocabulary, 44px targets, visible focus, a primary retry, neutral Evidence action, and clearly worded destructive discard. Do not use color alone to distinguish actions.

- [ ] **Step 5: Verify GREEN**

Run the Task 1 test command.

Expected: all recovery tests PASS.

- [ ] **Step 6: Commit**

Commit Task 1 files with message `fix(dimon): add identical-request recovery controls`.

### Task 2: Customer-safe error boundary

**Files:**
- Modify: `apps/dimon_live/tests/test_ui_client.py`
- Modify: `apps/dimon_live/static/app.js`

**Interfaces:**
- Consumes: existing `setOutcome`, `setSettlement`, voice live regions, and Evidence diagnostics state.
- Produces: `recordEvidenceDiagnostic(stage, error)` and customer messages that never interpolate `error.message`, HTTP detail, reason codes, FastAPI, or gateway terminology.

- [ ] **Step 1: Write failing public-copy boundary tests**

Add source contracts for startup, command submission, conversation follow-up, face approval, and voice errors. Assert customer-facing calls use fixed plain-language copy and that raw `error.message`, `reason_code`, HTTP status, “Gateway,” and “FastAPI” are routed only to Evidence diagnostics.

- [ ] **Step 2: Verify RED**

Run: `env UV_CACHE_DIR=/tmp/dimon-phase2-uv-cache uv run pytest apps/dimon_live/tests/test_ui_client.py -k 'customer_safe_error or evidence_diagnostic_boundary' -q`

Expected: FAIL on existing raw exception/code interpolation and implementation-language copy.

- [ ] **Step 3: Implement the diagnostic boundary**

Add `recordEvidenceDiagnostic(stage, error)` to store bounded stage, error name, reason code, and HTTP status for Evidence. Replace customer copy with specific outcome and next action: connection interruption, whether settlement is unknown or known unchanged, and retry/reload guidance. Do not remove existing logging or safety state.

- [ ] **Step 4: Verify GREEN**

Run the Task 2 test command.

Expected: all boundary tests PASS.

- [ ] **Step 5: Commit**

Commit Task 2 files with message `fix(dimon): separate customer and diagnostic errors`.

### Task 3: Accessible decision-first Evidence

**Files:**
- Modify: `apps/dimon_live/tests/test_ui_client.py`
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/app.css`

**Interfaces:**
- Consumes: current evidence IDs used by `app.js` and `banking_shell.mjs`.
- Produces: `#evidence-proof-summary` first, followed by current-run detail and collapsed native `<details>` groups for diagnostics, playbook, and history without changing queried IDs.

- [ ] **Step 1: Write failing hierarchy and accessibility tests**

Assert Evidence order is summary → validation → receipt → settlement → exactly-once status, then progressively disclosed diagnostics/playbook/history. Assert diagnostic disclosures use `<details><summary>`, headings remain associated, and legacy translucent/dark Evidence overrides are absent.

- [ ] **Step 2: Add contrast-token tests**

Assert Evidence uses explicit light surface and text tokens already meeting the project palette’s AA ratios: `--bank-surface`, `--bank-graphite`, `--bank-slate`, semantic status backgrounds plus text labels, and the standard focus token. Reject legacy overlay text/surface variables inside the Evidence override block.

- [ ] **Step 3: Verify RED**

Run: `env UV_CACHE_DIR=/tmp/dimon-phase2-uv-cache uv run pytest apps/dimon_live/tests/test_ui_client.py -k 'evidence_hierarchy or evidence_contrast or evidence_progressive' -q`

Expected: FAIL because technical material is still presented at equal weight and legacy dark/translucent rules remain active.

- [ ] **Step 4: Restructure Evidence without breaking IDs**

Add a compact current-run proof summary ahead of the lifecycle sections. Keep validation, receipt, settlement, and exactly-once content visible. Wrap runtime diagnostics, test playbook, and history in collapsed native disclosure sections with descriptive summaries and item counts/status where available.

- [ ] **Step 5: Replace legacy Evidence styling**

Create one light Evidence surface with explicit high-contrast text, status labels, borders, tables, links, and empty states. Remove old `.evidence-column` transparency and dark `.ledger-panel` collision. Ensure focus is visible and mobile layout has no nested-card overflow.

- [ ] **Step 6: Verify GREEN**

Run the Task 3 test command.

Expected: hierarchy, disclosure, and contrast contracts PASS.

- [ ] **Step 7: Run Phase 2 regression suite**

Run: `env UV_CACHE_DIR=/tmp/dimon-phase2-uv-cache uv run pytest apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_paper_equity_ui.py -q`

Expected: all tests PASS.

- [ ] **Step 8: Commit**

Commit Task 3 files with message `fix(dimon): make evidence decision-first and accessible`.

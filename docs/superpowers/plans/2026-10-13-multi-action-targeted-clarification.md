# Multi-Action Targeted Clarification Implementation Plan

> **For Codex:** Execute this plan with test-driven development and verify each safety boundary before deployment.

**Goal:** Preserve understood grouped actions, report every unresolved detail at once, and accept a short targeted follow-up without preparing any payment or order prematurely.

**Architecture:** Extend grouped preflight to return per-item resolution records instead of short-circuiting. Persist an expiring, user-scoped clarification session containing only the original grounded spans and bounded replacement targets. A clarification reply may fill those targets, after which the corrected grouped request is revalidated through the existing domain handlers and normal review-only draft creation path.

**Tech Stack:** Python/FastAPI, SQLite, vanilla JavaScript, unittest/pytest.

---

### Task 1: Aggregate grouped preflight failures

- Add failing unit tests proving every item is evaluated and resolved items are retained as non-executable previews.
- Add `PreparedActionIssue` / per-item status data to the pure multi-action preparation result.
- Produce one combined clarification question and never return a partially executable item set.

### Task 2: Persist bounded clarification state

- Add failing database tests for create, scope, expiry, cancellation, completion, and stale/replayed replies.
- Add a dedicated multi-action clarification table and lifecycle methods.
- Store exact original spans, replacement targets, and public checklist data; do not store or create drafts.

### Task 3: Complete a targeted follow-up safely

- Add failing service tests for recipient plus stock confirmation in one reply.
- Resolve only explicit saved-recipient names and explicit instrument confirmations.
- Re-run the corrected request through the full grouped validation path; create review drafts only if all items validate.

### Task 4: Present the grouped checklist consistently

- Add failing UI and speech tests for understood and unresolved items.
- Render a compact checklist and targeted question.
- Keep the displayed and spoken question identical.

### Task 5: Verify and deploy

- Run focused multi-action, database, UI, and speech suites.
- Run the broader Dimon suite and distinguish unrelated pre-existing failures.
- Commit, push, restart `dimon.service`, and verify health.

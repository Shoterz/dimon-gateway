# Dimon Banking Home Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the settlement-console-first interface with a DBS-inspired synthetic banking Home and focused Ask Dimon experience while preserving every existing backend security and approval boundary.

**Architecture:** Keep the FastAPI and framework-free JavaScript architecture. Add a small, testable navigation/presentation module and reorganize the existing DOM into state-driven Home, Pay, Ask, Activity, More, and Evidence views; existing action, conversation, voice, receipt, device, face, Singpass, investment, and ledger modules remain authoritative.

**Tech Stack:** FastAPI, semantic HTML, CSS, native ES modules, unittest/pytest, Node-based module tests, browser inspection.

**Spec:** `docs/superpowers/specs/2026-10-06-dimon-banking-home-redesign-design.md`

## Global Constraints

- Use only synthetic/demo balances, identities, investments, activity, and offers.
- Do not add DBS proprietary assets, real PayNow/bank connectivity, live trading, autonomous execution, or a new authentication system.
- Preserve the existing `/api/actions`, conversation, receipt/order, signing, face, Singpass, risk, session, and exactly-once behavior.
- Voice is input only and never grants approval authority.
- Use one inline SVG outline-icon vocabulary; do not use emoji or Unicode pictograms as controls.
- Use `#C9152C`, `#9D1023`, `#17191D`, `#F2F3F5`, `#FFFFFF`, and contrast-safe slate neutrals as the base palette.
- Keep visible keyboard focus, semantic live regions, native dialog behavior, WCAG AA contrast, reduced motion, and mobile safe-area spacing.
- Do not modify or stage the user's existing `apps/dimon_live/tests/test_bill_payment_grounding.py` change or the untracked `friend-frontend-dbs-ui/` tree.

## Review Focus

- Empty or partially populated account state must render an instructive Home state without throwing; Task 2 tests this through `renderHomeSummary`.
- Repeated bottom-navigation activation must not duplicate listeners or submit requests; Task 3 tests stable view activation.
- Unknown backend response views must remain visible in Ask and must not imply success; Task 4 tests fallback routing.
- Voice failure or denial must leave typed submission enabled in both composer locations; Task 4 preserves and tests the shared composer contract.
- Long account names, amounts, and assistant messages must wrap without obscuring bottom navigation at narrow widths; Task 5 verifies this in responsive browser inspection and CSS contract assertions.

---

### Task 1: Lock the banking shell contract

**Files:**
- Modify: `apps/dimon_live/tests/test_ui_client.py`
- Modify: `apps/dimon_live/tests/test_fixture_ui.py`

**Interfaces:**
- Consumes: Existing static page, app client, fixture session API, and approval controls.
- Produces: Failing contract tests for the new view IDs, navigation, composers, Evidence placement, icon vocabulary, and preserved approval coordinates.

- [ ] **Step 1: Add failing shell tests**

Add `test_banking_home_shell_has_five_views_and_persistent_ask_entry`, `test_technical_panels_live_only_inside_evidence_view`, `test_navigation_uses_svg_line_icons_without_unicode_control_icons`, and `test_existing_approval_dialog_coordinates_remain_present`. Assert exact IDs `home-view`, `pay-view`, `ask-view`, `activity-view`, `more-view`, `evidence-view`, `home-command-form`, `command-form`, and five `[data-view-target]` navigation buttons.

- [ ] **Step 2: Add failing fixture-boundary test**

Extend the fixture UI test to assert that `Synthetic demo` remains in the served shell while `Fixture inference` is contained in Evidence rather than Home.

- [ ] **Step 3: Run the tests and confirm failure**

Run: `uv run --extra test pytest apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_fixture_ui.py -q`

Expected: FAIL because the new shell and view IDs do not exist.

- [ ] **Step 4: Commit the failing tests**

```bash
git add apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_fixture_ui.py
git commit -m "test: define Dimon banking shell contract"
```

### Task 2: Add testable banking presentation helpers

**Files:**
- Create: `apps/dimon_live/static/banking_shell.mjs`
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/tests/test_ui_client.py`
- Modify: `apps/dimon_live/tests/test_api.py`

**Interfaces:**
- Consumes: Public `/api/state` accounts and transactions plus backend action result objects.
- Produces: `renderHomeSummary(state, helpers) -> { totalMinor, accountsHtml, activityHtml }`, `classifyActionResult(result) -> "payments" | "investing" | "account" | "request" | "unknown"`, and `activateView(root, viewName, { focus = true } = {}) -> boolean`.

- [ ] **Step 1: Write failing Node module tests**

Test that `renderHomeSummary` totals only `is_user_source` accounts, escapes labels, renders a helpful empty state, and turns committed transactions into consumer-readable activity without exposing full transaction IDs. Test known payment/investment/account/request views plus an unknown fallback. Test that `activateView` updates `hidden`, `aria-current`, and the destination heading focus target and returns `false` for an unknown view.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `uv run --extra test pytest apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_api.py -q`

Expected: FAIL because `banking_shell.mjs` and its static route do not exist.

- [ ] **Step 3: Implement the helper module and static route**

Create the three exported functions with no network calls or execution authority. Add `/static/banking_shell.mjs` beside the existing explicit module routes in `app.py`.

- [ ] **Step 4: Run focused tests and confirm success**

Run: `uv run --extra test pytest apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_api.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/dimon_live/static/banking_shell.mjs apps/dimon_live/app.py apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_api.py
git commit -m "feat: add banking presentation helpers"
```

### Task 3: Build the Home, navigation, Activity, More, and Evidence structure

**Files:**
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/app.css`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Consumes: `renderHomeSummary` and `activateView` from Task 2; existing state, ledger, health, playbook, and presenter elements.
- Produces: A responsive application shell with `data-view` sections and a five-item `data-view-target` bottom navigation; existing element IDs remain available to current modules.

- [ ] **Step 1: Add failing navigation and responsive CSS assertions**

Assert the shell includes fictional-account disclosure, promo header, account summary, six shortcuts, recent activity, More links, Evidence/Presenter access, `aria-current`, safe-area padding, `prefers-reduced-motion`, wrapping rules, and no old two-column workspace as the default view.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `uv run --extra test pytest apps/dimon_live/tests/test_ui_client.py -q`

Expected: FAIL on the new banking structure and styling contract.

- [ ] **Step 3: Rebuild the semantic shell in `index.html`**

Keep `account-list` on Home, place the consumer activity list in Activity,
place `invest-board` and profile entry in More, and move the playbook, trace,
commit summary, and technical ledger table into Evidence without changing IDs
needed by `app.js`. Add inline 24×24 outline SVG icons using `fill="none"`,
`stroke="currentColor"`, `stroke-linecap="round"`, and
`stroke-linejoin="round"`.

- [ ] **Step 4: Replace the visual system in `app.css`**

Implement the approved palette, compact top bar, red promotion, pale-gray Home, white account surfaces, shortcuts, dark bottom navigation, desktop width rules, focus-visible states, safe-area offsets, dialog compatibility, wrapping, and reduced motion. Retain selectors required by existing result, approval, investing, voice, and evidence rendering.

- [ ] **Step 5: Wire state rendering and navigation in `app.js`**

Import Task 2 helpers, render account totals/activity from every loaded state, attach one navigation handler per `[data-view-target]`, route Evidence and Presenter controls, and ensure reset/refresh re-render both consumer and technical views.

- [ ] **Step 6: Run focused tests**

Run: `uv run --extra test pytest apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_fixture_ui.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/dimon_live/static/index.html apps/dimon_live/static/app.css apps/dimon_live/static/app.js apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_fixture_ui.py
git commit -m "feat: build Dimon banking home shell"
```

### Task 4: Integrate persistent composers and routed assistant results

**Files:**
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/app.css`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/banking_shell.mjs`
- Modify: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Consumes: Existing `runCommand`, `renderResult`, voice capture, conversation, clarification, investing, paper-order, receipt approval, and Task 2 `classifyActionResult`.
- Produces: `submitFromComposer(form, input, source)`, shared Home/Ask draft synchronization, category labels, focused assistant transcript entries, and shortcut-to-intent routing that always enters the existing action path.

- [ ] **Step 1: Write failing composer and routing tests**

Test Home-to-Ask draft synchronization, Enter/submit behavior, shortcut prompt loading without execution, microphone buttons using the same voice state, payment/investing/account/request category mapping, visible unknown-result fallback, and typed-input availability after voice denial.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `uv run --extra test pytest apps/dimon_live/tests/test_ui_client.py -q`

Expected: FAIL on the new shared composer and routing behavior.

- [ ] **Step 3: Implement the persistent Home composer**

Add `home-command-form`, `home-command-input`, and `home-voice-button`. Route Home submission into the existing `command-form` with the same text and then activate Ask; do not create a second `/api/actions` implementation.

- [ ] **Step 4: Implement assistant presentation routing**

Add assistant transcript/result containers and have `renderResult` classify each backend result for presentation while preserving existing candidate, clarification, conversation, receipt, investment, and order controls. Unknown results display a neutral non-success message and their safe server detail.

- [ ] **Step 5: Wire shortcut and Pay launchers**

Use reviewed synthetic prompt templates or empty focused inputs. Buttons may load or submit through `runCommand`; they may not construct receipts, orders, recipients, account IDs, or approvals.

- [ ] **Step 6: Run focused tests**

Run: `uv run --extra test pytest apps/dimon_live/tests/test_ui_client.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/dimon_live/static/index.html apps/dimon_live/static/app.css apps/dimon_live/static/app.js apps/dimon_live/static/banking_shell.mjs apps/dimon_live/tests/test_ui_client.py
git commit -m "feat: integrate Ask Dimon banking flows"
```

### Task 5: Full security, accessibility, and visual verification

**Files:**
- Modify: `apps/dimon_live/tests/test_ui_client.py` only if a verification gap needs a regression assertion.
- Modify: `apps/dimon_live/static/index.html`, `apps/dimon_live/static/app.css`, or `apps/dimon_live/static/app.js` only for defects found during verification.

**Interfaces:**
- Consumes: Completed banking shell and all existing backend/security suites.
- Produces: Passing automated tests plus inspected Home, Ask, review, Activity, and Evidence states at mobile and desktop widths.

- [ ] **Step 1: Run the full Dimon suite**

Run: `uv run --extra test pytest apps/dimon_live/tests -q`

Expected: all tests PASS, including signed approval, strict payload, idempotency, session isolation, risk, voice, investment, and UI tests.

- [ ] **Step 2: Run repository boundary tests**

Run: `uv run --extra test pytest tests/test_repository_boundary.py tests/test_clean_clone.py -q`

Expected: PASS.

- [ ] **Step 3: Start the fixture demo and inspect mobile states**

Run the documented fixture development command. At approximately 390×844, inspect Home, Pay, Ask, Activity, More, Evidence, a clarification, and an actionable review. Confirm no overlap, clipping, horizontal scroll, or hidden composer and confirm long content wraps above the fixed navigation.

- [ ] **Step 4: Inspect desktop states**

At approximately 1440×900, inspect the same core views. Confirm focused content widths, readable Evidence layout, correct active navigation, and consistent line icons.

- [ ] **Step 5: Exercise security-critical interactions**

Submit a synthetic exact transfer, an ambiguous recipient, an investment request, and an unsupported request. Confirm only the exact actionable flows expose review controls, cancellation is non-mutating, approval facts come from stored backend results, and Evidence reflects committed state only after approval.

- [ ] **Step 6: Audit accessibility and motion**

Keyboard through navigation, shortcuts, both composers, review controls, and dialogs; inspect accessible names and live announcements; emulate reduced motion and verify nonessential transforms are removed.

- [ ] **Step 7: Fix each observed defect test-first and rerun Steps 1–6**

For every defect, add the smallest practical regression assertion before the fix. Repeat until automated and browser verification pass without known P0/P1 defects.

- [ ] **Step 8: Commit final verification fixes**

```bash
git add apps/dimon_live/static apps/dimon_live/tests/test_ui_client.py
git commit -m "fix: harden redesigned Dimon demo"
```

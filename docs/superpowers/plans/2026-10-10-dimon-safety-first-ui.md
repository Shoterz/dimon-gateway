# Dimon Safety-First UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reframe Dimon from a conventional consumer-bank clone into a safety-first synthetic banking demonstration where deterministic controls, approval coordinates, receipts, validation, and settlement evidence are the most discoverable product features.

**Architecture:** Keep the existing single-page shell and current service contracts. Change information architecture and DOM order in `index.html`, keep navigation and reusable presentation logic in focused `.mjs` modules, and consolidate visual decisions in `app.css` tokens. Each phase below is independently shippable; later phases may consume stable helpers introduced earlier but must not silently redesign their scope.

**Tech Stack:** Semantic HTML, CSS, browser-native ES modules, Python `unittest` contract tests, Node-based JavaScript unit snippets.

**Spec:** This document is the implementation spec and execution plan for the 2026-10-10 UI audit supplied by the product owner.

## Global Constraints

- The deterministic safety boundary—not generic banking chrome—is the primary product story.
- Preserve all existing API contracts, approval signing, validation, receipt, settlement, and exactly-once behavior.
- Use `S$50.00` (or `SGD 50.00`) for SGD and `US$50.00` for USD; never show a bare `$` on a transactional or approval surface.
- Identify funding sources as nickname plus masked account number, for example `Savings · •••• 1842`; fall back safely when the API does not supply the last four digits.
- Display `Synthetic demo · no real money` persistently in the top bar and in every approval surface.
- A blocked or completed action must expose a contextual `View execution evidence` control.
- DOM, visual, focus, and screen-reader reading order must agree at every breakpoint.
- Interactive controls must have a real action; otherwise render them as non-interactive content or remove them.
- Touch targets must be at least 44 × 44 CSS pixels, visible focus must be retained, and text/state contrast must meet WCAG 2.2 AA.
- Do not add frameworks or change server behavior unless a phase explicitly identifies a missing data contract.
- Run focused tests after each task and `python -m unittest apps.dimon_live.tests.test_ui_client apps.dimon_live.tests.test_fixture_ui` before handing off a phase.

## Five-Phase Roadmap

| Phase | Outcome | Primary audit items | Depends on |
|---|---|---|---|
| 1. Trust boundary and evidence IA | Safety story is visible from first paint and one tap away | Synthetic disclosure, clickable safety banner, top-level Evidence, remove dead notifications | None |
| 2. Exact approval coordinates | Every approval is precise, consistent, and explicitly synthetic | SGD ambiguity, masked source identity, approval warning, narrative/field parity | Phase 1 disclosure component/copy |
| 3. Evidence in the action loop | Success and failure states explain and link to execution proof | Receipts, validation, settlement, exactly-once discoverability | Phases 1–2 navigation and formatting |
| 4. Accessible shell and coherent theming | Keyboard, mobile order, controls, contrast, and tokens are trustworthy | Focus order, undersized controls, hard-coded/overlapping themes | Stable IA from phases 1–3 |
| 5. Wealth simplification and final polish | Wealth becomes three purposeful views instead of a card wall | Overview/Explore/Plans structure, repetitive catalog treatment, remove Evidence from Wealth | Phase 1 Evidence destination; Phase 4 tokens |

## Review Focus

- Approval data with missing account last-four digits: show the nickname without inventing digits and keep the surface usable (Phase 2 test).
- A blocked result without a receipt or trace: still link to Evidence and focus the general trace/status region (Phase 3 test).
- Deep-link/hash navigation and browser back behavior: activate the requested view and move focus once, without trapping it (Phase 1 test).
- Narrow screens and 200% zoom: no CSS `order` mismatch, clipped approval facts, or targets below 44 × 44 px (Phase 4 test/manual check).
- Empty or failed investment catalog data: Overview and Plans remain usable and Explore shows one clear empty state (Phase 5 test).

---

## Phase 1: Make the Trust Boundary and Evidence First-Class

### Product result

On first paint, a judge can tell this is a synthetic demonstration, understand that Dimon separates natural-language intent from deterministic execution, and reach Evidence directly. Generic or dead banking chrome no longer competes with that story.

### Task 1.1: Persistent synthetic disclosure and dead-control removal

**Files:**
- Modify: `apps/dimon_live/static/index.html` (top bar and approval dialog markup)
- Modify: `apps/dimon_live/static/app.css` (disclosure styling and top-bar layout)
- Test: `apps/dimon_live/tests/test_ui_client.py`
- Test: `apps/dimon_live/tests/test_fixture_ui.py`

**Interfaces:**
- Produces: `.synthetic-marker` with exact visible copy `Synthetic demo · no real money`; `.approval-synthetic-marker` using the same copy.
- Removes: the unhandled Notifications button and its interactive icon styling from the DOM.

- [ ] Add failing markup tests asserting one persistent top-bar marker, a marker inside each approval dialog (`#approval-dialog` and any separate stock/Singpass approval dialog), and no `aria-label="Notifications"` button.
- [ ] Run the two focused test modules and confirm the new assertions fail.
- [ ] Add the restrained marker to the top bar and approval surfaces; use muted styling that remains legible at AA contrast and does not resemble an error alert.
- [ ] Remove the notification control completely; do not retain a bell-shaped decorative affordance.
- [ ] Re-run the focused tests and commit as `feat(dimon): expose synthetic demo boundary`.

### Task 1.2: Safety explainer and first-class Evidence navigation

**Files:**
- Modify: `apps/dimon_live/static/index.html` (home banner, safety explainer, bottom navigation, Evidence view)
- Modify: `apps/dimon_live/static/banking_shell.mjs` (navigation/deep-link behavior)
- Modify: `apps/dimon_live/static/app.js` (wiring only if existing `[data-view-link]` handling is insufficient)
- Modify: `apps/dimon_live/static/app.css`
- Test: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Produces: view name `safety`; view name `evidence`; button/link contract `[data-view-link="safety"]` and `[data-view-target="evidence"]`.
- Safety explainer content: `Understand → Lock exact details → Validate policy → Approve → Settle once → Prove with evidence`.

- [ ] Add failing tests asserting the former `.promo-banner` is a real button/link, opens the `safety` view, the safety view explains all six stages, and Evidence exists in primary navigation.
- [ ] Add a navigation test for `#evidence`/`#safety` activation and heading focus; preserve browser back/forward when the shell already supports history, otherwise limit this task to stable view activation without inventing routing infrastructure.
- [ ] Implement the safety view as concise explanatory content, not another card catalog. Include a single `Open technical evidence` action.
- [ ] Put Evidence in the primary navigation. To stay within mobile width, replace `Activity` in the bottom bar with `Evidence`; keep Activity reachable from Home’s `See all`. Do not duplicate Evidence inside Wealth.
- [ ] Make the home safety banner open the safety view and remove the misleading outbound-arrow treatment unless it accurately signals in-app navigation.
- [ ] Re-run focused tests, manually tab through the new controls once at desktop and mobile widths, and commit as `feat(dimon): make safety evidence first class`.

### Phase 1 acceptance

- `Synthetic demo · no real money` remains visible in every primary view and every approval surface.
- The notification affordance is gone.
- The home safety banner works with pointer, Enter, and Space.
- Evidence is available directly from primary navigation; Activity still has a reachable route.
- No service/API behavior changes.

---

## Phase 2: Make Approval Coordinates Exact and Unambiguous

### Product result

The natural-language question, locked fields, and approval payload describe the same action with the same currency and source identity. A user never approves a less precise representation than the one Dimon narrated.

### Task 2.1: Centralize transactional money and source formatting

**Files:**
- Create: `apps/dimon_live/static/transaction_display.mjs`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/multi_action_review.mjs`
- Modify: `apps/dimon_live/static/paper_equity_ui.mjs`
- Modify: `apps/dimon_live/static/invest_ui.mjs` only where transactional approval money is rendered
- Test: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Produces: `formatTransactionalMoney(currency, minor) -> string` returning `S$50.00`, `US$50.00`, or `ISO 50.00`.
- Produces: `formatFundingSource({source_label, source_last4, account_last4}) -> string` returning `Savings · •••• 1842`, with nickname-only fallback when last four is absent.

- [ ] Add Node-backed failing tests for SGD, USD, unknown ISO currency, zero, negative values, and missing/non-numeric minor units; choose safe display output rather than silently producing `NaN`.
- [ ] Add failing tests for source labels with `source_last4`, `account_last4`, neither field, and HTML-like input (escaping remains the caller’s responsibility).
- [ ] Implement the pure helpers and replace duplicated transactional formatters in review/approval rendering. Portfolio-market display may keep its domain-specific formatter if it already distinguishes currency.
- [ ] Assert there is no `Intl.NumberFormat(... style: "currency", currency: "SGD")` path used by approval surfaces and no bare `$50.00` fixture expectation.
- [ ] Re-run tests and commit as `fix(dimon): make approval coordinates explicit`.

### Task 2.2: Enforce narrative/locked-field parity

**Files:**
- Modify: `apps/dimon_live/static/app.js` (receipt, batch, bill, and result rendering)
- Modify: `apps/dimon_live/static/multi_action_review.mjs`
- Modify: `apps/dimon_live/static/index.html` (approval fact labels if needed)
- Test: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Consumes: `formatTransactionalMoney` and `formatFundingSource` from Task 2.1.
- Approval fact contract: recipient/biller, exact currency amount, funding source, and action type all derive from the same receipt/order object used for approval submission.

- [ ] Add failing rendering tests proving a S$50 transfer question, review card, locked amount, and dialog total all display `S$50.00`, and all source fields display `Savings · •••• 1842` when last-four data exists.
- [ ] Add the missing-last-four test from Review Focus: all surfaces display `Savings`, never `•••• undefined`, and never invent a number.
- [ ] Replace display-only reads that use generic `source_label`/`formatSgd` inconsistently; do not alter signed payload fields or digest construction.
- [ ] Ensure batch and stock approvals preserve each item’s own currency and never relabel USD quotes as SGD funding debits.
- [ ] Re-run focused tests plus receipt/batch/stock approval suites and commit as `fix(dimon): align approval narrative and locked facts`.

### Phase 2 acceptance

- No transactional/approval surface shows an ambiguous bare dollar sign.
- Source identity is as specific as available data permits, consistently across narrative and locked fields.
- The synthetic marker appears inside approval surfaces without replacing substantive approval facts.
- Signed values, server payloads, and exactly-once behavior are unchanged.

---

## Phase 3: Put Execution Evidence in the Action Loop

### Product result

Evidence is not just a destination. Every completed or blocked action explains its safety disposition and offers one contextual route to the relevant proof, making receipts, validation, settlement state, and duplicate prevention discoverable at the moment they matter.

### Task 3.1: Contextual evidence links from outcomes

**Files:**
- Modify: `apps/dimon_live/static/index.html` (Evidence anchors/regions)
- Modify: `apps/dimon_live/static/app.js` (outcome action rendering and context handoff)
- Modify: `apps/dimon_live/static/banking_shell.mjs` (focused Evidence activation)
- Modify: `apps/dimon_live/static/app.css`
- Test: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Produces: `openEvidence(root, {receiptId, outcome, focusTarget}) -> boolean` in `banking_shell.mjs`.
- Produces: `[data-view-execution-evidence]` control with visible copy `View execution evidence`.
- Evidence target priority: matching receipt/ledger row, then validation/trace region, then Evidence heading.

- [ ] Add a failing pure-module test for target priority and a markup/source test requiring the control for executed and blocked outcomes.
- [ ] Add the Review Focus case: a blocked result with no receipt/trace still opens Evidence and focuses its heading/status region.
- [ ] Render the link beside—not inside—the status title for completed transfers, bills, batches, trades/plans, already-executed results, validation freezes, scam blocks, and generic blocks.
- [ ] Pass only non-sensitive identifiers already present in client state; do not put full approval payloads in URLs or DOM data attributes.
- [ ] Activate Evidence, reveal/highlight the best target temporarily, and move programmatic focus to it without creating a keyboard trap.
- [ ] Re-run tests and commit as `feat(dimon): link outcomes to execution evidence`.

### Task 3.2: Reframe the Evidence deep view around the deterministic lifecycle

**Files:**
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/app.js` (labels/state presentation only)
- Modify: `apps/dimon_live/static/app.css`
- Test: `apps/dimon_live/tests/test_ui_client.py`
- Test: `apps/dimon_live/tests/test_fixture_ui.py`

**Interfaces:**
- Evidence section order: `Decision trace`, `Validation`, `Approval receipt`, `Settlement`, `Exactly-once status`, then demo/runtime diagnostics.
- Status vocabulary: `Proposed`, `Blocked`, `Approved`, `Settled`, `Already settled`; never imply settlement from a proposed receipt.

- [ ] Add failing tests for section headings/order, receipt/settlement distinction, and the exactly-once status copy.
- [ ] Restructure the existing Evidence markup into the lifecycle order while preserving existing element IDs used by JavaScript and tests.
- [ ] Surface receipt digest/ID, validation disposition, ledger/settlement state, and duplicate/already-executed state in plain language; keep raw trace and fixture/model diagnostics available but visually secondary.
- [ ] Verify an awaiting-approval action does not show `Settled`, and an already-executed response does not imply a second execution.
- [ ] Re-run focused tests plus `test_receipts`, `test_validation_agent`, `test_audit_log`, and signed-approval tests; commit as `feat(dimon): clarify evidence lifecycle`.

### Phase 3 acceptance

- Every completed and blocked result has `View execution evidence` beside it.
- The link lands on the most relevant existing proof and has a safe fallback.
- The deep view distinguishes proposal, validation, authorization, and settlement.
- Exactly-once behavior is visible in user-facing language without changing backend semantics.

---

## Phase 4: Repair Reading Order, Accessibility, and Theme Discipline

### Product result

The page reads and tabs in the same logical sequence on mobile and desktop. Controls are comfortably operable, evidence states remain legible, and one coherent safety-first theme replaces mixed legacy/new styling and scattered magic colors.

### Task 4.1: Make DOM order canonical

**Files:**
- Modify: `apps/dimon_live/static/index.html` (Home child order)
- Modify: `apps/dimon_live/static/app.css` (desktop grid placement; remove mobile `order` workaround)
- Test: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Canonical Home DOM order: safety banner → greeting/title → Ask composer → accounts → shortcuts → recent activity → clearance.
- Desktop layout may use named grid areas; mobile must follow DOM flow without positive/negative `order` values.

- [ ] Replace the old test that asserts CSS reordering with a failing test that compares markup indices for the canonical order and rejects `.home-view > ... { order:` declarations.
- [ ] Move the Ask composer before accounts/shortcuts in HTML; preserve IDs and existing form wiring.
- [ ] Use desktop grid-area placement only where a different spatial arrangement is needed; do not change reading or focus order.
- [ ] Keyboard-test top bar → banner → Ask → accounts/shortcuts → activity → bottom navigation at 390 px and desktop width.
- [ ] Re-run focused tests and commit as `fix(dimon): align visual and focus order`.

### Task 4.2: Consolidate tokens and remove hard-coded UI colors

**Files:**
- Modify: `apps/dimon_live/static/app.css`
- Modify: `apps/dimon_live/static/invest_ui.mjs` (replace inline palette literals with semantic CSS classes/variables)
- Modify: `apps/dimon_live/static/index.html` only for semantic classes
- Test: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Token families: surface, text, border, accent, focus, success, warning, danger, info, and chart series.
- Components consume semantic tokens; legacy `bank-*` aliases may temporarily point to the new tokens but must not define competing values.

- [ ] Inventory hex/rgb/hsl literals in `app.css`, `index.html`, and UI `.mjs`; add a failing allowlist test that permits literals only in the root token declarations (plus technically required SVG/data assets).
- [ ] Define one canonical light theme in `:root`; map or delete overlapping legacy variables and use semantic state tokens for Evidence and approval states.
- [ ] Move sector colors from inline `style="--holding-sector-color:..."` generation to deterministic semantic classes or a bounded CSS variable palette whose literal values live in CSS tokens.
- [ ] Check normal text, muted text, badges, status text, focus rings, and disabled states for WCAG AA contrast; record checked foreground/background pairs in a short test comment or plan completion note.
- [ ] Re-run tests and commit as `refactor(dimon): unify visual tokens`.

### Task 4.3: Control-size and focus-state audit

**Files:**
- Modify: `apps/dimon_live/static/app.css`
- Modify: `apps/dimon_live/static/index.html` only where an accessible name/state is missing
- Test: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- All actionable buttons, links presented as controls, range inputs, and bottom-nav targets expose a minimum 44 × 44 px hit area.
- `:focus-visible` is never removed without an equal or stronger replacement.

- [ ] Add static assertions for the shared control minimum size and named tests for icon buttons, text buttons, Wealth controls, approval buttons, and bottom navigation.
- [ ] Normalize hit areas via shared selectors/tokens without inflating non-interactive text.
- [ ] Add/repair visible focus styles, dialog focus return, current navigation state, and accessible names for icon-only controls.
- [ ] At 390 × 844 and 200% zoom, manually verify no horizontal scrolling in approval facts and no control overlap; at reduced motion, ensure evidence highlighting does not rely on animation.
- [ ] Re-run focused tests and commit as `fix(dimon): harden accessible controls`.

### Phase 4 acceptance

- Mobile DOM, screen-reader, focus, and visual order agree.
- No undersized actionable control remains in the audited shell.
- Evidence/status colors pass AA and are not the sole carrier of meaning.
- Hard-coded component colors are eliminated or explicitly allowlisted as non-theme assets.

---

## Phase 5: Restructure Wealth and Remove Catalog/Card Fatigue

### Product result

Wealth is a compact, task-oriented area with three internal destinations—Overview, Explore, and Plans/profile. It no longer contains Evidence or Presenter tools and no longer renders ten equally weighted product cards in one long scroll.

### Task 5.1: Introduce Wealth sub-navigation and sections

**Files:**
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/invest_ui.mjs`
- Modify: `apps/dimon_live/static/app.css`
- Test: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Wealth tabs: `overview`, `explore`, `plans`, implemented as accessible tabs or an equivalent link/section pattern with correct keyboard behavior.
- Overview owns portfolio total, allocation, and holdings.
- Explore owns fund/equity discovery.
- Plans owns investor profile, goals, starter/recurring plan actions, and related quick actions.

- [ ] Add failing tests for the three destinations, selected state, keyboard switching (if tabs), and exactly one visible panel at a time.
- [ ] Move existing portfolio/allocation/holdings markup into Overview without changing IDs consumed by `renderDigiWealthPage`.
- [ ] Move starter/profile/long-term plan actions into Plans and remove the generic quick-action strip.
- [ ] Remove the Wealth `Evidence` and `Presenter view` tools entirely; Evidence is already primary navigation and Presenter remains available only from its purposeful demo/operator entry point.
- [ ] Preserve a clear route from an instrument or plan action into the existing Ask/approval flow.
- [ ] Re-run tests and commit as `refactor(dimon): split wealth into focused views`.

### Task 5.2: Build searchable/filterable Explore without a card wall

**Files:**
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/invest_ui.mjs`
- Modify: `apps/dimon_live/static/app.css`
- Test: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Produces: `filterWealthCatalog(items, {query, kind, sector}) -> item[]` as a pure exported function.
- Explore controls: search by name/symbol; kind filter `All | Funds & ETFs | Equities`; optional sector filter only if it can reuse existing sector data cleanly.
- Results use compact list/table rows with expandable details or one selected detail panel, not ten full cards.

- [ ] Add failing Node tests for case-insensitive name/symbol search, kind filtering, combined filters, whitespace, no matches, and stable source ordering.
- [ ] Add the Review Focus empty-data test: Explore shows one empty state while Overview and Plans remain operable.
- [ ] Implement search and filters with an announced result count and a real label; update on input without stealing focus.
- [ ] Render compact results showing symbol/name, kind/sector, one key fact, source link, and `Ask Dimon`; place long summaries in disclosure/detail UI rather than every row.
- [ ] Preserve issuer link security attributes and existing prompt text passed into Ask.
- [ ] Re-run tests and commit as `feat(dimon): add focused wealth exploration`.

### Task 5.3: Full regression and presentation review

**Files:**
- Modify: tests or UI files only for defects found during this task
- Test: all `apps/dimon_live/tests`

**Interfaces:**
- No new interface; this is the release gate for all five phases.

- [ ] Run `python -m unittest discover -s apps/dimon_live/tests` and fix only regressions attributable to these phases.
- [ ] Exercise: safety explainer → Ask transfer → approval → executed result → contextual Evidence; blocked action → Evidence; Wealth Overview → Explore → Ask; microphone unavailable → typed fallback.
- [ ] Verify top-bar and approval synthetic markers, explicit currencies, masked sources, navigation current state, tab order, 44 px targets, 200% zoom, and reduced motion.
- [ ] Search for dead interactive affordances and ambiguous currency: `rg -n 'Notifications|promo-banner|\$[0-9]|onclick|cursor: pointer|data-view-link="evidence"' apps/dimon_live/static` and review every hit rather than blindly requiring zero.
- [ ] Commit any final corrections as `fix(dimon): complete safety-first ui regression`.

### Phase 5 acceptance

- Wealth opens to Overview and exposes Explore and Plans/profile without a long all-in-one page.
- Explore is searchable/filterable and materially denser than the old card catalog.
- Evidence is absent from Wealth and remains easy to find globally/contextually.
- All UI and domain test suites pass, and the core demo path works by keyboard and pointer.

---

## Agent Handoff Template

Give each implementation agent exactly one phase and this plan path. Use this prompt:

> Implement Phase N from `docs/superpowers/plans/2026-10-10-dimon-safety-first-ui.md`. Read the Global Constraints, Review Focus, your phase tasks, and acceptance criteria. Use test-driven development, preserve unrelated workspace changes, run the listed focused tests, and stop after your phase with a concise diff/test report. Do not implement later phases or reinterpret settled product decisions.

Run phases sequentially. Phases 1–3 establish interfaces and information architecture; Phase 4 should audit their final markup rather than styling a moving target; Phase 5 relies on the global Evidence destination and canonical theme tokens.

# Dimon Release Repair Design

## Purpose

Restore the deployed Dimon banking interface to a usable, trustworthy state, then improve transaction recovery, Evidence accessibility, and loading performance. The interface must remain explicit that all balances, transfers, and investments are synthetic.

## Success criteria

- The deployed entry module and every transitive local import resolve successfully.
- Startup either completes or produces a visible, actionable failure state without relying on `app.js` having executed.
- Available balance never remains an unexplained dash after startup succeeds or fails.
- Invest and DigiWealth use distinct, meaningful navigation symbols rather than the same generic chart glyph.
- An uncertain transaction offers a true identical-request retry that retains its UUID and request text, an Evidence action, and an explicit discard action.
- Customer-facing errors describe outcome, money-movement state, and recovery without exposing implementation details.
- Evidence text and controls meet WCAG 2.2 AA contrast requirements and prioritize current-run proof over diagnostics.
- The targeted UI contract suite is release-clean, with intentional behavior documented in its assertions.
- Production loading avoids unnecessary sequential module discovery and uses appropriate caching.

## Phase 1: Restore and harden initialization

### Static delivery

Expose `transaction_display.mjs` through the FastAPI application. Add an automated deployment smoke test that begins at `/static/app.js`, follows every relative static import transitively, and asserts that each resource returns a successful JavaScript response. This test protects future modules from the manually enumerated route failure mode.

### Startup watchdog

Place a small classic script in the HTML shell before the module entry point. It starts a timeout and listens for a boot-complete signal dispatched by the application. If initialization does not complete, it replaces balance and health skeletons with a concise failure state and offers a reload button. The watchdog must not depend on any imported module.

### Balance state

Treat the dash as a loading placeholder only. The successful startup path renders a formatted balance; the watchdog renders `Unavailable` with an accessible explanation. This prevents a technical loading glyph from appearing to be an account value.

### Navigation identity

Preserve the existing icon vocabulary while assigning distinct meanings: Invest uses a market/candlestick symbol and DigiWealth uses a portfolio/managed-growth symbol. Both remain decorative inside text-labelled buttons and inherit current-color styling with at least 3:1 non-text contrast.

### Contract reconciliation

Run the targeted UI tests, classify each failure as an implementation regression or an obsolete expectation, and make the smallest behavior-preserving correction. Safety invariants remain fixed: receipts are immutable, filled and queued trade states are described honestly, navigation works, and microphone continuation never hides failure.

## Phase 2: Trustworthy recovery and Evidence

### Identical-request retry

Retain the pending action UUID and original text whenever outcome is unknown. Present three actions:

- **Retry same request** resubmits the retained UUID and text.
- **Check settlement evidence** opens Evidence at the current request.
- **Discard and start over** warns that the previous outcome remains unknown, then clears the pending request only after confirmation.

Controls prevent duplicate clicks while a retry is in flight and announce state changes through the existing live region.

### Error-language boundary

Conversation copy reports what happened, whether money or holdings may have changed, and the next safe action. Reason codes, HTTP details, exception messages, and service names appear only in Evidence diagnostics.

### Evidence surface

Remove the inherited dark-console collision and use one coherent light surface based on the committed banking tokens. All normal text reaches 4.5:1; large text, icons, focus indicators, and component boundaries reach 3:1. Status remains distinguishable by icon and label, not color alone.

Lead with a compact current-run summary: proposed action, approval state, settlement state, amount, account, and receipt identity. Put ledger detail, diagnostics, playbook probes, and history in progressively disclosed native sections.

## Phase 3: Optimize and polish

Measure the repaired module graph, then reduce sequential discovery with generated `modulepreload` links unless a repository-supported production bundler provides a smaller and safer change. Keep HTML non-cacheable and give versioned/static assets suitable immutable or revalidation headers without making development stale.

Perform a final responsive and accessibility pass at desktop and 390px widths. Verify keyboard order, visible focus, dialog focus management, reduced motion, no horizontal overflow, text and non-text contrast, startup failure, loading states, and the current-run Evidence hierarchy.

## Testing strategy

Use test-driven changes: add or update a failing contract before each behavior change, make the minimum implementation change, and rerun the narrow test first. At phase boundaries run the complete Dimon frontend/UI suite plus API tests affected by static delivery. Phase 3 includes a browser smoke pass and before/after request-count and loading measurements.

## Scope boundaries

This work does not change banking-domain rules, enable real payment rails, replace the existing visual identity, or introduce a new frontend framework. Existing unrelated worktree changes in API tests are preserved and excluded from these edits.

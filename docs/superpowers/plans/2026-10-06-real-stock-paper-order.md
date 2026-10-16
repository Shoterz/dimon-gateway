# Real-Stock Paper Order Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a completed real-stock conversation into an immutable, explicitly authorized, exactly-once paper order that updates isolated synthetic cash and holdings.

**Architecture:** Add focused real-equity contract and persistence modules beside the existing fictional investment engine. Consume `READY_FOR_DRAFT` conversations idempotently, reuse the mock Singpass and face/device primitives through stock-specific database methods, and keep quote refresh and execution policy deterministic. Payments, bills, and legacy investments remain independent.

**Tech Stack:** Python 3.12, dataclasses, `Decimal`, SQLite transactions, FastAPI/Pydantic, vanilla JavaScript modules, pytest, Yahoo Finance provider boundary.

**Spec:** `docs/superpowers/specs/2026-10-06-real-stock-paper-order-design.md`

## Global Constraints

- Research data is real and indicative; execution is always labeled paper/synthetic.
- The LLM may propose grounded intent but has no quote, approval, signing, database, or execution authority.
- Whole shares only; side is `BUY` for this slice.
- Money is stored as integer minor units; FX uses fixed-precision `Decimal` strings.
- An unqualified dollar allocation for a US-listed stock is treated as USD and visibly funded from SGD.
- Market orders default to `DAY`; limit orders require `DAY` or `GTC`.
- Market price protection is 200 basis points and may never exceed the user's allocation cap.
- The SGD fee is 8 basis points with a minimum of 50 minor units.
- An unapproved receipt expires after five minutes; `DAY` and `GTC` use
  separate authorized-order deadlines.
- `DAY` ends at exchange-local 4 p.m. for the current/next eligible weekday,
  capped at seven calendar days; demo `GTC` expires after 30 calendar days.
- Voice may draft or revise but may not approve or execute.
- Mock Singpass precedes face/device confirmation and all approvals bind the same receipt digest.
- Only `FILLED` debits synthetic cash or updates holdings.
- Real-stock failures must not break payments, bills, or the legacy fictional investment subsystem.
- Do not modify or delete `docs/FINAL_CHECKLIST.md`.

## Review Focus

- An allocation that leaves less than two percent spare must cap the signed market price at `allocation / quantity`, never exceed the user's stated allocation; Task 1 pins this calculation.
- A conversation-turn retry after the thread was consumed must return the same order instead of `CONVERSATION_TERMINAL`; Task 2 pins replay recovery.
- A provider returning `regular`, `pre`, `post`, an unknown state, or stale timestamps must normalize to safe execution states; Task 4 pins every case.
- A limit price in USD plus an SGD funding account must never mix minor units during affordability checks; Tasks 1 and 4 pin cross-currency arithmetic.
- A lost HTTP response or two concurrent execution requests must produce one trade, one debit, and the same stored result; Task 4 pins replay and concurrency.

---

## File structure

- `apps/dimon_live/paper_equity.py`: immutable contracts, enums, canonical receipt hashing, money/FX calculations, and pure fill decisions.
- `apps/dimon_live/paper_equity_store.py`: additive schema, ready-conversation consumption, order lifecycle, exactly-once fill transaction, cash entries, holdings, and public serialization.
- `apps/dimon_live/paper_equity_service.py`: provider refresh, receipt creation orchestration, approval orchestration, and stock-specific result envelopes.
- `apps/dimon_live/investment_conversation.py`: preserve provider/funding evidence needed to create a receipt.
- `apps/dimon_live/database.py`: initialize the stock schema and expose narrow Singpass/device helpers for paper-equity orders.
- `apps/dimon_live/app.py`: feature flag and stock order HTTP contracts.
- `apps/dimon_live/service.py`: construct the paper-equity service without changing payment routing.
- `apps/dimon_live/static/paper_equity_ui.mjs`: dynamic-currency review, pending, filled, and rejected cards.
- `apps/dimon_live/static/app.js`: convert a ready conversation into the order-review/authorization flow.
- `apps/dimon_live/static/invest_ui.mjs`: shared dynamic currency and Singpass display support.
- `apps/dimon_live/tests/test_paper_equity_contracts.py`: pure contract and arithmetic tests.
- `apps/dimon_live/tests/test_paper_equity_store.py`: schema, consumption, atomicity, replay, and holdings tests.
- `apps/dimon_live/tests/test_paper_equity_api.py`: endpoint, feature-flag, session, and approval-boundary tests.
- `apps/dimon_live/tests/test_paper_equity_ui.py`: browser-source contract tests for safe UI behavior.
- `apps/dimon_live/tests/test_stock_purchase_flow.py`: full market, limit, closed-market, insufficient-funds, and isolation scenarios.
- `docs/features/real-stock-transaction-flow.md`: concise living behavior guide.
- `docs/features/stock-contracts.md`: historical tracker reconciled only after all tests pass.

### Task 1: Immutable paper-equity contracts and calculations

**Files:**
- Create: `apps/dimon_live/paper_equity.py`
- Create: `apps/dimon_live/tests/test_paper_equity_contracts.py`

**Interfaces:**
- Consumes: `receipts.canonical_json`, `receipts.digest_canonical`, verified values preserved from `InvestmentConversationState`.
- Produces: `PaperOrderType`, `PaperTimeInForce`, `PaperOrderStatus`, `PaperInstrument`, `PaperQuoteSnapshot`, `PaperFxSnapshot`, `PaperEquityDraft`, `PaperEquityReceipt`, `PaperExecutionSnapshot`, `PaperFillDecision`, `build_receipt(draft, *, order_id, nonce, now)`, and `decide_fill(receipt, execution, *, now)`.

- [ ] **Step 1: Write failing contract tests**

Add tests named:

```python
def test_market_receipt_binds_canonical_cross_currency_terms_and_digest(): ...
def test_market_protection_never_exceeds_allocation_per_share(): ...
def test_limit_receipt_uses_the_explicit_limit_without_market_buffer(): ...
def test_receipt_rejects_fractional_or_zero_quantity(): ...
def test_receipt_rejects_binary_float_money_and_naive_timestamps(): ...
def test_fill_decision_never_compares_usd_minor_units_with_sgd_minor_units(): ...
```

Assert the NVDA example from the spec, including quantity `2`, quote price
`23706`, USD/SGD rate `1.2802`, fee floor `50`, 200-basis-point protection,
and a stable digest from equivalent canonical inputs.

- [ ] **Step 2: Run the tests and verify RED**

Run: `uv run pytest apps/dimon_live/tests/test_paper_equity_contracts.py -q`

Expected: FAIL during collection because `apps.dimon_live.paper_equity` does not exist.

- [ ] **Step 3: Implement the immutable contract types**

Create frozen dataclasses with explicit validation and these pure entry points:

```python
def build_receipt(
    draft: PaperEquityDraft,
    *,
    order_id: str,
    nonce: str,
    now: datetime,
) -> PaperEquityReceipt: ...

def decide_fill(
    receipt: PaperEquityReceipt,
    execution: PaperExecutionSnapshot,
    *,
    now: datetime,
) -> PaperFillDecision: ...
```

Normalize provider market states to `OPEN`, `CLOSED`, `PRE_MARKET`,
`POST_MARKET`, or `UNKNOWN`. Use `ROUND_HALF_UP` for money, `ROUND_DOWN` for
whole-share quantity, and canonical JSON for the digest.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `uv run pytest apps/dimon_live/tests/test_paper_equity_contracts.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the contract slice**

```bash
git add apps/dimon_live/paper_equity.py apps/dimon_live/tests/test_paper_equity_contracts.py
git commit -m "feat: define real equity paper order contracts"
```

### Task 2: Persist and consume one ready conversation

**Files:**
- Create: `apps/dimon_live/paper_equity_store.py`
- Create: `apps/dimon_live/tests/test_paper_equity_store.py`
- Modify: `apps/dimon_live/investment_conversation.py`
- Modify: `apps/dimon_live/database.py`
- Modify: `apps/dimon_live/tests/test_investment_conversation.py`
- Modify: `apps/dimon_live/tests/test_database.py`

**Interfaces:**
- Consumes: Task 1 contract types and canonical receipt builder; existing `conversation_threads`; existing `accounts` row `acct_savings_01`.
- Produces: `PAPER_EQUITY_SCHEMA`, `PaperEquityStore.consume_ready_conversation(...) -> PaperEquityReceipt`, `PaperEquityStore.get_order(...)`, `PaperEquityStore.supersede_for_revision(...)`, and `PaperEquityStore.portfolio(...)`.

- [ ] **Step 1: Write failing state-preservation tests**

Extend investment-conversation tests to assert that the private state preserved
for a ready draft includes `instrument_id`, `market_state`, quote source and
timestamp, FX pair/rate/source/timestamp, and `funding_account_id`, while the
generic API still exposes only the bounded public view.

- [ ] **Step 2: Run the conversation tests and verify RED**

Run: `uv run pytest apps/dimon_live/tests/test_investment_conversation.py -q`

Expected: FAIL because the required receipt evidence is absent.

- [ ] **Step 3: Preserve immutable provider and funding evidence**

Update `InvestmentConversationAdapter._state_from_brief()` and its validation;
increment the adapter version because persisted state semantics change. Do not
reintroduce the two-week range into spoken output.

- [ ] **Step 4: Write failing persistence and replay tests**

Add tests named:

```python
def test_initialize_adds_isolated_paper_equity_tables(): ...
def test_ready_conversation_is_consumed_with_exactly_one_receipt(): ...
def test_same_conversation_version_replays_the_same_receipt(): ...
def test_conflicting_session_user_or_version_is_rejected(): ...
def test_insufficient_current_balance_creates_no_receipt(): ...
def test_missing_investor_profile_pauses_without_creating_a_receipt(): ...
def test_receipt_and_conversation_consumption_roll_back_together(): ...
def test_legacy_investment_tables_are_not_written(): ...
```

- [ ] **Step 5: Run the store tests and verify RED**

Run: `uv run pytest apps/dimon_live/tests/test_paper_equity_store.py apps/dimon_live/tests/test_database.py -q`

Expected: FAIL because the schema and store do not exist.

- [ ] **Step 6: Implement the additive schema and transaction**

Create stock-specific order, trade, holding, and cash-entry tables. Include
`SUPERSEDED` as a non-executable terminal state for a receipt replaced by a
revision. Implement:

```python
def consume_ready_conversation(
    self,
    *,
    conversation_id: str,
    conversation_version: int,
    session_scope: str,
    user_id: str,
    request_id: str,
) -> PaperEquityReceipt: ...
```

Within one `BEGIN IMMEDIATE` transaction, return an existing unique
`(conversation_id, conversation_version)` receipt or validate the ready
thread, require the existing investor profile, re-check current cash, insert
the receipt, and change the thread to `CONSUMED`. Initialize
`PAPER_EQUITY_SCHEMA` from `Database.initialize()`.

- [ ] **Step 7: Run the focused tests and verify GREEN**

Run: `uv run pytest apps/dimon_live/tests/test_investment_conversation.py apps/dimon_live/tests/test_paper_equity_store.py apps/dimon_live/tests/test_database.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit persistence**

```bash
git add apps/dimon_live/investment_conversation.py apps/dimon_live/paper_equity_store.py apps/dimon_live/database.py apps/dimon_live/tests/test_investment_conversation.py apps/dimon_live/tests/test_paper_equity_store.py apps/dimon_live/tests/test_database.py
git commit -m "feat: persist one paper order per stock conversation"
```

### Task 3: Bind mock Singpass and device approval to paper orders

**Files:**
- Create: `apps/dimon_live/paper_equity_service.py`
- Modify: `apps/dimon_live/database.py`
- Modify: `apps/dimon_live/singpass.py`
- Modify: `apps/dimon_live/tests/test_paper_equity_store.py`
- Modify: `apps/dimon_live/tests/test_signed_approval.py`

**Interfaces:**
- Consumes: Task 2 stored order/receipt; existing `singpass.request`, `singpass.require_approved`, `singpass.consume`, `_require_device_signature`, and face challenge binding.
- Produces: `PaperEquityService.create_order(...)`, `request_singpass(...)`, and `issue_challenge(...)`, plus stock-specific `Database` methods that bind Singpass and the pending device challenge to the stored receipt digest.

- [ ] **Step 1: Write failing approval-boundary tests**

Add tests named:

```python
def test_voice_or_text_confirmation_cannot_authorize_a_paper_order(): ...
def test_paper_order_requires_mock_singpass_before_device_challenge(): ...
def test_singpass_approval_is_bound_to_paper_order_digest(): ...
def test_changed_or_expired_receipt_invalidates_every_approval(): ...
```

- [ ] **Step 2: Run the approval tests and verify RED**

Run: `uv run pytest apps/dimon_live/tests/test_paper_equity_store.py apps/dimon_live/tests/test_signed_approval.py -q`

Expected: FAIL because stock-specific authorization methods do not exist.

- [ ] **Step 3: Generalize only the reusable Singpass display fields**

Keep the signed Singpass tuple `(auth_id, user_id, order_id, order_digest,
uinfin, status, decided_at)` unchanged. Allow stock-specific title and SGD
maximum debit to be supplied by the trusted server. Do not make Singpass know
Yahoo Finance or foreign-currency calculations.

- [ ] **Step 4: Add stock-specific database approval methods**

Implement narrow methods:

```python
def request_paper_equity_singpass_authorisation(...): ...
def issue_paper_equity_challenge(...): ...
```

They must load the stored receipt, compare the digest, check KYC/profile/read
back/risk requirements, and require approved Singpass before issuing the
face/device challenge. This task creates no execution method and consumes no
approval; Task 4 performs those steps in one lifecycle transaction.

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run: `uv run pytest apps/dimon_live/tests/test_paper_equity_store.py apps/dimon_live/tests/test_signed_approval.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit authorization binding**

```bash
git add apps/dimon_live/paper_equity_service.py apps/dimon_live/database.py apps/dimon_live/singpass.py apps/dimon_live/tests/test_paper_equity_store.py apps/dimon_live/tests/test_signed_approval.py
git commit -m "feat: bind paper equity orders to explicit approval"
```

### Task 4: Execute, queue, or open paper orders exactly once

**Files:**
- Modify: `apps/dimon_live/paper_equity.py`
- Modify: `apps/dimon_live/paper_equity_store.py`
- Modify: `apps/dimon_live/paper_equity_service.py`
- Modify: `apps/dimon_live/database.py`
- Modify: `apps/dimon_live/tests/test_paper_equity_contracts.py`
- Modify: `apps/dimon_live/tests/test_paper_equity_store.py`

**Interfaces:**
- Consumes: authorized stored receipt, `MarketProvider.snapshot`, `MarketProvider.fx_estimate`, Task 1 `decide_fill`.
- Produces: `PaperEquityStore.apply_execution(order_id, digest, snapshot, ...)`, lifecycle results `FILLED`, `OPEN`, `QUEUED`, `REQUOTE_REQUIRED`, `EXPIRED`, `CANCELLED`, `REJECTED`, and combined paper portfolio view.

- [ ] **Step 1: Write failing fill-policy tests**

Add tests proving:

```python
def test_open_market_order_fills_within_both_signed_caps(): ...
def test_closed_pre_and_post_market_orders_queue_without_cash_mutation(): ...
def test_unknown_or_stale_market_state_rejects_without_mutation(): ...
def test_limit_order_fills_at_or_below_limit_and_otherwise_opens(): ...
def test_price_or_fx_beyond_signed_cap_requires_requote(): ...
def test_day_and_gtc_expiry_rules_are_deterministic(): ...
def test_face_and_device_signature_are_bound_to_the_same_digest(): ...
def test_singpass_is_consumed_once_when_authorized_state_commits(): ...
def test_fill_posts_balanced_cash_entries_and_updates_whole_share_holding(): ...
def test_execution_replay_returns_one_trade_and_one_debit(): ...
def test_concurrent_execution_creates_exactly_one_trade(): ...
def test_failure_rolls_back_trade_cash_holding_and_singpass_consumption(): ...
```

- [ ] **Step 2: Run the execution tests and verify RED**

Run: `uv run pytest apps/dimon_live/tests/test_paper_equity_contracts.py apps/dimon_live/tests/test_paper_equity_store.py -q`

Expected: FAIL because execution persistence is incomplete.

- [ ] **Step 3: Implement provider refresh and pure decision mapping**

`PaperEquityService` loads the server-owned symbol, refreshes price and FX
through the provider, rejects stale/unknown data, and constructs a
`PaperExecutionSnapshot`. No client-supplied quote is accepted.

- [ ] **Step 4: Implement the atomic lifecycle transaction**

`PaperEquityStore.apply_execution(...)` must use `BEGIN IMMEDIATE`, re-check
status/digest/approval deadline/order deadline/cash, return existing terminal
results idempotently, and write trade, balanced cash entries, holding, and
`FILLED` state in one commit. `OPEN` and `QUEUED` atomically consume the
approval and update status only; their later refresh does not ask the user to
approve unchanged signed terms again. Requote and rejection write no cash or
holding.

Add `Database.approve_paper_equity_order(...)` and
`PaperEquityService.approve_and_execute(...)` here so device verification,
Singpass consumption, and `FILLED`/`OPEN`/`QUEUED` state commit together.

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run: `uv run pytest apps/dimon_live/tests/test_paper_equity_contracts.py apps/dimon_live/tests/test_paper_equity_store.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit execution**

```bash
git add apps/dimon_live/paper_equity.py apps/dimon_live/paper_equity_store.py apps/dimon_live/paper_equity_service.py apps/dimon_live/database.py apps/dimon_live/tests/test_paper_equity_contracts.py apps/dimon_live/tests/test_paper_equity_store.py
git commit -m "feat: execute real equity paper orders exactly once"
```

### Task 5: Wire the conversation and HTTP contracts

**Files:**
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/service.py`
- Modify: `apps/dimon_live/tests/test_conversation_api.py`
- Create: `apps/dimon_live/tests/test_paper_equity_api.py`
- Modify: `apps/dimon_live/tests/test_public_boundary.py`

**Interfaces:**
- Consumes: Tasks 2–4 `PaperEquityService`; current conversation turn and voice-session results.
- Produces: `ORDER_CONFIRMATION_REQUEST` when a ready conversation is consumed and `/api/paper-equity/orders/{id}` read/authorize/challenge/execute/cancel/refresh/revise routes.

- [ ] **Step 1: Write failing API tests**

Add tests named:

```python
def test_buy_now_turn_returns_one_paper_order_confirmation(): ...
def test_same_turn_request_replays_the_same_order(): ...
def test_voice_turn_and_typed_turn_use_the_same_completion_path(): ...
def test_missing_profile_opens_profile_flow_then_retries_the_same_ready_version(): ...
def test_feature_flag_disables_only_paper_execution_routes(): ...
def test_order_cannot_be_read_or_authorized_from_another_session(): ...
def test_execute_route_ignores_client_price_fx_fee_and_status_fields(): ...
def test_cancel_and_refresh_obey_terminal_state_rules(): ...
def test_revision_supersedes_the_receipt_and_reopens_the_same_context(): ...
def test_stock_provider_failure_does_not_break_payment_or_bill_routes(): ...
```

- [ ] **Step 2: Run the API tests and verify RED**

Run: `uv run pytest apps/dimon_live/tests/test_conversation_api.py apps/dimon_live/tests/test_paper_equity_api.py apps/dimon_live/tests/test_public_boundary.py -q`

Expected: FAIL because the paper-equity HTTP boundary is absent.

- [ ] **Step 3: Construct the feature-flagged service**

Read `DIMON_REAL_STOCK_EXECUTION_ENABLED` with exact-zero disable semantics.
Construct `PaperEquityService` from the session database and existing stock
provider. Do not change the payment/bill service constructor contract.

- [ ] **Step 4: Convert ready conversation results into order review**

After a typed or voice turn reaches `READY_FOR_DRAFT`, call
`consume_ready_conversation`. Overlay the public result with:

```json
{
  "outcome": "ORDER_CONFIRMATION_REQUEST",
  "reason_code": "PAPER_EQUITY_APPROVAL_REQUIRED",
  "paper_order": {},
  "answer": "I prepared a paper order. Review and confirm it on screen."
}
```

On replay, return the existing order. If execution is disabled, preserve the
current non-executing `READY_FOR_DRAFT` response.

- [ ] **Step 5: Add stock-specific Pydantic request models and routes**

Expose only receipt ID/digest and signed approval material. Validate session
scope server-side and attach speech only to concise result text. The revision
route accepts one text/choice turn, marks the current receipt `SUPERSEDED`,
invalidates its approvals, and restores the original investment conversation
to `WAITING` at a new version before applying the revision. A later
`READY_FOR_DRAFT` version creates a new order and digest.

- [ ] **Step 6: Run the focused tests and verify GREEN**

Run: `uv run pytest apps/dimon_live/tests/test_conversation_api.py apps/dimon_live/tests/test_paper_equity_api.py apps/dimon_live/tests/test_public_boundary.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit API wiring**

```bash
git add apps/dimon_live/app.py apps/dimon_live/service.py apps/dimon_live/tests/test_conversation_api.py apps/dimon_live/tests/test_paper_equity_api.py apps/dimon_live/tests/test_public_boundary.py
git commit -m "feat: expose paper equity purchase lifecycle"
```

### Task 6: Render dynamic-currency review and safe voice behavior

**Files:**
- Create: `apps/dimon_live/static/paper_equity_ui.mjs`
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/invest_ui.mjs`
- Modify: `apps/dimon_live/static/app.css`
- Create: `apps/dimon_live/tests/test_paper_equity_ui.py`
- Modify: `apps/dimon_live/tests/test_ui_client.py`
- Modify: `apps/dimon_live/tests/test_fixture_voice_speech.py`

**Interfaces:**
- Consumes: Task 5 `paper_order` public contract and stock-specific endpoints.
- Produces: receipt review, Singpass/face/device sequence, queued/open/filled cards, dynamic USD/SGD formatting, and deliberate voice stop at approval.

- [ ] **Step 1: Write failing UI source-contract tests**

Add tests proving:

```python
def test_real_order_card_uses_quote_and_funding_currencies_not_format_sgd_everywhere(): ...
def test_confirmation_button_starts_singpass_then_face_device(): ...
def test_voice_yes_never_calls_an_approval_or_execution_endpoint(): ...
def test_automatic_microphone_reopen_stops_at_immutable_receipt(): ...
def test_filled_open_queued_and_requote_states_have_honest_copy(): ...
def test_input_and_order_result_remain_in_one_screenshot_region(): ...
```

- [ ] **Step 2: Run the UI tests and verify RED**

Run: `uv run pytest apps/dimon_live/tests/test_paper_equity_ui.py apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_fixture_voice_speech.py -q`

Expected: FAIL because the real-stock order renderer does not exist.

- [ ] **Step 3: Implement the dedicated paper-equity renderer**

Export `renderPaperEquityOrder`, `requestPaperEquityApproval`,
`refreshPaperEquityOrder`, and `cancelPaperEquityOrder`. Show paper label,
dynamic currencies, quantity/type/TIF, quote and FX freshness, fee, maximum SGD
debit, available balance, expiry, and digest short form. Add the explicit
FastAPI static route for `paper_equity_ui.mjs` with the same public-boundary
headers as the other JavaScript modules.

- [ ] **Step 4: Connect the explicit approval path**

Reuse the existing dialog sequence with stock-specific endpoints. Stop
automatic listening when `ORDER_CONFIRMATION_REQUEST` is rendered. Keep
**Speak request** available for an explicit change/cancel turn, which must
supersede rather than approve the current receipt.

- [ ] **Step 5: Implement result cards and portfolio refresh**

Render `FILLED` as paper fill, `OPEN`/`QUEUED` as no-cash-moved pending states,
and `REQUOTE_REQUIRED` as a new-review requirement. Merge paper holdings into
the investing panel with their quote currency and SGD cost basis.

- [ ] **Step 6: Run the focused tests and verify GREEN**

Run: `uv run pytest apps/dimon_live/tests/test_paper_equity_ui.py apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_fixture_voice_speech.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit the UI slice**

```bash
git add apps/dimon_live/app.py apps/dimon_live/static/paper_equity_ui.mjs apps/dimon_live/static/app.js apps/dimon_live/static/invest_ui.mjs apps/dimon_live/static/app.css apps/dimon_live/tests/test_paper_equity_ui.py apps/dimon_live/tests/test_ui_client.py apps/dimon_live/tests/test_fixture_voice_speech.py
git commit -m "feat: add real stock paper order review flow"
```

### Task 7: End-to-end scenarios, documentation reconciliation, and public verification

**Files:**
- Create: `apps/dimon_live/tests/test_stock_purchase_flow.py`
- Modify: `docs/features/real-stock-transaction-flow.md`
- Modify: `docs/features/stock-contracts.md`
- Modify: `apps/dimon_live/README.md`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: executable hero/edge-case proof, current feature tracker, environment/runbook instructions, and a verified public demo.

- [ ] **Step 1: Write failing end-to-end scenario tests**

Cover these user journeys with fixture providers and no network dependency:

```python
def test_market_open_purchase_reaches_one_filled_holding(): ...
def test_market_closed_purchase_queues_with_unchanged_cash(): ...
def test_fixed_limit_remains_open_above_limit(): ...
def test_relative_limit_normalizes_then_fills_at_or_below_limit(): ...
def test_insufficient_funds_can_use_available_balance_and_continue(): ...
def test_change_after_review_supersedes_receipt_and_approvals(): ...
def test_replay_concurrency_and_provider_failure_preserve_other_domains(): ...
```

- [ ] **Step 2: Run the end-to-end tests and verify RED or expose integration gaps**

Run: `uv run pytest apps/dimon_live/tests/test_stock_purchase_flow.py -q`

Expected: FAIL until all route and result contracts are connected.

- [ ] **Step 3: Fix only integration gaps required by the scenarios**

Do not add dividends, news, Project Holo, fractional shares, live brokerage,
or unrelated redesign work.

- [ ] **Step 4: Run the full DIMON suite**

Run: `uv run pytest apps/dimon_live/tests -q`

Expected: all tests pass. Report every pre-existing or new failure by name if
the suite is not green; do not omit unrelated failures.

- [ ] **Step 5: Reconcile the docs with verified behavior**

Update the concise feature document and the historical stock tracker using
test evidence. Add the feature flag and paper-order routes to the README.
Do not claim live execution, bank-grade biometrics, or user validation that
did not occur.

- [ ] **Step 6: Restart and verify the deployed service**

Restart `dimon.service`, then verify local health and
`https://dimon.stocksuite.app` with the public-boundary smoke path. Confirm the
running HTML/JS includes the paper-order renderer and that the API reports the
feature enabled.

Run: `systemctl restart dimon.service`

Expected: command succeeds and `systemctl is-active dimon.service` prints
`active`.

Run: `curl --silent --show-error --fail http://127.0.0.1:8890/api/health`

Expected: HTTP 200 JSON with the gateway ready and the paper-equity feature
enabled.

Run: `curl --silent --show-error --fail https://dimon.stocksuite.app/api/health`

Expected: HTTP 200 from the public tunnel with the same deployment identity.

- [ ] **Step 7: Commit verification and documentation**

```bash
git add apps/dimon_live/tests/test_stock_purchase_flow.py docs/features/real-stock-transaction-flow.md docs/features/stock-contracts.md apps/dimon_live/README.md
git commit -m "test: prove the complete paper stock purchase flow"
```

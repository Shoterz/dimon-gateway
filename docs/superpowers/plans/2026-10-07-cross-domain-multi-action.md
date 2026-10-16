# Cross-Domain Multi-Action Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare bill, saved-recipient transfer, and stock purchase actions from one request and show them together for review without dropping an action or bypassing approval.

**Architecture:** A bounded Qwen multi-action contract emits ordered action candidates with transcript evidence. Deterministic domain adapters validate and prepare every action before a coordinator atomically stores a grouped review and its child receipts. Existing per-domain approval endpoints remain authoritative; a parent group tracks each child action's status for the grouped UI.

**Tech Stack:** Python, Pydantic/JSON schema contracts, SQLite, FastAPI, vanilla JavaScript ES modules, existing Qwen/Nemotron/Kokoro providers.

**Spec:** `docs/superpowers/specs/2026-10-07-cross-domain-multi-action-design.md`

## Global Constraints

- Every candidate is grounded against the original user transcript; model values cannot replace missing evidence.
- Ambiguous, unsupported, or ungrounded actions block group preparation; no financial action executes on submission.
- Payment authentication tier uses the combined bill and friend-transfer amount from one request.
- Existing bill, transfer, and stock approval rules remain in force.
- Saving the group and child review records is atomic; later approvals and executions are independent across domains.
- Existing single-domain requests and saved-beneficiary batches remain backward compatible.

## Review Focus

- A planner returns only one of two requested actions; test that the exact count and order must match grounded action spans.
- A biller alias such as “popwater” or “water bill” is mistaken for a different biller; test exact registered-alias grounding and ambiguity refusal.
- An amount is associated with the wrong action in a sentence containing multiple amounts; test amount spans bind to the correct action evidence.
- Payment amounts fall below the authentication threshold separately but exceed it together; test that the combined payment tier applies to both payment approvals.
- One domain fails after another has prepared a review; test rollback leaves no partial group or orphan child receipt.

---

## File Map

- Create `apps/dimon_live/multi_action.schema.json`: strict model response schema for two or three ordered bill, transfer, and stock action candidates.
- Create `apps/dimon_live/multi_action.py`: bounded proposal validation, transcript evidence binding, and typed immutable candidate/preparation contracts.
- Modify `apps/dimon_live/model_adapter.py`: add the multi-action prompt and `MultiActionExtractor.extract(text)` provider adapter; amounts remain evidence strings, never model-calculated minor units.
- Modify `apps/dimon_live/inference.py` and `apps/dimon_live/provider_factory.py`: expose/inject the multi-action extractor in local, remote, and fixture provider modes.
- Modify `apps/dimon_live/service.py`: coordinate extraction, deterministic domain preparation, aggregate payment authentication, and grouped review creation before existing single-domain dispatch.
- Modify `apps/dimon_live/database.py`, `apps/dimon_live/receipts.py`, and the paper-equity store as needed: persist a parent review group and ordered child references with atomic creation and per-child state updates.
- Modify `apps/dimon_live/app.py`: expose group retrieval and update group status after child approvals while preserving existing approval endpoints.
- Create `apps/dimon_live/static/multi_action_review.mjs`; modify `apps/dimon_live/static/app.js` and `apps/dimon_live/static/app.css`: render a grouped review and refresh each item state after its existing approval flow.
- Create `apps/dimon_live/tests/test_multi_action.py`; modify `apps/dimon_live/tests/test_model_adapter.py`, `test_service.py`, `test_api.py`, `test_ui_client.py`, and the focused bill/stock contract tests.

## Task 1: Bounded Multi-Action Model Contract

**Files:**
- Create: `apps/dimon_live/multi_action.schema.json`
- Create: `apps/dimon_live/multi_action.py`
- Modify: `apps/dimon_live/model_adapter.py`
- Modify: `apps/dimon_live/inference.py`
- Modify: `apps/dimon_live/provider_factory.py`
- Test: `apps/dimon_live/tests/test_multi_action.py`
- Test: `apps/dimon_live/tests/test_model_adapter.py`

**Interfaces:**
- Produces: `MultiActionExtractor.extract(text: str) -> ExtractedIntent`, where `intent` has `contract_version: 1` and ordered `items` of two or three supported action candidates.
- Each item contains only its action kind, label/value evidence strings, and bounded intent fields; money is derived by server parsing, with no model-authored amount minor units or server IDs.
- Consumes: existing model transport, `ExtractedIntent`, bill, transfer, and stock field conventions.

- [ ] **Step 1: Add failing contract tests** named `test_multi_action_contract_preserves_item_order`, `test_multi_action_contract_accepts_two_or_three_supported_items`, and `test_multi_action_contract_rejects_extra_amount_and_invalid_evidence_fields`.
- [ ] **Step 2: Run tests to verify failure** with `UV_CACHE_DIR=/tmp/uv-cache uv run pytest apps/dimon_live/tests/test_multi_action.py apps/dimon_live/tests/test_model_adapter.py -q`.
- [ ] **Step 3: Define the strict schema and `validate_multi_action_intent(raw_output, *, text)`**, requiring two or three items and exact evidence substrings in source text; integrate the extractor into all provider modes.
- [ ] **Step 4: Run the focused contract tests**; expect all contract cases to pass while existing single-intent contract tests remain green.
- [ ] **Step 5: Commit** as `feat: add bounded multi-action intent contract`.

## Task 2: Deterministic Action Preparation and Evidence Binding

**Files:**
- Modify: `apps/dimon_live/multi_action.py`
- Modify: `apps/dimon_live/bill_payment_grounding.py` only if an adapter interface is needed
- Modify: `apps/dimon_live/stock_market.py` only if a pure preparation interface is needed
- Test: `apps/dimon_live/tests/test_multi_action.py`
- Test: `apps/dimon_live/tests/test_bill_payment_grounding.py`
- Test: `apps/dimon_live/tests/test_stock_market.py`

**Interfaces:**
- Consumes: validated `ExtractedIntent` from Task 1, original transcript, and existing server directories/grounding helpers.
- Produces: `prepare_multi_action(text: str, intent: Mapping[str, Any], *, bill_preparer, transfer_preparer, stock_preparer) -> MultiActionPreparation`, containing `items: tuple[PreparedAction, ...]` only when every candidate is ready, otherwise one `reason_code` and user-facing clarification; it performs no database mutation.

- [ ] **Step 1: Add failing preparation tests** named `test_prepare_multi_action_biller_transfer_and_stock`, `test_prepare_multi_action_binds_each_amount_to_its_action`, `test_prepare_multi_action_keeps_explicit_market_order`, and `test_prepare_multi_action_fails_closed_when_one_item_is_unresolved`.
- [ ] **Step 2: Run the new focused tests** and verify they fail because the coordinator is not implemented.
- [ ] **Step 3: Implement pure preparation** that binds exact evidence and amounts from the source transcript, uses bill directory statement data where policy allows, resolves saved recipients, and invokes stock quote/order validation without creating receipts.
- [ ] **Step 4: Run focused preparation tests**; expect supported groups to prepare all children and any invalid child to yield no prepared children.
- [ ] **Step 5: Commit** as `feat: ground multi-action candidates`.

## Task 3: Atomic Group Persistence and Payment Risk Tier

**Files:**
- Modify: `apps/dimon_live/database.py`
- Modify: `apps/dimon_live/receipts.py`
- Modify: `apps/dimon_live/paper_equity_store.py`
- Modify: `apps/dimon_live/service.py`
- Test: `apps/dimon_live/tests/test_multi_action.py`
- Test: `apps/dimon_live/tests/test_database.py`
- Test: `apps/dimon_live/tests/test_risk_policy.py`

**Interfaces:**
- Consumes: fully prepared children from Task 2.
- Produces: `Database.create_pending_multi_action_group(request_id: str, original_text: str, children: Sequence[PreparedAction], *, payment_auth_amount_minor: int) -> dict`, returning `MULTI_ACTION_CONFIRMATION_REQUEST`, parent group ID, ordered child review objects, and current child statuses.
- Produces: `Database.update_multi_action_item_status(group_id, item_id, status, result=None) -> dict` for existing approval handlers to update group state.

- [ ] **Step 1: Add failing persistence tests** named `test_multi_action_group_persists_all_children_in_order`, `test_multi_action_group_creation_rolls_back_all_children_on_failure`, `test_multi_action_group_updates_only_approved_child`, and `test_multi_action_group_uses_aggregate_bill_and_transfer_auth_tier`.
- [ ] **Step 2: Run focused tests** and verify missing group persistence and aggregate tier assertions fail.
- [ ] **Step 3: Add parent/item persistence and atomic create** in a single SQLite transaction; retain immutable child receipt digests and IDs, and store combined payment risk amount separately from stock.
- [ ] **Step 4: Integrate prepared bill/transfer/stock drafts** so only fully prepared groups are written; apply the combined payment threshold to both payment reviews and existing stock-specific authentication unchanged.
- [ ] **Step 5: Run database, risk, and multi-action tests**; expect rollback to leave no parent, child receipt, balance change, or order fill.
- [ ] **Step 6: Commit** as `feat: persist grouped action reviews atomically`.

## Task 4: Request Routing and Group Status API

**Files:**
- Modify: `apps/dimon_live/service.py`
- Modify: `apps/dimon_live/app.py`
- Test: `apps/dimon_live/tests/test_multi_action.py`
- Test: `apps/dimon_live/tests/test_api.py`
- Test: `apps/dimon_live/tests/test_conversation_api.py` if conversation action routes share group updates

**Interfaces:**
- Consumes: provider extractor, pure preparation, and group persistence from Tasks 1–3.
- Produces: `ActionService.process(...)` returns a grouped result before single-domain handlers can consume multi-action text; `GET /api/multi-actions/{group_id}` returns owner-scoped group state.
- Existing bill, transfer, and stock approval endpoints remain unchanged in request shape and update their parent group after success/failure.

- [ ] **Step 1: Add failing routing/API tests** named `test_three_domain_request_routes_to_group_instead_of_stock_only`, `test_single_domain_request_keeps_existing_route`, `test_multi_action_group_get_is_owner_scoped`, and `test_existing_child_approval_updates_only_its_group_item`.
- [ ] **Step 2: Run focused tests** and verify the reported request is currently routed only to stock.
- [ ] **Step 3: Route requests with multiple grounded action candidates to the coordinator**; only single-action text may fall through to existing single-domain dispatch.
- [ ] **Step 4: Add owner-scoped group retrieval and approval status updates** to bill, transfer, batch, and paper-equity approval paths without changing their approval contracts.
- [ ] **Step 5: Run multi-action service and API tests**; expect every requested action to appear, and each approval to update only its matching group item.
- [ ] **Step 6: Commit** as `feat: route and track grouped financial actions`.

## Task 5: Grouped Ask Review UI

**Files:**
- Create: `apps/dimon_live/static/multi_action_review.mjs`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/app.css`
- Test: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Consumes: `MULTI_ACTION_CONFIRMATION_REQUEST` response and `GET /api/multi-actions/{group_id}`.
- Produces: one grouped result card with ordered bill, transfer, and stock sections; each section keeps its existing review/approval callback and displays its own status.

- [ ] **Step 1: Add failing UI tests** named `test_multi_action_review_renders_each_domain_item`, `test_multi_action_approval_updates_only_matching_item`, `test_multi_action_failure_remains_visible_in_group`, and `test_blocked_multi_action_has_no_partial_review_cards`.
- [ ] **Step 2: Run focused UI tests** and verify grouped rendering is missing.
- [ ] **Step 3: Implement the grouped renderer and approval refresh** using the existing bill, transfer, batch, and stock control modules.
- [ ] **Step 4: Run UI tests and `node --check`**; expect complete group labels, independent status updates, and valid JavaScript syntax.
- [ ] **Step 5: Commit** as `feat: render grouped multi-action reviews`.

## Task 6: End-to-End Regression and Documentation

**Files:**
- Modify: `apps/dimon_live/tests/test_multi_action.py`
- Modify: `apps/dimon_live/tests/test_everyday_speech.py`
- Modify: `docs/policy/transaction-authentication.md`
- Modify: `apps/dimon_live/README.md`

- [ ] **Step 1: Add the exact screenshot transcript regression** with the three actions and assert all three domain cards are present, all amounts are correctly bound, and no transaction/order is committed before approval.
- [ ] **Step 2: Run the focused integration test and verify it fails** against single-domain routing.
- [ ] **Step 3: Add the agreed grouped-flow examples and combined payment-tier policy documentation.**
- [ ] **Step 4: Run the project test suite and static checks** named by repository guidance; record existing unrelated failures separately.
- [ ] **Step 5: Commit** as `docs: document grouped multi-action review`.

## Handoff

Use native inline execution in this workspace, as requested. After plan approval,
execute the tasks in order with `superpowers:executing-plans`; do not delegate.
The final review must cover unsupported or ambiguous action refusal, cross-domain
payment authentication aggregation, and the no-partial-group rollback behavior.

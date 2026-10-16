# Modular Conversation Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an isolated, server-owned multi-turn conversation core and an investment adapter that can refine a verified stock request through `READY_FOR_DRAFT` without creating or executing an order.

**Architecture:** A domain-neutral `ConversationService` coordinates versioned threads and turns stored in additive SQLite tables. Explicitly registered adapters own domain state and transitions; the initial investment adapter reuses the verified stock brief while existing payment, bill, clarification, receipt, and ledger paths bypass the new subsystem.

**Tech Stack:** Python 3.12, frozen dataclasses and protocols, SQLite, FastAPI/Pydantic, existing `Decimal`-based stock contracts, `unittest`, and `httpx`/FastAPI test clients.

**Spec:** `docs/superpowers/specs/2026-10-05-modular-conversation-core-design.md`

## Global Constraints

- The conversation core has no receipt-approval, Singpass, face-verification, device-signing, paper-broker, or ledger-write capability.
- `DIMON_CONVERSATIONS_ENABLED` defaults to `1`; setting it to `0` preserves the current one-turn behavior.
- Persist money and prices as decimal strings and timestamps as timezone-aware UTC ISO 8601 strings.
- Permit one active `WAITING` or `READY_FOR_DRAFT` thread per session scope, user, and domain; starting another supersedes the older active thread atomically.
- A state-changing investment turn resets expiry to exactly 15 minutes after the server clock; reasked and rejected turns do not extend expiry.
- Follow-up calculations reuse the thread's stored snapshot and FX rate; no market-provider call occurs during a follow-up.
- Relative buy limits must be from `0.01%` through `50.00%` below the stored reference price; absolute limits must be positive and use the instrument quote currency.
- A bound conversation error never falls through to general action routing.
- Starting conversation state after a successful stock brief is best-effort; resuming a known conversation is fail-closed.
- Existing payment, bill, amount-clarification, approval, and execution behavior must remain unchanged.

## Review Focus

- Two simultaneous turns with the same expected version: exactly one mutates state and the other receives `CONVERSATION_VERSION_CONFLICT` (Task 3).
- Reusing a request ID with different input: reject it as `CONVERSATION_IDEMPOTENCY_CONFLICT` without changing state (Task 2).
- An unrecognized follow-up while a payment-like phrase is present: reask inside the investment conversation and never route it as a payment (Tasks 4 and 6).
- Provider failure or a changed live quote after the initial brief: follow-ups still use the stored snapshot/FX values and do not call the provider (Task 4).
- Starting a new investment thread while an older one is ready but undrafted: supersede the old thread and reject later turns against it (Tasks 2 and 5).

---

## File structure

- Create `apps/dimon_live/conversation.py`: generic contracts, errors, adapter protocol, registry validation, and `ConversationService`.
- Create `apps/dimon_live/conversation_store.py`: additive SQLite schema and atomic thread/turn persistence.
- Create `apps/dimon_live/investment_conversation.py`: typed investment state, exact JSON codec, deterministic input parsing, and transitions.
- Modify `apps/dimon_live/database.py`: initialize/reset the additive conversation tables only.
- Modify `apps/dimon_live/service.py`: optionally open a conversation after the verified stock brief.
- Modify `apps/dimon_live/app.py`: construct the scoped service, expose conversation endpoints, and route optional voice context.
- Modify `apps/dimon_live/.env.example`: document the isolation flag.
- Create `apps/dimon_live/tests/test_conversation.py`: generic contracts and lifecycle service tests.
- Create `apps/dimon_live/tests/test_conversation_store.py`: schema, isolation, idempotency, expiry, supersession, and concurrency tests.
- Create `apps/dimon_live/tests/test_investment_conversation.py`: domain codec, parser, calculations, and state-machine tests.
- Create `apps/dimon_live/tests/test_conversation_api.py`: HTTP, session-boundary, feature-flag, voice-routing, and failure-isolation tests.
- Modify `apps/dimon_live/tests/test_stock_market.py`: stock-brief start integration assertions.
- Modify `docs/features/stock-contracts.md`: reconcile the implementation tracker and mark the conversation-core milestone accurately.

### Task 1: Generic conversation contracts and adapter boundary

**Files:**
- Create: `apps/dimon_live/conversation.py`
- Create: `apps/dimon_live/tests/test_conversation.py`

**Interfaces:**
- Consumes: only Python standard-library types.
- Produces: `ConversationStatus`, `InputKind`, `ConversationInput`, `ConversationThread`, `ConversationTurn`, `ConversationTransition`, `StartContext`, `TurnContext`, `ConversationAdapter`, and `ConversationRejected` for all later tasks.

- [ ] **Step 1: Write failing contract tests**

Add tests named:

```python
def test_text_input_requires_nonblank_bounded_text(): ...
def test_choice_input_requires_name_and_json_parameters(): ...
def test_transition_rejects_execution_or_approval_fields(): ...
def test_thread_public_envelope_omits_state_and_handler_internals(): ...
def test_adapter_registry_rejects_duplicate_domain_registration(): ...
def test_conversation_input_rejects_canonical_payload_over_4096_bytes(): ...
```

Assert text is stripped and limited to 500 characters, choices contain a
nonblank uppercase name plus a JSON-compatible mapping, and public envelopes
contain only `conversation_id`, `domain`, `status`, `version`, `question`,
`allowed_choices`, and `expires_at`. Reject a text/choice canonical JSON
payload larger than 4,096 UTF-8 bytes before it reaches an adapter.

- [ ] **Step 2: Run the contract tests and confirm the module is missing**

Run: `uv run python -m unittest apps.dimon_live.tests.test_conversation -v`

Expected: FAIL because `apps.dimon_live.conversation` does not exist.

- [ ] **Step 3: Implement the generic dataclasses, enums, validation, and protocol**

Create these exact public signatures in `conversation.py`:

```python
class ConversationStatus(str, Enum): ...
class InputKind(str, Enum): ...

class ConversationRejected(RuntimeError):
    reason_code: str

@dataclass(frozen=True)
class ConversationInput:
    kind: InputKind
    text: str | None = None
    choice: str | None = None
    parameters: Mapping[str, JSONValue] = field(default_factory=dict)
    def canonical_json(self) -> str: ...

@dataclass(frozen=True)
class ConversationThread: ...

@dataclass(frozen=True)
class ConversationTurn: ...

@dataclass(frozen=True)
class ConversationTransition:
    state: Mapping[str, JSONValue]
    status: ConversationStatus
    outcome: str
    answer: str
    question: str | None
    allowed_choices: tuple[str, ...]
    public_view: Mapping[str, JSONValue]
    advance_version: bool = True

@dataclass(frozen=True)
class StartContext:
    now: datetime
    payload: object

@dataclass(frozen=True)
class TurnContext:
    now: datetime
    session_scope: str
    user_id: str

class ConversationAdapter(Protocol): ...

def build_adapter_registry(
    adapters: Iterable[ConversationAdapter],
) -> Mapping[str, ConversationAdapter]: ...
```

`ConversationAdapter` exposes `domain`, `name`, `version`, `start()`,
`apply_turn()`, and `render_public()` exactly as defined by the specification.
Reject transition mappings containing keys named `receipt`, `approval`,
`execution`, or `ledger_mutation` at the core boundary.

- [ ] **Step 4: Run the contract tests**

Run: `uv run python -m unittest apps.dimon_live.tests.test_conversation -v`

Expected: PASS.

- [ ] **Step 5: Commit the contracts**

```bash
git add apps/dimon_live/conversation.py apps/dimon_live/tests/test_conversation.py
git commit -m "feat: define conversation contracts"
```

### Task 2: Atomic SQLite conversation store

**Files:**
- Create: `apps/dimon_live/conversation_store.py`
- Create: `apps/dimon_live/tests/test_conversation_store.py`
- Modify: `apps/dimon_live/database.py:1-35,407-430,550-585`

**Interfaces:**
- Consumes: Task 1's `ConversationThread`, `ConversationTurn`, `ConversationStatus`, and `ConversationRejected`.
- Produces: `CONVERSATION_SCHEMA` and `ConversationStore` with `create_thread()`, `get_thread()`, `commit_turn()`, and `get_turn()` for Task 3.

- [ ] **Step 1: Write failing schema and lifecycle tests**

Add tests named:

```python
def test_database_initialize_creates_conversation_tables(): ...
def test_starting_thread_supersedes_waiting_and_ready_thread_in_same_domain(): ...
def test_threads_in_other_domains_are_not_superseded(): ...
def test_cross_session_and_cross_user_reads_return_not_found(): ...
def test_identical_request_replays_stored_turn(): ...
def test_changed_input_with_same_request_id_conflicts_without_mutation(): ...
def test_commit_turn_compare_and_swap_allows_only_one_writer(): ...
def test_expired_thread_is_persisted_as_expired(): ...
def test_demo_reset_removes_turns_before_threads(): ...
```

Use two threads whose statuses are `WAITING` and `READY_FOR_DRAFT` to prove
both are superseded. In the concurrency test, give two workers the same
thread version and assert one version advances while the other receives
`CONVERSATION_VERSION_CONFLICT`.

- [ ] **Step 2: Run the store tests and confirm they fail**

Run: `uv run python -m unittest apps.dimon_live.tests.test_conversation_store -v`

Expected: FAIL because the schema and store do not exist.

- [ ] **Step 3: Implement schema initialization and reset integration**

Define `CONVERSATION_SCHEMA` with the exact two tables and partial active
index from the specification. Update `Database.initialize()` to execute it.
Update `Database.reset()` to delete `conversation_turns` before
`conversation_threads`, without changing existing reset ordering otherwise.

- [ ] **Step 4: Implement `ConversationStore`**

Create:

```python
class ConversationStore:
    def __init__(
        self,
        path: Path,
        clock: Callable[[], datetime],
    ) -> None: ...

    def create_thread(
        self,
        *,
        session_scope: str,
        user_id: str,
        domain: str,
        handler_name: str,
        handler_version: int,
        transition: ConversationTransition,
        expires_at: datetime,
    ) -> tuple[ConversationThread, bool]: ...

    def get_thread(
        self,
        conversation_id: str,
        session_scope: str,
        user_id: str,
    ) -> ConversationThread: ...

    def commit_turn(
        self,
        *,
        thread: ConversationThread,
        request_id: str,
        source: str,
        conversation_input: ConversationInput,
        transition: ConversationTransition,
        expires_at: datetime,
    ) -> tuple[ConversationThread, ConversationTurn, bool]: ...

    def get_turn(
        self,
        conversation_id: str,
        request_id: str,
    ) -> ConversationTurn | None: ...
```

The boolean from `create_thread()` reports whether an older active thread was
superseded. The returned boolean from `commit_turn()` is `True` only for an
idempotent replay. Generate IDs as `conversation_` plus 32 lowercase hex
characters and `turn_` plus 32 lowercase hex characters. Use `BEGIN
IMMEDIATE`, canonical input comparison, a conditional
`UPDATE ... WHERE version = ?`, and a single transaction for the thread and
turn writes. Convert an elapsed active thread to `EXPIRED` before rejecting
it. Return `CONVERSATION_NOT_FOUND` for a scope mismatch.

- [ ] **Step 5: Run focused store and database tests**

Run: `uv run python -m unittest apps.dimon_live.tests.test_conversation_store apps.dimon_live.tests.test_database -v`

Expected: PASS.

- [ ] **Step 6: Commit the store**

```bash
git add apps/dimon_live/conversation_store.py apps/dimon_live/database.py apps/dimon_live/tests/test_conversation_store.py
git commit -m "feat: persist conversation threads"
```

### Task 3: Conversation lifecycle service

**Files:**
- Modify: `apps/dimon_live/conversation.py`
- Modify: `apps/dimon_live/tests/test_conversation.py`

**Interfaces:**
- Consumes: Task 1's adapter protocol and Task 2's `ConversationStore`.
- Produces: `ConversationService.start()`, `get()`, and `apply_turn()` for service, HTTP, and voice integration.

- [ ] **Step 1: Write failing lifecycle tests with a fake adapter**

Add tests named:

```python
def test_start_creates_public_waiting_thread_with_fifteen_minute_expiry(): ...
def test_apply_turn_updates_state_and_resets_expiry(): ...
def test_reasked_turn_keeps_version_and_expiry(): ...
def test_terminal_thread_rejects_another_turn(): ...
def test_adapter_exception_leaves_thread_unchanged(): ...
def test_missing_and_mismatched_handler_versions_fail_closed(): ...
def test_two_service_instances_cannot_commit_same_expected_version(): ...
def test_public_results_use_bounded_conversation_trace_stages(): ...
```

The fake adapter must expose no financial behavior. For the race test, load
version 1 twice, apply distinct turns concurrently, and assert one stored
thread reaches version 2 while the other call fails with
`CONVERSATION_VERSION_CONFLICT`.

- [ ] **Step 2: Run the lifecycle tests and confirm missing methods**

Run: `uv run python -m unittest apps.dimon_live.tests.test_conversation -v`

Expected: FAIL because `ConversationService` does not exist.

- [ ] **Step 3: Implement `ConversationService`**

Add:

```python
class ConversationService:
    def __init__(
        self,
        store: ConversationStore,
        adapters: Mapping[str, ConversationAdapter],
        clock: Callable[[], datetime],
        ttl: timedelta = timedelta(minutes=15),
    ) -> None: ...

    def start(
        self,
        *,
        domain: str,
        session_scope: str,
        user_id: str,
        payload: object,
    ) -> dict[str, JSONValue]: ...

    def get(
        self,
        conversation_id: str,
        *,
        session_scope: str,
        user_id: str,
    ) -> dict[str, JSONValue]: ...

    def apply_turn(
        self,
        conversation_id: str,
        *,
        session_scope: str,
        user_id: str,
        request_id: str,
        expected_version: int,
        source: str,
        conversation_input: ConversationInput,
    ) -> dict[str, JSONValue]: ...
```

Validate handler identity/version before parsing state. Validate the allowed
generic lifecycle transitions. Wrap unknown adapter exceptions as
`CONVERSATION_HANDLER_FAILED` without committing. Return the persisted
answer, question, choices, public domain view, version, expiry, and an
`idempotent_replay` marker. Emit only these public trace stages:
`CONVERSATION_STARTED`, `CONVERSATION_TURN_APPLIED`,
`CONVERSATION_REASKED`, `CONVERSATION_CANCELLED`,
`CONVERSATION_READY_FOR_DRAFT`, and `CONVERSATION_SUPERSEDED`. Map rejected
operations to their exact bounded reason code as the trace stage. Treat
`conversation_turns.request_id`, `outcome`, and public `response_json` as the
conversation audit record; never persist raw domain state in a turn response.

- [ ] **Step 4: Run contract, lifecycle, and store tests**

Run: `uv run python -m unittest apps.dimon_live.tests.test_conversation apps.dimon_live.tests.test_conversation_store -v`

Expected: PASS.

- [ ] **Step 5: Commit the lifecycle service**

```bash
git add apps/dimon_live/conversation.py apps/dimon_live/tests/test_conversation.py
git commit -m "feat: coordinate conversation lifecycle"
```

### Task 4: Investment conversation adapter

**Files:**
- Create: `apps/dimon_live/investment_conversation.py`
- Create: `apps/dimon_live/tests/test_investment_conversation.py`

**Interfaces:**
- Consumes: Task 1's adapter protocol and existing `stock_market.InvestmentBrief`, `MarketDataSnapshot`, `FxEstimate`, and `assess_funding()` semantics.
- Produces: `InvestmentConversationState` and `InvestmentConversationAdapter` for Task 5.

- [ ] **Step 1: Write failing exact-codec and start-state tests**

Add tests named:

```python
def test_state_round_trip_preserves_decimal_strings_and_utc_timestamp(): ...
def test_underfunded_brief_starts_at_funding_repair_question(): ...
def test_funded_brief_starts_at_order_type_question(): ...
def test_malformed_or_unknown_state_version_is_rejected(): ...
```

Assert JSON never stores money as a binary float. Assert underfunded choices
are `CHANGE_ALLOCATION`, `USE_AVAILABLE_BALANCE`, and `CANCEL`; funded choices
are `BUY_AT_MARKET`, `SET_LIMIT`, and `CANCEL`.

- [ ] **Step 2: Write failing transition tests**

Add tests named:

```python
def test_buy_it_now_and_button_both_reach_market_day_ready_state(): ...
def test_absolute_limit_asks_for_day_or_gtc_then_reaches_ready(): ...
def test_relative_limit_accepts_bounds_and_rejects_values_outside_them(): ...
def test_use_explicit_lower_amount_rebuilds_funding_from_stored_fx(): ...
def test_use_available_balance_rebuilds_whole_shares_from_stored_fx(): ...
def test_follow_up_never_calls_market_provider(): ...
def test_cancel_is_terminal_from_each_waiting_question(): ...
def test_unrecognized_payment_like_text_reasks_without_mutation(): ...
```

Use the existing CTSH/NVDA fixture values. Inject a provider that raises on
every method after the start state has been produced, then prove all follow-up
tests still pass from stored snapshot and FX values.

- [ ] **Step 3: Run the adapter tests and confirm the module is missing**

Run: `uv run python -m unittest apps.dimon_live.tests.test_investment_conversation -v`

Expected: FAIL because the adapter module does not exist.

- [ ] **Step 4: Implement the state codec and public rendering**

Create:

```python
@dataclass(frozen=True)
class InvestmentConversationState:
    contract_version: int
    brief: stock_market.InvestmentBrief
    order_type: str
    absolute_limit_price: Decimal | None
    relative_limit_percent: Decimal | None
    time_in_force: str
    question_kind: str

    def to_contract(self) -> Mapping[str, JSONValue]: ...
    @classmethod
    def from_contract(cls, value: Mapping[str, object]) -> InvestmentConversationState: ...

class InvestmentConversationAdapter:
    domain = "INVESTMENT"
    name = "investment-conversation"
    version = 1
```

Write an explicit codec for every nested stock contract. Do not call
`InvestmentBrief.to_dict()` because that public serializer converts Decimal
values to floats.

- [ ] **Step 5: Implement deterministic input parsing and transitions**

Implement `start()`, `apply_turn()`, and `render_public()` from the generic
protocol. Text recognition is limited to cancellation, market-order wording,
positive absolute maximum prices, `0.01%`-through-`50.00%` relative buy
limits, `DAY`, `GTC`, explicit revised allocations, and available-balance
wording. All choices call the same private transition functions used by text.

Use only the persisted market price and FX rate to recompute allocation,
whole shares, spend, cash remainder, funding requirement, and shortfall.
Return a reask transition with `advance_version=False` for unrecognized or
out-of-range input.

- [ ] **Step 6: Run adapter and existing stock-contract tests**

Run: `uv run python -m unittest apps.dimon_live.tests.test_investment_conversation apps.dimon_live.tests.test_stock_market -v`

Expected: PASS.

- [ ] **Step 7: Commit the investment adapter**

```bash
git add apps/dimon_live/investment_conversation.py apps/dimon_live/tests/test_investment_conversation.py
git commit -m "feat: add investment conversation adapter"
```

### Task 5: Open conversations from verified stock briefs

**Files:**
- Modify: `apps/dimon_live/service.py:140-170,1493-1568`
- Modify: `apps/dimon_live/app.py:349-535`
- Modify: `apps/dimon_live/tests/test_stock_market.py`
- Modify: `apps/dimon_live/tests/test_conversation.py`

**Interfaces:**
- Consumes: Task 3's `ConversationService.start()` and Task 4's `InvestmentConversationAdapter`.
- Produces: an optional top-level `conversation` envelope on `REAL_STOCK_BRIEF` responses and a scoped `conversation_service_for(context)` factory for Tasks 6 and 7.

- [ ] **Step 1: Write failing stock-start and isolation tests**

Add tests named:

```python
def test_real_stock_brief_opens_waiting_conversation(): ...
def test_new_stock_brief_supersedes_waiting_or_ready_investment_thread(): ...
def test_conversation_start_failure_keeps_verified_stock_brief_answered(): ...
def test_conversation_start_failure_does_not_affect_payment_request(): ...
def test_disabled_flag_preserves_one_turn_stock_response(): ...
```

The best-effort failure test injects a conversation service whose `start()`
raises and asserts the response remains `ANSWERED / REAL_STOCK_BRIEF` without
a `conversation` key. The payment regression must still create its ordinary
receipt choices.

- [ ] **Step 2: Run focused tests and confirm no envelope exists**

Run: `uv run python -m unittest apps.dimon_live.tests.test_stock_market apps.dimon_live.tests.test_conversation -v`

Expected: FAIL on the new conversation assertions.

- [ ] **Step 3: Extend `ActionService` with optional conversation start**

Add optional constructor parameters:

```python
conversation_service: ConversationService | None = None
session_scope: str = "legacy"
```

After funding assessment and before returning `_real_stock_brief`, call
`start(domain="INVESTMENT", session_scope=self.session_scope,
user_id=DEMO_USER_ID, payload=brief)`. Add the resulting public envelope at
top level. Catch `ConversationRejected` and unexpected subsystem errors only
at this best-effort start boundary, append a non-sensitive
`CONVERSATION_START_FAILED` trace stage, and retain the stock answer.

- [ ] **Step 4: Add explicit application construction and isolation flag**

In `create_app()`, evaluate `DIMON_CONVERSATIONS_ENABLED` once as enabled
unless its stripped value is exactly `0`. Add `conversation_service_for()`
that creates a store for `context.database.path`, registers only
`InvestmentConversationAdapter`, and passes `context.session_id` into
`ActionService`. When disabled, pass no conversation service.

- [ ] **Step 5: Run stock, service, and payment API tests**

Run: `uv run python -m unittest apps.dimon_live.tests.test_stock_market apps.dimon_live.tests.test_service apps.dimon_live.tests.test_api -v`

Expected: PASS.

- [ ] **Step 6: Commit stock integration**

```bash
git add apps/dimon_live/service.py apps/dimon_live/app.py apps/dimon_live/tests/test_stock_market.py apps/dimon_live/tests/test_conversation.py
git commit -m "feat: open stock conversation threads"
```

### Task 6: HTTP follow-up and resume endpoints

**Files:**
- Modify: `apps/dimon_live/app.py:190-340,519-570`
- Create: `apps/dimon_live/tests/test_conversation_api.py`

**Interfaces:**
- Consumes: Task 5's scoped service factory and Task 3's `get()` and `apply_turn()` methods.
- Produces: `GET /api/conversations/{conversation_id}` and `POST /api/conversations/{conversation_id}/turns`.

- [ ] **Step 1: Write failing endpoint and session tests**

Add tests named:

```python
def test_three_ordered_http_turns_reach_ready_for_draft(): ...
def test_text_and_equivalent_choice_produce_same_transition(): ...
def test_get_resumes_persisted_public_state_without_raw_state_json(): ...
def test_cross_session_conversation_access_returns_not_found(): ...
def test_version_and_idempotency_conflicts_return_http_409(): ...
def test_expired_conversation_returns_http_410(): ...
def test_handler_failure_returns_http_503_without_action_fallback(): ...
def test_unrecognized_payment_like_turn_reasks_and_creates_no_payment_receipt(): ...
```

The three-turn test uses: set an absolute limit, answer `GTC`, then verify the
terminal public state is `READY_FOR_DRAFT`; use a separate reask before those
two state-changing turns so three turn rows exist.

- [ ] **Step 2: Run endpoint tests and confirm routes are missing**

Run: `uv run python -m unittest apps.dimon_live.tests.test_conversation_api -v`

Expected: FAIL with route-not-found responses.

- [ ] **Step 3: Add strict Pydantic request models**

Define discriminated `ConversationTextInputRequest` and
`ConversationChoiceInputRequest`, plus:

```python
class ConversationTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_version: Annotated[int, Field(ge=1)]
    input: Annotated[
        ConversationTextInputRequest | ConversationChoiceInputRequest,
        Field(discriminator="kind"),
    ]
```

Limit text to 500 stripped characters, choice names to uppercase letters and
underscores, and parameters to a JSON object with at most 16 keys.

- [ ] **Step 4: Implement the resume and turn endpoints**

Use the resolved `SessionContext` for both calls. Derive source as `typed` for
text and `button` for choices. Map `CONVERSATION_NOT_FOUND` to 404,
`CONVERSATION_EXPIRED` to 410, version/idempotency/terminal conflicts to 409,
and handler/state failures to 503. Successful turn responses pass through
`attach_speech()`; GET responses do not synthesize audio.

- [ ] **Step 5: Run API, session, and payment regression tests**

Run: `uv run python -m unittest apps.dimon_live.tests.test_conversation_api apps.dimon_live.tests.test_sessions apps.dimon_live.tests.test_api -v`

Expected: PASS.

- [ ] **Step 6: Commit the HTTP boundary**

```bash
git add apps/dimon_live/app.py apps/dimon_live/tests/test_conversation_api.py
git commit -m "feat: expose conversation turn API"
```

### Task 7: Optional voice conversation routing

**Files:**
- Modify: `apps/dimon_live/app.py:960-990`
- Modify: `apps/dimon_live/tests/test_conversation_api.py`
- Modify: `apps/dimon_live/tests/test_stock_market.py:620-660`

**Interfaces:**
- Consumes: Task 6's turn conversion and Task 5's scoped conversation-service factory.
- Produces: optional `conversation_id` plus `expected_version` voice WebSocket query context; the existing no-query voice path remains unchanged.

- [ ] **Step 1: Write failing voice-context tests**

Add tests named:

```python
def test_voice_follow_up_uses_same_transition_as_typed_text(): ...
def test_voice_requires_conversation_id_and_version_together(): ...
def test_voice_without_conversation_context_remains_new_action(): ...
def test_voice_cross_session_conversation_is_not_found(): ...
```

Use a dispatching fake `VoiceBridge` so the tests do not require audio or the
Nemotron service. Assert voice is stored as the turn source and never invokes
`ActionService.process()` when conversation query parameters are present.

- [ ] **Step 2: Run voice tests and confirm they fail**

Run: `uv run python -m unittest apps.dimon_live.tests.test_conversation_api apps.dimon_live.tests.test_stock_market.StockMarketContractTests.test_voice_endpoint_runs_market_provider_off_realtime_event_loop -v`

Expected: FAIL on the new query-context behavior while the existing voice
test still passes.

- [ ] **Step 3: Route transcribed voice through the scoped conversation service**

Read `conversation_id` and `expected_version` from `websocket.query_params`.
Reject a partial pair with `CONVERSATION_CONTEXT_INVALID`. With a complete
pair, generate the request ID server-side and call `apply_turn(...,
source="voice", ConversationInput(kind=TEXT, text=transcript))` in
`asyncio.to_thread`. Without the pair, retain the current
`ActionService.process()` dispatch exactly.

- [ ] **Step 4: Run voice, endpoint, and stock tests**

Run: `uv run python -m unittest apps.dimon_live.tests.test_conversation_api apps.dimon_live.tests.test_voice apps.dimon_live.tests.test_stock_market -v`

Expected: PASS.

- [ ] **Step 5: Commit voice routing**

```bash
git add apps/dimon_live/app.py apps/dimon_live/tests/test_conversation_api.py apps/dimon_live/tests/test_stock_market.py
git commit -m "feat: route voice conversation turns"
```

### Task 8: Configuration, documentation, and full verification

**Files:**
- Modify: `apps/dimon_live/.env.example`
- Modify: `docs/features/stock-contracts.md`
- Test: all DIMON and repository tests.

**Interfaces:**
- Consumes: all previous tasks.
- Produces: documented rollout switch, reconciled tracker, and final verification evidence.

- [ ] **Step 1: Document the feature flag and completed boundary**

Add `DIMON_CONVERSATIONS_ENABLED=1` to `.env.example` with copy stating that
`0` disables only server-owned follow-up threads. In `stock-contracts.md`,
mark the verified-market work already completed, mark the conversation core
through `READY_FOR_DRAFT` as implemented, and leave stock receipt, approval,
paper execution, dividend, and real-broker items unchecked.

- [ ] **Step 2: Run the complete DIMON suite**

Run: `uv run python -m unittest discover -s apps/dimon_live/tests -p 'test_*.py'`

Expected: all tests pass.

- [ ] **Step 3: Run root tests and static verification**

Run: `uv run python -m unittest discover -s tests -p 'test_*.py'`

Expected: all tests pass.

Run: `uv run python -m compileall -q apps/dimon_live`

Expected: exit 0.

Run: `uv run python scripts/check-repository-boundary`

Expected: `repository boundary passed`.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 4: Exercise the local API boundary**

Start or reuse the local DIMON service, then verify one funded fixture-backed
conversation can be started, resumed, refined through at least two turns, and
read back as `READY_FOR_DRAFT`. Verify an ordinary payment still reaches its
existing receipt flow. Do not approve or execute either action as part of this
milestone.

- [ ] **Step 5: Commit configuration and documentation**

```bash
git add apps/dimon_live/.env.example docs/features/stock-contracts.md
git commit -m "docs: track conversation core rollout"
```

- [ ] **Step 6: Review the final diff and repository status**

Confirm only planned files and pre-existing user files are present. Do not add
or modify `docs/FINAL_CHECKLIST.md` unless the user separately requests it.

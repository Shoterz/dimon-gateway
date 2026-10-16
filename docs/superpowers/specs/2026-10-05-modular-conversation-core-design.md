# Modular conversation core design

**Status:** Approved in chat; awaiting written-spec review
**Date:** 2026-10-05
**Initial domain:** Real-equity investment conversations
**Future optional domains:** Payments and bills

## Purpose

DIMON currently handles most requests as one independent turn. The verified
stock path can resolve an instrument, retrieve research data, calculate a
whole-share allocation, check funding, and ask a question, but it cannot bind
the user's next reply to that stock context.

This design adds a small, server-owned conversation core. The core persists
threads and turns, applies optimistic concurrency, enforces expiry and
idempotency, and delegates domain meaning to explicitly registered adapters.
Investing is the first adapter. Payments and bills may use the core later, but
they remain fully functional if they never do.

The first implementation ends when an investment conversation reaches
`READY_FOR_DRAFT`. It does not create a stock receipt, request approval, or
execute an order.

## Success criteria

The milestone is successful when:

1. A verified real-stock brief creates a durable investment conversation.
2. Several subsequent voice, typed, or structured-choice turns can update the
   same server-owned context.
3. The server rejects cross-session access, stale concurrent updates, expired
   threads, replay conflicts, and turns after a terminal state.
4. A conversation failure cannot interrupt or reinterpret an unrelated
   payment, bill, or existing clarification request.
5. The investment adapter can reach `READY_FOR_DRAFT`, but nothing in the new
   core can approve a receipt or mutate a ledger.
6. Disabling the conversation feature restores the existing one-turn stock
   brief without changing other action routes.

## Non-goals

This milestone does not:

- create `EquityOrderDraft`, stock `ReviewReceipt`, or approval records;
- invoke mock Singpass, face verification, or device signing;
- submit or fill paper orders;
- generalize the existing payment clarification implementation;
- migrate payment or bill flows to conversations;
- add dividend research, news, Project Holo, or more instrument types;
- infer an active conversation from arbitrary unrelated text;
- give the language model access to conversation storage or execution tools.

## Architectural choice

Use a generic conversation envelope with typed, domain-owned state adapters.
This is a modular-monolith boundary rather than a universal financial state
machine or several unrelated conversation implementations.

```text
POST /api/actions ── verified stock brief ──> ConversationService
                                                   │
                                                   ▼
                                      ConversationStore (generic)
                                                   │
                                                   ▼
                                      InvestmentConversationAdapter

POST /api/conversations/{id}/turns ────────────────┘

Existing payment, bill, clarification, receipt, and ledger paths bypass this
core unless a future change explicitly registers a domain adapter for them.
```

## Module boundaries

### `conversation.py`

Owns domain-neutral contracts, enums, transition validation, the adapter
protocol, and `ConversationService`. It understands thread lifecycle but no
stock, payment, bill, receipt, or ledger fields.

### `conversation_store.py`

Owns SQLite persistence for threads and turns. Its mutations run in explicit
transactions and use compare-and-swap version updates. It has no dependency on
domain adapters.

### `investment_conversation.py`

Owns `InvestmentConversationState`, investment follow-up parsing, allowed
choices, public rendering, and state transitions. It may use the verified
stock-market contracts and provider boundary. It cannot approve or execute an
order.

### `service.py` and `app.py`

`service.py` asks the investment adapter to open a conversation after it has
built and funding-checked a real-stock brief. `app.py` exposes the read and
turn endpoints and passes the authenticated session scope into the
conversation service. Existing action routing remains unchanged.

## Generic contracts

### `ConversationThread`

```python
@dataclass(frozen=True)
class ConversationThread:
    conversation_id: str
    session_scope: str
    user_id: str
    domain: str
    handler_name: str
    handler_version: int
    status: ConversationStatus
    version: int
    state_json: str
    last_response_json: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
```

`state_json` is opaque to the core. The registered domain adapter must parse
and validate it before every transition. The raw state is never returned by a
generic API serializer.

### `ConversationStatus`

```text
WAITING
READY_FOR_DRAFT
CANCELLED
SUPERSEDED
EXPIRED
CONSUMED
```

Only `WAITING` accepts another user turn in this milestone.
`READY_FOR_DRAFT` is deliberately non-executing and terminal for the first
implementation. A later order-drafting slice may consume it exactly once and
move it to `CONSUMED`.

### `ConversationTurn`

```python
@dataclass(frozen=True)
class ConversationTurn:
    turn_id: str
    conversation_id: str
    request_id: str
    source: str
    input_kind: str
    input_json: str
    before_version: int
    after_version: int
    outcome: str
    response_json: str
    created_at: datetime
```

`source` is `typed`, `voice`, or `button`. `input_kind` is `TEXT` or
`CHOICE`. Reasked and rejected turns are recorded with the same before and
after version because they do not mutate domain state.

### `ConversationInput`

A turn accepts exactly one of:

```json
{"kind": "TEXT", "text": "make it GTC"}
```

or:

```json
{
  "kind": "CHOICE",
  "choice": "SET_TIME_IN_FORCE",
  "parameters": {"time_in_force": "GTC"}
}
```

The API validates the outer shape. The adapter validates choice names and
parameters. The raw text or choice is retained for auditability.

### `ConversationTransition`

An adapter returns a proposed transition containing:

- the next validated domain state;
- the next generic status;
- an outcome code;
- a concise answer and optional question;
- allowed structured choices;
- a domain-owned public view;
- whether the state version should advance.

The core validates allowed status changes before persistence. An adapter
cannot write its own thread or turn rows.

## Adapter protocol and registry

Adapters implement:

```python
class ConversationAdapter(Protocol):
    domain: str
    name: str
    version: int

    def start(self, context: StartContext) -> ConversationTransition: ...
    def apply_turn(
        self,
        state_json: str,
        conversation_input: ConversationInput,
        context: TurnContext,
    ) -> ConversationTransition: ...
    def render_public(self, state_json: str) -> dict: ...
```

The application constructs an explicit registry such as:

```python
{"INVESTMENT": InvestmentConversationAdapter(...)}
```

There is no filesystem discovery, import-time registration, or fallback to an
adapter from another domain. An unregistered domain returns
`CONVERSATION_DOMAIN_UNAVAILABLE`.

## Persistence schema

### `conversation_threads`

```sql
CREATE TABLE conversation_threads (
  conversation_id TEXT PRIMARY KEY,
  session_scope TEXT NOT NULL,
  user_id TEXT NOT NULL,
  domain TEXT NOT NULL,
  handler_name TEXT NOT NULL,
  handler_version INTEGER NOT NULL,
  status TEXT NOT NULL,
  version INTEGER NOT NULL,
  state_json TEXT NOT NULL,
  last_response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
```

A partial unique index permits only one active thread (`WAITING` or
`READY_FOR_DRAFT`) per session, user, and domain. Starting another investment
conversation marks an older waiting or ready-but-undrafted thread
`SUPERSEDED` in the same transaction. Threads in different domains may
coexist.

### `conversation_turns`

```sql
CREATE TABLE conversation_turns (
  turn_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversation_threads(conversation_id),
  request_id TEXT NOT NULL,
  source TEXT NOT NULL,
  input_kind TEXT NOT NULL,
  input_json TEXT NOT NULL,
  before_version INTEGER NOT NULL,
  after_version INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (conversation_id, request_id)
);
```

The store compares both the request ID and canonical input. Repeating an
identical request returns the stored response. Reusing the request ID with
different input returns `CONVERSATION_IDEMPOTENCY_CONFLICT`.

## Investment state contract

`InvestmentConversationState` contains:

```text
contract_version
resolved instrument
requested allocation and currency interpretation
funding account, balance, required amount, and shortfall
investment objective
verified market snapshot and snapshot capture time
order type: UNSPECIFIED | MARKET | LIMIT
absolute limit price, or relative percentage below market
time in force: UNSPECIFIED | DAY | GTC
missing fields
current question kind
```

Money and prices serialize as decimal strings. Datetimes serialize as UTC ISO
8601 strings. Provider objects are converted into repository-owned contracts
before persistence.

The first adapter supports these transitions:

| Current question | Accepted meaning | Result |
| --- | --- | --- |
| Funding repair | Set a lower amount and optional explicit currency | Rebuild brief and funding check |
| Funding repair | Use available funding balance | Rebuild allocation deterministically |
| Any waiting state | Cancel | `CANCELLED` |
| Order type | Buy now / market | Set `MARKET`, set `DAY`, reach `READY_FOR_DRAFT` when funded |
| Order type | Set an absolute maximum price | Set `LIMIT`; ask for `DAY` or `GTC` |
| Order type | Set a percentage below the reference price | Store the bounded relative instruction; ask for `DAY` or `GTC` |
| Time in force | `DAY` or `GTC` | Reach `READY_FOR_DRAFT` when funded |

Text parsing is deterministic and deliberately narrow. Structured button
choices invoke the same transition functions. Unrecognized text returns the
current focused question and allowed choices without changing state.

Conversational recalculation uses the snapshot and FX rate already stored in
the thread. It does not silently fetch a different price or rate between
questions. Absolute limits must be positive and use the instrument's quote
currency. Relative buy limits must be between `0.01%` and `50.00%` below the
stored reference price. The later order-drafting slice refreshes or validates
execution-critical data before creating a receipt.

The adapter stores an order instruction, not an executable order. The next
slice will refresh or validate the quote, apply tick-size rules, calculate the
final quantity, and create `EquityOrderDraft` from `READY_FOR_DRAFT`.

## Lifecycle and data flow

### Starting a conversation

1. `POST /api/actions` receives a new request as it does today.
2. The existing deterministic real-stock parser, resolver, provider, funding
   check, and response builder run unchanged.
3. If conversations are enabled and the investment adapter is registered,
   `ConversationService.start()` receives the verified brief.
4. The adapter creates validated `InvestmentConversationState` and its first
   transition.
5. The store supersedes an older waiting investment thread and inserts the new
   thread atomically.
6. The existing stock response gains a public `conversation` envelope.
7. If conversation creation fails, the stock brief remains a successful,
   non-executing answer and includes an internal trace stage; unrelated routes
   are unaffected.

### Applying a turn

1. The endpoint authenticates and resolves the same server-side session DB.
2. The core loads the thread by conversation ID, session scope, and user ID.
3. It checks terminal status, expiry, handler registration, handler version,
   request idempotency, and expected state version.
4. The adapter validates stored state and interprets the new input.
5. The core validates the proposed status transition.
6. The store inserts the turn and updates the thread with
   `WHERE version = expected_version` in one SQLite transaction.
7. The endpoint returns the persisted public envelope and domain view.

### Expiry and cancellation

Investment conversations expire 15 minutes after the most recent successful
state-changing turn. Reasked or rejected inputs do not extend expiry.
Cancellation is expressed as a normal turn and is idempotent. Expired,
cancelled, superseded, consumed, and ready-for-draft conversations do not
accept further turns.

## API contracts

### Initial action response addition

When a stock brief opens a thread, the existing response adds:

```json
{
  "conversation": {
    "conversation_id": "conversation_...",
    "domain": "INVESTMENT",
    "status": "WAITING",
    "version": 1,
    "question": "Buy at market, or set a maximum price?",
    "allowed_choices": ["BUY_AT_MARKET", "SET_LIMIT", "CANCEL"],
    "expires_at": "2026-10-05T09:15:00+00:00"
  }
}
```

The response omits internal state JSON, handler configuration, funding IDs,
and provider implementation objects.

### Apply a turn

```text
POST /api/conversations/{conversation_id}/turns
```

```json
{
  "request_id": "uuid",
  "expected_version": 1,
  "input": {"kind": "TEXT", "text": "set the maximum at 225 dollars"}
}
```

The successful response includes the public conversation envelope, answer,
question, allowed choices, and investment view. It does not include a receipt
or approval action. The server derives `typed` or `button` source metadata
from the validated input and route; it does not trust a client-supplied audit
source.

For voice follow-ups, the existing voice WebSocket accepts optional
`conversation_id` and `expected_version` query parameters. They must be
present together. After transcription, the server applies the text through
the same `ConversationService` transition used by the HTTP endpoint and
records the source as `voice`. Without those parameters, voice retains its
existing new-action behavior.

### Resume a thread

```text
GET /api/conversations/{conversation_id}
```

This returns the persisted public rendering for reconnect and refresh. Access
is restricted to the same authenticated session scope and user.

## Isolation and failure behavior

- Feature flag `DIMON_CONVERSATIONS_ENABLED` controls only this subsystem.
- Existing payment and bill action routes do not depend on the registry,
  conversation tables, or investment adapter.
- A recognized conversation turn never falls through to general action
  routing after an error.
- Starting a conversation is best-effort because the existing brief remains
  useful and non-executing. Resuming a known conversation is fail-closed.
- Adapter exceptions return `CONVERSATION_HANDLER_FAILED` and do not update
  thread state.
- Invalid stored domain state returns `CONVERSATION_STATE_INVALID`; the core
  does not attempt model-based repair.
- A missing or mismatched handler version returns
  `CONVERSATION_HANDLER_VERSION_UNAVAILABLE`.
- A stale expected version returns `CONVERSATION_VERSION_CONFLICT` with the
  current public thread version so the client can refresh.
- Cross-session or cross-user lookups return `CONVERSATION_NOT_FOUND` to avoid
  revealing another user's thread.
- No conversation response contains an execution claim.

## Security boundary

The conversation core coordinates intent refinement only. It cannot:

- create approval records;
- mark a receipt approved;
- call ledger mutation methods;
- invoke paper or real broker execution;
- turn an earlier approval into authorization for changed terms.

When order drafting is added, it will consume one exact
`READY_FOR_DRAFT` state and create an immutable receipt through the existing
receipt boundary. Mock Singpass authorization, face/liveness verification,
and device signing remain immediately before order submission. Any change to
a receipt-bound order field requires a new receipt and approval sequence.

## Test strategy

### Contract and storage tests

- round-trip generic and investment contracts;
- reject invalid statuses, versions, decimal strings, and datetimes;
- identical request replay returns the stored transition;
- changed input with the same request ID conflicts;
- compare-and-swap rejects two turns using the same expected version;
- state and turn update commit atomically;
- cancellation, supersession, terminal states, and expiry are enforced;
- cross-session and cross-user access return not found.

### Adapter tests

- market-order wording and the equivalent button reach the same state;
- absolute and relative limit instructions ask for time in force;
- `DAY` and `GTC` complete a funded limit instruction;
- allocation changes rebuild the verified brief and funding assessment;
- available-balance repair uses deterministic currency conversion;
- unrecognized replies reask without changing state or extending expiry;
- cancellation works from every waiting question;
- malformed persisted state is rejected without repair.

### Service and API tests

- a real-stock brief contains the new conversation envelope;
- the follow-up endpoint performs at least three ordered turns;
- typed, voice, and structured-choice inputs use the same transition path;
- reconnect returns the same public state;
- disabled feature flag preserves the current one-turn response;
- injected adapter/store failures do not break ordinary payment requests;
- a known conversation failure never reroutes the input as another action;
- all existing DIMON and repository test suites continue to pass.

## Rollout and observability

The feature ships enabled for local development and the synthetic demo, with
an environment flag for immediate isolation. Trace stages include conversation
start, turn applied, reasked, conflict, cancellation, expiry, supersession,
ready-for-draft, and subsystem failure. Audit records retain request IDs and
public outcomes but never expose raw domain state to the browser.

No data migration is required for existing transactions. New tables are
additive and can remain unused when the feature is disabled.

## Follow-on slice

After this milestone is verified, the next design will consume
`READY_FOR_DRAFT` to create a deterministic `EquityOrderDraft`, refresh or
validate execution-critical market data, build an immutable stock receipt,
run mock Singpass plus face/device approval, and submit exactly once to the
paper broker.

# Real-stock paper-order completion design

**Status:** Approved in chat; awaiting written-spec review
**Date:** 2026-10-06
**Scenario:** Voice-enabled personal wealth and investing transaction
**Scope:** Complete a user-directed whole-share equity purchase as a paper trade

## Purpose

DIMON can already resolve a company or ticker, retrieve verified Yahoo Finance
research, calculate a whole-share allocation, maintain a multi-turn investment
conversation, and reach `READY_FOR_DRAFT`. It deliberately stops before an
order exists.

This design completes that flow without pretending to be a live brokerage. A
finished conversation creates an immutable paper-order receipt. The user must
review it, approve it through mock Singpass, and complete face/device
confirmation before deterministic code may accept or fill the paper order.
Only a successful fill changes the synthetic investment cash and portfolio.

The stock subsystem remains optional and isolated. Payments and bills keep
their existing routes and ledgers, even if quote retrieval, paper execution,
or any stock-specific table is unavailable.

## Competition and product position

The DBS handbook makes domestic payment the safest core competition story and
describes equities as an optional extension. Therefore this feature is a
polished proof that DIMON's direct-conversation transaction architecture also
works for personal investing; it must not displace or destabilize the payment
hero flow.

At presentation time the team should state **Scenario 1: Voice-Enabled Payment
and Transaction**. The stock demonstration should be described as:

- real market research and instrument verification;
- conversational order drafting;
- immutable, user-reviewed transaction terms;
- mock identity and biometric authorization;
- paper/synthetic execution only.

No screen or narration may imply that DIMON is a licensed broker, that money
was sent to an exchange, or that Yahoo Finance is an execution venue.

## Success criteria

The milestone is complete when:

1. A conversation in `READY_FOR_DRAFT` creates at most one immutable paper
   order, even if the request is retried.
2. The receipt binds the security, side, quantity, order type, limit price when
   applicable, time in force, quote and FX snapshots, funding account,
   consideration, fees, maximum debit, expiry, version, nonce, and digest.
3. A spoken or typed follow-up may prepare or refine terms, but cannot approve
   or execute the order.
4. The user sees the exact receipt before mock Singpass and face/device
   confirmation.
5. Approval is cryptographically bound to the receipt digest. Any changed
   material term requires a new receipt and approval.
6. Execution is deterministic and exactly once. Replay, stale receipt,
   insufficient cash, excessive price movement, and concurrent execution are
   rejected without a partial mutation.
7. A filled order atomically debits synthetic SGD investment cash, records a
   paper trade, and updates the real-equity holding.
8. An open, queued, rejected, cancelled, or expired order does not change cash
   or holdings.
9. Turning off real-stock execution, or a failure inside it, leaves payments,
   bills, legacy synthetic investments, and their databases usable.
10. The UI and API consistently label the result as a paper trade.

## Non-goals

This milestone does not:

- connect to a live broker or exchange;
- move real money or custody real securities;
- recommend whether the user should buy the stock;
- support fractional shares, short selling, options, bonds, or dividends;
- implement background market-news or Project Holo analysis;
- use voice identity as transaction approval;
- make payments or bills depend on the conversation or stock subsystem;
- promise bank-grade face liveness or a hardware passkey on the Linux demo;
- build a complete long-running exchange simulator.

## Trust boundary

```text
voice / typed words
        |
        v
LLM extracts candidate intent and conversation meaning
        |
        v
deterministic instrument, quote, FX, cash, and policy checks
        |
        v
immutable paper-order receipt -------------------------------+
        |                                                     |
        v                                                     |
explicit on-screen review                                    |
        |                                                     |
        v                                                     |
mock Singpass approval bound to receipt digest               |
        |                                                     |
        v                                                     |
face + device signature bound to the same digest             |
        |                                                     |
        v                                                     |
paper execution transaction revalidates every bound field <--+
        |
        v
paper trade + synthetic cash + holding
```

The model has no database mutation, approval, signing, or execution
credential. It may propose structured terms, but deterministic code owns
instrument resolution, calculations, state transitions, receipt creation,
approval verification, and ledger writes.

## Architectural choice

Add an isolated real-equity paper-order module behind a narrow interface. Reuse
the existing conversation, mock Singpass, face/device, audit, and request
idempotency primitives. Do not insert Yahoo symbols into the fictional market
catalog and do not overload the legacy SGD-only investment tables with foreign
currency semantics.

```text
InvestmentConversationAdapter
        |
        | consume READY_FOR_DRAFT exactly once
        v
PaperEquityOrderService ---- QuoteProvider / FxProvider
        |                              |
        v                              v
paper_equity_orders              verified snapshots
        |
        +---- mock Singpass approval
        +---- face/device challenge
        |
        v
PaperExecutionGateway
        |
        +---- paper_equity_trades
        +---- paper_equity_holdings
        +---- paper_investment_cash_entries

Payments, bills, and legacy investments do not import this module.
```

Use a feature flag such as `DIMON_REAL_STOCK_EXECUTION_ENABLED`. When disabled,
the current verified stock brief and conversation may still run, but drafting
returns `PAPER_EXECUTION_UNAVAILABLE` and no other route changes behavior.

## End-to-end user flow

### 1. Draft through conversation

The user begins with a request such as “Put five hundred dollars into NVIDIA.”
DIMON presents the concise verified brief and asks whether to buy at market or
set a maximum price. Follow-up speech, typed text, or buttons update the same
server-owned conversation.

For the hero path, “Just buy now at market” produces whole-share market terms
with `DAY` time in force. Limit orders collect a maximum price and explicit
`DAY` or `GTC` time in force.

### 2. Create one immutable receipt

When the conversation reaches `READY_FOR_DRAFT`, the server consumes that
conversation version exactly once and creates an order in
`AWAITING_APPROVAL`. A retry returns the same order rather than a second order.
The response changes from the current informational terminal card to
`ORDER_CONFIRMATION_REQUEST` with the real-equity receipt.

The spoken response is short: “I prepared a paper order for eight NVIDIA
shares. Review and confirm it on screen.” It must not read every receipt field
or claim that an order has been placed.

### 3. Review and authorize

The on-screen card shows:

- paper-order label;
- company, ticker, exchange, and side;
- whole-share quantity;
- market or limit price terms;
- `DAY` or `GTC`;
- quote currency consideration;
- SGD funding estimate, fee, and signed maximum debit;
- quote and FX freshness;
- funding account and available balance;
- what happens while the market is closed;
- expiry and a concise risk statement.

The user deliberately selects **Confirm paper order**. Voice phrases such as
“yes,” “buy it,” or “go ahead” may navigate back to the review card but never
count as approval.

Confirmation starts mock Singpass approval. A successful Singpass record is
bound to the order ID, receipt digest, user, purpose, and expiry. The existing
face/device step follows immediately before execution and signs the same order
ID and digest. A changed digest invalidates both approvals.

### 4. Accept or fill the paper order

The execution transaction reloads the order, authorization records, current
cash, and current provider data. It then applies the deterministic fill rules
below.

- An open-session market order fills immediately if its verified price and
  funding debit remain within the signed maximum.
- A closed, pre-market, or post-market market order becomes `QUEUED`; it does
  not debit cash or create a holding.
- An open-session limit buy fills immediately only when the verified price is
  at or below the limit; otherwise it becomes `OPEN`.
- A closed-session limit buy becomes `OPEN`.
- An accepted but unfilled order is clearly shown as paper and pending.
- Only `FILLED` atomically changes cash, trade history, and holdings.

The first hackathon implementation does not need an autonomous exchange
worker. A deterministic “refresh paper order” operation may re-evaluate a
queued/open order on page load or explicit refresh. The same execution
idempotency key prevents duplicate fills. `DAY` orders expire when the recorded
eligible session ends; `GTC` orders use a documented demo expiry.

## Order states

```text
READY_FOR_DRAFT conversation
        |
        v
AWAITING_APPROVAL
        | cancel / receipt expiry
        +--------------------------> CANCELLED / EXPIRED
        |
        v
AUTHORIZED
        |
        +-- market closed ---------> QUEUED
        +-- limit not reached -----> OPEN
        +-- policy/data failure ---> REJECTED
        +-- eligible fill ---------> FILLED

QUEUED / OPEN -- refresh ----------> OPEN / FILLED / EXPIRED / CANCELLED
```

Terminal states are `FILLED`, `CANCELLED`, `EXPIRED`, and `REJECTED`. A
terminal order cannot be authorized or executed again.

## Bound receipt contract

The immutable receipt should contain at least:

```json
{
  "order_id": "ord_...",
  "conversation_id": "conv_...",
  "conversation_version": 4,
  "user_id": "demo-user",
  "funding_account_id": "savings",
  "instrument": {
    "instrument_id": "YF:NVDA",
    "symbol": "NVDA",
    "name": "NVIDIA Corporation",
    "exchange": "NasdaqGS",
    "quote_currency": "USD"
  },
  "side": "BUY",
  "quantity": 2,
  "order_type": "MARKET",
  "limit_price_minor": null,
  "time_in_force": "DAY",
  "quote_snapshot": {
    "snapshot_id": "...",
    "price_minor": 23706,
    "market_state": "OPEN",
    "captured_at": "..."
  },
  "fx_snapshot": {
    "snapshot_id": "...",
    "pair": "USD/SGD",
    "rate": "1.2802",
    "captured_at": "..."
  },
  "quote_consideration_minor": 47412,
  "estimated_funding_minor": 60700,
  "fee_funding_minor": 50,
  "max_quote_price_minor": 24180,
  "max_funding_debit_minor": 62000,
  "price_protection_basis_points": 200,
  "approval_expires_at": "...",
  "order_expires_at": "...",
  "nonce": "...",
  "receipt_version": 1,
  "digest": "sha256:..."
}
```

Integer minor units are required for money. Decimal FX rates must use a
fixed-precision decimal representation, never binary floating point. The
canonical receipt is serialized deterministically before hashing.

For a limit order, `limit_price_minor` replaces the market price as the user's
maximum price and `max_quote_price_minor` equals that limit. Fields not yet
known at drafting must not be invented.

## Price, FX, fee, and funding rules

### Ambiguous dollars

As in the current brief, an unqualified “$500” for a US-listed stock is treated
as a USD allocation. The receipt also shows the corresponding SGD funding
requirement because the demo savings account is SGD. This convention avoids a
clarification turn while keeping the currency assumption visible.

### Whole shares

Quantity is the greatest whole number that fits within the user allocation at
the reviewed price terms. Quantity must be at least one. If current SGD cash is
insufficient for the maximum funding debit, DIMON returns the existing concise
insufficient-funds response and does not create an approvable receipt.

### Market price protection

Market orders use the existing two-percent price-protection principle. The
receipt binds quantity, a maximum quote price, and a maximum SGD debit. The
maximum quote price is the lower of two percent above the reviewed quote or
the user's quote-currency allocation divided by the whole-share quantity. The
protection buffer therefore never authorizes spending beyond the allocation.
If a fresh execution price or FX rate would exceed either signed maximum,
execution returns `REQUOTE_REQUIRED`; it never silently reduces quantity or
increases the debit.

### Fees

Reuse the demo's deterministic fee policy in SGD: 0.08 percent with a minimum
of S$0.50. The exact estimated fee and maximum debit are visible and bound.
If the implementation chooses zero fees instead, the receipt must explicitly
say fees are excluded; it may not omit the field.

### Freshness

An unapproved receipt expires after five minutes, matching the existing order
TTL. That approval deadline is separate from the authorized order lifetime.
A `DAY` order expires at 4 p.m. in the exchange timezone for the current
eligible weekday when authorized during an open session, or the next weekday
when authorized outside it, capped at seven calendar days. A `GTC` paper order
expires after 30 calendar days. Execution requires fresh quote and FX data.
Provider failure or stale data returns a non-mutating retry state. A material
refresh creates a new receipt and invalidates prior approval.

## Persistence and atomicity

Use stock-specific tables so foreign-currency paper positions cannot corrupt
the fictional SGD catalog:

- `paper_equity_orders` stores immutable receipt JSON/digest plus lifecycle
  columns, separate approval/order deadlines, and unique
  `(conversation_id, conversation_version)`;
- `paper_equity_approvals` references the order and digest;
- `paper_equity_trades` has unique `order_id`, making one fill per order;
- `paper_equity_holdings` is keyed by user and instrument and records whole
  quantity, quote-currency cost basis, and SGD funding cost;
- `paper_investment_cash_entries` records double-entry funding mutations and
  has a unique idempotency key.

Receipt creation and conversation consumption occur in one SQLite transaction.
Fill execution uses `BEGIN IMMEDIATE`, compares the expected order version,
verifies all authorization and policy predicates, inserts the unique trade,
posts balanced cash entries, updates the holding, and marks the order `FILLED`
before commit. Any error rolls back the entire operation.

An execution retry returns the previously committed result. A request ID reused
with different payload returns `IDEMPOTENCY_CONFLICT`.

## API behavior

The conversation turn endpoint may return an `order` object in addition to the
updated conversation when `READY_FOR_DRAFT` is consumed. No client-provided
price, symbol metadata, cost, fee, digest, or status is trusted.

Minimum stock-specific operations are:

```text
POST /api/conversations/{conversation_id}/turns
  -> conversation + optional ORDER_CONFIRMATION_REQUEST

POST /api/paper-equity/orders/{order_id}/authorize
  -> mock Singpass challenge or authorization state

POST /api/paper-equity/orders/{order_id}/device-challenge
  -> digest-bound face/device challenge

POST /api/paper-equity/orders/{order_id}/execute
  -> FILLED, OPEN, QUEUED, REQUOTE_REQUIRED, or deterministic rejection

GET /api/paper-equity/orders/{order_id}
  -> server-owned public receipt and lifecycle state

POST /api/paper-equity/orders/{order_id}/cancel
  -> cancellation for an unfilled order
```

Existing generic approval endpoints may back these public routes internally,
but stock contracts must not inherit legacy assumptions that all instruments,
prices, fees, and balances are SGD.

## UI and voice behavior

The input and outcome remain close enough for one judge screenshot. The order
review appears directly beneath the conversation result. It uses dynamic
currency labels rather than the legacy `formatSgd` helper.

Buttons are primary for consequential choices:

- **Confirm paper order**
- **Change order**
- **Cancel**
- **Refresh order** for `OPEN` or `QUEUED`

Voice remains available for conversational refinement. After DIMON speaks, the
microphone may reopen for the next drafting turn. Once an immutable receipt is
shown, automatic listening stops so ambient speech cannot begin identity or
approval steps. The user can still press **Speak request** to say “change it to
a limit of 230,” which supersedes the old receipt and returns to drafting.

Spoken text uses TTS-specific wording such as “price to earnings ratio,”
“U.S. dollars,” and “Singapore dollars.” Visual currency notation remains
`US$` and `S$`.

## Risk profile and user-directed execution

The four-question investor profile is optional for a direct, self-directed
equity order. It remains available for suitability warnings and is used by
recommendation and investment-plan flows, but it must not block a user who
names an instrument and explicitly requests a purchase. Explicit order
approval and device/Singpass controls remain mandatory before execution.

This is a self-directed paper order, not an AI recommendation. The profile may
control warnings and review friction but cannot fabricate a suitability score
from Yahoo data. A generic equity-risk acknowledgment is recorded when policy
requires it. Risk level never bypasses explicit approval.

## Failure isolation

- Quote or FX failure affects only the current stock request.
- Stock table migration failure disables paper execution and keeps the app and
  non-stock routes running.
- No payment, bill, beneficiary, or legacy investment code imports the paper
  broker implementation.
- Each stock endpoint returns a stock-specific error envelope; it cannot fall
  through into recipient or bill interpretation.
- Circuit-breaking the quote provider cannot block ledger reads or payment
  settlement.
- Stock schema changes are additive and reversible.

## Security and responsible-AI requirements

- Treat the model output as untrusted proposed data.
- Resolve the symbol and retrieve every price through deterministic providers.
- Never put an execution capability or database credential in the model
  context.
- Bind approval to the canonical receipt digest.
- Reject prompt text that attempts to override policy, hidden instructions,
  account balance, price, quantity, or approval state.
- Log state transitions and rejection reasons without storing biometric video
  or unnecessary voice audio.
- Label Singpass, face liveness, broker, and execution as demo/mock/paper.
- Voice recognition is an accessibility/input signal, never an identity or
  authorization factor.

## Test strategy and judge evidence

The implementation plan must include tests proving:

- `READY_FOR_DRAFT` creates exactly one receipt across retries;
- market and limit terms are bound correctly;
- USD consideration, SGD funding, fee, and maximum debit use integer/decimal
  arithmetic;
- insufficient cash produces no receipt or ledger mutation;
- voice “yes” and “buy it” cannot approve;
- changed terms invalidate the previous digest and approvals;
- missing/expired Singpass or face/device proof blocks execution;
- market-closed orders queue without debiting cash;
- untriggered limits remain open without a holding;
- eligible orders fill atomically and exactly once;
- replay and concurrent execution create one trade only;
- stale quote, stale FX, and price outside protection require a new receipt;
- a stock-provider or stock-database failure does not break payment or bill
  tests;
- the public UI never labels a paper fill as a live trade.

Judge-facing evidence should show a concise latency trace, the immutable
receipt digest, explicit approval sequence, one replay/concurrency test, and
zero unauthorized writes in the finite safety suite. Any usability claim must
be based on actual sessions; do not invent user-validation results.

## Requirements mapping

| Source | Requirement | Design response |
|---|---|---|
| Competition handbook | Model drafts; deterministic system executes | LLM is restricted to intent and conversation proposals |
| Competition handbook | Exact review and explicit approval | Canonical receipt, on-screen review, Singpass, face/device |
| Competition handbook | Honest prototype boundaries | Real research, mock identity, paper execution labels |
| Competition handbook | Human-centered voice experience | Multi-turn speech for drafting; concise TTS; buttons for approval |
| Competition handbook | Technical proof and replay safety | Digest binding, idempotency, atomic fill, concurrency tests |
| `stock-contracts.md` | Brief to draft to receipt to approval to paper result | Implemented as the explicit lifecycle in this design |
| `stock-contracts.md` | Market, relative/fixed limit, DAY/GTC, whole shares | Market and fixed limit in this slice; relative limit is normalized before receipt creation |
| `approval-and-friction.md` | Risk changes friction, never removes consent | Profile/warnings may add friction; approval is always explicit |
| `identity-and-transaction-authorization.md` | Singpass before face/device for investments | Preserved and bound to the same digest |
| `voice-identity-and-noisy-audio.md` | Voice is input, not approval | Automatic listening stops at immutable receipt review |
| `prompt-injection-research.md` | No model execution authority | Provider, policy, approval, and database boundaries remain deterministic |
| `backend-edits-capability-map.md` | Reuse proven synthetic controls | Reuse approval/audit patterns, not fictional symbols or SGD-only tables |

## Documentation reconciliation after implementation

After the code and tests pass, update `docs/features/stock-contracts.md` in the
same change set:

- mark the now-implemented conversation, voice, receipt, approval, and paper
  execution stages accurately;
- replace stale two-week-range response examples with the current one-year
  comparison;
- record the exact price protection, fee, freshness, fill, and expiry rules;
- add the public test phrases and expected states;
- keep dividend, news, Project Holo, and live brokerage ideas in later phases.

The competition submission should separately preserve the required
CodeBuddy/WorkBuddy conversation history and screenshots. Codex development
history does not replace that artifact.

## Open review boundary

This document resolves the major architecture and policy choices needed for an
implementation plan. The next step, after user review, is a file-by-file plan
that sequences schema additions, order service contracts, API wiring, dynamic
currency UI, voice behavior, tests, service restart, and public verification.
No production code should be changed until this written design is accepted.

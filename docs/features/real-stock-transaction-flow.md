# DIMON real-stock transaction flow

**Last updated:** 2026-10-06
**Purpose:** Practical source of truth for the real-equity conversational demo
**Execution boundary:** Real research, paper orders, synthetic cash and holdings

This document records what the real-stock path does today, how its contracts
fit together, and the scenarios the end-to-end purchase flow must support. It
is intentionally shorter and more operational than `stock-contracts.md`.

## Current status

| Capability | Status | Notes |
|---|---|---|
| Natural-language stock request | Implemented | LLM extracts a grounded company label; deterministic code verifies it |
| Company-name to ticker resolution | Implemented | Yahoo Finance provider boundary; supports real listed symbols |
| Current research snapshot | Implemented | Price, market state, trailing P/E, one-year change and FX estimate |
| Whole-share allocation | Implemented | No fractional shares in this MVP |
| Funding check | Implemented | Compares estimated SGD requirement with the synthetic savings balance |
| Concise insufficient-funds response | Implemented | Stops before unnecessary research narration |
| Multi-turn order refinement | Implemented | Market versus limit, limit price, DAY/GTC, funding repair and cancel |
| Typed and button follow-ups | Implemented | Same server-owned conversation |
| LLM natural-language follow-up interpretation | Implemented | Bounded action proposal with deterministic validation and fallback |
| Voice follow-ups | Implemented | Audio response can reopen the microphone for the next drafting turn |
| Compact voice-intent history | Implemented | Prior spoken requests remain available without dominating the screen |
| TTS-specific text formatting | Implemented | Visual response and spoken response are separate representations |
| Immutable real-stock order receipt | Implemented | One receipt per ready conversation version; SHA-256 digest |
| Mock Singpass bound to real-stock receipt | Implemented | Stock-specific request and challenge routes |
| Paper execution and real-stock holding | Implemented | Exactly-once synthetic cash/holding writes |
| HTTP order lifecycle | Implemented | Read, refresh, Singpass, challenge, approve, cancel |
| On-screen paper-order review | Implemented | Voice drafts; approval requires deliberate screen/device flow |
| Live broker execution | Not planned | Outside the hackathon boundary |

## User-facing flow

```text
User intention
  “Put 500 dollars into NVIDIA”
        |
        v
Verified stock brief
  NVDA, price, market state, P/E, one-year change,
  whole shares, funding estimate
        |
        v
DIMON asks one useful next question
  “Buy now at market, or set a maximum price?”
        |
        +---- “Buy now” ------------------------------+
        |                                              |
        +---- “Set a maximum price” -> price -> DAY/GTC|
                                                       v
                                             READY_FOR_DRAFT
                                                       |
                                     immutable paper-order receipt
                                                       v
                                     immutable paper-order review
                                                       |
                                  mock Singpass -> face/device approval
                                                       |
                                                       v
                                      filled / open / queued / rejected
```

Conversational speech is allowed to create and refine the draft. It is not an
approval factor. A consequential transaction always stops at an exact visual
receipt and requires deliberate on-screen authorization.

## Architecture

### Research and intent

- `stock_market.py` owns the verified instrument and market-data contracts.
- The LLM may identify the company words the user used. It does not choose the
  ticker, price, quantity, currency conversion, order type, or approval state.
- Yahoo Finance supplies indicative research data, not an executable quote.
- `InvestmentBrief` is the immutable handoff from research into conversation.

### Conversation

- `conversation.py` owns the domain-neutral thread and turn contracts.
- `conversation_store.py` persists the server-owned thread, version,
  idempotency key, expiry, and bounded public response.
- `investment_conversation.py` owns investment-specific phases and choices.
- Payments and bills do not depend on this adapter.

#### Natural-language follow-ups

Text and voice replies are sent to the follow-up interpreter with the stored
phase, question, symbol, and quote context. The interpreter returns only the
versioned `INVESTMENT_FOLLOW_UP` contract:

```json
{
  "contract_version": 1,
  "kind": "INVESTMENT_FOLLOW_UP",
  "action": "BUY_NOW"
}
```

The action is a proposal, never an approval or execution command. The server
checks the proposal against the persisted phase before applying the existing
deterministic transition. A valid `UNKNOWN` is respected and causes a re-ask;
it is not silently replaced by a phrase match. Only a missing interpreter, a
model-unavailable failure, or invalid model output activates the bounded
deterministic fallback (for example, `buy at market when market opens`). This
keeps the allowlist as a resilience path rather than the normal language
understanding path. With the model available, phrases such as `execute at the
next opening bell` and `go ahead with the current price` can resolve to
`BUY_NOW`. Buttons still submit the exact enum directly and do not require
model interpretation.

The machine-readable schema is
`apps/dimon_live/investment_followup.schema.json` and is intentionally limited
to conversation actions; it contains no price, ticker, receipt, approval, or
execution fields.

### Order and execution boundary

The separate real-equity paper-order service reuses
the existing mock Singpass, face/device, audit, and idempotency patterns but
will not insert real tickers into the fictional market catalog or reuse its
SGD-only order assumptions.

```text
InvestmentBrief
  -> InvestmentConversationState
  -> PaperEquityOrderDraft
  -> PaperEquityReviewReceipt
  -> SingpassApproval + DeviceApproval
  -> PaperExecutionResult
```

Only the last transition may mutate synthetic cash and holdings.

When the conversation reaches `READY_FOR_DRAFT`, the server consumes that
conversation version exactly once and returns `ORDER_CONFIRMATION_REQUEST`
with `paper_order`. The order routes are:

```text
GET  /api/paper-equity/orders/{order_id}
POST /api/paper-equity/orders/{order_id}/refresh
POST /api/paper-equity/orders/{order_id}/singpass
POST /api/paper-equity/orders/{order_id}/challenge
POST /api/paper-equity/orders/{order_id}/approve
POST /api/paper-equity/orders/{order_id}/cancel
```

The approval body contains only the receipt digest and device assertion. The
server refreshes Yahoo Finance and FX data itself; client-supplied price, fee,
status, or cash values are ignored. `FILLED` writes one synthetic cash debit,
one trade, and one whole-share holding update. `OPEN`, `QUEUED`,
`REQUOTE_REQUIRED`, `REJECTED`, and `CANCELLED` write no investment cash.

If Yahoo omits its `marketState` label during the final refresh, the provider
uses the exchange's regular weekday hours as a conservative session fallback.
This prevents a missing informational field from becoming an unexplained
failure: a market order outside regular hours is recorded as `QUEUED` until a
later eligible session, while the normal price and funding protections still
apply.

## Multi-turn conversation contract

### Generic turn request

Text or speech becomes:

```json
{
  "request_id": "uuid",
  "expected_version": 2,
  "input": {
    "kind": "TEXT",
    "text": "Just buy now at market"
  }
}
```

A button becomes:

```json
{
  "request_id": "uuid",
  "expected_version": 2,
  "input": {
    "kind": "CHOICE",
    "choice": "BUY_NOW"
  }
}
```

The server rejects stale versions, conflicting reuse of a request ID,
cross-session access, expired conversations, and turns after a terminal state.

### Investment phases

| Phase | Question | Accepted result |
|---|---|---|
| `FUNDING` | Use available funds, another account, or cancel? | Repair allocation or stop |
| `ORDER_STYLE` | Market or maximum price? | Market terms or limit-price phase |
| `LIMIT_PRICE` | What maximum price? | Fixed price or percentage below market |
| `TIME_IN_FORCE` | DAY or GTC? | Complete limit terms |
| `READY` | None | Conversation reaches `READY_FOR_DRAFT` |

Examples of equivalent natural replies:

- `Buy now`, `market order`, `just buy now at market` -> market order.
- `Set the maximum at 230 dollars` -> fixed limit.
- `Five percent below the current price` -> deterministically calculated limit.
- `Today` -> `DAY`.
- `Good till cancelled` -> `GTC`.
- `Cancel` or `stop` -> no order.

## Visual text and audio text

The screen and the audio must carry the same facts, but they do not use the
same formatting.

### Visual form

```text
NVDA is US$237.06. Nasdaq is open. P/E: 30.0x.
It is up 26.4% from one year ago.
```

### Spoken form

```text
N V D A is 237 U.S. dollars and 6 cents.
Nasdaq is open. The price to earnings ratio is 30 times.
It is up 26.4 percent from one year ago.
```

TTS rules:

- say `price to earnings ratio`, without a hyphen;
- pronounce ticker letters separately when needed;
- say `U.S. dollars` and `Singapore dollars`, not `US dollar` or `S dollar`;
- expand percentages and currency symbols into natural speech;
- do not read timestamps, provider disclaimers, digests, or raw identifiers;
- do not read the removed two-week range;
- say `Nasdaq is open`, `Nasdaq is closed`, or `Nasdaq is currently in
  session`; never read raw provider state such as `regular` or `pre`;
- use a short spoken handoff once an order receipt exists: “I prepared a paper
  order for two NVIDIA shares. Review and confirm it on screen.”

Audio behavior:

1. User selects **Speak request** for the initial voice turn.
2. The system transcribes and processes the request.
3. The returned MP3 player is shown and begins playing automatically.
4. When playback finishes, the microphone reopens if the conversation still
   needs a drafting answer.
5. Once an immutable receipt is displayed, automatic listening stops.
6. The user may deliberately activate speech again to change or cancel terms.

## Contract templates

### 1. Verified investment brief

```json
{
  "instrument": {
    "symbol": "NVDA",
    "name": "NVIDIA Corporation",
    "exchange": "NasdaqGS",
    "quote_currency": "USD",
    "source": "YAHOO_FINANCE"
  },
  "market": {
    "captured_at": "timestamp",
    "price": "237.06",
    "market_state": "OPEN",
    "trailing_pe": "30.00",
    "one_year_change_pct": "26.40"
  },
  "requested_allocation": "500.00",
  "allocation_currency": "USD",
  "funding_currency": "SGD",
  "whole_shares": 2,
  "estimated_spend": "474.12",
  "unallocated_cash": "25.88",
  "funding_required": "607.00",
  "funding_balance": "1000.00",
  "funding_shortfall": "0.00"
}
```

Decimals remain decimals inside Python. JSON examples use strings to avoid
binary floating-point ambiguity.

### 2. Conversation-ready order terms

```json
{
  "conversation_id": "conversation_...",
  "conversation_version": 2,
  "instrument_id": "YF:NVDA",
  "side": "BUY",
  "quantity": 2,
  "order_type": "MARKET",
  "limit_price": null,
  "time_in_force": "DAY",
  "allocation_currency": "USD",
  "funding_currency": "SGD"
}
```

### 3. Immutable paper-order receipt

```json
{
  "order_id": "paper_order_...",
  "receipt_version": 1,
  "conversation_id": "conversation_...",
  "conversation_version": 2,
  "user_id": "demo-user",
  "funding_account_id": "acct_savings_01",
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
    "price_minor": 23706,
    "market_state": "OPEN",
    "captured_at": "timestamp"
  },
  "fx_snapshot": {
    "pair": "USD/SGD",
    "rate": "1.2802",
    "captured_at": "timestamp"
  },
  "quote_consideration_minor": 47412,
  "estimated_funding_minor": 60700,
  "fee_funding_minor": 50,
  "max_quote_price_minor": 24180,
  "max_funding_debit_minor": 62000,
  "price_protection_basis_points": 200,
  "status": "AWAITING_APPROVAL",
  "approval_expires_at": "timestamp",
  "order_expires_at": "timestamp",
  "nonce": "nonce_...",
  "digest": "sha256:..."
}
```

The digest is calculated from canonical server-owned data. Client-submitted
prices, fees, totals, balances, market state, and digests are never trusted.
The approval deadline is five minutes. It is distinct from the authorized
order deadline: `DAY` ends at the relevant exchange-local 4 p.m. session
deadline (with a seven-calendar-day safety cap for a closed-market queue), and
the demo `GTC` lifetime is 30 calendar days.

### 4. Paper execution result

```json
{
  "outcome": "FILLED_PAPER_EQUITY_ORDER",
  "order_id": "paper_order_...",
  "status": "FILLED",
  "paper_trade": true,
  "trade": {
    "symbol": "NVDA",
    "quantity": 2,
    "fill_price_minor": 23706,
    "quote_currency": "USD",
    "funding_debit_minor": 60750,
    "funding_currency": "SGD"
  },
  "portfolio": {
    "symbol": "NVDA",
    "whole_shares": 2
  }
}
```

## Scenario behavior

### A. Enough money, market order, market open

Input:

```text
Put 500 dollars into NVIDIA.
Just buy now at market.
```

Behavior:

1. Verify NVDA and current provider data.
2. Calculate two whole shares and SGD funding requirement.
3. Create one immutable market `DAY` receipt.
4. Show the paper-order review.
5. Require mock Singpass, then face/device confirmation.
6. Revalidate quote, FX, cash, digest, expiry, and price protection.
7. Fill exactly once and atomically debit synthetic cash/update the holding.

### B. Not enough money

Response style:

```text
NVDA is US$233.95. Your US$20,000 allocation needs about S$25,600.80,
but your savings balance is S$1,000. You’re short S$24,600.80.
Lower the amount?
```

Do not narrate P/E or one-year performance before stating that the requested
transaction is impossible. No receipt, approval, cash entry, or holding is
created. **Use available balance** may repair the same conversation.

### C. Market order while the market is closed

The brief says the market is closed and explains that a market order would
wait for an eligible session. After authorization, the paper order becomes
`QUEUED`; cash and holdings remain unchanged. Refreshing the order during an
open session may fill it once if it remains inside the signed protection.

### D. Fixed limit order

Input:

```text
Set a maximum price of 230 dollars.
Good till cancelled.
```

The receipt binds `LIMIT`, `23000` USD minor units, and `GTC`. If the open
market price is at or below US$230, it may fill. Otherwise it becomes `OPEN`.
An unfilled order never debits cash.

### E. Relative limit order

Input:

```text
Set it five percent below the current price.
```

The server converts the percentage into a fixed two-decimal limit using the
verified conversation price. The immutable receipt stores the fixed price,
not an instruction to recalculate the percentage later.

### F. Price or FX moved beyond the signed maximum

Execution returns `REQUOTE_REQUIRED`. The user receives a new receipt with a
new digest and must approve again. DIMON cannot silently increase the debit or
reduce the share quantity.

For a market order, the signed maximum price is the lower of two percent above
the reviewed quote or the user's allocation divided by the whole-share
quantity. Price protection must never authorize spending beyond the stated
allocation.

### G. User changes the order after review

**Change order** supersedes the receipt and returns to conversation drafting.
Any Singpass, face, or device approval bound to the old digest becomes invalid.

### H. Duplicate or concurrent execution

Retries return the previously committed result. Concurrent attempts create at
most one trade and one set of balanced cash entries.

### I. Provider failure or stale data

The stock action returns a stock-specific retry result and performs no
mutation. Payments, bills, account views, and the legacy synthetic catalog
continue working.

## State model

```text
WAITING conversation
  -> READY_FOR_DRAFT
  -> AWAITING_APPROVAL
  -> AUTHORIZED
     -> FILLED
     -> OPEN
     -> QUEUED
     -> REJECTED

AWAITING_APPROVAL -> CANCELLED / EXPIRED
OPEN / QUEUED -> FILLED / CANCELLED / EXPIRED
```

`FILLED`, `CANCELLED`, `EXPIRED`, and `REJECTED` are terminal.

## Public test script

Use these phrases after the paper-order slice is implemented:

1. `I want to put 500 dollars into NVIDIA.`
2. Say or click `Just buy now at market.`
3. Review the exact paper receipt.
4. Approve in mock Singpass.
5. Complete face/device confirmation.
6. Verify one fill and the updated NVDA holding when Nasdaq is open, or a
   queued order with unchanged cash when it is closed.
7. Retry the same execution and verify there is still only one trade.

Limit path:

1. `I have allocated 500 dollars and feel like investing it in NVIDIA.`
2. `Set it five percent below the current price.`
3. `Good till cancelled.`
4. Verify the fixed limit and `GTC` appear in the receipt.
5. Authorize and verify `OPEN` or `FILLED` according to the verified price.

Insufficient-funds path:

1. `Put 20,000 dollars into NVIDIA.`
2. Verify the concise shortfall response.
3. Select **Use available balance**.
4. Continue the repaired conversation without starting a second intention.

## Source documents

- `docs/superpowers/specs/2026-10-06-real-stock-paper-order-design.md` is the
  approved architecture.
- `docs/superpowers/plans/2026-10-06-real-stock-paper-order.md` is the tracked
  implementation sequence.
- `docs/features/stock-contracts.md` remains the historical idea and research
  backlog; it is no longer the concise day-to-day implementation guide.

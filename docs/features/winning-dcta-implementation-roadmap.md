# DIMON winning implementation roadmap

**Last updated:** 2026-10-06
**Purpose:** Living, one-item-at-a-time tracker for the competition-critical
Direct Conversational Transaction Agent experience
**Demo boundary:** Synthetic banking ledger, real market research, paper stock
orders, and deliberate on-screen approval

This document records the work that matters most for winning. Complete one
item, capture its evidence, and update its checkbox before starting the next
one. A larger feature count is not the goal. The goal is one memorable,
reliable demonstration in which a user speaks naturally, corrects the plan,
approves exact stored facts, and can see what changed.

## Working now

**Current priority:** Finish customer-visible `BILL_PAYMENT`, then implement
`INTERNAL_TRANSFER` and combine both with beneficiary transfer in the
heterogeneous banking hero flow.

The first genuine CodeBuddy contribution is preserved under
`docs/evidence/codebuddy/2026-10-06-bill-payment-contract.md`, including the
reviewed change, original session reference, passing tests, and three hashed
development screenshots.

The strict bill-payment proposal contract is connected to a dedicated Qwen
schema, deterministic transcript grounding, a server-owned synthetic biller
directory, and the existing action dispatcher. Exact biller matches produce a
read-only proposal; ambiguous matches offer explicit choices; unsupported
names ask the user to revise the biller. Missing biller, amount, or currency
also receives a clarification. Receipt construction, approval, and ledger
execution remain the next bill work.

## What matters most for winning

| Order | Deliverable | Status | Completion evidence |
|---:|---|---|---|
| 0 | Genuine CodeBuddy or WorkBuddy contribution evidence | [x] Complete | Original conversation reference, reviewed diff, tests, and three hashed development screenshots |
| 1 | Heterogeneous conversational transaction | [~] Bill extraction and grounding implemented; resolution/execution and internal transfer missing | One request creates a bill payment, beneficiary transfer, and internal transfer in one plan |
| 2 | Correction and receipt supersession | [~] Infrastructure exists; full hero flow not demonstrated | A spoken correction creates version two and makes version one impossible to approve |
| 3 | Polished voice state machine | [~] Core voice path exists | End-to-end recording shows listening, transcript, response, automatic follow-up listening, recovery, review, and completion |
| 4 | Attack and replay demonstration | [~] Defensive controls and recording script exist; release rehearsal pending | Tampering, stale approval, replay, and injection attempts visibly leave the ledger unchanged |
| 5 | Unified stock order and personal wealth hub | [~] Paper order APIs and holdings exist | One screen shows cash, commitments, holdings, orders, exposure, and activity |
| 6 | Measured evaluation | [~] Protocol and results card defined; measurements and five user sessions pending | Reproducible accuracy, refusal, security, and latency results |
| 7 | Dividend-investing mode | [ ] Missing | Verified dividend facts and estimated income are available without weakening order approval |

Do not mark `[x]` from code presence alone. Mark it complete only when the
customer-visible flow works and the named evidence has been captured.

## Winning hero flow

The primary competition demonstration should be broader than stock research:

> Pay my electricity bill S$120, send Mum S$500, and move S$1,000 into our
> joint account.

DIMON should:

1. Transcribe and visibly preserve the request.
2. Produce one versioned plan containing three different action types.
3. Resolve the biller, beneficiary, and owned account using server-owned data.
4. Ask only questions required to remove a real ambiguity.
5. Accept a natural correction such as, “Actually, make Mum's amount S$450.”
6. Create plan version two and visibly invalidate version one.
7. Show one exact review receipt, including every action, total debit, funding
   account, and remaining balance.
8. Require deliberate Singpass and device approval bound to that receipt.
9. Execute atomically: all actions commit once, or none of them commit.
10. Show and speak a short confirmation derived from the committed result.

### Hero-flow contracts to add

- `BILL_PAYMENT`
- `INTERNAL_TRANSFER`
- A heterogeneous `TRANSACTION_PLAN` containing multiple supported actions
- One plan-level validation decision and receipt digest
- One plan-level approval challenge
- One atomic execution result with per-action outcomes

The model may propose action labels and evidence spans. Deterministic code must
resolve canonical destinations, derive monetary values, validate ownership and
funding, construct the receipt, verify approval, and mutate the synthetic
ledger.

## Speech experience to implement

### Voice state machine

```text
IDLE
  -> LISTENING
  -> TRANSCRIBING
  -> PROCESSING
  -> SPEAKING
  -> WAITING_FOR_REPLY
  -> REVIEWING
  -> APPROVING
  -> COMPLETED
```

Every state must have visible feedback and a safe recovery path.

### Required speech behaviour

- [ ] Show a partial transcript while the user is speaking.
- [ ] Preserve every user and DIMON turn in a compact conversation timeline.
- [ ] Let the user correct an inaccurate transcript before it becomes a draft.
- [ ] Autoplay DIMON's spoken response when the browser permits it.
- [ ] Automatically listen after speech ends only when the server state expects
      a reply.
- [ ] Keep an obvious manual microphone control available.
- [ ] Support barge-in: speaking or pressing the microphone stops current TTS.
- [ ] Support “repeat that,” “go back,” “change it,” and “cancel.”
- [ ] Recover from microphone, ASR, LLM, and TTS failures without losing the
      active transaction.
- [ ] Highlight low-confidence names, numbers, currencies, and account labels.
- [ ] Keep buttons as fast alternatives for bounded choices.
- [ ] Never treat voice as the authorization factor for moving money or placing
      a paper order.

### Interaction rule

- Use voice for intention, questions, corrections, and navigation.
- Use buttons as optional shortcuts for bounded choices.
- Use an exact on-screen receipt plus Singpass/device confirmation for
  authorization.
- Speak concise summaries. Keep sources, timestamps, disclaimers, and detailed
  calculations in expandable visual details unless they change the decision.

## Flow architecture

```text
Voice or text
      |
      v
Transcript + conversation version
      |
      v
LLM proposes a bounded structured action
      |
      v
Deterministic evidence grounding and resolution
      |
      v
Domain adapter
  |-- beneficiary payments
  |-- bills
  |-- internal transfers
  `-- investments
      |
      v
Policy + independent validation
      |
      v
Immutable review receipt
      |
      v
Singpass/device approval
      |
      v
Domain executor
      |
      v
Ledger result + concise visual and spoken response
```

### Shared contracts

The reusable conversation layer should own these envelopes:

- `ConversationTurn`
- `ActionDraft`
- `ClarificationRequest`
- `PlanVersion`
- `ValidationDecision`
- `ReviewReceipt`
- `ApprovalChallenge`
- `ExecutionResult`

Each domain adapter owns its domain-specific fields and execution rules. A
Yahoo Finance outage must not break payments. A bill-provider failure must not
break stock research. Use bounded timeouts and stable failure contracts so one
adapter can fail without bringing down the conversation core.

### Architectural invariants

1. The LLM interprets language; it never authorizes or executes.
2. Every execution-critical field is grounded in user evidence or trusted
   server-owned data.
3. A correction creates a new plan version and supersedes earlier receipts.
4. Approval is bound to the exact stored receipt, authenticated user, expiry,
   and one-time challenge.
5. Policy and available funding are checked again at execution.
6. The committed result, not the model draft, is the source for the final UI
   and spoken confirmation.
7. Every interaction can be reconstructed without exposing hidden model
   reasoning to the customer.

## Stock and personal wealth hub

The stock path already has real market research, whole-share calculations,
market and limit paper orders, multi-turn refinement, receipt-bound approval,
paper holdings, and order lifecycle endpoints. The next step is to make those
capabilities visible as one coherent wealth experience.

### Unified wealth screen

- [ ] Available cash by currency.
- [ ] Cash reserved or committed to pending orders.
- [ ] Real-market paper holdings, separate from the fictional demo catalog.
- [ ] Quantity, average cost, current value, and unrealized gain or loss.
- [ ] Queued, open, filled, cancelled, rejected, and expired orders.
- [ ] Refresh, revise, and cancel actions where the order state permits them.
- [ ] Allocation and concentration by company, sector, market, and currency.
- [ ] SGD/USD exposure and the indicative FX rate used for estimates.
- [ ] Projected annual dividend income.
- [ ] A compact chronological activity timeline.

### Market-closed experience

An approved market order outside an eligible session should not end as an
opaque failure. It should become a visible queued paper order, for example:

> Your order for one NVIDIA share is queued for the next Nasdaq session. I
> reserved up to S$306.17. You can review or cancel it from Orders.

The order must immediately appear in the wealth hub with its state, reserved
cash, expiry or time-in-force, and available actions.

### Dividend-investing mode

Dividend mode is the clearest remaining stock contract identified in
[`stock-contracts.md`](stock-contracts.md). When the user's objective is income,
add:

- forward dividend yield;
- indicated annual dividend per share;
- usual payment frequency;
- latest known ex-dividend and payment dates when available;
- estimated annual income for the proposed whole-share quantity;
- withholding-tax, fee, and FX caveats;
- explicit missing, irregular, suspended, or recently reduced dividend states.

The dividend objective changes the research brief, not the confirmation or
execution rules. Dividend income must always be described as an estimate.

The spoken response should remain short:

> DBS currently yields approximately 5.2%. With 100 shares, indicated annual
> income is about S$240 before fees or future dividend changes. Would you like
> to prepare an order?

Detailed figures, dates, source information, tax and FX caveats, and dividend
history should live in an expandable visual card rather than lengthening the
spoken response.

### Portfolio conversation examples

- “How much cash is available after my queued NVIDIA order?”
- “What orders are still waiting?”
- “Cancel the NVIDIA order.”
- “How concentrated am I in US technology stocks?”
- “How much dividend income is this portfolio expected to generate?”
- “Put S$500 into DBS for dividend income.”

Read-only questions must remain read-only. Commands that revise, cancel, or
create an order must pass through the appropriate deterministic state
transition and, where required, a new receipt.

## Evidence judges should see

Use the recording sequence, results-card template, and integrity checklist in
[`judge-facing-security-proof-script.md`](../demo/judge-facing-security-proof-script.md).
The script separates the claim that the model interprets requests from the
measured authorization guarantee: only a valid customer approval bound to the
stored receipt can produce a ledger write.

Prepare a fixed evaluation pack and capture:

- exact field accuracy for recipient, amount, account, ticker, quantity, and
  order type;
- clarification rate and average clarification turns;
- false acceptance and false refusal rate;
- ASR errors for names, numbers, currencies, and account labels;
- tampering, replay, stale-receipt, and prompt-injection rejection;
- unauthorized ledger writes in the fixed evaluation suite;
- p50 and p95 latency for ASR, LLM, validation, TTS, and end to end;
- observations from at least five short user-testing sessions.

The fixed scenarios must include:

- one valid multi-action request;
- one natural correction;
- one prompt-injection attack;
- one stale-receipt attack;
- one replay or tampering attempt;
- one ambiguous-recipient recovery;
- one market-closed stock order.

The unchanged ledger after a rejected action must be visible rather than
merely stated. The target for unauthorized ledger writes is zero in the fixed
evaluation suite; report this as a measured result for that suite, not a
universal security guarantee.

## One-by-one implementation order

1. Complete genuine CodeBuddy or WorkBuddy contribution evidence. **Done.**
2. Implement `BILL_PAYMENT` and `INTERNAL_TRANSFER`. **In progress:** bounded
   bill contract, extraction, transcript grounding, biller resolution, missing
   detail clarification, and read-only dispatch are implemented. Bill receipts,
   approval, and execution remain outstanding.
3. Implement one heterogeneous atomic plan and receipt.
4. Make conversational corrections visibly produce a new version and
   invalidate the old receipt.
5. Finish the speech state machine and compact conversation timeline.
6. Add a stock Orders panel and unified wealth dashboard.
7. Add dividend-income mode.
8. Build the evaluation dashboard and rehearse the attack demonstration.
9. Produce the cover, under-ten-word blurb, description, video, repository
   instructions, and final screenshots listed in the competition playbook.

## Deliberately deferred

Do not expand into these areas until the ordered work above is complete:

- live brokerage or real bank connectivity;
- options, bonds, or complex products;
- Reddit or long-running sentiment research;
- voice biometrics presented as an authorization mechanism;
- broad autonomous financial recommendations;
- additional market-data providers without a demonstrated provider failure.

## Competition references

- `docs/competition handbook/dbs-banking-playbook.md`
- `docs/roadmap.md`
- `docs/codebuddy/README.md`
- `docs/features/real-stock-transaction-flow.md`
- `docs/features/stock-contracts.md`

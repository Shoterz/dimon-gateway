# Initial action catalog

**Status:** proposed initial scope, extracted from the banking-track brief. These are synthetic/mock actions for the hackathon demo.

## Financial actions in scope

| Action kind | Brief scenario | Example | Required resolution |
| --- | --- | --- | --- |
| `BILL_PAYMENT` | Pay a domestic bill | “Pay my electricity bill 120 dollars” | Saved biller and bill reference resolve on the server. |
| `BENEFICIARY_TRANSFER` | Send money to a saved person | “Send Mum 500 dollars” | “Mum” resolves to an authenticated user's saved beneficiary ID. |
| `INTERNAL_TRANSFER` | Move funds between owned or linked accounts | “Move 1,000 dollars to my joint account” | Destination resolves to an account the user owns or is permitted to use. |
| `EQUITY_BUY` | Invest a specified amount or permitted remainder in a share | “Invest whatever is left in Apple shares” | Server resolves an allow-listed instrument, snapshots the mock price, and calculates the investable amount after earlier actions and policy reserve. |

The brief's example combines a transfer to Mum with investing the remaining amount in Apple shares. The expanded demo can show a broader personal-finance plan: pay a 120 SGD bill, send 500 SGD to Mum, move 1,000 SGD to a joint account, then present the server-calculated amount available for Apple shares. A receipt must show the equity order, price snapshot, quantity or fractional quantity, total consideration, and remaining cash before approval.

## Conversation outcomes, not financial actions

| Outcome | Purpose |
| --- | --- |
| `CLARIFICATION_REQUEST` | Ask for a missing, ambiguous, or conflicting detail. |
| `REJECTED_PROPOSAL` | Explain why an unsupported or invalid request cannot become a draft. |
| `ACTION_BATCH_DRAFT` | A server-created set of proposed actions ready for review, never an execution command. |

Approval and execution are server-controlled state transitions: `approve_receipt` and `execute_approved_receipt`. They must never be types that the model can invoke.

## Deferred actions

Keep these out of the initial build:

- cross-border transfers and FX;
- new-payee enrollment;
- real-money integrations.

The equity scope is intentionally narrow: a mock, allow-listed buy order with a fixed or server-snapshotted demo price. Do not add real brokerage connectivity, margin, options, short selling, arbitrary tickers, or autonomous rebalancing.

## Product direction

Dimon Gateway can feel closer to a conversational personal-finance home than a single-purpose payment rail: it sees accounts, pays obligations, moves money, and proposes a guarded investment action in the same reviewable plan. That is the useful part of a SoFi-style direction. Lending, credit underwriting, insurance, and wealth-advice dashboards remain outside the first demo because they do not strengthen the DCTA transaction proof.

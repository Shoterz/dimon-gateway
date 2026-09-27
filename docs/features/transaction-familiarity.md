# Transaction familiarity

Status: exploration

## The idea

When someone sends money to a counterparty they have recently and repeatedly paid, for a similar amount, the proposed payment is more familiar. Dimon Gateway can use that familiarity to explain why the proposal looks expected or why it deserves more scrutiny.

Example: last month, a user sent their mum SGD 50. A new request to send their mum SGD 50 is a familiar pattern. A request to send SGD 20,000 to a new recipient is not.

“Familiar” does not mean “authorized.” The user still sees the exact recipient, amount, and account, then explicitly approves the stored proposal.

## Separate three decisions

| Decision | Meaning | Who makes it |
| --- | --- | --- |
| Intent extraction | Did the system understand the spoken request? | Speech recognition and language model, then schema validation |
| Familiarity assessment | Does this resemble a user's confirmed history? | Deterministic gateway rules |
| Execution authorization | May the mock ledger be changed? | Explicit user approval plus gateway verification |

Do not collapse these into one vague “confidence score.” A high transcription confidence does not make a payment familiar, and a familiar payment does not authorize execution.

## Candidate familiarity signals

Use only transactions that reached the executed state in the user's own mock ledger. A draft, cancelled transaction, or another user's activity must not create familiarity.

| Signal | Familiar pattern | Less familiar pattern |
| --- | --- | --- |
| Counterparty | Same known recipient | New or unresolved recipient |
| Direction | Same sender-to-recipient direction | Reverse direction or a first-time direction |
| Recency | Confirmed payment in the recent past | No recent history |
| Frequency | Repeated payments over time | One isolated payment |
| Amount | Near a user's normal range for that recipient | Large jump from the recipient's history |
| Currency | Previously used with that recipient | New currency |
| Timing | Similar cadence, such as monthly | Unusual timing or burst of requests |

“Near” should be defined with a stable rule, such as a bounded percentage band around past comparable amounts or a robust median-based range. Do not use simple averages alone: one unusually large transfer can distort them.

## First-demo behaviour

Start with explainable bands rather than a pretend-precise 0–100 number:

| Band | Example | Review behaviour |
| --- | --- | --- |
| Familiar | Same recipient and a similar recent amount | Show “similar to a recent payment”; show standard approval screen |
| Needs attention | Known recipient but unusual amount | Highlight the amount difference; require normal explicit approval |
| Unfamiliar | New recipient or a major amount jump | Highlight what is new; require enhanced review and explicit approval |

The first version should show the evidence behind the band, for example: “You sent SGD 50 to Mum on 18 August.” Avoid opaque statements such as “confidence: 87%.”

## Abuse and edge cases to test

- A person makes several tiny payments to make a later huge payment appear familiar.
- The speech recognizer substitutes a similar-sounding recipient name.
- A recipient name is reused by two different accounts.
- A recurring payment changes amount because of a genuine emergency.
- A user transfers money back to a person who previously transferred money to them.
- A copied or replayed approval is presented for a payment whose amount changed.

Large amount changes, changed recipient identities, or a different currency should reduce familiarity even when a relationship exists. The gateway rechecks the exact approved details at execution time.

## Questions to resolve

- What counts as a “similar” amount for the demo: percentage range, fixed SGD range, or both?
- What is the recent-history window: 30, 60, or 90 days?
- Should a user be able to label counterparties, for example “Mum,” independently of their bank-style display name?
- Which signals are shown to the user, and which remain internal anti-abuse checks?

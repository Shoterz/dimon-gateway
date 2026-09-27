# Relationship proximity

Status: exploration

## Proper term

The idea is a **relationship graph**, also called an **interaction graph**. Accounts or counterparties are nodes; confirmed payment relationships are edges. The relevant measures are **graph distance** or **relationship proximity**:

- A direct, confirmed counterparty is one hop away.
- A counterparty connected through one direct relationship is two hops away.
- A counterparty three connections away is three hops away.

This resembles LinkedIn connection distance, but it must not be treated as social proof or proof that a payment is safe.

## Privacy-safe demo model

The demo uses a user-owned relationship graph derived only from confirmed transactions in that user's seeded mock ledger. It does not reveal other users' identities, account balances, transaction history, or contact lists.

```text
You ── confirmed payment ── Mum
You ── confirmed payment ── Alex ── confirmed payment ── Merchant
```

In this example, Mum is a direct relationship. Merchant is two hops away through Alex. That can be a weak familiarity signal; it must never create permission to pay Merchant.

## Candidate graph signals

| Signal | What it asks | How it affects the review |
| --- | --- | --- |
| Direct relationship | Has the user paid this exact recipient before? | Strongest relationship-familiarity signal |
| Reciprocity | Has value moved in both directions? | Context only; does not establish trust on its own |
| Edge strength | Is the direct history frequent and recent? | More history can support a familiar label |
| Graph distance | How many confirmed edges separate the user and recipient? | Greater distance can add caution for unfamiliar, large transfers |
| Amount context | Is this amount normal for the direct relationship? | Prevents a small-payment history from normalizing a large jump |

For the initial demo, direct history and amount context are enough. Two-hop and three-hop graph logic is a strong later feature because it makes the idea visually compelling, but it adds privacy, data-model, and explanation work.

## Suggested rule for the pitch

> A familiar payment can be easier to review. A distant or unusual payment gets clearer warnings. Every payment still requires a fresh, explicit approval.

Example: a large payment to a third-hop recipient with no direct payment history becomes “unfamiliar and high attention.” The review should name the concrete reason: “This is your first payment to this recipient and the amount is higher than your normal transfers.” Do not tell the user “this is unsafe” based on graph distance alone.

## Risks to avoid

- Treating a mutual contact as endorsement.
- Allowing another person's history to influence a user's payment decision without clear permission.
- Letting a few small transactions create a trustworthy-looking relationship before a large transfer.
- Exposing the route through the graph, which can reveal private relationships.
- Using relationship distance as a substitute for recipient identity verification.

## Future demonstration idea

Show a small synthetic network beside the review card. Let a judge compare a familiar direct payment with a large, distant payment. The ledger remains the source of truth; the graph only explains review friction.

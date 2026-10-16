# Transaction authentication policy

This document defines authentication for friend transfers and utility bill
payments in the synthetic demo.

## Authentication tiers

| Amount used for the tier | Required confirmation |
| --- | --- |
| Below S$200 | The user checks the mock “yes, confirm” affirmation and signs with their device. This is a simple user attestation; it does not record audio or verify speaker identity. |
| S$200 or more | Singpass, facial recognition, and device signature. The boundary is inclusive, so exactly S$200 uses this tier. |

Other risk checks continue to apply and can add requirements. Friend-transfer
batches use their combined transfer total. Read-back and unusual-amount checks
remain in force for friend transfers. Utility billers are server-owned
synthetic destinations, so person-specific new-payee and unusual-amount checks
do not apply to them. Transaction, daily, and velocity limits still apply.

## Single utility bill payment

A complete payment to a supported synthetic biller creates an immutable review
receipt. The receipt binds the biller, amount, source account, and evidence from
the user's request. The user must review and approve that receipt with the
device signature and the authentication tier above. No payment is created
before approval. At approval, the demo ledger atomically debits the source
account and credits the synthetic biller's settlement account. The result is
recorded as a utility bill payment and shown in the account history. No external
biller, bank, or payment rail is contacted.

The server-owned biller directory recognizes `PopWater` as a voice alias for
the synthetic `PUB Water` destination. When no amount is spoken, the displayed
statement amount comes from that directory; the model cannot supply or change
the amount.

## Grouped bill and friend transfer in one intent

When one grouped request contains a grounded utility bill payment and one or
more friend transfers, the authentication amount is the sum of every payment
in the group:

```text
utility bill amount + all friend-transfer amounts
```

The server grounds each amount in its own exact transcript span and resolves
the biller and each saved recipient before creating any review. It shows the
bill and transfer as separate child reviews under one grouped request. Every
payment child uses the same aggregate payment amount to select its required
authentication tier. The browser cannot lower that tier by changing an amount.

For example, a S$150 friend transfer combined with a S$50 utility proposal
totals S$200 and therefore requires Singpass and facial recognition. A S$150
transfer combined with a S$40 utility proposal totals S$190 and uses the mock
affirmation tier, subject to other risk checks.

Each payment child keeps its own amount, funding account, recipient, receipt,
and ledger entry. The grouped payment total is used only for authentication;
it is not treated as one combined debit. Each child must receive its own
approval before that child can execute. Approvals and execution across children
are independent, so approving one child does not approve another. A stock
purchase in the same request remains a separate action and uses the existing
paper-order approval policy. When the transcript explicitly says “at market,”
Dimon carries that choice into the paper-order review instead of asking the
user to choose market versus limit again. The order still requires the separate
Singpass, face, and device approval, and no order is placed before approval.

If the service cannot ground or resolve every requested action, it blocks the
group before creating review receipts. Missing, ambiguous, or unsupported
actions do not produce partial review cards. Saving the parent group and its
ordered child references is atomic. Domain drafts are prepared stock-first so
market lookup failures occur before payment receipts are created. If a later
child or group persistence step fails, any still-pending drafts from that
request are superseded or expired before a failure response is returned. After
the group is created, each existing domain approval endpoint remains
authoritative and updates only its matching child status. A utility-only request
creates a review receipt and displays the tier for its amount; it moves money
only after approval.

## Implementation references

- `apps/dimon_live/service.py` identifies and grounds bill payment intents.
- `apps/dimon_live/receipts.py` and `apps/dimon_live/database.py` bind the
  utility bill details into an immutable receipt and recheck it at approval.
- `apps/dimon_live/risk_policy.py` applies the inclusive S$200 threshold.
- `apps/dimon_live/static/app.js` shows bill review, approval, and synthetic
  settlement details.
- `apps/dimon_live/multi_action.py` validates ordered transcript spans and
  rejects a group when any action cannot be grounded.
- `apps/dimon_live/database.py` stores the grouped review references and uses
  the aggregate payment total when rechecking authentication for each payment
  child at approval time.
- `GET /api/multi-actions/{group_id}` returns the current user's group state;
  existing bill, transfer, and paper-order approval routes update only the
  matching child.

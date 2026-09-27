# Atomic Saved-Beneficiary Batch Design

**Date:** 2026-09-24
**Status:** proposed for implementation
**Scope:** one natural-language request containing two to four transfers to
distinct saved beneficiaries, reviewed and executed as one atomic synthetic
ledger batch.

## Purpose

Dimon Live already grounds and executes one saved-beneficiary or PayNow
transfer. This increment proves a different capability: Qwen may identify
multiple intended recipients, while deterministic server code binds each
money expression to the corresponding recipient, creates one immutable batch
receipt, and executes all transfers exactly once or none of them.

The first demonstration sentence is:

> Send John Smith S$6.50 and Alicia Powell S$4.

Before approval, balances and ledger history remain unchanged. The review
shows both transfers and the S$10.50 total debit. One approval commits both
transfers inside one SQLite transaction. A failed item, stale state, changed
receipt, or replay cannot produce a partial batch.

## Non-goals

- PayNow destinations inside a batch.
- Bill payment, internal transfer, or equity actions.
- More than four batch items.
- Automatic batch execution without review.
- “Each,” shared-total, percentage, remainder, conditional, or scheduled
  amounts.
- Generic plans, loops, arbitrary tools, Python, SQL, or shell access for
  Qwen.
- Large evaluation-corpus work during this increment.

## Raw model proposal

The action schema becomes a union of the existing version-three single-action
proposals and one version-four batch proposal:

```json
{
  "contract_version": 4,
  "kind": "BENEFICIARY_TRANSFER_BATCH",
  "source_account_ref": null,
  "source_evidence": null,
  "items": [
    {
      "recipient_label": "John Smith",
      "recipient_evidence": "John Smith"
    },
    {
      "recipient_label": "Alicia Powell",
      "recipient_evidence": "Alicia Powell"
    }
  ],
  "currency": "SGD",
  "requested_execution_date": null
}
```

The raw batch contains no amount fields. Qwen cannot calculate minor units,
associate a model-created amount with an item, resolve a beneficiary ID, or
request execution. Each evidence value must be a verbatim substring of the
original request. The schema permits two to four items and rejects additional
fields.

Single-action version-three behavior remains backward compatible.

### Directory-preservation fallback

The model is not allowed to erase an explicit recipient. If a transfer-command
sentence contains two to four complete saved-beneficiary display names exactly
once, exactly one server-derived SGD amount per name, and no payment reference,
the gateway upgrades a collapsed single-beneficiary proposal into the version-
four batch shape. This narrow fallback is directory lookup, not fuzzy language
inference: it does not invent names, recover PayNow destinations, interpret
non-command prose, or assign amounts. The normal batch grammar and receipt
checks still run afterward.

## Deterministic clause grounding

The gateway derives all supported SGD mentions from the original user text,
including their character spans. It grounds every proposed recipient evidence
span, resolves it to exactly one saved beneficiary, and then sorts recipients
and amount mentions by their positions in the original text.

The first recipient occurrence is paired with the first amount occurrence,
the second with the second, and so on. This supports both forms:

- `Send John Smith $6.50 and Alicia Powell $4`
- `Send $6.50 to John Smith and $4 to Alicia Powell`

The batch is rejected without creating a receipt when:

- there are fewer than two or more than four recipients;
- recipient and amount counts differ;
- an evidence span is missing, repeated, overlapping, or ungrounded;
- a recipient is not an exact saved-beneficiary match;
- the same canonical beneficiary occurs more than once;
- multiple funding accounts are proposed;
- a PayNow proxy, unsupported currency, negation, shared amount, or unsupported
  amount expression appears; or
- the total exceeds the source balance.

Position pairing is not semantic guesswork: it is a documented input grammar.
Unsupported phrasing fails closed with `AMBIGUOUS_BATCH_BINDING` and asks the
user to restate one amount per named recipient.

## Server-owned batch receipt

The gateway creates one canonical batch receipt containing:

- contract version;
- batch receipt ID, request ID, authenticated demo user ID, expiry, and nonce;
- one server-owned source account ID and label;
- an ordered list of item positions, beneficiary IDs, destination account IDs,
  display names, exact integer amounts, and currency;
- total debit in integer minor units; and
- the evidence bundle derived from the original request.

Canonical JSON serialization and SHA-256 bind all of these facts into one
digest. The browser submits only the batch receipt ID and digest. It cannot
submit revised items, totals, recipients, or account IDs.

The API response uses:

```json
{
  "outcome": "BATCH_CONFIRMATION_REQUEST",
  "reason_code": "BATCH_CONFIRMATION_REQUIRED",
  "batch_receipt": {
    "receipt_id": "batch_receipt_...",
    "receipt_digest": "...",
    "source_label": "savings",
    "items": [
      {"recipient_display_name": "John Smith", "amount_minor": 650},
      {"recipient_display_name": "Alicia Powell", "amount_minor": 400}
    ],
    "total_minor": 1050,
    "currency": "SGD",
    "expires_at": "..."
  }
}
```

The approval endpoint is
`POST /api/batch-receipts/{receipt_id}/approve` with the same strict
digest-only body used by single receipts.

## Persistence and atomic execution

Add focused tables rather than overloading the ambiguity-oriented single
receipt rows:

- `batch_review_receipts`: lifecycle, canonical payload, digest, source, and
  total, while the existing `action_attempts` row retains raw model output and
  the validated grounded plan;
- `batch_receipt_items`: ordered immutable item facts;
- `batch_receipt_executions`: exactly-once mapping from batch receipt to one
  batch transaction ID;
- `batch_ledger_transactions`: one server-owned transaction for the complete
  batch; and
- `batch_ledger_entries`: one source debit for the total plus one destination
  credit per ordered item.

Approval performs one `BEGIN IMMEDIATE` transaction. It reloads the stored
receipt and items, verifies owner, digest, status, expiry, canonical JSON,
source ownership, destination mappings, per-item values, total, available
funds, and absence of an earlier execution. It then creates one batch ledger
transaction, one source entry for the negative total, and one positive
destination entry per item. It verifies that all entries sum to zero and that
the source debit equals the stored batch total, records the execution, marks
the receipt executed, and commits.

Any validation or database error rolls back the entire SQLite transaction.
Replaying the same approval returns the stored group and transaction IDs
without another debit. Changing the digest or any stored/canonical fact is
blocked.

## UI behavior

The existing clarification area displays one batch card containing an ordered
row for each recipient and amount, followed by the total debit and expiry. It
has one **Confirm exact batch** button.

After execution:

- the success banner says the batch settled exactly once;
- the settlement rail shows batch receipt approval, batch revalidation, and
  atomic SQLite commit;
- database proof shows one transaction boundary containing both debits and
  credits; and
- the committed ledger continues to show one row per resulting transfer,
  expanded from the one server-owned batch transaction.

Existing single-transfer and PayNow rendering remains unchanged.


## Stable outcomes

New stable reason codes are:

- `BATCH_CONFIRMATION_REQUIRED`
- `AMBIGUOUS_BATCH_BINDING`
- `BATCH_RECIPIENT_COUNT_MISMATCH`
- `DUPLICATE_BATCH_RECIPIENT`
- `BATCH_RECEIPT_NOT_FOUND`
- `BATCH_RECEIPT_DIGEST_MISMATCH`
- `BATCH_RECEIPT_EXPIRED`
- `BATCH_RECEIPT_INVALID_STATUS`
- `BATCH_RECEIPT_USER_MISMATCH`
- `BATCH_RECEIPT_TAMPERED`

Existing codes such as `UNKNOWN_RECIPIENT`, `UNGROUNDED_RECIPIENT`,
`INSUFFICIENT_FUNDS`, `IDEMPOTENCY_CONFLICT`, and `DATABASE_ERROR` are reused
where their meaning is unchanged.

## Verification

The implementation uses focused TDD rather than a large generated corpus:

1. Schema accepts the bounded batch and rejects amount fields, PayNow items,
   fewer than two items, and more than four items.
2. Span grounding binds both recipient-first and amount-first examples.
3. Count mismatch, duplicate recipient, unknown recipient, and ambiguous
   evidence create no receipt and do not mutate balances.
4. The hero request creates one stored receipt with two items and S$10.50
   total, without mutation.
5. Approval debits savings S$10.50 and credits John S$6.50 and Alicia S$4 in
   one atomic commit.
6. Replay returns the original result without another debit.
7. Digest/item tampering, expiry, changed beneficiary mapping, and insufficient
   funds at approval roll back the whole batch.
8. Existing single-beneficiary, PayNow, clarification, and UI tests remain
   green.
9. A live Qwen3.5 request produces the batch review, and approval is left for
   the user to exercise in the browser.

## Completion boundary

This increment is complete when the typed hero request reaches one reviewable
batch receipt and one approval changes all three relevant balances exactly
once, while every tested failure changes none. Adding other banking action
types begins a separate increment.

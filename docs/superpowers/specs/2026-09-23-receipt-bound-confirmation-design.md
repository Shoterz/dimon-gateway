# Receipt-Bound Confirmation Design

**Status:** approved conversational design, awaiting written-spec review
**Scope:** synthetic `BENEFICIARY_TRANSFER` flow in Dimon Live
**Related note:** `docs/deferred-next-step-contracts-and-gateway.md`

## Objective

Bind every ledger mutation to an immutable, server-created receipt. Qwen may
interpret a user request once, but it must never be asked to reinterpret a
confirmed action. Exact explicit commands retain immediate execution; vague,
misspelled, and ambiguous commands pause on a receipt-backed confirmation.

The visible experience remains fast:

- `Send Alicia Powell S$5` creates an internal receipt, records explicit-command
  policy authorization, and executes immediately.
- `I owe Alicia Powell S$5, help me resolve it` creates a pending receipt and
  asks the user to confirm its exact stored facts.
- `Send Jhon Tan S$2.50` creates a pending candidate receipt for John Tan.
- `Send John S$2.50` creates separate pending candidate receipts for John Smith
  and John Tan.

## Non-goals

- No real bank, payment, broker, authentication, signing-key, or remote-service
  integration.
- No second action type, multi-action batch, voice approval, or semantic
  prompt-injection classifier in this phase.
- No client-created receipts, client-selected account IDs, or model-created
  receipt fields.
- No claim that an explicit-command policy is suitable for real-money banking.

## Trust boundary

```text
user text
  -> Qwen extracts one untrusted six-field intent
  -> gateway resolves server-owned account and beneficiary IDs
  -> gateway creates immutable receipt(s)
  -> exact explicit command: server policy authorizes receipt immediately
  -> uncertain command: browser confirms one stored receipt
  -> gateway revalidates receipt and current state
  -> gateway performs one atomic balanced ledger mutation
```

Qwen receives no receipt IDs, receipt digest, nonce, approval credential, SQL,
Python, shell, or ledger tool. Confirmation and execution do not call Qwen.

## Authorization paths

Every successful transfer uses the same receipt execution path.

### Explicit exact command

An explicit `send`, `pay`, or `transfer` request with a uniquely resolved source
and beneficiary creates one receipt. The gateway records authorization method
`EXPLICIT_COMMAND_POLICY` and executes the receipt inside the same SQLite
transaction. The UI still appears to settle immediately.

### User-confirmed command

A vague request with one resolved beneficiary, a close recipient match, or an
ambiguous recipient creates one or more receipts with status
`AWAITING_CONFIRMATION`. The UI renders the exact stored source, recipient,
amount, currency, and expiry. Selecting a receipt calls the approval endpoint
with its ID and digest. The gateway records authorization method
`USER_CONFIRMATION` and executes that receipt without another model call.

For an ambiguous recipient, each displayed candidate has its own receipt. All
candidate receipts share a `clarification_group_id`. Executing one candidate
marks the unselected siblings `SUPERSEDED` atomically.

Negated, unsupported, unknown-recipient, invalid-model-output, and insufficient-
funds outcomes create no executable receipt.

## Persistent records

SQLite remains the single storage owner.

### `review_receipts`

| Field | Meaning |
| --- | --- |
| `receipt_id` | Server-generated opaque identifier; primary key |
| `clarification_group_id` | Shared opaque identifier for mutually exclusive candidates; nullable |
| `request_id` | Original idempotency key; may repeat across candidates in one clarification group |
| `user_id` | Synthetic owner, initially `demo_user_01` |
| `source_account_id` | Server-resolved source account foreign key |
| `beneficiary_id` | Server-resolved saved beneficiary foreign key |
| `recipient_display_name` | Server-owned display value shown in review |
| `amount_minor` | Positive integer SGD minor units |
| `currency` | `SGD` only |
| `original_text` | Original user request retained for audit |
| `expires_at` | UTC timestamp, five minutes after creation for every receipt |
| `nonce` | Server-generated unique one-time value with a database uniqueness constraint |
| `canonical_json` | Exact canonical receipt payload used for hashing |
| `receipt_digest` | Lowercase SHA-256 digest of `canonical_json` |
| `status` | `AWAITING_CONFIRMATION`, `EXECUTED`, `EXPIRED`, or `SUPERSEDED` |
| `created_at` | Server timestamp |

The canonical payload includes a contract version, receipt ID, request ID,
synthetic user ID, source account ID, beneficiary ID, display name, amount,
currency, expiry, nonce, and original text. Canonical JSON uses UTF-8,
lexicographically sorted keys, and compact separators before SHA-256 hashing.
Those canonical fields never change after insertion; only `status` may move
through the allowed state transitions.

The only valid transitions are `AWAITING_CONFIRMATION -> EXECUTED`,
`AWAITING_CONFIRMATION -> EXPIRED`, and `AWAITING_CONFIRMATION -> SUPERSEDED`.
An exact command creates its receipt and moves it to `EXECUTED` in the same
database transaction. An executed receipt is terminal. Its expiry limits new
authorization, but an identical replay after expiry still returns the already
stored transaction without creating writes.

### `authorization_records`

| Field | Meaning |
| --- | --- |
| `authorization_id` | Server-generated primary key |
| `receipt_id` | Unique foreign key to the authorized receipt |
| `receipt_digest` | Digest authorized by policy or user |
| `user_id` | Synthetic authorizing user |
| `method` | `EXPLICIT_COMMAND_POLICY` or `USER_CONFIRMATION` |
| `authorized_at` | Server timestamp |

### `receipt_executions`

| Field | Meaning |
| --- | --- |
| `receipt_id` | Primary key and foreign key to the receipt |
| `transaction_id` | Unique foreign key to the existing ledger transaction |
| `executed_at` | Server timestamp |

The unique receipt and transaction keys provide the database-level exactly-once
boundary without changing the existing balanced `ledger_entries` model.

## API contracts

### `POST /api/actions`

The request remains:

```json
{
  "request_id": "uuid",
  "text": "I owe Alicia Powell five dollars"
}
```

The response is one of:

- an immediately executed transfer with its executed receipt summary;
- a clarification containing `receipt_choices`; or
- a blocked/failed non-mutating outcome.

Each pending choice contains only display-safe facts plus the opaque approval
coordinates:

```json
{
  "receipt_id": "receipt_...",
  "receipt_digest": "sha256...",
  "source_label": "savings",
  "recipient_display_name": "Alicia Powell",
  "amount_minor": 500,
  "currency": "SGD",
  "expires_at": "2026-09-23T03:00:00+00:00"
}
```

An identical retry of the action request returns the same receipts or execution
result without another Qwen call or additional ledger mutation. Reusing its
request ID with different text remains an idempotency conflict.

### `POST /api/receipts/{receipt_id}/approve`

Request:

```json
{
  "receipt_digest": "sha256..."
}
```

The server supplies the authenticated identity internally; the initial demo
uses fixed `demo_user_01`. The request accepts no amount, currency, beneficiary,
source, status, or authorization field. Unknown fields receive HTTP 422.

Approval returns the normal executed-transfer response. Repeating the same
valid approval returns the existing transaction with an idempotent-replay
marker. A different digest, wrong user, expired receipt, superseded receipt,
missing receipt, or invalid status produces a stable non-mutating reason code.

## Atomic execution algorithm

Approval and execution use `BEGIN IMMEDIATE` and one SQLite connection:

1. Load the receipt and any existing receipt execution.
2. If already executed and identity/digest match, return the stored result;
   this idempotent read is allowed even when the receipt's authorization window
   has since expired.
3. Verify user ownership, supplied digest, recomputed canonical digest, status,
   expiry, nonce uniqueness, and candidate-group state.
4. Resolve the stored server IDs again and reread the source balance.
5. Reject insufficient funds or missing server-owned state.
6. Insert the authorization record.
7. Debit source, credit destination, insert one ledger transaction and two
   equal-and-opposite ledger entries.
8. Verify the new transaction's entry sum is zero.
9. Insert the receipt-execution link, mark the receipt `EXECUTED`, and mark
   unselected group siblings `SUPERSEDED`.
10. Commit; roll back every write on any error.

The explicit-command path calls this same algorithm with server-generated
authorization method `EXPLICIT_COMMAND_POLICY` immediately after receipt
creation.

## UI behavior

- Exact explicit actions retain the current immediate settlement result.
- Clarification cards show the exact receipt source, recipient, amount and
  expiry rather than reconstructing a natural-language command.
- Candidate buttons carry only receipt ID and digest received from the server.
- Selecting a candidate disables the confirmation controls, calls the receipt
  approval endpoint, and renders the returned ledger state and trace.
- Qwen latency appears only on the initial request. Confirmation traces begin
  at `RECEIPT_APPROVAL_RECEIVED` and contain no model stage.
- Expired, superseded, tampered and replay outcomes are visibly distinct.
- Reset clears receipts, authorizations, receipt executions, action attempts,
  ledger transactions and ledger entries, then restores seed balances.

## Error codes

- `RECEIPT_NOT_FOUND`
- `RECEIPT_DIGEST_MISMATCH`
- `RECEIPT_USER_MISMATCH`
- `RECEIPT_EXPIRED`
- `RECEIPT_SUPERSEDED`
- `RECEIPT_INVALID_STATUS`
- `RECEIPT_ALREADY_EXECUTED` only for incompatible retries; identical retries
  return the existing execution
- existing `INSUFFICIENT_FUNDS`, `DATABASE_ERROR`, and idempotency codes remain

Every error returns current server state and performs no ledger mutation.

## Testing and completion evidence

Tests must prove:

1. Exact explicit transfer creates a receipt, policy authorization, one ledger
   transaction, and two balanced entries.
2. Vague exact request creates a pending receipt and no transaction.
3. Confirming that receipt executes without invoking Qwen again.
4. Close-match confirmation executes the canonical saved beneficiary.
5. Ambiguous `John` creates two grouped candidate receipts; approving one
   supersedes the other.
6. Changed digest, unexpected approval fields, wrong user, expiry, superseded
   candidate, and missing receipt all fail without a balance change.
7. Identical repeated approval returns the same transaction without a second
   debit.
8. A changed balance is rechecked at approval time.
9. Forced ledger-entry failure rolls back authorization, balances, transaction,
   entries and receipt status.
10. Reset removes every receipt-related record.
11. Confirmation API and UI issue no Qwen request.
12. Existing Dimon Live and local structured-action suites remain green.

## Migration and operational boundary

The three receipt tables are additive and created with `CREATE TABLE IF NOT
EXISTS`; the existing runtime SQLite file requires no destructive migration.
The first development reset after deployment clears old demo history and seeds
the same four accounts. Runtime SQLite files remain ignored.

This is still a single-user localhost prototype. `demo_user_01` is an
injectable server-side identity used to test ownership boundaries, not real
authentication. Real authentication, cryptographic signing with managed keys,
and real payment integration remain explicitly out of scope.

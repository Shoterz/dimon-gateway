# Guarded Autopilot Prototype Design

**Date:** 2026-09-22
**Status:** proposed for review
**Purpose:** a fast, local, synthetic demonstration—not production banking software

## Goal

Replace the CLI-only experience with a polished localhost interface where a
person types a natural-language beneficiary transfer, watches the existing
local Qwen model extract the intent, and sees a deterministic Python service
apply the resulting transfer to a SQLite ledger.

The demonstration should feel immediate. A conflict-free eligible transfer is
executed automatically. Ambiguous, invalid, unsupported, or unfunded requests
do not mutate balances.

## First-version scope

The prototype supports one action:

```text
BENEFICIARY_TRANSFER
```

Example:

```text
Send John Smith S$50 from savings.
```

It includes:

- the existing local Qwen/llama.cpp structured-output path;
- one seeded demo user, source account, and saved beneficiaries;
- a SQLite database containing balances and ledger history;
- guarded automatic execution for eligible synthetic transfers;
- visible action stages, before/after balances, and ledger entries;
- clarification for ambiguous recipients;
- blocked outcomes for invalid requests and insufficient funds; and
- a reset control that restores the seed state.

It excludes speech, real accounts, external payment systems, approval receipts,
stocks, bills, internal transfers, authentication, multiple users, and cloud
deployment. Those remain later phases.

## User experience

The browser opens to a single **Dimon Live Console** marked clearly as synthetic
demo mode. The primary interaction is a free-form command bar. There are no
preset transaction buttons.

The screen contains:

1. account balance cards;
2. the natural-language command bar;
3. a live action trace;
4. the most recent outcome;
5. a ledger-history table; and
6. a reset-demo control.

Submitting an eligible request initially shows “Qwen extracting intent.” When
the backend responds, the interface reveals and animates the real recorded
stages: validated intent, canonical account and beneficiary resolution,
eligibility checks, SQLite transaction, and updated balances. It does not
pretend that prerecorded stages occurred.

Successful example:

```text
Savings: S$1,000.00 → S$950.00
John Smith: S$100.00 → S$150.00
Outcome: EXECUTED_SYNTHETIC_TRANSFER
```

An ambiguous request such as “Send John S$50” displays server-supplied
candidates and leaves every balance unchanged.

## Architecture

```text
Browser
  │  POST /api/actions, GET /api/state, POST /api/reset
  ▼
Python FastAPI application
  ├── existing Qwen structured-intent adapter
  ├── strict schema and semantic validation
  ├── deterministic account/beneficiary resolver
  ├── guarded-autopilot eligibility policy
  └── one predefined SQLite transfer operation
        ▼
      SQLite
```

The FastAPI application serves the static HTML, CSS, and JavaScript as well as
the JSON API, so the prototype has one application command. The already-running
llama.cpp server remains a separate local dependency.

The browser never contacts Qwen or SQLite directly. Qwen receives no SQL,
Python, shell, approval, or arbitrary tool interface. It can only propose the
existing constrained transfer-intent object.

## API

### `GET /api/state`

Returns current visible accounts, recent synthetic transactions, service/model
health, and demo-mode metadata.

### `POST /api/actions`

Request:

```json
{
  "request_id": "client-generated-uuid",
  "text": "Send John Smith S$50 from savings."
}
```

The response is one of:

- `EXECUTED_SYNTHETIC_TRANSFER` with the validated intent, resolved action,
  truthful stage trace, ledger transaction identifier, and updated state;
- `CLARIFICATION_REQUEST` with server-supplied candidates and unchanged state;
- `BLOCKED` with a stable reason code and unchanged state; or
- `FAILED` for model/database availability problems, with unchanged state.

### `POST /api/reset`

Recreates the known seed state and returns it. This endpoint is available only
in the local synthetic-demo configuration.

## Guarded-autopilot policy

Automatic mutation is permitted only when all conditions hold:

- the model response finishes normally and passes the strict schema;
- the action is exactly `BENEFICIARY_TRANSFER`;
- the source label resolves to one seeded source account;
- the recipient resolves by case-insensitive exact name or a configured exact
  alias to one saved beneficiary;
- the amount is a positive integer number of minor units;
- the currency is SGD;
- the request contains no unresolved or conflicting fields;
- the source balance is sufficient at transaction time; and
- the request identifier has not already produced a different operation.

The model does not provide a confidence score. Prefix, edit-distance, phonetic,
or embedding similarity never authorizes mutation. Ambiguous or approximate
identity matches request clarification.

## SQLite data model

### `accounts`

- `account_id` primary key
- `owner_display_name`
- `label`
- `currency`
- `balance_minor`
- `is_user_source`

### `beneficiaries`

- `beneficiary_id` primary key
- `display_name`
- `normalized_alias` unique
- `destination_account_id` foreign key to `accounts`
- `masked_destination`

### `action_attempts`

- `request_id` primary key
- original user text
- raw model output
- validated intent JSON
- outcome and stable reason code
- created timestamp

### `ledger_transactions`

- `transaction_id` primary key
- `request_id` unique foreign key
- action kind
- amount and currency
- created timestamp

### `ledger_entries`

- entry ID primary key
- transaction ID foreign key
- account ID foreign key
- signed `delta_minor`

Every successful transfer creates one negative source entry and one equal
positive destination entry. Their deltas must sum to zero.

## Transaction and idempotency behavior

Execution uses one SQLite transaction beginning with `BEGIN IMMEDIATE`:

1. check for an existing `request_id`;
2. reread the current source balance;
3. reject insufficient funds;
4. update both account balances;
5. insert the transaction and its two balanced entries;
6. record the successful action attempt; and
7. commit.

Any error rolls the complete transaction back. Repeating a successful
`request_id` returns the previously recorded result without changing balances
again. Reusing a request ID with different text is blocked as an idempotency
conflict.

## Seed data

- Demo User / savings: S$1,000.00
- John Smith / synthetic destination: S$100.00
- John Tan / synthetic destination: S$75.00

John Smith and John Tan deliberately make the label “John” ambiguous.

## Failure behavior

- Qwen unavailable or timeout: show `MODEL_UNAVAILABLE`; no database mutation.
- Truncated or invalid model JSON: show `INVALID_MODEL_OUTPUT`; no mutation.
- Unknown or ambiguous account/recipient: clarify or block; no mutation.
- Unsupported action: show `UNSUPPORTED_ACTION`; no mutation.
- Insufficient funds: show `INSUFFICIENT_FUNDS`; no mutation.
- SQLite failure: rollback and show `DATABASE_ERROR` without claiming success.
- Duplicate identical request: return the existing result without another
  debit.

The UI derives success wording from the stored backend result, never from model
prose.

## Verification

Automated tests cover:

- seed and reset behavior;
- exact eligible transfer and two balanced ledger entries;
- unchanged balances for ambiguous recipient;
- unchanged balances for invalid amount and insufficient funds;
- rollback on a simulated database failure;
- identical retry without a second debit;
- conflicting reuse of a request ID;
- API behavior with a fake Qwen transport; and
- preservation of the existing structured-action tests.

One live test uses the existing local Qwen server. Browser verification confirms
that the command bar, execution trace, balance animation, ledger row, ambiguous
state, blocked state, and reset control are usable at localhost.

## Success criteria

The prototype is successful when a user can open one localhost URL, type the
golden transfer request, and observe a real SQLite debit and credit through the
GUI. “Send John S$50” must request clarification with no balance change. A
duplicate request must not debit twice, and resetting the demo must restore the
documented seed balances.

## Deferred continuation

After this intermediate GUI is complete, resume the work captured in
[`docs/deferred-next-step-contracts-and-gateway.md`](../../deferred-next-step-contracts-and-gateway.md):
versioned shared contracts, immutable review receipts, explicit approvals, and
the fuller mock gateway.

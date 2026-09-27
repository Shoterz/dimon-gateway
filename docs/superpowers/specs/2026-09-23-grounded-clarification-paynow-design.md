# Grounded Clarification and Synthetic PayNow Design

**Status:** approved
**Scope:** Dimon Live synthetic transfer prototype
**Build order:** grounded critical fields, stored amount clarification, synthetic
PayNow lookup, then bounded phonetic candidate retrieval

## Objective

Prevent Qwen from inventing an amount or recipient while preserving a natural,
multi-turn experience. A request such as `send Alicia Powell money for flowers`
must ask for the missing amount; the reply `ten dollars` may fill only that
missing field and may not reinterpret the stored recipient. A request such as
`PayNow 8695 2933 ten dollars for flowers` must perform an exact synthetic proxy
lookup, display the directory-owned masked name, and require confirmation of an
immutable receipt. A phonetic name such as `Alesha` may retrieve Alicia Powell
as a candidate, but never as an authorization.

Success means that every execution-critical fact is either grounded in the
user's text or returned by a server-owned directory, every uncertain or ad-hoc
destination is confirmed, and no missing, changed, or merely similar value can
mutate the ledger.

## Priorities

1. **Required safety:** nullable extracted amounts plus deterministic evidence
   verification. Missing or ungrounded amounts never create executable receipts.
2. **Required interaction:** an expiring, server-owned clarification session so
   an amount-only follow-up completes the stored intent without a second full
   interpretation.
3. **Required demo path:** exact synthetic PayNow mobile lookup with a masked
   recipient name and mandatory receipt confirmation.
4. **Bounded usability:** one general phonetic fallback for candidate generation
   only. Do not pursue exhaustive pronunciation coverage.

## Non-goals

- No production PayNow, FAST, bank, payment-service, or identity integration.
- No real phone-number lookup and no claim that the seed number belongs to a
  real person.
- No fuzzy correction of mobile numbers.
- No arbitrary multi-turn conversation memory. Version one clarifications fill
  only one missing amount.
- No change of recipient, source, currency, or action type through an amount
  clarification response. The user must start a new action to change them.
- No automatic execution for PayNow, phonetic matches, partial names, or
  multi-turn-completed transfers.
- No hand-maintained dictionary of name misspellings and no promise of perfect
  multilingual phonetic resolution.
- No voice authentication or speech-to-text implementation in this phase. The
  text flow must nevertheless be safe for a later transcript source.

## Trust boundary

```text
user text or untrusted transcript
  -> Qwen emits one schema-constrained proposal with verbatim evidence spans
  -> gateway verifies evidence and independently parses critical values
  -> missing amount: gateway stores a scoped partial intent
  -> amount-only reply: deterministic parser fills only the missing amount
  -> saved name: server directory performs exact/fuzzy/phonetic candidate search
  -> PayNow mobile: server performs exact synthetic proxy lookup
  -> gateway creates an immutable receipt from resolved server facts
  -> required confirmation submits receipt ID + digest only
  -> gateway revalidates and executes exactly once
```

Qwen never receives SQL, Python, shell, ledger mutation, receipt approval, or
general-purpose tools. It cannot create a recipient ID, lookup reference,
masked name, receipt, authorization record, or execution command.

## Raw intent contract, version 2

Create a Dimon Live-owned JSON Schema rather than changing the historical spike
schema. It is a discriminated union with two variants.

### Saved-beneficiary transfer

```json
{
  "contract_version": 2,
  "kind": "BENEFICIARY_TRANSFER",
  "source_account_ref": null,
  "source_evidence": null,
  "recipient_label": "Alesha",
  "recipient_evidence": "alesha",
  "amount_minor": 1000,
  "amount_evidence": "$10",
  "currency": "SGD",
  "payment_reference": "flowers",
  "reference_evidence": "for flowers",
  "requested_execution_date": null
}
```

`recipient_label`, `recipient_evidence`, `amount_minor`, and `amount_evidence`
may be `null` when absent. `amount_minor` and `amount_evidence` must be both
present or both null. `source_account_ref` and `source_evidence` follow the same
rule; a null source permits the existing unique-user-account default policy.
The gateway derives missing fields; the model does not claim that a field is
missing.

### PayNow mobile transfer

```json
{
  "contract_version": 2,
  "kind": "PAYNOW_MOBILE_TRANSFER",
  "source_account_ref": null,
  "source_evidence": null,
  "mobile_proxy": "86952933",
  "proxy_evidence": "8695 2933",
  "amount_minor": 1000,
  "amount_evidence": "ten dollars",
  "currency": "SGD",
  "payment_reference": "flowers",
  "reference_evidence": "for flowers",
  "requested_execution_date": null
}
```

`mobile_proxy`, `proxy_evidence`, `amount_minor`, and `amount_evidence` follow
the same paired-null rule. The model may propose a normalized proxy, but the
gateway derives the canonical proxy again from `proxy_evidence`.

All evidence fields must be verbatim substrings of the submitted text after
case-insensitive Unicode normalization and whitespace folding. Evidence is an
audit pointer, not authority. Payment reference and reference evidence are also
paired; an absent or ungrounded optional reference is omitted from the resolved
action rather than becoming an execution fact.

## Critical-field grounding

### Amounts

The gateway parses `amount_evidence` independently and requires exactly one
unambiguous positive SGD amount equal to `amount_minor`. The bounded parser
supports:

- `S$10`, `$10`, `10 SGD`, `10 dollars`, and decimal forms such as `$2.50`;
- English number words needed by the voice demo, including `ten dollars`,
  compound tens, hundreds, and thousands; and
- optional commas and ordinary whitespace.

The parser rejects zero, negative values, more than two decimal places,
conflicting amounts, unsupported currencies, and text outside the bounded
amount grammar. It uses integer minor units only.

Outcomes:

- no amount and both model fields null -> `MISSING_AMOUNT` clarification;
- model amount without verbatim evidence -> `UNGROUNDED_AMOUNT`;
- independently parsed value differs -> `AMOUNT_EVIDENCE_MISMATCH`;
- multiple or conflicting values -> `AMBIGUOUS_AMOUNT`.

None of these outcomes creates a receipt or changes a balance.

### Recipient and proxy evidence

The gateway requires the saved-name mention or PayNow proxy evidence to occur
in the original text. A proposed saved label remains untrusted and is resolved
only against saved beneficiaries. A PayNow mobile proxy is normalized by
removing spaces, hyphens, parentheses, and an optional Singapore `+65` prefix;
the result must be exactly eight decimal digits accepted by the synthetic demo
validator. The gateway never substitutes, rounds, completes, or fuzzy-matches
digits.

If a saved recipient or PayNow proxy is absent, version one returns the
non-mutating `MISSING_RECIPIENT` or `MISSING_PAYNOW_PROXY` outcome and asks the
user to submit a complete new request. Only a missing amount creates a stored
multi-turn clarification session. A named source must have grounded evidence;
otherwise the gateway returns `UNGROUNDED_SOURCE`.

## Stored amount clarification

When every required fact except amount is grounded and resolvable, the gateway
creates a `clarification_sessions` record, not a receipt.

| Field | Meaning |
| --- | --- |
| `clarification_id` | Server-generated opaque identifier |
| `request_id` | Original action idempotency key |
| `user_id` | Server-owned synthetic identity |
| `kind` | Stored action discriminator |
| `partial_intent_json` | Grounded fields from the first turn |
| `evidence_bundle_json` | Original text and verified evidence |
| `missing_field` | Exactly `amount_minor` in version one |
| `status` | `AWAITING_RESPONSE`, `COMPLETED`, `EXPIRED`, or `SUPERSEDED` |
| `expires_at` | Five minutes after creation |
| `created_at` | Server timestamp |

The response displays the locked facts and asks, `How much would you like to
send?` It contains `clarification_id` and expiry but no receipt coordinates.

### Clarification response API

```http
POST /api/clarifications/{clarification_id}/respond
Content-Type: application/json

{
  "request_id": "new-uuid",
  "text": "ten dollars"
}
```

This endpoint does not run the full Qwen extractor. A deterministic amount-only
parser accepts the bounded amount grammar. Any extra instruction, recipient,
phone number, source change, or multiple amount is rejected as
`CLARIFICATION_SCOPE_VIOLATION`; the user is told to start a new request.

Inside one `BEGIN IMMEDIATE` transaction, the gateway verifies ownership,
status, expiry, original request consistency, and response idempotency; fills
only the stored amount; marks the clarification completed; and creates the
appropriate pending receipt. A multi-turn-completed transfer always requires
receipt confirmation, even when the original wording contained an explicit
`send`, `pay`, or `transfer` verb.

Repeating the same clarification response UUID and text returns the same
receipt. Reusing it with different text is an idempotency conflict. A completed
or expired session cannot create another receipt.

## Synthetic PayNow lookup

The prototype introduces a narrow `PayNowDirectory` interface:

```text
lookup_mobile(canonical_proxy, request_id) ->
  lookup_reference, masked_name, beneficiary_id, expires_at
```

Its SQLite-backed synthetic implementation owns two tables:

- `paynow_proxies`: synthetic proxy, active flag, masked display name, and a
  server-owned synthetic destination beneficiary/account;
- `paynow_lookups`: opaque lookup reference, request, resolved proxy record,
  returned masked name, creation time, and five-minute expiry.

Seed `86952933` as explicitly synthetic and map it to a masked display value
such as `JXXX SXXXX`. The value shown in confirmation must come from the
directory record, never Qwen. Unknown, inactive, malformed, or expired proxies
produce non-mutating outcomes. Lookup results are request-idempotent.

PayNow transfers always create `AWAITING_CONFIRMATION` receipts. The interface
mirrors the real PayNow interaction shape—proxy lookup followed by recipient
verification—without claiming access to the production rail. ABS and DBS both
describe verification of the recipient name after proxy lookup:

- <https://www.abs.org.sg/e-payments/pay-now>
- <https://www.dbs.com.sg/personal/deposits/cas-paynow-faq.page>

## Bounded phonetic candidate retrieval

The saved-beneficiary resolver keeps its existing order:

1. exact full saved name;
2. exact token/prefix candidates;
3. lexical similarity candidates;
4. phonetic candidates;
5. missing.

The phonetic layer uses one deterministic, name-agnostic Latin phonetic key
over individual name tokens. It runs only when earlier layers produce no
candidate. A matching phonetic key returns one or more directory candidates
with reason `PHONETIC_RECIPIENT_MATCH`. It never returns resolution kind
`exact`, never authorizes immediate execution, and never stores spelling
variants in production data.

Evaluation includes held-out positives such as `Alesha` -> Alicia Powell and
held-out unrelated names. False positives are acceptable only as clearly
labeled confirmation candidates; they must never result in an automatic debit.

## Receipt contract version 2

Extend the receipt-bound execution model rather than creating a second payment
executor. Every receipt additionally binds:

- `destination_kind`: `SAVED_BENEFICIARY` or `PAYNOW_MOBILE`;
- `destination_reference`: beneficiary ID for saved recipients or canonical
  mobile proxy for PayNow;
- `lookup_reference`: null for saved beneficiaries, required for PayNow;
- `evidence_bundle_json`: verified evidence and all contributing user turns;
- `contract_version`: `2`.

The existing `beneficiary_id` remains the resolved internal destination foreign
key for both channels. Synthetic PayNow entries are excluded from saved-name
search. PayNow approval revalidates the stored lookup reference, exact proxy,
masked name, destination mapping, expiry, balance, receipt digest, and identity
inside the existing atomic execution transaction.

Any change to proxy, destination, masked name, amount, source, evidence bundle,
lookup reference, or expiry changes the canonical digest. Confirmation still
submits only receipt ID and digest; it never calls Qwen.

## Execution policy

| Path | Immediate execution allowed? | Required outcome |
| --- | --- | --- |
| Exact full saved name, explicit verb, all fields grounded in one turn | Yes, existing synthetic policy | Receipt created and executed atomically |
| Vague saved-name instruction | No | Receipt confirmation |
| Partial, fuzzy, or phonetic saved name | No | Candidate receipt confirmation |
| Amount completed through clarification | No | Receipt confirmation |
| PayNow mobile, whether saved-looking or new | No | Lookup-backed receipt confirmation |
| Missing or ungrounded critical field | No | Clarification or block; no receipt |

## UI behavior

- A missing-amount result shows the locked source and recipient or masked
  PayNow identity, an expiry indicator, and one amount-only response box.
- While a clarification is active, submitting its response calls the dedicated
  endpoint rather than `/api/actions`.
- A scope violation keeps balances unchanged and offers `Start a new request`.
- PayNow receipts show amount, source, normalized proxy, directory-owned masked
  name, payment reference when grounded, and expiry.
- Phonetic suggestions are labeled as possible matches and require selection.
- Confirmation continues to submit only receipt ID and digest.
- Reset removes clarification sessions, synthetic lookup records, receipts,
  authorization records, attempts, transactions, and ledger entries while
  restoring seed state.

## Stable outcome codes

Add:

- `MISSING_AMOUNT`
- `UNGROUNDED_AMOUNT`
- `AMOUNT_EVIDENCE_MISMATCH`
- `AMBIGUOUS_AMOUNT`
- `MISSING_RECIPIENT`
- `MISSING_PAYNOW_PROXY`
- `UNGROUNDED_RECIPIENT`
- `UNGROUNDED_SOURCE`
- `INVALID_PAYNOW_PROXY`
- `PAYNOW_PROXY_NOT_FOUND`
- `PAYNOW_LOOKUP_EXPIRED`
- `CLARIFICATION_NOT_FOUND`
- `CLARIFICATION_EXPIRED`
- `CLARIFICATION_ALREADY_COMPLETED`
- `CLARIFICATION_SCOPE_VIOLATION`
- `PHONETIC_RECIPIENT_MATCH`

Existing receipt, idempotency, insufficient-funds, model, and database outcomes
remain stable.

## Testing and completion evidence

Tests must demonstrate:

1. Missing amount produces a stored clarification and no receipt or mutation.
2. A model-invented amount without evidence is rejected, including with an
   exact saved recipient and explicit transfer verb.
3. Evidence/value mismatch, conflicting amounts, zero, negative, excessive
   precision, and unsupported currency are non-mutating.
4. Numeric and English-word amounts parse to literal expected minor units.
5. `ten dollars` fills only the stored amount and creates a confirmation receipt
   without another full Qwen interpretation.
6. A clarification reply attempting to change recipient/source or containing
   multiple amounts is rejected.
7. Replayed clarification responses create one receipt; expiry and concurrent
   responses cannot create duplicates.
8. `8695 2933`, `8695-2933`, and `+65 8695 2933` normalize to the same exact
   synthetic proxy, while any changed digit does not resolve.
9. PayNow confirmation shows the directory-owned masked name and receipt-bound
   lookup reference; Qwen cannot override either.
10. Tampered, expired, replayed, or mismatched PayNow receipts never debit.
11. `Alesha` produces a phonetic Alicia Powell candidate receipt, never an
    immediate execution; unrelated names remain missing or confirmation-only.
12. Existing exact, ambiguous John, fuzzy, prompt-injection, receipt,
    concurrency, rollback, and balanced-ledger tests remain green.
13. Live Qwen probes cover missing amount, explicit numeric amount, English-word
    amount, and PayNow mobile extraction. Synthetic ledger state is reset after.

The decisive metric is unauthorized ledger mutations: it must remain zero.
Extraction accuracy, clarification rate, candidate recall, and false candidate
rate are reported separately.

## Migration and operational boundary

The runtime is a disposable synthetic SQLite demo, but initialization must use
explicit additive migrations rather than assuming a fresh database. Saved-name
queries must exclude synthetic PayNow-only destinations. Existing version-one
receipts remain readable for idempotent history; new actions create only
version-two receipts.

The localhost server remains single-user with server-owned `demo_user_01`.
Production authentication, lookup authorization/rate limits, privacy controls,
bank-grade masking, fraud screening, regulatory compliance, and real payment
connectivity require a separate design and are not implied by this prototype.

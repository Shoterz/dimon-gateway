# Live Qwen evaluation — 2026-09-23

Model: `qwen35-9b-abliterated-q5` at the existing local OpenAI-compatible
endpoint. Each probe used a clean temporary SQLite ledger. The production demo
database was not used.

## Structured probes

### `send Alicia Powell money for flowers`

```json
{"contract_version":2,"kind":"BENEFICIARY_TRANSFER","source_account_ref":null,"source_evidence":null,"recipient_label":"Alicia Powell","recipient_evidence":"Alicia Powell","amount_minor":null,"amount_evidence":null,"currency":"SGD","payment_reference":"flowers","reference_evidence":"flowers","requested_execution_date":null}
```

Gateway: `CLARIFICATION_REQUEST / MISSING_AMOUNT`; zero receipts, zero
transactions. The amount-only reply `ten dollars` produced
`AMOUNT_CLARIFICATION_COMPLETED`; receipt approval settled S$10 exactly once.

### `send Alicia Powell $10 for flowers`

```json
{"contract_version":2,"kind":"BENEFICIARY_TRANSFER","source_account_ref":null,"source_evidence":null,"recipient_label":"Alicia Powell","recipient_evidence":"Alicia Powell","amount_minor":1000,"amount_evidence":"$10","currency":"SGD","payment_reference":"flowers","reference_evidence":"flowers","requested_execution_date":null}
```

Gateway at the time of extraction evaluation: one balanced synthetic
transaction. The final reviewed policy now routes memo-bearing text such as
`for flowers` to `ACTION_CONFIRMATION_REQUIRED`; the Qwen output above is still
the recorded raw extraction.

### `send Alicia Powell ten dollars for flowers`

```json
{"contract_version":2,"kind":"BENEFICIARY_TRANSFER","source_account_ref":null,"source_evidence":null,"recipient_label":"Alicia Powell","recipient_evidence":"Alicia Powell","amount_minor":1000,"amount_evidence":"ten dollars","currency":"SGD","payment_reference":"flowers","reference_evidence":"for flowers","requested_execution_date":null}
```

Gateway at the time of extraction evaluation: one balanced synthetic
transaction. The final reviewed policy now requires confirmation for this
memo-bearing form.

### `PayNow 8695 2933 ten dollars for flowers`

```json
{"contract_version":2,"kind":"PAYNOW_MOBILE_TRANSFER","source_account_ref":null,"source_evidence":null,"mobile_proxy":"8695 2933","proxy_evidence":"8695 2933","amount_minor":1000,"amount_evidence":"ten dollars","currency":"SGD","payment_reference":"flowers","reference_evidence":"for flowers","requested_execution_date":null}
```

Gateway: `CLARIFICATION_REQUEST / PAYNOW_CONFIRMATION_REQUIRED`; one receipt
for directory-owned `JXXX SXXXX`, zero transactions before approval. Receipt
approval settled S$10 exactly once to the synthetic destination.

### `send Alesha $10`

```json
{"contract_version":2,"kind":"BENEFICIARY_TRANSFER","source_account_ref":null,"source_evidence":null,"recipient_label":"Alesha","recipient_evidence":"Alesha","amount_minor":1000,"amount_evidence":"$10","currency":"SGD","payment_reference":null,"reference_evidence":null,"requested_execution_date":null}
```

Gateway: `CLARIFICATION_REQUEST / PHONETIC_RECIPIENT_MATCH`; one Alicia Powell
candidate receipt and zero transactions before confirmation.

## Safety observation

An initial live pass showed the model inventing `current` or `CURRENT_ACCOUNT`
as a source, and using the PayNow proxy as a source. The deterministic gateway
blocked every attempt with zero receipts or mutations. The general extraction
instructions were then tightened to forbid inferred/default sources and to
separate PayNow proxy semantics. The final outputs above passed without
weakening the grounding policy.

## Final ledger check

After the two approval probes, the temporary ledger was reset: zero
transactions and savings restored to `100000` minor units. All PayNow names,
accounts, proxies, lookups, receipts, and balances in this evaluation are
synthetic.

## Post-review authorization smoke check

After final policy hardening, live Qwen plus the current gateway produced:

- `send Alicia Powell $10` → immediate exact synthetic settlement, one balanced
  transaction.
- `send Alicia Powell $10 for flowers` →
  `CLARIFICATION_REQUEST / ACTION_CONFIRMATION_REQUIRED`, an immutable receipt,
  and zero transactions before approval.

The runtime ledger was reset again after these checks.

## Version-three amount-authority hardening

The extraction contract was subsequently narrowed from version two to version
three. Qwen no longer returns `amount_minor` or `amount_evidence`. Deterministic
Python scans the original user text, requires exactly one supported SGD amount,
and produces the integer minor-unit value used by policy and receipts. This
removes the observed `$10` to `10`-minor-unit model conversion error from the
authorization path rather than attempting to improve it with another prompt.

Regression coverage deliberately supplies a wrong legacy-style model amount of
`10` for `Send Alicia Powell $10`; the service ignores it, derives `1000`, and
settles S$10. Missing, multiple, negative, and unsupported amounts cannot be
replaced by a model guess. The synthetic PayNow proxy `8695 2933` is now exposed
to the browser from the server-owned directory.

The complete deterministic verification after this change passed 132/132
Dimon Live tests and 18/18 historical structured-action tests. A final live
version-three Qwen replay was attempted, but the existing model endpoint timed
out before returning a proposal. The gateway recorded `MODEL_UNAVAILABLE` and
made zero ledger mutations; therefore the earlier version-two live transcripts
above remain the latest successful live-model evidence.

## Atomic saved-beneficiary batch — 2026-09-24

The bounded batch demonstration request is:
`Send John Smith S$6.50 and Alicia Powell S$4`.

The version-four schema requires Qwen to return only the ordered recipient
labels and verbatim evidence. Amounts are derived from the original text by
the deterministic gateway. The implementation and focused tests verify a
single review receipt containing John Smith S$6.50, Alicia Powell S$4, and a
S$10.50 total; balances and ledger history remain unchanged before approval.
Approval then creates one atomic batch transaction with one S$10.50 source
debit and two destination credits, and an identical retry returns the stored
batch transaction ID.

This increment was verified with deterministic extractor fixtures rather than
a live Qwen call. Per the execution boundary, the live localhost service was
not restarted or mutated, so there is no new raw Qwen output or model latency
to record for this batch request. The existing configured model remains
`qwen35-9b-abliterated-q5`; the focused gateway result is
`BATCH_CONFIRMATION_REQUEST / BATCH_CONFIRMATION_REQUIRED` with zero
pre-approval transactions and zero balance mutation.

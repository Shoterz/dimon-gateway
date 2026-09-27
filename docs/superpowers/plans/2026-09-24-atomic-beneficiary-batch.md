# Atomic Saved-Beneficiary Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:test-driven-development` for every production change. This
> workspace has no usable Git metadata, so worktrees and commit checkpoints are
> unavailable; run tasks sequentially in the shared workspace and preserve the
> RED/GREEN outputs in the parent session.

**Goal:** Turn one typed request containing two to four distinct saved-recipient
transfers into one immutable review receipt and one exactly-once, all-or-nothing
synthetic SQLite batch.

**Architecture:** Qwen3.5 proposes only recipient labels and verbatim evidence.
Pure Python extracts amount spans from the original text and pairs recipients
and amounts by source-text order. The gateway resolves exact server-owned IDs,
stores one hashed batch receipt, and approval commits one batch transaction
containing one total source debit and one credit per item.

**Tech Stack:** Python 3.10, FastAPI, SQLite, JSON Schema draft 2020-12,
`unittest`, browser ES modules, Node-based client tests, local Qwen3.5 through
the existing OpenAI-compatible llama.cpp endpoint.

**Spec:**
`docs/superpowers/specs/2026-09-24-atomic-beneficiary-batch-design.md`

## Global Constraints

- Support only two to four distinct `BENEFICIARY_TRANSFER` items.
- Every amount comes from deterministic parsing of the original user text;
  Qwen never returns an amount field.
- Batch recipient and amount counts must match exactly.
- Bind recipients and amounts by their occurrence order in the original text.
- Require exact saved-beneficiary resolution; fuzzy and PayNow batch items are
  out of scope.
- Every batch requires one immutable receipt confirmation; never auto-execute.
- Approval executes all items in one SQLite transaction or changes nothing.
- Replaying the identical approval returns the stored result without another
  debit.
- Existing single-beneficiary and PayNow behavior must remain unchanged.
- Use no new runtime dependencies and do not create a large generated corpus.

## Review Focus

- A recipient name repeated in prose must produce `AMBIGUOUS_BATCH_BINDING`,
  not silently select one occurrence; Task 2 pins this.
- Amount-first and recipient-first clauses must bind to the same ordered item
  values; Task 2 pins both forms.
- A balance reduction between review and approval must roll back the whole
  batch; Task 4 pins this.
- Concurrent duplicate approvals must create one batch transaction; Task 4
  pins this.
- An existing database initialized before these tables existed must migrate
  without deleting single-transfer history; Task 4 pins this.

---

### Task 1: Version-Four Batch Proposal Contract

**Files:**
- Modify: `apps/dimon_live/action_intent.schema.json`
- Modify: `apps/dimon_live/model_adapter.py`
- Modify: `apps/dimon_live/tests/test_model_adapter.py`

**Interfaces:**
- Consumes: existing `validate_intent(raw_output: str) -> dict[str, Any]` and
  `QwenTransferExtractor.extract(text: str) -> ExtractedIntent`.
- Produces: validated `BENEFICIARY_TRANSFER_BATCH` proposals with
  `contract_version == 4` and `items: list[dict[str, str]]`; existing
  version-three proposals remain accepted.

- [ ] **Step 1: Write failing validator tests**

Add the fixture and tests:

```python
BATCH_INTENT = {
    "contract_version": 4,
    "kind": "BENEFICIARY_TRANSFER_BATCH",
    "source_account_ref": None,
    "source_evidence": None,
    "items": [
        {
            "recipient_label": "John Smith",
            "recipient_evidence": "John Smith",
        },
        {
            "recipient_label": "Alicia Powell",
            "recipient_evidence": "Alicia Powell",
        },
    ],
    "currency": "SGD",
    "requested_execution_date": None,
}

def test_validate_intent_accepts_bounded_beneficiary_batch(self):
    self.assertEqual(validate_intent(json.dumps(BATCH_INTENT)), BATCH_INTENT)

def test_validate_intent_rejects_invalid_batch_shapes(self):
    cases = (
        {**BATCH_INTENT, "items": BATCH_INTENT["items"][:1]},
        {**BATCH_INTENT, "items": BATCH_INTENT["items"] * 3},
        {
            **BATCH_INTENT,
            "items": [{**BATCH_INTENT["items"][0], "amount_minor": 650},
                      BATCH_INTENT["items"][1]],
        },
        {
            **BATCH_INTENT,
            "items": [{"mobile_proxy": "86952933",
                       "proxy_evidence": "86952933"},
                      BATCH_INTENT["items"][1]],
        },
    )
    for intent in cases:
        with self.subTest(intent=intent):
            with self.assertRaises(InvalidModelOutput):
                validate_intent(json.dumps(intent))
```

- [ ] **Step 2: Run the validator tests and observe RED**

Run:

```bash
python3 -B -m unittest \
  apps.dimon_live.tests.test_model_adapter.QwenTransferExtractorTests.test_validate_intent_accepts_bounded_beneficiary_batch \
  apps.dimon_live.tests.test_model_adapter.QwenTransferExtractorTests.test_validate_intent_rejects_invalid_batch_shapes -v
```

Expected: the valid batch fails with `unsupported intent kind`.

- [ ] **Step 3: Extend the JSON Schema and manual validator**

Add a third `oneOf` object with exact required keys, `contract_version: 4`,
`kind: BENEFICIARY_TRANSFER_BATCH`, and:

```json
"items": {
  "type": "array",
  "minItems": 2,
  "maxItems": 4,
  "items": {
    "type": "object",
    "additionalProperties": false,
    "required": ["recipient_label", "recipient_evidence"],
    "properties": {
      "recipient_label": {"type": "string", "minLength": 1},
      "recipient_evidence": {"type": "string", "minLength": 1}
    }
  }
}
```

In `validate_intent`, define `BATCH_FIELDS`, accept version four only for the
batch kind, require a real list of length two through four, require exact item
keys, and call `_require_paired` for each item. Do not accept payment-reference,
amount, proxy, approval, or execution keys.

Replace the first prompt line with:

```text
You extract one transfer proposal or one saved-beneficiary transfer batch.
Use BENEFICIARY_TRANSFER_BATCH only when the user names two to four distinct
saved recipients with one explicit amount expression for each. For batch
items, copy only recipient_label and recipient_evidence; never return amounts.
Preserve recipient order from the user's text.
```

Raise `max_tokens` from `256` to `512` so four bounded items cannot be
truncated.

- [ ] **Step 4: Run Task 1 tests and the full adapter file**

Run:

```bash
python3 -B -m unittest apps.dimon_live.tests.test_model_adapter -v
```

Expected: all model-adapter tests pass, including unchanged version-three
single and PayNow cases.

### Task 2: Deterministic Multi-Amount Span Binding

**Files:**
- Modify: `apps/dimon_live/grounding.py`
- Create: `apps/dimon_live/batch_grounding.py`
- Modify: `apps/dimon_live/tests/test_grounding.py`
- Create: `apps/dimon_live/tests/test_batch_grounding.py`

**Interfaces:**
- Consumes: `parse_sgd_amount(value, require_full_match=True)` and the existing
  amount mention regular expressions.
- Produces:
  `derive_amount_mentions_from_text(value: str) -> tuple[AmountMention, ...]`
  and
  `bind_batch_evidence(text: str, recipient_evidences: Sequence[str]) -> tuple[BoundBatchEvidence, ...]`.

- [ ] **Step 1: Write failing span-extraction tests**

Add to `test_grounding.py`:

```python
def test_amount_mentions_preserve_values_evidence_and_spans(self):
    text = "Send John Smith S$6.50 and Alicia Powell four dollars"
    mentions = derive_amount_mentions_from_text(text)
    self.assertEqual(
        [(item.amount_minor, item.evidence) for item in mentions],
        [(650, "S$6.50"), (400, "four dollars")],
    )
    self.assertEqual(
        [text[item.start:item.end] for item in mentions],
        ["S$6.50", "four dollars"],
    )
```

- [ ] **Step 2: Run the span test and observe RED**

Run:

```bash
python3 -B -m unittest \
  apps.dimon_live.tests.test_grounding.GroundingTests.test_amount_mentions_preserve_values_evidence_and_spans -v
```

Expected: import failure because `derive_amount_mentions_from_text` does not
exist.

- [ ] **Step 3: Expose parsed amount spans without changing single parsing**

Add:

```python
@dataclass(frozen=True)
class AmountMention:
    amount_minor: int
    evidence: str
    start: int
    end: int

def derive_amount_mentions_from_text(value: str) -> tuple[AmountMention, ...]:
    matches = _amount_matches(value)
    return tuple(
        AmountMention(
            parse_sgd_amount(match.group(0), require_full_match=True),
            match.group(0),
            match.start(),
            match.end(),
        )
        for match in matches
    )
```

Refactor the existing private matcher to return non-overlapping `re.Match`
objects. Keep `_amount_mentions` as the string projection used by old callers,
and implement `derive_amount_from_text` through the new span API. Existing
single-amount results and reason codes must not change.

- [ ] **Step 4: Run all grounding tests GREEN**

Run:

```bash
python3 -B -m unittest apps.dimon_live.tests.test_grounding -v
```

Expected: all grounding tests pass.

- [ ] **Step 5: Write failing batch-binding tests**

Create `test_batch_grounding.py` with:

```python
class BatchGroundingTests(unittest.TestCase):
    def test_binds_recipient_first_and_amount_first_by_text_order(self):
        cases = (
            ("Send John Smith $6.50 and Alicia Powell $4",
             ("John Smith", "Alicia Powell")),
            ("Send $6.50 to John Smith and $4 to Alicia Powell",
             ("John Smith", "Alicia Powell")),
        )
        for text, recipients in cases:
            with self.subTest(text=text):
                bound = bind_batch_evidence(text, recipients)
                self.assertEqual(
                    [(item.recipient_evidence, item.amount_minor)
                     for item in bound],
                    [("John Smith", 650), ("Alicia Powell", 400)],
                )

    def test_rejects_count_mismatch_and_repeated_evidence(self):
        cases = (
            ("Send John Smith and Alicia Powell $4",
             ("John Smith", "Alicia Powell"),
             "BATCH_RECIPIENT_COUNT_MISMATCH"),
            ("Send John Smith $5 and John Smith $7",
             ("John Smith", "John Smith"),
             "AMBIGUOUS_BATCH_BINDING"),
        )
        for text, recipients, reason in cases:
            with self.subTest(text=text):
                with self.assertRaisesRegex(GroundingError, reason):
                    bind_batch_evidence(text, recipients)
```

- [ ] **Step 6: Run binding tests and observe RED**

Run:

```bash
python3 -B -m unittest apps.dimon_live.tests.test_batch_grounding -v
```

Expected: import failure because `batch_grounding.py` does not exist.

- [ ] **Step 7: Implement the pure binder**

Create:

```python
@dataclass(frozen=True)
class BoundBatchEvidence:
    recipient_evidence: str
    recipient_start: int
    amount_minor: int
    amount_evidence: str
    amount_start: int

def bind_batch_evidence(
    text: str,
    recipient_evidences: Sequence[str],
) -> tuple[BoundBatchEvidence, ...]:
```

Require two to four non-empty evidence strings. Find each evidence with
case-sensitive `str.find`, require exactly one occurrence using a second search
after the first, reject overlapping recipient spans, derive all amount spans,
require equal counts, sort both collections by their original start offsets,
and zip them. Raise only `GroundingError("AMBIGUOUS_BATCH_BINDING")` or
`GroundingError("BATCH_RECIPIENT_COUNT_MISMATCH")` from this module.

- [ ] **Step 8: Run Task 2 tests GREEN**

Run:

```bash
python3 -B -m unittest \
  apps.dimon_live.tests.test_grounding \
  apps.dimon_live.tests.test_batch_grounding -v
```

Expected: all grounding and batch-binding tests pass.

### Task 3: Canonical Batch Receipt Contract

**Files:**
- Create: `apps/dimon_live/batch_receipts.py`
- Create: `apps/dimon_live/tests/test_batch_receipts.py`

**Interfaces:**
- Consumes: only standard-library `dataclasses`, `datetime`, `hashlib`, and
  `json`.
- Produces: `BatchReceiptItemDraft`, `BatchReceiptDraft`, and
  `build_batch_receipt_record(draft, *, now, receipt_id, nonce) -> dict`.

- [ ] **Step 1: Write failing canonicalization and tamper tests**

Create tests that build a fixed draft:

```python
def draft(self):
    return BatchReceiptDraft(
        request_id="00000000-0000-4000-8000-000000000140",
        original_text="Send John Smith $6.50 and Alicia Powell $4",
        user_id="demo_user_01",
        source_account_id="acct_savings_01",
        currency="SGD",
        items=(
            BatchReceiptItemDraft(0, "beneficiary_john_smith_01",
                "acct_john_smith_01", "John Smith", 650,
                "John Smith", "$6.50"),
            BatchReceiptItemDraft(1, "beneficiary_alicia_powell_01",
                "acct_alicia_powell_01", "Alicia Powell", 400,
                "Alicia Powell", "$4"),
        ),
    )

def test_digest_binds_ordered_items_and_total(self):
    record = build_batch_receipt_record(
        self.draft(), now=NOW,
        receipt_id="batch_receipt_fixed", nonce="batch_nonce_fixed",
    )
    self.assertEqual(record["canonical_payload"]["total_minor"], 1050)
    self.assertEqual(
        [item["amount_minor"] for item in
         record["canonical_payload"]["items"]], [650, 400]
    )
    reversed_items = dataclasses.replace(
        self.draft(), items=tuple(reversed(self.draft().items))
    )
    changed = build_batch_receipt_record(
        reversed_items, now=NOW,
        receipt_id="batch_receipt_fixed", nonce="batch_nonce_fixed",
    )
    self.assertNotEqual(record["receipt_digest"], changed["receipt_digest"])
```

Also test duplicate positions, duplicate beneficiaries, zero amount, USD,
naive time, and fewer than two items all raise `ValueError`.

- [ ] **Step 2: Run receipt tests and observe RED**

Run:

```bash
python3 -B -m unittest apps.dimon_live.tests.test_batch_receipts -v
```

Expected: import failure because `batch_receipts.py` does not exist.

- [ ] **Step 3: Implement immutable batch receipt construction**

Use frozen dataclasses with these exact fields:

```python
@dataclass(frozen=True)
class BatchReceiptItemDraft:
    position: int
    beneficiary_id: str
    destination_account_id: str
    recipient_display_name: str
    amount_minor: int
    recipient_evidence: str
    amount_evidence: str

@dataclass(frozen=True)
class BatchReceiptDraft:
    request_id: str
    original_text: str
    user_id: str
    source_account_id: str
    currency: str
    items: tuple[BatchReceiptItemDraft, ...]
```

Canonicalize with `json.dumps(payload, ensure_ascii=False,
sort_keys=True, separators=(",", ":"))`. Use contract version `1` for the
stored batch-receipt contract, compute `total_minor` from items, set a five
minute UTC expiry, and hash the canonical UTF-8 bytes using SHA-256.

- [ ] **Step 4: Run Task 3 tests GREEN**

Run:

```bash
python3 -B -m unittest apps.dimon_live.tests.test_batch_receipts -v
```

Expected: all batch-receipt tests pass.

### Task 4: Batch Persistence and Atomic Ledger Execution

**Files:**
- Modify: `apps/dimon_live/database.py`
- Modify: `apps/dimon_live/tests/test_database.py`

**Interfaces:**
- Consumes: `BatchReceiptDraft` and `build_batch_receipt_record` from Task 3.
- Produces:
  `Database.create_pending_batch_receipt(...) -> dict[str, Any]`,
  `Database.get_batch_receipt(receipt_id: str) -> dict[str, Any] | None`, and
  `Database.approve_batch_receipt(receipt_id: str, receipt_digest: str,
  user_id: str = DEMO_USER_ID, now: datetime | None = None) -> dict[str, Any]`.

- [ ] **Step 1: Write failing persistence and atomic-approval test**

Add a `batch_draft()` helper matching Task 3, then:

```python
def test_batch_receipt_approval_commits_all_items_once(self):
    before = {row["label"]: row["balance_minor"]
              for row in self.database.get_state()["accounts"]}
    pending = self.database.create_pending_batch_receipt(
        self.batch_draft(), raw_model_output="{}",
        validated_intent={"kind": "BENEFICIARY_TRANSFER_BATCH"},
        now=FIXED_NOW,
    )
    self.assertEqual(self.database.get_state()["transactions"], [])
    receipt = pending["batch_receipt"]
    executed = self.database.approve_batch_receipt(
        receipt["receipt_id"], receipt["receipt_digest"],
        now=FIXED_NOW + timedelta(minutes=1),
    )
    replay = self.database.approve_batch_receipt(
        receipt["receipt_id"], receipt["receipt_digest"],
        now=FIXED_NOW + timedelta(minutes=2),
    )
    after = {row["label"]: row["balance_minor"]
             for row in self.database.get_state()["accounts"]}
    self.assertEqual(after["savings"], before["savings"] - 1050)
    self.assertEqual(after["John Smith"], before["John Smith"] + 650)
    self.assertEqual(after["Alicia Powell"], before["Alicia Powell"] + 400)
    self.assertEqual(executed["batch_transaction_id"],
                     replay["batch_transaction_id"])
    self.assertEqual(len(self.database.get_state()["transactions"]), 2)
```

- [ ] **Step 2: Run atomic approval test and observe RED**

Run:

```bash
python3 -B -m unittest \
  apps.dimon_live.tests.test_database.DatabaseSeedTests.test_batch_receipt_approval_commits_all_items_once -v
```

Expected: `Database` has no `create_pending_batch_receipt` method.

- [ ] **Step 3: Add migration-safe batch tables**

Add `BATCH_SCHEMA` using `CREATE TABLE IF NOT EXISTS` for:

```sql
batch_review_receipts(
  receipt_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE REFERENCES action_attempts(request_id),
  user_id TEXT NOT NULL,
  source_account_id TEXT NOT NULL REFERENCES accounts(account_id),
  total_minor INTEGER NOT NULL CHECK(total_minor > 0),
  currency TEXT NOT NULL CHECK(currency = 'SGD'),
  original_text TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  nonce TEXT NOT NULL UNIQUE,
  canonical_json TEXT NOT NULL,
  receipt_digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('AWAITING_CONFIRMATION','EXECUTED','EXPIRED')),
  created_at TEXT NOT NULL
);
batch_receipt_items(
  receipt_id TEXT NOT NULL REFERENCES batch_review_receipts(receipt_id),
  position INTEGER NOT NULL,
  beneficiary_id TEXT NOT NULL REFERENCES beneficiaries(beneficiary_id),
  destination_account_id TEXT NOT NULL REFERENCES accounts(account_id),
  recipient_display_name TEXT NOT NULL,
  amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
  recipient_evidence TEXT NOT NULL,
  amount_evidence TEXT NOT NULL,
  PRIMARY KEY(receipt_id, position),
  UNIQUE(receipt_id, beneficiary_id)
);
batch_ledger_transactions(
  batch_transaction_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE REFERENCES action_attempts(request_id),
  total_minor INTEGER NOT NULL CHECK(total_minor > 0),
  currency TEXT NOT NULL CHECK(currency = 'SGD'),
  created_at TEXT NOT NULL
);
batch_ledger_entries(
  entry_id TEXT PRIMARY KEY,
  batch_transaction_id TEXT NOT NULL
    REFERENCES batch_ledger_transactions(batch_transaction_id),
  account_id TEXT NOT NULL REFERENCES accounts(account_id),
  item_position INTEGER,
  delta_minor INTEGER NOT NULL CHECK(delta_minor != 0)
);
batch_receipt_executions(
  receipt_id TEXT PRIMARY KEY REFERENCES batch_review_receipts(receipt_id),
  batch_transaction_id TEXT NOT NULL UNIQUE
    REFERENCES batch_ledger_transactions(batch_transaction_id),
  executed_at TEXT NOT NULL
);
```

Execute `BATCH_SCHEMA` during every `initialize()` call. Add a migration test
that initializes a database, inserts one existing single transfer, calls
`initialize()` again, and asserts both the old transaction and all five batch
tables exist.

- [ ] **Step 4: Implement receipt persistence and atomic approval**

`create_pending_batch_receipt` must start `BEGIN IMMEDIATE`, enforce request
idempotency, reload source and beneficiaries, reconstruct a server-owned draft,
build the record, insert `action_attempts`, the receipt, and ordered items, then
commit. Its response must contain `BATCH_CONFIRMATION_REQUEST`,
`BATCH_CONFIRMATION_REQUIRED`, and the public batch receipt from the spec.

`approve_batch_receipt` must start `BEGIN IMMEDIATE` and:

1. Return the stored execution when `batch_receipt_executions` already exists.
2. Verify user, digest, status, expiry, canonical JSON reconstruction, source,
   item mappings, item sum, and current funds.
3. Insert one `batch_ledger_transactions` row.
4. Debit the source by `total_minor` once.
5. Credit each destination by its own `amount_minor`.
6. Insert one negative source ledger entry and one positive entry per item.
7. Assert `SUM(delta_minor) = 0` for the batch transaction.
8. Insert `batch_receipt_executions`, mark the receipt `EXECUTED`, update the
   action-attempt response, and commit.

Return:

```python
{
    "outcome": "EXECUTED_SYNTHETIC_BATCH",
    "batch_transaction_id": batch_transaction_id,
    "amount_minor": total_minor,
    "resolved_actions": resolved_actions,
}
```

Extend `get_state()` to append one display transaction per batch item and
extend `reset()` to delete batch rows in foreign-key order.

- [ ] **Step 5: Add failure and concurrency tests**

Add tests that mutate each of these independently before approval:

```python
cases = (
    ("receipt_digest = '" + "0" * 64 + "'", "BATCH_RECEIPT_TAMPERED"),
    ("total_minor = 1051", "BATCH_RECEIPT_TAMPERED"),
    ("expires_at = '2020-01-01T00:00:00+00:00'", "BATCH_RECEIPT_EXPIRED"),
)
```

For each case, assert balances, batch transactions, and entries remain
unchanged. Add a destination-remap test, a balance-reduced-after-review test
expecting `INSUFFICIENT_FUNDS`, and a two-thread barrier test asserting both
approvals return one `batch_transaction_id` and only one source debit exists.

- [ ] **Step 6: Run database tests GREEN**

Run:

```bash
python3 -B -m unittest apps.dimon_live.tests.test_database -v
```

Expected: all old and new database tests pass.

### Task 5: Service Orchestration and HTTP Approval Boundary

**Files:**
- Modify: `apps/dimon_live/service.py`
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/tests/test_service.py`
- Modify: `apps/dimon_live/tests/test_api.py`

**Interfaces:**
- Consumes: validated batch proposal from Task 1, bound evidence from Task 2,
  drafts from Task 3, and database methods from Task 4.
- Produces: `ActionService.process` batch branch,
  `ActionService.approve_batch(receipt_id, digest)`, and
  `POST /api/batch-receipts/{receipt_id}/approve`.

- [ ] **Step 1: Write failing service hero and fail-closed tests**

Add `BATCH_INTENT` from Task 1 to `test_service.py`, then:

```python
def test_batch_request_creates_one_exact_nonmutating_receipt(self):
    before = self.balances()
    result = self.service_for(BATCH_INTENT).process(
        REQUEST_ID,
        "Send John Smith $6.50 and Alicia Powell $4",
    )
    self.assertEqual(result["outcome"], "BATCH_CONFIRMATION_REQUEST")
    self.assertEqual(result["reason_code"], "BATCH_CONFIRMATION_REQUIRED")
    self.assertEqual(result["batch_receipt"]["total_minor"], 1050)
    self.assertEqual(
        [(item["recipient_display_name"], item["amount_minor"])
         for item in result["batch_receipt"]["items"]],
        [("John Smith", 650), ("Alicia Powell", 400)],
    )
    self.assertEqual(self.balances(), before)
    self.assertEqual(self.database.get_state()["transactions"], [])

def test_batch_unknown_duplicate_and_count_mismatch_never_create_receipt(self):
    cases = (
        ({**BATCH_INTENT, "items": [BATCH_INTENT["items"][0],
                                     BATCH_INTENT["items"][0]]},
         "Send John Smith $5 and John Smith $7",
         "AMBIGUOUS_BATCH_BINDING"),
        (BATCH_INTENT,
         "Send John Smith and Alicia Powell $4",
         "BATCH_RECIPIENT_COUNT_MISMATCH"),
        ({**BATCH_INTENT, "items": [BATCH_INTENT["items"][0],
             {"recipient_label": "Nobody", "recipient_evidence": "Nobody"}]},
         "Send John Smith $5 and Nobody $7",
         "UNKNOWN_RECIPIENT"),
        (BATCH_INTENT,
         "PayNow John Smith $5 and Alicia Powell $7 using 86952933",
         "AMBIGUOUS_BATCH_BINDING"),
        (BATCH_INTENT,
         "Send John Smith $5 and Alicia Powell $7 if they call",
         "AMBIGUOUS_BATCH_BINDING"),
        (BATCH_INTENT,
         "Do not send John Smith $5 and Alicia Powell $7",
         "NEGATED_ACTION"),
    )
```

For each case, assert `BLOCKED`, the stated reason, zero receipt rows, and no
balance or ledger changes.

- [ ] **Step 2: Run service tests and observe RED**

Run:

```bash
python3 -B -m unittest \
  apps.dimon_live.tests.test_service.ActionServiceTests.test_batch_request_creates_one_exact_nonmutating_receipt \
  apps.dimon_live.tests.test_service.ActionServiceTests.test_batch_unknown_duplicate_and_count_mismatch_never_create_receipt -v
```

Expected: batch requests fail before producing `BATCH_CONFIRMATION_REQUEST`.

- [ ] **Step 3: Add a dedicated batch branch before single amount derivation**

Immediately after kind validation and source-evidence grounding, route
`BENEFICIARY_TRANSFER_BATCH` to a private `_process_beneficiary_batch` method.
This ordering is mandatory because the existing single-action amount parser
correctly rejects multiple amounts.

The method must:

1. Reject negation, explicit PayNow text, and conditional markers `if`,
   `unless`, `when`, `before`, and `after`.
2. Resolve the one explicit or default source.
3. Call `bind_batch_evidence` using model recipient evidence strings.
4. Require `_same_grounded_value` for each label/evidence pair.
5. Resolve every recipient with `Database.resolve_beneficiary` and require
   `resolution.kind == "exact"`.
6. Reject repeated canonical beneficiary IDs with
   `DUPLICATE_BATCH_RECIPIENT`.
7. Build ordered `BatchReceiptItemDraft` values using server-owned names and
   destinations plus deterministic amounts.
8. Check the total against current source funds.
9. Call `create_pending_batch_receipt` and append trace stages
   `BATCH_EVIDENCE_BOUND`, `BATCH_RECIPIENTS_RESOLVED`, and
   `BATCH_CONFIRMATION_REQUIRED`.

Add `approve_batch` as a thin exception-mapping wrapper around the database
method, producing trace stages `BATCH_RECEIPT_APPROVAL_RECEIVED`,
`BATCH_RECEIPT_REVALIDATED`, and `SQLITE_BATCH_COMMITTED`.

- [ ] **Step 4: Add and test strict HTTP approval endpoint**

Reuse `ReceiptApprovalRequest` and add:

```python
@application.post("/api/batch-receipts/{receipt_id}/approve")
async def approve_batch_receipt(
    receipt_id: str,
    request: ReceiptApprovalRequest,
):
    return service.approve_batch(receipt_id, request.receipt_digest)
```

API tests must assert extra request fields return HTTP 422, the digest pattern
is enforced, one valid approval returns `EXECUTED_SYNTHETIC_BATCH`, and a replay
returns the same `batch_transaction_id`.

- [ ] **Step 5: Run service and API tests GREEN**

Run:

```bash
python3 -B -m unittest \
  apps.dimon_live.tests.test_service \
  apps.dimon_live.tests.test_api -v
```

Expected: all service/API tests pass, including existing immediate single
transfer and mandatory PayNow confirmation cases.

### Task 6: Batch Review and Settlement UI

**Files:**
- Create: `apps/dimon_live/static/batch_receipt_approval.mjs`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/app.css`
- Modify: `apps/dimon_live/tests/test_ui_client.py`

**Interfaces:**
- Consumes: `batch_receipt` response from Task 5 and strict batch approval API.
- Produces:
  `requestBatchReceiptApproval(receipt, fetchImpl = fetch) -> Promise<{kind,
  result?, message?}>` plus one visible batch review card.

- [ ] **Step 1: Write failing retry-coordinate client test**

Add a module constant and test patterned on the single receipt client:

```javascript
const receipt = {
  receipt_id: "batch_receipt_fixed",
  receipt_digest: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
};
const first = await requestBatchReceiptApproval(receipt, fakeFetch);
const second = await requestBatchReceiptApproval(receipt, fakeFetch);
```

Assert both calls target
`/api/batch-receipts/batch_receipt_fixed/approve`, submit only
`receipt_digest`, the interrupted first call returns `UNKNOWN`, and the retry
returns the original result.

- [ ] **Step 2: Run UI client test and observe RED**

Run:

```bash
python3 -B -m unittest \
  apps.dimon_live.tests.test_ui_client.ReceiptApprovalClientTests.test_unknown_batch_approval_retries_identical_coordinates -v
```

Expected: module import fails because `batch_receipt_approval.mjs` is absent.

- [ ] **Step 3: Implement the approval client and batch card**

Implement the same transport semantics as `receipt_approval.mjs`, changing
only the endpoint path. In `renderResult`, handle
`BATCH_CONFIRMATION_REQUEST` before the generic clarification branch. Render
ordered rows with `From`, `To`, and `Amount`, then a visually separated
`Total debit`, expiry, and one `Confirm exact batch` button.

On success `EXECUTED_SYNTHETIC_BATCH`, render:

```text
Synthetic batch settled
The complete plan moved exactly once.
S$10.50 was split across 2 transfers in one atomic database transaction.
```

Extend `renderCommit` to show one `BEGIN IMMEDIATE`, one total source debit,
one credit line per `resolved_actions` item, the zero-sum invariant, and one
commit using `batch_transaction_id`. Add stage copy for the three new approval
trace events. Add only the CSS needed to distinguish item rows and total.

- [ ] **Step 4: Run all client tests GREEN**

Run:

```bash
python3 -B -m unittest apps.dimon_live.tests.test_ui_client -v
```

Expected: all UI client tests pass.

### Task 7: Documentation, Full Regression, and Live Qwen3.5 Demonstration

**Files:**
- Modify: `apps/dimon_live/README.md`
- Modify: `docs/live-qwen-evaluation-2026-09-23.md`
- Verify: all files changed in Tasks 1–6

**Interfaces:**
- Consumes: completed schema, grounding, gateway, approval, and UI behavior.
- Produces: reproducible demo instructions and fresh verification evidence.

- [ ] **Step 1: Document the bounded batch example and limitations**

Add this demo entry:

```markdown
Atomic batch — `Send John Smith S$6.50 and Alicia Powell S$4`.
Expected: one review card lists both transfers and a S$10.50 total. Before
approval no balance changes. One approval commits both routes; retry returns
the same batch transaction. PayNow, shared amounts, fuzzy recipients, and more
than four items are intentionally unsupported in a batch.
```

Record the live raw Qwen output, model ID, model latency, gateway outcome, and
pre-approval zero-mutation evidence in the evaluation note.

- [ ] **Step 2: Run Python compilation and both complete suites**

Run:

```bash
python3 -m py_compile \
  apps/dimon_live/model_adapter.py \
  apps/dimon_live/grounding.py \
  apps/dimon_live/batch_grounding.py \
  apps/dimon_live/batch_receipts.py \
  apps/dimon_live/database.py \
  apps/dimon_live/service.py \
  apps/dimon_live/app.py
python3 -B -m unittest discover -s apps/dimon_live/tests -q
python3 -B -m unittest discover -s experiments/local-structured-action/tests -q
```

Expected: compilation exits zero and both suites report `OK` with zero
failures or errors.

- [ ] **Step 3: Restart only Dimon and reset the synthetic ledger**

Resolve the exact port-8890 PID, terminate only that process, restart:

```bash
python3 -m uvicorn apps.dimon_live.app:app --host 127.0.0.1 --port 8890
```

Verify `/api/state` reports `qwen35-9b-abliterated-q5`, then POST `/api/reset`.

- [ ] **Step 4: Run the live hero request without approval**

POST:

```json
{
  "request_id": "00000000-0000-4000-8000-000000000149",
  "text": "Send John Smith S$6.50 and Alicia Powell S$4"
}
```

Expected: `BATCH_CONFIRMATION_REQUEST`, two exact items, total `1050`, model
and grounding trace stages, and zero transactions/ledger entries before
approval. Reset once more so the UI is clean for the user.

- [ ] **Step 5: Conduct a fresh final review**

Review the complete changed-file set against the spec with special attention
to the five `Review Focus` cases. Any Important or Critical finding receives a
new failing regression test, one minimal fix, and a fresh full-suite run.
Report Minor findings rather than expanding scope.

## Execution Notes

The tasks are sequential because each consumes a precise interface from the
previous task. If delegated, use one Luna xhigh implementer at a time in this
shared workspace; the main agent independently reads every diff and runs every
named RED/GREEN command. Do not run multiple editing agents concurrently.

The repository's `.git` directory is an empty read-only mount, so neither Git
commits nor isolated worktrees are possible in this environment. Test evidence,
the spec, and this plan are the recovery record for this implementation run.

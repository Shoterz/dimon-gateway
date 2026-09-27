# Receipt-Bound Confirmation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Qwen-reinterpreted confirmations with immutable, server-created transfer receipts that execute exactly once after policy authorization or user approval.

**Architecture:** Qwen remains a one-shot, untrusted intent extractor. Deterministic service code resolves server-owned IDs, while SQLite creates, hashes, stores, authorizes, and executes receipts inside `BEGIN IMMEDIATE` transactions; the approval route accepts only an opaque receipt ID and its digest and never calls Qwen.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic v2, Python `sqlite3`, SHA-256 from `hashlib`, framework-free HTML/CSS/JavaScript, `unittest`, and the existing local llama.cpp/Qwen endpoint.

**Spec:** `docs/superpowers/specs/2026-09-23-receipt-bound-confirmation-design.md`

## Global Constraints

- Synthetic localhost demo only; never connect this executor to a real bank, broker, payment API, or production identity system.
- Version one supports only `BENEFICIARY_TRANSFER`, immediate SGD transfers, and server-side identity `demo_user_01`.
- Qwen may interpret the initial request once and receives no receipt ID, digest, nonce, SQL, Python, shell, approval credential, or ledger tool.
- Confirmation and execution never call Qwen and never accept amount, currency, source, beneficiary, or authorization method from the browser.
- Every receipt expires five minutes after creation; an identical replay of an already executed receipt returns the stored transaction even after expiry.
- Exact explicit commands still execute immediately through the same receipt execution engine using `EXPLICIT_COMMAND_POLICY`.
- Approximate, ambiguous, and vague commands create `AWAITING_CONFIRMATION` receipts and perform no ledger mutation.
- The backend is the only SQLite owner, and each successful transfer is one atomic balanced debit and credit linked to one receipt.
- Use installed dependencies only; no package installation is required.
- Keep all Dimon Live tests and `experiments/local-structured-action` tests passing.
- This workspace is not a Git repository. Do not run `git init`; commit steps are intentionally omitted.

## Review Focus

- Two simultaneous approvals of the same receipt must return one transaction and debit savings once; Task 3 adds a barrier-based concurrency test.
- A client-supplied valid-looking but different 64-character digest must return `RECEIPT_DIGEST_MISMATCH` without changing receipt authorization or balances; Task 3 tests it.
- Approval at exactly `expires_at` must be rejected as `RECEIPT_EXPIRED`, while replay of an earlier execution at that time must return the stored transaction; Task 3 tests both boundaries.
- Funds can disappear after review but before confirmation, so approval must reread the balance and return `INSUFFICIENT_FUNDS` without an authorization or partial ledger write; Task 3 tests changed state.
- Replaying the original action request before and after approval must reuse the stored receipts/result without a second Qwen call or second debit; Tasks 4 and 5 test both states.

---

## File Structure

```text
apps/dimon_live/
  receipts.py                    Canonical receipt payloads, digests, clocks, dataclasses
  database.py                    Receipt tables, persistence, authorization, atomic execution
  service.py                     Intent policy, receipt creation, approval outcome mapping
  app.py                         Strict approval request model and receipt approval route
  static/app.js                  Receipt rendering and direct approval request
  static/app.css                 Review-card and disabled/busy confirmation states
  README.md                      Updated receipt-backed demo sequence and trust boundary
  tests/
    test_receipts.py             Pure canonicalization and digest tests
    test_database.py             Persistence, expiry, replay, concurrency, rollback tests
    test_service.py              One-shot model and receipt-policy orchestration tests
    test_api.py                  HTTP validation and no-Qwen approval tests
docs/superpowers/specs/
  2026-09-23-receipt-bound-confirmation-design.md
```

`index.html` already contains the reusable `#candidate-list` region, so this
change does not require new static markup. The JavaScript will replace the
current natural-language resubmission buttons with receipt review cards.

### Task 1: Canonical Receipt Contract

**Files:**
- Create: `apps/dimon_live/receipts.py`
- Create: `apps/dimon_live/tests/test_receipts.py`

**Interfaces:**
- Produces: `DEMO_USER_ID: str = "demo_user_01"`
- Produces: `RECEIPT_CONTRACT_VERSION: int = 1`
- Produces: `RECEIPT_TTL: timedelta = timedelta(minutes=5)`
- Produces: immutable `ReceiptDraft` with the resolved transfer facts and audit input
- Produces: `build_receipt_record(draft: ReceiptDraft, *, now: datetime, receipt_id: str, nonce: str) -> dict[str, Any]`
- Produces: `canonical_json(payload: Mapping[str, Any]) -> str`
- Produces: `digest_canonical(canonical: str) -> str`
- Consumed later by: `Database.create_pending_receipts()` and `Database.execute_policy_transfer()`

- [ ] **Step 1: Write the failing canonicalization tests**

Create `test_receipts.py` with a fixed UTC clock and literal expected payload:

```python
import unittest
from datetime import datetime, timezone

from apps.dimon_live.receipts import ReceiptDraft, build_receipt_record


NOW = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)


class ReceiptContractTests(unittest.TestCase):
    def draft(self):
        return ReceiptDraft(
            request_id="00000000-0000-4000-8000-000000000030",
            original_text="I owe Alicia Powell five dollars",
            user_id="demo_user_01",
            source_account_id="acct_savings_01",
            beneficiary_id="beneficiary_alicia_powell_01",
            recipient_display_name="Alicia Powell",
            amount_minor=500,
            currency="SGD",
        )

    def test_build_receipt_record_is_deterministic_and_expires_in_five_minutes(self):
        first = build_receipt_record(self.draft(), now=NOW, receipt_id="receipt_fixed", nonce="nonce_fixed")
        second = build_receipt_record(self.draft(), now=NOW, receipt_id="receipt_fixed", nonce="nonce_fixed")
        self.assertEqual(first, second)
        self.assertEqual(first["expires_at"], "2026-09-23T03:05:00+00:00")
        self.assertEqual(len(first["receipt_digest"]), 64)
        self.assertEqual(first["receipt_digest"], first["receipt_digest"].lower())

    def test_canonical_payload_contains_only_bound_execution_facts(self):
        record = build_receipt_record(self.draft(), now=NOW, receipt_id="receipt_fixed", nonce="nonce_fixed")
        self.assertEqual(list(record["canonical_payload"]), sorted(record["canonical_payload"]))
        self.assertEqual(record["canonical_payload"]["contract_version"], 1)
        self.assertEqual(record["canonical_payload"]["amount_minor"], 500)
        self.assertNotIn("raw_model_output", record["canonical_payload"])
        self.assertNotIn("status", record["canonical_payload"])
```

- [ ] **Step 2: Run the receipt tests and verify RED**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_receipts -v`
Expected: FAIL because `apps.dimon_live.receipts` does not exist.

- [ ] **Step 3: Implement the immutable receipt primitives**

Implement the dataclass and canonical functions with these exact rules:

```python
@dataclass(frozen=True)
class ReceiptDraft:
    request_id: str
    original_text: str
    user_id: str
    source_account_id: str
    beneficiary_id: str
    recipient_display_name: str
    amount_minor: int
    currency: str


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_canonical(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

`build_receipt_record` must reject a naive `now`, non-positive or boolean
`amount_minor`, and currencies other than `SGD`. It builds a sorted payload
containing exactly `contract_version`, `receipt_id`, `request_id`, `user_id`,
`source_account_id`, `beneficiary_id`, `recipient_display_name`,
`amount_minor`, `currency`, `expires_at`, `nonce`, and `original_text`; it then
returns that payload plus `canonical_json`, `receipt_digest`, `status` set to
`AWAITING_CONFIRMATION`, and `created_at`.

- [ ] **Step 4: Add invalid-input tests and verify GREEN**

Add this table-driven method to `ReceiptContractTests`:

```python
def test_invalid_receipt_inputs_are_rejected(self):
    cases = (
        (dataclasses.replace(self.draft(), amount_minor=True), NOW),
        (dataclasses.replace(self.draft(), amount_minor=0), NOW),
        (dataclasses.replace(self.draft(), currency="USD"), NOW),
        (self.draft(), NOW.replace(tzinfo=None)),
    )
    for invalid_draft, invalid_now in cases:
        with self.subTest(draft=invalid_draft, now=invalid_now):
            with self.assertRaises(ValueError):
                build_receipt_record(
                    invalid_draft,
                    now=invalid_now,
                    receipt_id="receipt_fixed",
                    nonce="nonce_fixed",
                )
```

Import `dataclasses`, then run:

Run: `python3 -B -m unittest apps.dimon_live.tests.test_receipts -v`
Expected: all receipt tests PASS.

### Task 2: Pending Receipt Persistence and Idempotency

**Files:**
- Modify: `apps/dimon_live/database.py:27-97,194-208,252-517`
- Modify: `apps/dimon_live/tests/test_database.py`

**Interfaces:**
- Consumes: `ReceiptDraft`, `build_receipt_record`, `DEMO_USER_ID`
- Produces: `Database.create_pending_receipts(drafts: tuple[ReceiptDraft, ...], *, raw_model_output: str, validated_intent: Mapping[str, Any], reason_code: str, response_base: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]`
- Produces: `Database.get_receipt(receipt_id: str) -> dict[str, Any] | None`
- Preserves: `Database.get_attempt()`, `record_attempt()`, resolution methods, and existing ledger reads

- [ ] **Step 1: Write failing schema and pending-receipt tests**

Extend `test_database.py` with a helper that creates one Alicia draft, then add:

```python
from datetime import datetime, timedelta, timezone
from apps.dimon_live.receipts import ReceiptDraft

FIXED_NOW = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)

def receipt_draft(self, *, beneficiary_id="beneficiary_alicia_powell_01", recipient="Alicia Powell"):
    return ReceiptDraft(
        request_id="00000000-0000-4000-8000-000000000040",
        original_text="I owe Alicia Powell five dollars",
        user_id="demo_user_01",
        source_account_id="acct_savings_01",
        beneficiary_id=beneficiary_id,
        recipient_display_name=recipient,
        amount_minor=500,
        currency="SGD",
    )

def john_candidate_drafts(self):
    request_id = "00000000-0000-4000-8000-000000000041"
    original_text = "Send John five dollars"
    return (
        dataclasses.replace(self.receipt_draft(beneficiary_id="beneficiary_john_smith_01", recipient="John Smith"), request_id=request_id, original_text=original_text),
        dataclasses.replace(self.receipt_draft(beneficiary_id="beneficiary_john_tan_01", recipient="John Tan"), request_id=request_id, original_text=original_text),
    )

def create_pending_alicia(self):
    return self.database.create_pending_receipts(
        (self.receipt_draft(),), raw_model_output="{}", validated_intent={},
        reason_code="ACTION_CONFIRMATION_REQUIRED",
        response_base={"outcome": "CLARIFICATION_REQUEST", "question": "Send S$5.00 to Alicia Powell from savings?", "trace": []},
        now=FIXED_NOW,
    )

def create_pending_johns(self):
    return self.database.create_pending_receipts(
        self.john_candidate_drafts(), raw_model_output="{}", validated_intent={},
        reason_code="AMBIGUOUS_RECIPIENT",
        response_base={"outcome": "CLARIFICATION_REQUEST", "question": "Which saved John?", "trace": []},
        now=FIXED_NOW,
    )

def count_rows(self, table):
    with sqlite3.connect(self.db_path) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

def scalar(self, sql):
    with sqlite3.connect(self.db_path) as connection:
        return connection.execute(sql).fetchone()[0]

def test_pending_receipt_is_stored_with_attempt_and_without_transaction(self):
    response = self.database.create_pending_receipts(
        (self.receipt_draft(),), raw_model_output="{}", validated_intent={},
        reason_code="ACTION_CONFIRMATION_REQUIRED",
        response_base={"outcome": "CLARIFICATION_REQUEST", "question": "Send S$5.00 to Alicia Powell from savings?", "trace": []},
        now=FIXED_NOW,
    )
    choice = response["receipt_choices"][0]
    stored = self.database.get_receipt(choice["receipt_id"])
    self.assertEqual(stored["beneficiary_id"], "beneficiary_alicia_powell_01")
    self.assertEqual(stored["amount_minor"], 500)
    self.assertEqual(stored["status"], "AWAITING_CONFIRMATION")
    self.assertEqual(choice["receipt_digest"], stored["receipt_digest"])
    self.assertEqual(self.database.get_state()["transactions"], [])

def test_ambiguous_candidates_share_group_but_have_distinct_receipts():
    response = self.database.create_pending_receipts(
        self.john_candidate_drafts(), raw_model_output="{}", validated_intent={},
        reason_code="AMBIGUOUS_RECIPIENT",
        response_base={"outcome": "CLARIFICATION_REQUEST", "question": "Which saved John?", "trace": []},
        now=FIXED_NOW,
    )
    choices = response["receipt_choices"]
    self.assertEqual(len(choices), 2)
    self.assertNotEqual(choices[0]["receipt_id"], choices[1]["receipt_id"])
    groups = {self.database.get_receipt(choice["receipt_id"])["clarification_group_id"] for choice in choices}
    self.assertEqual(len(groups), 1)

def test_pending_action_retry_returns_same_receipts():
    first = self.create_pending_alicia()
    second = self.create_pending_alicia()
    self.assertEqual(second, first)
    self.assertEqual(self.count_rows("review_receipts"), 1)
```

Add these helpers as methods on the existing `DatabaseSeedTests` class; the
table names passed to `count_rows` are fixed test literals, never user input.

Add the idempotency-conflict and reset assertions explicitly:

```python
def test_pending_request_id_with_changed_text_conflicts(self):
    self.create_pending_alicia()
    changed = dataclasses.replace(
        self.receipt_draft(), original_text="Send Alicia Powell fifty dollars"
    )
    with self.assertRaisesRegex(IdempotencyConflict, "IDEMPOTENCY_CONFLICT"):
        self.database.create_pending_receipts(
            (changed,), raw_model_output="{}", validated_intent={},
            reason_code="ACTION_CONFIRMATION_REQUIRED",
            response_base={"outcome": "CLARIFICATION_REQUEST", "trace": []},
            now=FIXED_NOW,
        )

def test_reset_clears_all_receipt_records(self):
    self.create_pending_alicia()
    self.database.reset()
    for table in ("receipt_executions", "authorization_records", "review_receipts"):
        with self.subTest(table=table):
            self.assertEqual(self.count_rows(table), 0)
```

- [ ] **Step 2: Run the database tests and verify RED**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_database -v`
Expected: FAIL because the receipt tables and methods do not exist.

- [ ] **Step 3: Add the additive SQLite schema**

Append these tables to `SCHEMA` after `ledger_entries`:

```sql
CREATE TABLE IF NOT EXISTS review_receipts (
  receipt_id TEXT PRIMARY KEY,
  clarification_group_id TEXT,
  request_id TEXT NOT NULL REFERENCES action_attempts(request_id),
  user_id TEXT NOT NULL,
  source_account_id TEXT NOT NULL REFERENCES accounts(account_id),
  beneficiary_id TEXT NOT NULL REFERENCES beneficiaries(beneficiary_id),
  recipient_display_name TEXT NOT NULL,
  amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
  currency TEXT NOT NULL CHECK (currency = 'SGD'),
  original_text TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  nonce TEXT NOT NULL UNIQUE,
  canonical_json TEXT NOT NULL,
  receipt_digest TEXT NOT NULL CHECK (
    length(receipt_digest) = 64 AND receipt_digest NOT GLOB '*[^0-9a-f]*'
  ),
  status TEXT NOT NULL CHECK (status IN ('AWAITING_CONFIRMATION','EXECUTED','EXPIRED','SUPERSEDED')),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authorization_records (
  authorization_id TEXT PRIMARY KEY,
  receipt_id TEXT NOT NULL UNIQUE REFERENCES review_receipts(receipt_id),
  receipt_digest TEXT NOT NULL,
  user_id TEXT NOT NULL,
  method TEXT NOT NULL CHECK (method IN ('EXPLICIT_COMMAND_POLICY','USER_CONFIRMATION')),
  authorized_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipt_executions (
  receipt_id TEXT PRIMARY KEY REFERENCES review_receipts(receipt_id),
  transaction_id TEXT NOT NULL UNIQUE REFERENCES ledger_transactions(transaction_id),
  executed_at TEXT NOT NULL
);
```

- [ ] **Step 4: Implement atomic pending creation and reset ordering**

`create_pending_receipts` must require one or more drafts with the same request
ID, original text, user, source, amount, and currency. Under one
`BEGIN IMMEDIATE`, it checks `action_attempts`, generates one group ID only
when there is more than one choice, builds all receipt records, builds
display-safe `receipt_choices`, inserts the action attempt first and receipts
second, and commits. Each choice exposes only:

```python
{
    "receipt_id": record["receipt_id"],
    "receipt_digest": record["receipt_digest"],
    "source_label": source["label"],
    "recipient_display_name": record["recipient_display_name"],
    "amount_minor": record["amount_minor"],
    "currency": record["currency"],
    "expires_at": record["expires_at"],
}
```

On an identical request-ID replay, return `action_attempts.response_json`
without generating new IDs. Reset deletion order is `ledger_entries`,
`receipt_executions`, `ledger_transactions`, `authorization_records`,
`review_receipts`, `action_attempts`, `beneficiaries`, `accounts`.

- [ ] **Step 5: Run receipt and database suites and verify GREEN**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_receipts apps.dimon_live.tests.test_database -v`
Expected: all tests PASS, including the existing transfer and reset tests.

### Task 3: Atomic Receipt Authorization and Execution

**Files:**
- Modify: `apps/dimon_live/database.py`
- Modify: `apps/dimon_live/tests/test_database.py`

**Interfaces:**
- Consumes: `TransferCommand` for exact-policy creation and stored receipt rows for confirmation
- Produces: `ReceiptRejected(reason_code: str)` with a public `reason_code` attribute
- Produces: `Database.execute_policy_transfer(command: TransferCommand, *, user_id: str = DEMO_USER_ID, now: datetime | None = None) -> dict[str, Any]`
- Produces: `Database.approve_receipt(receipt_id: str, receipt_digest: str, user_id: str, *, now: datetime | None = None) -> dict[str, Any]`
- Produces internal: `Database._execute_stored_receipt(connection, receipt, *, method: str, now: datetime) -> dict[str, Any]`

Define the rejection type before `Database` so service code can map stable
reason codes without parsing exception text:

```python
class ReceiptRejected(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code
```

- [ ] **Step 1: Write failing exact-policy and confirmation tests**

Add tests proving both authorization paths use the same records:

```python
def test_exact_policy_transfer_creates_receipt_authorization_and_execution(self):
    result = self.database.execute_policy_transfer(self.command(amount_minor=500), now=FIXED_NOW)
    receipt = self.database.get_receipt(result["receipt"]["receipt_id"])
    self.assertEqual(receipt["status"], "EXECUTED")
    self.assertEqual(self.scalar("SELECT method FROM authorization_records"), "EXPLICIT_COMMAND_POLICY")
    self.assertEqual(self.scalar("SELECT COUNT(*) FROM receipt_executions"), 1)
    self.assertEqual([row["delta_minor"] for row in self.database.get_state()["ledger_entries"]], [-500, 500])

def test_approve_pending_receipt_executes_stored_facts(self):
    pending = self.create_pending_alicia()
    choice = pending["receipt_choices"][0]
    result = self.database.approve_receipt(choice["receipt_id"], choice["receipt_digest"], "demo_user_01", now=FIXED_NOW)
    self.assertEqual(result["resolved_action"]["beneficiary_id"], "beneficiary_alicia_powell_01")
    self.assertEqual(result["amount_minor"], 500)
    self.assertEqual(self.scalar("SELECT method FROM authorization_records"), "USER_CONFIRMATION")
```

- [ ] **Step 2: Write failing tamper, expiry, replay, group, and rollback tests**

Add literal assertions for:

```python
def approve(self, choice, *, now=FIXED_NOW, user_id="demo_user_01"):
    return self.database.approve_receipt(
        choice["receipt_id"], choice["receipt_digest"], user_id, now=now
    )

def test_changed_digest_is_rejected_without_writes(self):
    choice = self.create_pending_alicia()["receipt_choices"][0]
    before = self.database.get_state()
    with self.assertRaisesRegex(ReceiptRejected, "RECEIPT_DIGEST_MISMATCH"):
        self.database.approve_receipt(choice["receipt_id"], "0" * 64, "demo_user_01", now=FIXED_NOW)
    self.assertEqual(self.database.get_state(), before)
    self.assertEqual(self.count_rows("authorization_records"), 0)

def test_identical_repeated_approval_returns_one_transaction(self):
    choice = self.create_pending_alicia()["receipt_choices"][0]
    first = self.approve(choice)
    second = self.approve(choice)
    self.assertEqual(second["transaction_id"], first["transaction_id"])
    self.assertTrue(second["idempotent_replay"])
    self.assertEqual(self.count_rows("ledger_transactions"), 1)

def test_user_approval_of_policy_executed_receipt_is_incompatible_retry(self):
    executed = self.database.execute_policy_transfer(self.command(amount_minor=500), now=FIXED_NOW)
    receipt = executed["receipt"]
    with self.assertRaisesRegex(ReceiptRejected, "RECEIPT_ALREADY_EXECUTED"):
        self.database.approve_receipt(
            receipt["receipt_id"], receipt["receipt_digest"], "demo_user_01", now=FIXED_NOW
        )
    self.assertEqual(self.count_rows("ledger_transactions"), 1)

def test_approval_at_expiry_marks_receipt_expired_without_ledger_mutation(self):
    choice = self.create_pending_alicia()["receipt_choices"][0]
    with self.assertRaisesRegex(ReceiptRejected, "RECEIPT_EXPIRED"):
        self.approve(choice, now=FIXED_NOW + timedelta(minutes=5))
    self.assertEqual(self.database.get_receipt(choice["receipt_id"])["status"], "EXPIRED")
    self.assertEqual(self.count_rows("ledger_transactions"), 0)

def test_selecting_one_ambiguous_candidate_supersedes_sibling(self):
    choices = self.create_pending_johns()["receipt_choices"]
    self.approve(choices[1])
    self.assertEqual(self.database.get_receipt(choices[0]["receipt_id"])["status"], "SUPERSEDED")
    with self.assertRaisesRegex(ReceiptRejected, "RECEIPT_SUPERSEDED"):
        self.approve(choices[0])
```

Add these failure, consistency, and concurrency tests:

```python
def test_wrong_user_and_missing_receipt_are_non_mutating(self):
    choice = self.create_pending_alicia()["receipt_choices"][0]
    with self.assertRaisesRegex(ReceiptRejected, "RECEIPT_USER_MISMATCH"):
        self.approve(choice, user_id="other_user")
    with self.assertRaisesRegex(ReceiptRejected, "RECEIPT_NOT_FOUND"):
        self.database.approve_receipt("receipt_missing", "0" * 64, "demo_user_01", now=FIXED_NOW)
    self.assertEqual(self.count_rows("ledger_transactions"), 0)
    self.assertEqual(self.count_rows("authorization_records"), 0)

def test_canonical_json_or_bound_column_tampering_is_rejected(self):
    for assignment in ("canonical_json = '{}'", "amount_minor = 501"):
        with self.subTest(assignment=assignment):
            self.database.reset()
            choice = self.create_pending_alicia()["receipt_choices"][0]
            with sqlite3.connect(self.db_path) as connection:
                connection.execute(
                    f"UPDATE review_receipts SET {assignment} WHERE receipt_id = ?",
                    (choice["receipt_id"],),
                )
            with self.assertRaisesRegex(ReceiptRejected, "RECEIPT_DIGEST_MISMATCH"):
                self.approve(choice)
            self.assertEqual(self.count_rows("ledger_transactions"), 0)

def test_balance_is_rechecked_at_approval_time(self):
    choice = self.create_pending_alicia()["receipt_choices"][0]
    with sqlite3.connect(self.db_path) as connection:
        connection.execute("UPDATE accounts SET balance_minor = 0 WHERE account_id = 'acct_savings_01'")
    with self.assertRaisesRegex(InsufficientFunds, "INSUFFICIENT_FUNDS"):
        self.approve(choice)
    self.assertEqual(self.count_rows("authorization_records"), 0)
    self.assertEqual(self.database.get_receipt(choice["receipt_id"])["status"], "AWAITING_CONFIRMATION")

def test_entry_failure_rolls_back_authorization_balance_and_receipt(self):
    choice = self.create_pending_alicia()["receipt_choices"][0]
    before = self.database.get_state()["accounts"]
    with sqlite3.connect(self.db_path) as connection:
        connection.executescript("""
          CREATE TRIGGER force_receipt_entry_failure BEFORE INSERT ON ledger_entries
          BEGIN SELECT RAISE(ABORT, 'forced receipt entry failure'); END;
        """)
    with self.assertRaisesRegex(sqlite3.IntegrityError, "forced receipt entry failure"):
        self.approve(choice)
    self.assertEqual(self.database.get_state()["accounts"], before)
    self.assertEqual(self.count_rows("authorization_records"), 0)
    self.assertEqual(self.count_rows("receipt_executions"), 0)
    self.assertEqual(self.database.get_receipt(choice["receipt_id"])["status"], "AWAITING_CONFIRMATION")

def test_executed_receipt_replays_after_expiry(self):
    choice = self.create_pending_alicia()["receipt_choices"][0]
    first = self.approve(choice)
    replay = self.approve(choice, now=FIXED_NOW + timedelta(minutes=10))
    self.assertEqual(replay["transaction_id"], first["transaction_id"])
    self.assertTrue(replay["idempotent_replay"])

def test_invalid_executed_state_without_link_is_rejected(self):
    choice = self.create_pending_alicia()["receipt_choices"][0]
    with sqlite3.connect(self.db_path) as connection:
        connection.execute("UPDATE review_receipts SET status = 'EXECUTED' WHERE receipt_id = ?", (choice["receipt_id"],))
    with self.assertRaisesRegex(ReceiptRejected, "RECEIPT_INVALID_STATUS"):
        self.approve(choice)
    self.assertEqual(self.count_rows("ledger_transactions"), 0)

def test_two_threads_approving_one_receipt_create_one_transaction(self):
    choice = self.create_pending_alicia()["receipt_choices"][0]
    barrier = Barrier(2)
    def execute():
        barrier.wait()
        return self.approve(choice)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(execute), pool.submit(execute))]
    self.assertEqual(results[0]["transaction_id"], results[1]["transaction_id"])
    self.assertEqual(self.count_rows("ledger_transactions"), 1)
    self.assertEqual(self.database.get_state()["accounts"][0]["balance_minor"], 99500)
```

Import `ReceiptRejected`, `Barrier`, `ThreadPoolExecutor`, and `timedelta` in
the test module. Table/assignment strings above are fixed test literals.

- [ ] **Step 3: Run the new database tests and verify RED**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_database -v`
Expected: FAIL because policy execution and receipt approval are absent.

- [ ] **Step 4: Implement the single transactional execution engine**

Both public methods open one connection and call `_execute_stored_receipt`.
Within the existing `BEGIN IMMEDIATE` transaction, the internal method must:

1. Check for an existing `receipt_executions` and authorization row first.
   Matching user, digest, and authorization method return the stored result
   with `idempotent_replay=True`; a method mismatch returns
   `RECEIPT_ALREADY_EXECUTED` without a write.
2. Compare user, supplied digest, stored digest, and a freshly recomputed digest
   from `canonical_json`. Parse the canonical payload and compare every bound
   field to its receipt column before using those facts: contract version,
   receipt ID, request ID, user ID, source account ID, beneficiary ID, display
   name, amount, currency, expiry, nonce, and original text. Any difference is
   `RECEIPT_DIGEST_MISMATCH`.
3. Reject `SUPERSEDED`, `EXPIRED`, and non-`AWAITING_CONFIRMATION` states with
   their stable receipt codes.
4. Treat `now >= expires_at` as expired, persist `EXPIRED`, commit only that
   lifecycle update, then raise `ReceiptRejected("RECEIPT_EXPIRED")`.
5. Reread source account and beneficiary by the stored IDs and verify SGD,
   positive amount, current balance, and destination account.
6. Insert the authorization, debit and credit accounts, insert one ledger
   transaction and two entries, verify the entry sum is zero, insert the
   receipt-execution link, update the selected receipt to `EXECUTED`, and update
   other `AWAITING_CONFIRMATION` rows in its group to `SUPERSEDED`.
7. Update the original `action_attempts` row to the executed outcome and full
   response, then commit. Every other exception rolls the whole transaction
   back.

`execute_policy_transfer` inserts an initially awaiting receipt and its action
attempt inside the same transaction before invoking this engine with
`EXPLICIT_COMMAND_POLICY`; no pending state becomes externally visible.
Replace the old `execute_transfer` method and update its existing tests to call
`execute_policy_transfer`, so there is no receipt-bypassing execution path.

- [ ] **Step 5: Run all receipt/database tests and verify GREEN**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_receipts apps.dimon_live.tests.test_database -v`
Expected: all tests PASS, including concurrent approval and forced rollback.

### Task 4: One-Shot Service Orchestration

**Files:**
- Modify: `apps/dimon_live/service.py:1-341`
- Modify: `apps/dimon_live/tests/test_service.py`

**Interfaces:**
- Consumes: `Database.create_pending_receipts()`, `execute_policy_transfer()`, and `approve_receipt()`
- Produces: `ActionService.process(request_id: str, text: str) -> dict[str, Any]` with `receipt_choices` for confirmation
- Produces: `ActionService.approve(receipt_id: str, receipt_digest: str, *, user_id: str = DEMO_USER_ID) -> dict[str, Any]`
- Removes from responses: `confirmation_command`

- [ ] **Step 1: Replace the second-interpretation test with receipt tests**

Delete `test_generated_confirmation_command_executes_on_new_request` and add:

```python
VAGUE_ALICIA_INTENT = {
    **GOLDEN_INTENT,
    "source_account_ref": None,
    "recipient_label": "Alicia Powell",
    "amount_minor": 500,
}

class RecordingExtractor:
    def __init__(self, intent):
        self.intent = intent
        self.calls = []

    def extract(self, text):
        self.calls.append(text)
        return ExtractedIntent(dict(self.intent), json.dumps(self.intent), 12.5)

def test_vague_request_returns_receipt_not_generated_command(self):
    extractor = RecordingExtractor(VAGUE_ALICIA_INTENT)
    service = ActionService(self.database, extractor)
    result = service.process(REQUEST_ID, "I owe Alicia Powell 5 dollars, help me resolve it.")
    self.assertEqual(len(extractor.calls), 1)
    self.assertNotIn("confirmation_command", result)
    self.assertEqual(result["receipt_choices"][0]["recipient_display_name"], "Alicia Powell")
    self.assertEqual(self.database.get_state()["transactions"], [])

def test_approval_executes_receipt_without_calling_extractor_again(self):
    extractor = RecordingExtractor(VAGUE_ALICIA_INTENT)
    service = ActionService(self.database, extractor)
    pending = service.process(REQUEST_ID, "I owe Alicia Powell 5 dollars, help me resolve it.")
    choice = pending["receipt_choices"][0]
    executed = service.approve(choice["receipt_id"], choice["receipt_digest"])
    self.assertEqual(extractor.calls, ["I owe Alicia Powell 5 dollars, help me resolve it."])
    self.assertEqual(executed["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
    self.assertEqual(executed["trace"][0]["stage"], "RECEIPT_APPROVAL_RECEIVED")
    self.assertNotIn("MODEL_RESPONSE_RECEIVED", [item["stage"] for item in executed["trace"]])
```

Update close-match and ambiguous-recipient tests to assert one choice per
resolved saved beneficiary and exact amounts/source labels in every choice.

- [ ] **Step 2: Add service error and replay tests**

Add these service-boundary tests; database tests already cover the underlying
write invariants:

```python
def test_receipt_rejections_map_to_blocked_with_current_state(self):
    service = ActionService(self.database, RecordingExtractor(VAGUE_ALICIA_INTENT))
    pending = service.process(REQUEST_ID, "I owe Alicia Powell 5 dollars, help me resolve it.")
    choice = pending["receipt_choices"][0]
    cases = (
        ("receipt_missing", "0" * 64, "demo_user_01", "RECEIPT_NOT_FOUND"),
        (choice["receipt_id"], "0" * 64, "demo_user_01", "RECEIPT_DIGEST_MISMATCH"),
        (choice["receipt_id"], choice["receipt_digest"], "other_user", "RECEIPT_USER_MISMATCH"),
    )
    for receipt_id, digest, user_id, expected in cases:
        with self.subTest(reason=expected):
            result = service.approve(receipt_id, digest, user_id=user_id)
            self.assertEqual((result["outcome"], result["reason_code"]), ("BLOCKED", expected))
            self.assertIn("accounts", result["state"])

def test_action_replay_before_and_after_approval_never_recalls_model(self):
    text = "I owe Alicia Powell 5 dollars, help me resolve it."
    extractor = RecordingExtractor(VAGUE_ALICIA_INTENT)
    service = ActionService(self.database, extractor)
    pending = service.process(REQUEST_ID, text)
    pending_replay = service.process(REQUEST_ID, text)
    self.assertEqual(pending_replay["receipt_choices"], pending["receipt_choices"])
    choice = pending["receipt_choices"][0]
    executed = service.approve(choice["receipt_id"], choice["receipt_digest"])
    executed_replay = service.process(REQUEST_ID, text)
    self.assertEqual(executed_replay["transaction_id"], executed["transaction_id"])
    self.assertEqual(extractor.calls, [text])
    self.assertEqual(len(self.database.get_state()["transactions"]), 1)
```

Add the two lifecycle mappings explicitly:

```python
def test_superseded_and_expired_receipts_map_to_stable_codes(self):
    john_intent = {**GOLDEN_INTENT, "recipient_label": "John", "amount_minor": 500}
    john_service = ActionService(self.database, RecordingExtractor(john_intent))
    pending = john_service.process(REQUEST_ID, "Send John five dollars")
    selected, sibling = pending["receipt_choices"]
    john_service.approve(selected["receipt_id"], selected["receipt_digest"])
    superseded = john_service.approve(sibling["receipt_id"], sibling["receipt_digest"])
    self.assertEqual(superseded["reason_code"], "RECEIPT_SUPERSEDED")
    self.database.reset()

    vague_service = ActionService(self.database, RecordingExtractor(VAGUE_ALICIA_INTENT))
    pending = vague_service.process(REQUEST_ID, "I owe Alicia Powell 5 dollars, help me resolve it.")
    choice = pending["receipt_choices"][0]
    with sqlite3.connect(self.database.path) as connection:
        connection.execute("UPDATE review_receipts SET status = 'EXPIRED' WHERE receipt_id = ?", (choice["receipt_id"],))
    expired = vague_service.approve(choice["receipt_id"], choice["receipt_digest"])
    self.assertEqual(expired["reason_code"], "RECEIPT_EXPIRED")
    self.assertEqual(len(self.database.get_state()["transactions"]), 0)
```

- [ ] **Step 3: Run service tests and verify RED**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_service -v`
Expected: FAIL because clarification still produces natural-language commands.

- [ ] **Step 4: Route every resolvable action through receipts**

After source and recipient resolution:

- Before creating any exact, close-match, or ambiguous receipt, compare the
  proposed amount to the resolved source balance. Insufficient funds retain the
  existing blocked outcome and create zero receipt rows.
- For `close` and `candidates`, create one `ReceiptDraft` per match and call
  `create_pending_receipts` with the existing question and reason code.
- For one exact beneficiary without `send`, `pay`, or `transfer`, create one
  pending receipt with `ACTION_CONFIRMATION_REQUIRED`.
- For an exact explicit action, call `execute_policy_transfer`; append
  `ELIGIBILITY_PASSED` and `SQLITE_COMMITTED` as today.
- Preserve unknown, negated, unsupported, malformed-model, unavailable-model,
  and idempotency outcomes without executable receipts.

Implement `approve` as a model-free wrapper around `Database.approve_receipt`.
Success trace is exactly `RECEIPT_APPROVAL_RECEIVED`, `RECEIPT_REVALIDATED`,
`SQLITE_COMMITTED`. `ReceiptRejected` maps to `BLOCKED`; SQLite failures map to
`FAILED/DATABASE_ERROR`. Both return current `state`.

- [ ] **Step 5: Run database and service suites and verify GREEN**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_receipts apps.dimon_live.tests.test_database apps.dimon_live.tests.test_service -v`
Expected: all tests PASS and no service test confirms by submitting rewritten text.

### Task 5: Strict Approval HTTP Contract

**Files:**
- Modify: `apps/dimon_live/app.py:1-103`
- Modify: `apps/dimon_live/tests/test_api.py`

**Interfaces:**
- Consumes: `ActionService.approve()`
- Produces: `POST /api/receipts/{receipt_id}/approve`
- Produces strict body: `{"receipt_digest": "<64 lowercase hex characters>"}`

- [ ] **Step 1: Write failing API contract tests**

Add an extractor whose response can be changed to the vague Alicia intent, then
test:

```python
class RecordingExtractor:
    def __init__(self):
        self.calls = []
        self.intent = dict(GOLDEN_INTENT)

    def extract(self, text):
        self.calls.append(text)
        return ExtractedIntent(dict(self.intent), json.dumps(self.intent), 12.5)

def post_vague_alicia(self):
    self.extractor.intent = {
        **GOLDEN_INTENT,
        "source_account_ref": None,
        "recipient_label": "Alicia Powell",
        "amount_minor": 500,
    }
    return self.request(
        "POST", "/api/actions",
        json={
            "request_id": "00000000-0000-4000-8000-000000000050",
            "text": "I owe Alicia Powell five dollars, help me resolve it.",
        },
    )

def test_approval_endpoint_executes_receipt_without_second_model_call(self):
    pending = self.post_vague_alicia().json()
    choice = pending["receipt_choices"][0]
    response = self.request("POST", f"/api/receipts/{choice['receipt_id']}/approve", json={"receipt_digest": choice["receipt_digest"]})
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
    self.assertEqual(len(self.extractor.calls), 1)

def test_approval_rejects_client_supplied_transfer_fields(self):
    choice = self.post_vague_alicia().json()["receipt_choices"][0]
    response = self.request("POST", f"/api/receipts/{choice['receipt_id']}/approve", json={"receipt_digest": choice["receipt_digest"], "amount_minor": 5000})
    self.assertEqual(response.status_code, 422)
    self.assertEqual(len(self.app.state.database.get_state()["transactions"]), 0)
```

Add strict-shape and semantic-tamper tests:

```python
def test_approval_digest_shape_is_strict(self):
    choice = self.post_vague_alicia().json()["receipt_choices"][0]
    bodies = ({}, {"receipt_digest": "abc"}, {"receipt_digest": choice["receipt_digest"].upper()})
    for body in bodies:
        with self.subTest(body=body):
            response = self.request("POST", f"/api/receipts/{choice['receipt_id']}/approve", json=body)
            self.assertEqual(response.status_code, 422)
    self.assertEqual(len(self.app.state.database.get_state()["transactions"]), 0)

def test_valid_shape_wrong_digest_is_non_mutating_block(self):
    choice = self.post_vague_alicia().json()["receipt_choices"][0]
    response = self.request("POST", f"/api/receipts/{choice['receipt_id']}/approve", json={"receipt_digest": "0" * 64})
    self.assertEqual(response.status_code, 200)
    self.assertEqual((response.json()["outcome"], response.json()["reason_code"]), ("BLOCKED", "RECEIPT_DIGEST_MISMATCH"))
    self.assertEqual(len(response.json()["state"]["transactions"]), 0)
```

- [ ] **Step 2: Run API tests and verify RED**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_api -v`
Expected: FAIL with HTTP 404 for the approval route.

- [ ] **Step 3: Add the strict request model and route**

Use Pydantic v2 extra-field rejection:

```python
class ReceiptApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


@application.post("/api/receipts/{receipt_id}/approve")
async def approve_receipt(receipt_id: str, request: ReceiptApprovalRequest):
    return service.approve(receipt_id, request.receipt_digest)
```

Do not add amount, recipient, source, user, status, or method parameters. The
route uses the service's fixed server-side demo identity.

- [ ] **Step 4: Run API and backend suites and verify GREEN**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_receipts apps.dimon_live.tests.test_database apps.dimon_live.tests.test_service apps.dimon_live.tests.test_api -v`
Expected: all Dimon Live backend and API tests PASS.

### Task 6: Receipt Review UI, Documentation, and Full Verification

**Files:**
- Modify: `apps/dimon_live/static/app.js:65-240`
- Modify: `apps/dimon_live/static/app.css:91-140`
- Modify: `apps/dimon_live/tests/test_api.py`
- Modify: `apps/dimon_live/README.md`

**Interfaces:**
- Consumes: response `receipt_choices[]` and `POST /api/receipts/{id}/approve`
- Removes: `candidateCommand()` and confirmation through `ui.form.requestSubmit()`
- Produces: `approveReceipt(choice, button) -> Promise<void>`

- [ ] **Step 1: Write failing static behavior tests**

Replace the current generated-command assertion with checks that the shipped
script uses stored approval coordinates:

```python
def test_static_script_approves_receipt_without_resubmitting_text(self):
    script = self.request("GET", "/static/app.js").text
    self.assertIn("/api/receipts/${choice.receipt_id}/approve", script)
    self.assertIn("receipt_digest: choice.receipt_digest", script)
    self.assertNotIn("function candidateCommand", script)
    self.assertNotIn("ui.input.value = result.confirmation_command", script)

def test_static_script_disables_choices_during_approval(self):
    script = self.request("GET", "/static/app.js").text
    self.assertIn("setChoiceBusy(true)", script)
    self.assertIn("setChoiceBusy(false)", script)
```

- [ ] **Step 2: Run the static tests and verify RED**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_api.DimonLiveApiTests.test_static_script_approves_receipt_without_resubmitting_text apps.dimon_live.tests.test_api.DimonLiveApiTests.test_static_script_disables_choices_during_approval -v`
Expected: FAIL because the current UI rewrites and resubmits a command.

- [ ] **Step 3: Implement direct receipt approval in the browser**

Add stage copy for `RECEIPT_APPROVAL_RECEIVED`, `RECEIPT_REVALIDATED`,
`RECEIPT_DIGEST_MISMATCH`, `RECEIPT_EXPIRED`, `RECEIPT_SUPERSEDED`,
`RECEIPT_USER_MISMATCH`, and `RECEIPT_NOT_FOUND`.

Render each choice as a review card containing escaped source label, recipient,
formatted amount, currency, and expiry plus a button labelled
`Confirm exact receipt`. Store no facts in editable inputs. The click handler
must call:

```javascript
async function approveReceipt(choice, button) {
  const started = performance.now();
  setChoiceBusy(true);
  try {
    const response = await fetch(`/api/receipts/${choice.receipt_id}/approve`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({receipt_digest: choice.receipt_digest}),
    });
    if (!response.ok) throw new Error(`Gateway returned HTTP ${response.status}`);
    renderResult(await response.json(), Math.round(performance.now() - started));
  } finally {
    setChoiceBusy(false);
  }
}
```

`setChoiceBusy` disables every `.candidate-choice`; it must not disable the
text box after the response finishes. Update the clarification detail to say
that confirmation executes the displayed stored receipt without a second Qwen
interpretation. Add compact `.receipt-choice`, `.receipt-facts`, and disabled
button styles while retaining the current visual language.

- [ ] **Step 4: Update the README demo and trust-boundary explanation**

Change the close-match and vague examples to say that selecting the displayed
receipt directly calls the approval endpoint. Document the five-minute expiry,
exact-policy immediate path, one-time execution, and the fact that replay
returns the existing transaction. Remove any description suggesting that the
browser generates and submits a corrected sentence.

- [ ] **Step 5: Run the complete automated regression suite**

Run: `python3 -B -m unittest discover -s apps/dimon_live/tests -v`
Expected: all Dimon Live tests PASS.

Run: `python3 -B -m unittest discover -s experiments/local-structured-action/tests -v`
Expected: all structured-action harness tests PASS.

- [ ] **Step 6: Restart and smoke-test the localhost demo**

Stop only the existing Dimon Live Uvicorn process, then restart with:

```bash
python3 -m uvicorn apps.dimon_live.app:app --host 127.0.0.1 --port 8890
```

At `http://127.0.0.1:8890`, reset the demo and verify:

1. `Send Alicia Powell S$5 from savings.` settles immediately and displays a
   receipt-backed committed result.
2. `I owe Alicia Powell 5 dollars, help me resolve it.` displays one exact
   receipt; confirmation settles S$5 to Alicia without changing the input or
   showing a second Qwen stage.
3. `Send Jhon Tan S$2.50 from savings.` displays John Tan and settles only after
   direct receipt confirmation.
4. `Send John S$2.50 from savings.` displays separate John Smith and John Tan
   receipts; choosing one settles once and invalidates the other.
5. Double-clicking or retrying a confirmation leaves exactly one committed
   ledger row and one debit.

Inspect the network panel for the confirmation request and verify its body has
exactly one key, `receipt_digest`, and that no `/api/actions` request occurs on
confirmation.

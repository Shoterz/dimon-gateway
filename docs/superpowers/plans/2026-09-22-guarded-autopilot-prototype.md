# Guarded Autopilot Prototype Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a polished localhost console where free-form beneficiary-transfer requests are extracted by the existing local Qwen model and eligible requests atomically update a synthetic SQLite ledger.

**Architecture:** A single FastAPI process serves a framework-free single-page interface and owns all deterministic resolution, autopilot policy, and SQLite access. The browser submits text only; the existing llama.cpp/Qwen endpoint returns an untrusted six-field intent and never receives SQL or a ledger tool.

**Tech Stack:** Python 3.10, FastAPI 0.136.1, Uvicorn 0.46.0, Pydantic 2.13.4, Python `sqlite3`, HTML/CSS/JavaScript, existing llama.cpp OpenAI-compatible endpoint.

**Spec:** `docs/superpowers/specs/2026-09-22-guarded-autopilot-prototype-design.md`

## Global Constraints

- Synthetic local demo only; never connect to a real bank, broker, or payment API.
- Version one supports only `BENEFICIARY_TRANSFER`, immediate SGD transfers, and one demo user.
- Qwen may emit only the six fields already defined in `experiments/local-structured-action/transfer_draft.schema.json`.
- Qwen receives no SQL, Python, shell, approval, or arbitrary tool interface.
- Autopilot requires exact case-insensitive account and recipient/alias resolution; prefix, edit-distance, phonetic, and embedding matches never authorize mutation.
- The backend is the only SQLite owner, and every transfer is an atomic balanced debit and credit.
- Use installed dependencies only; do not add a package manager or download packages.
- Serve the static interface from FastAPI so one application command starts the prototype.
- Keep the existing experiment and its tests passing.
- This workspace is not a Git repository. Do not run `git init`; commit steps are intentionally omitted.

## Review Focus

- Case and surrounding whitespace may normalize, but an approximate recipient such as `John` must clarify and never execute; Task 2 tests this.
- Two simultaneous uses of one request ID must create one ledger transaction and one debit; Task 1 tests this with two threads.
- Empty, oversized, or non-UUID action requests must receive validation errors without calling Qwen; Task 3 tests this.
- A truncated, malformed, or timed-out model response must become a non-mutating failure; Task 2 tests each branch.
- Reset racing with an action must serialize through SQLite rather than leave half-seeded state; Task 1 tests reset after a held write transaction.

---

## File Structure

```text
apps/
  __init__.py                         Python package marker
  dimon_live/
    __init__.py                       Package marker
    app.py                            FastAPI composition and HTTP routes
    database.py                       SQLite schema, seed, reads, atomic transfer
    model_adapter.py                  Qwen request, parsing, and validation
    service.py                        Resolution, eligibility, and action outcomes
    static/
      index.html                      Accessible console structure
      app.css                         Visual system, layout, and animations
      app.js                          API calls and state rendering
    tests/
      __init__.py
      test_database.py                Ledger and idempotency behavior
      test_model_adapter.py           Structured-output boundary
      test_service.py                 Autopilot/clarification/blocking behavior
      test_api.py                     HTTP contracts and static delivery
    README.md                         Run, reset, and demo instructions
```

Runtime SQLite files live under `apps/dimon_live/data/` and are ignored by the
root `.gitignore`.

### Task 1: Atomic SQLite ledger

**Files:**
- Create: `apps/__init__.py`
- Create: `apps/dimon_live/__init__.py`
- Create: `apps/dimon_live/database.py`
- Create: `apps/dimon_live/tests/__init__.py`
- Create: `apps/dimon_live/tests/test_database.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `Database(path: Path)`
- Produces: `Database.initialize() -> None`
- Produces: `Database.reset() -> dict[str, Any]`
- Produces: `Database.get_state() -> dict[str, Any]`
- Produces: `Database.resolve_source(label: str) -> dict[str, Any] | None`
- Produces: `Database.resolve_beneficiary(label: str) -> Resolution`
- Produces: `Database.get_attempt(request_id: str) -> dict[str, Any] | None`
- Produces: `Database.record_attempt(attempt: ActionAttempt) -> dict[str, Any]`
- Produces: `Database.execute_transfer(command: TransferCommand) -> dict[str, Any]`
- Produces immutable dataclasses `Resolution`, `ActionAttempt`, and `TransferCommand`

- [ ] **Step 1: Write failing seed and reset tests**

Create a temporary database and assert literal seed balances:

```python
def test_initialize_and_reset_restore_seed_balances(self):
    database = Database(self.db_path)
    database.initialize()
    self.assertEqual(
        [(row["label"], row["balance_minor"]) for row in database.get_state()["accounts"]],
        [("savings", 100000), ("John Smith", 10000), ("John Tan", 7500)],
    )
    database.execute_transfer(self.command(amount_minor=5000))
    reset_state = database.reset()
    self.assertEqual(
        [(row["label"], row["balance_minor"]) for row in reset_state["accounts"]],
        [("savings", 100000), ("John Smith", 10000), ("John Tan", 7500)],
    )
    self.assertEqual(reset_state["transactions"], [])
```

- [ ] **Step 2: Run the test and verify RED**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_database -v`
Expected: import failure because `apps.dimon_live.database` does not exist.

- [ ] **Step 3: Implement the schema and seed transaction**

Use exact tables:

```sql
CREATE TABLE accounts (
  account_id TEXT PRIMARY KEY,
  owner_display_name TEXT NOT NULL,
  label TEXT NOT NULL,
  normalized_label TEXT NOT NULL UNIQUE,
  currency TEXT NOT NULL CHECK (currency = 'SGD'),
  balance_minor INTEGER NOT NULL CHECK (balance_minor >= 0),
  is_user_source INTEGER NOT NULL CHECK (is_user_source IN (0, 1))
);
CREATE TABLE beneficiaries (
  beneficiary_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  normalized_alias TEXT NOT NULL UNIQUE,
  destination_account_id TEXT NOT NULL REFERENCES accounts(account_id),
  masked_destination TEXT NOT NULL
);
CREATE TABLE action_attempts (
  request_id TEXT PRIMARY KEY,
  original_text TEXT NOT NULL,
  raw_model_output TEXT,
  validated_intent_json TEXT,
  outcome TEXT NOT NULL,
  reason_code TEXT,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE ledger_transactions (
  transaction_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE REFERENCES action_attempts(request_id),
  kind TEXT NOT NULL CHECK (kind = 'BENEFICIARY_TRANSFER'),
  amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
  currency TEXT NOT NULL CHECK (currency = 'SGD'),
  created_at TEXT NOT NULL
);
CREATE TABLE ledger_entries (
  entry_id TEXT PRIMARY KEY,
  transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
  account_id TEXT NOT NULL REFERENCES accounts(account_id),
  delta_minor INTEGER NOT NULL CHECK (delta_minor != 0)
);
```

Seed account IDs `acct_savings_01`, `acct_john_smith_01`, and
`acct_john_tan_01`; beneficiary IDs match the existing experiment IDs.

- [ ] **Step 4: Run the seed/reset test and verify GREEN**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_database -v`
Expected: the seed/reset test passes.

- [ ] **Step 5: Write failing transfer, rollback, resolution, and concurrency tests**

Add tests asserting:

```python
def test_transfer_creates_equal_debit_and_credit(self):
    result = self.database.execute_transfer(self.command(amount_minor=5000))
    state = self.database.get_state()
    self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
    self.assertEqual([row["balance_minor"] for row in state["accounts"][:2]], [95000, 15000])
    self.assertEqual(sum(row["delta_minor"] for row in state["ledger_entries"]), 0)

def test_insufficient_funds_leave_balances_unchanged(self):
    before = self.database.get_state()["accounts"]
    with self.assertRaisesRegex(InsufficientFunds, "INSUFFICIENT_FUNDS"):
        self.database.execute_transfer(self.command(amount_minor=100001))
    self.assertEqual(self.database.get_state()["accounts"], before)

def test_identical_retry_returns_recorded_result_without_second_debit(self):
    first = self.database.execute_transfer(self.command(amount_minor=5000))
    second = self.database.execute_transfer(self.command(amount_minor=5000))
    self.assertEqual(second, first)
    self.assertEqual(len(self.database.get_state()["transactions"]), 1)
    self.assertEqual(self.database.get_state()["accounts"][0]["balance_minor"], 95000)

def test_changed_text_with_same_request_id_is_an_idempotency_conflict(self):
    self.database.execute_transfer(self.command(amount_minor=5000))
    changed = dataclasses.replace(self.command(amount_minor=5000), original_text="Send S$60")
    with self.assertRaisesRegex(IdempotencyConflict, "IDEMPOTENCY_CONFLICT"):
        self.database.execute_transfer(changed)

def test_insert_failure_rolls_back_both_balance_updates(self):
    before = self.database.get_state()["accounts"]
    with sqlite3.connect(self.db_path) as connection:
        connection.executescript("""
          CREATE TRIGGER force_entry_failure BEFORE INSERT ON ledger_entries
          BEGIN SELECT RAISE(ABORT, 'forced entry failure'); END;
        """)
    with self.assertRaises(sqlite3.IntegrityError):
        self.database.execute_transfer(self.command(amount_minor=5000))
    self.assertEqual(self.database.get_state()["accounts"], before)

def test_two_threads_with_same_request_id_create_one_transaction(self):
    barrier = threading.Barrier(2)
    def execute():
        barrier.wait()
        return self.database.execute_transfer(self.command(amount_minor=5000))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(execute), pool.submit(execute))]
    self.assertEqual(results[0], results[1])
    state = self.database.get_state()
    self.assertEqual(len(state["transactions"]), 1)
    self.assertEqual(state["accounts"][0]["balance_minor"], 95000)

def test_exact_resolution_normalizes_case_and_whitespace(self):
    result = self.database.resolve_beneficiary("  JOHN SMITH ")
    self.assertEqual(result.kind, "exact")
    self.assertEqual(result.matches[0]["beneficiary_id"], "beneficiary_john_smith_01")

def test_prefix_recipient_is_never_returned_as_an_exact_match(self):
    result = self.database.resolve_beneficiary("John")
    self.assertEqual(result.kind, "candidates")
    self.assertEqual([row["display_name"] for row in result.matches], ["John Smith", "John Tan"])

def test_blocked_attempt_can_be_recorded_without_a_ledger_transaction(self):
    stored = self.database.record_attempt(self.blocked_attempt(reason="AMBIGUOUS_RECIPIENT"))
    self.assertEqual(stored["outcome"], "CLARIFICATION_REQUEST")
    self.assertEqual(self.database.get_state()["transactions"], [])

def test_reset_waits_for_a_held_write_transaction_then_restores_seed(self):
    blocker = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(self.database.reset)
        self.assertFalse(future.done())
        blocker.rollback()
        state = future.result(timeout=2)
    blocker.close()
    self.assertEqual(state["accounts"][0]["balance_minor"], 100000)
```

For rollback, install a temporary SQLite trigger that raises before a
`ledger_entries` insert and assert both balances retain their literal starting
values. For concurrency, release two threads from one barrier with the same
`TransferCommand` and assert one transaction, savings `95000`, and John Smith
`15000`.

- [ ] **Step 6: Implement atomic execution and resolution**

Normalize labels with `value.strip().casefold()`. Use `BEGIN IMMEDIATE`, check
`action_attempts` first, reread the balance inside the transaction, update both
accounts, insert one transaction and two entries, verify `SUM(delta_minor) = 0`,
store the complete response JSON, and commit. Roll back on every exception.

`resolve_beneficiary` returns one of:

```python
Resolution(kind="exact", matches=(beneficiary,))
Resolution(kind="candidates", matches=(john_smith, john_tan))
Resolution(kind="missing", matches=())
```

Only normalized exact name/alias equality produces `kind="exact"`; prefix
matches are candidates regardless of candidate count.

`record_attempt` stores clarification, blocked, and failed outcomes without a
ledger transaction. `get_attempt` returns the stored response JSON so a retry
does not call Qwen again. A request ID paired with different original text is
an idempotency conflict.

- [ ] **Step 7: Run Task 1 tests**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_database -v`
Expected: all Task 1 tests pass with no thread exceptions or database locks.

### Task 2: Qwen boundary and guarded action service

**Files:**
- Create: `apps/dimon_live/model_adapter.py`
- Create: `apps/dimon_live/service.py`
- Create: `apps/dimon_live/tests/test_model_adapter.py`
- Create: `apps/dimon_live/tests/test_service.py`

**Interfaces:**
- Consumes: `Database` and `TransferCommand` from Task 1
- Produces: immutable `ExtractedIntent(intent, raw_output, latency_ms)` dataclass
- Produces: `Extractor` protocol with `extract(text: str) -> ExtractedIntent`
- Produces: `QwenTransferExtractor.extract(text: str) -> ExtractedIntent`
- Produces: `validate_intent(raw_output: str) -> dict[str, Any]`
- Produces: `ActionService(database, extractor).process(request_id: str, text: str) -> dict[str, Any]`

- [ ] **Step 1: Write failing model-boundary tests**

Use injected fake HTTP transport and assert:

```python
def test_extractor_sends_strict_schema_without_tools_and_disables_thinking(self):
    calls = []
    extractor = QwenTransferExtractor(transport=self.success_transport(calls))
    extractor.extract(GOLDEN_REQUEST)
    payload = calls[0][1]
    self.assertNotIn("tools", payload)
    self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
    self.assertEqual(payload["response_format"]["type"], "json_schema")

def test_extractor_returns_the_six_validated_fields(self):
    extractor = QwenTransferExtractor(transport=self.success_transport([]))
    result = extractor.extract(GOLDEN_REQUEST)
    self.assertEqual(result.intent, GOLDEN_INTENT)
    self.assertEqual(json.loads(result.raw_output), GOLDEN_INTENT)

def test_extractor_rejects_finish_reason_length(self):
    extractor = QwenTransferExtractor(transport=self.response_transport("{}", finish_reason="length"))
    with self.assertRaisesRegex(InvalidModelOutput, "finish_reason"):
        extractor.extract(GOLDEN_REQUEST)

def test_extractor_rejects_extra_approval_or_command_fields(self):
    unsafe = {**GOLDEN_INTENT, "approved": True, "command": "python transfer.py"}
    extractor = QwenTransferExtractor(transport=self.response_transport(json.dumps(unsafe)))
    with self.assertRaisesRegex(InvalidModelOutput, "unexpected fields"):
        extractor.extract(GOLDEN_REQUEST)

def test_extractor_rejects_malformed_json(self):
    extractor = QwenTransferExtractor(transport=self.response_transport("{not-json"))
    with self.assertRaises(InvalidModelOutput):
        extractor.extract(GOLDEN_REQUEST)

def test_extractor_maps_timeout_to_model_unavailable(self):
    def timeout_transport(url, payload, timeout):
        raise TimeoutError("timed out")
    extractor = QwenTransferExtractor(transport=timeout_transport)
    with self.assertRaisesRegex(ModelUnavailable, "MODEL_UNAVAILABLE"):
        extractor.extract(GOLDEN_REQUEST)
```

Expected intent is the literal six-field John Smith/S$50 object already used in
the experiment tests.

- [ ] **Step 2: Run model tests and verify RED**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_model_adapter -v`
Expected: import failure because `model_adapter.py` does not exist.

- [ ] **Step 3: Implement the adapter**

Build the same OpenAI-compatible request used by the proven harness:

```python
{
  "model": "qwen35-9b-abliterated-q5",
  "temperature": 0,
  "max_tokens": 256,
  "chat_template_kwargs": {"enable_thinking": False},
  "messages": [system_message, user_message],
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "raw_beneficiary_transfer_intent",
      "strict": True,
      "schema": loaded_transfer_schema
    }
  }
}
```

Load the schema from
`experiments/local-structured-action/transfer_draft.schema.json`. Preserve the
existing field and semantic checks and require `finish_reason == "stop"`.
Convert HTTP timeout/refusal into `ModelUnavailable`; keep invalid output as
`InvalidModelOutput`.

- [ ] **Step 4: Run model tests and verify GREEN**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_model_adapter -v`

- [ ] **Step 5: Write failing service tests**

Use a callable fake extractor and a real temporary SQLite database:

```python
def test_exact_transfer_executes_and_returns_truthful_trace(self):
    service = self.service_for({**GOLDEN_INTENT})
    result = service.process(REQUEST_ID, GOLDEN_REQUEST)
    self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
    self.assertEqual(result["resolved_action"]["source_account_id"], "acct_savings_01")
    self.assertEqual(result["resolved_action"]["beneficiary_id"], "beneficiary_john_smith_01")
    self.assertEqual(result["trace"][-1]["stage"], "SQLITE_COMMITTED")
    self.assertEqual([row["balance_minor"] for row in result["state"]["accounts"][:2]], [95000, 15000])

def test_ambiguous_john_returns_candidates_without_mutation(self):
    before = self.database.get_state()["accounts"]
    result = self.service_for({**GOLDEN_INTENT, "recipient_label": "John"}).process(REQUEST_ID, "Send John S$50")
    self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
    self.assertEqual(result["candidate_display_names"], ["John Smith", "John Tan"])
    self.assertEqual(self.database.get_state()["accounts"], before)

def test_single_prefix_candidate_still_requires_clarification(self):
    result = self.service_for({**GOLDEN_INTENT, "recipient_label": "John S"}).process(REQUEST_ID, "Send John S S$50")
    self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
    self.assertEqual(result["candidate_display_names"], ["John Smith"])

def test_insufficient_funds_is_blocked_without_mutation(self):
    before = self.database.get_state()["accounts"]
    result = self.service_for({**GOLDEN_INTENT, "amount_minor": 100001}).process(REQUEST_ID, "Send John Smith S$1,000.01")
    self.assertEqual((result["outcome"], result["reason_code"]), ("BLOCKED", "INSUFFICIENT_FUNDS"))
    self.assertEqual(self.database.get_state()["accounts"], before)

def test_unsupported_action_is_blocked_without_mutation(self):
    before = self.database.get_state()["accounts"]
    result = self.service_for({**GOLDEN_INTENT, "kind": "BILL_PAYMENT"}).process(REQUEST_ID, "Pay my bill")
    self.assertEqual((result["outcome"], result["reason_code"]), ("BLOCKED", "UNSUPPORTED_ACTION"))
    self.assertEqual(self.database.get_state()["accounts"], before)

def test_invalid_model_output_is_failed_without_mutation(self):
    before = self.database.get_state()["accounts"]
    result = ActionService(self.database, RaisingExtractor(InvalidModelOutput("bad JSON"))).process(REQUEST_ID, GOLDEN_REQUEST)
    self.assertEqual((result["outcome"], result["reason_code"]), ("FAILED", "INVALID_MODEL_OUTPUT"))
    self.assertEqual(self.database.get_state()["accounts"], before)

def test_model_timeout_is_failed_without_mutation(self):
    before = self.database.get_state()["accounts"]
    result = ActionService(self.database, RaisingExtractor(ModelUnavailable("timeout"))).process(REQUEST_ID, GOLDEN_REQUEST)
    self.assertEqual((result["outcome"], result["reason_code"]), ("FAILED", "MODEL_UNAVAILABLE"))
    self.assertEqual(self.database.get_state()["accounts"], before)
```

For the success case, assert the response outcome is
`EXECUTED_SYNTHETIC_TRANSFER`, source/destination IDs are server-resolved,
status trace ends in `SQLITE_COMMITTED`, and state contains literal balances
`95000` and `15000`. For every failure case, snapshot and compare balances.

- [ ] **Step 6: Run service tests and verify RED**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_service -v`
Expected: import failure because `service.py` does not exist.

- [ ] **Step 7: Implement service outcomes**

Return stable response shapes with `outcome`, `request_id`, `trace`, and `state`.
Trace events are recorded only after their work actually finishes:

```text
MODEL_RESPONSE_RECEIVED
INTENT_VALIDATED
SOURCE_RESOLVED
RECIPIENT_RESOLVED
ELIGIBILITY_PASSED
SQLITE_COMMITTED
```

Map ambiguity to `CLARIFICATION_REQUEST`; invalid/unsupported/unfunded cases to
`BLOCKED`; model availability/format problems to `FAILED`. Never pass model text
into SQL identifiers or statements.

- [ ] **Step 8: Run all Task 2 and existing experiment tests**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_model_adapter apps.dimon_live.tests.test_service -v`
Run: `python3 -B -m unittest discover -s experiments/local-structured-action/tests -v`
Expected: both commands pass.

### Task 3: FastAPI application boundary

**Files:**
- Create: `apps/dimon_live/app.py`
- Create: `apps/dimon_live/static/index.html`
- Create: `apps/dimon_live/tests/test_api.py`

**Interfaces:**
- Consumes: `Database`, `QwenTransferExtractor`, and `ActionService`
- Produces: `create_app(db_path: Path, extractor: Extractor | None = None) -> FastAPI`
- Produces: module-level `app` for `uvicorn apps.dimon_live.app:app`

- [ ] **Step 1: Write failing API tests**

Use FastAPI `TestClient`, a temporary database, and fake extractor:

```python
def test_get_state_returns_seeded_demo_state(self):
    response = self.client.get("/api/state")
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()["accounts"][0]["balance_minor"], 100000)

def test_post_action_executes_eligible_transfer(self):
    response = self.client.post("/api/actions", json={"request_id": REQUEST_ID, "text": GOLDEN_REQUEST})
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")

def test_post_action_rejects_blank_text_without_calling_extractor(self):
    response = self.client.post("/api/actions", json={"request_id": REQUEST_ID, "text": "   "})
    self.assertEqual(response.status_code, 422)
    self.assertEqual(self.extractor.calls, [])

def test_post_action_rejects_text_over_500_characters_without_calling_extractor(self):
    response = self.client.post("/api/actions", json={"request_id": REQUEST_ID, "text": "x" * 501})
    self.assertEqual(response.status_code, 422)
    self.assertEqual(self.extractor.calls, [])

def test_post_action_rejects_non_uuid_request_id_without_calling_extractor(self):
    response = self.client.post("/api/actions", json={"request_id": "not-a-uuid", "text": GOLDEN_REQUEST})
    self.assertEqual(response.status_code, 422)
    self.assertEqual(self.extractor.calls, [])

def test_reset_restores_seed_state(self):
    self.client.post("/api/actions", json={"request_id": REQUEST_ID, "text": GOLDEN_REQUEST})
    response = self.client.post("/api/reset")
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()["accounts"][0]["balance_minor"], 100000)
    self.assertEqual(response.json()["transactions"], [])

def test_static_root_is_served_as_html(self):
    response = self.client.get("/")
    self.assertEqual(response.status_code, 200)
    self.assertTrue(response.headers["content-type"].startswith("text/html"))
```

- [ ] **Step 2: Run API tests and verify RED**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_api -v`
Expected: import failure because `app.py` does not exist.

- [ ] **Step 3: Implement the app factory and routes**

Define:

```python
class ActionRequest(BaseModel):
    request_id: UUID
    text: str = Field(min_length=1, max_length=500)

@router.get("/api/state")
@router.post("/api/actions")
@router.post("/api/reset")
@router.get("/api/health")
```

Trim text before the explicit empty check. Run synchronous SQLite/model work in
FastAPI's threadpool. Mount `static/` only after API routes and serve
`static/index.html` at `/`. Create a minimal semantic HTML shell so the root is
real during this task; Task 4 replaces its contents with the complete console.
The module-level app reads `DIMON_DB_PATH`,
`DIMON_QWEN_API_BASE`, and `DIMON_QWEN_MODEL`, with documented local defaults.

- [ ] **Step 4: Run API tests and verify GREEN**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_api -v`

- [ ] **Step 5: Run all backend tests**

Run: `python3 -B -m unittest discover -s apps/dimon_live/tests -v`
Expected: every database, adapter, service, and API test passes.

### Task 4: Polished live console

**Files:**
- Modify: `apps/dimon_live/static/index.html`
- Create: `apps/dimon_live/static/app.css`
- Create: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/tests/test_api.py`

**Interfaces:**
- Consumes: `GET /api/state`, `POST /api/actions`, and `POST /api/reset`
- Produces: one responsive, keyboard-accessible localhost interface

- [ ] **Step 1: Extend the static-delivery test and verify RED**

Assert `/static/app.css` and `/static/app.js` return HTTP 200 with their correct
content types. Before creating the assets, run:

`python3 -B -m unittest apps.dimon_live.tests.test_api -v`

Expected: static asset requests return 404.

- [ ] **Step 2: Build semantic HTML structure**

Include a visible `SYNTHETIC DEMO` badge, health indicator, balance-card region,
one labeled command form, live-region outcome panel, ordered trace list,
transaction table, empty state, and reset button. Do not add scripted transfer
buttons or conceal the free-form text input.

- [ ] **Step 3: Implement the visual system**

Use the approved dark “DIMON / LIVE” direction with a restrained green success
signal, tabular numerals, strong balance typography, clear focus states, and a
responsive two-column-to-one-column layout. Respect
`prefers-reduced-motion: reduce`; animation enhances but never carries the only
meaning.

- [ ] **Step 4: Implement browser behavior**

On load, fetch state. On submit, generate `crypto.randomUUID()`, disable only
the command form, show an honest pending Qwen state, POST the text, render the
returned trace, replace balances from returned state, and prepend the stored
transaction. Render clarification candidates as information—not executable
buttons. Reset requires one confirmation dialog, calls `/api/reset`, and fully
rerenders server state.

Format money exclusively from integer minor units:

```javascript
const formatSgd = minor => new Intl.NumberFormat("en-SG", {
  style: "currency",
  currency: "SGD"
}).format(minor / 100);
```

- [ ] **Step 5: Run API/static tests and inspect responsive behavior**

Run: `python3 -B -m unittest apps.dimon_live.tests.test_api -v`
Expected: API and all three static-resource checks pass.

Start the test server and inspect desktop and narrow viewport states. Confirm
Enter submits, focus remains visible, the live region announces outcomes, and
reduced-motion mode removes balance animation.

### Task 5: Runbook and end-to-end verification

**Files:**
- Create: `apps/dimon_live/README.md`
- Modify: `docs/README.md`

**Interfaces:**
- Consumes: completed application
- Produces: repeatable local startup and demo sequence

- [ ] **Step 1: Write the runbook**

Document exactly:

```bash
python3 -m uvicorn apps.dimon_live.app:app --host 127.0.0.1 --port 8890
```

Record the existing Qwen prerequisite at `http://127.0.0.1:8080/v1`, the model
ID `qwen35-9b-abliterated-q5`, the browser URL `http://127.0.0.1:8890`, the seed
balances, golden request, ambiguous request, insufficient-funds request, reset
procedure, and troubleshooting for `MODEL_UNAVAILABLE`.

- [ ] **Step 2: Run the complete automated verification**

Run:

```bash
python3 -B -m unittest discover -s apps/dimon_live/tests -v
python3 -B -m unittest discover -s experiments/local-structured-action/tests -v
```

Expected: zero failures and zero errors.

- [ ] **Step 3: Run one real-Qwen golden transfer**

Reset, submit `Send John Smith S$50 from savings.`, and verify from the API
response and a direct read of SQLite that:

- outcome is `EXECUTED_SYNTHETIC_TRANSFER`;
- savings is `95000` minor units;
- John Smith is `15000` minor units;
- exactly one ledger transaction exists; and
- its two entries sum to zero.

- [ ] **Step 4: Run the real ambiguity and idempotency demonstrations**

Reset and submit `Send John S$50 from savings.`; verify clarification and no
balance change. Submit one golden request twice with the same request UUID;
verify both responses refer to one transaction and savings remains `95000`.

- [ ] **Step 5: Perform browser verification**

At `http://127.0.0.1:8890`, verify the golden, ambiguous, insufficient-funds,
model-unavailable, and reset flows. Capture one final screenshot showing the
executed transfer, changed balance, completed trace, and ledger row.

- [ ] **Step 6: Final artifact checks**

Validate that runtime databases, bytecode, `.superpowers/`, and screenshots are
ignored or deliberately placed; no secret, absolute credential, or real account
data appears in source. Re-run the two complete unittest commands after the
last change.

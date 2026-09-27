# Grounded Clarification and Synthetic PayNow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ground every execution-critical transfer field in user evidence, support an amount-only stored clarification turn, add exact synthetic PayNow lookup with masked-name confirmation, and add bounded phonetic recipient suggestions without expanding model authority.

**Architecture:** Qwen emits a version-two discriminated intent with verbatim evidence spans. Pure deterministic code verifies and parses those spans; SQLite owns expiring partial clarifications, synthetic PayNow lookup records, version-two receipts, approval, and exactly-once execution. PayNow, multi-turn, fuzzy, and phonetic paths always pause on an immutable receipt.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic v2, Python `sqlite3`, JSON Schema, framework-free HTML/CSS/ES modules, `unittest`, Node.js client-module tests, and the existing local llama.cpp/Qwen endpoint.

**Spec:** `docs/superpowers/specs/2026-09-23-grounded-clarification-paynow-design.md`

## Global Constraints

- Synthetic localhost demo only; do not connect to PayNow, FAST, a bank, a real phone directory, or a production identity service.
- Keep the historical `experiments/local-structured-action/transfer_draft.schema.json` unchanged; Dimon Live owns its new version-two schema.
- Qwen may propose labels, proxies, values, and verbatim evidence only. It cannot create canonical IDs, masked names, lookup references, clarifications, receipts, approvals, or execution commands.
- A model-proposed amount is unusable until deterministic parsing of a verbatim evidence span produces the same integer minor-unit value.
- Only a missing amount receives multi-turn continuation in this version. The follow-up accepts an amount-only grammar and never invokes the full Qwen extractor.
- Exact full saved beneficiaries may retain the existing immediate synthetic policy only for a one-turn, explicit, fully grounded request.
- Partial, fuzzy, phonetic, PayNow, and multi-turn-completed requests always require receipt confirmation.
- Mobile proxies are normalized but never fuzzy-matched or auto-corrected.
- Confirmation accepts only receipt ID and digest and never calls Qwen.
- Existing version-one receipts remain verifiable; all newly created receipts use contract version two.
- Preserve the atomic balanced-ledger and exactly-once invariants.
- Use installed dependencies only. Implement the bounded amount parser and phonetic key locally.
- This workspace is not a Git repository. Do not run `git init`; commit steps are intentionally omitted.

## Review Focus

- `send Alicia Powell money` must not execute if Qwen invents `amount_minor=1000`; Task 3 tests missing evidence and exact-recipient immediate-policy bypass attempts.
- Two simultaneous `ten dollars` replies to one clarification must create one receipt group and no transaction; Task 4 adds a barrier-based concurrency test.
- `86952933` and `86952938` must never converge through normalization or similarity; Task 5 tests exact proxy identity and changed-digit failure.
- A PayNow masked name or lookup reference changed in SQLite must fail digest/revalidation without a debit; Tasks 2 and 5 test canonical binding and lookup revalidation.
- Phonetic collisions may create multiple confirmation candidates but must never reach `EXPLICIT_COMMAND_POLICY`; Task 6 tests both a positive `Alesha` case and a collision/false-positive case.

---

## File Structure

```text
apps/dimon_live/
  action_intent.schema.json          Version-two discriminated raw intent
  grounding.py                       Evidence normalization and SGD amount parsing
  model_adapter.py                   Qwen request and strict v2 validation
  name_resolution.py                 Existing lexical plus bounded phonetic fallback
  paynow.py                          Exact mobile normalization and lookup interface
  receipts.py                        Versioned canonical receipt payloads
  database.py                        Additive migrations, clarification/lookup persistence
  service.py                         Grounding, resolution, clarification and PayNow policy
  app.py                             Clarification response API and static module route
  static/
    clarification_response.mjs       Exact clarification response client
    app.js                            Clarification mode and PayNow receipt rendering
    app.css                           Locked-fact and clarification controls
  tests/
    test_grounding.py                 Pure evidence/amount parser cases
    test_model_adapter.py             V2 schema and strict model envelope
    test_receipts.py                  V1 compatibility and V2 canonical binding
    test_database.py                  Migrations, clarification concurrency, lookup checks
    test_service.py                   End-to-end policy without HTTP
    test_api.py                       Strict endpoint contracts
    test_ui_client.py                 Exact clarification retry coordinates
    test_recipient_resolution_eval.py Phonetic holdouts and negative controls
docs/superpowers/specs/
  2026-09-23-grounded-clarification-paynow-design.md
apps/dimon_live/README.md             Demo flow and safety boundary
```

### Task 1: Pure Critical-Field Grounding

**Files:**
- Create: `apps/dimon_live/grounding.py`
- Create: `apps/dimon_live/tests/test_grounding.py`

**Interfaces:**
- Produces: `GroundingError(reason_code: str)`
- Produces: `normalize_evidence(value: str) -> str`
- Produces: `is_grounded(original_text: str, evidence: str) -> bool`
- Produces: `parse_sgd_amount(value: str, *, require_full_match: bool = False) -> int`
- Produces: `verify_amount(original_text: str, amount_minor: int | None, evidence: str | None) -> int | None`
- Produces: `verify_optional_reference(original_text: str, reference: str | None, evidence: str | None) -> str | None`
- Consumed by: Task 3 service grounding and Task 4 amount-only clarification responses.

- [ ] **Step 1: Write failing literal amount and evidence tests**

Create `test_grounding.py` with hand-derived values:

```python
import unittest

from apps.dimon_live.grounding import (
    GroundingError,
    is_grounded,
    parse_sgd_amount,
    verify_amount,
)


class GroundingTests(unittest.TestCase):
    def test_numeric_and_word_amounts_become_exact_minor_units(self):
        cases = (
            ("S$10", 1000),
            ("$2.50", 250),
            ("1,234.56 SGD", 123456),
            ("ten dollars", 1000),
            ("twenty one dollars", 2100),
            ("one hundred and five dollars", 10500),
            ("two thousand dollars", 200000),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_sgd_amount(text, require_full_match=True), expected)

    def test_missing_amount_stays_missing(self):
        self.assertIsNone(verify_amount("send Alicia money", None, None))

    def test_invented_or_changed_amount_is_rejected(self):
        cases = (
            ("send Alicia money", 1000, "$10", "UNGROUNDED_AMOUNT"),
            ("send Alicia $10", 5000, "$10", "AMOUNT_EVIDENCE_MISMATCH"),
        )
        for original, amount, evidence, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(GroundingError, reason):
                    verify_amount(original, amount, evidence)

    def test_amount_only_mode_rejects_extra_instruction(self):
        with self.assertRaisesRegex(GroundingError, "CLARIFICATION_SCOPE_VIOLATION"):
            parse_sgd_amount("send John ten dollars", require_full_match=True)

    def test_evidence_matching_folds_case_unicode_and_whitespace(self):
        self.assertTrue(is_grounded("Pay ALÍCIA   ten dollars", "alícia ten dollars"))
```

Name the mutations these tests catch before running: returning a default for
missing amounts, trusting model integers, accepting an amount substring in the
clarification endpoint, or comparing raw evidence without normalization.

- [ ] **Step 2: Verify RED**

Run:

```bash
python3 -B -m unittest apps.dimon_live.tests.test_grounding -v
```

Expected: import failure because `apps.dimon_live.grounding` does not exist.

- [ ] **Step 3: Implement the bounded parser**

Implement `grounding.py` with:

```python
class GroundingError(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def verify_amount(original_text, amount_minor, evidence):
    if amount_minor is None and evidence is None:
        return None
    if amount_minor is None or evidence is None or not is_grounded(original_text, evidence):
        raise GroundingError("UNGROUNDED_AMOUNT")
    parsed = parse_sgd_amount(evidence, require_full_match=True)
    if parsed != amount_minor:
        raise GroundingError("AMOUNT_EVIDENCE_MISMATCH")
    return parsed
```

Use `Decimal` for numeric major-unit parsing, explicit dictionaries for English
number grammar (`zero` through `nineteen`, tens, `hundred`, `thousand`), and
`ROUND_DOWN` only after rejecting more than two fractional digits. The parser
must consume the whole value when `require_full_match=True`; it must never use a
floating-point number.

- [ ] **Step 4: Run the grounding tests GREEN**

Run the command from Step 2. Expected: all grounding tests pass.

- [ ] **Step 5: Run the existing suites**

```bash
python3 -B -m unittest discover -s apps/dimon_live/tests -v
python3 -B -m unittest discover -s experiments/local-structured-action/tests -v
```

Expected baseline after adding isolated code: 83 existing Dimon Live tests plus
the new grounding tests pass, and 18/18 historical harness tests pass.

### Task 2: Version-Two Receipt and Additive Storage Migration

**Files:**
- Modify: `apps/dimon_live/receipts.py`
- Modify: `apps/dimon_live/database.py`
- Modify: `apps/dimon_live/tests/test_receipts.py`
- Modify: `apps/dimon_live/tests/test_database.py`

**Interfaces:**
- Extends `ReceiptDraft` with `destination_kind`, `destination_reference`,
  `lookup_reference`, `evidence_bundle`, and `contract_version`.
- Produces: `build_expected_receipt_payload(row: Mapping[str, Any]) -> dict[str, Any]`
  with explicit v1 and v2 branches.
- Adds nullable/defaulted v2 columns to `review_receipts` without deleting v1 rows.
- Adds `clarification_sessions`, `paynow_proxies`, and `paynow_lookups` tables.
- Adds `directory_kind` to `beneficiaries`, defaulting existing rows to `SAVED`.
- Consumed by: Tasks 4 and 5.

- [ ] **Step 1: Write failing v2 canonical and migration tests**

Add tests that construct this literal draft:

```python
draft = ReceiptDraft(
    request_id="00000000-0000-4000-8000-000000000090",
    original_text="PayNow 8695 2933 ten dollars",
    user_id="demo_user_01",
    source_account_id="acct_savings_01",
    beneficiary_id="beneficiary_paynow_jxxx_01",
    recipient_display_name="JXXX SXXXX",
    amount_minor=1000,
    currency="SGD",
    destination_kind="PAYNOW_MOBILE",
    destination_reference="86952933",
    lookup_reference="lookup_fixed",
    evidence_bundle=(
        {"turn": 1, "text": "PayNow 8695 2933 ten dollars", "amount": "ten dollars", "proxy": "8695 2933"},
    ),
)
```

Assert that the canonical payload has `contract_version == 2`, contains all
destination and evidence fields, and changes digest when any one of amount,
proxy, masked name, lookup reference, or evidence changes. Add a migration test
that creates the current v1 schema, inserts a v1 receipt, calls `initialize()`,
and proves the row still verifies while a new v2 row contains the added fields.

- [ ] **Step 2: Verify RED**

```bash
python3 -B -m unittest apps.dimon_live.tests.test_receipts apps.dimon_live.tests.test_database -v
```

Expected: `ReceiptDraft` rejects the v2 fields and the new tables/columns are
absent.

- [ ] **Step 3: Implement explicit additive migrations**

In `Database.initialize()`, inspect `PRAGMA table_info` and apply named,
idempotent additions:

```sql
ALTER TABLE beneficiaries ADD COLUMN directory_kind TEXT NOT NULL DEFAULT 'SAVED';
ALTER TABLE review_receipts ADD COLUMN contract_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE review_receipts ADD COLUMN destination_kind TEXT;
ALTER TABLE review_receipts ADD COLUMN destination_reference TEXT;
ALTER TABLE review_receipts ADD COLUMN lookup_reference TEXT;
ALTER TABLE review_receipts ADD COLUMN evidence_bundle_json TEXT;
```

Create `clarification_sessions`, `paynow_proxies`, and `paynow_lookups` with the
fields and lifecycle constraints in the spec. Never catch and ignore arbitrary
SQLite errors; skip only columns already proven present by `PRAGMA table_info`.

- [ ] **Step 4: Implement versioned receipt construction and verification**

Set new drafts to version two. Build the v2 canonical payload from server-owned
fields and canonical JSON for `evidence_bundle`. In `_execute_stored_receipt`,
read `contract_version` from stored canonical JSON and reconstruct exactly the
v1 field set for version one or the v2 field set for version two. Reject every
unknown version as `RECEIPT_INVALID_VERSION`.

For a saved beneficiary default `destination_reference` to the server-owned
beneficiary ID; never derive PayNow fields from `original_text` during approval.

- [ ] **Step 5: Run receipt/database tests GREEN, then full suites**

Run Step 2, followed by both discovery commands from Task 1. Existing execution,
tamper, expiry, rollback, and concurrency tests must remain green.

### Task 3: Version-Two Qwen Contract and One-Turn Grounding Policy

**Files:**
- Create: `apps/dimon_live/action_intent.schema.json`
- Modify: `apps/dimon_live/model_adapter.py`
- Modify: `apps/dimon_live/service.py`
- Modify: `apps/dimon_live/tests/test_model_adapter.py`
- Modify: `apps/dimon_live/tests/test_service.py`
- Modify: `apps/dimon_live/tests/test_api.py`

**Interfaces:**
- Produces the two strict raw intent variants defined in the spec.
- Adds `validate_intent(raw_output: str) -> dict[str, Any]` cross-field paired-null checks.
- Adds service grounding before source or recipient resolution.
- Produces `MISSING_AMOUNT`, `UNGROUNDED_AMOUNT`,
  `AMOUNT_EVIDENCE_MISMATCH`, `MISSING_RECIPIENT`,
  `MISSING_PAYNOW_PROXY`, `UNGROUNDED_RECIPIENT`, and `UNGROUNDED_SOURCE`.
- At this task boundary, `MISSING_AMOUNT` is a non-mutating response; Task 4
  persists the continuation session.

- [ ] **Step 1: Write failing adapter tests**

Add literal saved and PayNow fixtures. Assert the Qwen request uses
`action_intent.schema.json`, still disables thinking, and contains no tools.
Assert validation accepts paired null amount fields, rejects amount without
evidence, rejects fields from the wrong discriminator branch, and rejects
unexpected IDs or approval fields.

```python
MISSING_AMOUNT_INTENT = {
    "contract_version": 2,
    "kind": "BENEFICIARY_TRANSFER",
    "source_account_ref": None,
    "source_evidence": None,
    "recipient_label": "Alicia Powell",
    "recipient_evidence": "Alicia Powell",
    "amount_minor": None,
    "amount_evidence": None,
    "currency": "SGD",
    "payment_reference": "flowers",
    "reference_evidence": "for flowers",
    "requested_execution_date": None,
}
```

- [ ] **Step 2: Write the service safety regression before production changes**

Using a real temporary `Database` and `StaticExtractor`, submit
`send Alicia Powell money for flowers` while the model proposes S$10 with
`amount_evidence="$10"`. Assert `UNGROUNDED_AMOUNT`, unchanged balances, zero
receipts, and zero transactions. Then submit the null-amount fixture and assert
`MISSING_AMOUNT` with the resolved Alicia display name and no receipt.

- [ ] **Step 3: Verify RED**

```bash
python3 -B -m unittest apps.dimon_live.tests.test_model_adapter -v
python3 -B -m unittest apps.dimon_live.tests.test_service -v
```

Expected: v2 fixtures are rejected or trusted without grounding.

- [ ] **Step 4: Implement the schema, prompt, validation, and service guard**

The system prompt must say: use null when absent, copy exact evidence spans,
never infer a default amount, and never output a masked name or canonical ID.
Call `verify_amount`, evidence checks, and source/recipient cross-field checks
immediately after schema validation and before balance or directory logic.

Update every existing service/API fixture to v2 with evidence that literally
occurs in its request. Do not add a compatibility path for legacy model output;
only persisted v1 receipts require compatibility.

- [ ] **Step 5: Verify GREEN and run full suites**

Run Step 3 and both discovery commands. Specifically confirm the exact saved
recipient cannot bypass an ungrounded amount through immediate policy.

### Task 4: Atomic Stored Amount Clarification

**Files:**
- Modify: `apps/dimon_live/database.py`
- Modify: `apps/dimon_live/service.py`
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/tests/test_database.py`
- Modify: `apps/dimon_live/tests/test_service.py`
- Modify: `apps/dimon_live/tests/test_api.py`

**Interfaces:**
- Produces: `Database.create_amount_clarification(*, request_id: str, original_text: str, user_id: str, partial_action: Mapping[str, Any], evidence_bundle: Sequence[Mapping[str, Any]], raw_model_output: str, validated_intent: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]`
- Produces: `Database.complete_amount_clarification(clarification_id: str, response_request_id: str, response_text: str, user_id: str, *, now: datetime | None = None) -> dict[str, Any]`
- Produces: `ActionService.respond_to_clarification(clarification_id, request_id, text, user_id=DEMO_USER_ID) -> dict[str, Any]`
- Adds: `POST /api/clarifications/{clarification_id}/respond`
- Request body: strict `{request_id: UUID, text: 1..100 chars}` with no extra fields.

- [ ] **Step 1: Write failing persistence and lifecycle tests**

Test with a fixed clock:

```python
pending = database.create_amount_clarification(
    request_id=ORIGINAL_REQUEST_ID,
    original_text="send Alicia Powell money for flowers",
    user_id="demo_user_01",
    partial_action={
        "kind": "BENEFICIARY_TRANSFER",
        "source_account_id": "acct_savings_01",
        "beneficiary_ids": ["beneficiary_alicia_powell_01"],
        "recipient_display_names": ["Alicia Powell"],
        "currency": "SGD",
        "payment_reference": "flowers",
        "reason_code": "MISSING_AMOUNT",
    },
    evidence_bundle=[{"turn": 1, "text": "send Alicia Powell money for flowers"}],
    raw_model_output="{}",
    validated_intent=MISSING_AMOUNT_INTENT,
    now=FIXED_NOW,
)
```

Assert one `AWAITING_RESPONSE` session, zero receipts, and zero transactions.
Complete it with `ten dollars`; assert one pending S$10 receipt, session status
`COMPLETED`, and unchanged balances. Add tests for expiry at exactly five
minutes, wrong user, missing session, changed response text under the same UUID,
scope violation, and identical replay returning the same receipt.

- [ ] **Step 2: Add a two-thread completion test and verify RED**

Use `Barrier(2)` and two database instances against the same file. Both threads
submit the same clarification response UUID. Assert both receive the same
receipt ID and the database contains one receipt group and no transaction.

Run:

```bash
python3 -B -m unittest apps.dimon_live.tests.test_database.DatabaseSeedTests.test_two_threads_complete_one_clarification_once -v
```

Expected: missing database method.

- [ ] **Step 3: Implement creation and completion under `BEGIN IMMEDIATE`**

Creation must store the original `action_attempt` and session atomically.
Completion must acquire the write lock before reading the clock; check existing
response request ID first; validate session owner/status/expiry; parse the full
response with `parse_sgd_amount(response_text, require_full_match=True)`; create v2 receipt
drafts solely from stored IDs plus parsed amount; insert the response
`action_attempt`; mark the session `COMPLETED`; and commit once.

The evidence bundle for the receipt must contain literal turn one and turn two
text and the parsed amount evidence. No call to `Extractor.extract()` occurs.

- [ ] **Step 4: Implement service and strict API mapping**

Return a clarification object shaped as:

```json
{
  "clarification_id": "clarification_fixed",
  "missing_field": "amount_minor",
  "locked_facts": {
    "source_label": "savings",
    "recipient_display_names": ["Alicia Powell"],
    "currency": "SGD"
  },
  "expires_at": "2026-09-23T07:05:00+00:00"
}
```

Map stable lifecycle and scope errors to non-mutating responses. Reject client
amounts, recipients, source IDs, and digests at this endpoint with HTTP 422.

- [ ] **Step 5: Run targeted and full suites GREEN**

Run all database, service, and API tests, then both discovery suites.

### Task 5: Exact Synthetic PayNow Lookup and Receipt Execution

**Files:**
- Create: `apps/dimon_live/paynow.py`
- Create: `apps/dimon_live/tests/test_paynow.py`
- Modify: `apps/dimon_live/database.py`
- Modify: `apps/dimon_live/service.py`
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/tests/test_database.py`
- Modify: `apps/dimon_live/tests/test_service.py`
- Modify: `apps/dimon_live/tests/test_api.py`

**Interfaces:**
- Produces: `normalize_mobile_proxy(evidence: str) -> str`
- Produces: immutable `PayNowLookup` with `lookup_reference`,
  `canonical_proxy`, `masked_name`, `beneficiary_id`, and `expires_at`.
- Produces: `PayNowDirectory` protocol with
  `lookup_mobile(canonical_proxy: str, request_id: str) -> PayNowLookup`.
- Produces: `SyntheticPayNowDirectory(Database)` implementation.
- Seeds exact synthetic proxy `86952933` -> `JXXX SXXXX` -> synthetic destination account.

- [ ] **Step 1: Write failing normalization and directory tests**

```python
def test_supported_spellings_normalize_to_one_exact_proxy(self):
    for value in ("86952933", "8695 2933", "8695-2933", "+65 8695 2933"):
        self.assertEqual(normalize_mobile_proxy(value), "86952933")

def test_changed_digit_never_resolves(self):
    with self.assertRaisesRegex(PayNowLookupError, "PAYNOW_PROXY_NOT_FOUND"):
        self.directory.lookup_mobile("86952938", REQUEST_ID)
```

Also assert letters, too few/many digits, unsupported prefixes, and punctuation
outside the allowed normalization grammar return `INVALID_PAYNOW_PROXY`.

- [ ] **Step 2: Write failing PayNow service and receipt tests**

For `PayNow 8695 2933 ten dollars for flowers`, assert:

- Qwen evidence is verified;
- the directory returns `JXXX SXXXX` and an opaque lookup reference;
- outcome is `CLARIFICATION_REQUEST` even with an explicit `pay` verb;
- the receipt binds `PAYNOW_MOBILE`, `86952933`, the lookup reference, masked
  name, S$10, and grounded reference;
- balances remain unchanged before approval;
- approval credits the synthetic destination exactly once.

Add tamper cases for proxy, lookup reference, masked name, destination mapping,
and expired lookup; every case must produce zero new ledger entries.

- [ ] **Step 3: Verify RED**

```bash
python3 -B -m unittest apps.dimon_live.tests.test_paynow apps.dimon_live.tests.test_service apps.dimon_live.tests.test_database -v
```

Expected: PayNow module and branch do not exist.

- [ ] **Step 4: Implement exact lookup persistence and filtering**

Seed a synthetic destination account, hidden `PAYNOW` beneficiary, and proxy in
`Database._insert_seed()`. Filter saved-name resolution with
`directory_kind = 'SAVED'`. Make lookup request-idempotent and store its returned
masked name snapshot and expiry in `paynow_lookups`.

PayNow receipt creation must use only the lookup row for masked name and
beneficiary ID. Approval must join the receipt's lookup reference to the lookup
and proxy tables and compare every bound value before ledger writes.

- [ ] **Step 5: Integrate one-turn and missing-amount PayNow flows**

A grounded one-turn PayNow request creates a pending receipt. A missing-amount
PayNow request performs the exact lookup, stores its lookup facts in the
clarification session, and creates the pending receipt only after an amount-only
reply. If the lookup expires before completion, return `PAYNOW_LOOKUP_EXPIRED`
and require a new request.

- [ ] **Step 6: Run PayNow, full app, and historical harness suites GREEN**

Run Step 3 followed by both discovery commands.

### Task 6: Confirmation-Only Phonetic Candidate Retrieval

**Files:**
- Modify: `apps/dimon_live/name_resolution.py`
- Modify: `apps/dimon_live/database.py`
- Modify: `apps/dimon_live/service.py`
- Modify: `apps/dimon_live/tests/test_recipient_resolution_eval.py`
- Modify: `apps/dimon_live/tests/test_service.py`

**Interfaces:**
- Produces: `phonetic_key(token: str) -> str` for normalized Latin tokens.
- Extends `Resolution.kind` with `phonetic`.
- Produces service reason `PHONETIC_RECIPIENT_MATCH`.
- Keeps all phonetic results on `USER_CONFIRMATION` receipts.

- [ ] **Step 1: Write held-out positive, collision, and negative tests**

Add literal cases rather than production aliases:

```python
def test_phonetic_holdout_suggests_but_never_exactly_resolves(self):
    result = self.database.resolve_beneficiary("Alesha")
    self.assertEqual(result.kind, "phonetic")
    self.assertEqual([row["display_name"] for row in result.matches], ["Alicia Powell"])

def test_phonetic_match_requires_confirmation_without_mutation(self):
    result = self.service_for(ALESHA_INTENT).process(REQUEST_ID, "send Alesha $10")
    self.assertEqual(result["reason_code"], "PHONETIC_RECIPIENT_MATCH")
    self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
    self.assertEqual(self.database.get_state()["transactions"], [])
```

Retain unrelated-name controls and add at least one synthetic phonetic collision
that returns both candidates rather than choosing one. The exact output is a
candidate set; no score may change it to `exact`.

- [ ] **Step 2: Verify RED**

Run the recipient evaluation and named service test. Expected: `Alesha` remains
missing.

- [ ] **Step 3: Implement one name-agnostic phonetic key**

Normalize diacritics and Latin letters, preserve the first letter, remove
vowels after the first position, collapse adjacent consonant classes, and map
the standard Soundex-style consonant groups to a fixed key. Apply it to each
query and saved-name token only after exact, token/prefix, and lexical layers
return no candidate. Empty/non-Latin keys do not match.

Return every directory row with a matching token key, ordered by display name.
Do not add `Alesha`, `Alisha`, or other spelling variants to production data.

- [ ] **Step 4: Run evaluation, service, and full suites GREEN**

Report generated lexical variation count, phonetic positive recall, unrelated
negative results, and—separately—zero automatic phonetic executions.

### Task 7: Multi-Turn and PayNow User Interface

**Files:**
- Create: `apps/dimon_live/static/clarification_response.mjs`
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/app.css`
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/tests/test_ui_client.py`
- Modify: `apps/dimon_live/tests/test_api.py`

**Interfaces:**
- Produces: `requestClarificationResponse(clarification, requestId, text, fetchImpl=globalThis.fetch)`.
- The UI stores only `clarification_id` and expiry as active continuation
  coordinates; locked facts remain server response data.
- The main form switches between `/api/actions` and the clarification endpoint.

- [ ] **Step 1: Write failing executable client-module tests**

Use Node from `unittest` as the existing receipt client test does. A fake fetch
must prove the module calls exactly:

```text
POST /api/clarifications/clarification_fixed/respond
{"request_id":"00000000-0000-4000-8000-000000000099","text":"ten dollars"}
```

Simulate a lost first response and assert the retry uses the identical URL and
body. Do not assert source text with grep when executable module behavior can be
tested.

- [ ] **Step 2: Verify RED**

```bash
python3 -B -m unittest apps.dimon_live.tests.test_ui_client -v
```

Expected: module not found.

- [ ] **Step 3: Implement clarification mode and receipt display**

When `reason_code === "MISSING_AMOUNT"`, render locked source and recipient or
PayNow masked name, change the input label to `How much?`, change the button to
`Continue exact request`, and retain the server clarification coordinates.
Submitting calls the dedicated module and then clears clarification mode only
after a definitive server response. A lost response preserves coordinates and
offers safe retry.

Render PayNow receipt cards with `Via PayNow`, canonical proxy, masked name,
amount, reference, and expiry. Render a `Possible phonetic match` label for
phonetic candidates. Add `Start a new request` to clear client clarification
mode without mutating server state.

- [ ] **Step 4: Serve the module and verify API/static contracts**

Add the explicit static route and media type. Extend API tests to load the
module, execute its exported function, reject extra clarification request
fields, and verify the approval route still accepts only receipt digest.

- [ ] **Step 5: Run JavaScript syntax and all tests GREEN**

```bash
node --check apps/dimon_live/static/app.js
node --check apps/dimon_live/static/clarification_response.mjs
node --check apps/dimon_live/static/receipt_approval.mjs
python3 -B -m unittest discover -s apps/dimon_live/tests -v
python3 -B -m unittest discover -s experiments/local-structured-action/tests -v
```

### Task 8: Documentation, Live Qwen Evaluation, and Final Review

**Files:**
- Modify: `apps/dimon_live/README.md`
- Modify: `.superpowers/sdd/2026-09-23-receipt-bound-confirmation/progress.md`

**Interfaces:**
- No new production interface; this task supplies reproducible completion evidence.

- [ ] **Step 1: Document the exact demo sequence**

Add these synthetic scenarios and expected outcomes:

1. `send Alicia Powell money for flowers` -> stored `MISSING_AMOUNT`; reply
   `ten dollars` -> immutable confirmation receipt; approval -> one S$10 debit.
2. A malicious/invented model amount without evidence -> blocked, no receipt.
3. `PayNow 8695 2933 ten dollars for flowers` -> `JXXX SXXXX` lookup-backed
   receipt, mandatory confirmation, one balanced synthetic transfer.
4. Changed digit `8695 2938` -> exact lookup failure, no candidate correction.
5. `send Alesha $10` -> Alicia Powell phonetic suggestion, no mutation before
   confirmation.

State prominently that the PayNow directory, masked name, accounts, and ledger
are synthetic and not connected to the real scheme.

- [ ] **Step 2: Run deterministic verification from a clean ledger**

Run both full suites and all syntax checks from Task 7. Record exact pass counts.
Then reset the synthetic database.

- [ ] **Step 3: Run live Qwen probes**

Against the existing local endpoint, record raw structured output and gateway
outcome for:

```text
send Alicia Powell money for flowers
send Alicia Powell $10 for flowers
send Alicia Powell ten dollars for flowers
PayNow 8695 2933 ten dollars for flowers
send Alesha $10
```

For every probe, compare extracted evidence with the original text and inspect
the ledger. Do not approve until the returned receipt facts are checked. Approve
one saved multi-turn receipt and one PayNow receipt, verify one transaction each,
then reset the synthetic ledger.

- [ ] **Step 4: Request one fresh whole-change review**

The reviewer checks the spec, current affected files, complete test output,
critical amount-grounding bypasses, clarification concurrency, PayNow lookup
binding, v1 receipt compatibility, and phonetic non-authorization. Fix Critical
and Important findings in one TDD pass; record deferred Minor findings.

- [ ] **Step 5: Restart and smoke-test localhost**

Resolve the exact process bound to port 8890, restart only that process, reset
the synthetic ledger, and verify HTTP 200 for `/`, `/api/state`, `/api/health`,
and both client modules. Leave the app running with clean seed balances.

## Completion Gate

The feature is complete only when:

- no missing or ungrounded amount can create a receipt or ledger mutation;
- an amount-only response can complete only its stored partial request;
- PayNow proxies resolve exactly and display only directory-owned masked names;
- PayNow, multi-turn, lexical-fuzzy, and phonetic paths require confirmation;
- receipt approval remains immutable, model-free, atomic, and exactly once;
- all deterministic suites, syntax checks, selected live Qwen probes, and the
  fresh review are clean or have explicitly documented deferred Minor findings.

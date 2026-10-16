import dataclasses
import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event

from apps.dimon_live.database import (
    ActionAttempt,
    ClarificationRejected,
    Database,
    IdempotencyConflict,
    InsufficientFunds,
    ReceiptRejected,
    ResolutionError,
    SCHEMA,
)
from apps.dimon_live.batch_receipts import BatchReceiptDraft, BatchReceiptItemDraft
from apps.dimon_live.receipts import ReceiptDraft, build_receipt_record
from apps.dimon_live.tests.device_signer import SoftwareAuthenticator


FIXED_NOW = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)
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


class DatabaseSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "demo.sqlite3"
        self.database = Database(self.db_path, single_account=True)
        self.database.initialize()
        self.device = SoftwareAuthenticator()
        self.device.enroll(self.database)

    def tearDown(self):
        self.temp_dir.cleanup()

    def receipt_draft(
        self,
        *,
        beneficiary_id="beneficiary_alicia_powell_01",
        recipient="Alicia Powell",
    ):
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

    def batch_draft(self):
        return BatchReceiptDraft(
            request_id="00000000-0000-4000-8000-000000000140",
            original_text="Send John Smith $6.50 and Alicia Powell $4",
            user_id="demo_user_01",
            source_account_id="acct_savings_01",
            currency="SGD",
            items=(
                BatchReceiptItemDraft(
                    0,
                    "beneficiary_john_smith_01",
                    "acct_john_smith_01",
                    "John Smith",
                    650,
                    "John Smith",
                    "$6.50",
                ),
                BatchReceiptItemDraft(
                    1,
                    "beneficiary_alicia_powell_01",
                    "acct_alicia_powell_01",
                    "Alicia Powell",
                    400,
                    "Alicia Powell",
                    "$4",
                ),
            ),
        )

    def utility_bill_draft(self, amount_minor=12_000):
        return ReceiptDraft(
            request_id="00000000-0000-4000-8000-000000000042",
            original_text="Pay my Singapore Power bill $120 from savings.",
            user_id="demo_user_01",
            source_account_id="acct_savings_01",
            beneficiary_id="biller_sp_services",
            recipient_display_name="SP Services",
            amount_minor=amount_minor,
            currency="SGD",
            destination_kind="UTILITY_BILL",
            destination_reference="biller_sp_services",
            destination_account_id="acct_biller_sp_services",
            evidence_bundle=(
                {"turn": 1, "text": "Pay my Singapore Power bill $120 from savings."},
                {"kind": "UTILITY_BILL_PAYMENT", "biller_evidence": "Singapore Power", "amount_evidence": "$120"},
            ),
        )

    def john_candidate_drafts(self):
        request_id = "00000000-0000-4000-8000-000000000041"
        original_text = "Send John five dollars"
        return (
            dataclasses.replace(
                self.receipt_draft(
                    beneficiary_id="beneficiary_john_smith_01",
                    recipient="John Smith",
                ),
                request_id=request_id,
                original_text=original_text,
            ),
            dataclasses.replace(
                self.receipt_draft(
                    beneficiary_id="beneficiary_john_tan_01",
                    recipient="John Tan",
                ),
                request_id=request_id,
                original_text=original_text,
            ),
        )

    def create_pending_alicia(self):
        return self.database.create_pending_receipts(
            (self.receipt_draft(),),
            raw_model_output="{}",
            validated_intent={},
            reason_code="ACTION_CONFIRMATION_REQUIRED",
            response_base={
                "outcome": "CLARIFICATION_REQUEST",
                "question": "Send S$5.00 to Alicia Powell from savings?",
                "trace": [],
            },
            now=FIXED_NOW,
        )

    def create_pending_johns(self):
        return self.database.create_pending_receipts(
            self.john_candidate_drafts(),
            raw_model_output="{}",
            validated_intent={},
            reason_code="AMBIGUOUS_RECIPIENT",
            response_base={
                "outcome": "CLARIFICATION_REQUEST",
                "question": "Which saved John?",
                "trace": [],
            },
            now=FIXED_NOW,
        )

    def count_rows(self, table):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            return connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]

    def create_amount_clarification(self, *, now=FIXED_NOW):
        return self.database.create_amount_clarification(
            request_id="00000000-0000-4000-8000-000000000070",
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
            evidence_bundle=[
                {"turn": 1, "text": "send Alicia Powell money for flowers"}
            ],
            raw_model_output="{}",
            validated_intent=MISSING_AMOUNT_INTENT,
            now=now,
        )

    def test_amount_clarification_creates_one_pending_receipt_on_completion(self):
        balances_before = [
            row["balance_minor"] for row in self.database.get_state()["accounts"]
        ]
        pending = self.create_amount_clarification()

        self.assertEqual(pending["reason_code"], "MISSING_AMOUNT")
        self.assertEqual(pending["clarification"]["missing_field"], "amount_minor")
        self.assertEqual(self.count_rows("clarification_sessions"), 1)
        self.assertEqual(self.count_rows("review_receipts"), 0)

        completed = self.database.complete_amount_clarification(
            pending["clarification"]["clarification_id"],
            "00000000-0000-4000-8000-000000000071",
            "ten dollars",
            "demo_user_01",
            now=FIXED_NOW + timedelta(minutes=1),
        )

        self.assertEqual(completed["reason_code"], "AMOUNT_CLARIFICATION_COMPLETED")
        self.assertEqual(completed["receipt_choices"][0]["amount_minor"], 1000)
        self.assertEqual(self.count_rows("review_receipts"), 1)
        self.assertEqual(self.count_rows("ledger_transactions"), 0)
        self.assertEqual(
            [row["balance_minor"] for row in self.database.get_state()["accounts"]],
            balances_before,
        )
        self.assertEqual(
            self.scalar("SELECT status FROM clarification_sessions"), "COMPLETED"
        )

        replay = self.database.complete_amount_clarification(
            pending["clarification"]["clarification_id"],
            "00000000-0000-4000-8000-000000000071",
            "ten dollars",
            "demo_user_01",
            now=FIXED_NOW + timedelta(minutes=2),
        )
        self.assertEqual(replay["receipt_choices"], completed["receipt_choices"])
        self.assertEqual(self.count_rows("review_receipts"), 1)

    def test_batch_receipt_approval_commits_all_items_once(self):
        before = {
            row["label"]: row["balance_minor"]
            for row in self.database.get_state()["accounts"]
        }
        pending = self.database.create_pending_batch_receipt(
            self.batch_draft(),
            raw_model_output="{}",
            validated_intent={"kind": "BENEFICIARY_TRANSFER_BATCH"},
            now=FIXED_NOW,
        )
        self.assertEqual(self.database.get_state()["transactions"], [])
        receipt = pending["batch_receipt"]
        assertion = self.sign_batch(receipt)
        executed = self.database.approve_batch_receipt(
            receipt["receipt_id"],
            receipt["receipt_digest"],
            assertion=assertion,
            context=self.device.context,
            voice_confirmation="yes, confirm",
            now=FIXED_NOW + timedelta(minutes=1),
        )
        replay = self.database.approve_batch_receipt(
            receipt["receipt_id"],
            receipt["receipt_digest"],
            assertion=assertion,
            context=self.device.context,
            voice_confirmation="yes, confirm",
            now=FIXED_NOW + timedelta(minutes=2),
        )
        after = {
            row["label"]: row["balance_minor"]
            for row in self.database.get_state()["accounts"]
        }
        self.assertEqual(after["savings"], before["savings"] - 1050)
        self.assertEqual(after["John Smith"], before["John Smith"] + 650)
        self.assertEqual(after["Alicia Powell"], before["Alicia Powell"] + 400)
        self.assertEqual(
            executed["batch_transaction_id"], replay["batch_transaction_id"]
        )
        transactions = self.database.get_state()["transactions"]
        self.assertEqual(len(transactions), 2)
        self.assertTrue(all("transaction_id" in row for row in transactions))

    def test_initialize_adds_batch_tables_without_deleting_single_history(self):
        self.approve(self.create_pending_alicia()["receipt_choices"][0])
        self.database.initialize()
        self.assertEqual(self.count_rows("ledger_transactions"), 1)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertTrue(
            {
                "batch_review_receipts",
                "batch_receipt_items",
                "batch_receipt_executions",
                "batch_ledger_transactions",
                "batch_ledger_entries",
            }.issubset(tables)
        )

    def test_batch_tampering_rolls_back_without_ledger_mutation(self):
        cases = (
            ("receipt_digest = '" + "0" * 64 + "'", "BATCH_RECEIPT_TAMPERED"),
            ("total_minor = 1051", "BATCH_RECEIPT_TAMPERED"),
            (
                "expires_at = '2020-01-01T00:00:00+00:00'",
                "BATCH_RECEIPT_TAMPERED",
            ),
        )
        for expression, reason in cases:
            with self.subTest(expression=expression):
                self.database.reset()
                pending = self.database.create_pending_batch_receipt(
                    self.batch_draft(),
                    raw_model_output="{}",
                    validated_intent={"kind": "BENEFICIARY_TRANSFER_BATCH"},
                    now=FIXED_NOW,
                )
                receipt = pending["batch_receipt"]
                # The user signs the genuine receipt; tampering happens after.
                assertion = self.sign_batch(receipt)
                before = self.database.get_state()
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    connection.execute(
                        f"UPDATE batch_review_receipts SET {expression} WHERE receipt_id = ?",
                        (receipt["receipt_id"],),
                    )
                with self.assertRaisesRegex(ReceiptRejected, reason):
                    self.database.approve_batch_receipt(
                        receipt["receipt_id"],
                        receipt["receipt_digest"],
                        assertion=assertion,
                        context=self.device.context,
                        voice_confirmation="yes, confirm",
                        now=FIXED_NOW + timedelta(minutes=1),
                    )
                after = self.database.get_state()
                self.assertEqual(after["transactions"], before["transactions"])
                self.assertEqual(after["ledger_entries"], before["ledger_entries"])

    def test_batch_destination_remap_rolls_back_without_ledger_mutation(self):
        pending = self.database.create_pending_batch_receipt(
            self.batch_draft(),
            raw_model_output="{}",
            validated_intent={"kind": "BENEFICIARY_TRANSFER_BATCH"},
            now=FIXED_NOW,
        )
        receipt = pending["batch_receipt"]
        assertion = self.sign_batch(receipt)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE beneficiaries SET destination_account_id = ? WHERE beneficiary_id = ?",
                ("acct_john_tan_01", "beneficiary_john_smith_01"),
            )
        with self.assertRaisesRegex(ReceiptRejected, "BATCH_RECEIPT_TAMPERED"):
            self.database.approve_batch_receipt(
                receipt["receipt_id"],
                receipt["receipt_digest"],
                assertion=assertion,
                context=self.device.context,
                voice_confirmation="yes, confirm",
                now=FIXED_NOW,
            )
        self.assertEqual(self.count_rows("batch_ledger_transactions"), 0)
        self.assertEqual(self.count_rows("batch_ledger_entries"), 0)

    def test_batch_balance_reduction_at_approval_rolls_back_whole_batch(self):
        pending = self.database.create_pending_batch_receipt(
            self.batch_draft(),
            raw_model_output="{}",
            validated_intent={"kind": "BENEFICIARY_TRANSFER_BATCH"},
            now=FIXED_NOW,
        )
        receipt = pending["batch_receipt"]
        assertion = self.sign_batch(receipt)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE accounts SET balance_minor = 500 WHERE account_id = 'acct_savings_01'"
            )
        with self.assertRaisesRegex(InsufficientFunds, "INSUFFICIENT_FUNDS"):
            self.database.approve_batch_receipt(
                receipt["receipt_id"],
                receipt["receipt_digest"],
                assertion=assertion,
                context=self.device.context,
                voice_confirmation="yes, confirm",
                now=FIXED_NOW,
            )
        self.assertEqual(self.count_rows("batch_ledger_transactions"), 0)
        self.assertEqual(self.count_rows("batch_ledger_entries"), 0)

    def test_two_threads_approve_batch_once(self):
        pending = self.database.create_pending_batch_receipt(
            self.batch_draft(),
            raw_model_output="{}",
            validated_intent={"kind": "BENEFICIARY_TRANSFER_BATCH"},
            now=FIXED_NOW,
        )
        receipt = pending["batch_receipt"]
        assertion = self.sign_batch(receipt)
        barrier = Barrier(2)

        def approve():
            contender = Database(
                self.db_path,
                clock=lambda: FIXED_NOW + timedelta(minutes=1),
            )
            barrier.wait()
            return contender.approve_batch_receipt(
                receipt["receipt_id"],
                receipt["receipt_digest"],
                assertion=assertion,
                context=self.device.context,
                voice_confirmation="yes, confirm",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _: approve(), range(2)))
        self.assertEqual(
            first["batch_transaction_id"], second["batch_transaction_id"]
        )
        self.assertEqual(self.count_rows("batch_ledger_transactions"), 1)
        self.assertEqual(self.count_rows("batch_ledger_entries"), 3)

    def test_two_threads_complete_one_clarification_once(self):
        pending = self.create_amount_clarification()
        clarification_id = pending["clarification"]["clarification_id"]
        barrier = Barrier(2)

        def complete():
            contender = Database(
                self.db_path,
                clock=lambda: FIXED_NOW + timedelta(minutes=1),
            )
            barrier.wait()
            return contender.complete_amount_clarification(
                clarification_id,
                "00000000-0000-4000-8000-000000000072",
                "ten dollars",
                "demo_user_01",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _: complete(), range(2)))

        self.assertEqual(
            first["receipt_choices"][0]["receipt_id"],
            second["receipt_choices"][0]["receipt_id"],
        )
        self.assertEqual(self.count_rows("review_receipts"), 1)
        self.assertEqual(self.count_rows("ledger_transactions"), 0)

    def test_amount_clarification_rejects_invalid_lifecycle_and_scope(self):
        pending = self.create_amount_clarification()
        clarification_id = pending["clarification"]["clarification_id"]
        response_id = "00000000-0000-4000-8000-000000000073"

        cases = (
            (
                "clarification_missing",
                response_id,
                "ten dollars",
                "demo_user_01",
                FIXED_NOW + timedelta(minutes=1),
                "CLARIFICATION_NOT_FOUND",
            ),
            (
                clarification_id,
                response_id,
                "ten dollars",
                "other_user",
                FIXED_NOW + timedelta(minutes=1),
                "CLARIFICATION_USER_MISMATCH",
            ),
            (
                clarification_id,
                response_id,
                "send Alicia ten dollars",
                "demo_user_01",
                FIXED_NOW + timedelta(minutes=1),
                "CLARIFICATION_SCOPE_VIOLATION",
            ),
        )
        for session_id, request_id, text, user_id, now, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ClarificationRejected, expected):
                    self.database.complete_amount_clarification(
                        session_id,
                        request_id,
                        text,
                        user_id,
                        now=now,
                    )
        self.assertEqual(self.count_rows("review_receipts"), 0)

        completed = self.database.complete_amount_clarification(
            clarification_id,
            response_id,
            "ten dollars",
            "demo_user_01",
            now=FIXED_NOW + timedelta(minutes=1),
        )
        with self.assertRaisesRegex(IdempotencyConflict, "IDEMPOTENCY_CONFLICT"):
            self.database.complete_amount_clarification(
                clarification_id,
                response_id,
                "twenty dollars",
                "demo_user_01",
                now=FIXED_NOW + timedelta(minutes=2),
            )
        self.assertEqual(completed["receipt_choices"][0]["amount_minor"], 1000)

    def test_amount_clarification_expires_at_exact_boundary(self):
        pending = self.create_amount_clarification()

        with self.assertRaisesRegex(ClarificationRejected, "CLARIFICATION_EXPIRED"):
            self.database.complete_amount_clarification(
                pending["clarification"]["clarification_id"],
                "00000000-0000-4000-8000-000000000074",
                "ten dollars",
                "demo_user_01",
                now=FIXED_NOW + timedelta(minutes=5),
            )

        self.assertEqual(
            self.scalar("SELECT status FROM clarification_sessions"), "EXPIRED"
        )
        self.assertEqual(self.count_rows("review_receipts"), 0)

    def test_initialize_additively_migrates_and_preserves_v1_receipt(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        legacy = Database(legacy_path)
        with legacy._connect() as connection:
            connection.executescript(SCHEMA)
            legacy._insert_seed(connection)
            draft = dataclasses.replace(self.receipt_draft(), contract_version=1)
            record = build_receipt_record(
                draft,
                now=FIXED_NOW,
                receipt_id="receipt_legacy",
                nonce="nonce_legacy",
            )
            pending = {
                "outcome": "CLARIFICATION_REQUEST",
                "request_id": draft.request_id,
            }
            connection.execute(
                """
                INSERT INTO action_attempts (
                  request_id, original_text, raw_model_output,
                  validated_intent_json, outcome, reason_code,
                  response_json, created_at
                ) VALUES (?, ?, '{}', '{}', ?, 'ACTION_CONFIRMATION_REQUIRED', ?, ?)
                """,
                (
                    draft.request_id,
                    draft.original_text,
                    pending["outcome"],
                    json.dumps(pending),
                    record["created_at"],
                ),
            )
            connection.execute(
                """
                INSERT INTO review_receipts (
                  receipt_id, clarification_group_id, request_id, user_id,
                  source_account_id, beneficiary_id, recipient_display_name,
                  amount_minor, currency, original_text, expires_at, nonce,
                  canonical_json, receipt_digest, status, created_at
                ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["receipt_id"], record["request_id"], record["user_id"],
                    record["source_account_id"], record["beneficiary_id"],
                    record["recipient_display_name"], record["amount_minor"],
                    record["currency"], record["original_text"], record["expires_at"],
                    record["nonce"], record["canonical_json"], record["receipt_digest"],
                    record["status"], record["created_at"],
                ),
            )

        legacy.initialize()

        with closing(sqlite3.connect(legacy_path)) as connection, connection:
            beneficiary_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(beneficiaries)")
            }
            receipt_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(review_receipts)")
            }
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("directory_kind", beneficiary_columns)
        self.assertTrue(
            {
                "contract_version",
                "destination_kind",
                "destination_reference",
                "lookup_reference",
                "evidence_bundle_json",
            }.issubset(receipt_columns)
        )
        self.assertTrue(
            {"clarification_sessions", "paynow_proxies", "paynow_lookups"}.issubset(
                tables
            )
        )
        self.device.enroll(legacy)
        challenge = legacy.issue_approval_challenge(
            "single",
            "receipt_legacy",
            record["receipt_digest"],
            rp_id=self.device.rp_id,
            now=FIXED_NOW,
        )
        response = legacy.approve_receipt(
            "receipt_legacy",
            record["receipt_digest"],
            "demo_user_01",
            assertion=self.device.sign(challenge),
            context=self.device.context,
            voice_confirmation="yes, confirm",
            now=FIXED_NOW + timedelta(minutes=1),
        )
        self.assertEqual(response["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")

    def scalar(self, sql):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            return connection.execute(sql).fetchone()[0]

    def blocked_attempt(self):
        response = {
            "outcome": "CLARIFICATION_REQUEST",
            "request_id": "00000000-0000-4000-8000-000000000002",
            "candidate_display_names": ["Alicia Powell"],
        }
        return ActionAttempt(
            request_id=response["request_id"],
            original_text="Send Alicia S$50",
            raw_model_output="{}",
            validated_intent={"recipient_label": "Alicia"},
            outcome=response["outcome"],
            reason_code="AMBIGUOUS_RECIPIENT",
            response=response,
        )

    def test_pending_receipt_is_stored_with_attempt_and_without_transaction(self):
        response = self.create_pending_alicia()

        choice = response["receipt_choices"][0]
        stored = self.database.get_receipt(choice["receipt_id"])
        self.assertEqual(stored["beneficiary_id"], "beneficiary_alicia_powell_01")
        self.assertEqual(stored["amount_minor"], 500)
        self.assertEqual(stored["status"], "AWAITING_CONFIRMATION")
        self.assertEqual(choice["receipt_digest"], stored["receipt_digest"])
        self.assertEqual(choice["source_label"], "savings")
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_utility_bill_receipt_uses_auth_tier_and_posts_balanced_payment_once(self):
        before = {row["account_id"]: row["balance_minor"] for row in self.database.get_state()["accounts"]}
        draft = self.utility_bill_draft()
        pending = self.database.create_pending_receipts(
            (draft,),
            raw_model_output="{}",
            validated_intent={"kind": "UTILITY_BILL_PAYMENT"},
            reason_code="BILL_PAYMENT_CONFIRMATION_REQUIRED",
            response_base={"outcome": "CLARIFICATION_REQUEST", "question": "Review bill", "trace": []},
            now=FIXED_NOW,
        )
        choice = pending["receipt_choices"][0]
        policy = self.database.policy_for_receipt("single", choice["receipt_id"])
        self.assertFalse(policy["requires_singpass"])
        self.assertTrue(policy["requires_voice_confirmation"])
        self.assertEqual(self.count_rows("ledger_transactions"), 0)

        challenge = self.database.issue_approval_challenge(
            "single", choice["receipt_id"], choice["receipt_digest"],
            rp_id=self.device.rp_id, now=FIXED_NOW,
        )
        execution = self.database.approve_receipt(
            choice["receipt_id"], choice["receipt_digest"], "demo_user_01",
            assertion=self.device.sign(challenge), context=self.device.context,
            voice_confirmation="yes, confirm", now=FIXED_NOW + timedelta(minutes=1),
        )

        self.assertEqual(execution["outcome"], "EXECUTED_SYNTHETIC_BILL_PAYMENT")
        self.assertEqual(execution["resolved_action"]["balance_before_minor"], 200_000)
        self.assertEqual(execution["resolved_action"]["balance_after_minor"], 188_000)
        self.assertEqual(self.database.get_receipt(choice["receipt_id"])["status"], "EXECUTED")
        state = self.database.get_state()
        self.assertEqual(len(state["transactions"]), 1)
        self.assertEqual(state["transactions"][0]["kind"], "UTILITY_BILL_PAYMENT")
        self.assertEqual(state["transactions"][0]["biller_display_name"], "SP Services")
        after = {row["account_id"]: row["balance_minor"] for row in state["accounts"]}
        self.assertEqual(after["acct_savings_01"], before["acct_savings_01"] - 12_000)
        self.assertEqual(after["acct_biller_sp_services"], before["acct_biller_sp_services"] + 12_000)

    def test_utility_bill_at_two_hundred_uses_singpass_face_tier(self):
        draft = dataclasses.replace(
            self.utility_bill_draft(),
            request_id="00000000-0000-4000-8000-000000000043",
            original_text="Pay my Singapore Power bill $200 from savings.",
            amount_minor=20_000,
            evidence_bundle=(
                {"turn": 1, "text": "Pay my Singapore Power bill $200 from savings."},
                {"kind": "UTILITY_BILL_PAYMENT", "biller_evidence": "Singapore Power", "amount_evidence": "$200"},
            ),
        )
        pending = self.database.create_pending_receipts(
            (draft,), raw_model_output="{}", validated_intent={"kind": "UTILITY_BILL_PAYMENT"},
            reason_code="BILL_PAYMENT_CONFIRMATION_REQUIRED",
            response_base={"outcome": "CLARIFICATION_REQUEST", "trace": []}, now=FIXED_NOW,
        )
        policy = self.database.policy_for_receipt("single", pending["receipt_choices"][0]["receipt_id"])
        self.assertTrue(policy["requires_singpass"])
        self.assertTrue(policy["requires_face"])
        self.assertFalse(policy["requires_voice_confirmation"])
        challenge = self.database.issue_approval_challenge(
            "single", pending["receipt_choices"][0]["receipt_id"],
            pending["receipt_choices"][0]["receipt_digest"],
            rp_id=self.device.rp_id, now=FIXED_NOW,
        )
        self.assertTrue(challenge["review"]["requires_read_back"])
        self.assertEqual(
            challenge["review"]["read_back"]["text"],
            "Pay S$200.00 to SP Services from savings.",
        )

    def test_utility_bill_receipt_rejects_unbound_destination_account(self):
        draft = dataclasses.replace(self.utility_bill_draft(), destination_account_id="acct_alicia_powell_01")
        with self.assertRaisesRegex(ResolutionError, "server-owned biller resolution failed"):
            self.database.create_pending_receipts(
                (draft,), raw_model_output="{}", validated_intent={},
                reason_code="BILL_PAYMENT_CONFIRMATION_REQUIRED",
                response_base={"outcome": "CLARIFICATION_REQUEST", "trace": []}, now=FIXED_NOW,
            )

    def test_ambiguous_candidates_share_group_but_have_distinct_receipts(self):
        choices = self.create_pending_johns()["receipt_choices"]

        self.assertEqual(
            [choice["recipient_display_name"] for choice in choices],
            ["John Smith", "John Tan"],
        )
        self.assertNotEqual(choices[0]["receipt_id"], choices[1]["receipt_id"])
        groups = {
            self.database.get_receipt(choice["receipt_id"])[
                "clarification_group_id"
            ]
            for choice in choices
        }
        self.assertEqual(len(groups), 1)
        self.assertNotIn(None, groups)

    def test_pending_action_retry_returns_same_receipts(self):
        first = self.create_pending_alicia()
        second = self.create_pending_alicia()

        self.assertEqual(second, first)
        self.assertEqual(self.count_rows("review_receipts"), 1)

    def test_pending_request_id_with_changed_text_conflicts(self):
        self.create_pending_alicia()
        changed = dataclasses.replace(
            self.receipt_draft(),
            original_text="Send Alicia Powell fifty dollars",
        )

        with self.assertRaisesRegex(IdempotencyConflict, "IDEMPOTENCY_CONFLICT"):
            self.database.create_pending_receipts(
                (changed,),
                raw_model_output="{}",
                validated_intent={},
                reason_code="ACTION_CONFIRMATION_REQUIRED",
                response_base={"outcome": "CLARIFICATION_REQUEST", "trace": []},
                now=FIXED_NOW,
            )

    def test_reset_clears_all_receipt_records(self):
        self.create_pending_alicia()

        self.database.reset()

        for table in (
            "receipt_executions",
            "authorization_records",
            "review_receipts",
        ):
            with self.subTest(table=table):
                self.assertEqual(self.count_rows(table), 0)

    def sign(self, choice, *, now=FIXED_NOW):
        challenge = self.database.issue_approval_challenge(
            "single",
            choice["receipt_id"],
            choice["receipt_digest"],
            rp_id=self.device.rp_id,
            now=now,
        )
        return self.device.sign(challenge)

    def sign_batch(self, receipt, *, now=FIXED_NOW):
        challenge = self.database.issue_approval_challenge(
            "batch",
            receipt["receipt_id"],
            receipt["receipt_digest"],
            rp_id=self.device.rp_id,
            now=now,
        )
        return self.device.sign(challenge)

    def approve(self, choice, *, now=FIXED_NOW, user_id="demo_user_01", assertion=None):
        if assertion is None:
            assertion = self.sign(choice, now=now)
        return self.database.approve_receipt(
            choice["receipt_id"],
            choice["receipt_digest"],
            user_id,
            assertion=assertion,
            context=self.device.context,
            voice_confirmation="yes, confirm",
            now=now,
        )

    def test_receipt_never_executes_without_a_device_signature(self):
        choice = self.create_pending_alicia()["receipt_choices"][0]

        for context in (None, self.device.context):
            with self.subTest(context=context):
                with self.assertRaisesRegex(ReceiptRejected, "SIGNED_APPROVAL_REQUIRED"):
                    self.database.approve_receipt(
                        choice["receipt_id"],
                        choice["receipt_digest"],
                        "demo_user_01",
                        assertion=None,
                        context=context,
                        voice_confirmation="yes, confirm",
                        now=FIXED_NOW,
                    )

        self.assertEqual(self.count_rows("authorization_records"), 0)
        self.assertEqual(self.count_rows("ledger_transactions"), 0)
        self.assertEqual(
            self.database.get_receipt(choice["receipt_id"])["status"],
            "AWAITING_CONFIRMATION",
        )

    def test_approval_stores_verifiable_signature_evidence(self):
        choice = self.create_pending_alicia()["receipt_choices"][0]

        result = self.approve(choice)

        self.assertEqual(result["approval"]["credential_kind"], "WEBAUTHN_PLATFORM")
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            stored = connection.execute(
                "SELECT receipt_id, receipt_digest, credential_id FROM approval_signatures"
            ).fetchall()
        self.assertEqual(
            stored,
            [(choice["receipt_id"], choice["receipt_digest"], self.device.credential_id)],
        )

    def test_approve_pending_receipt_executes_stored_facts(self):
        choice = self.create_pending_alicia()["receipt_choices"][0]

        result = self.approve(choice)

        self.assertEqual(
            result["resolved_action"]["beneficiary_id"],
            "beneficiary_alicia_powell_01",
        )
        self.assertEqual(result["amount_minor"], 500)
        self.assertEqual(
            self.scalar("SELECT method FROM authorization_records"),
            "USER_CONFIRMATION",
        )
        self.assertEqual(self.database.get_state()["accounts"][0]["balance_minor"], 199500)

    def test_changed_digest_is_rejected_without_writes(self):
        choice = self.create_pending_alicia()["receipt_choices"][0]
        before = self.database.get_state()

        with self.assertRaisesRegex(ReceiptRejected, "APPROVAL_RECEIPT_MISMATCH"):
            self.database.approve_receipt(
                choice["receipt_id"],
                "0" * 64,
                "demo_user_01",
                assertion=self.sign(choice),
                context=self.device.context,
                voice_confirmation="yes, confirm",
                now=FIXED_NOW,
            )

        self.assertEqual(self.database.get_state(), before)
        self.assertEqual(self.count_rows("authorization_records"), 0)

    def test_identical_repeated_approval_returns_one_transaction(self):
        choice = self.create_pending_alicia()["receipt_choices"][0]

        assertion = self.sign(choice)
        first = self.approve(choice, assertion=assertion)
        second = self.approve(choice, assertion=assertion)

        self.assertEqual(second["transaction_id"], first["transaction_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(self.count_rows("ledger_transactions"), 1)
        with self.assertRaisesRegex(ReceiptRejected, "RECEIPT_ALREADY_EXECUTED"):
            self.sign(choice)

    def test_accepted_signature_cannot_approve_a_different_receipt(self):
        johns = self.create_pending_johns()["receipt_choices"]
        assertion = self.sign(johns[0])
        self.approve(johns[0], assertion=assertion)

        with self.assertRaisesRegex(ReceiptRejected, "APPROVAL_CHALLENGE_REPLAYED"):
            self.approve(johns[1], assertion=assertion)

        self.assertEqual(self.count_rows("ledger_transactions"), 1)

    def test_approval_at_expiry_marks_receipt_expired_without_ledger_mutation(self):
        choice = self.create_pending_alicia()["receipt_choices"][0]

        assertion = self.sign(choice, now=FIXED_NOW + timedelta(minutes=4))
        with self.assertRaisesRegex(ReceiptRejected, "RECEIPT_EXPIRED"):
            self.approve(
                choice, now=FIXED_NOW + timedelta(minutes=5), assertion=assertion
            )

        self.assertEqual(
            self.database.get_receipt(choice["receipt_id"])["status"],
            "EXPIRED",
        )
        self.assertEqual(self.count_rows("ledger_transactions"), 0)

    def test_production_clock_is_read_after_waiting_for_write_lock(self):
        choice = self.create_pending_alicia()["receipt_choices"][0]
        clock_called = Event()

        def expired_clock():
            clock_called.set()
            return FIXED_NOW + timedelta(minutes=5)

        assertion = self.sign(choice, now=FIXED_NOW + timedelta(minutes=4))
        controlled = Database(self.db_path, clock=expired_clock)
        blocker = sqlite3.connect(
            self.db_path,
            isolation_level=None,
            check_same_thread=False,
        )
        blocker.execute("BEGIN IMMEDIATE")

        def approve_after_lock():
            return controlled.approve_receipt(
                choice["receipt_id"],
                choice["receipt_digest"],
                "demo_user_01",
                assertion=assertion,
                context=self.device.context,
                voice_confirmation="yes, confirm",
            )

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(approve_after_lock)
                self.assertFalse(clock_called.wait(timeout=0.2))
                blocker.rollback()
                with self.assertRaisesRegex(ReceiptRejected, "RECEIPT_EXPIRED"):
                    future.result(timeout=2)
        finally:
            blocker.close()

        self.assertTrue(clock_called.is_set())
        self.assertEqual(
            controlled.get_receipt(choice["receipt_id"])["status"],
            "EXPIRED",
        )
        self.assertEqual(self.count_rows("ledger_transactions"), 0)

    def test_selecting_one_ambiguous_candidate_supersedes_sibling(self):
        choices = self.create_pending_johns()["receipt_choices"]

        selected = self.approve(choices[1])

        self.assertEqual(
            selected["resolved_action"]["beneficiary_id"],
            "beneficiary_john_tan_01",
        )
        self.assertEqual(
            self.database.get_receipt(choices[0]["receipt_id"])["status"],
            "SUPERSEDED",
        )
        with self.assertRaisesRegex(ReceiptRejected, "RECEIPT_SUPERSEDED"):
            self.approve(choices[0])

    def test_wrong_user_and_missing_receipt_are_non_mutating(self):
        choice = self.create_pending_alicia()["receipt_choices"][0]

        with self.assertRaisesRegex(ReceiptRejected, "APPROVAL_USER_MISMATCH"):
            self.approve(choice, user_id="other_user", assertion=self.sign(choice))
        with self.assertRaisesRegex(ReceiptRejected, "RECEIPT_NOT_FOUND"):
            self.database.approve_receipt(
                "receipt_missing",
                "0" * 64,
                "demo_user_01",
                assertion=None,
                context=None,
                now=FIXED_NOW,
            )

        self.assertEqual(self.count_rows("ledger_transactions"), 0)
        self.assertEqual(self.count_rows("authorization_records"), 0)

    def test_canonical_json_or_bound_column_tampering_is_rejected(self):
        assignments = (
            ("canonical_json = ?", ("{}",)),
            ("amount_minor = ?", (501,)),
        )
        for assignment, values in assignments:
            with self.subTest(assignment=assignment):
                self.database.reset()
                choice = self.create_pending_alicia()["receipt_choices"][0]
                assertion = self.sign(choice)
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    connection.execute(
                        f"UPDATE review_receipts SET {assignment} WHERE receipt_id = ?",
                        (*values, choice["receipt_id"]),
                    )
                with self.assertRaisesRegex(
                    ReceiptRejected,
                    "RECEIPT_DIGEST_MISMATCH",
                ):
                    self.approve(choice, assertion=assertion)
                self.assertEqual(self.count_rows("ledger_transactions"), 0)

    def test_balance_is_rechecked_at_approval_time(self):
        choice = self.create_pending_alicia()["receipt_choices"][0]
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """
                UPDATE accounts SET balance_minor = 0
                WHERE account_id = 'acct_savings_01'
                """
            )

        with self.assertRaisesRegex(InsufficientFunds, "INSUFFICIENT_FUNDS"):
            self.approve(choice)

        self.assertEqual(self.count_rows("authorization_records"), 0)
        self.assertEqual(
            self.database.get_receipt(choice["receipt_id"])["status"],
            "AWAITING_CONFIRMATION",
        )

    def test_entry_failure_rolls_back_authorization_balance_and_receipt(self):
        choice = self.create_pending_alicia()["receipt_choices"][0]
        before = self.database.get_state()["accounts"]
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.executescript(
                """
                CREATE TRIGGER force_receipt_entry_failure
                BEFORE INSERT ON ledger_entries
                BEGIN SELECT RAISE(ABORT, 'forced receipt entry failure'); END;
                """
            )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "forced receipt entry failure",
        ):
            self.approve(choice)

        self.assertEqual(self.database.get_state()["accounts"], before)
        self.assertEqual(self.count_rows("authorization_records"), 0)
        self.assertEqual(self.count_rows("receipt_executions"), 0)
        self.assertEqual(
            self.database.get_receipt(choice["receipt_id"])["status"],
            "AWAITING_CONFIRMATION",
        )

    def test_executed_receipt_replays_after_expiry(self):
        choice = self.create_pending_alicia()["receipt_choices"][0]
        assertion = self.sign(choice)
        first = self.approve(choice, assertion=assertion)

        replay = self.approve(
            choice, now=FIXED_NOW + timedelta(minutes=10), assertion=assertion
        )

        self.assertEqual(replay["transaction_id"], first["transaction_id"])
        self.assertTrue(replay["idempotent_replay"])

    def test_invalid_executed_state_without_link_is_rejected(self):
        choice = self.create_pending_alicia()["receipt_choices"][0]
        assertion = self.sign(choice)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """
                UPDATE review_receipts SET status = 'EXECUTED'
                WHERE receipt_id = ?
                """,
                (choice["receipt_id"],),
            )

        with self.assertRaisesRegex(ReceiptRejected, "RECEIPT_INVALID_STATUS"):
            self.approve(choice, assertion=assertion)

        self.assertEqual(self.count_rows("ledger_transactions"), 0)

    def test_two_threads_approving_one_receipt_create_one_transaction(self):
        choice = self.create_pending_alicia()["receipt_choices"][0]
        assertion = self.sign(choice)
        barrier = Barrier(2)

        def execute():
            barrier.wait()
            return self.approve(choice, assertion=assertion)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (pool.submit(execute), pool.submit(execute))
            results = [future.result() for future in futures]

        self.assertEqual(results[0]["transaction_id"], results[1]["transaction_id"])
        self.assertEqual(self.count_rows("ledger_transactions"), 1)
        self.assertEqual(self.database.get_state()["accounts"][0]["balance_minor"], 199500)

    def test_initialize_and_reset_restore_seed_balances(self):
        self.assertEqual(
            [
                (row["label"], row["balance_minor"])
                for row in self.database.get_state()["accounts"]
            ],
            [
                ("savings", 200000),
                ("Alicia Powell", 7500),
                ("DBS Credit Card settlement", 0),
                ("IRAS settlement", 0),
                ("John Smith", 10000),
                ("John Tan", 7500),
                ("JXXX SXXXX", 0),
                ("Lim Siew Hua", 20000),
                ("M1 settlement", 0),
                ("MXXX LXX", 0),
                ("PUB Water settlement", 0),
                ("Singtel settlement", 0),
                ("SP Services settlement", 0),
                ("StarHub settlement", 0),
            ],
        )
        self.approve(self.create_pending_alicia()["receipt_choices"][0])

        reset_state = self.database.reset()

        self.assertEqual(
            [
                (row["label"], row["balance_minor"])
                for row in reset_state["accounts"]
            ],
            [
                ("savings", 200000),
                ("Alicia Powell", 7500),
                ("DBS Credit Card settlement", 0),
                ("IRAS settlement", 0),
                ("John Smith", 10000),
                ("John Tan", 7500),
                ("JXXX SXXXX", 0),
                ("Lim Siew Hua", 20000),
                ("M1 settlement", 0),
                ("MXXX LXX", 0),
                ("PUB Water settlement", 0),
                ("Singtel settlement", 0),
                ("SP Services settlement", 0),
                ("StarHub settlement", 0),
            ],
        )
        self.assertEqual(reset_state["transactions"], [])

    def test_transfer_creates_equal_debit_and_credit(self):
        result = self.approve(self.create_pending_alicia()["receipt_choices"][0])
        state = self.database.get_state()

        self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
        balances = {row["label"]: row["balance_minor"] for row in state["accounts"]}
        self.assertEqual(balances["savings"], 199500)
        self.assertEqual(balances["Alicia Powell"], 8000)
        self.assertEqual(balances["John Smith"], 10000)
        self.assertEqual(balances["John Tan"], 7500)
        self.assertEqual(
            [row["delta_minor"] for row in state["ledger_entries"]],
            [-500, 500],
        )
        self.assertEqual(state["transactions"][0]["source_label"], "savings")
        self.assertEqual(
            state["transactions"][0]["destination_label"],
            "Alicia Powell",
        )

    def test_exact_resolution_normalizes_case_and_whitespace(self):
        result = self.database.resolve_beneficiary("  JOHN SMITH ")

        self.assertEqual(result.kind, "exact")
        self.assertEqual(
            result.matches[0]["beneficiary_id"],
            "beneficiary_john_smith_01",
        )

    def test_alicia_powell_is_an_exact_saved_beneficiary(self):
        result = self.database.resolve_beneficiary("  ALICIA POWELL ")

        self.assertEqual(result.kind, "exact")
        self.assertEqual(
            result.matches[0]["beneficiary_id"],
            "beneficiary_alicia_powell_01",
        )

    def test_misspelled_saved_names_are_close_matches_not_exact_matches(self):
        cases = (
            ("Alicia Powel", "Alicia Powell"),
            ("Jhon Smith", "John Smith"),
            ("Jon Smith", "John Smith"),
        )

        for spoken_name, expected_name in cases:
            with self.subTest(spoken_name=spoken_name):
                result = self.database.resolve_beneficiary(spoken_name)
                self.assertEqual(result.kind, "close")
                self.assertEqual(result.matches[0]["display_name"], expected_name)

    def test_transposed_john_tan_is_a_close_match(self):
        result = self.database.resolve_beneficiary("Jhon Tan")

        self.assertEqual(result.kind, "close")
        self.assertEqual(result.matches[0]["display_name"], "John Tan")

    def test_prefix_recipient_is_never_returned_as_an_exact_match(self):
        result = self.database.resolve_beneficiary("John")

        self.assertEqual(result.kind, "candidates")
        self.assertEqual(
            [row["display_name"] for row in result.matches],
            ["John Smith", "John Tan"],
        )

    def test_blocked_attempt_can_be_recorded_without_a_ledger_transaction(self):
        stored = self.database.record_attempt(self.blocked_attempt())

        self.assertEqual(stored["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_reset_waits_for_a_held_write_transaction_then_restores_seed(self):
        blocker = sqlite3.connect(
            self.db_path,
            isolation_level=None,
            check_same_thread=False,
        )
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.database.reset)
                self.assertFalse(future.done())
                blocker.rollback()
                state = future.result(timeout=2)
        finally:
            blocker.close()

        self.assertEqual(state["accounts"][0]["balance_minor"], 200000)


if __name__ == "__main__":
    unittest.main()

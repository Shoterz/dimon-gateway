import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from apps.dimon_live.database import Database, ReceiptRejected
from apps.dimon_live.tests.device_signer import SoftwareAuthenticator
from apps.dimon_live.model_adapter import (
    ExtractedIntent,
    InvalidModelOutput,
    ModelUnavailable,
)


REQUEST_ID = "00000000-0000-4000-8000-000000000010"
GOLDEN_REQUEST = "Send John Smith S$50 from savings."
GOLDEN_INTENT = {
    "contract_version": 2,
    "kind": "BENEFICIARY_TRANSFER",
    "source_account_ref": "savings",
    "source_evidence": "savings",
    "recipient_label": "John Smith",
    "recipient_evidence": "John Smith",
    "amount_minor": 5000,
    "amount_evidence": "S$50",
    "currency": "SGD",
    "payment_reference": None,
    "reference_evidence": None,
    "requested_execution_date": None,
}

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
PAYNOW_INTENT = {
    "contract_version": 2,
    "kind": "PAYNOW_MOBILE_TRANSFER",
    "source_account_ref": None,
    "source_evidence": None,
    "mobile_proxy": "86952933",
    "proxy_evidence": "8695 2933",
    "amount_minor": 1000,
    "amount_evidence": "ten dollars",
    "currency": "SGD",
    "payment_reference": "flowers",
    "reference_evidence": "for flowers",
    "requested_execution_date": None,
}
BATCH_INTENT = {
    "contract_version": 4,
    "kind": "BENEFICIARY_TRANSFER_BATCH",
    "source_account_ref": None,
    "source_evidence": None,
    "items": [
        {"recipient_label": "John Smith", "recipient_evidence": "John Smith"},
        {"recipient_label": "Alicia Powell", "recipient_evidence": "Alicia Powell"},
    ],
    "currency": "SGD",
    "requested_execution_date": None,
}


class StaticExtractor:
    def __init__(self, intent):
        self.intent = intent

    def extract(self, text):
        raw = json.dumps(self.intent)
        return ExtractedIntent(dict(self.intent), raw, 12.5)


class RecordingExtractor(StaticExtractor):
    def __init__(self, intent):
        super().__init__(intent)
        self.calls = []

    def extract(self, text):
        self.calls.append(text)
        return super().extract(text)


class RaisingExtractor:
    def __init__(self, error):
        self.error = error

    def extract(self, text):
        raise self.error


class ActionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "demo.sqlite3")
        self.database.initialize()
        self.device = SoftwareAuthenticator()
        self.device.enroll(self.database)

    def tearDown(self):
        self.temp_dir.cleanup()

    def sign(self, choice):
        challenge = self.database.issue_approval_challenge(
            "single",
            choice["receipt_id"],
            choice["receipt_digest"],
            rp_id=self.device.rp_id,
        )
        return self.device.sign(challenge)

    def signed_approve(self, service, choice, *, assertion=None, user_id="demo_user_01"):
        return service.approve(
            choice["receipt_id"],
            choice["receipt_digest"],
            assertion=assertion or self.sign(choice),
            context=self.device.context,
            user_id=user_id,
        )

    def service_for(self, intent):
        from apps.dimon_live.service import ActionService

        return ActionService(self.database, StaticExtractor(intent))

    def balances(self):
        return [
            row["balance_minor"] for row in self.database.get_state()["accounts"]
        ]

    def receipt_count(self):
        with closing(sqlite3.connect(self.database.path)) as connection, connection:
            return connection.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0]

    def batch_receipt_count(self):
        with closing(sqlite3.connect(self.database.path)) as connection, connection:
            return connection.execute(
                "SELECT COUNT(*) FROM batch_review_receipts"
            ).fetchone()[0]

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
            [
                (item["recipient_display_name"], item["amount_minor"])
                for item in result["batch_receipt"]["items"]
            ],
            [("John Smith", 650), ("Alicia Powell", 400)],
        )
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.database.get_state()["transactions"], [])
        self.assertEqual(self.batch_receipt_count(), 1)

    def test_batch_unknown_duplicate_and_count_mismatch_never_create_receipt(self):
        cases = (
            (
                {
                    **BATCH_INTENT,
                    "items": [BATCH_INTENT["items"][0], BATCH_INTENT["items"][0]],
                },
                "Send John Smith $5 and John Smith $7",
                "AMBIGUOUS_BATCH_BINDING",
            ),
            (
                BATCH_INTENT,
                "Send John Smith and Alicia Powell $4",
                "BATCH_RECIPIENT_COUNT_MISMATCH",
            ),
            (
                {
                    **BATCH_INTENT,
                    "items": [
                        BATCH_INTENT["items"][0],
                        {"recipient_label": "Nobody", "recipient_evidence": "Nobody"},
                    ],
                },
                "Send John Smith $5 and Nobody $7",
                "UNKNOWN_RECIPIENT",
            ),
            (
                BATCH_INTENT,
                "PayNow John Smith $5 and Alicia Powell $7 using 86952933",
                "AMBIGUOUS_BATCH_BINDING",
            ),
            (
                BATCH_INTENT,
                "Send John Smith $5 and Alicia Powell $7 if they call",
                "AMBIGUOUS_BATCH_BINDING",
            ),
            (
                BATCH_INTENT,
                "Do not send John Smith $5 and Alicia Powell $7",
                "NEGATED_ACTION",
            ),
        )
        for index, (intent, text, reason) in enumerate(cases, start=141):
            with self.subTest(reason=reason):
                self.database.reset()
                before = self.balances()
                result = self.service_for(intent).process(
                    f"00000000-0000-4000-8000-{index:012d}", text
                )
                self.assertEqual(result["outcome"], "BLOCKED")
                self.assertEqual(result["reason_code"], reason)
                self.assertEqual(self.batch_receipt_count(), 0)
                self.assertEqual(self.balances(), before)
                self.assertEqual(self.database.get_state()["transactions"], [])

    def test_batch_unsupported_language_never_creates_a_receipt(self):
        cases = (
            "Send John Smith and Alicia Powell $5 each, total $10",
            "Send John Smith 50% of $10 and Alicia Powell $7",
            "Send John Smith $5 and Alicia Powell $7 tomorrow",
            "Send John Smith $5 but not Alicia Powell $7",
            "Pay now John Smith $5 to 86952933 and Alicia Powell $7",
            "Send John Smith $5 from savings and Alicia Powell $7 from current",
        )
        for index, text in enumerate(cases, start=151):
            with self.subTest(text=text):
                self.database.reset()
                before = self.balances()
                result = self.service_for(BATCH_INTENT).process(
                    f"00000000-0000-4000-8000-{index:012d}", text
                )
                self.assertEqual(result["outcome"], "BLOCKED")
                self.assertEqual(result["reason_code"], "AMBIGUOUS_BATCH_BINDING")
                self.assertEqual(self.batch_receipt_count(), 0)
                self.assertEqual(self.balances(), before)

    def test_batch_recipient_evidence_cannot_narrow_an_unknown_name(self):
        intent = {
            **BATCH_INTENT,
            "items": [
                {
                    "recipient_label": "John Smith",
                    "recipient_evidence": "John Smithson",
                },
                BATCH_INTENT["items"][1],
            ],
        }
        before = self.balances()
        result = self.service_for(intent).process(
            "00000000-0000-4000-8000-000000000157",
            "Send John Smithson $5 and Alicia Powell $7",
        )
        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertEqual(result["reason_code"], "AMBIGUOUS_BATCH_BINDING")
        self.assertEqual(self.batch_receipt_count(), 0)
        self.assertEqual(self.balances(), before)

    def test_batch_model_item_order_cannot_change_text_order_binding(self):
        intent = {**BATCH_INTENT, "items": list(reversed(BATCH_INTENT["items"]))}
        result = self.service_for(intent).process(
            "00000000-0000-4000-8000-000000000158",
            "Send John Smith $6.50 and Alicia Powell $4",
        )
        self.assertEqual(result["outcome"], "BATCH_CONFIRMATION_REQUEST")
        self.assertEqual(
            [
                (item["recipient_display_name"], item["amount_minor"])
                for item in result["batch_receipt"]["items"]
            ],
            [("John Smith", 650), ("Alicia Powell", 400)],
        )

    def test_exact_batch_text_cannot_be_collapsed_to_one_model_recipient(self):
        single_recipient_proposal = {
            **GOLDEN_INTENT,
            "contract_version": 3,
            "source_account_ref": None,
            "source_evidence": None,
            "amount_minor": None,
            "amount_evidence": None,
        }
        result = self.service_for(single_recipient_proposal).process(
            "00000000-0000-4000-8000-000000000159",
            "Send John Smith S$6.50 and Alicia Powell S$4",
        )
        self.assertEqual(result["outcome"], "BATCH_CONFIRMATION_REQUEST")
        self.assertIn(
            "BATCH_RECIPIENTS_RECOVERED_FROM_DIRECTORY",
            [step["stage"] for step in result["trace"]],
        )
        self.assertEqual(
            [
                (item["recipient_display_name"], item["amount_minor"])
                for item in result["batch_receipt"]["items"]
            ],
            [("John Smith", 650), ("Alicia Powell", 400)],
        )

    def test_non_command_name_list_is_not_promoted_to_a_batch(self):
        single_recipient_proposal = {
            **GOLDEN_INTENT,
            "contract_version": 3,
            "source_account_ref": None,
            "source_evidence": None,
            "amount_minor": None,
            "amount_evidence": None,
        }
        result = self.service_for(single_recipient_proposal).process(
            "00000000-0000-4000-8000-000000000160",
            "Compare John Smith S$6.50 and Alicia Powell S$4",
        )
        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertEqual(result["reason_code"], "AMBIGUOUS_AMOUNT")
        self.assertEqual(self.batch_receipt_count(), 0)

    def test_exact_transfer_needs_signature_then_returns_truthful_trace(self):
        service = self.service_for(GOLDEN_INTENT)
        pending = service.process(REQUEST_ID, GOLDEN_REQUEST)

        # A fully specified command is still only a draft.
        self.assertEqual(pending["outcome"], "CLARIFICATION_REQUEST")
        stages = [step["stage"] for step in pending["trace"]]
        self.assertEqual(
            stages[-3:],
            ["SIGNED_APPROVAL_REQUIRED", "VALIDATION_PASSED", "RISK_POLICY_PASSED"],
        )
        self.assertEqual(pending["receipt_choices"][0]["validation"]["verdict"], "PASS")
        self.assertEqual(self.database.get_state()["transactions"], [])

        result = self.signed_approve(service, pending["receipt_choices"][0])

        self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
        self.assertIn("DEVICE_SIGNATURE_VERIFIED", [step["stage"] for step in result["trace"]])
        self.assertEqual(
            result["resolved_action"]["source_account_id"], "acct_savings_01"
        )
        self.assertEqual(
            result["resolved_action"]["beneficiary_id"],
            "beneficiary_john_smith_01",
        )
        self.assertEqual(
            result["resolved_action"]["destination_account_id"],
            "acct_john_smith_01",
        )
        self.assertEqual(result["trace"][-1]["stage"], "SQLITE_COMMITTED")
        balances = {
            row["label"]: row["balance_minor"]
            for row in result["state"]["accounts"]
        }
        self.assertEqual(balances["savings"], 95000)
        self.assertEqual(balances["John Smith"], 15000)
        self.assertEqual(balances["Alicia Powell"], 7500)
        self.assertEqual(balances["John Tan"], 7500)

    def test_conflicting_amounts_never_execute_when_model_cites_only_one(self):
        before = self.balances()
        intent = {
            **GOLDEN_INTENT,
            "recipient_label": "Alicia Powell",
            "recipient_evidence": "Alicia Powell",
            "amount_minor": 1000,
            "amount_evidence": "$10",
            "source_account_ref": None,
            "source_evidence": None,
        }

        result = self.service_for(intent).process(
            REQUEST_ID,
            "Send Alicia Powell $10 or $100",
        )

        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertEqual(result["reason_code"], "AMBIGUOUS_AMOUNT")
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_python_derives_ten_dollars_when_model_returns_ten_cents(self):
        wrong_model_amount = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "recipient_label": "Alicia Powell",
            "recipient_evidence": "Alicia Powell",
            "amount_minor": 10,
            "amount_evidence": "10",
        }

        service = self.service_for(wrong_model_amount)
        pending = service.process(
            REQUEST_ID,
            "Send Alicia Powell $10",
        )
        self.assertEqual(pending["receipt_choices"][0]["amount_minor"], 1000)
        result = self.signed_approve(service, pending["receipt_choices"][0])

        self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
        self.assertEqual(result["amount_minor"], 1000)
        self.assertEqual(result["validated_intent"]["amount_minor"], 1000)
        self.assertEqual(result["validated_intent"]["amount_evidence"], "$10")
        self.assertEqual(len(self.database.get_state()["transactions"]), 1)

    def test_competing_exact_recipient_mentions_never_auto_execute(self):
        before = self.balances()
        selected_john = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "amount_minor": 1000,
            "amount_evidence": "$10",
        }

        result = self.service_for(selected_john).process(
            REQUEST_ID,
            "Send Alicia Powell or John Smith $10",
        )

        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertEqual(result["reason_code"], "AMBIGUOUS_RECIPIENT")
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_non_target_recipient_mentions_require_immutable_confirmation(self):
        selected_john = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "amount_minor": 1000,
            "amount_evidence": "$10",
        }
        for index, text in enumerate(
            (
                "Send anybody except John Smith $10",
                "Send somebody other than John Smith $10",
                "Send anyone but John Smith $10",
            ),
            start=91,
        ):
            with self.subTest(text=text):
                self.database.reset()
                result = self.service_for(selected_john).process(
                    f"00000000-0000-4000-8000-{index:012d}",
                    text,
                )

                self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
                self.assertEqual(
                    result["reason_code"],
                    "ACTION_CONFIRMATION_REQUIRED",
                )
                self.assertEqual(
                    result["receipt_choices"][0]["recipient_display_name"],
                    "John Smith",
                )
                self.assertEqual(self.database.get_state()["transactions"], [])

    def test_conditional_or_memo_tail_never_auto_executes(self):
        selected_john = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "amount_minor": 1000,
            "amount_evidence": "$10",
            "payment_reference": "flowers",
            "reference_evidence": "for flowers",
        }
        for index, text in enumerate(
            (
                "Send John Smith $10 for flowers if he calls",
                "Send John Smith $10 for flowers unless I change my mind",
                "Send John Smith $10 with payment only after delivery",
            ),
            start=94,
        ):
            with self.subTest(text=text):
                self.database.reset()
                result = self.service_for(selected_john).process(
                    f"00000000-0000-4000-8000-{index:012d}",
                    text,
                )

                self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
                self.assertEqual(
                    result["reason_code"],
                    "ACTION_CONFIRMATION_REQUIRED",
                )
                self.assertEqual(self.database.get_state()["transactions"], [])

    def test_interrogative_transfer_requires_immutable_confirmation(self):
        selected_john = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "amount_minor": 1000,
            "amount_evidence": "$10",
        }
        result = self.service_for(selected_john).process(
            "00000000-0000-4000-8000-000000000097",
            "Send John Smith $10?",
        )

        self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(result["reason_code"], "ACTION_CONFIRMATION_REQUIRED")
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_model_invented_amount_is_ignored_when_user_stated_none(self):
        before = self.balances()
        invented = {
            **MISSING_AMOUNT_INTENT,
            "amount_minor": 1000,
            "amount_evidence": "$10",
        }

        result = self.service_for(invented).process(
            REQUEST_ID,
            "send Alicia Powell money for flowers",
        )

        self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(result["reason_code"], "MISSING_AMOUNT")
        self.assertIsNone(result["validated_intent"]["amount_minor"])
        self.assertIsNone(result["validated_intent"]["amount_evidence"])
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.database.get_state()["transactions"], [])
        self.assertEqual(self.receipt_count(), 0)

    def test_missing_amount_is_non_mutating_and_names_resolved_recipient(self):
        before = self.balances()

        result = self.service_for(MISSING_AMOUNT_INTENT).process(
            REQUEST_ID,
            "send Alicia Powell money for flowers",
        )

        self.assertEqual(result["reason_code"], "MISSING_AMOUNT")
        self.assertEqual(result["resolved_recipient_display_name"], "Alicia Powell")
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.receipt_count(), 0)

    def test_amount_reply_creates_receipt_without_second_model_call(self):
        from apps.dimon_live.service import ActionService

        extractor = RecordingExtractor(MISSING_AMOUNT_INTENT)
        service = ActionService(self.database, extractor)
        text = "send Alicia Powell money for flowers"
        pending = service.process(REQUEST_ID, text)

        completed = service.respond_to_clarification(
            pending["clarification"]["clarification_id"],
            "00000000-0000-4000-8000-000000000075",
            "ten dollars",
        )

        self.assertEqual(extractor.calls, [text])
        self.assertEqual(completed["receipt_choices"][0]["amount_minor"], 1000)
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_amount_reply_scope_violation_is_non_mutating(self):
        service = self.service_for(MISSING_AMOUNT_INTENT)
        pending = service.process(
            REQUEST_ID,
            "send Alicia Powell money for flowers",
        )

        result = service.respond_to_clarification(
            pending["clarification"]["clarification_id"],
            "00000000-0000-4000-8000-000000000076",
            "send John ten dollars",
        )

        self.assertEqual(result["reason_code"], "CLARIFICATION_SCOPE_VIOLATION")
        self.assertEqual(self.receipt_count(), 0)

    def test_explicit_paynow_proxy_overrides_model_beneficiary_classification(self):
        before = self.balances()
        misclassified = {
            **GOLDEN_INTENT,
            "contract_version": 3,
            "kind": "BENEFICIARY_TRANSFER",
            "source_account_ref": None,
            "source_evidence": None,
            "recipient_label": "john smith's paynow",
            "recipient_evidence": "john smith's paynow",
            "amount_minor": None,
            "amount_evidence": None,
        }

        result = self.service_for(misclassified).process(
            REQUEST_ID,
            "i need to transfer money to john smith's paynow and his number is "
            "86952933 help me transfer $6.50 to him",
        )

        self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(result["reason_code"], "PAYNOW_CONFIRMATION_REQUIRED")
        self.assertEqual(result["validated_intent"]["kind"], "PAYNOW_MOBILE_TRANSFER")
        self.assertEqual(result["validated_intent"]["mobile_proxy"], "86952933")
        self.assertEqual(result["validated_intent"]["proxy_evidence"], "86952933")
        choice = result["receipt_choices"][0]
        self.assertEqual(choice["recipient_display_name"], "JXXX SXXXX")
        self.assertEqual(choice["destination_reference"], "86952933")
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_multiple_explicit_paynow_proxies_are_blocked_without_mutation(self):
        before = self.balances()

        result = self.service_for(PAYNOW_INTENT).process(
            REQUEST_ID,
            "PayNow 86952933 or 86952938 $6.50",
        )

        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertEqual(result["reason_code"], "AMBIGUOUS_PAYNOW_PROXY")
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.database.get_state()["transactions"], [])
        self.assertEqual(self.receipt_count(), 0)

    def test_paynow_requires_receipt_then_executes_directory_destination_once(self):
        before = self.balances()
        service = self.service_for(PAYNOW_INTENT)
        pending = service.process(
            REQUEST_ID,
            "PayNow 8695 2933 ten dollars for flowers",
        )

        self.assertEqual(pending["outcome"], "CLARIFICATION_REQUEST")
        choice = pending["receipt_choices"][0]
        self.assertEqual(choice["recipient_display_name"], "JXXX SXXXX")
        self.assertEqual(choice["destination_reference"], "86952933")
        receipt = self.database.get_receipt(choice["receipt_id"])
        self.assertEqual(receipt["destination_kind"], "PAYNOW_MOBILE")
        self.assertEqual(receipt["destination_reference"], "86952933")
        self.assertTrue(receipt["lookup_reference"].startswith("lookup_"))
        self.assertEqual(self.balances(), before)

        assertion = self.sign(choice)
        executed = self.signed_approve(service, choice, assertion=assertion)
        replay = self.signed_approve(service, choice, assertion=assertion)

        self.assertEqual(executed["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
        self.assertEqual(executed["transaction_id"], replay["transaction_id"])
        self.assertEqual(len(self.database.get_state()["transactions"]), 1)

    def test_missing_amount_paynow_reuses_lookup_without_second_model_call(self):
        from apps.dimon_live.service import ActionService

        intent = {**PAYNOW_INTENT, "amount_minor": None, "amount_evidence": None}
        extractor = RecordingExtractor(intent)
        service = ActionService(self.database, extractor)
        text = "PayNow 8695 2933 for flowers"
        pending = service.process(REQUEST_ID, text)

        completed = service.respond_to_clarification(
            pending["clarification"]["clarification_id"],
            "00000000-0000-4000-8000-000000000081",
            "ten dollars",
        )

        self.assertEqual(extractor.calls, [text])
        choice = completed["receipt_choices"][0]
        self.assertEqual(choice["destination_reference"], "86952933")
        self.assertEqual(choice["recipient_display_name"], "JXXX SXXXX")

    def test_paynow_receipt_and_directory_tampering_never_mutates_ledger(self):
        cases = (
            ("destination_reference = '86952938'", None),
            ("lookup_reference = 'lookup_changed'", None),
            ("recipient_display_name = 'AXXX PXXXX'", None),
            ("beneficiary_id = 'beneficiary_alicia_powell_01'", None),
            (None, "expires_at = '2020-01-01T00:00:00+00:00'"),
        )
        for index, (receipt_change, lookup_change) in enumerate(cases, start=82):
            with self.subTest(
                receipt_change=receipt_change,
                lookup_change=lookup_change,
            ):
                self.database.reset()
                request_id = f"00000000-0000-4000-8000-{index:012d}"
                service = self.service_for(PAYNOW_INTENT)
                pending = service.process(
                    request_id,
                    "PayNow 8695 2933 ten dollars for flowers",
                )
                choice = pending["receipt_choices"][0]
                assertion = self.sign(choice)
                with closing(sqlite3.connect(self.database.path)) as connection, connection:
                    if receipt_change:
                        connection.execute(
                            f"UPDATE review_receipts SET {receipt_change} WHERE receipt_id = ?",
                            (choice["receipt_id"],),
                        )
                    if lookup_change:
                        connection.execute(
                            f"UPDATE paynow_lookups SET {lookup_change} WHERE lookup_reference = ?",
                            (choice["lookup_reference"],),
                        )

                result = self.signed_approve(service, choice, assertion=assertion)

                self.assertEqual(result["outcome"], "BLOCKED")
                self.assertEqual(self.database.get_state()["transactions"], [])

    def test_paynow_beneficiary_destination_remap_never_redirects_receipt(self):
        service = self.service_for(PAYNOW_INTENT)
        pending = service.process(
            REQUEST_ID,
            "PayNow 8695 2933 ten dollars for flowers",
        )
        choice = pending["receipt_choices"][0]
        assertion = self.sign(choice)
        before = self.balances()
        with closing(sqlite3.connect(self.database.path)) as connection, connection:
            connection.execute(
                """
                UPDATE beneficiaries SET destination_account_id = ?
                WHERE beneficiary_id = 'beneficiary_paynow_jxxx_01'
                """,
                ("acct_alicia_powell_01",),
            )

        result = self.signed_approve(service, choice, assertion=assertion)

        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertEqual(result["reason_code"], "PAYNOW_LOOKUP_MISMATCH")
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_paynow_lookup_expiry_blocks_missing_amount_completion(self):
        intent = {**PAYNOW_INTENT, "amount_minor": None, "amount_evidence": None}
        service = self.service_for(intent)
        pending = service.process(
            REQUEST_ID,
            "PayNow 8695 2933 for flowers",
        )
        with closing(sqlite3.connect(self.database.path)) as connection, connection:
            connection.execute(
                "UPDATE paynow_lookups SET expires_at = '2020-01-01T00:00:00+00:00'"
            )

        result = service.respond_to_clarification(
            pending["clarification"]["clarification_id"],
            "00000000-0000-4000-8000-000000000089",
            "ten dollars",
        )

        self.assertEqual(result["reason_code"], "PAYNOW_LOOKUP_EXPIRED")
        self.assertEqual(self.receipt_count(), 0)
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_prefix_john_requires_clarification_without_mutation(self):
        before = self.balances()

        result = self.service_for(
            {
                **GOLDEN_INTENT,
                "source_account_ref": None,
                "source_evidence": None,
                "recipient_label": "John",
                "recipient_evidence": "John",
            }
        ).process(REQUEST_ID, "Send John S$50")

        self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(
            [choice["recipient_display_name"] for choice in result["receipt_choices"]],
            ["John Smith", "John Tan"],
        )
        self.assertTrue(
            all(choice["amount_minor"] == 5000 for choice in result["receipt_choices"])
        )
        self.assertEqual(self.balances(), before)

    def test_single_prefix_candidate_still_requires_clarification(self):
        result = self.service_for(
            {
                **GOLDEN_INTENT,
                "source_account_ref": None,
                "source_evidence": None,
                "recipient_label": "John S",
                "recipient_evidence": "John S",
            }
        ).process(REQUEST_ID, "Send John S S$50")

        self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(
            [choice["recipient_display_name"] for choice in result["receipt_choices"]],
            ["John Smith"],
        )

    def test_close_recipient_match_requests_confirmation_without_mutation(self):
        before = self.balances()
        typo_intent = {
            **GOLDEN_INTENT,
            "recipient_label": "Alicia Powel",
            "recipient_evidence": "Alicia Powel",
            "amount_minor": 250,
            "amount_evidence": "S$2.50",
        }

        result = self.service_for(typo_intent).process(
            REQUEST_ID,
            "Send Alicia Powel S$2.50 from savings.",
        )

        self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(result["reason_code"], "CLOSE_RECIPIENT_MATCH")
        self.assertEqual(result["question"], "Did you mean Alicia Powell ending in 9876?")
        self.assertEqual(
            result["receipt_choices"][0]["recipient_display_name"],
            "Alicia Powell",
        )
        self.assertEqual(result["receipt_choices"][0]["masked_destination"], "•••• 9876")
        self.assertEqual(result["receipt_choices"][0]["amount_minor"], 250)
        self.assertEqual(self.balances(), before)

    def test_same_named_payees_are_told_apart_by_account_ending(self):
        intent = {
            **GOLDEN_INTENT,
            "recipient_label": "John",
            "recipient_evidence": "John",
            "amount_minor": 500,
            "amount_evidence": "five dollars",
            "source_account_ref": None,
            "source_evidence": None,
        }

        result = self.service_for(intent).process(REQUEST_ID, "Send John five dollars")

        self.assertEqual(result["reason_code"], "AMBIGUOUS_RECIPIENT")
        self.assertEqual(
            result["question"],
            "I see 2 saved payees named John. "
            "Did you mean John Smith ending in 4321 or John Tan ending in 2468?",
        )
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_misspelled_first_name_requires_receipt_confirmation(self):
        before = self.balances()
        intent = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "recipient_label": "Alecia",
            "recipient_evidence": "Alecia",
            "amount_minor": 1000,
            "amount_evidence": "$10",
        }

        result = self.service_for(intent).process(
            REQUEST_ID,
            "I owe Alecia like $10; can you help me transfer my money to her?",
        )

        self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(result["reason_code"], "CLOSE_RECIPIENT_MATCH")
        self.assertEqual(
            [choice["recipient_display_name"] for choice in result["receipt_choices"]],
            ["Alicia Powell"],
        )
        self.assertEqual(result["receipt_choices"][0]["amount_minor"], 1000)
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_phonetic_match_requires_confirmation_without_mutation(self):
        before = self.balances()
        intent = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "recipient_label": "Alesha",
            "recipient_evidence": "Alesha",
            "amount_minor": 1000,
            "amount_evidence": "$10",
        }

        result = self.service_for(intent).process(
            REQUEST_ID,
            "send Alesha $10",
        )

        self.assertEqual(result["reason_code"], "PHONETIC_RECIPIENT_MATCH")
        self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(
            [choice["recipient_display_name"] for choice in result["receipt_choices"]],
            ["Alicia Powell"],
        )
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_explicit_transfer_without_source_defaults_only_user_account(self):
        intent = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "recipient_label": "Alicia Powell",
            "recipient_evidence": "Alicia Powell",
            "amount_minor": 500,
            "amount_evidence": "five dollars",
        }

        service = self.service_for(intent)
        pending = service.process(
            REQUEST_ID,
            "Send Alicia Powell five dollars.",
        )
        self.assertIn("SOURCE_DEFAULTED", [step["stage"] for step in pending["trace"]])
        result = self.signed_approve(service, pending["receipt_choices"][0])

        self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
        self.assertEqual(
            result["resolved_action"]["source_account_id"],
            "acct_savings_01",
        )
        balances = {
            row["label"]: row["balance_minor"]
            for row in result["state"]["accounts"]
        }
        self.assertEqual(balances["savings"], 99500)
        self.assertEqual(balances["Alicia Powell"], 8000)

    def test_vague_debt_statement_requires_confirmation_without_mutation(self):
        before = self.balances()
        intent = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "recipient_label": "Alicia Powell",
            "recipient_evidence": "Alicia Powell",
            "amount_minor": 500,
            "amount_evidence": "5 dollars",
        }

        result = self.service_for(intent).process(
            REQUEST_ID,
            "I owe Alicia Powell 5 dollars, help me resolve it.",
        )

        self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(result["reason_code"], "ACTION_CONFIRMATION_REQUIRED")
        self.assertEqual(
            result["question"],
            "Send S$5.00 to Alicia Powell from savings?",
        )
        self.assertNotIn("confirmation_command", result)
        self.assertEqual(len(result["receipt_choices"]), 1)
        self.assertEqual(
            result["receipt_choices"][0]["recipient_display_name"],
            "Alicia Powell",
        )
        self.assertEqual(result["receipt_choices"][0]["amount_minor"], 500)
        self.assertEqual(result["receipt_choices"][0]["source_label"], "savings")
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_approval_executes_receipt_without_calling_extractor_again(self):
        from apps.dimon_live.service import ActionService

        intent = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "recipient_label": "Alicia Powell",
            "recipient_evidence": "Alicia Powell",
            "amount_minor": 500,
            "amount_evidence": "5 dollars",
        }
        extractor = RecordingExtractor(intent)
        service = ActionService(self.database, extractor)
        text = "I owe Alicia Powell 5 dollars, help me resolve it."
        clarification = service.process(
            REQUEST_ID,
            text,
        )
        choice = clarification["receipt_choices"][0]

        executed = self.signed_approve(service, choice)

        self.assertEqual(extractor.calls, [text])
        self.assertEqual(executed["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
        self.assertEqual(executed["trace"][0]["stage"], "RECEIPT_APPROVAL_RECEIVED")
        self.assertNotIn(
            "MODEL_RESPONSE_RECEIVED",
            [step["stage"] for step in executed["trace"]],
        )
        self.assertEqual(len(self.database.get_state()["transactions"]), 1)

    def test_action_replay_before_and_after_approval_never_recalls_model(self):
        from apps.dimon_live.service import ActionService

        intent = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "recipient_label": "Alicia Powell",
            "recipient_evidence": "Alicia Powell",
            "amount_minor": 500,
            "amount_evidence": "5 dollars",
        }
        text = "I owe Alicia Powell 5 dollars, help me resolve it."
        extractor = RecordingExtractor(intent)
        service = ActionService(self.database, extractor)

        pending = service.process(REQUEST_ID, text)
        pending_replay = service.process(REQUEST_ID, text)
        choice = pending["receipt_choices"][0]
        executed = self.signed_approve(service, choice)
        executed_replay = service.process(REQUEST_ID, text)

        self.assertEqual(
            pending_replay["receipt_choices"],
            pending["receipt_choices"],
        )
        self.assertEqual(
            executed_replay["transaction_id"],
            executed["transaction_id"],
        )
        self.assertEqual(extractor.calls, [text])
        self.assertEqual(len(self.database.get_state()["transactions"]), 1)

    def test_receipt_rejections_map_to_blocked_with_current_state(self):
        from apps.dimon_live.service import ActionService

        intent = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "recipient_label": "Alicia Powell",
            "recipient_evidence": "Alicia Powell",
            "amount_minor": 500,
            "amount_evidence": "5 dollars",
        }
        service = ActionService(self.database, RecordingExtractor(intent))
        pending = service.process(
            REQUEST_ID,
            "I owe Alicia Powell 5 dollars, help me resolve it.",
        )
        choice = pending["receipt_choices"][0]

        cases = (
            ("receipt_missing", "0" * 64, "demo_user_01", False, "RECEIPT_NOT_FOUND"),
            (
                choice["receipt_id"],
                choice["receipt_digest"],
                "demo_user_01",
                False,
                "SIGNED_APPROVAL_REQUIRED",
            ),
            (
                choice["receipt_id"],
                "0" * 64,
                "demo_user_01",
                True,
                "APPROVAL_RECEIPT_MISMATCH",
            ),
            (
                choice["receipt_id"],
                choice["receipt_digest"],
                "other_user",
                True,
                "APPROVAL_USER_MISMATCH",
            ),
        )
        for receipt_id, digest, user_id, signed, expected in cases:
            with self.subTest(reason=expected):
                result = service.approve(
                    receipt_id,
                    digest,
                    assertion=self.sign(choice) if signed else None,
                    context=self.device.context,
                    user_id=user_id,
                )
                self.assertEqual(
                    (result["outcome"], result["reason_code"]),
                    ("BLOCKED", expected),
                )
                self.assertIn("accounts", result["state"])

    def test_superseded_and_expired_receipts_map_to_stable_codes(self):
        from apps.dimon_live.service import ActionService

        john_intent = {
            **GOLDEN_INTENT,
            "recipient_label": "John",
            "recipient_evidence": "John",
            "amount_minor": 500,
            "amount_evidence": "five dollars",
            "source_account_ref": None,
            "source_evidence": None,
        }
        john_service = ActionService(self.database, RecordingExtractor(john_intent))
        pending = john_service.process(REQUEST_ID, "Send John five dollars")
        selected, sibling = pending["receipt_choices"]
        stale_sibling_assertion = self.sign(sibling)
        self.signed_approve(john_service, selected)

        # A superseded receipt can no longer be put in front of the user...
        with self.assertRaisesRegex(ReceiptRejected, "RECEIPT_SUPERSEDED"):
            self.sign(sibling)
        # ...and a signature captured before the newer approval is stale.
        superseded = self.signed_approve(
            john_service, sibling, assertion=stale_sibling_assertion
        )

        self.assertEqual(superseded["reason_code"], "APPROVAL_COUNTER_REPLAY")
        self.assertEqual(len(self.database.get_state()["transactions"]), 1)
        self.database.reset()

        vague_intent = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "recipient_label": "Alicia Powell",
            "recipient_evidence": "Alicia Powell",
            "amount_minor": 500,
            "amount_evidence": "5 dollars",
        }
        vague_service = ActionService(
            self.database,
            RecordingExtractor(vague_intent),
        )
        pending = vague_service.process(
            REQUEST_ID,
            "I owe Alicia Powell 5 dollars, help me resolve it.",
        )
        choice = pending["receipt_choices"][0]
        assertion = self.sign(choice)
        with closing(sqlite3.connect(self.database.path)) as connection, connection:
            connection.execute(
                """
                UPDATE review_receipts SET status = 'EXPIRED'
                WHERE receipt_id = ?
                """,
                (choice["receipt_id"],),
            )

        expired = self.signed_approve(vague_service, choice, assertion=assertion)

        self.assertEqual(expired["reason_code"], "RECEIPT_EXPIRED")
        self.assertEqual(len(self.database.get_state()["transactions"]), 0)

    def test_insufficient_funds_creates_no_ambiguous_receipts(self):
        intent = {
            **GOLDEN_INTENT,
            "recipient_label": "John",
            "recipient_evidence": "John",
            "amount_minor": 100001,
            "amount_evidence": "S$1,000.01",
            "source_account_ref": None,
            "source_evidence": None,
        }

        result = self.service_for(intent).process(
            REQUEST_ID,
            "Send John S$1,000.01",
        )

        self.assertEqual(result["reason_code"], "INSUFFICIENT_FUNDS")
        with closing(sqlite3.connect(self.database.path)) as connection, connection:
            receipt_count = connection.execute(
                "SELECT COUNT(*) FROM review_receipts"
            ).fetchone()[0]
        self.assertEqual(receipt_count, 0)

    def test_negated_transfer_never_executes(self):
        before = self.balances()

        result = self.service_for(GOLDEN_INTENT).process(
            REQUEST_ID,
            "Do not send John Smith S$50 from savings.",
        )

        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertEqual(result["reason_code"], "NEGATED_ACTION")
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_insufficient_funds_is_blocked_without_mutation(self):
        before = self.balances()

        result = self.service_for(
            {
                **GOLDEN_INTENT,
                "source_account_ref": None,
                "source_evidence": None,
                "amount_minor": 100001,
                "amount_evidence": "S$1,000.01",
            }
        ).process(REQUEST_ID, "Send John Smith S$1,000.01")

        self.assertEqual(
            (result["outcome"], result["reason_code"]),
            ("BLOCKED", "INSUFFICIENT_FUNDS"),
        )
        self.assertEqual(self.balances(), before)

    def test_unsupported_action_is_blocked_without_mutation(self):
        before = self.balances()

        result = self.service_for(
            {
                **GOLDEN_INTENT,
                "kind": "BILL_PAYMENT",
            }
        ).process(REQUEST_ID, "Pay my bill")

        self.assertEqual(
            (result["outcome"], result["reason_code"]),
            ("BLOCKED", "UNSUPPORTED_ACTION"),
        )
        self.assertEqual(self.balances(), before)

    def test_invalid_model_output_is_failed_without_mutation(self):
        from apps.dimon_live.service import ActionService

        before = self.balances()
        service = ActionService(
            self.database,
            RaisingExtractor(InvalidModelOutput("bad JSON")),
        )

        result = service.process(REQUEST_ID, GOLDEN_REQUEST)

        self.assertEqual(
            (result["outcome"], result["reason_code"]),
            ("FAILED", "INVALID_MODEL_OUTPUT"),
        )
        self.assertEqual(self.balances(), before)

    def test_model_timeout_is_failed_without_mutation(self):
        from apps.dimon_live.service import ActionService

        before = self.balances()
        service = ActionService(
            self.database,
            RaisingExtractor(ModelUnavailable("timeout")),
        )

        result = service.process(REQUEST_ID, GOLDEN_REQUEST)

        self.assertEqual(
            (result["outcome"], result["reason_code"]),
            ("FAILED", "MODEL_UNAVAILABLE"),
        )
        self.assertEqual(self.balances(), before)

    def test_idempotent_replay_does_not_call_model_or_debit_again(self):
        from apps.dimon_live.service import ActionService

        service = self.service_for(GOLDEN_INTENT)
        pending = service.process(REQUEST_ID, GOLDEN_REQUEST)
        first = self.signed_approve(service, pending["receipt_choices"][0])
        replay_service = ActionService(
            self.database,
            RaisingExtractor(AssertionError("model must not be called")),
        )

        replay = replay_service.process(REQUEST_ID, GOLDEN_REQUEST)

        self.assertEqual(replay["transaction_id"], first["transaction_id"])
        self.assertEqual(
            replay["resolved_action"]["destination_account_id"],
            "acct_john_smith_01",
        )
        self.assertEqual(self.balances()[0], 95000)
        self.assertEqual(len(self.database.get_state()["transactions"]), 1)


if __name__ == "__main__":
    unittest.main()

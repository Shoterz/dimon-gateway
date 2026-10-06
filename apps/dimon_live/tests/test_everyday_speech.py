"""Everyday and Singlish phrasing: bare amounts, nicknames, one-sentence batches."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from apps.dimon_live.database import Database
from apps.dimon_live.grounding import derive_amount_mentions_from_text
from apps.dimon_live.model_adapter import ExtractedIntent
from apps.dimon_live.service import ActionService
from apps.dimon_live.validation_agent import (
    DirectoryEntry,
    DraftItem,
    DraftUnderAudit,
    audit_draft,
    transcript_amounts,
)


def single(label, evidence=None, reference=None):
    return {
        "contract_version": 3, "kind": "BENEFICIARY_TRANSFER",
        "source_account_ref": None, "source_evidence": None,
        "recipient_label": label, "recipient_evidence": evidence or label,
        "currency": "SGD", "payment_reference": reference,
        "reference_evidence": reference, "requested_execution_date": None,
    }


def batch(*labels):
    return {
        "contract_version": 4, "kind": "BENEFICIARY_TRANSFER_BATCH",
        "source_account_ref": None, "source_evidence": None,
        "items": [{"recipient_label": label, "recipient_evidence": label} for label in labels],
        "currency": "SGD", "requested_execution_date": None,
    }


class StaticExtractor:
    def __init__(self, intent):
        self.intent = intent

    def extract(self, text):
        intent = copy.deepcopy(self.intent)
        return ExtractedIntent(intent, json.dumps(intent), 1.0)


class BareAmountParsingTests(unittest.TestCase):
    def test_amounts_in_a_payment_slot_are_read_as_dollars(self):
        cases = {
            "can help me pay alicia ten bucks": [1000],
            "transfer my mum fifty for the makan": [5000],
            "send john smith twenty can?": [2000],
            "Pay Alicia 10 and John Smith 20 for the flowers": [1000, 2000],
            "send 10 to alicia": [1000],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual([m.amount_minor for m in derive_amount_mentions_from_text(text)], expected)
                self.assertEqual(transcript_amounts(text), set(expected))

    def test_counts_and_numbers_that_are_not_money_stay_unread(self):
        for text in (
            "pay alicia for 2 cakes",
            "send john 3 times",
            "Send Alicia Powell 500 cents",
            "What's my balance for 2024",
        ):
            with self.subTest(text=text):
                self.assertEqual(derive_amount_mentions_from_text(text), ())
                self.assertEqual(transcript_amounts(text), set())

    def test_a_marked_amount_wins_over_bare_numbers(self):
        mentions = derive_amount_mentions_from_text("pay john smith 2 x S$15")
        self.assertEqual([m.amount_minor for m in mentions], [1500])


class ValidatorNicknameTests(unittest.TestCase):
    DIRECTORY = (
        DirectoryEntry("b_mum", "Lim Siew Hua", ("mum", "my mum")),
        DirectoryEntry("b_alicia", "Alicia Powell"),
    )

    def audit(self, text, beneficiary_id, name, amount):
        return audit_draft(
            [text],
            DraftUnderAudit("Savings", "SGD", (DraftItem(beneficiary_id, name, amount),)),
            directory=self.DIRECTORY,
            source_labels=["Savings"],
        )

    def test_a_saved_nickname_confirms_the_payee(self):
        report = self.audit("transfer my mum fifty for the makan", "b_mum", "Lim Siew Hua", 5000)
        self.assertEqual(report.verdict, "PASS")

    def test_a_nickname_for_someone_else_freezes_the_draft(self):
        report = self.audit("transfer my mum fifty", "b_alicia", "Alicia Powell", 5000)
        self.assertEqual(report.verdict, "FROZEN")


class EverydaySpeechServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "ledger.sqlite3", single_account=True)
        self.database.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def process(self, suffix, text, intent):
        return ActionService(self.database, StaticExtractor(intent)).process(
            f"00000000-0000-4000-8000-{suffix:012d}", text
        )

    def test_several_payments_in_one_sentence_become_one_batch(self):
        result = self.process(
            901, "Pay Alicia 10 and John Smith 20 for the flowers", batch("Alicia", "John Smith")
        )
        receipt = result["batch_receipt"]
        self.assertEqual(
            [(item["recipient_display_name"], item["amount_minor"]) for item in receipt["items"]],
            [("Alicia Powell", 1000), ("John Smith", 2000)],
        )
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_a_batch_collapsed_by_the_model_is_recovered_with_first_names(self):
        collapsed = {**single("Alicia", reference="the flowers"), "reference_evidence": "for the flowers"}
        result = self.process(906, "Pay Alicia 10 and John Smith 20 for the flowers", collapsed)
        self.assertEqual(result["outcome"], "BATCH_CONFIRMATION_REQUEST")
        stages = [step["stage"] for step in result["trace"]]
        self.assertIn("BATCH_RECIPIENTS_RECOVERED_FROM_DIRECTORY", stages)
        self.assertIn("BATCH_REFERENCE_NOTED", stages)
        self.assertEqual(
            [(i["recipient_display_name"], i["masked_destination"], i["amount_minor"]) for i in result["batch_receipt"]["items"]],
            [("Alicia Powell", "•••• 9876", 1000), ("John Smith", "•••• 4321", 2000)],
        )

    def test_a_reference_in_the_middle_of_a_batch_is_not_recovered(self):
        collapsed = {**single("Alicia", reference="lunch"), "reference_evidence": "for lunch"}
        result = self.process(907, "Pay Alicia 10 for lunch and John Smith 20", collapsed)
        self.assertNotEqual(result["outcome"], "BATCH_CONFIRMATION_REQUEST")

    def test_an_ambiguous_first_name_in_a_batch_is_still_refused(self):
        result = self.process(902, "Pay John 10 and Alicia Powell 20", batch("John", "Alicia Powell"))
        self.assertEqual((result["outcome"], result["reason_code"]), ("BLOCKED", "AMBIGUOUS_BATCH_BINDING"))

    def test_singlish_requests_draft_the_right_payment(self):
        cases = (
            (903, "can help me pay alicia ten bucks", single("alicia"), "Alicia Powell", 1000),
            (904, "transfer my mum fifty for the makan", single("my mum"), "Lim Siew Hua", 5000),
            (905, "send john smith twenty can?", single("john smith"), "John Smith", 2000),
        )
        for suffix, text, intent, name, amount in cases:
            with self.subTest(text=text):
                result = self.process(suffix, text, intent)
                self.assertNotEqual(result["outcome"], "BLOCKED", result.get("reason_code"))
                choice = result["receipt_choices"][0]
                self.assertEqual((choice["recipient_display_name"], choice["amount_minor"]), (name, amount))
        self.assertEqual(self.database.get_state()["transactions"], [])


class NoApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "ledger.sqlite3", single_account=True)
        self.database.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def process(self, suffix, text):
        extractor = StaticExtractor(single("Alicia Powell"))
        extractor.calls = []
        original = extractor.extract

        def counting(value):
            extractor.calls.append(value)
            return original(value)

        extractor.extract = counting
        self.extractor = extractor
        return ActionService(self.database, extractor).process(
            f"00000000-0000-4000-8000-{suffix:012d}", text
        )

    def balances(self):
        return {a["account_id"]: a["balance_minor"] for a in self.database.get_state()["accounts"]}

    def test_balance_is_answered_without_the_model_or_a_receipt(self):
        for suffix, text in ((951, "What's my balance?"), (952, "how much money do i have")):
            with self.subTest(text=text):
                result = self.process(suffix, text)
                self.assertEqual((result["outcome"], result["reason_code"]), ("ANSWERED", "BALANCE_INQUIRY"))
                self.assertIn("Savings: S$", result["answer"])
                self.assertNotIn("receipt_choices", result)
        self.assertEqual(self.extractor.calls, [])

    def test_money_request_is_recorded_and_moves_nothing(self):
        before = self.balances()
        result = self.process(953, "Request S$20 from John Smith for dinner")
        self.assertEqual((result["outcome"], result["reason_code"]), ("ANSWERED", "MONEY_REQUEST_SENT"))
        self.assertEqual(result["money_request"]["recipient_display_name"], "John Smith")
        self.assertEqual(result["money_request"]["amount_minor"], 2000)
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.database.get_state()["transactions"], [])

        for suffix, text, name, amount in (
            (954, "ask my mum for fifty", "Lim Siew Hua", 5000),
            (955, "request 15 from alicia", "Alicia Powell", 1500),
        ):
            with self.subTest(text=text):
                result = self.process(suffix, text)
                self.assertEqual(
                    (result["money_request"]["recipient_display_name"], result["money_request"]["amount_minor"]),
                    (name, amount),
                )

    def test_unclear_money_requests_ask_instead_of_guessing(self):
        self.assertEqual(self.process(956, "request S$20 from John")["reason_code"], "AMBIGUOUS_RECIPIENT")
        self.assertEqual(self.process(957, "request money from Alicia Powell")["reason_code"], "MISSING_AMOUNT")
        self.assertEqual(self.process(958, "request S$20 from Bob")["reason_code"], "UNKNOWN_RECIPIENT")

    def test_anything_that_pays_out_still_goes_through_signing(self):
        # Two requests: the balance is answered, the payment still needs a signed receipt.
        result = self.process(959, "Check my balance and send Alicia Powell S$5")
        self.assertEqual(result["outcome"], "MULTI_REQUEST")
        balance, payment = (step["result"] for step in result["steps"])
        self.assertEqual(balance["outcome"], "ANSWERED")
        self.assertNotEqual(payment["outcome"], "ANSWERED")
        self.assertTrue(payment["receipt_choices"])
        self.assertEqual(payment["state"]["transactions"], [])


if __name__ == "__main__":
    unittest.main()

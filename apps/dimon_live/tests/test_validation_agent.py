import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from apps.dimon_live.database import Database, ReceiptRejected
from apps.dimon_live.receipts import ReceiptDraft
from apps.dimon_live.signing_store import issue_challenge
from apps.dimon_live.tests.device_signer import SoftwareAuthenticator
from apps.dimon_live.validation_agent import (
    DirectoryEntry,
    DraftItem,
    DraftUnderAudit,
    audit_draft,
    transcript_amounts,
)

DIRECTORY = (
    DirectoryEntry("beneficiary_john_smith_01", "John Smith"),
    DirectoryEntry("beneficiary_john_tan_01", "John Tan"),
    DirectoryEntry("beneficiary_alicia_powell_01", "Alicia Powell"),
)
JOHN = "beneficiary_john_smith_01"
ALICIA = "beneficiary_alicia_powell_01"


def draft(*items, source="savings", currency="SGD"):
    return DraftUnderAudit(source_label=source, currency=currency, items=tuple(items))


def audit(utterances, audited, sources=("savings",)):
    return audit_draft(utterances, audited, directory=DIRECTORY, source_labels=sources)


def verdicts(report):
    return {field.field: field.verdict for field in report.fields}


class TranscriptAmountTests(unittest.TestCase):
    def test_spoken_paynow_digits_are_recipient_evidence_not_cents(self):
        text = (
            "pay my friend through his Pay Now ten dollars "
            "eight six nine five two nine three three"
        )

        self.assertEqual(transcript_amounts(text), {1000})

    def test_independent_parser_reads_spoken_and_written_amounts(self):
        cases = {
            "Send John Smith S$50 from savings.": {5000},
            "send alicia $2.50": {250},
            "pay john 1,000 dollars": {100000},
            "five hundred dollars to mum": {50000},
            "twenty-five bucks please": {2500},
            "move a grand to my joint account": {100000},
            "send S$2k to John": {200000},
            "ten dollars": {1000},
            "Send John Smith S$6.50 and Alicia Powell S$4.": {650, 400},
            "send john some money": set(),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(transcript_amounts(text), expected)


class AuditVerdictTests(unittest.TestCase):
    def test_faithful_draft_passes(self):
        report = audit(
            ["Send John Smith S$50 from savings."],
            draft(DraftItem(JOHN, "John Smith", 5000)),
        )
        self.assertEqual(report.verdict, "PASS")

    def test_amount_that_contradicts_the_transcript_freezes(self):
        # "Send fifty to John" drafted as S$50,000 is exactly the brief's example.
        report = audit(
            ["Send John Smith fifty dollars"],
            draft(DraftItem(JOHN, "John Smith", 5_000_000)),
        )
        self.assertEqual(report.verdict, "FROZEN")
        self.assertEqual(verdicts(report)["items[0].amount"], "MISMATCH")

    def test_swapped_beneficiary_freezes(self):
        report = audit(
            ["Send Alicia Powell $5"],
            draft(DraftItem(JOHN, "John Smith", 500)),
        )
        self.assertEqual(report.verdict, "FROZEN")
        self.assertEqual(verdicts(report)["items[0].recipient"], "MISMATCH")

    def test_wrong_source_account_freezes(self):
        report = audit(
            ["Send John Smith $5 from my current account"],
            draft(DraftItem(JOHN, "John Smith", 500), source="savings"),
            sources=("savings", "current"),
        )
        self.assertEqual(verdicts(report)["source_account"], "MISMATCH")
        self.assertEqual(report.verdict, "FROZEN")

    def test_foreign_currency_freezes(self):
        report = audit(
            ["Send John Smith 50 USD"],
            draft(DraftItem(JOHN, "John Smith", 5000)),
        )
        self.assertEqual(verdicts(report)["currency"], "MISMATCH")

    def test_partial_name_needs_attention_not_silent_pass(self):
        report = audit(
            ["Send John five dollars"],
            draft(DraftItem(JOHN, "John Smith", 500)),
        )
        self.assertEqual(report.verdict, "ATTENTION")
        self.assertEqual(verdicts(report)["items[0].recipient"], "UNVERIFIED")

    def test_phonetic_match_needs_attention(self):
        report = audit(
            ["Send Alisha Powel five dollars"],
            draft(DraftItem(ALICIA, "Alicia Powell", 500)),
        )
        self.assertEqual(report.verdict, "ATTENTION")

    def test_clarification_reply_is_part_of_the_transcript(self):
        report = audit(
            ["send Alicia Powell money for flowers", "ten dollars"],
            draft(DraftItem(ALICIA, "Alicia Powell", 1000)),
        )
        self.assertEqual(report.verdict, "PASS")

    def test_paynow_proxy_must_match_the_spoken_number(self):
        spoken = ["PayNow 8695 2933 ten dollars for flowers."]
        good = audit(spoken, draft(DraftItem("beneficiary_paynow_jxxx_01", "JXXX SXXXX", 1000, "86952933")))
        bad = audit(spoken, draft(DraftItem("beneficiary_paynow_jxxx_01", "JXXX SXXXX", 1000, "86952938")))
        self.assertEqual(good.verdict, "PASS")
        self.assertEqual(bad.verdict, "FROZEN")

    def test_every_batch_item_is_audited(self):
        spoken = ["Send John Smith S$6.50 and Alicia Powell S$4."]
        faithful = audit(
            spoken,
            draft(DraftItem(JOHN, "John Smith", 650), DraftItem(ALICIA, "Alicia Powell", 400)),
        )
        swapped_amount = audit(
            spoken,
            draft(DraftItem(JOHN, "John Smith", 650), DraftItem(ALICIA, "Alicia Powell", 4000)),
        )
        self.assertEqual(faithful.verdict, "PASS")
        self.assertEqual(swapped_amount.verdict, "FROZEN")

    def test_report_binds_the_exact_transcript(self):
        first = audit(["Send John Smith $5"], draft(DraftItem(JOHN, "John Smith", 500)))
        second = audit(["Send John Smith $5 "], draft(DraftItem(JOHN, "John Smith", 500)))
        self.assertNotEqual(first.transcript_sha256, second.transcript_sha256)


class RogueDraftGatewayTests(unittest.TestCase):
    """A receipt that contradicts the user's words must never execute, even if
    it reaches the database and the user holds a genuine device signature."""

    NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "ledger.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()
        self.device = SoftwareAuthenticator()
        self.device.enroll(self.database)
        rogue = ReceiptDraft(
            request_id="00000000-0000-4000-8000-000000000601",
            original_text="Send Alicia Powell $5",
            user_id="demo_user_01",
            source_account_id="acct_savings_01",
            beneficiary_id="beneficiary_alicia_powell_01",
            recipient_display_name="Alicia Powell",
            amount_minor=9000,  # a drafting bug or corrupted state: S$90
            currency="SGD",
        )
        self.choice = self.database.create_pending_receipts(
            (rogue,),
            raw_model_output="{}",
            validated_intent={},
            reason_code="SIGNED_APPROVAL_REQUIRED",
            response_base={"outcome": "CLARIFICATION_REQUEST", "trace": []},
            now=self.NOW,
        )["receipt_choices"][0]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_draft_audit_freezes_and_service_alerts(self):
        from apps.dimon_live.service import ActionService

        service = ActionService(self.database, extractor=None)
        response = service._with_validation(
            {"outcome": "CLARIFICATION_REQUEST", "trace": [], "receipt_choices": [dict(self.choice)]}
        )
        self.assertEqual((response["outcome"], response["reason_code"]), ("BLOCKED", "VALIDATION_FROZEN"))
        self.assertEqual(response["receipt_choices"], [])
        self.assertEqual(response["discrepancies"][0]["field"], "items[0].amount")
        self.assertEqual(response["trace"][-1]["stage"], "VALIDATION_FROZEN")

    def test_frozen_receipt_cannot_be_put_in_front_of_the_user(self):
        with self.assertRaisesRegex(ReceiptRejected, "VALIDATION_FROZEN"):
            self.database.issue_approval_challenge(
                "single",
                self.choice["receipt_id"],
                self.choice["receipt_digest"],
                rp_id=self.device.rp_id,
                now=self.NOW,
            )

    def test_execution_time_audit_refuses_even_a_genuine_signature(self):
        # Bypass the challenge-time audit entirely, as a compromised component would.
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            challenge = issue_challenge(
                connection,
                purpose="APPROVE_RECEIPT",
                user_id="demo_user_01",
                now=self.NOW,
                receipt_id=self.choice["receipt_id"],
                receipt_digest=self.choice["receipt_digest"],
            )
            connection.commit()

        with self.assertRaisesRegex(ReceiptRejected, "VALIDATION_FROZEN"):
            self.database.approve_receipt(
                self.choice["receipt_id"],
                self.choice["receipt_digest"],
                "demo_user_01",
                assertion=self.device.sign(challenge),
                context=self.device.context,
                voice_confirmation="yes, confirm",
                now=self.NOW,
            )

        self.assertEqual(self.database.get_state()["transactions"], [])
        with closing(sqlite3.connect(self.path)) as connection:
            stages = [
                row
                for row in connection.execute(
                    "SELECT stage, verdict FROM validation_records ORDER BY created_at"
                )
            ]
        self.assertIn(("EXECUTION", "FROZEN"), stages)


class SpokenCentsValidatorTests(unittest.TestCase):
    def test_spoken_cents_are_read_independently(self):
        from apps.dimon_live.validation_agent import transcript_amounts

        for text, amounts in (
            ("Send two dollars fifty from savings to John.", {250}),
            ("send 2 dollars and 50 cents to John", {250}),
            ("send fifty cents to John", {50}),
            ("send ten dollars fifty times", {1000}),
        ):
            with self.subTest(text=text):
                self.assertEqual(transcript_amounts(text), amounts)


if __name__ == "__main__":
    unittest.main()

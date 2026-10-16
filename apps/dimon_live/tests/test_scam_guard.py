import copy
import json
import tempfile
import unittest
from pathlib import Path

from apps.dimon_live.database import Database
from apps.dimon_live.model_adapter import ExtractedIntent
from apps.dimon_live.scam_guard import assess
from apps.dimon_live.service import ActionService

HELD = {
    "The police officer told me to move my savings to a safe account": "AUTHORITY_IMPERSONATION",
    "Transfer everything to the government safe account": "SAFE_ACCOUNT",
    "MAS asked me to send S$900 to protect my money": "AUTHORITY_IMPERSONATION",
    "Send John S$500 but don't tell the bank": "SECRECY",
    "Pay S$300 or I will be arrested for money laundering": "THREAT_URGENCY",
    "Send S$200 to unlock my commission for the tasks": "INVESTMENT_OR_JOB_SCAM",
    "Invest S$500, guaranteed returns next week": "INVESTMENT_OR_JOB_SCAM",
    "The bank officer said to install AnyDesk and send S$100": "REMOTE_CONTROL_OR_CODES",
}
ALLOWED = (
    "Send John Smith S$50 from savings.",
    "Move my savings to my joint account",
    "Pay John S$20 for the court booking he told me about",
    "transfer my mum fifty for the makan",
    "Send Alicia S$10, she said thanks for dinner",
    "Pay the police fine of S$50 to Alicia",
    "can help me pay alicia ten bucks",
    "Transfer $30 to Mas Ayu for the batik",
)


class ScamGuardTests(unittest.TestCase):
    def test_scam_patterns_are_held(self):
        for text, signal in HELD.items():
            with self.subTest(text=text):
                verdict = assess(text)
                self.assertTrue(verdict.held)
                self.assertIn(signal, verdict.signals)
                self.assertIn("1799", verdict.to_dict()["warning"])

    def test_ordinary_requests_pass(self):
        for text in ALLOWED:
            with self.subTest(text=text):
                self.assertFalse(assess(text).held, assess(text).signals)


class StaticExtractor:
    def __init__(self):
        self.calls = []

    def extract(self, text):
        self.calls.append(text)
        intent = {
            "contract_version": 3, "kind": "BENEFICIARY_TRANSFER",
            "source_account_ref": "savings", "source_evidence": "savings",
            "recipient_label": "John Smith", "recipient_evidence": "John Smith",
            "currency": "SGD", "payment_reference": None, "reference_evidence": None,
            "requested_execution_date": None,
        }
        return ExtractedIntent(copy.deepcopy(intent), json.dumps(intent), 1.0)


class ScamHoldServiceTests(unittest.TestCase):
    def test_a_well_formed_scam_request_is_held_and_never_drafted(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "ledger.sqlite3")
            database.initialize()
            extractor = StaticExtractor()
            result = ActionService(database, extractor).process(
                "00000000-0000-4000-8000-000000000801",
                "The police officer told me to send John Smith S$900 from savings to a safe account",
            )
            self.assertEqual((result["outcome"], result["reason_code"]), ("BLOCKED", "SCAM_SUSPECTED"))
            self.assertIn("ScamShield", result["warning"])
            self.assertNotIn("receipt_choices", result)
            self.assertEqual(extractor.calls, [])
            self.assertEqual(database.get_state()["transactions"], [])


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
import sqlite3

from apps.dimon_live.database import Database
from apps.dimon_live.model_adapter import ExtractedIntent, ModelUnavailable
from apps.dimon_live.inference import InferenceProviders
from apps.dimon_live.service import ActionService
from apps.dimon_live.tests.device_signer import SoftwareAuthenticator


FULL_BILL = {
    "contract_version": 1,
    "kind": "BILL_PAYMENT",
    "biller_label": "Singapore Power",
    "biller_evidence": "Singapore Power bill",
    "amount_evidence": "$120",
    "currency": "SGD",
    "currency_evidence": "$",
    "source_account_label": "savings",
    "source_account_evidence": "savings",
    "requested_execution_date": None,
    "execution_date_evidence": None,
}


class NeverTransferExtractor:
    def extract(self, text):
        raise AssertionError("transfer extractor must not receive a bill request")


class StaticBillExtractor:
    def __init__(self, intent):
        self.intent = intent
        self.calls = []

    def extract(self, text):
        self.calls.append(text)
        return ExtractedIntent(dict(self.intent), json.dumps(self.intent), 7.5)


class FailingBillExtractor:
    def extract(self, text):
        raise ModelUnavailable("private provider detail")


class UnusedRecognizer:
    async def open_stream(self, on_partial):
        raise AssertionError("not called")

    async def transcribe(self, audio, on_partial):
        raise AssertionError("not called")


class UnusedSynthesizer:
    async def __call__(self, text):
        raise AssertionError("not called")

    async def synthesize_result(self, result):
        raise AssertionError("not called")


class BillPaymentDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "demo.sqlite3")
        self.database.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def service(self, bill_extractor):
        return ActionService(
            self.database,
            NeverTransferExtractor(),
            bill_payment_extractor=bill_extractor,
        )

    def counts(self):
        with closing(sqlite3.connect(self.database.path)) as connection:
            return {
                "receipts": connection.execute(
                    "SELECT COUNT(*) FROM review_receipts"
                ).fetchone()[0],
                "transactions": connection.execute(
                    "SELECT COUNT(*) FROM ledger_transactions"
                ).fetchone()[0],
            }

    def test_complete_bill_request_creates_grounded_bill_receipt_for_review(self):
        before = self.database.get_state()["accounts"]
        result = self.service(StaticBillExtractor(FULL_BILL)).process(
            "00000000-0000-4000-8000-000000000601",
            "Pay my Singapore Power bill $120 from savings.",
        )

        self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(result["reason_code"], "BILL_PAYMENT_CONFIRMATION_REQUIRED")
        choice = result["receipt_choices"][0]
        self.assertEqual(choice["amount_minor"], 12_000)
        self.assertEqual(choice["recipient_display_name"], "SP Services")
        self.assertEqual(choice["destination_kind"], "UTILITY_BILL")
        self.assertIn("confirm on your device", result["question"])
        self.assertEqual(result["state"]["accounts"], before)
        self.assertEqual(self.counts(), {"receipts": 1, "transactions": 0})

    def test_bill_payment_completes_only_after_receipt_signature(self):
        device = SoftwareAuthenticator()
        device.enroll(self.database)
        service = self.service(StaticBillExtractor(FULL_BILL))
        pending = service.process(
            "00000000-0000-4000-8000-000000000604",
            "Pay my Singapore Power bill $120 from savings.",
        )
        choice = pending["receipt_choices"][0]
        self.assertEqual(self.database.get_state()["transactions"], [])

        challenge = self.database.issue_approval_challenge(
            "single", choice["receipt_id"], choice["receipt_digest"],
            rp_id=device.rp_id,
        )
        result = self.database.approve_receipt(
            choice["receipt_id"], choice["receipt_digest"], "demo_user_01",
            assertion=device.sign(challenge), context=device.context,
            voice_confirmation="yes, confirm",
        )

        self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_BILL_PAYMENT")
        self.assertEqual(result["resolved_action"]["biller_display_name"], "SP Services")
        self.assertEqual(result["resolved_action"]["balance_after_minor"], 188_000)
        state = self.database.get_state()
        self.assertEqual(state["transactions"][0]["kind"], "UTILITY_BILL_PAYMENT")
        self.assertEqual(state["transactions"][0]["destination_label"], "SP Services")

    def test_incomplete_bill_request_returns_clarification(self):
        intent = {**FULL_BILL, "amount_evidence": None}
        intent.update(
            {"biller_label": "M1", "biller_evidence": "M1 bill"}
        )
        result = self.service(StaticBillExtractor(intent)).process(
            "00000000-0000-4000-8000-000000000602",
            "Pay my M1 bill from savings.",
        )

        self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(result["reason_code"], "AMOUNT_REQUIRED")
        self.assertEqual(result["question"], "How much should I pay?")
        self.assertEqual(self.counts(), {"receipts": 0, "transactions": 0})

    def test_plural_bills_request_stays_in_bill_flow(self):
        intent = {
            **FULL_BILL,
            "biller_label": None,
            "biller_evidence": None,
            "amount_evidence": None,
            "currency_evidence": None,
            "source_account_label": None,
            "source_account_evidence": None,
        }
        extractor = StaticBillExtractor(intent)

        result = self.service(extractor).process(
            "00000000-0000-4000-8000-000000000606",
            "I want to pay my bills.",
        )

        self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(result["reason_code"], "BILLER_REQUIRED")
        self.assertEqual(result["question"], "Which bill would you like to pay?")
        self.assertEqual(extractor.calls, ["I want to pay my bills."])
        self.assertEqual(self.counts(), {"receipts": 0, "transactions": 0})

    def test_outstanding_water_bill_uses_server_statement_total_for_review(self):
        intent = {
            **FULL_BILL,
            "biller_label": "PUB Water",
            "biller_evidence": "water bill",
            "amount_evidence": None,
            "currency_evidence": None,
            "source_account_label": None,
            "source_account_evidence": None,
        }
        result = self.service(StaticBillExtractor(intent)).process(
            "00000000-0000-4000-8000-000000000605",
            "Help me pay my outstanding water bill.",
        )

        self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(result["reason_code"], "BILL_PAYMENT_CONFIRMATION_REQUIRED")
        choice = result["receipt_choices"][0]
        self.assertEqual(choice["recipient_display_name"], "PUB Water")
        self.assertEqual(choice["amount_minor"], 3_280)
        self.assertEqual(choice["source_label"], "savings")
        self.assertEqual(self.counts(), {"receipts": 1, "transactions": 0})

    def test_bill_model_failure_is_stable_and_nonmutating(self):
        result = self.service(FailingBillExtractor()).process(
            "00000000-0000-4000-8000-000000000603",
            "Pay my Singapore Power bill $120.",
        )

        self.assertEqual(result["outcome"], "FAILED")
        self.assertEqual(result["reason_code"], "MODEL_UNAVAILABLE")
        self.assertNotIn("private provider detail", str(result))
        self.assertEqual(self.counts(), {"receipts": 0, "transactions": 0})

    def test_app_preserves_the_provider_bill_extractor_for_action_services(self):
        from apps.dimon_live.app import create_app

        bill_extractor = StaticBillExtractor(FULL_BILL)
        app = create_app(
            Path(self.temp_dir.name) / "api.sqlite3",
            providers=InferenceProviders(
                mode="fixture",
                extractor=NeverTransferExtractor(),
                recognizer=UnusedRecognizer(),
                voice_lifecycle=None,
                synthesizer=UnusedSynthesizer(),
                bill_payment_extractor=bill_extractor,
            ),
        )

        self.assertIs(app.state.bill_payment_extractor, bill_extractor)


if __name__ == "__main__":
    unittest.main()

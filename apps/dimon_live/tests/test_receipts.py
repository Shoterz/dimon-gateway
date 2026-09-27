import dataclasses
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
        first = build_receipt_record(
            self.draft(),
            now=NOW,
            receipt_id="receipt_fixed",
            nonce="nonce_fixed",
        )
        second = build_receipt_record(
            self.draft(),
            now=NOW,
            receipt_id="receipt_fixed",
            nonce="nonce_fixed",
        )

        self.assertEqual(first, second)
        self.assertEqual(first["expires_at"], "2026-09-23T03:05:00+00:00")
        self.assertEqual(len(first["receipt_digest"]), 64)
        self.assertEqual(first["receipt_digest"], first["receipt_digest"].lower())

    def test_canonical_payload_contains_only_bound_execution_facts(self):
        record = build_receipt_record(
            self.draft(),
            now=NOW,
            receipt_id="receipt_fixed",
            nonce="nonce_fixed",
        )

        self.assertEqual(
            record["canonical_payload"],
            {
                "amount_minor": 500,
                "beneficiary_id": "beneficiary_alicia_powell_01",
                "contract_version": 2,
                "currency": "SGD",
                "destination_kind": "SAVED_BENEFICIARY",
                "destination_reference": "beneficiary_alicia_powell_01",
                "evidence_bundle": [],
                "expires_at": "2026-09-23T03:05:00+00:00",
                "lookup_reference": None,
                "nonce": "nonce_fixed",
                "original_text": "I owe Alicia Powell five dollars",
                "receipt_id": "receipt_fixed",
                "recipient_display_name": "Alicia Powell",
                "request_id": "00000000-0000-4000-8000-000000000030",
                "source_account_id": "acct_savings_01",
                "user_id": "demo_user_01",
            },
        )
        self.assertNotIn("raw_model_output", record["canonical_payload"])
        self.assertNotIn("status", record["canonical_payload"])

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

    def test_v2_payload_binds_destination_lookup_and_evidence(self):
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
            destination_account_id="acct_paynow_jxxx_01",
            lookup_reference="lookup_fixed",
            evidence_bundle=(
                {
                    "turn": 1,
                    "text": "PayNow 8695 2933 ten dollars",
                    "amount": "ten dollars",
                    "proxy": "8695 2933",
                },
            ),
        )
        record = build_receipt_record(
            draft,
            now=NOW,
            receipt_id="receipt_fixed",
            nonce="nonce_fixed",
        )

        self.assertEqual(record["canonical_payload"]["contract_version"], 2)
        self.assertEqual(
            record["canonical_payload"]["destination_kind"], "PAYNOW_MOBILE"
        )
        self.assertEqual(
            record["canonical_payload"]["destination_reference"], "86952933"
        )
        self.assertEqual(record["canonical_payload"]["lookup_reference"], "lookup_fixed")
        self.assertEqual(
            record["canonical_payload"]["destination_account_id"],
            "acct_paynow_jxxx_01",
        )
        self.assertEqual(
            record["canonical_payload"]["evidence_bundle"],
            [
                {
                    "amount": "ten dollars",
                    "proxy": "8695 2933",
                    "text": "PayNow 8695 2933 ten dollars",
                    "turn": 1,
                }
            ],
        )

        mutations = (
            dataclasses.replace(draft, amount_minor=1100),
            dataclasses.replace(draft, destination_reference="86952934"),
            dataclasses.replace(draft, recipient_display_name="AXXX PXXXX"),
            dataclasses.replace(draft, lookup_reference="lookup_other"),
            dataclasses.replace(
                draft,
                evidence_bundle=({"turn": 1, "text": "changed evidence"},),
            ),
        )
        for mutated in mutations:
            with self.subTest(mutated=mutated):
                changed = build_receipt_record(
                    mutated,
                    now=NOW,
                    receipt_id="receipt_fixed",
                    nonce="nonce_fixed",
                )
                self.assertNotEqual(record["receipt_digest"], changed["receipt_digest"])


if __name__ == "__main__":
    unittest.main()

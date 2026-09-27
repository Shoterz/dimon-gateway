import dataclasses
import unittest
from datetime import datetime, timezone

from apps.dimon_live.batch_receipts import (
    BatchReceiptDraft,
    BatchReceiptItemDraft,
    build_batch_receipt_record,
)


NOW = datetime(2026, 9, 24, 3, 0, tzinfo=timezone.utc)


class BatchReceiptContractTests(unittest.TestCase):
    def draft(self):
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

    def test_digest_binds_ordered_items_and_total(self):
        record = build_batch_receipt_record(
            self.draft(),
            now=NOW,
            receipt_id="batch_receipt_fixed",
            nonce="batch_nonce_fixed",
        )
        self.assertEqual(record["canonical_payload"]["total_minor"], 1050)
        self.assertEqual(
            [item["amount_minor"] for item in record["canonical_payload"]["items"]],
            [650, 400],
        )
        reversed_items = dataclasses.replace(
            self.draft(), items=tuple(reversed(self.draft().items))
        )
        changed = build_batch_receipt_record(
            reversed_items,
            now=NOW,
            receipt_id="batch_receipt_fixed",
            nonce="batch_nonce_fixed",
        )
        self.assertNotEqual(record["receipt_digest"], changed["receipt_digest"])

    def test_invalid_batch_receipt_inputs_are_rejected(self):
        draft = self.draft()
        cases = (
            dataclasses.replace(
                draft,
                items=(
                    dataclasses.replace(draft.items[0], position=1),
                    draft.items[1],
                ),
            ),
            dataclasses.replace(
                draft,
                items=(draft.items[0], dataclasses.replace(
                    draft.items[1],
                    beneficiary_id=draft.items[0].beneficiary_id,
                )),
            ),
            dataclasses.replace(
                draft,
                items=(dataclasses.replace(draft.items[0], amount_minor=0), draft.items[1]),
            ),
            dataclasses.replace(draft, currency="USD"),
            dataclasses.replace(draft, items=(draft.items[0],)),
        )
        for invalid_draft in cases:
            with self.subTest(draft=invalid_draft):
                with self.assertRaises(ValueError):
                    build_batch_receipt_record(
                        invalid_draft,
                        now=NOW,
                        receipt_id="batch_receipt_fixed",
                        nonce="batch_nonce_fixed",
                    )
        with self.assertRaises(ValueError):
            build_batch_receipt_record(
                draft,
                now=NOW.replace(tzinfo=None),
                receipt_id="batch_receipt_fixed",
                nonce="batch_nonce_fixed",
            )


if __name__ == "__main__":
    unittest.main()

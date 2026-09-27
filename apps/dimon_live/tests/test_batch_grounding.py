import unittest

from apps.dimon_live.batch_grounding import bind_batch_evidence
from apps.dimon_live.grounding import GroundingError


class BatchGroundingTests(unittest.TestCase):
    def test_binds_recipient_first_and_amount_first_by_text_order(self):
        cases = (
            (
                "Send John Smith $6.50 and Alicia Powell $4",
                ("John Smith", "Alicia Powell"),
            ),
            (
                "Send $6.50 to John Smith and $4 to Alicia Powell",
                ("John Smith", "Alicia Powell"),
            ),
        )
        for text, recipients in cases:
            with self.subTest(text=text):
                bound = bind_batch_evidence(text, recipients)
                self.assertEqual(
                    [
                        (item.recipient_evidence, item.amount_minor)
                        for item in bound
                    ],
                    [("John Smith", 650), ("Alicia Powell", 400)],
                )

    def test_rejects_count_mismatch_and_repeated_evidence(self):
        cases = (
            (
                "Send John Smith and Alicia Powell $4",
                ("John Smith", "Alicia Powell"),
                "BATCH_RECIPIENT_COUNT_MISMATCH",
            ),
            (
                "Send John Smith $5 and John Smith $7",
                ("John Smith", "John Smith"),
                "AMBIGUOUS_BATCH_BINDING",
            ),
        )
        for text, recipients, reason in cases:
            with self.subTest(text=text):
                with self.assertRaisesRegex(GroundingError, reason):
                    bind_batch_evidence(text, recipients)


if __name__ == "__main__":
    unittest.main()

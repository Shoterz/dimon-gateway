import unittest

from apps.dimon_live.grounding import (
    derive_amount_from_text,
    derive_amount_mentions_from_text,
    GroundingError,
    is_grounded,
    parse_sgd_amount,
    verify_amount,
    verify_optional_reference,
)


class GroundingTests(unittest.TestCase):
    def test_amount_mentions_preserve_values_evidence_and_spans(self):
        text = "Send John Smith S$6.50 and Alicia Powell four dollars"
        mentions = derive_amount_mentions_from_text(text)
        self.assertEqual(
            [(item.amount_minor, item.evidence) for item in mentions],
            [(650, "S$6.50"), (400, "four dollars")],
        )
        self.assertEqual(
            [text[item.start:item.end] for item in mentions],
            ["S$6.50", "four dollars"],
        )

    def test_numeric_and_word_amounts_become_exact_minor_units(self):
        cases = (
            ("S$10", 1000),
            ("$2.50", 250),
            ("1,234.56 SGD", 123456),
            ("10 dollars", 1000),
            ("ten dollars", 1000),
            ("twenty one dollars", 2100),
            ("one hundred and five dollars", 10500),
            ("two thousand dollars", 200000),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    parse_sgd_amount(text, require_full_match=True),
                    expected,
                )

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
        with self.assertRaisesRegex(
            GroundingError,
            "CLARIFICATION_SCOPE_VIOLATION",
        ):
            parse_sgd_amount(
                "send John ten dollars",
                require_full_match=True,
            )

    def test_evidence_matching_folds_case_unicode_and_whitespace(self):
        self.assertTrue(
            is_grounded(
                "Pay ALÍCIA   ten dollars",
                "alícia ten dollars",
            )
        )

    def test_evidence_must_end_on_a_token_boundary(self):
        self.assertFalse(is_grounded("Send Alicia Powells $10", "Alicia Powell"))
        self.assertFalse(is_grounded("Send Alicia $100", "$10"))

    def test_conflicting_amounts_are_rejected_even_when_one_is_cited(self):
        for text in (
            "Send Alicia $10 or $100",
            "Send Alicia $10 or $10",
            "Send Alicia $10 or USD 100",
        ):
            with self.subTest(text=text):
                with self.assertRaisesRegex(GroundingError, "AMBIGUOUS_AMOUNT"):
                    verify_amount(text, 1000, "$10")

    def test_server_derived_amount_rejects_negative_user_text(self):
        for text in (
            "Send Alicia -$5",
            "Send Alicia -5 dollars",
            "Send Alicia negative five dollars",
        ):
            with self.subTest(text=text):
                with self.assertRaisesRegex(GroundingError, "INVALID_AMOUNT"):
                    derive_amount_from_text(text)

    def test_invalid_and_ambiguous_amounts_have_stable_codes(self):
        cases = (
            ("zero dollars", "INVALID_AMOUNT"),
            ("-5 dollars", "INVALID_AMOUNT"),
            ("$2.345", "INVALID_AMOUNT"),
            ("10 USD", "UNSUPPORTED_CURRENCY"),
            ("10 or 20 dollars", "AMBIGUOUS_AMOUNT"),
        )
        for text, reason in cases:
            with self.subTest(text=text):
                with self.assertRaisesRegex(GroundingError, reason):
                    parse_sgd_amount(text, require_full_match=True)

    def test_optional_reference_is_kept_only_with_grounded_pair(self):
        self.assertEqual(
            verify_optional_reference(
                "send Alicia $10 for flowers",
                "flowers",
                "for flowers",
            ),
            "flowers",
        )
        self.assertIsNone(
            verify_optional_reference(
                "send Alicia $10",
                "flowers",
                "for flowers",
            )
        )
        self.assertIsNone(
            verify_optional_reference("send Alicia $10", None, None)
        )


if __name__ == "__main__":
    unittest.main()

import unittest

from apps.dimon_live.bill_payment_grounding import (
    BillPaymentGroundingError,
    prepare_bill_payment,
)


def proposal(**changes):
    value = {
        "contract_version": 1,
        "kind": "BILL_PAYMENT",
        "biller_label": "Singapore Power",
        "biller_evidence": "Singapore Power bill",
        "amount_evidence": "$120",
        "currency": "SGD",
        "currency_evidence": "$",
        "source_account_label": None,
        "source_account_evidence": None,
        "requested_execution_date": None,
        "execution_date_evidence": None,
    }
    value.update(changes)
    return value


class BillPaymentGroundingTests(unittest.TestCase):
    def test_complete_proposal_is_parsed_and_amount_is_server_derived(self):
        result = prepare_bill_payment(
            "Pay my Singapore Power bill $120.",
            proposal(),
        )

        self.assertEqual(result.status, "READY")
        self.assertEqual(result.amount_minor, 12_000)
        self.assertEqual(result.proposal.biller_label, "Singapore Power")
        self.assertEqual(result.question, None)

    def test_missing_required_fields_return_one_stable_clarification(self):
        cases = (
            (
                proposal(biller_label=None, biller_evidence=None),
                "BILLER_REQUIRED",
                "Which bill would you like to pay?",
            ),
            (
                proposal(amount_evidence=None),
                "AMOUNT_REQUIRED",
                "How much should I pay?",
            ),
            (
                proposal(currency_evidence=None),
                "CURRENCY_REQUIRED",
                "Which currency should I use?",
            ),
        )
        for raw, status, question in cases:
            with self.subTest(status=status):
                result = prepare_bill_payment("Pay my bill.", raw)
                self.assertEqual((result.status, result.question), (status, question))
                self.assertIsNone(result.proposal)

    def test_not_bill_payment_is_kept_out_of_the_bill_flow(self):
        raw = {key: None for key in proposal()}
        raw.update({"contract_version": 1, "kind": "NOT_BILL_PAYMENT"})

        result = prepare_bill_payment("Send John $10.", raw)

        self.assertEqual(result.status, "NOT_BILL_PAYMENT")
        self.assertIsNone(result.proposal)

    def test_invented_or_mismatched_evidence_is_rejected(self):
        cases = (
            ("Pay my SP bill $120.", proposal(), "UNGROUNDED_BILLER"),
            (
                "Pay my Singapore Power bill $120.",
                proposal(amount_evidence="$500"),
                "UNGROUNDED_AMOUNT",
            ),
            (
                "Pay my Singapore Power bill $120.",
                proposal(source_account_label="savings", source_account_evidence="savings"),
                "UNGROUNDED_SOURCE_ACCOUNT",
            ),
        )
        for text, raw, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(BillPaymentGroundingError, reason):
                    prepare_bill_payment(text, raw)

    def test_multiple_amounts_fail_closed(self):
        with self.assertRaisesRegex(BillPaymentGroundingError, "AMBIGUOUS_AMOUNT"):
            prepare_bill_payment(
                "Pay my Singapore Power bill $120 or $150.",
                proposal(),
            )

    def test_explicit_source_and_iso_date_must_be_grounded(self):
        result = prepare_bill_payment(
            "Pay my Singapore Power bill S$120 from savings on 2026-10-10.",
            proposal(
                amount_evidence="S$120",
                currency_evidence="S$",
                source_account_label="savings",
                source_account_evidence="savings",
                requested_execution_date="2026-10-10",
                execution_date_evidence="2026-10-10",
            ),
        )

        self.assertEqual(result.status, "READY")
        self.assertEqual(result.proposal.requested_execution_date, "2026-10-10")

    def test_currency_marker_is_recovered_only_from_grounded_amount_evidence(self):
        result = prepare_bill_payment(
            "Pay my Singapore Power bill $120.",
            proposal(currency_evidence="SGD"),
        )

        self.assertEqual(result.status, "READY")
        self.assertEqual(result.proposal.currency_evidence, "$")


if __name__ == "__main__":
    unittest.main()

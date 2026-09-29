"""KYC, value, velocity, and friction tiers at the execution gateway.

Tiers (the checks only ever get stricter):
  saved payee under S$200         device signature
  saved payee S$200 - S$2,000     + spoken read-back
  new payee, or over S$2,000      + hardware-token code
  unusual amount for the payee    + explicit confirmation
  new payee, first 24 hours       capped at S$100
Every rule is checked at challenge time and again inside execution.
"""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apps.dimon_live.database import Database, ReceiptRejected
from apps.dimon_live.receipts import ReceiptDraft
from apps.dimon_live.risk_policy import token_code
from apps.dimon_live.signing_store import issue_challenge
from apps.dimon_live.tests.device_signer import SoftwareAuthenticator

NOW = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)  # 12:00 in Singapore
PAYEES = {
    "alicia": ("beneficiary_alicia_powell_01", "Alicia Powell"),   # usually S$5-15
    "john": ("beneficiary_john_smith_01", "John Smith"),           # usually S$20-50
    "tan": ("beneficiary_john_tan_01", "John Tan"),
}


class RiskPolicyGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "ledger.sqlite3"
        self.database = Database(self.path, clock=lambda: NOW)
        self.database.initialize()
        self.device = SoftwareAuthenticator()
        self.device.enroll(self.database)
        self.sequence = 0

    def tearDown(self):
        self.temp_dir.cleanup()

    def sql(self, statement, params=()):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            return connection.execute(statement, params).fetchall()

    def fund(self, minor):
        self.sql("UPDATE accounts SET balance_minor = ? WHERE account_id = 'acct_savings_01'", (minor,))

    def usually_pay_john(self, minor):
        """Give John Smith a history of payments this size, so they're normal."""
        for days in (8, 20):
            self.sql(
                "INSERT INTO prior_payments VALUES (?, 'beneficiary_john_smith_01', ?, ?)",
                (f"hist_{days}_{minor}", minor, (NOW - timedelta(days=days)).isoformat()),
            )

    def make_new(self, payee="tan"):
        self.sql("DELETE FROM prior_payments WHERE beneficiary_id = ?", (PAYEES[payee][0],))

    def pending(self, amount_minor, payee="alicia", *, now=NOW):
        self.sequence += 1
        beneficiary_id, name = PAYEES[payee]
        text = f"Send {name} S${amount_minor / 100:.2f}"
        draft = ReceiptDraft(
            request_id=f"00000000-0000-4000-8000-{self.sequence:012d}",
            original_text=text,
            user_id="demo_user_01",
            source_account_id="acct_savings_01",
            beneficiary_id=beneficiary_id,
            recipient_display_name=name,
            amount_minor=amount_minor,
            currency="SGD",
            evidence_bundle=({"turn": 1, "text": text},),
        )
        return self.database.create_pending_receipts(
            (draft,),
            raw_model_output="{}",
            validated_intent={},
            reason_code="SIGNED_APPROVAL_REQUIRED",
            response_base={"outcome": "CLARIFICATION_REQUEST", "trace": []},
            now=now,
        )["receipt_choices"][0]

    def challenge(self, choice, *, now=NOW):
        return self.database.issue_approval_challenge(
            "single", choice["receipt_id"], choice["receipt_digest"], rp_id=self.device.rp_id, now=now
        )

    def raw_challenge(self, choice, *, now=NOW):
        """A challenge that skips the challenge-time policy check, as a
        compromised component would, to prove the execution-time check."""
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            challenge = issue_challenge(
                connection,
                purpose="APPROVE_RECEIPT",
                user_id="demo_user_01",
                now=now,
                receipt_id=choice["receipt_id"],
                receipt_digest=choice["receipt_digest"],
            )
            connection.commit()
        return challenge

    def approve(self, choice, challenge, *, code=None, read_back=False, confirm_unusual=False, now=NOW):
        return self.database.approve_receipt(
            choice["receipt_id"],
            choice["receipt_digest"],
            "demo_user_01",
            assertion=self.device.sign(challenge),
            context=self.device.context,
            out_of_band_code=code,
            read_back_played=read_back,
            unusual_amount_confirmed=confirm_unusual,
            now=now,
        )

    def code_for(self, choice):
        secret = self.sql("SELECT token_secret FROM customer_profiles")[0][0]
        return token_code(secret, choice["receipt_id"], choice["receipt_digest"])

    def execute(self, amount_minor, payee="alicia", *, now=NOW):
        """Approve with every check the tier asks for."""
        choice = self.pending(amount_minor, payee, now=now)
        review = self.challenge(choice, now=now)["review"]
        return self.approve(
            choice,
            self.challenge(choice, now=now),
            code=self.code_for(choice) if review["requires_out_of_band"] else None,
            read_back=review["requires_read_back"],
            confirm_unusual=review["requires_unusual_confirmation"],
            now=now,
        )

    def transactions(self):
        return len(self.database.get_state()["transactions"])

    # KYC --------------------------------------------------------------------
    def test_unverified_or_expired_kyc_blocks_every_movement(self):
        for status, expiry, reason in (
            ("PENDING", (NOW + timedelta(days=1)).isoformat(), "KYC_PENDING"),
            ("VERIFIED", (NOW - timedelta(seconds=1)).isoformat(), "KYC_EXPIRED"),
        ):
            with self.subTest(reason=reason):
                self.sql("UPDATE customer_profiles SET kyc_status = 'VERIFIED', kyc_expires_at = ?", ((NOW + timedelta(days=1)).isoformat(),))
                choice = self.pending(500)
                challenge = self.challenge(choice)
                self.sql("UPDATE customer_profiles SET kyc_status = ?, kyc_expires_at = ?", (status, expiry))
                with self.assertRaisesRegex(ReceiptRejected, reason):
                    self.challenge(choice)
                with self.assertRaisesRegex(ReceiptRejected, reason):
                    self.approve(choice, challenge)
        self.assertEqual(self.transactions(), 0)

    # Tiers ------------------------------------------------------------------
    def test_small_transfer_to_a_saved_payee_needs_only_the_signature(self):
        choice = self.pending(1_500)
        review = self.challenge(choice)["review"]
        self.assertEqual(review["policy"]["tier"], "SIGN")
        self.assertFalse(review["requires_read_back"] or review["requires_out_of_band"])
        self.assertEqual(self.approve(choice, self.challenge(choice))["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")

    def test_s200_to_s2000_needs_a_read_back(self):
        self.usually_pay_john(40_000)
        choice = self.pending(30_000, "john")
        review = self.challenge(choice)["review"]
        self.assertEqual(review["policy"]["tier"], "SIGN_AND_READ_BACK")
        self.assertFalse(review["requires_out_of_band"])
        with self.assertRaisesRegex(ReceiptRejected, "READ_BACK_REQUIRED"):
            self.approve(choice, self.challenge(choice))
        result = self.approve(choice, self.challenge(choice), read_back=True)
        self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")

    def test_over_s2000_needs_the_token_and_the_read_back(self):
        self.fund(1_000_000)
        self.usually_pay_john(250_000)
        choice = self.pending(250_000, "john")
        review = self.challenge(choice)["review"]
        self.assertEqual(review["policy"]["tier"], "SIGN_AND_TOKEN")
        self.assertTrue(review["requires_read_back"])
        with self.assertRaisesRegex(ReceiptRejected, "OUT_OF_BAND_REQUIRED"):
            self.approve(choice, self.challenge(choice), read_back=True)
        result = self.approve(choice, self.challenge(choice), read_back=True, code=self.code_for(choice))
        self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")

    def test_new_payee_needs_the_token_and_is_capped_for_24_hours(self):
        self.make_new("tan")
        over_cap = self.pending(10_001, "tan")
        with self.assertRaisesRegex(ReceiptRejected, "NEW_PAYEE_LIMIT_EXCEEDED"):
            self.challenge(over_cap)
        with self.assertRaisesRegex(ReceiptRejected, "NEW_PAYEE_LIMIT_EXCEEDED"):
            self.approve(over_cap, self.raw_challenge(over_cap), code=self.code_for(over_cap))
        choice = self.pending(5_000, "tan")
        review = self.challenge(choice)["review"]
        self.assertTrue(review["requires_out_of_band"])
        self.assertTrue(review["items"][0]["new_payee"])
        self.assertEqual(self.execute(5_000, "tan")["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
        # Still new within 24 hours of the first payment; the cap lifts after.
        later_today = self.pending(15_000, "tan", now=NOW + timedelta(hours=2))
        with self.assertRaisesRegex(ReceiptRejected, "NEW_PAYEE_LIMIT_EXCEEDED"):
            self.challenge(later_today, now=NOW + timedelta(hours=2))
        next_day = NOW + timedelta(hours=25)
        self.assertEqual(self.execute(15_000, "tan", now=next_day)["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")

    def test_unusual_amount_needs_explicit_confirmation(self):
        # Alicia is usually paid S$5-15; S$50 is "fifteen" misheard as "fifty".
        choice = self.pending(5_000)
        review = self.challenge(choice)["review"]
        self.assertTrue(review["requires_unusual_confirmation"])
        self.assertEqual(review["items"][0]["unusual_amount"]["typical_max_minor"], 1_500)
        with self.assertRaisesRegex(ReceiptRejected, "UNUSUAL_AMOUNT_UNCONFIRMED"):
            self.approve(choice, self.challenge(choice), read_back=True)
        result = self.approve(choice, self.challenge(choice), read_back=True, confirm_unusual=True)
        self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")

    # Limits -----------------------------------------------------------------
    def test_per_transfer_limit(self):
        self.fund(1_000_000)
        over = self.pending(500_001, "john")
        with self.assertRaisesRegex(ReceiptRejected, "AMOUNT_LIMIT_EXCEEDED"):
            self.challenge(over)
        with self.assertRaisesRegex(ReceiptRejected, "AMOUNT_LIMIT_EXCEEDED"):
            self.approve(over, self.raw_challenge(over), code=self.code_for(over), read_back=True, confirm_unusual=True)
        self.assertEqual(self.transactions(), 0)

    def test_daily_cumulative_limit_uses_the_singapore_day(self):
        self.fund(2_000_000)
        self.usually_pay_john(480_000)
        self.execute(480_000, "john")
        self.execute(480_000, "john", now=NOW + timedelta(minutes=1))
        third = self.pending(50_000, "john", now=NOW + timedelta(minutes=2))
        with self.assertRaisesRegex(ReceiptRejected, "DAILY_LIMIT_EXCEEDED"):
            self.challenge(third, now=NOW + timedelta(minutes=2))
        tomorrow = NOW.replace(hour=16, minute=30)  # 00:30 the next day in Singapore
        self.assertEqual(self.execute(50_000, "john", now=tomorrow)["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")

    def test_more_than_five_transfers_in_ten_minutes_are_paused(self):
        for index in range(5):
            self.execute(2_000, "john", now=NOW + timedelta(seconds=index))
        sixth = self.pending(2_000, "john")
        with self.assertRaisesRegex(ReceiptRejected, "VELOCITY_LIMIT_EXCEEDED"):
            self.challenge(sixth, now=NOW + timedelta(seconds=10))
        later = NOW + timedelta(minutes=11)
        self.assertEqual(self.execute(2_000, "john", now=later)["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")

    def test_small_frequent_transfers_are_rate_limited_first(self):
        for index in range(3):
            self.execute(500, now=NOW + timedelta(seconds=index))
        fourth_small = self.pending(500)
        with self.assertRaisesRegex(ReceiptRejected, "MICRO_TRANSFER_RATE_LIMITED"):
            self.challenge(fourth_small, now=NOW + timedelta(seconds=5))
        self.assertEqual(
            self.execute(2_000, "john", now=NOW + timedelta(seconds=6))["outcome"],
            "EXECUTED_SYNTHETIC_TRANSFER",
        )

    # Hardware token ---------------------------------------------------------
    def test_token_code_only_confirms_its_own_receipt(self):
        self.make_new("tan")
        choice = self.pending(5_000, "tan")
        other = self.pending(4_000, "tan")
        with self.assertRaisesRegex(ReceiptRejected, "OUT_OF_BAND_REQUIRED"):
            self.approve(choice, self.challenge(choice))
        with self.assertRaisesRegex(ReceiptRejected, "OUT_OF_BAND_CODE_INVALID"):
            self.approve(choice, self.challenge(choice), code=self.code_for(other))
        self.assertEqual(self.transactions(), 0)
        result = self.approve(choice, self.challenge(choice), code=self.code_for(choice))
        self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")

    def test_mock_token_shows_codes_only_for_receipts_that_need_one(self):
        self.make_new("tan")
        needs_token = self.pending(5_000, "tan")
        self.pending(1_500)  # saved payee, small: no token
        self.assertEqual(
            self.database.mock_token_display(now=NOW),
            [{
                "receipt_id": needs_token["receipt_id"],
                "payee": "John Tan",
                "total_minor": 5_000,
                "code": self.code_for(needs_token),
            }],
        )

    def test_token_locks_after_three_wrong_codes(self):
        self.make_new("tan")
        choice = self.pending(5_000, "tan")
        correct = self.code_for(choice)
        wrong = f"{(int(correct) + 1) % 1_000_000:06d}"
        for _ in range(3):
            with self.assertRaisesRegex(ReceiptRejected, "OUT_OF_BAND_CODE_INVALID"):
                self.approve(choice, self.challenge(choice), code=wrong)
        with self.assertRaisesRegex(ReceiptRejected, "OUT_OF_BAND_LOCKED"):
            self.approve(choice, self.challenge(choice), code=correct)
        self.assertEqual(self.transactions(), 0)

    # Draft-time preview -----------------------------------------------------
    def test_policy_preview_reports_the_tier_before_signing(self):
        self.fund(1_000_000)
        blocked = self.pending(600_000, "john")
        self.assertEqual(
            self.database.policy_for_receipt("single", blocked["receipt_id"]),
            {"allowed": False, "reason_code": "AMOUNT_LIMIT_EXCEEDED"},
        )
        preview = self.database.policy_for_receipt("single", self.pending(5_000)["receipt_id"])
        self.assertTrue(preview["allowed"])
        self.assertEqual(preview["tier"], "SIGN_AND_READ_BACK")
        self.assertTrue(preview["requires_unusual_confirmation"])


if __name__ == "__main__":
    unittest.main()

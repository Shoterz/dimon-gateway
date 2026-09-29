"""KYC, value, velocity, and out-of-band controls at the execution gateway.

Every control has an allow case and a block case, and each block is checked
again inside the executing transaction so a change after review cannot slip
a movement through.
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

NOW = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)  # 12:00 SGT


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

    def pending(self, amount_minor, *, now=NOW):
        self.sequence += 1
        text = f"Send Alicia Powell S${amount_minor / 100:.2f}"
        draft = ReceiptDraft(
            request_id=f"00000000-0000-4000-8000-{self.sequence:012d}",
            original_text=text,
            user_id="demo_user_01",
            source_account_id="acct_savings_01",
            beneficiary_id="beneficiary_alicia_powell_01",
            recipient_display_name="Alicia Powell",
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
        """Issue a challenge without the challenge-time policy check, as a
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

    def approve(self, choice, challenge, *, code=None, now=NOW):
        return self.database.approve_receipt(
            choice["receipt_id"],
            choice["receipt_digest"],
            "demo_user_01",
            assertion=self.device.sign(challenge),
            context=self.device.context,
            out_of_band_code=code,
            now=now,
        )

    def code_for(self, choice):
        secret = self.sql("SELECT token_secret FROM customer_profiles")[0][0]
        return token_code(secret, choice["receipt_id"], choice["receipt_digest"])

    def execute(self, amount_minor, *, now=NOW):
        choice = self.pending(amount_minor, now=now)
        code = self.code_for(choice) if amount_minor >= 20_000 else None
        return self.approve(choice, self.challenge(choice, now=now), code=code, now=now)

    def transactions(self):
        return len(self.database.get_state()["transactions"])

    # KYC ------------------------------------------------------------------
    def test_unverified_or_expired_kyc_blocks_every_movement(self):
        for status, expiry, reason in (
            ("PENDING", (NOW + timedelta(days=1)).isoformat(), "KYC_PENDING"),
            ("VERIFIED", (NOW - timedelta(seconds=1)).isoformat(), "KYC_EXPIRED"),
        ):
            with self.subTest(reason=reason):
                self.sql("UPDATE customer_profiles SET kyc_status = 'VERIFIED', kyc_expires_at = ?", ((NOW + timedelta(days=1)).isoformat(),))
                choice = self.pending(500)
                challenge = self.challenge(choice)  # KYC fine at review time
                self.sql("UPDATE customer_profiles SET kyc_status = ?, kyc_expires_at = ?", (status, expiry))
                with self.assertRaisesRegex(ReceiptRejected, reason):
                    self.challenge(choice)
                with self.assertRaisesRegex(ReceiptRejected, reason):
                    self.approve(choice, challenge)
        self.assertEqual(self.transactions(), 0)

    def test_verified_kyc_allows_a_small_transfer(self):
        self.assertEqual(self.execute(1500)["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")

    # Value limits ---------------------------------------------------------
    def test_per_transfer_limit(self):
        self.assertEqual(self.execute(50_000)["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
        over = self.pending(50_001)
        with self.assertRaisesRegex(ReceiptRejected, "AMOUNT_LIMIT_EXCEEDED"):
            self.challenge(over)
        with self.assertRaisesRegex(ReceiptRejected, "AMOUNT_LIMIT_EXCEEDED"):
            self.approve(over, self.raw_challenge(over), code=self.code_for(over))
        self.assertEqual(self.transactions(), 1)

    def test_daily_cumulative_limit_uses_the_singapore_day(self):
        self.execute(45_000)
        second = self.pending(40_000)
        with self.assertRaisesRegex(ReceiptRejected, "DAILY_LIMIT_EXCEEDED"):
            self.challenge(second)
        # 16:00 UTC is already the next day in Singapore.
        tomorrow = NOW.replace(hour=16, minute=30)
        self.assertEqual(self.execute(40_000, now=tomorrow)["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")

    # Velocity -------------------------------------------------------------
    def test_transfer_velocity_is_limited_per_window(self):
        for index in range(5):
            self.execute(2_000, now=NOW + timedelta(seconds=index))
        sixth = self.pending(2_000)
        with self.assertRaisesRegex(ReceiptRejected, "VELOCITY_LIMIT_EXCEEDED"):
            self.challenge(sixth, now=NOW + timedelta(seconds=10))
        later = NOW + timedelta(minutes=11)
        self.assertEqual(self.execute(2_000, now=later)["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")

    def test_small_frequent_transfers_are_rate_limited_first(self):
        for index in range(3):
            self.execute(500, now=NOW + timedelta(seconds=index))
        fourth_small = self.pending(500)
        with self.assertRaisesRegex(ReceiptRejected, "MICRO_TRANSFER_RATE_LIMITED"):
            self.challenge(fourth_small, now=NOW + timedelta(seconds=5))
        # A normal-sized transfer is still allowed inside the same window.
        self.assertEqual(
            self.execute(2_000, now=NOW + timedelta(seconds=6))["outcome"],
            "EXECUTED_SYNTHETIC_TRANSFER",
        )

    # Out-of-band confirmation ---------------------------------------------
    def test_high_value_transfer_needs_the_token_code_for_this_receipt(self):
        choice = self.pending(30_000)
        review = self.challenge(choice)
        self.assertTrue(review["review"]["requires_out_of_band"])
        with self.assertRaisesRegex(ReceiptRejected, "OUT_OF_BAND_REQUIRED"):
            self.approve(choice, review)

        other = self.pending(30_000)
        with self.assertRaisesRegex(ReceiptRejected, "OUT_OF_BAND_CODE_INVALID"):
            # A genuine code for a different receipt (dynamic linking).
            self.approve(choice, self.challenge(choice), code=self.code_for(other))
        self.assertEqual(self.transactions(), 0)

        result = self.approve(choice, self.challenge(choice), code=self.code_for(choice))
        self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")

    def test_mock_token_shows_the_code_with_the_amount_and_payee(self):
        choice = self.pending(30_000)
        shown = self.database.mock_token_display(now=NOW)
        self.assertEqual(
            shown,
            [{
                "receipt_id": choice["receipt_id"],
                "payee": "Alicia Powell",
                "total_minor": 30_000,
                "code": self.code_for(choice),
            }],
        )
        self.pending(1_500)  # below the threshold: never shown
        self.assertEqual(len(self.database.mock_token_display(now=NOW)), 1)

    def test_token_locks_after_three_wrong_codes(self):
        choice = self.pending(30_000)
        correct = self.code_for(choice)
        wrong = f"{(int(correct) + 1) % 1_000_000:06d}"
        for _ in range(3):
            with self.assertRaisesRegex(ReceiptRejected, "OUT_OF_BAND_CODE_INVALID"):
                self.approve(choice, self.challenge(choice), code=wrong)
        with self.assertRaisesRegex(ReceiptRejected, "OUT_OF_BAND_LOCKED"):
            self.approve(choice, self.challenge(choice), code=correct)
        self.assertEqual(self.transactions(), 0)

    def test_small_transfer_needs_no_token(self):
        choice = self.pending(19_999)
        self.assertFalse(self.challenge(choice)["review"]["requires_out_of_band"])

    # Draft-time preview ---------------------------------------------------
    def test_policy_preview_reports_before_signing(self):
        self.assertEqual(self.database.policy_preview((60_000,)), {"allowed": False, "reason_code": "AMOUNT_LIMIT_EXCEEDED"})
        preview = self.database.policy_preview((25_000,))
        self.assertTrue(preview["allowed"])
        self.assertTrue(preview["requires_out_of_band"])


if __name__ == "__main__":
    unittest.main()

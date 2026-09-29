"""Adversarial tests for the zero-trust approval gateway.

Every case is an attempt to move money without the enrolled user's device
signing the exact receipt. Each must be refused with a stable reason code and
leave the ledger untouched.
"""

import dataclasses
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from apps.dimon_live.approval_gateway import ApprovalRejected
from apps.dimon_live.database import Database, ReceiptRejected
from apps.dimon_live.receipts import ReceiptDraft
from apps.dimon_live.tests.device_signer import SoftwareAuthenticator

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


class SignedApprovalGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "ledger.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()
        self.device = SoftwareAuthenticator()
        self.device.enroll(self.database)

    def tearDown(self):
        self.temp_dir.cleanup()

    def pending(self, request_id="00000000-0000-4000-8000-000000000501", amount_minor=500):
        draft = ReceiptDraft(
            request_id=request_id,
            original_text=f"Send Alicia Powell {amount_minor} cents",
            user_id="demo_user_01",
            source_account_id="acct_savings_01",
            beneficiary_id="beneficiary_alicia_powell_01",
            recipient_display_name="Alicia Powell",
            amount_minor=amount_minor,
            currency="SGD",
        )
        return self.database.create_pending_receipts(
            (draft,),
            raw_model_output="{}",
            validated_intent={},
            reason_code="SIGNED_APPROVAL_REQUIRED",
            response_base={"outcome": "CLARIFICATION_REQUEST", "trace": []},
            now=NOW,
        )["receipt_choices"][0]

    def challenge(self, choice, *, now=NOW):
        return self.database.issue_approval_challenge(
            "single",
            choice["receipt_id"],
            choice["receipt_digest"],
            rp_id=self.device.rp_id,
            now=now,
        )

    def approve(self, choice, assertion, *, now=NOW, context=None):
        return self.database.approve_receipt(
            choice["receipt_id"],
            choice["receipt_digest"],
            "demo_user_01",
            assertion=assertion,
            context=context or self.device.context,
            now=now,
        )

    def assert_refused(self, choice, assertion, reason, **kwargs):
        with self.assertRaisesRegex(ReceiptRejected, reason):
            self.approve(choice, assertion, **kwargs)
        self.assertEqual(self.database.get_state()["transactions"], [])
        self.assertEqual(
            self.database.get_receipt(choice["receipt_id"])["status"],
            "AWAITING_CONFIRMATION",
        )

    def test_genuine_signature_executes_exactly_once(self):
        choice = self.pending()
        result = self.approve(choice, self.device.sign(self.challenge(choice)))
        self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
        self.assertEqual(len(self.database.get_state()["transactions"]), 1)

    def test_signature_from_an_unenrolled_key_is_refused(self):
        choice = self.pending()
        impostor = SoftwareAuthenticator()
        impostor.credential_id = self.device.credential_id  # claims the real credential
        self.assert_refused(
            choice, impostor.sign(self.challenge(choice)), "APPROVAL_SIGNATURE_INVALID"
        )

    def test_unknown_credential_is_refused(self):
        choice = self.pending()
        stranger = SoftwareAuthenticator()
        self.assert_refused(choice, stranger.sign(self.challenge(choice)), "CREDENTIAL_UNKNOWN")

    def test_user_verification_is_mandatory(self):
        choice = self.pending()
        # User present but not verified (no biometric / PIN).
        self.assert_refused(
            choice,
            self.device.sign(self.challenge(choice), flags=0x01),
            "APPROVAL_USER_NOT_VERIFIED",
        )
        choice = self.pending("00000000-0000-4000-8000-000000000502")
        self.assert_refused(
            choice,
            self.device.sign(self.challenge(choice), flags=0x04),
            "APPROVAL_USER_NOT_PRESENT",
        )

    def test_signature_for_another_relying_party_is_refused(self):
        choice = self.pending()
        self.assert_refused(
            choice,
            self.device.sign(self.challenge(choice), rp_id="attacker.example"),
            "APPROVAL_RP_MISMATCH",
        )

    def test_signature_bound_to_another_receipt_cannot_be_moved(self):
        first = self.pending()
        second = self.pending("00000000-0000-4000-8000-000000000503", amount_minor=90000)
        assertion = self.device.sign(self.challenge(first))
        with self.assertRaisesRegex(ReceiptRejected, "APPROVAL_RECEIPT_MISMATCH"):
            self.approve(second, assertion)
        self.assertEqual(self.database.get_state()["transactions"], [])

    def test_forged_challenge_text_is_refused(self):
        choice = self.pending()
        challenge = self.challenge(choice)
        self.assert_refused(
            choice,
            self.device.sign(challenge, challenge_text="A" * 43),
            "APPROVAL_CHALLENGE_MISMATCH",
        )

    def test_client_data_edited_after_signing_breaks_the_signature(self):
        choice = self.pending()
        assertion = self.device.sign(self.challenge(choice))
        edited = json.loads(assertion.client_data_json)
        edited["tokenBinding"] = {"status": "not-supported"}
        tampered = dataclasses.replace(
            assertion, client_data_json=json.dumps(edited).encode("utf-8")
        )
        self.assert_refused(choice, tampered, "APPROVAL_SIGNATURE_INVALID")

    def test_registration_ceremony_cannot_be_replayed_as_approval(self):
        choice = self.pending()
        challenge = self.challenge(choice)
        assertion = self.device.sign(challenge)
        wrong_type = self.device.client_data("webauthn.create", challenge["challenge"])
        auth_data = assertion.authenticator_data
        signature = self.device.key.sign(
            auth_data + hashlib.sha256(wrong_type).digest(), ec.ECDSA(hashes.SHA256())
        )
        forged = dataclasses.replace(
            assertion, client_data_json=wrong_type, signature=signature
        )
        self.assert_refused(choice, forged, "APPROVAL_WRONG_CEREMONY")

    def test_challenge_expires(self):
        choice = self.pending()
        assertion = self.device.sign(self.challenge(choice))
        self.assert_refused(
            choice, assertion, "APPROVAL_CHALLENGE_EXPIRED", now=NOW + timedelta(seconds=121)
        )

    def test_failed_attempt_burns_the_challenge(self):
        choice = self.pending()
        challenge = self.challenge(choice)
        self.assert_refused(
            choice, self.device.sign(challenge, flags=0x01), "APPROVAL_USER_NOT_VERIFIED"
        )
        # A correct signature over the same, now spent, challenge is refused.
        self.assert_refused(choice, self.device.sign(challenge), "APPROVAL_CHALLENGE_REPLAYED")

    def test_cloned_authenticator_counter_is_detected(self):
        first = self.pending()
        self.approve(first, self.device.sign(self.challenge(first)))
        second = self.pending("00000000-0000-4000-8000-000000000504")
        challenge = self.challenge(second)
        self.device.counter = 0  # a clone replays from an old counter value
        with self.assertRaisesRegex(ReceiptRejected, "APPROVAL_COUNTER_REPLAY"):
            self.approve(second, self.device.sign(challenge))
        self.assertEqual(len(self.database.get_state()["transactions"]), 1)

    def test_context_from_a_different_origin_is_refused(self):
        choice = self.pending()
        assertion = self.device.sign(self.challenge(choice))
        foreign = dataclasses.replace(
            self.device.context, allowed_origins=frozenset({"https://phish.example"})
        )
        self.assert_refused(choice, assertion, "APPROVAL_ORIGIN_MISMATCH", context=foreign)

    def test_mock_device_key_uses_raw_webcrypto_signatures(self):
        mock = SoftwareAuthenticator()
        registration = self.database.issue_registration_challenge(now=NOW)
        client_data = mock.client_data("webauthn.create", registration["challenge"])
        auth_data = mock.authenticator_data()

        def raw_signature(message: bytes) -> bytes:
            der = mock.key.sign(message, ec.ECDSA(hashes.SHA256()))
            r, s = decode_dss_signature(der)
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")

        self.database.register_signing_credential(
            challenge_id=registration["challenge_id"],
            credential_kind="MOCK_DEVICE_KEY",
            credential_id=mock.credential_id,
            public_key_spki=mock.public_key_spki,
            algorithm=-7,
            client_data_json=client_data,
            authenticator_data=auth_data,
            signature=raw_signature(auth_data + hashlib.sha256(client_data).digest()),
            context=mock.context,
            now=NOW,
        )
        choice = self.pending()
        der_assertion = mock.sign(self.challenge(choice))
        raw = dataclasses.replace(
            der_assertion,
            signature=raw_signature(
                der_assertion.authenticator_data
                + hashlib.sha256(der_assertion.client_data_json).digest()
            ),
        )

        result = self.approve(choice, raw)

        self.assertEqual(result["approval"]["credential_kind"], "MOCK_DEVICE_KEY")

    def test_mock_device_key_registration_requires_proof_of_possession(self):
        mock = SoftwareAuthenticator()
        registration = self.database.issue_registration_challenge(now=NOW)
        with self.assertRaisesRegex(ApprovalRejected, "APPROVAL_SIGNATURE_INVALID"):
            self.database.register_signing_credential(
                challenge_id=registration["challenge_id"],
                credential_kind="MOCK_DEVICE_KEY",
                credential_id=mock.credential_id,
                public_key_spki=mock.public_key_spki,
                algorithm=-7,
                client_data_json=mock.client_data("webauthn.create", registration["challenge"]),
                authenticator_data=mock.authenticator_data(),
                signature=None,
                context=mock.context,
                now=NOW,
            )
        self.assertEqual(self.database.signing_credentials(mock.rp_id), [
            row for row in self.database.signing_credentials(mock.rp_id)
            if row["credential_id"] == self.device.credential_id
        ])

    def test_stored_evidence_can_be_reverified_independently(self):
        choice = self.pending()
        self.approve(choice, self.device.sign(self.challenge(choice)))
        with closing(sqlite3.connect(self.path)) as connection:
            client_data_b64, auth_b64, sig_b64 = connection.execute(
                "SELECT client_data_json, authenticator_data, signature FROM approval_signatures"
            ).fetchone()
        import base64

        client_data = base64.b64decode(client_data_b64)
        # An auditor holding only the public key can re-verify non-repudiably.
        self.device.key.public_key().verify(
            base64.b64decode(sig_b64),
            base64.b64decode(auth_b64) + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()),
        )
        self.assertEqual(json.loads(client_data)["type"], "webauthn.get")


if __name__ == "__main__":
    unittest.main()

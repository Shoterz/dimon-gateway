"""Face registration at KYC and face confirmation of high-tier transactions."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from apps.dimon_live import face_id
from apps.dimon_live.approval_gateway import ApprovalRejected
from apps.dimon_live.database import Database, ReceiptRejected
from apps.dimon_live.face_id import FaceRejected, OpenCvFaceEngine, model_paths
from apps.dimon_live.receipts import ReceiptDraft
from apps.dimon_live.tests.device_signer import SoftwareAuthenticator
from apps.dimon_live.tests.face_fake import FakeFaceEngine, live_frames

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
STILL_PHOTO = (0.1, 0.11, 0.1, 0.12, 0.1, 0.11)


class DefaultEngineTests(unittest.TestCase):
    def tearDown(self):
        face_id.default_engine.cache_clear()

    def test_missing_models_do_not_permanently_disable_the_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            detector = Path(directory) / "detector.onnx"
            recognizer = Path(directory) / "recognizer.onnx"
            engine = object()
            with (
                patch.object(face_id, "model_paths", return_value=(detector, recognizer)),
                patch.object(face_id, "OpenCvFaceEngine", return_value=engine) as constructor,
            ):
                self.assertIsNone(face_id.default_engine())
                detector.touch()
                recognizer.touch()

                self.assertIs(face_id.default_engine(), engine)
                self.assertIs(face_id.default_engine(), engine)
                constructor.assert_called_once_with(detector, recognizer)

    def test_temporary_load_failure_is_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            detector = Path(directory) / "detector.onnx"
            recognizer = Path(directory) / "recognizer.onnx"
            detector.touch()
            recognizer.touch()
            engine = object()
            with (
                patch.object(face_id, "model_paths", return_value=(detector, recognizer)),
                patch.object(
                    face_id, "OpenCvFaceEngine", side_effect=(OSError("file locked"), engine)
                ) as constructor,
            ):
                self.assertIsNone(face_id.default_engine())

                self.assertIs(face_id.default_engine(), engine)
                self.assertEqual(constructor.call_count, 2)


class FaceConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "ledger.sqlite3"
        self.database = Database(self.path, face_engine=FakeFaceEngine(), clock=lambda: NOW)
        self.database.initialize()
        self.device = SoftwareAuthenticator()
        self.device.enroll(self.database)

    def tearDown(self):
        self.temp_dir.cleanup()

    def pending(self, suffix=1, amount_minor=20_000, now=NOW):
        draft = ReceiptDraft(
            request_id=f"00000000-0000-4000-8000-{suffix:012d}",
            original_text=f"Send Alicia Powell {amount_minor} cents",
            user_id="demo_user_01",
            source_account_id="acct_savings_01",
            beneficiary_id="beneficiary_alicia_powell_01",
            recipient_display_name="Alicia Powell",
            amount_minor=amount_minor,
            currency="SGD",
        )
        return self.database.create_pending_receipts(
            (draft,), raw_model_output="{}", validated_intent={},
            reason_code="SIGNED_APPROVAL_REQUIRED",
            response_base={"outcome": "CLARIFICATION_REQUEST", "trace": []}, now=now,
        )["receipt_choices"][0]

    def challenge(self, choice):
        challenge = self.database.issue_approval_challenge(
            "single", choice["receipt_id"], choice["receipt_digest"], rp_id=self.device.rp_id, now=NOW
        )
        if challenge["review"]["requires_singpass"]:
            request = self.database.request_receipt_singpass_authorisation(
                "single", choice["receipt_id"], choice["receipt_digest"], now=NOW,
            )
            self.database.singpass_decide(request["auth_id"], True)
        return challenge

    def sign_without_face(self, challenge):
        database, self.device.database = self.device.database, None
        try:
            return self.device.sign(challenge)
        finally:
            self.device.database = database

    def approve(self, choice, assertion):
        return self.database.approve_receipt(
            choice["receipt_id"], choice["receipt_digest"], "demo_user_01",
            assertion=assertion, context=self.device.context,
            read_back_played=True, unusual_amount_confirmed=True,
            voice_confirmation="yes, confirm", now=NOW,
        )

    def assert_refused(self, choice, assertion, reason):
        with self.assertRaises(ReceiptRejected) as caught:
            self.approve(choice, assertion)
        self.assertEqual(caught.exception.reason_code, reason)
        self.assertEqual(self.database.get_state()["transactions"], [])

    def scan(self, challenge, frames):
        return self.database.verify_face(challenge["challenge_id"], frames, now=NOW)

    def test_the_registered_face_confirms_the_transaction(self):
        choice = self.pending()
        result = self.approve(choice, self.device.sign(self.challenge(choice)))
        self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
        self.assertTrue(result["approval"]["face_scan_id"].startswith("face_scan_"))
        self.assertGreaterEqual(result["approval"]["face_similarity"], 0.9)

    def test_face_data_lives_in_its_own_database(self):
        faces = self.path.with_name("ledger.faces.db")
        self.assertTrue(faces.exists())
        with closing(sqlite3.connect(self.path)) as ledger:
            tables = {row[0] for row in ledger.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(any(name.startswith("face") for name in tables))
        with closing(sqlite3.connect(faces)) as store:
            templates = store.execute("SELECT user_id, status, length(template) FROM face_templates").fetchall()
            columns = {row[1] for row in store.execute("PRAGMA table_info(face_scans)")}
        # One account, one face, stored as a 128-number template (no photo).
        self.assertEqual(templates, [("demo_user_01", "ACTIVE", 128 * 4)])
        self.assertFalse(columns & {"image", "frame", "photo"})

    def test_kyc_cannot_finish_without_a_registered_face(self):
        database = Database(Path(self.temp_dir.name) / "other.sqlite3", face_engine=FakeFaceEngine())
        database.initialize()
        device = SoftwareAuthenticator()
        consent = database.start_myinfo()
        challenge = database.issue_registration_challenge()
        with self.assertRaises(ApprovalRejected) as caught:
            database.complete_kyc_onboarding(
                consent_id=consent["consent_id"], challenge_id=challenge["challenge_id"],
                credential_kind="WEBAUTHN_PLATFORM", credential_id=device.credential_id,
                public_key_spki=device.public_key_spki, algorithm=-7,
                client_data_json=device.client_data("webauthn.create", challenge["challenge"]),
                authenticator_data=device.authenticator_data(), signature=None, context=device.context,
            )
        self.assertEqual(caught.exception.reason_code, "KYC_FACE_REQUIRED")
        self.assertNotEqual(database.kyc_status()["kyc_status"], "VERIFIED")

    def test_a_signature_without_a_face_scan_moves_nothing(self):
        choice = self.pending()
        self.assert_refused(choice, self.sign_without_face(self.challenge(choice)), "FACE_NOT_VERIFIED")

    def test_someone_else_s_face_is_refused(self):
        choice = self.pending()
        challenge = self.challenge(choice)
        with self.assertRaises(FaceRejected) as caught:
            self.scan(challenge, live_frames("stranger"))
        self.assertEqual(caught.exception.reason_code, "FACE_NO_MATCH")
        self.assert_refused(choice, self.sign_without_face(challenge), "FACE_NOT_VERIFIED")

    def test_liveness_and_frame_checks(self):
        cases = {
            "FACE_NOT_LIVE": live_frames(yaws=STILL_PHOTO),             # a photo held still
            "FACE_MULTIPLE_FACES": live_frames(faces=2),                # someone else in view
            "FACE_NOT_FOUND": live_frames(faces=0),                     # nobody in view
            "FACE_INCONSISTENT": live_frames()[:3] + live_frames("stranger")[3:],  # face swapped mid-scan
            "FACE_FRAME_COUNT": live_frames(yaws=(0.0, 0.3)),
        }
        for reason, frames in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaises(FaceRejected) as caught:
                    self.scan(self.challenge(self.pending()), frames)
                self.assertEqual(caught.exception.reason_code, reason)

    def test_replayed_frames_are_refused(self):
        frames = live_frames()
        self.scan(self.challenge(self.pending(1)), frames)
        with self.assertRaises(FaceRejected) as caught:
            self.scan(self.challenge(self.pending(2)), frames)
        self.assertEqual(caught.exception.reason_code, "FACE_REPLAYED")

    def test_a_face_match_cannot_confirm_a_different_payment(self):
        first, second = self.pending(1, 20_500), self.pending(2, 20_700)
        self.scan(self.challenge(first), live_frames())
        self.assert_refused(second, self.sign_without_face(self.challenge(second)), "FACE_NOT_VERIFIED")

    def test_a_face_match_is_spent_once(self):
        choice = self.pending()
        challenge = self.challenge(choice)
        self.scan(challenge, live_frames())
        with self.assertRaises(FaceRejected) as caught:
            self.scan(challenge, live_frames())
        self.assertEqual(caught.exception.reason_code, "FACE_ALREADY_CONFIRMED")

    def test_repeated_failures_lock_face_confirmation(self):
        for _ in range(5):
            with self.assertRaises(FaceRejected):
                self.scan(self.challenge(self.pending()), live_frames("stranger"))
        with self.assertRaises(FaceRejected) as caught:
            self.scan(self.challenge(self.pending()), live_frames())
        self.assertEqual(caught.exception.reason_code, "FACE_LOCKED")
        after = NOW + timedelta(minutes=11)
        choice = self.pending(9, now=after)
        later = self.database.verify_face(
            self.database.issue_approval_challenge(
                "single", choice["receipt_id"], choice["receipt_digest"], rp_id=self.device.rp_id, now=after,
            )["challenge_id"],
            live_frames(), now=after,
        )
        self.assertEqual(later["verdict"], "MATCH")

    def test_only_the_same_face_can_bind_a_new_device(self):
        consent = self.database.start_myinfo()
        with self.assertRaises(FaceRejected) as caught:
            self.database.register_face(consent["consent_id"], live_frames("stranger"))
        self.assertEqual(caught.exception.reason_code, "FACE_DOES_NOT_MATCH_ACCOUNT")
        same = self.database.register_face(consent["consent_id"], live_frames())
        self.assertEqual(same["verdict"], "REGISTERED")
        with closing(sqlite3.connect(self.path.with_name("ledger.faces.db"))) as store:
            self.assertEqual(store.execute("SELECT COUNT(*) FROM face_templates").fetchone()[0], 1)

    def test_every_scan_is_recorded_and_audited(self):
        choice = self.pending()
        challenge = self.challenge(choice)
        with self.assertRaises(FaceRejected):
            self.scan(challenge, live_frames("stranger"))
        self.scan(challenge, live_frames())
        self.approve(choice, self.sign_without_face(challenge))
        scans = [s for s in self.database.face_store.scans("demo_user_01") if s["purpose"] == "VERIFY"]
        self.assertEqual([s["verdict"] for s in scans[:2]], ["MATCH", "REJECTED"])
        self.assertIsNotNone(scans[0]["consumed_at"])
        events = [e["event_type"] for e in self.database.audit_overview()["events"]]
        for event in ("FACE_REGISTERED", "FACE_SCAN_REJECTED", "FACE_SCAN_MATCHED", "FACE_MATCH_CONSUMED"):
            self.assertIn(event, events)
        self.assertTrue(self.database.audit_overview()["chain"]["valid"])

    def test_without_the_face_engine_nothing_can_be_confirmed(self):
        choice = self.pending()
        challenge = self.challenge(choice)
        self.database.face_engine = None
        self.database.face_store._engine = lambda: None
        with self.assertRaises(FaceRejected) as caught:
            self.scan(challenge, live_frames())
        self.assertEqual(caught.exception.reason_code, "FACE_ENGINE_UNAVAILABLE")
        self.assert_refused(choice, self.sign_without_face(challenge), "FACE_NOT_VERIFIED")


@unittest.skipUnless(all(path.is_file() for path in model_paths()), "face models not installed")
class OpenCvEngineTests(unittest.TestCase):
    def test_the_installed_models_detect_and_embed_a_face(self):
        import cv2
        import numpy as np

        engine = OpenCvFaceEngine(*model_paths())
        blank = cv2.imencode(".jpg", np.zeros((480, 640, 3), dtype=np.uint8))[1].tobytes()
        self.assertEqual(engine.analyze(blank).face_count, 0)
        with self.assertRaises(FaceRejected):
            engine.analyze(b"not an image")


if __name__ == "__main__":
    unittest.main()

"""Voiceprint registration, spoken confirmation, and the approval tiers."""

import asyncio
import base64
import tempfile
import unittest
import uuid
from pathlib import Path

import httpx
import numpy as np

from apps.dimon_live import voice_dialog, voiceprint
from apps.dimon_live.model_adapter import ModelUnavailable
from apps.dimon_live.tests.device_signer import SoftwareAuthenticator
from apps.dimon_live.tests.face_fake import FakeFaceEngine, live_frames
from apps.dimon_live.tests.voice_fake import FakeSpeakerEngine, clip


class NoModel:
    def extract(self, text):
        raise ModelUnavailable("not used")


def face_frames(person="account-holder"):
    return [base64.b64encode(frame).decode() for frame in live_frames(person)]


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class FeatureTests(unittest.TestCase):
    def test_fbank_shape_and_mean_removed(self):
        features = voiceprint.fbank(np.random.default_rng(1).standard_normal(16_000).astype(np.float32) * 0.1)
        self.assertEqual(features.shape, (98, 80))
        self.assertLess(float(np.abs(features.mean(axis=0)).max()), 1e-4)

    def test_digits_are_heard_from_words_or_numerals(self):
        self.assertEqual(voiceprint.digits_heard("four seven one nine"), "4719")
        self.assertEqual(voiceprint.digits_heard("4 7 1 9."), "4719")
        self.assertEqual(voiceprint.digits_heard("It's 4, 7, 1, 9"), "4719")
        self.assertTrue(voiceprint.phrase_spoken("4 7 1 9", "um four seven one nine"))
        self.assertFalse(voiceprint.phrase_spoken("4 7 1 9", "four seven nine one"))

    def test_phrases_are_fresh_four_digits(self):
        phrases = {voiceprint.new_phrase() for _ in range(200)}
        self.assertGreater(len(phrases), 150)
        for phrase in phrases:
            digits = phrase.split()
            self.assertEqual(len(digits), 4)
            self.assertTrue(all(a != b for a, b in zip(digits, digits[1:])))

    def test_trim_silence(self):
        pcm = np.concatenate([np.zeros(16_000), np.full(8_000, 0.2), np.zeros(16_000)]).astype(np.float32)
        self.assertEqual(voiceprint.trim_silence(pcm).size, 8_000)


class DialogTests(unittest.TestCase):
    def test_wake_phrase(self):
        self.assertEqual(voice_dialog.wake("Hey Dimon"), (True, ""))
        self.assertEqual(voice_dialog.wake("hey diamond, send alicia 2 dollars"), (True, "send alicia 2 dollars"))
        self.assertEqual(voice_dialog.wake("send alicia 2 dollars")[0], False)
        self.assertEqual(voice_dialog.wake("the demon in the film")[0], False)
        # How local Whisper actually wrote the wake phrase from real audio.
        for heard in ("Hey Dimmon!", "Hey Dimin, buy $100 of Nvidia.", "Hi Daimon"):
            self.assertTrue(voice_dialog.wake(heard)[0], heard)
        self.assertEqual(voice_dialog.wake("Hey Dimin, buy $100 of Nvidia.")[1], "buy $100 of Nvidia")

    def test_confirmation_never_guesses(self):
        self.assertIs(voice_dialog.confirm("yes go ahead"), True)
        self.assertIs(voice_dialog.confirm("yes, no wait"), False)
        self.assertIs(voice_dialog.confirm("hmm maybe"), None)

    def test_replies_choose_only_offered_options(self):
        self.assertEqual(voice_dialog.account("my everyday account"), "everyday")
        self.assertIsNone(voice_dialog.account("savings or everyday"))
        self.assertEqual(voice_dialog.choice("the second one", ["John Smith", "John Tan"]), 1)
        self.assertEqual(voice_dialog.choice("John Smith", ["John Smith", "John Tan"]), 0)
        self.assertIsNone(voice_dialog.choice("Alicia", ["John Smith", "John Tan"]))
        self.assertEqual(voice_dialog.nickname("call them uncle tan"), "Uncle Tan")
        self.assertIsNone(voice_dialog.nickname("8800 8800"))


class VoiceApprovalTests(unittest.TestCase):
    """End to end through the HTTP API, with stand-in face and voice engines."""

    def setUp(self):
        from apps.dimon_live.app import create_app

        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(
            Path(self.temp.name) / "demo.sqlite3", extractor=NoModel(),
            face_engine=FakeFaceEngine(), voice_engine=FakeSpeakerEngine(),
        )
        self.db = self.app.state.database

    def tearDown(self):
        self.temp.cleanup()

    def request(self, method, path, **kwargs):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def act(self, text):
        return self.request("POST", "/api/actions", json={"request_id": str(uuid.uuid4()), "text": text}).json()

    def enroll_device(self):
        self.device = SoftwareAuthenticator("http://test")
        consent_id = self.request("POST", "/api/kyc/myinfo").json()["consent_id"]
        self.request("POST", "/api/face/register", json={"consent_id": consent_id, "frames": face_frames()})
        challenge = self.request("POST", "/api/signing/registration-challenge").json()
        response = self.request("POST", "/api/kyc/complete", json={
            "consent_id": consent_id,
            "challenge_id": challenge["challenge_id"],
            "credential_kind": "WEBAUTHN_PLATFORM",
            "credential_id": self.device.credential_id,
            "public_key_spki": b64(self.device.public_key_spki),
            "algorithm": -7,
            "client_data_json": b64(self.device.client_data("webauthn.create", challenge["challenge"])),
            "authenticator_data": b64(self.device.authenticator_data()),
        })
        self.assertEqual(response.status_code, 200, response.text)

    def challenge(self, collection, receipt_id, digest, *, face=True):
        challenge = self.request("POST", f"/api/{collection}/{receipt_id}/challenge", json={"receipt_digest": digest}).json()
        self.assertIn("challenge_id", challenge, challenge)
        if face:
            self.request("POST", "/api/face/verify", json={"challenge_id": challenge["challenge_id"], "frames": face_frames()})
        return challenge

    def approve(self, collection, receipt_id, digest, challenge):
        return self.request("POST", f"/api/{collection}/{receipt_id}/approve", json={
            "receipt_digest": digest, "assertion": SoftwareAuthenticator.wire(self.device.sign(challenge)),
        }).json()

    def register_voice(self, speaker="account-holder"):
        enrollment = self.request("POST", "/api/voiceprint/enrollments").json()
        self.assertEqual(enrollment["samples_needed"], 3, enrollment)
        result = None
        for _ in range(3):
            result = self.db.add_voice_enrollment_sample(enrollment["enrollment_id"], clip(speaker))
        self.assertTrue(result["complete"])
        challenge = self.challenge("voice-enrollments", result["receipt_id"], result["receipt_digest"])
        self.assertEqual(challenge["review"]["kind"], "VOICE")
        return self.approve("voice-enrollments", result["receipt_id"], result["receipt_digest"], challenge)

    def transfer(self, amount):
        return self.act(f"send John Smith ${amount} from savings")["receipt_choices"][0]

    def speak_phrase(self, challenge, *, speaker="account-holder", say=None, audio=None):
        phrase = self.request("POST", "/api/voice/phrase", json={"challenge_id": challenge["challenge_id"]})
        if phrase.status_code != 200:
            return phrase.json()["detail"]
        spoken = say if say is not None else phrase.json()["phrase"]
        try:
            return self.db.verify_voice(challenge["challenge_id"], audio or clip(speaker), spoken)
        except voiceprint.VoiceRejected as exc:
            return exc.reason_code

    def savings(self):
        state = self.request("GET", "/api/state").json()
        return next(a["balance_minor"] for a in state["accounts"] if a["account_id"] == "acct_savings_01")

    # ------------------------------------------------------------------
    def test_registration_is_approved_with_face_and_then_voice_confirms_a_small_payment(self):
        self.enroll_device()
        registered = self.register_voice()
        self.assertEqual(registered["outcome"], "EXECUTED_VOICE_REGISTERED", registered)
        self.assertTrue(self.request("GET", "/api/voiceprint").json()["voice_registered"])

        choice = self.transfer(5)
        challenge = self.challenge("receipts", choice["receipt_id"], choice["receipt_digest"], face=False)
        self.assertEqual(self.speak_phrase(challenge)["verdict"], "MATCH")
        result = self.approve("receipts", choice["receipt_id"], choice["receipt_digest"], challenge)
        self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRANSFER", result)
        self.assertEqual(result["approval"]["confirmed_by"], "VOICE")
        self.assertEqual(self.savings(), 100000 - 500)

    def test_s200_and_above_always_needs_the_face(self):
        self.enroll_device()
        self.register_voice()
        choice = self.transfer(250)
        challenge = self.challenge("receipts", choice["receipt_id"], choice["receipt_digest"], face=False)
        self.assertEqual(self.speak_phrase(challenge), "VOICE_NOT_ALLOWED_FOR_THIS_APPROVAL")
        result = self.approve("receipts", choice["receipt_id"], choice["receipt_digest"], challenge)
        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertEqual(result["reason_code"], "FACE_NOT_VERIFIED")
        self.assertEqual(self.savings(), 100000)

    def test_spoken_confirmation_rejections(self):
        self.enroll_device()
        self.register_voice()
        choice = self.transfer(5)
        challenge = self.challenge("receipts", choice["receipt_id"], choice["receipt_digest"], face=False)
        self.assertEqual(self.speak_phrase(challenge, say="one two three four"), "VOICE_PHRASE_MISMATCH")
        self.assertEqual(self.speak_phrase(challenge, speaker="stranger"), "VOICE_NO_MATCH")
        recording = clip()
        self.assertEqual(self.speak_phrase(challenge, audio=recording)["verdict"], "MATCH")
        # The same bytes again: a replay, even with the right phrase.
        choice2 = self.transfer(6)
        challenge2 = self.challenge("receipts", choice2["receipt_id"], choice2["receipt_digest"], face=False)
        self.assertEqual(self.speak_phrase(challenge2, audio=recording), "VOICE_REPLAYED")

    def test_a_voice_match_is_bound_to_one_receipt(self):
        self.enroll_device()
        self.register_voice()
        first, second = self.transfer(5), self.transfer(7)
        challenge = self.challenge("receipts", first["receipt_id"], first["receipt_digest"], face=False)
        self.speak_phrase(challenge)
        # The matched challenge signed against the other receipt is refused.
        stolen = self.approve("receipts", second["receipt_id"], second["receipt_digest"], challenge)
        self.assertEqual(stolen["outcome"], "BLOCKED")
        self.assertEqual(self.savings(), 100000)

    def test_voice_cannot_approve_its_own_registration_or_a_new_payee(self):
        self.enroll_device()
        enrollment = self.request("POST", "/api/voiceprint/enrollments").json()
        for _ in range(3):
            result = self.db.add_voice_enrollment_sample(enrollment["enrollment_id"], clip())
        challenge = self.challenge("voice-enrollments", result["receipt_id"], result["receipt_digest"], face=False)
        self.assertEqual(self.speak_phrase(challenge), "VOICE_NOT_ALLOWED_FOR_THIS_APPROVAL")
        unregistered = self.approve("voice-enrollments", result["receipt_id"], result["receipt_digest"], challenge)
        self.assertEqual(unregistered["reason_code"], "FACE_NOT_VERIFIED")
        self.assertFalse(self.request("GET", "/api/voiceprint").json()["voice_registered"])

        self.register_voice()
        addition = self.act("add 88008800 to my paynow as Uncle Tan")["payee_addition"]
        challenge = self.challenge("payee-additions", addition["addition_id"], addition["receipt_digest"], face=False)
        self.assertEqual(self.speak_phrase(challenge), "VOICE_NOT_ALLOWED_FOR_THIS_APPROVAL")

    def test_registration_needs_kyc_and_consistent_samples(self):
        self.assertEqual(self.request("POST", "/api/voiceprint/enrollments").status_code, 400)
        self.enroll_device()
        enrollment = self.request("POST", "/api/voiceprint/enrollments").json()
        self.db.add_voice_enrollment_sample(enrollment["enrollment_id"], clip())
        with self.assertRaises(voiceprint.VoiceRejected) as raised:
            self.db.add_voice_enrollment_sample(enrollment["enrollment_id"], clip("stranger"))
        self.assertEqual(raised.exception.reason_code, "VOICE_SAMPLES_INCONSISTENT")
        with self.assertRaises(voiceprint.VoiceRejected) as raised:
            self.db.add_voice_enrollment_sample(enrollment["enrollment_id"], clip(seconds=0.4))
        self.assertEqual(raised.exception.reason_code, "VOICE_TOO_SHORT")

    def test_repeated_failures_lock_voice_confirmation(self):
        self.enroll_device()
        self.register_voice()
        choice = self.transfer(5)
        challenge = self.challenge("receipts", choice["receipt_id"], choice["receipt_digest"], face=False)
        for _ in range(voiceprint.MAX_FAILED):
            self.assertEqual(self.speak_phrase(challenge, speaker="stranger"), "VOICE_NO_MATCH")
        self.assertEqual(self.speak_phrase(challenge), "VOICE_LOCKED")

    def test_without_a_voiceprint_no_phrase_is_issued(self):
        self.enroll_device()
        choice = self.transfer(5)
        challenge = self.challenge("receipts", choice["receipt_id"], choice["receipt_digest"], face=False)
        self.assertEqual(self.speak_phrase(challenge), "VOICE_NOT_REGISTERED")


if __name__ == "__main__":
    unittest.main()

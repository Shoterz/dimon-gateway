import asyncio
import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from apps.dimon_live.database import Database
from apps.dimon_live.read_back import amount_words, build_read_back
from apps.dimon_live.tencent_tts import (
    FallbackSynthesizer,
    TencentTtsSynthesizer,
    sign_request,
)

NOW = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)


class ReadBackTests(unittest.TestCase):
    def test_amounts_are_spoken_as_words(self):
        self.assertEqual(amount_words(1000), "ten dollars")
        self.assertEqual(amount_words(250), "two dollars and fifty cents")
        self.assertEqual(amount_words(100), "one dollar")
        self.assertEqual(amount_words(123456), "one thousand, two hundred and thirty-four dollars and fifty-six cents")

    def test_single_draft_reads_amount_payee_ending_and_source(self):
        text = build_read_back({
            "items": [{"recipient_display_name": "Alicia Powell", "masked_destination": "•••• 9876", "amount_minor": 1000}],
            "source_label": "savings",
            "total_minor": 1000,
        })
        self.assertTrue(text.startswith("Ten dollars to Alicia Powell, ending 9 8 7 6, from savings."))
        self.assertIn("approve with your fingerprint, face, or PIN", text)

    def test_batch_new_payee_unusual_and_token_are_all_spoken(self):
        text = build_read_back({
            "items": [
                {"recipient_display_name": "John Smith", "masked_destination": "•••• 4321", "amount_minor": 2000},
                {"recipient_display_name": "John Tan", "masked_destination": "•••• 2468", "amount_minor": 1000,
                 "new_payee": True, "unusual_amount": {"amount_minor": 1000}},
            ],
            "source_label": "savings",
            "total_minor": 3000,
            "policy": {"requires_out_of_band": True},
        })
        self.assertIn("Twenty dollars to John Smith, ending 4 3 2 1; and ten dollars to John Tan", text)
        self.assertIn("Total thirty dollars.", text)
        self.assertIn("John Tan is a new payee.", text)
        self.assertIn("more than you usually send to John Tan", text)
        self.assertIn("hardware token", text)


class TencentTtsTests(unittest.TestCase):
    def test_request_is_signed_with_tc3(self):
        payload = b'{"Text":"hi"}'
        headers = sign_request(secret_id="AKIDtest", secret_key="secret", payload=payload, timestamp=1_700_000_000, region="ap-singapore")
        self.assertTrue(headers["Authorization"].startswith("TC3-HMAC-SHA256 Credential=AKIDtest/2023-11-14/tts/tc3_request, SignedHeaders=content-type;host;x-tc-action, Signature="))
        self.assertEqual(headers["X-TC-Action"], "TextToVoice")
        self.assertEqual(headers["X-TC-Version"], "2019-08-23")
        # Recompute the signature independently.
        canonical = "\n".join((
            "POST", "/", "",
            "content-type:application/json; charset=utf-8\nhost:tts.tencentcloudapi.com\nx-tc-action:texttovoice\n",
            "content-type;host;x-tc-action",
            hashlib.sha256(payload).hexdigest(),
        ))
        to_sign = "\n".join(("TC3-HMAC-SHA256", "1700000000", "2023-11-14/tts/tc3_request", hashlib.sha256(canonical.encode()).hexdigest()))
        key = b"TC3secret"
        for part in ("2023-11-14", "tts", "tc3_request"):
            key = hmac.new(key, part.encode(), hashlib.sha256).digest()
        expected = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        self.assertTrue(headers["Authorization"].endswith(f"Signature={expected}"))

    def test_synthesizer_decodes_audio_and_reports_api_errors(self):
        wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            if seen["body"]["Text"] == "fail":
                return httpx.Response(200, json={"Response": {"Error": {"Code": "AuthFailure"}}})
            return httpx.Response(200, json={"Response": {"Audio": base64.b64encode(wav).decode()}})

        tts = TencentTtsSynthesizer(secret_id="id", secret_key="key", transport=httpx.MockTransport(handler), clock=lambda: 1_700_000_000)
        self.assertEqual(asyncio.run(tts("Ten dollars to Alicia")), wav)
        self.assertEqual(seen["body"]["Codec"], "wav")
        with self.assertRaisesRegex(RuntimeError, "TENCENT_TTS_AuthFailure"):
            asyncio.run(tts("fail"))

    def test_fallback_uses_the_local_voice_when_the_cloud_fails(self):
        async def broken(text):
            raise RuntimeError("offline")

        async def local(text):
            return b"local"

        self.assertEqual(asyncio.run(FallbackSynthesizer(broken, local)("hi")), b"local")


class PayeeInsightTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "ledger.sqlite3", clock=lambda: NOW)
        self.database.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_history_describes_the_last_payment_and_usual_range(self):
        insight = self.database.payee_insight("beneficiary_john_smith_01", 2_000)
        self.assertEqual(insight["last_payment"], "paid S$20 6 days ago")
        self.assertEqual(insight["typical"], "usually S$20–S$50")
        self.assertFalse(insight["new_payee"])
        self.assertIsNone(insight["unusual_amount"])
        self.assertEqual(self.database.payee_insight("beneficiary_john_tan_01")["last_payment"], "last paid 6 months ago")

    def test_fifty_instead_of_fifteen_is_unusual(self):
        self.assertIsNone(self.database.payee_insight("beneficiary_alicia_powell_01", 1_500)["unusual_amount"])
        flagged = self.database.payee_insight("beneficiary_alicia_powell_01", 5_000)["unusual_amount"]
        self.assertEqual(flagged, {"amount_minor": 5_000, "typical_min_minor": 500, "typical_max_minor": 1_500})

    def test_a_paynow_number_never_paid_is_a_new_payee(self):
        self.assertTrue(self.database.payee_insight("beneficiary_paynow_jxxx_01", 1_000)["new_payee"])

    def test_nicknames_resolve_to_the_saved_payee(self):
        for nickname in ("mum", "My Mum", "mummy", "ma"):
            with self.subTest(nickname=nickname):
                resolution = self.database.resolve_beneficiary(nickname)
                self.assertEqual(resolution.kind, "exact")
                self.assertEqual(resolution.matches[0]["display_name"], "Lim Siew Hua")

    def test_reset_restores_history_relative_to_now(self):
        self.database.reset()
        self.assertEqual(self.database.payee_insight("beneficiary_mum_01")["last_payment"], "paid S$50 5 days ago")


if __name__ == "__main__":
    unittest.main()

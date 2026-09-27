import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from apps.dimon_live.database import Database
from apps.dimon_live.paynow import (
    PayNowLookupError,
    SyntheticPayNowDirectory,
    normalize_mobile_proxy,
)


NOW = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)
REQUEST_ID = "00000000-0000-4000-8000-000000000080"


class PayNowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self.temp_dir.name) / "demo.sqlite3",
            clock=lambda: NOW,
        )
        self.database.initialize()
        self.directory = SyntheticPayNowDirectory(self.database)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_supported_spellings_normalize_to_one_exact_proxy(self):
        for value in ("86952933", "8695 2933", "8695-2933", "+65 8695 2933"):
            with self.subTest(value=value):
                self.assertEqual(normalize_mobile_proxy(value), "86952933")

    def test_invalid_proxy_grammar_is_rejected(self):
        for value in (
            "8695293",
            "869529333",
            "65 8695 2933",
            "+1 8695 2933",
            "8695.2933",
            "8695ABCD",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(PayNowLookupError, "INVALID_PAYNOW_PROXY"):
                    normalize_mobile_proxy(value)

    def test_changed_digit_never_resolves(self):
        with self.assertRaisesRegex(PayNowLookupError, "PAYNOW_PROXY_NOT_FOUND"):
            self.directory.lookup_mobile("86952938", REQUEST_ID)

    def test_lookup_is_idempotent_and_directory_owned(self):
        first = self.directory.lookup_mobile("86952933", REQUEST_ID)
        second = self.directory.lookup_mobile("86952933", REQUEST_ID)

        self.assertEqual(first, second)
        self.assertEqual(first.canonical_proxy, "86952933")
        self.assertEqual(first.masked_name, "JXXX SXXXX")
        self.assertEqual(first.beneficiary_id, "beneficiary_paynow_jxxx_01")
        self.assertTrue(first.lookup_reference.startswith("lookup_"))


if __name__ == "__main__":
    unittest.main()

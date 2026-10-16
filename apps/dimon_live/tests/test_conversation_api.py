import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from apps.dimon_live.tests.async_helpers import run_async_test
from apps.dimon_live.stock_market import (
    DailyBar,
    InstrumentCandidate,
    ProviderMarketData,
    ResolvedInstrument,
)


TEST_NOW = datetime(2026, 10, 4, 4, 0, tzinfo=timezone.utc)


class ApiMarketProvider:
    def search(self, label):
        return (
            InstrumentCandidate(
                symbol="NVDA",
                name="NVIDIA Corporation",
                exchange="NasdaqGS",
                instrument_type="EQUITY",
            ),
        )

    def describe(self, symbol):
        return ResolvedInstrument(
            symbol="NVDA",
            name="NVIDIA Corporation",
            exchange="NasdaqGS",
            instrument_type="EQUITY",
            quote_currency="USD",
            source="Yahoo Finance",
        )

    def snapshot(self, symbol, captured_at):
        return ProviderMarketData(
            price=Decimal("233.95"),
            market_state="OPEN",
            trailing_pe=Decimal("29.6"),
            bars=(
                DailyBar(date(2025, 10, 3), Decimal("240"), Decimal("180"), Decimal("200")),
                DailyBar(date(2026, 9, 20), Decimal("238"), Decimal("221"), Decimal("230")),
                DailyBar(date(2026, 10, 2), Decimal("237"), Decimal("225"), Decimal("233")),
            ),
        )

    def fx_estimate(self, base_currency, quote_currency, amount, captured_at):
        from apps.dimon_live.stock_market import FxEstimate

        return FxEstimate(
            pair="USD/SGD",
            rate=Decimal("1.28"),
            captured_at=captured_at,
            source="Yahoo Finance",
            converted_amount=amount * Decimal("1.28"),
            warning="Indicative only",
        )


class ConversationApiTests(unittest.TestCase):
    def setUp(self):
        from apps.dimon_live.app import create_app

        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(
            Path(self.temp_dir.name) / "demo.sqlite3",
            extractor=None,
            stock_provider=ApiMarketProvider(),
        )
        self.app.state.database._clock = lambda: TEST_NOW

    def tearDown(self):
        self.temp_dir.cleanup()

    def request(self, method, path, **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, path, **kwargs)

        return run_async_test(send)

    def test_stock_action_opens_thread_and_turn_endpoint_reaches_ready(self):
        started = self.request(
            "POST",
            "/api/actions",
            json={
                "request_id": "00000000-0000-4000-8000-000000001001",
                "text": "I want to put $500 into NVIDIA.",
            },
        )
        self.assertEqual(started.status_code, 200, started.text)
        payload = started.json()
        conversation = payload["conversation"]
        self.assertEqual(conversation["domain"], "INVESTMENT")
        self.assertEqual(conversation["status"], "WAITING")
        self.assertEqual(payload["allowed_choices"], ["BUY_NOW", "SET_LIMIT", "CANCEL"])
        conversation_id = conversation["conversation_id"]

        resumed = self.request("GET", f"/api/conversations/{conversation_id}")
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(resumed.json()["conversation"]["version"], 1)
        self.assertNotIn("audio_artifact_id", resumed.json())

        turned = self.request(
            "POST",
            f"/api/conversations/{conversation_id}/turns",
            json={
                "request_id": "00000000-0000-4000-8000-000000001002",
                "expected_version": 1,
                "input": {"kind": "CHOICE", "choice": "BUY_NOW"},
            },
        )
        self.assertEqual(turned.status_code, 200, turned.text)
        self.assertEqual(turned.json()["conversation"]["status"], "READY_FOR_DRAFT")
        self.assertEqual(turned.json()["public_view"]["order_type"], "MARKET")

    def test_turns_are_idempotent_and_stale_versions_conflict(self):
        started = self.request(
            "POST",
            "/api/actions",
            json={
                "request_id": "00000000-0000-4000-8000-000000001003",
                "text": "I want to put $500 into NVIDIA.",
            },
        ).json()
        conversation_id = started["conversation"]["conversation_id"]
        body = {
            "request_id": "00000000-0000-4000-8000-000000001004",
            "expected_version": 1,
            "input": {"kind": "CHOICE", "choice": "BUY_NOW"},
        }
        first = self.request("POST", f"/api/conversations/{conversation_id}/turns", json=body)
        replay = self.request("POST", f"/api/conversations/{conversation_id}/turns", json=body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["idempotent_replay"])

        conflict = self.request(
            "POST",
            f"/api/conversations/{conversation_id}/turns",
            json={
                "request_id": "00000000-0000-4000-8000-000000001005",
                "expected_version": 1,
                "input": {"kind": "CHOICE", "choice": "BUY_NOW"},
            },
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["detail"], "CONVERSATION_VERSION_CONFLICT")

    def test_turn_input_is_strictly_discriminated(self):
        started = self.request(
            "POST",
            "/api/actions",
            json={
                "request_id": "00000000-0000-4000-8000-000000001006",
                "text": "I want to put $500 into NVIDIA.",
            },
        ).json()
        conversation_id = started["conversation"]["conversation_id"]
        invalid = self.request(
            "POST",
            f"/api/conversations/{conversation_id}/turns",
            json={
                "request_id": "00000000-0000-4000-8000-000000001007",
                "expected_version": 1,
                "input": {
                    "kind": "CHOICE",
                    "choice": "BUY_NOW",
                    "state": {"execution": True},
                },
            },
        )
        self.assertEqual(invalid.status_code, 422)

    def test_exact_zero_flag_disables_conversation_routes(self):
        previous = os.environ.get("DIMON_CONVERSATIONS_ENABLED")
        os.environ["DIMON_CONVERSATIONS_ENABLED"] = "0"
        try:
            from apps.dimon_live.app import create_app

            disabled = create_app(Path(self.temp_dir.name) / "disabled.sqlite3", stock_provider=ApiMarketProvider())
            self.assertFalse(disabled.state.conversation_enabled)
        finally:
            if previous is None:
                os.environ.pop("DIMON_CONVERSATIONS_ENABLED", None)
            else:
                os.environ["DIMON_CONVERSATIONS_ENABLED"] = previous


if __name__ == "__main__":
    unittest.main()

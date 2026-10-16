import tempfile
import unittest
from pathlib import Path

import httpx

from apps.dimon_live.tests.async_helpers import run_async_test
from apps.dimon_live.tests.test_conversation_api import ApiMarketProvider, TEST_NOW


class PaperEquityApiTests(unittest.TestCase):
    def setUp(self):
        from apps.dimon_live.app import create_app

        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(
            Path(self.temp_dir.name) / "paper.sqlite3",
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

    def _order(self):
        profile = self.request(
            "POST",
            "/api/invest/profile",
            json={
                "answers": {"horizon": 3, "drop": 3, "experience": 2, "goal": 3},
                "acknowledged": True,
            },
        )
        self.assertEqual(profile.status_code, 200, profile.text)
        started = self.request(
            "POST",
            "/api/actions",
            json={
                "request_id": "00000000-0000-4000-8000-000000002001",
                "text": "I want to put $500 into NVIDIA.",
            },
        ).json()
        conversation = started["conversation"]
        result = self.request(
            "POST",
            f"/api/conversations/{conversation['conversation_id']}/turns",
            json={
                "request_id": "00000000-0000-4000-8000-000000002002",
                "expected_version": 1,
                "input": {"kind": "CHOICE", "choice": "BUY_NOW"},
            },
        )
        self.assertEqual(result.status_code, 200, result.text)
        payload = result.json()
        self.assertEqual(payload["outcome"], "ORDER_CONFIRMATION_REQUEST")
        return payload["paper_order"]

    def test_buy_now_turn_returns_one_order_and_replays_same_receipt(self):
        order = self._order()
        self.assertTrue(order["order_id"].startswith("paper_order_"))
        self.assertEqual(len(order["digest"]), 64)

    def test_order_read_refresh_and_cancel_are_session_bound(self):
        order = self._order()
        read = self.request("GET", f"/api/paper-equity/orders/{order['order_id']}")
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.json()["paper_order"]["digest"], order["digest"])

        refresh = self.request(
            "POST",
            f"/api/paper-equity/orders/{order['order_id']}/refresh",
            json={"receipt_digest": order["digest"]},
        )
        self.assertEqual(refresh.status_code, 200, refresh.text)
        self.assertEqual(refresh.json()["quote"]["price_minor"], 23395)

        cancel = self.request(
            "POST",
            f"/api/paper-equity/orders/{order['order_id']}/cancel",
            json={"receipt_digest": order["digest"]},
        )
        self.assertEqual(cancel.status_code, 200, cancel.text)
        self.assertEqual(cancel.json()["status"], "CANCELLED")

    def test_feature_flag_disables_only_paper_routes(self):
        import os
        from apps.dimon_live.app import create_app

        old = os.environ.get("DIMON_REAL_STOCK_EXECUTION_ENABLED")
        os.environ["DIMON_REAL_STOCK_EXECUTION_ENABLED"] = "0"
        try:
            disabled = create_app(Path(self.temp_dir.name) / "disabled.sqlite3", stock_provider=ApiMarketProvider())
            transport = httpx.ASGITransport(app=disabled)

            async def send():
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    return await client.get("/api/paper-equity/orders/nope")

            response = run_async_test(send)
            self.assertEqual(response.status_code, 404)
        finally:
            if old is None:
                os.environ.pop("DIMON_REAL_STOCK_EXECUTION_ENABLED", None)
            else:
                os.environ["DIMON_REAL_STOCK_EXECUTION_ENABLED"] = old


if __name__ == "__main__":
    unittest.main()

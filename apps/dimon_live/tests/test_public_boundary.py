import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from apps.dimon_live.model_adapter import ExtractedIntent


class StaticExtractor:
    def extract(self, text):
        intent = {
            "contract_version": 3,
            "kind": "BENEFICIARY_TRANSFER",
            "source_account_ref": "savings",
            "source_evidence": "savings",
            "recipient_label": "John Smith",
            "recipient_evidence": "John Smith",
            "currency": "SGD",
            "payment_reference": None,
            "reference_evidence": None,
            "requested_execution_date": None,
        }
        return ExtractedIntent(intent, json.dumps(intent), 1.0)


class PublicBoundaryTests(unittest.TestCase):
    def setUp(self):
        from apps.dimon_live.app import create_app

        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.app = create_app(
            extractor=StaticExtractor(),
            owner_path=root / "owner.sqlite3",
            visitor_dir=root / "visitors",
            allowed_origins={"https://visitor.example"},
        )

    def tearDown(self):
        self.app.state.session_manager.close()
        self.temp_dir.cleanup()

    def request(self, method, path, **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://visitor.example") as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def test_configured_origin_bootstraps_visitor_session(self):
        response = self.request("GET", "/api/session", headers={"origin": "https://visitor.example"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "visitor")
        self.assertIn("dimon_session", response.headers.get("set-cookie", ""))

    def test_unconfigured_origin_is_rejected(self):
        response = self.request("GET", "/api/state", headers={"origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "ORIGIN_NOT_ALLOWED")

    def test_unconfigured_origin_cannot_load_the_app_shell(self):
        response = self.request("GET", "/", headers={"origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)

    def test_public_health_is_not_exposed(self):
        response = self.request("GET", "/api/health", headers={"origin": "https://visitor.example"})
        self.assertEqual(response.status_code, 404)

    def test_action_text_limit_is_enforced_at_boundary(self):
        response = self.request(
            "POST",
            "/api/actions",
            headers={"origin": "https://visitor.example"},
            json={"request_id": "00000000-0000-4000-8000-000000000901", "text": "x" * 501},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()

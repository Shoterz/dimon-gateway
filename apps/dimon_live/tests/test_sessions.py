import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from apps.dimon_live.approval_gateway import b64url_encode
from apps.dimon_live.model_adapter import ExtractedIntent
from apps.dimon_live.tests.device_signer import SoftwareAuthenticator
from apps.dimon_live.session_manager import OwnerAccessError, SessionManager


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


def access_jwt(audience="owner-demo"):
    payload = {"aud": [audience], "exp": 4102444800, "email": "owner@example.test"}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    return "header." + encoded.decode() + ".signature"


class SessionIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.manager = SessionManager(
            owner_path=root / "owner.sqlite3",
            visitor_dir=root / "visitors",
            allowed_origins={"https://owner.test", "https://visitor.ngrok.app"},
            owner_origin="https://owner.test",
            owner_access_audience="owner-demo",
            max_visitor_sessions=10,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_two_visitors_get_independent_seeded_databases(self):
        first = self.manager.create_visitor()
        second = self.manager.create_visitor()
        self.assertNotEqual(first.database.path, second.database.path)
        self.assertEqual(first.database.get_state()["accounts"][0]["balance_minor"], 100000)
        self.assertEqual(second.database.get_state()["accounts"][0]["balance_minor"], 100000)

        first.database.reset()
        self.assertEqual(second.database.get_state()["transactions"], [])
        self.assertNotEqual(first.session_id, second.session_id)

    def test_owner_requires_access_assertion(self):
        with self.assertRaises(OwnerAccessError):
            self.manager.resolve_headers(
                host="owner.test",
                origin="https://owner.test",
                cookie=None,
                access_assertion=None,
            )

    def test_ngrok_host_never_selects_owner(self):
        context = self.manager.resolve_headers(
            host="visitor.ngrok.app",
            origin="https://visitor.ngrok.app",
            cookie=None,
            access_assertion=access_jwt(),
        )
        self.assertEqual(context.role, "visitor")
        self.assertNotEqual(context.database.path, self.manager.owner_path)

    def test_owner_assertion_selects_persistent_owner_database(self):
        context = self.manager.resolve_headers(
            host="owner.test",
            origin="https://owner.test",
            cookie=None,
            access_assertion=access_jwt(),
        )
        self.assertEqual(context.role, "owner")
        self.assertEqual(context.database.path, self.manager.owner_path)

    def test_owner_hostname_without_access_configuration_is_a_visitor(self):
        manager = SessionManager(
            owner_path=self.manager.owner_path,
            visitor_dir=self.manager.visitor_dir,
            allowed_origins={"https://owner.test"},
            owner_origin="https://owner.test",
            owner_access_audience=None,
        )
        context = manager.resolve_headers(
            host="owner.test",
            origin="https://owner.test",
            cookie=None,
            access_assertion=None,
        )
        self.assertEqual(context.role, "visitor")
        self.assertNotEqual(context.database.path, manager.owner_path)


class SessionApiTests(unittest.TestCase):
    def test_requests_with_different_cookies_do_not_share_balances(self):
        from apps.dimon_live.app import create_app

        temp_dir = tempfile.TemporaryDirectory()
        try:
            app = create_app(
                extractor=StaticExtractor(),
                owner_path=Path(temp_dir.name) / "owner.sqlite3",
                visitor_dir=Path(temp_dir.name) / "visitors",
                allowed_origins={"http://test"},
            )

            async def run():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as first_client:
                    one = await first_client.get("/api/state")
                    cookie_one = one.cookies.get("dimon_session")
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as second_client:
                    two = await second_client.get("/api/state")
                    cookie_two = two.cookies.get("dimon_session")
                    self.assertNotEqual(cookie_one, cookie_two)
                    return one, two

            one, two = asyncio.run(run())
            self.assertEqual(one.status_code, 200)
            self.assertEqual(two.status_code, 200)
        finally:
            temp_dir.cleanup()

    def test_transfer_in_visitor_a_does_not_change_visitor_b_or_owner(self):
        from apps.dimon_live.app import create_app

        temp_dir = tempfile.TemporaryDirectory()
        try:
            root = Path(temp_dir.name)
            app = create_app(
                extractor=StaticExtractor(),
                owner_path=root / "owner.sqlite3",
                visitor_dir=root / "visitors",
                allowed_origins={"http://test"},
            )

            async def run():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as first, httpx.AsyncClient(transport=transport, base_url="http://test") as second:
                    await first.get("/api/state")
                    await second.get("/api/state")
                    pending = (await first.post(
                        "/api/actions",
                        json={
                            "request_id": "00000000-0000-4000-8000-000000000902",
                            "text": "Send John Smith S$5 from savings.",
                        },
                    )).json()
                    choice = pending["receipt_choices"][0]
                    device = SoftwareAuthenticator("http://test")
                    consent = (await first.post("/api/kyc/myinfo")).json()
                    registration = (await first.post("/api/signing/registration-challenge")).json()
                    await first.post(
                        "/api/kyc/complete",
                        json={
                            "consent_id": consent["consent_id"],
                            "challenge_id": registration["challenge_id"],
                            "credential_kind": "WEBAUTHN_PLATFORM",
                            "credential_id": device.credential_id,
                            "public_key_spki": b64url_encode(device.public_key_spki),
                            "algorithm": -7,
                            "client_data_json": b64url_encode(
                                device.client_data("webauthn.create", registration["challenge"])
                            ),
                            "authenticator_data": b64url_encode(device.authenticator_data()),
                        },
                    )
                    digest = {"receipt_digest": choice["receipt_digest"]}
                    # Visitor B can neither see nor be challenged for A's receipt.
                    foreign = (await second.post(
                        f"/api/receipts/{choice['receipt_id']}/challenge", json=digest
                    )).json()
                    challenge = (await first.post(
                        f"/api/receipts/{choice['receipt_id']}/challenge", json=digest
                    )).json()
                    result = await first.post(
                        f"/api/receipts/{choice['receipt_id']}/approve",
                        json={**digest, "assertion": SoftwareAuthenticator.wire(device.sign(challenge))},
                    )
                    other = await second.get("/api/state")
                    return result, other, foreign

            result, other, foreign = asyncio.run(run())
            self.assertEqual(foreign["reason_code"], "RECEIPT_NOT_FOUND")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
            self.assertEqual(other.json()["accounts"][0]["balance_minor"], 100000)
            self.assertFalse((Path(temp_dir.name) / "owner.sqlite3").exists())
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()

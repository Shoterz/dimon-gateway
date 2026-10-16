"""Log out locks the account; only the registered face unlocks it. Also the
Contacts list, which reads the saved payees the server already owns."""

import unittest

from starlette.testclient import TestClient

from apps.dimon_live.tests import test_api
from apps.dimon_live.tests.test_api import GOLDEN_REQUEST, REQUEST_ID, face_frames


class Harness(unittest.TestCase):
    # Borrow the API harness without re-running its tests.
    setUp = test_api.DimonLiveApiTests.setUp
    tearDown = test_api.DimonLiveApiTests.tearDown
    request = test_api.DimonLiveApiTests.request
    onboarding_body = test_api.DimonLiveApiTests.onboarding_body
    enroll_device = test_api.DimonLiveApiTests.enroll_device


class SavedPayeeListTests(Harness):
    def test_lists_saved_payees_with_nicknames_and_history(self):
        payees = self.request("GET", "/api/payees").json()
        by_name = {payee["display_name"]: payee for payee in payees}

        self.assertIn("John Smith", by_name)
        self.assertIn("Alicia Powell", by_name)
        mum = by_name["Lim Siew Hua"]
        self.assertIn("mum", mum["aliases"])
        self.assertEqual(mum["payment_count"], 3)
        self.assertTrue(mum["last_payment"].startswith("paid S$50"))
        self.assertFalse(mum["is_new"])
        # Directory-only PayNow numbers are not saved payees.
        self.assertTrue(all(not name.startswith("J") or " " in name for name in by_name))
        self.assertEqual([p["display_name"] for p in payees], sorted(by_name, key=str.casefold))


class AppLockTests(Harness):
    def lock(self):
        self.enroll_device()
        response = self.request("POST", "/api/session/lock")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["locked"])

    def unlock_body(self, person="account-holder"):
        challenge = self.request("POST", "/api/session/unlock-challenge")
        self.assertEqual(challenge.status_code, 200, challenge.text)
        return {"challenge_id": challenge.json()["challenge_id"], "frames": face_frames(person)}

    def test_cannot_lock_without_a_registered_face(self):
        response = self.request("POST", "/api/session/lock")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "FACE_NOT_REGISTERED")
        self.assertFalse(self.request("GET", "/api/session").json()["locked"])

    def test_locked_account_refuses_every_money_route(self):
        self.lock()

        for method, path, body in (
            ("GET", "/api/state", None),
            ("GET", "/api/payees", None),
            ("GET", "/api/invest", None),
            ("POST", "/api/actions", {"request_id": REQUEST_ID, "text": GOLDEN_REQUEST}),
            ("POST", "/api/reset", None),
        ):
            with self.subTest(path=path):
                response = self.request(method, path, json=body)
                self.assertEqual(response.status_code, 423, response.text)
                self.assertEqual(response.json()["detail"], "APP_LOCKED")
        session = self.request("GET", "/api/session").json()
        self.assertTrue(session["locked"])

    def test_registered_face_unlocks_once(self):
        self.lock()
        body = self.unlock_body()

        unlocked = self.request("POST", "/api/session/unlock", json=body)

        self.assertEqual(unlocked.status_code, 200, unlocked.text)
        self.assertFalse(unlocked.json()["locked"])
        self.assertEqual(self.request("GET", "/api/state").status_code, 200)
        replay = self.request("POST", "/api/session/unlock", json=body)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json()["detail"], "UNLOCK_CHALLENGE_REPLAYED")

    def test_someone_elses_face_does_not_unlock(self):
        self.lock()

        response = self.request("POST", "/api/session/unlock", json=self.unlock_body("stranger"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "FACE_NO_MATCH")
        self.assertEqual(self.request("GET", "/api/state").status_code, 423)

    def test_payment_face_match_cannot_unlock(self):
        self.lock()
        body = self.unlock_body()
        # A match bound to another challenge (e.g. a payment) is not this unlock's.
        body["challenge_id"] = "chal_" + "0" * 32

        response = self.request("POST", "/api/session/unlock", json=body)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "UNLOCK_CHALLENGE_UNKNOWN")

    def test_unlock_challenge_needs_a_locked_account(self):
        response = self.request("POST", "/api/session/unlock-challenge")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "APP_NOT_LOCKED")

    def test_voice_sockets_refuse_a_locked_account(self):
        self.lock()
        client = TestClient(self.app, base_url="http://test")

        for path in ("/api/voice/session", "/api/voice/hands-free"):
            with self.subTest(path=path), client.websocket_connect(path) as socket:
                self.assertEqual(socket.receive_json(), {"type": "error", "reason_code": "APP_LOCKED"})

    def test_existing_voice_sockets_recheck_lock_before_dispatch(self):
        self.enroll_device()

        class LockThenDispatch:
            async def handle(_self, websocket, context, dispatch):
                await websocket.accept()
                context.database.lock_app()
                result = await dispatch("what is my balance", {})
                await websocket.send_json(result)
                await websocket.close()

        client = TestClient(self.app, base_url="http://test")
        for path, state_name in (
            ("/api/voice/session", "voice_bridge"),
            ("/api/voice/hands-free", "hands_free"),
        ):
            with self.subTest(path=path):
                # Unlock between subtests without weakening the production route.
                with self.app.state.database._connect() as connection:
                    connection.execute("DELETE FROM app_locks")
                    connection.commit()
                setattr(self.app.state, state_name, LockThenDispatch())
                with client.websocket_connect(path) as socket:
                    self.assertEqual(
                        socket.receive_json(),
                        {"outcome": "BLOCKED", "reason_code": "APP_LOCKED"},
                    )


if __name__ == "__main__":
    unittest.main()

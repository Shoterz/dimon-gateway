import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from apps.dimon_live.approval_gateway import b64url_encode
from apps.dimon_live.tests.device_signer import SoftwareAuthenticator

from apps.dimon_live.model_adapter import ExtractedIntent


REQUEST_ID = "00000000-0000-4000-8000-000000000020"
GOLDEN_REQUEST = "Send John Smith S$50 from savings."
GOLDEN_INTENT = {
    "contract_version": 2,
    "kind": "BENEFICIARY_TRANSFER",
    "source_account_ref": "savings",
    "source_evidence": "savings",
    "recipient_label": "John Smith",
    "recipient_evidence": "John Smith",
    "amount_minor": 5000,
    "amount_evidence": "S$50",
    "currency": "SGD",
    "payment_reference": None,
    "reference_evidence": None,
    "requested_execution_date": None,
}
BATCH_INTENT = {
    "contract_version": 4,
    "kind": "BENEFICIARY_TRANSFER_BATCH",
    "source_account_ref": None,
    "source_evidence": None,
    "items": [
        {"recipient_label": "John Smith", "recipient_evidence": "John Smith"},
        {"recipient_label": "Alicia Powell", "recipient_evidence": "Alicia Powell"},
    ],
    "currency": "SGD",
    "requested_execution_date": None,
}


class RecordingExtractor:
    def __init__(self):
        self.calls = []
        self.intent = dict(GOLDEN_INTENT)

    def extract(self, text):
        self.calls.append(text)
        return ExtractedIntent(
            dict(self.intent),
            json.dumps(self.intent),
            12.5,
        )


class DimonLiveApiTests(unittest.TestCase):
    def setUp(self):
        from apps.dimon_live.app import create_app

        self.temp_dir = tempfile.TemporaryDirectory()
        self.extractor = RecordingExtractor()
        self.app = create_app(
            Path(self.temp_dir.name) / "demo.sqlite3",
            extractor=self.extractor,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def request(self, method, path, **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def enroll_device(self):
        self.device = SoftwareAuthenticator("http://test")
        challenge = self.request("POST", "/api/signing/registration-challenge").json()
        self.assertEqual(challenge["rp_id"], "test")
        response = self.request(
            "POST",
            "/api/signing/credentials",
            json={
                "challenge_id": challenge["challenge_id"],
                "credential_kind": "WEBAUTHN_PLATFORM",
                "credential_id": self.device.credential_id,
                "public_key_spki": b64url_encode(self.device.public_key_spki),
                "algorithm": -7,
                "client_data_json": b64url_encode(
                    self.device.client_data("webauthn.create", challenge["challenge"])
                ),
                "authenticator_data": b64url_encode(self.device.authenticator_data()),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    def signed_body(self, collection, receipt_id, digest, **sign_options):
        if not hasattr(self, "device"):
            self.enroll_device()
        challenge = self.request(
            "POST",
            f"/api/{collection}/{receipt_id}/challenge",
            json={"receipt_digest": digest},
        ).json()
        self.assertIn("challenge_id", challenge, challenge)
        return {
            "receipt_digest": digest,
            "assertion": SoftwareAuthenticator.wire(
                self.device.sign(challenge, **sign_options)
            ),
        }

    def post_vague_alicia(self):
        self.extractor.intent = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "recipient_label": "Alicia Powell",
            "recipient_evidence": "Alicia Powell",
            "amount_minor": 500,
            "amount_evidence": "five dollars",
        }
        return self.request(
            "POST",
            "/api/actions",
            json={
                "request_id": "00000000-0000-4000-8000-000000000050",
                "text": "I owe Alicia Powell five dollars, help me resolve it.",
            },
        )

    def test_get_state_returns_seeded_demo_state(self):
        response = self.request("GET", "/api/state")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accounts"][0]["balance_minor"], 100000)
        self.assertTrue(response.json()["demo_mode"])
        self.assertEqual(
            response.json()["model"],
            "qwen35-9b-abliterated-q5",
        )
        paynow = next(
            account
            for account in response.json()["accounts"]
            if account["account_id"] == "acct_paynow_jxxx_01"
        )
        self.assertEqual(paynow["paynow_proxy"], "8695 2933")

    def test_post_action_drafts_and_only_a_signed_approval_executes(self):
        response = self.request(
            "POST",
            "/api/actions",
            json={"request_id": REQUEST_ID, "text": GOLDEN_REQUEST},
        )

        self.assertEqual(response.status_code, 200)
        pending = response.json()
        self.assertEqual(pending["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(pending["trace"][-1]["stage"], "SIGNED_APPROVAL_REQUIRED")
        self.assertEqual(pending["state"]["accounts"][0]["balance_minor"], 100000)
        choice = pending["receipt_choices"][0]

        unsigned = self.request(
            "POST",
            f"/api/receipts/{choice['receipt_id']}/approve",
            json={"receipt_digest": choice["receipt_digest"]},
        )
        self.assertEqual(unsigned.status_code, 422)

        executed = self.request(
            "POST",
            f"/api/receipts/{choice['receipt_id']}/approve",
            json=self.signed_body("receipts", choice["receipt_id"], choice["receipt_digest"]),
        )
        self.assertEqual(executed.json()["outcome"], "EXECUTED_SYNTHETIC_TRANSFER")
        self.assertEqual(executed.json()["state"]["accounts"][0]["balance_minor"], 95000)

    def test_challenge_requires_an_enrolled_device(self):
        choice = self.post_vague_alicia().json()["receipt_choices"][0]

        response = self.request(
            "POST",
            f"/api/receipts/{choice['receipt_id']}/challenge",
            json={"receipt_digest": choice["receipt_digest"]},
        )

        self.assertEqual(
            (response.json()["outcome"], response.json()["reason_code"]),
            ("BLOCKED", "SIGNING_CREDENTIAL_REQUIRED"),
        )

    def test_challenge_review_shows_stored_receipt_facts(self):
        self.enroll_device()
        choice = self.post_vague_alicia().json()["receipt_choices"][0]

        challenge = self.request(
            "POST",
            f"/api/receipts/{choice['receipt_id']}/challenge",
            json={"receipt_digest": choice["receipt_digest"]},
        ).json()

        self.assertEqual(challenge["review"]["total_minor"], 500)
        self.assertEqual(
            challenge["review"]["items"][0]["recipient_display_name"], "Alicia Powell"
        )
        self.assertEqual(
            [item["credential_id"] for item in challenge["allow_credentials"]],
            [self.device.credential_id],
        )

    def test_signature_from_another_origin_is_rejected(self):
        choice = self.post_vague_alicia().json()["receipt_choices"][0]

        response = self.request(
            "POST",
            f"/api/receipts/{choice['receipt_id']}/approve",
            json=self.signed_body(
                "receipts",
                choice["receipt_id"],
                choice["receipt_digest"],
                origin="https://attacker.example",
            ),
        )

        self.assertEqual(response.json()["reason_code"], "APPROVAL_ORIGIN_MISMATCH")
        self.assertEqual(len(response.json()["state"]["transactions"]), 0)

    def test_registration_with_foreign_origin_is_refused(self):
        device = SoftwareAuthenticator("https://attacker.example")
        challenge = self.request("POST", "/api/signing/registration-challenge").json()
        response = self.request(
            "POST",
            "/api/signing/credentials",
            json={
                "challenge_id": challenge["challenge_id"],
                "credential_kind": "WEBAUTHN_PLATFORM",
                "credential_id": device.credential_id,
                "public_key_spki": b64url_encode(device.public_key_spki),
                "algorithm": -7,
                "client_data_json": b64url_encode(
                    device.client_data("webauthn.create", challenge["challenge"])
                ),
                "authenticator_data": b64url_encode(device.authenticator_data(rp_id="test")),
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "APPROVAL_ORIGIN_MISMATCH")

    def test_amount_clarification_endpoint_is_strict_and_does_not_recall_model(self):
        self.extractor.intent = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "recipient_label": "Alicia Powell",
            "recipient_evidence": "Alicia Powell",
            "amount_minor": None,
            "amount_evidence": None,
            "payment_reference": "flowers",
            "reference_evidence": "for flowers",
        }
        text = "send Alicia Powell money for flowers"
        pending = self.request(
            "POST",
            "/api/actions",
            json={
                "request_id": "00000000-0000-4000-8000-000000000077",
                "text": text,
            },
        ).json()
        path = (
            f"/api/clarifications/{pending['clarification']['clarification_id']}"
            "/respond"
        )

        rejected = self.request(
            "POST",
            path,
            json={
                "request_id": "00000000-0000-4000-8000-000000000078",
                "text": "ten dollars",
                "amount_minor": 1000,
            },
        )
        self.assertEqual(rejected.status_code, 422)

        completed = self.request(
            "POST",
            path,
            json={
                "request_id": "00000000-0000-4000-8000-000000000078",
                "text": "ten dollars",
            },
        )
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["receipt_choices"][0]["amount_minor"], 1000)
        self.assertEqual(self.extractor.calls, [text])

    def test_paynow_action_returns_directory_owned_confirmation(self):
        self.extractor.intent = {
            "contract_version": 2,
            "kind": "PAYNOW_MOBILE_TRANSFER",
            "source_account_ref": None,
            "source_evidence": None,
            "mobile_proxy": "86952933",
            "proxy_evidence": "8695 2933",
            "amount_minor": 1000,
            "amount_evidence": "ten dollars",
            "currency": "SGD",
            "payment_reference": "flowers",
            "reference_evidence": "for flowers",
            "requested_execution_date": None,
        }

        response = self.request(
            "POST",
            "/api/actions",
            json={
                "request_id": "00000000-0000-4000-8000-000000000079",
                "text": "PayNow 8695 2933 ten dollars for flowers",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(body["receipt_choices"][0]["recipient_display_name"], "JXXX SXXXX")
        self.assertEqual(body["receipt_choices"][0]["destination_reference"], "86952933")

    def test_approval_endpoint_executes_receipt_without_second_model_call(self):
        pending = self.post_vague_alicia().json()
        choice = pending["receipt_choices"][0]

        response = self.request(
            "POST",
            f"/api/receipts/{choice['receipt_id']}/approve",
            json=self.signed_body("receipts", choice["receipt_id"], choice["receipt_digest"]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["outcome"],
            "EXECUTED_SYNTHETIC_TRANSFER",
        )
        self.assertEqual(
            self.extractor.calls,
            ["I owe Alicia Powell five dollars, help me resolve it."],
        )

    def test_approval_rejects_client_supplied_transfer_fields(self):
        choice = self.post_vague_alicia().json()["receipt_choices"][0]

        response = self.request(
            "POST",
            f"/api/receipts/{choice['receipt_id']}/approve",
            json={
                "receipt_digest": choice["receipt_digest"],
                "amount_minor": 5000,
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            len(self.app.state.database.get_state()["transactions"]),
            0,
        )

    def test_approval_digest_shape_is_strict(self):
        choice = self.post_vague_alicia().json()["receipt_choices"][0]
        bodies = (
            {},
            {"receipt_digest": "abc"},
            {"receipt_digest": choice["receipt_digest"].upper()},
        )

        for body in bodies:
            with self.subTest(body=body):
                response = self.request(
                    "POST",
                    f"/api/receipts/{choice['receipt_id']}/approve",
                    json=body,
                )
                self.assertEqual(response.status_code, 422)
        self.assertEqual(
            len(self.app.state.database.get_state()["transactions"]),
            0,
        )

    def test_batch_approval_endpoint_is_strict_and_replays_once(self):
        self.extractor.intent = dict(BATCH_INTENT)
        pending = self.request(
            "POST",
            "/api/actions",
            json={
                "request_id": "00000000-0000-4000-8000-000000000142",
                "text": "Send John Smith $6.50 and Alicia Powell $4",
            },
        ).json()
        receipt = pending["batch_receipt"]
        path = f"/api/batch-receipts/{receipt['receipt_id']}/approve"
        rejected = self.request(
            "POST",
            path,
            json={
                "receipt_digest": receipt["receipt_digest"],
                "total_minor": 1,
            },
        )
        self.assertEqual(rejected.status_code, 422)
        for body in (
            {},
            {"receipt_digest": "abc"},
            {"receipt_digest": receipt["receipt_digest"].upper()},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", path, json=body).status_code, 422)
        signed = self.signed_body(
            "batch-receipts", receipt["receipt_id"], receipt["receipt_digest"]
        )
        first = self.request("POST", path, json=signed)
        second = self.request("POST", path, json=signed)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["outcome"], "EXECUTED_SYNTHETIC_BATCH")
        self.assertEqual(
            first.json()["batch_transaction_id"], second.json()["batch_transaction_id"]
        )

    def test_valid_shape_wrong_digest_is_non_mutating_block(self):
        choice = self.post_vague_alicia().json()["receipt_choices"][0]

        signed = self.signed_body("receipts", choice["receipt_id"], choice["receipt_digest"])
        response = self.request(
            "POST",
            f"/api/receipts/{choice['receipt_id']}/approve",
            json={**signed, "receipt_digest": "0" * 64},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (response.json()["outcome"], response.json()["reason_code"]),
            ("BLOCKED", "APPROVAL_RECEIPT_MISMATCH"),
        )
        challenge = self.request(
            "POST",
            f"/api/receipts/{choice['receipt_id']}/challenge",
            json={"receipt_digest": "0" * 64},
        )
        self.assertEqual(challenge.json()["reason_code"], "RECEIPT_DIGEST_MISMATCH")
        self.assertEqual(len(response.json()["state"]["transactions"]), 0)

    def test_post_action_rejects_blank_text_without_calling_extractor(self):
        response = self.request(
            "POST",
            "/api/actions",
            json={"request_id": REQUEST_ID, "text": "   "},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.extractor.calls, [])

    def test_post_action_rejects_text_over_500_characters_without_calling_extractor(self):
        response = self.request(
            "POST",
            "/api/actions",
            json={"request_id": REQUEST_ID, "text": "x" * 501},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.extractor.calls, [])

    def test_post_action_rejects_non_uuid_request_id_without_calling_extractor(self):
        response = self.request(
            "POST",
            "/api/actions",
            json={"request_id": "not-a-uuid", "text": GOLDEN_REQUEST},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.extractor.calls, [])

    def test_reset_restores_seed_state(self):
        self.request(
            "POST",
            "/api/actions",
            json={"request_id": REQUEST_ID, "text": GOLDEN_REQUEST},
        )

        response = self.request("POST", "/api/reset")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accounts"][0]["balance_minor"], 100000)
        self.assertEqual(response.json()["transactions"], [])

    def test_static_root_is_served_as_html(self):
        response = self.request("GET", "/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertIn("Send Alicia Powell S$2.50 from savings.", response.text)

    def test_static_stylesheet_is_served_as_css(self):
        response = self.request("GET", "/static/app.css")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/css"))

    def test_static_script_is_served_as_javascript(self):
        response = self.request("GET", "/static/app.js")

        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])

    def test_clarification_client_module_is_served_as_javascript(self):
        response = self.request("GET", "/static/clarification_response.mjs")

        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])
        self.assertIn("requestClarificationResponse", response.text)

    def test_action_client_module_is_served_as_javascript(self):
        response = self.request("GET", "/static/action_request.mjs")

        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])
        self.assertIn("requestAction", response.text)

    def test_account_display_module_is_served_as_javascript(self):
        response = self.request("GET", "/static/account_display.mjs")

        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])
        self.assertIn("accountDisplay", response.text)

    def test_mobile_voice_assets_are_served_from_same_origin(self):
        capture = self.request("GET", "/static/voice_capture.mjs")
        worklet = self.request("GET", "/static/voice_capture_worklet.js")
        failure = self.request("GET", "/static/voice_failure.mjs")

        self.assertEqual(capture.status_code, 200)
        self.assertIn("javascript", capture.headers["content-type"])
        self.assertEqual(worklet.status_code, 200)
        self.assertIn("javascript", worklet.headers["content-type"])
        self.assertEqual(failure.status_code, 200)
        self.assertIn("javascript", failure.headers["content-type"])

    def test_voice_client_diagnostic_logs_only_bounded_failure_metadata(self):
        with self.assertLogs("apps.dimon_live.app", level="WARNING") as captured:
            response = self.request(
                "POST",
                "/api/voice/diagnostics",
                headers={"user-agent": "Mobile Safari test"},
                json={"stage": "worklet_module", "error_name": "AbortError"},
            )

        self.assertEqual(response.status_code, 204)
        self.assertIn("stage=worklet_module", captured.output[0])
        self.assertIn("error_name=AbortError", captured.output[0])
        self.assertIn("Mobile Safari test", captured.output[0])

    def test_static_script_supports_enter_to_submit_and_shift_enter_for_newline(self):
        script = self.request("GET", "/static/app.js").text

        self.assertIn('event.key === "Enter"', script)
        self.assertIn("!event.shiftKey", script)
        self.assertIn("ui.form.requestSubmit()", script)

    def test_static_script_approves_receipt_without_resubmitting_text(self):
        script = self.request("GET", "/static/app.js").text
        client_response = self.request("GET", "/static/receipt_approval.mjs")
        client = client_response.text

        self.assertEqual(client_response.status_code, 200)
        self.assertIn("candidate-choice", script)
        self.assertIn("requestReceiptApproval,", script)
        self.assertIn("deviceApprover.signChallenge(issued.challenge)", script)
        self.assertIn("/api/receipts/${choice.receipt_id}/approve", client)
        self.assertIn("receipt_digest: choice.receipt_digest, assertion", client)
        device = self.request("GET", "/static/device_approval.mjs")
        self.assertEqual(device.status_code, 200)
        self.assertIn('userVerification: "required"', device.text)
        self.assertNotIn("function candidateCommand", script)
        self.assertNotIn(
            "ui.input.value = result.confirmation_command",
            script,
        )

    def test_static_script_disables_choices_during_approval(self):
        script = self.request("GET", "/static/app.js").text

        self.assertIn("setChoiceBusy(true)", script)
        self.assertIn("setChoiceBusy(false)", script)

    def test_static_voice_uses_automatic_endpoint_and_non_duplicate_error_stop(self):
        script = self.request("GET", "/static/app.js").text

        self.assertIn('"Listening… pause to send"', script)
        self.assertIn('stopVoice({sendStop: false})', script)
        self.assertIn('waitForResultAudio', script)


if __name__ == "__main__":
    unittest.main()

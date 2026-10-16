"""The browser mock signer and the Python gateway must agree byte for byte.

Node runs the real device_approval.mjs with WebCrypto; Python then verifies
its registration proof-of-possession and its approval assertion with the same
code the server uses.
"""

import json
import subprocess
import unittest
from pathlib import Path

from apps.dimon_live.approval_gateway import (
    ApprovalContext,
    ApprovalRejected,
    DeviceAssertion,
    b64url_decode,
    b64url_encode,
    verify_assertion,
    verify_registration,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MODULE = PROJECT_ROOT / "apps" / "dimon_live" / "static" / "device_approval.mjs"
REGISTRATION_CHALLENGE = b"r" * 32
APPROVAL_CHALLENGE = b"a" * 32
CONTEXT = ApprovalContext(
    rp_id="127.0.0.1", allowed_origins=frozenset({"http://127.0.0.1:8890"})
)


def run_mock_signer() -> dict:
    program = f"""
      import {{ DeviceApprover, memoryKeyStore, wireAssertion }} from {json.dumps(MODULE.as_uri())};
      const posted = [];
      const fakeFetch = async (url, options) => {{
        const body = options.body ? JSON.parse(options.body) : null;
        posted.push({{url, body}});
        const payload = url === "/api/signing/registration-challenge"
          ? {{challenge_id: "chal_reg", challenge: {json.dumps(b64url_encode(REGISTRATION_CHALLENGE))},
              rp_id: "127.0.0.1", user: {{id: "demo_user_01", name: "Demo User"}}}}
          : {{credential_id: body.credential_id, kind: body.credential_kind}};
        return {{ok: true, json: async () => payload}};
      }};
      const approver = new DeviceApprover({{
        fetchImpl: fakeFetch,
        location: {{origin: "http://127.0.0.1:8890", hostname: "127.0.0.1"}},
        keyStore: memoryKeyStore(),
        credentials: undefined,
        PublicKeyCredential: undefined,
      }});
      await approver.enroll();
      const registration = posted[1].body;
      const allow = [{{credential_id: registration.credential_id, kind: "MOCK_DEVICE_KEY"}}];
      const first = wireAssertion(await approver.signChallenge({{
        challenge_id: "chal_one",
        challenge: {json.dumps(b64url_encode(APPROVAL_CHALLENGE))},
        allow_credentials: allow,
      }}));
      const second = wireAssertion(await approver.signChallenge({{
        challenge_id: "chal_two",
        challenge: {json.dumps(b64url_encode(APPROVAL_CHALLENGE))},
        allow_credentials: allow,
      }}));
      process.stdout.write(JSON.stringify({{registration, first, second}}));
    """
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", program],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


class DeviceApprovalClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_mock_signer()
        registration = cls.result["registration"]
        cls.public_key = b64url_decode(registration["public_key_spki"])

    def assertion(self, wire) -> DeviceAssertion:
        return DeviceAssertion.from_wire(wire)

    def test_mock_registration_carries_valid_proof_of_possession(self):
        registration = self.result["registration"]
        self.assertEqual(registration["credential_kind"], "MOCK_DEVICE_KEY")
        self.assertEqual(registration["algorithm"], -7)
        verify_registration(
            credential_kind="MOCK_DEVICE_KEY",
            public_key_spki=self.public_key,
            algorithm=-7,
            client_data_json=b64url_decode(registration["client_data_json"]),
            authenticator_data=b64url_decode(registration["authenticator_data"]),
            signature=b64url_decode(registration["signature"]),
            expected_challenge=REGISTRATION_CHALLENGE,
            context=CONTEXT,
        )

    def test_mock_approvals_verify_with_increasing_counters(self):
        count = 0
        for key in ("first", "second"):
            count = verify_assertion(
                self.assertion(self.result[key]),
                credential_kind="MOCK_DEVICE_KEY",
                public_key_spki=self.public_key,
                algorithm=-7,
                stored_sign_count=count,
                expected_challenge=APPROVAL_CHALLENGE,
                context=CONTEXT,
            )
        self.assertEqual(count, 2)

    def test_default_fetch_keeps_the_global_receiver(self):
        program = f"""
          // Browsers throw "Illegal invocation" when fetch gets a foreign receiver.
          globalThis.fetch = function (url) {{
            if (this !== undefined && this !== globalThis) throw new TypeError("Illegal invocation");
            return Promise.resolve({{ok: true, json: async () => ({{called: url}})}});
          }};
          const {{ DeviceApprover, memoryKeyStore }} = await import({json.dumps(MODULE.as_uri())});
          const approver = new DeviceApprover({{
            location: {{origin: "http://127.0.0.1:8890", hostname: "127.0.0.1"}},
            keyStore: memoryKeyStore(),
          }});
          process.stdout.write(JSON.stringify(await approver.post("/api/signing/registration-challenge")));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout), {"called": "/api/signing/registration-challenge"}
        )

    def test_wire_assertion_carries_no_client_metadata(self):
        self.assertEqual(
            sorted(self.result["first"]),
            ["authenticator_data", "challenge_id", "client_data_json", "credential_id", "signature"],
        )

    def test_mock_signature_does_not_verify_for_another_challenge(self):
        with self.assertRaisesRegex(ApprovalRejected, "APPROVAL_CHALLENGE_MISMATCH"):
            verify_assertion(
                self.assertion(self.result["first"]),
                credential_kind="MOCK_DEVICE_KEY",
                public_key_spki=self.public_key,
                algorithm=-7,
                stored_sign_count=0,
                expected_challenge=b"b" * 32,
                context=CONTEXT,
            )


def run_voice_key() -> dict:
    program = f"""
      import {{ DeviceApprover, memoryKeyStore, wireAssertion }} from {json.dumps(MODULE.as_uri())};
      const approver = new DeviceApprover({{
        fetchImpl: async () => {{ throw new Error("no network needed"); }},
        location: {{origin: "http://127.0.0.1:8890", hostname: "127.0.0.1"}},
        keyStore: memoryKeyStore(),
        credentials: undefined,
        PublicKeyCredential: undefined,
      }});
      const registration = await approver.createVoiceKey({{
        challenge_id: "chal_reg", challenge: {json.dumps(b64url_encode(REGISTRATION_CHALLENGE))}, rp_id: "127.0.0.1",
      }});
      const held = await approver.hasKey(registration.credential_id);
      const missing = await approver.hasKey("not-a-key");
      const signed = wireAssertion(await approver.signWithKey({{
        challenge_id: "chal_one", challenge: {json.dumps(b64url_encode(APPROVAL_CHALLENGE))},
      }}, registration.credential_id));
      process.stdout.write(JSON.stringify({{registration, held, missing, signed}}));
    """
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", program],
        cwd=PROJECT_ROOT, capture_output=True, check=False, text=True, encoding="utf-8",
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


class VoiceKeyClientTests(unittest.TestCase):
    """The silent key used for "Yes Confirm" approvals verifies on the server."""

    def test_voice_key_registers_and_signs_without_a_prompt(self):
        result = run_voice_key()
        registration = result["registration"]
        self.assertEqual(registration["credential_kind"], "MOCK_DEVICE_KEY")
        self.assertTrue(result["held"])
        self.assertFalse(result["missing"])
        public_key = b64url_decode(registration["public_key_spki"])
        verify_registration(
            credential_kind="MOCK_DEVICE_KEY", public_key_spki=public_key, algorithm=-7,
            client_data_json=b64url_decode(registration["client_data_json"]),
            authenticator_data=b64url_decode(registration["authenticator_data"]),
            signature=b64url_decode(registration["signature"]),
            expected_challenge=REGISTRATION_CHALLENGE, context=CONTEXT,
        )
        count = verify_assertion(
            DeviceAssertion.from_wire(result["signed"]), credential_kind="MOCK_DEVICE_KEY",
            public_key_spki=public_key, algorithm=-7, stored_sign_count=0,
            expected_challenge=APPROVAL_CHALLENGE, context=CONTEXT,
        )
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()

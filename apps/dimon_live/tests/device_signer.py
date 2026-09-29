"""A software WebAuthn-style authenticator for tests.

It holds a real P-256 private key and produces the same authenticatorData and
clientDataJSON a platform authenticator would, so tests exercise the exact
verification path used for Windows Hello, Touch ID, and the mock device key.
"""

from __future__ import annotations

import hashlib
import json
import os
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from apps.dimon_live.approval_gateway import (
    ApprovalContext,
    DeviceAssertion,
    b64url_encode,
)

FLAGS_PRESENT_AND_VERIFIED = 0x05


class SoftwareAuthenticator:
    def __init__(self, origin: str = "http://127.0.0.1:8890"):
        self.origin = origin
        self.rp_id = urlparse(origin).hostname or ""
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = b64url_encode(os.urandom(16))
        self.counter = 0

    @property
    def context(self) -> ApprovalContext:
        return ApprovalContext(rp_id=self.rp_id, allowed_origins=frozenset({self.origin}))

    @property
    def public_key_spki(self) -> bytes:
        return self.key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def authenticator_data(self, flags: int = FLAGS_PRESENT_AND_VERIFIED, *, rp_id: str | None = None) -> bytes:
        return (
            hashlib.sha256((rp_id or self.rp_id).encode("utf-8")).digest()
            + bytes([flags])
            + self.counter.to_bytes(4, "big")
        )

    def client_data(self, kind: str, challenge: str, *, origin: str | None = None) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": origin or self.origin}
        ).encode("utf-8")

    def enroll(self, database) -> None:
        """KYC onboarding: Myinfo consent, then this device's passkey is bound."""
        consent = database.start_myinfo()
        challenge = database.issue_registration_challenge()
        database.complete_kyc_onboarding(
            consent_id=consent["consent_id"],
            challenge_id=challenge["challenge_id"],
            credential_kind="WEBAUTHN_PLATFORM",
            credential_id=self.credential_id,
            public_key_spki=self.public_key_spki,
            algorithm=-7,
            client_data_json=self.client_data("webauthn.create", challenge["challenge"]),
            authenticator_data=self.authenticator_data(),
            signature=None,
            context=self.context,
        )

    def sign(
        self,
        challenge: dict,
        *,
        flags: int = FLAGS_PRESENT_AND_VERIFIED,
        origin: str | None = None,
        rp_id: str | None = None,
        challenge_text: str | None = None,
    ) -> DeviceAssertion:
        self.counter += 1
        client_data = self.client_data(
            "webauthn.get", challenge_text or challenge["challenge"], origin=origin
        )
        auth_data = self.authenticator_data(flags, rp_id=rp_id)
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()),
        )
        return DeviceAssertion(
            challenge_id=challenge["challenge_id"],
            credential_id=self.credential_id,
            client_data_json=client_data,
            authenticator_data=auth_data,
            signature=signature,
        )

    def approve(self, database, receipt_id: str, receipt_digest: str, kind: str = "single") -> DeviceAssertion:
        challenge = database.issue_approval_challenge(
            kind, receipt_id, receipt_digest, rp_id=self.rp_id
        )
        return self.sign(challenge)

    @staticmethod
    def wire(assertion: DeviceAssertion) -> dict:
        return {
            "challenge_id": assertion.challenge_id,
            "credential_id": assertion.credential_id,
            "client_data_json": b64url_encode(assertion.client_data_json),
            "authenticator_data": b64url_encode(assertion.authenticator_data),
            "signature": b64url_encode(assertion.signature),
        }

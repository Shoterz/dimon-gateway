"""Zero-trust approval verification.

The conversational agent can only create receipts. A receipt moves money only
when the user's device signs a one-time challenge that is cryptographically
bound to that receipt's digest. Signatures use the WebAuthn assertion format
(authenticatorData || SHA-256(clientDataJSON)) so a platform authenticator
(Windows Hello, Touch ID, Face ID) and the labelled mock-biometric device key
are verified by the same code. Nothing here can be reached by model output:
every input is either server-issued (challenge, digest, origin, RP ID) or a
signature over those server-issued values.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature


APPROVAL_DOMAIN = b"dimon-approval-v1"
CHALLENGE_TTL = timedelta(seconds=120)
ALGORITHM_ES256 = -7
ALGORITHM_RS256 = -257
SUPPORTED_ALGORITHMS = (ALGORITHM_ES256, ALGORITHM_RS256)
CREDENTIAL_KINDS = ("WEBAUTHN_PLATFORM", "MOCK_DEVICE_KEY")
FLAG_USER_PRESENT = 0x01
FLAG_USER_VERIFIED = 0x04
MAX_FIELD_BYTES = 4096


class ApprovalRejected(Exception):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_FIELD_BYTES * 2:
        raise ApprovalRejected("APPROVAL_MALFORMED")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise ApprovalRejected("APPROVAL_MALFORMED") from exc


def approval_challenge(purpose: str, challenge_id: str, receipt_digest: str, nonce: str) -> bytes:
    """The exact bytes a device signs: domain-separated and receipt-bound."""

    material = b"\x00".join(
        (
            APPROVAL_DOMAIN,
            purpose.encode("ascii"),
            challenge_id.encode("ascii"),
            receipt_digest.encode("ascii"),
            nonce.encode("ascii"),
        )
    )
    return hashlib.sha256(material).digest()


@dataclass(frozen=True)
class DeviceAssertion:
    """A signature produced by the user's device, as submitted by the browser."""

    challenge_id: str
    credential_id: str
    client_data_json: bytes
    authenticator_data: bytes
    signature: bytes

    @classmethod
    def from_wire(cls, payload: dict) -> "DeviceAssertion":
        try:
            return cls(
                challenge_id=str(payload["challenge_id"]),
                credential_id=str(payload["credential_id"]),
                client_data_json=b64url_decode(payload["client_data_json"]),
                authenticator_data=b64url_decode(payload["authenticator_data"]),
                signature=b64url_decode(payload["signature"]),
            )
        except KeyError as exc:
            raise ApprovalRejected("APPROVAL_MALFORMED") from exc


@dataclass(frozen=True)
class ApprovalContext:
    """Server-derived values the signature must match; never client-chosen."""

    rp_id: str
    allowed_origins: frozenset[str]


def _check_client_data(
    client_data_json: bytes,
    *,
    expected_type: str,
    expected_challenge: bytes,
    allowed_origins: frozenset[str],
) -> None:
    try:
        client_data = json.loads(client_data_json.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApprovalRejected("APPROVAL_MALFORMED") from exc
    if not isinstance(client_data, dict):
        raise ApprovalRejected("APPROVAL_MALFORMED")
    if client_data.get("type") != expected_type:
        raise ApprovalRejected("APPROVAL_WRONG_CEREMONY")
    if client_data.get("challenge") != b64url_encode(expected_challenge):
        raise ApprovalRejected("APPROVAL_CHALLENGE_MISMATCH")
    if client_data.get("origin") not in allowed_origins:
        raise ApprovalRejected("APPROVAL_ORIGIN_MISMATCH")
    if client_data.get("crossOrigin") is True:
        raise ApprovalRejected("APPROVAL_ORIGIN_MISMATCH")


def _check_authenticator_data(authenticator_data: bytes, rp_id: str) -> tuple[bool, int]:
    if len(authenticator_data) < 37:
        raise ApprovalRejected("APPROVAL_MALFORMED")
    if authenticator_data[:32] != hashlib.sha256(rp_id.encode("utf-8")).digest():
        raise ApprovalRejected("APPROVAL_RP_MISMATCH")
    flags = authenticator_data[32]
    if not flags & FLAG_USER_PRESENT:
        raise ApprovalRejected("APPROVAL_USER_NOT_PRESENT")
    if not flags & FLAG_USER_VERIFIED:
        raise ApprovalRejected("APPROVAL_USER_NOT_VERIFIED")
    sign_count = int.from_bytes(authenticator_data[33:37], "big")
    return True, sign_count


def load_public_key(public_key_spki: bytes, algorithm: int):
    try:
        key = serialization.load_der_public_key(public_key_spki)
    except (ValueError, TypeError) as exc:
        raise ApprovalRejected("CREDENTIAL_KEY_INVALID") from exc
    if algorithm == ALGORITHM_ES256:
        if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
            raise ApprovalRejected("CREDENTIAL_KEY_INVALID")
    elif algorithm == ALGORITHM_RS256:
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ApprovalRejected("CREDENTIAL_KEY_INVALID")
    else:
        raise ApprovalRejected("CREDENTIAL_ALGORITHM_UNSUPPORTED")
    return key


def _normalise_signature(signature: bytes, algorithm: int, credential_kind: str) -> bytes:
    # WebCrypto returns raw r||s; WebAuthn returns DER. Accept each only from
    # the credential kind that produces it.
    if algorithm == ALGORITHM_ES256 and credential_kind == "MOCK_DEVICE_KEY":
        if len(signature) != 64:
            raise ApprovalRejected("APPROVAL_SIGNATURE_INVALID")
        return encode_dss_signature(
            int.from_bytes(signature[:32], "big"),
            int.from_bytes(signature[32:], "big"),
        )
    return signature


def verify_signature(
    public_key_spki: bytes,
    algorithm: int,
    credential_kind: str,
    authenticator_data: bytes,
    client_data_json: bytes,
    signature: bytes,
) -> None:
    key = load_public_key(public_key_spki, algorithm)
    signed = authenticator_data + hashlib.sha256(client_data_json).digest()
    signature = _normalise_signature(signature, algorithm, credential_kind)
    try:
        if algorithm == ALGORITHM_ES256:
            key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        else:
            key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise ApprovalRejected("APPROVAL_SIGNATURE_INVALID") from exc


def verify_registration(
    *,
    credential_kind: str,
    public_key_spki: bytes,
    algorithm: int,
    client_data_json: bytes,
    authenticator_data: bytes,
    signature: bytes | None,
    expected_challenge: bytes,
    context: ApprovalContext,
) -> None:
    if credential_kind not in CREDENTIAL_KINDS:
        raise ApprovalRejected("CREDENTIAL_KIND_UNSUPPORTED")
    load_public_key(public_key_spki, algorithm)
    _check_client_data(
        client_data_json,
        expected_type="webauthn.create",
        expected_challenge=expected_challenge,
        allowed_origins=context.allowed_origins,
    )
    _check_authenticator_data(authenticator_data, context.rp_id)
    if credential_kind == "MOCK_DEVICE_KEY":
        # No attestation exists for a software key, so require proof that the
        # browser holds the private half of the key it is enrolling.
        if signature is None:
            raise ApprovalRejected("APPROVAL_SIGNATURE_INVALID")
        verify_signature(
            public_key_spki,
            algorithm,
            credential_kind,
            authenticator_data,
            client_data_json,
            signature,
        )


def verify_assertion(
    assertion: DeviceAssertion,
    *,
    credential_kind: str,
    public_key_spki: bytes,
    algorithm: int,
    stored_sign_count: int,
    expected_challenge: bytes,
    context: ApprovalContext,
) -> int:
    """Verify a device approval and return the authenticator's new sign count."""

    _check_client_data(
        assertion.client_data_json,
        expected_type="webauthn.get",
        expected_challenge=expected_challenge,
        allowed_origins=context.allowed_origins,
    )
    _, sign_count = _check_authenticator_data(assertion.authenticator_data, context.rp_id)
    # A non-increasing counter indicates a cloned authenticator.
    if (sign_count or stored_sign_count) and sign_count <= stored_sign_count:
        raise ApprovalRejected("APPROVAL_COUNTER_REPLAY")
    verify_signature(
        public_key_spki,
        algorithm,
        credential_kind,
        assertion.authenticator_data,
        assertion.client_data_json,
        assertion.signature,
    )
    return sign_count

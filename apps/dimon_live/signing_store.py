"""Persistence for device credentials, one-time approval challenges, and the
stored signatures that prove what the user approved (non-repudiation).

Every function takes an open connection so callers can run it inside the same
BEGIN IMMEDIATE transaction that executes the ledger movement: a challenge is
consumed if and only if the approved receipt commits.
"""

from __future__ import annotations

import base64
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .approval_gateway import (
    CHALLENGE_TTL,
    CREDENTIAL_KINDS,
    SUPPORTED_ALGORITHMS,
    ApprovalContext,
    ApprovalRejected,
    DeviceAssertion,
    approval_challenge,
    b64url_encode,
    verify_assertion,
    verify_registration,
)


SIGNING_SCHEMA = """
CREATE TABLE IF NOT EXISTS signing_credentials (
  credential_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('WEBAUTHN_PLATFORM', 'MOCK_DEVICE_KEY')),
  public_key_spki TEXT NOT NULL,
  algorithm INTEGER NOT NULL CHECK(algorithm IN (-7, -257)),
  rp_id TEXT NOT NULL,
  sign_count INTEGER NOT NULL DEFAULT 0 CHECK(sign_count >= 0),
  status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'REVOKED')),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_challenges (
  challenge_id TEXT PRIMARY KEY,
  purpose TEXT NOT NULL CHECK(
    purpose IN ('REGISTER', 'APPROVE_RECEIPT', 'APPROVE_BATCH')
  ),
  receipt_id TEXT,
  receipt_digest TEXT,
  user_id TEXT NOT NULL,
  nonce TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ISSUED', 'CONSUMED')),
  created_at TEXT NOT NULL,
  consumed_at TEXT
);
CREATE TABLE IF NOT EXISTS approval_signatures (
  signature_id TEXT PRIMARY KEY,
  challenge_id TEXT NOT NULL UNIQUE REFERENCES approval_challenges(challenge_id),
  purpose TEXT NOT NULL,
  receipt_id TEXT NOT NULL,
  receipt_digest TEXT NOT NULL,
  user_id TEXT NOT NULL,
  credential_id TEXT NOT NULL REFERENCES signing_credentials(credential_id),
  credential_kind TEXT NOT NULL,
  client_data_json TEXT NOT NULL,
  authenticator_data TEXT NOT NULL,
  signature TEXT NOT NULL,
  verified_at TEXT NOT NULL
);
"""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _utc(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _challenge_bytes(row: sqlite3.Row | dict) -> bytes:
    return approval_challenge(
        row["purpose"],
        row["challenge_id"],
        row["receipt_digest"] or "",
        row["nonce"],
    )


def issue_challenge(
    connection: sqlite3.Connection,
    *,
    purpose: str,
    user_id: str,
    now: datetime,
    receipt_id: str | None = None,
    receipt_digest: str | None = None,
) -> dict[str, Any]:
    now = _utc(now)
    row = {
        "challenge_id": f"chal_{secrets.token_hex(16)}",
        "purpose": purpose,
        "receipt_id": receipt_id,
        "receipt_digest": receipt_digest,
        "user_id": user_id,
        "nonce": secrets.token_hex(32),
        "expires_at": (now + CHALLENGE_TTL).isoformat(),
        "created_at": now.isoformat(),
    }
    connection.execute(
        """
        INSERT INTO approval_challenges (
          challenge_id, purpose, receipt_id, receipt_digest, user_id, nonce,
          expires_at, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ISSUED', ?)
        """,
        (
            row["challenge_id"],
            row["purpose"],
            row["receipt_id"],
            row["receipt_digest"],
            row["user_id"],
            row["nonce"],
            row["expires_at"],
            row["created_at"],
        ),
    )
    return {
        "challenge_id": row["challenge_id"],
        "challenge": b64url_encode(_challenge_bytes(row)),
        "expires_at": row["expires_at"],
    }


def _take_challenge(
    connection: sqlite3.Connection,
    challenge_id: str,
    *,
    purpose: str,
    user_id: str,
    now: datetime,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM approval_challenges WHERE challenge_id = ?",
        (challenge_id,),
    ).fetchone()
    if row is None or row["purpose"] != purpose:
        raise ApprovalRejected("APPROVAL_CHALLENGE_UNKNOWN")
    if row["user_id"] != user_id:
        raise ApprovalRejected("APPROVAL_USER_MISMATCH")
    if row["status"] != "ISSUED":
        raise ApprovalRejected("APPROVAL_CHALLENGE_REPLAYED")
    if _utc(now) >= datetime.fromisoformat(row["expires_at"]):
        raise ApprovalRejected("APPROVAL_CHALLENGE_EXPIRED")
    consumed = connection.execute(
        """
        UPDATE approval_challenges SET status = 'CONSUMED', consumed_at = ?
        WHERE challenge_id = ? AND status = 'ISSUED'
        """,
        (_utc(now).isoformat(), challenge_id),
    )
    if consumed.rowcount != 1:
        raise ApprovalRejected("APPROVAL_CHALLENGE_REPLAYED")
    return row


def register_credential(
    connection: sqlite3.Connection,
    *,
    challenge_id: str,
    user_id: str,
    credential_kind: str,
    credential_id: str,
    public_key_spki: bytes,
    algorithm: int,
    client_data_json: bytes,
    authenticator_data: bytes,
    signature: bytes | None,
    context: ApprovalContext,
    now: datetime,
) -> dict[str, Any]:
    if credential_kind not in CREDENTIAL_KINDS:
        raise ApprovalRejected("CREDENTIAL_KIND_UNSUPPORTED")
    if algorithm not in SUPPORTED_ALGORITHMS:
        raise ApprovalRejected("CREDENTIAL_ALGORITHM_UNSUPPORTED")
    if not credential_id or len(credential_id) > 1024:
        raise ApprovalRejected("APPROVAL_MALFORMED")
    challenge = _take_challenge(
        connection, challenge_id, purpose="REGISTER", user_id=user_id, now=now
    )
    verify_registration(
        credential_kind=credential_kind,
        public_key_spki=public_key_spki,
        algorithm=algorithm,
        client_data_json=client_data_json,
        authenticator_data=authenticator_data,
        signature=signature,
        expected_challenge=_challenge_bytes(challenge),
        context=context,
    )
    try:
        connection.execute(
            """
            INSERT INTO signing_credentials (
              credential_id, user_id, kind, public_key_spki, algorithm, rp_id,
              sign_count, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
            """,
            (
                credential_id,
                user_id,
                credential_kind,
                _b64(public_key_spki),
                algorithm,
                context.rp_id,
                int.from_bytes(authenticator_data[33:37], "big"),
                _utc(now).isoformat(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ApprovalRejected("CREDENTIAL_ALREADY_REGISTERED") from exc
    return {"credential_id": credential_id, "kind": credential_kind}


def list_credentials(connection: sqlite3.Connection, user_id: str, rp_id: str) -> list[dict]:
    rows = connection.execute(
        """
        SELECT credential_id, kind, created_at FROM signing_credentials
        WHERE user_id = ? AND rp_id = ? AND status = 'ACTIVE'
        ORDER BY created_at
        """,
        (user_id, rp_id),
    ).fetchall()
    return [dict(row) for row in rows]


def prior_signature(
    connection: sqlite3.Connection, assertion: DeviceAssertion
) -> sqlite3.Row | None:
    """The stored signature if this exact assertion was already accepted."""

    row = connection.execute(
        "SELECT * FROM approval_signatures WHERE challenge_id = ?",
        (assertion.challenge_id,),
    ).fetchone()
    if row is None or row["signature"] != _b64(assertion.signature):
        return None
    return row


def consume_approval(
    connection: sqlite3.Connection,
    assertion: DeviceAssertion,
    *,
    purpose: str,
    receipt_id: str,
    receipt_digest: str,
    user_id: str,
    context: ApprovalContext,
    now: datetime,
) -> dict[str, Any]:
    """Verify a device signature over this exact receipt and spend its challenge."""

    challenge = _take_challenge(
        connection, assertion.challenge_id, purpose=purpose, user_id=user_id, now=now
    )
    if challenge["receipt_id"] != receipt_id or challenge["receipt_digest"] != receipt_digest:
        raise ApprovalRejected("APPROVAL_RECEIPT_MISMATCH")
    credential = connection.execute(
        """
        SELECT * FROM signing_credentials
        WHERE credential_id = ? AND user_id = ? AND status = 'ACTIVE'
        """,
        (assertion.credential_id, user_id),
    ).fetchone()
    if credential is None:
        raise ApprovalRejected("CREDENTIAL_UNKNOWN")
    if credential["rp_id"] != context.rp_id:
        raise ApprovalRejected("APPROVAL_RP_MISMATCH")
    sign_count = verify_assertion(
        assertion,
        credential_kind=credential["kind"],
        public_key_spki=base64.b64decode(credential["public_key_spki"]),
        algorithm=credential["algorithm"],
        stored_sign_count=credential["sign_count"],
        expected_challenge=_challenge_bytes(challenge),
        context=context,
    )
    connection.execute(
        "UPDATE signing_credentials SET sign_count = ? WHERE credential_id = ?",
        (sign_count, credential["credential_id"]),
    )
    record = {
        "signature_id": f"sig_{secrets.token_hex(16)}",
        "challenge_id": assertion.challenge_id,
        "purpose": purpose,
        "receipt_id": receipt_id,
        "receipt_digest": receipt_digest,
        "user_id": user_id,
        "credential_id": credential["credential_id"],
        "credential_kind": credential["kind"],
        "client_data_json": _b64(assertion.client_data_json),
        "authenticator_data": _b64(assertion.authenticator_data),
        "signature": _b64(assertion.signature),
        "verified_at": _utc(now).isoformat(),
    }
    connection.execute(
        """
        INSERT INTO approval_signatures (
          signature_id, challenge_id, purpose, receipt_id, receipt_digest,
          user_id, credential_id, credential_kind, client_data_json,
          authenticator_data, signature, verified_at
        ) VALUES (
          :signature_id, :challenge_id, :purpose, :receipt_id, :receipt_digest,
          :user_id, :credential_id, :credential_kind, :client_data_json,
          :authenticator_data, :signature, :verified_at
        )
        """,
        record,
    )
    return {
        "signature_id": record["signature_id"],
        "credential_kind": record["credential_kind"],
        "verified_at": record["verified_at"],
    }

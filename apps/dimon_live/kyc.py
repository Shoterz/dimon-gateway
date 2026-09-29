"""KYC onboarding through a mock Singpass Myinfo, bound to one device.

Singapore banks verify identity for digital onboarding with Singpass Myinfo:
the customer logs in with Singpass and consents to share government-verified
data. This module stands in for that service with a fixed synthetic person,
because the real one needs a registered bank. The flow is otherwise the real
shape:

  1. retrieve Myinfo data  -> a short-lived, single-use consent
  2. customer consents and registers this device's passkey in the same step
  3. KYC becomes VERIFIED and that passkey becomes the ONLY device allowed to
     approve transfers; onboarding again (new consent) replaces the device.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import audit_log
from .approval_gateway import ApprovalContext, ApprovalRejected
from .signing_store import register_credential

CONSENT_TTL = timedelta(minutes=10)
KYC_VALIDITY = timedelta(days=365)

KYC_SCHEMA = """
CREATE TABLE IF NOT EXISTS kyc_consents (
  consent_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  person_sha256 TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  created_at TEXT NOT NULL
);
"""

_PROFILE_COLUMNS = {
    "kyc_method": "TEXT",
    "kyc_name": "TEXT",
    "kyc_uinfin_masked": "TEXT",
    "bound_credential_id": "TEXT",
}

# A fixed, clearly synthetic Myinfo person (not a real individual).
MYINFO_PERSON = {
    "name": "DEMO USER",
    "uinfin_masked": "S****001Z",
    "sex": "FEMALE",
    "dob": "1990-01-01",
    "nationality": "SINGAPORE CITIZEN",
    "residential_status": "CITIZEN",
    "mobile": "+65 9*** 0001",
    "email": "demo.user@example.com",
    "address": "BLK 1 DEMO STREET, #01-01, SINGAPORE 000001",
    "source": "Government-verified (Myinfo mock)",
}
REQUESTED_ATTRIBUTES = (
    "Name", "NRIC/FIN", "Sex", "Date of birth", "Nationality",
    "Residential status", "Mobile number", "Email", "Registered address",
)


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(KYC_SCHEMA)
    present = {row[1] for row in connection.execute("PRAGMA table_info(customer_profiles)")}
    for column, kind in _PROFILE_COLUMNS.items():
        if column not in present:
            connection.execute(f"ALTER TABLE customer_profiles ADD COLUMN {column} {kind}")


def _utc(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


def retrieve_myinfo(connection: sqlite3.Connection, *, user_id: str, now: datetime) -> dict[str, Any]:
    """Mock of Singpass login + Myinfo person retrieval; returns a consent."""

    consent_id = f"consent_{secrets.token_hex(16)}"
    person_sha = hashlib.sha256(json.dumps(MYINFO_PERSON, sort_keys=True).encode("utf-8")).hexdigest()
    connection.execute(
        """
        INSERT INTO kyc_consents (consent_id, user_id, person_sha256, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (consent_id, user_id, person_sha, _utc(now + CONSENT_TTL), _utc(now)),
    )
    audit_log.append(
        connection,
        "MYINFO_RETRIEVED",
        {"consent_id": consent_id, "person_sha256": person_sha, "attributes": list(REQUESTED_ATTRIBUTES)},
        now=now,
    )
    return {
        "consent_id": consent_id,
        "provider": "Singpass Myinfo (mock service)",
        "requested_attributes": list(REQUESTED_ATTRIBUTES),
        "person": dict(MYINFO_PERSON),
        "expires_at": _utc(now + CONSENT_TTL),
    }


def complete_onboarding(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    consent_id: str,
    registration: dict[str, Any],
    context: ApprovalContext,
    now: datetime,
) -> dict[str, Any]:
    """Consume the consent, register the passkey, verify KYC, bind the device."""

    consent = connection.execute(
        "SELECT * FROM kyc_consents WHERE consent_id = ?", (consent_id,)
    ).fetchone()
    if consent is None or consent["user_id"] != user_id:
        raise ApprovalRejected("KYC_CONSENT_UNKNOWN")
    if consent["used_at"] is not None:
        raise ApprovalRejected("KYC_CONSENT_USED")
    if now.astimezone(timezone.utc) >= datetime.fromisoformat(consent["expires_at"]):
        raise ApprovalRejected("KYC_CONSENT_EXPIRED")
    credential = register_credential(connection, user_id=user_id, context=context, now=now, **registration)
    connection.execute(
        "UPDATE kyc_consents SET used_at = ? WHERE consent_id = ?", (_utc(now), consent_id)
    )
    # Only one device may approve: every other credential is revoked.
    connection.execute(
        """
        UPDATE signing_credentials SET status = 'REVOKED'
        WHERE user_id = ? AND credential_id != ? AND status = 'ACTIVE'
        """,
        (user_id, credential["credential_id"]),
    )
    connection.execute(
        """
        UPDATE customer_profiles
        SET kyc_status = 'VERIFIED', kyc_verified_at = ?, kyc_expires_at = ?,
            kyc_method = 'MYINFO', kyc_name = ?, kyc_uinfin_masked = ?,
            bound_credential_id = ?
        WHERE user_id = ?
        """,
        (
            _utc(now),
            _utc(now + KYC_VALIDITY),
            MYINFO_PERSON["name"],
            MYINFO_PERSON["uinfin_masked"],
            credential["credential_id"],
            user_id,
        ),
    )
    audit_log.append(
        connection,
        "KYC_VERIFIED_DEVICE_BOUND",
        {
            "consent_id": consent_id,
            "method": "MYINFO",
            "credential_id": credential["credential_id"],
            "credential_kind": credential["kind"],
        },
        now=now,
    )
    return {"kyc_status": "VERIFIED", **credential}


def status(connection: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT p.kyc_status, p.kyc_verified_at, p.kyc_expires_at, p.kyc_method,
               p.kyc_name, p.kyc_uinfin_masked, p.bound_credential_id, c.kind AS bound_kind
        FROM customer_profiles p
        LEFT JOIN signing_credentials c
          ON c.credential_id = p.bound_credential_id AND c.status = 'ACTIVE'
        WHERE p.user_id = ?
        """,
        (user_id,),
    ).fetchone()
    return dict(row) if row else {"kyc_status": "PENDING"}


def bound_credential_id(connection: sqlite3.Connection, user_id: str) -> str | None:
    row = connection.execute(
        "SELECT bound_credential_id FROM customer_profiles WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0] if row else None

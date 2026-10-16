"""Singpass authorisation for investment transactions (mock Singpass app).

Stock, ETF, bond-fund and monthly-plan orders need a Singpass approval in
addition to the face scan and device signature. The approval is requested for
one exact order (its ID and digest), shown in the Singpass app with the order
details and a two-digit match code that also appears on the Dimon screen,
and is valid once, for five minutes. The mock Singpass service signs every
decision with its own key; the gateway re-verifies that signature and spends
the approval inside the transaction that fills the order.

Only a customer whose identity was verified through Singpass Myinfo can
authorise, and the approval records the verified (masked) NRIC.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

AUTHORISATION_TTL = timedelta(minutes=5)

SINGPASS_SCHEMA = """
CREATE TABLE IF NOT EXISTS singpass_keys (
  key_id TEXT PRIMARY KEY,
  secret_hex TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS singpass_authorisations (
  auth_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  order_digest TEXT NOT NULL,
  uinfin_masked TEXT NOT NULL,
  title TEXT NOT NULL,
  total_minor INTEGER NOT NULL,
  match_code TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING', 'APPROVED', 'REJECTED', 'SUPERSEDED', 'CONSUMED')),
  signature TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  decided_at TEXT,
  consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS singpass_by_order ON singpass_authorisations(order_id);
"""


class SingpassRejected(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _secret(connection: sqlite3.Connection, now: datetime) -> bytes:
    row = connection.execute("SELECT secret_hex FROM singpass_keys WHERE key_id = 'mock-singpass-1'").fetchone()
    if row is None:
        secret = secrets.token_hex(32)
        connection.execute(
            "INSERT INTO singpass_keys (key_id, secret_hex, created_at) VALUES ('mock-singpass-1', ?, ?)",
            (secret, _utc(now)),
        )
        return bytes.fromhex(secret)
    return bytes.fromhex(row[0])


def _sign(secret: bytes, row: Any, status: str, decided_at: str) -> str:
    material = "|".join((
        "dimon-singpass-v1", row["auth_id"], row["user_id"], row["order_id"], row["order_digest"],
        row["uinfin_masked"], status, decided_at,
    ))
    return hmac.new(secret, material.encode("utf-8"), hashlib.sha256).hexdigest()


def _identity(connection: sqlite3.Connection, user_id: str, now: datetime) -> str:
    profile = connection.execute(
        "SELECT kyc_status, kyc_method, kyc_uinfin_masked, kyc_expires_at FROM customer_profiles WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if (
        profile is None
        or profile["kyc_status"] != "VERIFIED"
        or profile["kyc_method"] != "MYINFO"
        or not profile["kyc_uinfin_masked"]
        or not profile["kyc_expires_at"]
        or now.astimezone(timezone.utc) >= datetime.fromisoformat(profile["kyc_expires_at"])
    ):
        raise SingpassRejected("SINGPASS_IDENTITY_REQUIRED")
    return profile["kyc_uinfin_masked"]


def public(row: Any, *, for_app: bool = False) -> dict[str, Any]:
    result = {
        "auth_id": row["auth_id"],
        "order_id": row["order_id"],
        "status": row["status"],
        "title": row["title"],
        "total_minor": row["total_minor"],
        "match_code": row["match_code"],
        "expires_at": row["expires_at"],
        "decided_at": row["decided_at"],
    }
    if for_app:
        result.update(
            requester="Dimon Invest",
            uinfin_masked=row["uinfin_masked"],
            order_digest_short=f"{row['order_digest'][:8]}…{row['order_digest'][-6:]}",
        )
    return result


def request(
    connection: sqlite3.Connection, *, user_id: str, order_id: str, order_digest: str, title: str,
    total_minor: int, now: datetime,
) -> dict[str, Any]:
    """Ask the customer's Singpass app to authorise one exact order."""

    uinfin = _identity(connection, user_id, now)
    connection.execute(
        "UPDATE singpass_authorisations SET status = 'SUPERSEDED' WHERE order_id = ? AND status = 'PENDING'",
        (order_id,),
    )
    row = {
        "auth_id": f"sgauth_{secrets.token_hex(16)}",
        "user_id": user_id,
        "order_id": order_id,
        "order_digest": order_digest,
        "uinfin_masked": uinfin,
        "title": title,
        "total_minor": total_minor,
        "match_code": f"{secrets.randbelow(90) + 10}",
        "status": "PENDING",
        "created_at": _utc(now),
        "expires_at": _utc(now + AUTHORISATION_TTL),
    }
    connection.execute(
        f"INSERT INTO singpass_authorisations ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )
    return public(connection.execute("SELECT * FROM singpass_authorisations WHERE auth_id = ?", (row["auth_id"],)).fetchone())


def _row(connection: sqlite3.Connection, user_id: str, auth_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM singpass_authorisations WHERE auth_id = ? AND user_id = ?", (auth_id, user_id)
    ).fetchone()
    if row is None:
        raise SingpassRejected("SINGPASS_AUTHORISATION_UNKNOWN")
    return row


def status(connection: sqlite3.Connection, user_id: str, auth_id: str, now: datetime) -> dict[str, Any]:
    row = _row(connection, user_id, auth_id)
    result = public(row)
    if row["status"] in ("PENDING", "APPROVED") and now.astimezone(timezone.utc) >= datetime.fromisoformat(row["expires_at"]):
        result["status"] = "EXPIRED"
    return result


def pending(connection: sqlite3.Connection, user_id: str, now: datetime) -> list[dict[str, Any]]:
    """What the Singpass app shows: requests still waiting for a decision."""

    return [
        public(row, for_app=True)
        for row in connection.execute(
            """
            SELECT * FROM singpass_authorisations
            WHERE user_id = ? AND status = 'PENDING' AND expires_at > ?
            ORDER BY created_at DESC
            """,
            (user_id, _utc(now)),
        )
    ]


def decide(connection: sqlite3.Connection, *, user_id: str, auth_id: str, approve: bool, now: datetime) -> dict[str, Any]:
    """The customer's decision in the Singpass app, signed by Singpass."""

    row = _row(connection, user_id, auth_id)
    if row["status"] != "PENDING":
        raise SingpassRejected(f"SINGPASS_AUTHORISATION_{row['status']}")
    if now.astimezone(timezone.utc) >= datetime.fromisoformat(row["expires_at"]):
        raise SingpassRejected("SINGPASS_AUTHORISATION_EXPIRED")
    new_status = "APPROVED" if approve else "REJECTED"
    decided_at = _utc(now)
    connection.execute(
        """
        UPDATE singpass_authorisations SET status = ?, signature = ?, decided_at = ?
        WHERE auth_id = ? AND status = 'PENDING'
        """,
        (new_status, _sign(_secret(connection, now), row, new_status, decided_at), decided_at, auth_id),
    )
    return public(_row(connection, user_id, auth_id))


def _approved(connection: sqlite3.Connection, user_id: str, order_id: str, order_digest: str, now: datetime) -> sqlite3.Row:
    rows = connection.execute(
        """
        SELECT * FROM singpass_authorisations
        WHERE order_id = ? AND user_id = ? AND status = 'APPROVED'
        ORDER BY decided_at DESC
        """,
        (order_id, user_id),
    ).fetchall()
    if not rows:
        rejected = connection.execute(
            "SELECT 1 FROM singpass_authorisations WHERE order_id = ? AND status = 'REJECTED'", (order_id,)
        ).fetchone()
        raise SingpassRejected("SINGPASS_REJECTED" if rejected else "SINGPASS_AUTHORISATION_REQUIRED")
    row = rows[0]
    if row["order_digest"] != order_digest:
        raise SingpassRejected("SINGPASS_ORDER_MISMATCH")
    if now.astimezone(timezone.utc) >= datetime.fromisoformat(row["expires_at"]):
        raise SingpassRejected("SINGPASS_AUTHORISATION_EXPIRED")
    expected = _sign(_secret(connection, now), row, "APPROVED", row["decided_at"])
    if not hmac.compare_digest(expected, row["signature"] or ""):
        raise SingpassRejected("SINGPASS_SIGNATURE_INVALID")
    if row["uinfin_masked"] != _identity(connection, user_id, now):
        raise SingpassRejected("SINGPASS_IDENTITY_MISMATCH")
    return row


def require_approved(connection: sqlite3.Connection, *, user_id: str, order_id: str, order_digest: str, now: datetime) -> None:
    """Check without spending: used before the face and device are asked."""

    _approved(connection, user_id, order_id, order_digest, now)


def consume(connection: sqlite3.Connection, *, user_id: str, order_id: str, order_digest: str, now: datetime) -> dict[str, Any]:
    """Spend the approval once, inside the order's filling transaction."""

    row = _approved(connection, user_id, order_id, order_digest, now)
    spent = connection.execute(
        "UPDATE singpass_authorisations SET status = 'CONSUMED', consumed_at = ? WHERE auth_id = ? AND status = 'APPROVED'",
        (_utc(now), row["auth_id"]),
    )
    if spent.rowcount != 1:
        raise SingpassRejected("SINGPASS_AUTHORISATION_REQUIRED")
    return {"auth_id": row["auth_id"], "uinfin_masked": row["uinfin_masked"], "decided_at": row["decided_at"]}

"""Logging out locks the app; only the registered face unlocks it.

There is no password sign-in: the account lives in this browser's session.
"Log out" therefore locks the account on the server, and while it is locked
every account route refuses to answer. Unlocking needs a live face scan that
matches the face registered at KYC, bound to a one-time unlock challenge, so a
reload or a copied cookie cannot get past the lock screen.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import audit_log

UNLOCK_CHALLENGE_TTL = timedelta(minutes=2)

APP_LOCK_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_locks (
  user_id TEXT PRIMARY KEY,
  locked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unlock_challenges (
  challenge_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  digest TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ISSUED', 'CONSUMED')),
  created_at TEXT NOT NULL,
  consumed_at TEXT
);
"""


class LockRejected(Exception):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def unlock_digest(challenge_id: str) -> str:
    """What the face scan is bound to, so a payment's face match can't unlock."""

    return hashlib.sha256(f"dimon-unlock:{challenge_id}".encode()).hexdigest()


def status(connection: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT locked_at FROM app_locks WHERE user_id = ?", (user_id,)
    ).fetchone()
    return {"locked": row is not None, "locked_at": row[0] if row else None}


def lock(connection: sqlite3.Connection, user_id: str, now: datetime) -> dict[str, Any]:
    connection.execute(
        "INSERT OR IGNORE INTO app_locks (user_id, locked_at) VALUES (?, ?)", (user_id, _utc(now))
    )
    audit_log.append(connection, "APP_LOCKED", {"user_id": user_id}, now=now)
    return status(connection, user_id)


def issue_challenge(connection: sqlite3.Connection, user_id: str, now: datetime) -> dict[str, Any]:
    if not status(connection, user_id)["locked"]:
        raise LockRejected("APP_NOT_LOCKED")
    challenge_id = f"chal_{secrets.token_hex(16)}"
    expires_at = _utc(now + UNLOCK_CHALLENGE_TTL)
    connection.execute(
        """
        INSERT INTO unlock_challenges (challenge_id, user_id, digest, expires_at, status, created_at)
        VALUES (?, ?, ?, ?, 'ISSUED', ?)
        """,
        (challenge_id, user_id, unlock_digest(challenge_id), expires_at, _utc(now)),
    )
    return {"challenge_id": challenge_id, "expires_at": expires_at}


def live_challenge(connection: sqlite3.Connection, challenge_id: str, user_id: str, now: datetime) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM unlock_challenges WHERE challenge_id = ?", (challenge_id,)
    ).fetchone()
    if row is None or row["user_id"] != user_id:
        raise LockRejected("UNLOCK_CHALLENGE_UNKNOWN")
    if row["status"] != "ISSUED":
        raise LockRejected("UNLOCK_CHALLENGE_REPLAYED")
    if now.astimezone(timezone.utc) >= datetime.fromisoformat(row["expires_at"]):
        raise LockRejected("UNLOCK_CHALLENGE_EXPIRED")
    return row


def unlock(connection: sqlite3.Connection, challenge_id: str, user_id: str, now: datetime) -> dict[str, Any]:
    """Spend the challenge and lift the lock. The caller has already spent the face match."""

    updated = connection.execute(
        """
        UPDATE unlock_challenges SET status = 'CONSUMED', consumed_at = ?
        WHERE challenge_id = ? AND user_id = ? AND status = 'ISSUED'
        """,
        (_utc(now), challenge_id, user_id),
    ).rowcount
    if updated != 1:
        raise LockRejected("UNLOCK_CHALLENGE_REPLAYED")
    connection.execute("DELETE FROM app_locks WHERE user_id = ?", (user_id,))
    audit_log.append(connection, "APP_UNLOCKED", {"user_id": user_id, "challenge_id": challenge_id}, now=now)
    return status(connection, user_id)

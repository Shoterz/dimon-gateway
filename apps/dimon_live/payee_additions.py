"""Add a PayNow mobile number as a saved payee: "add 88008800 to my PayNow".

Adding a payee is how many scams start, so it is treated like a payment:

* the request is parsed by fixed rules, never by the model;
* the number is looked up in the PayNow directory, and the user sees the
  masked registered name before anything is saved;
* the exact draft (number, registered name, nickname) is stored and hashed,
  and saving it needs a device signature over that hash plus the registered
  face, checked inside the transaction that saves it;
* the new payee is registered as new, so for 24 hours every payment to it
  needs the one-time code and stays under the new-payee cap.

No money moves when a payee is added.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .grounding import GroundingError, derive_amount_mentions_from_text
from .paynow import PayNowLookupError, normalize_mobile_proxy
from .payees import NEW_PAYEE_WINDOW

ADDITION_TTL = timedelta(minutes=5)

PAYEE_ADDITION_SCHEMA = """
CREATE TABLE IF NOT EXISTS payee_additions (
  addition_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE,
  user_id TEXT NOT NULL,
  canonical_proxy TEXT NOT NULL,
  masked_name TEXT NOT NULL,
  nickname TEXT NOT NULL,
  destination_account_id TEXT NOT NULL REFERENCES accounts(account_id),
  lookup_reference TEXT NOT NULL,
  original_text TEXT NOT NULL,
  addition_digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('AWAITING_CONFIRMATION', 'EXECUTED', 'EXPIRED')),
  beneficiary_id TEXT,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  executed_at TEXT
);
"""

_PROXY = r"(?P<proxy>(?:\+65[\s-]*)?\d{4}[\s-]?\d{4})"
_PAYNOW = r"pay\s*now"
_LIST = rf"(?:(?:my|the)\s+)?(?:{_PAYNOW}(?:\s+(?:contacts?|payees?|list))?|payees?|contacts?|payee\s+list)"
_NICK = r"(?:\s+(?:as|called|named)\s+(?P<nickname>[A-Za-z][A-Za-z '\-]{0,38}[A-Za-z]))?"
_LEAD = r"^\s*(?:please\s+)?(?:add|save|register)\s+"
_PATTERNS = (
    # add 88008800 to my paynow (as Uncle Tan)
    re.compile(rf"{_LEAD}(?:(?:{_PAYNOW}|mobile|number|phone)\s+)?(?:number\s+)?{_PROXY}\s+(?:to|in|into|on|onto)\s+{_LIST}{_NICK}\s*[.!]?\s*$", re.IGNORECASE),
    # add 88008800 as a paynow payee / as uncle tan on paynow
    re.compile(rf"{_LEAD}{_PROXY}\s+as\s+(?:a\s+|my\s+)?(?:new\s+)?{_PAYNOW}\s+(?:payee|contact){_NICK}\s*[.!]?\s*$", re.IGNORECASE),
    # add paynow 88008800 (as Uncle Tan)
    re.compile(rf"{_LEAD}(?:a\s+)?(?:new\s+)?{_PAYNOW}\s+(?:payee\s+|contact\s+|number\s+)?{_PROXY}{_NICK}\s*[.!]?\s*$", re.IGNORECASE),
)


class PayeeAdditionRejected(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def understand(text: str) -> dict[str, str | None] | None:
    """{proxy, proxy_evidence, nickname} for a payee addition, else None."""

    match = next((m for pattern in _PATTERNS if (m := pattern.match(text))), None)
    if match is None:
        return None
    try:
        if derive_amount_mentions_from_text(text):
            return None   # an amount means a payment, not an addition
    except GroundingError:
        return None
    evidence = match.group("proxy")
    try:
        proxy = normalize_mobile_proxy(evidence)
    except PayNowLookupError:
        return None
    nickname = match.group("nickname")
    return {
        "proxy": proxy,
        "proxy_evidence": evidence,
        "nickname": " ".join(nickname.split()) if nickname else None,
    }


def _utc(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


def _digest(record: dict[str, Any]) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def masked_proxy(proxy: str) -> str:
    return f"PayNow •••• {proxy[-4:]}"


def check_not_saved(connection: sqlite3.Connection, destination_account_id: str) -> None:
    if connection.execute(
        "SELECT 1 FROM beneficiaries WHERE destination_account_id = ? AND directory_kind = 'SAVED'",
        (destination_account_id,),
    ).fetchone():
        raise PayeeAdditionRejected("PAYEE_ALREADY_SAVED")


def check_nickname(connection: sqlite3.Connection, nickname: str) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z '\-]{1,38}[A-Za-z]", nickname):
        raise PayeeAdditionRejected("INVALID_NICKNAME")
    taken = connection.execute(
        """
        SELECT 1 FROM beneficiaries WHERE normalized_alias = :n OR lower(display_name) = :n
        UNION SELECT 1 FROM beneficiary_aliases WHERE alias = :n
        """,
        {"n": nickname.casefold()},
    ).fetchone()
    if taken:
        raise PayeeAdditionRejected("NICKNAME_IN_USE")


def draft(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    request_id: str,
    text: str,
    lookup: Any,
    nickname: str,
    now: datetime,
) -> dict[str, Any]:
    check_not_saved(connection, lookup.destination_account_id)
    check_nickname(connection, nickname)
    record = {
        "addition_id": f"addition_{secrets.token_hex(16)}",
        "user_id": user_id,
        "canonical_proxy": lookup.canonical_proxy,
        "masked_name": lookup.masked_name,
        "nickname": nickname,
        "destination_account_id": lookup.destination_account_id,
        "lookup_reference": lookup.lookup_reference,
        "expires_at": _utc(now + ADDITION_TTL),
        "nonce": secrets.token_hex(16),
    }
    digest = _digest(record)
    connection.execute(
        """
        INSERT INTO payee_additions (
          addition_id, request_id, user_id, canonical_proxy, masked_name, nickname,
          destination_account_id, lookup_reference, original_text, addition_digest,
          status, expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'AWAITING_CONFIRMATION', ?, ?)
        """,
        (
            record["addition_id"], request_id, user_id, record["canonical_proxy"],
            record["masked_name"], nickname, record["destination_account_id"],
            record["lookup_reference"], text, digest, record["expires_at"], _utc(now),
        ),
    )
    return public(connection.execute(
        "SELECT * FROM payee_additions WHERE addition_id = ?", (record["addition_id"],)
    ).fetchone())


def public(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "addition_id": row["addition_id"],
        # receipt_id / receipt_digest let the browser's signing helper treat
        # this like any other signed receipt.
        "receipt_id": row["addition_id"],
        "receipt_digest": row["addition_digest"],
        "masked_name": row["masked_name"],
        "nickname": row["nickname"],
        "masked_proxy": masked_proxy(row["canonical_proxy"]),
        "status": row["status"],
        "expires_at": row["expires_at"],
    }


def read_back(row: sqlite3.Row) -> str:
    digits = " ".join(row["canonical_proxy"][-4:])
    return (
        f"Add a new PayNow payee: mobile ending {digits}, registered to "
        f"{row['masked_name']}, saved as {row['nickname']}. No money moves now. "
        f"For the next 24 hours, payments to this payee need a one-time code."
    )


def review(row: sqlite3.Row) -> dict[str, Any]:
    """What the confirmation overlay shows, from the stored draft only."""

    return {
        "kind": "PAYEE",
        "receipt_id": row["addition_id"],
        "receipt_digest": row["addition_digest"],
        "source_label": "—",
        "currency": "SGD",
        "total_minor": 0,
        "items": [{
            "recipient_display_name": f"{row['nickname']} ({row['masked_name']})",
            "masked_destination": masked_proxy(row["canonical_proxy"]),
            "amount_minor": 0,
            "new_payee": True,
        }],
        "receipt_expires_at": row["expires_at"],
        "validation": {
            "verdict": "PASS", "fields": [],
            "note": "Number checked in the PayNow directory. Built by fixed rules from your own words; the AI took no part.",
        },
        "requires_out_of_band": False,
        "requires_read_back": False,
        "requires_unusual_confirmation": False,
        "read_back": {"text": read_back(row), "audio_artifact_id": None},
    }


def awaiting(connection: sqlite3.Connection, addition_id: str, digest: str, user_id: str, now: datetime) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM payee_additions WHERE addition_id = ?", (addition_id,)).fetchone()
    if row is None:
        raise PayeeAdditionRejected("RECEIPT_NOT_FOUND")
    if row["user_id"] != user_id:
        raise PayeeAdditionRejected("RECEIPT_USER_MISMATCH")
    if row["addition_digest"] != digest:
        raise PayeeAdditionRejected("RECEIPT_DIGEST_MISMATCH")
    if row["status"] == "EXECUTED":
        raise PayeeAdditionRejected("RECEIPT_ALREADY_EXECUTED")
    if row["status"] != "AWAITING_CONFIRMATION":
        raise PayeeAdditionRejected(f"RECEIPT_{row['status']}")
    if now.astimezone(timezone.utc) >= datetime.fromisoformat(row["expires_at"]):
        raise PayeeAdditionRejected("RECEIPT_EXPIRED")
    return row


def save(connection: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> dict[str, Any]:
    """Create the saved payee. Re-checks everything that could have changed."""

    check_not_saved(connection, row["destination_account_id"])
    check_nickname(connection, row["nickname"])
    proxy = connection.execute(
        "SELECT is_active FROM paynow_proxies WHERE canonical_proxy = ?", (row["canonical_proxy"],)
    ).fetchone()
    if proxy is None or not proxy["is_active"]:
        raise PayeeAdditionRejected("PAYNOW_PROXY_INACTIVE")
    beneficiary_id = f"beneficiary_saved_{secrets.token_hex(8)}"
    connection.execute(
        """
        INSERT INTO beneficiaries (
          beneficiary_id, display_name, normalized_alias,
          destination_account_id, masked_destination, directory_kind
        ) VALUES (?, ?, ?, ?, ?, 'SAVED')
        """,
        (beneficiary_id, row["nickname"], row["nickname"].casefold(),
         row["destination_account_id"], masked_proxy(row["canonical_proxy"])),
    )
    connection.execute(
        "INSERT INTO payee_registrations (beneficiary_id, added_at, origin) VALUES (?, ?, 'PAYNOW')",
        (beneficiary_id, _utc(now)),
    )
    connection.execute(
        "UPDATE payee_additions SET status = 'EXECUTED', beneficiary_id = ?, executed_at = ? WHERE addition_id = ?",
        (beneficiary_id, _utc(now), row["addition_id"]),
    )
    return {
        "beneficiary_id": beneficiary_id,
        "display_name": row["nickname"],
        "masked_name": row["masked_name"],
        "masked_destination": masked_proxy(row["canonical_proxy"]),
        "new_payee_until": _utc(now + NEW_PAYEE_WINDOW),
    }

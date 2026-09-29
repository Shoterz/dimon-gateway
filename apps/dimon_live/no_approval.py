"""Requests that move no money out and so need no approval.

"What's my balance?" and "Request S$20 from John Smith" are answered by
deterministic rules before the model runs. Neither can debit an account: a
balance is read-only, and a payment request only records that the user asked
someone to pay them. Anything that also sounds like a payment is left to the
normal draft-and-sign path.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from .grounding import (
    GroundingError,
    _BARE_NUMBER,
    _bare_amount_minor,
    derive_amount_mentions_from_text,
)

REQUESTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS money_requests (
  money_request_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE,
  beneficiary_id TEXT NOT NULL REFERENCES beneficiaries(beneficiary_id),
  amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
  status TEXT NOT NULL CHECK(status IN ('SENT')),
  created_at TEXT NOT NULL
);
"""

_PAYS_OUT = re.compile(r"\b(?:send|pay|transfer|give|bank\s+in|pass|move|paynow)\b", re.IGNORECASE)
_BALANCE = re.compile(
    r"\b(?:balances?|how\s+much\s+(?:money\s+)?(?:do\s+i\s+have|is\s+(?:left|in))|what'?s\s+in\s+my)\b",
    re.IGNORECASE,
)
_REQUEST = re.compile(
    r"^\W*(?:(?:please|pls|can\s+you|could\s+you|help\s+me)\s+)*"
    r"(?:request|ask(?:\s+for)?|collect)\b",
    re.IGNORECASE,
)
# "request 15 from alicia" / "ask my mum for fifty (back)"
_REQUEST_BARE_AMOUNT = re.compile(
    r"\b(?:request|ask\s+for|ask|collect)\s+(?P<amount>" + _BARE_NUMBER + r")\s+from\b"
    r"|\bfor\s+(?P<tail>" + _BARE_NUMBER + r")(?=\s*(?:[.,!?]|$)|\s+(?:back|lah|can|please|pls)\b)",
    re.IGNORECASE,
)


def is_balance_inquiry(text: str) -> bool:
    return bool(_BALANCE.search(text)) and not _PAYS_OUT.search(text)


def looks_like_money_request(text: str) -> bool:
    return bool(_REQUEST.search(text)) and not _PAYS_OUT.search(text)


@dataclass(frozen=True)
class MoneyRequest:
    beneficiary: dict[str, Any] | None
    amount_minor: int | None
    reason_code: str | None


def _mentions(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE) is not None


def parse_money_request(text: str, directory: Sequence[dict[str, Any]]) -> MoneyRequest:
    """Find exactly one saved payee (full name, nickname, or a unique first
    name) and exactly one amount in the user's words."""

    named = [
        entry for entry in directory
        if _mentions(text, entry["display_name"])
        or any(_mentions(text, alias) for alias in entry.get("aliases") or ())
    ]
    if not named:
        named = [
            entry for entry in directory
            if _mentions(text, entry["display_name"].split()[0])
        ]
    if not named:
        return MoneyRequest(None, None, "UNKNOWN_RECIPIENT")
    if len({entry["beneficiary_id"] for entry in named}) != 1:
        return MoneyRequest(None, None, "AMBIGUOUS_RECIPIENT")
    try:
        mentions = derive_amount_mentions_from_text(text)
        if not mentions:
            bare = _REQUEST_BARE_AMOUNT.search(text)
            amounts = [_bare_amount_minor(bare.group("amount") or bare.group("tail"))] if bare else []
        else:
            amounts = [mention.amount_minor for mention in mentions]
    except GroundingError as exc:
        return MoneyRequest(named[0], None, exc.reason_code)
    if len(amounts) != 1:
        return MoneyRequest(named[0], None, "MISSING_AMOUNT" if not amounts else "AMBIGUOUS_AMOUNT")
    return MoneyRequest(named[0], amounts[0], None)


def record_money_request(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    beneficiary_id: str,
    amount_minor: int,
    now: datetime,
) -> str:
    money_request_id = f"moneyreq_{secrets.token_hex(12)}"
    connection.execute(
        """
        INSERT INTO money_requests (
          money_request_id, request_id, beneficiary_id, amount_minor, status, created_at
        ) VALUES (?, ?, ?, ?, 'SENT', ?)
        """,
        (money_request_id, request_id, beneficiary_id, amount_minor, now.astimezone(timezone.utc).isoformat()),
    )
    return money_request_id

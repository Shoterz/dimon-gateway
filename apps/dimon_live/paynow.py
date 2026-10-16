"""Exact synthetic PayNow proxy normalization and directory lookup."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .database import Database


class PayNowLookupError(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class PayNowLookup:
    lookup_reference: str
    canonical_proxy: str
    masked_name: str
    beneficiary_id: str
    destination_account_id: str
    expires_at: str


class PayNowDirectory(Protocol):
    def lookup_mobile(self, canonical_proxy: str, request_id: str) -> PayNowLookup: ...


_ALLOWED_PROXY = re.compile(r"^[+0-9()\s-]+$")
_PAYNOW_MENTION = re.compile(r"\bpay\s*now\b", re.IGNORECASE)
_MOBILE_PROXY_MENTION = re.compile(
    r"(?<!\d)(?:\+65[\s-]*)?(?:\(\s*\d{4}\s*\)|\d{4})[\s-]?\d{4}(?!\d)"
)
_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9",
}
_SPOKEN_MOBILE_PROXY = re.compile(
    r"\b(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)"
    r"(?:[\s-]+(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)){7}\b",
    re.IGNORECASE,
)


def normalize_mobile_proxy(evidence: str) -> str:
    if not isinstance(evidence, str) or not evidence.strip():
        raise PayNowLookupError("INVALID_PAYNOW_PROXY")
    value = evidence.strip()
    spoken = re.findall(r"[A-Za-z]+", value.casefold())
    if len(spoken) == 8 and all(word in _DIGIT_WORDS for word in spoken):
        return "".join(_DIGIT_WORDS[word] for word in spoken)
    if _ALLOWED_PROXY.fullmatch(value) is None:
        raise PayNowLookupError("INVALID_PAYNOW_PROXY")
    if "+" in value and not value.startswith("+65"):
        raise PayNowLookupError("INVALID_PAYNOW_PROXY")
    if value.startswith("+65"):
        value = value[3:]
    elif "+" in value:
        raise PayNowLookupError("INVALID_PAYNOW_PROXY")
    digits = re.sub(r"[()\s-]", "", value)
    if not digits.isdecimal() or len(digits) != 8:
        raise PayNowLookupError("INVALID_PAYNOW_PROXY")
    return digits


def derive_mobile_proxy_from_text(value: str) -> tuple[str | None, str | None]:
    """Derive one PayNow mobile proxy from user-owned text.

    A number is execution evidence only when the user explicitly mentions
    PayNow. The model cannot add, remove, or replace this destination. More
    than one proxy is ambiguous and must not be guessed.
    """

    if not isinstance(value, str) or _PAYNOW_MENTION.search(value) is None:
        return None, None
    matches = [*_MOBILE_PROXY_MENTION.finditer(value), *_SPOKEN_MOBILE_PROXY.finditer(value)]
    matches.sort(key=lambda match: match.start())
    if not matches:
        return None, None
    if len(matches) != 1:
        raise PayNowLookupError("AMBIGUOUS_PAYNOW_PROXY")
    evidence = matches[0].group(0)
    return normalize_mobile_proxy(evidence), evidence


class SyntheticPayNowDirectory:
    def __init__(self, database: "Database"):
        self.database = database

    def lookup_mobile(self, canonical_proxy: str, request_id: str) -> PayNowLookup:
        canonical = normalize_mobile_proxy(canonical_proxy)
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = self.database._clock()
                existing = connection.execute(
                    "SELECT * FROM paynow_lookups WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if existing is not None:
                    if existing["canonical_proxy"] != canonical:
                        raise PayNowLookupError("IDEMPOTENCY_CONFLICT")
                    connection.rollback()
                    return PayNowLookup(
                        existing["lookup_reference"],
                        existing["canonical_proxy"],
                        existing["masked_name"],
                        existing["beneficiary_id"],
                        existing["destination_account_id"],
                        existing["expires_at"],
                    )
                proxy = connection.execute(
                    """
                    SELECT p.*, b.destination_account_id
                    FROM paynow_proxies p
                    JOIN beneficiaries b ON b.beneficiary_id = p.beneficiary_id
                    WHERE p.canonical_proxy = ?
                    """,
                    (canonical,),
                ).fetchone()
                if proxy is None:
                    raise PayNowLookupError("PAYNOW_PROXY_NOT_FOUND")
                if not proxy["is_active"]:
                    raise PayNowLookupError("PAYNOW_PROXY_INACTIVE")
                lookup_reference = f"lookup_{uuid.uuid4().hex}"
                created_at = now.astimezone(timezone.utc).isoformat()
                expires_at = (
                    now.astimezone(timezone.utc) + timedelta(minutes=5)
                ).isoformat()
                connection.execute(
                    """
                    INSERT INTO paynow_lookups (
                      lookup_reference, request_id, canonical_proxy,
                      masked_name, beneficiary_id, destination_account_id,
                      expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lookup_reference,
                        request_id,
                        canonical,
                        proxy["masked_name"],
                        proxy["beneficiary_id"],
                        proxy["destination_account_id"],
                        expires_at,
                        created_at,
                    ),
                )
                connection.commit()
                return PayNowLookup(
                    lookup_reference,
                    canonical,
                    proxy["masked_name"],
                    proxy["beneficiary_id"],
                    proxy["destination_account_id"],
                    expires_at,
                )
            except Exception:
                connection.rollback()
                raise

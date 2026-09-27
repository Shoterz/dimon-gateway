"""Immutable canonical records for atomic saved-beneficiary batches."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


BATCH_RECEIPT_CONTRACT_VERSION = 1
BATCH_RECEIPT_TTL = timedelta(minutes=5)


@dataclass(frozen=True)
class BatchReceiptItemDraft:
    position: int
    beneficiary_id: str
    destination_account_id: str
    recipient_display_name: str
    amount_minor: int
    recipient_evidence: str
    amount_evidence: str


@dataclass(frozen=True)
class BatchReceiptDraft:
    request_id: str
    original_text: str
    user_id: str
    source_account_id: str
    currency: str
    items: tuple[BatchReceiptItemDraft, ...]


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_batch_receipt_record(
    draft: BatchReceiptDraft,
    *,
    now: datetime,
    receipt_id: str,
    nonce: str,
) -> dict[str, Any]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if draft.currency != "SGD":
        raise ValueError("currency must be SGD")
    if not 2 <= len(draft.items) <= 4:
        raise ValueError("batch must contain two to four items")

    positions = [item.position for item in draft.items]
    beneficiaries = [item.beneficiary_id for item in draft.items]
    if len(set(positions)) != len(positions) or any(
        type(position) is not int or position < 0 for position in positions
    ):
        raise ValueError("batch item positions must be unique non-negative integers")
    if len(set(beneficiaries)) != len(beneficiaries):
        raise ValueError("batch beneficiaries must be unique")
    for item in draft.items:
        if type(item.amount_minor) is not int or item.amount_minor <= 0:
            raise ValueError("amount_minor must be a positive integer")
        for value in (
            item.beneficiary_id,
            item.destination_account_id,
            item.recipient_display_name,
            item.recipient_evidence,
            item.amount_evidence,
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("batch item fields must be non-empty strings")

    created_at = now.astimezone(timezone.utc).isoformat()
    expires_at = (now.astimezone(timezone.utc) + BATCH_RECEIPT_TTL).isoformat()
    items = [
        {
            "amount_evidence": item.amount_evidence,
            "amount_minor": item.amount_minor,
            "beneficiary_id": item.beneficiary_id,
            "destination_account_id": item.destination_account_id,
            "position": item.position,
            "recipient_display_name": item.recipient_display_name,
            "recipient_evidence": item.recipient_evidence,
        }
        for item in draft.items
    ]
    total_minor = sum(item["amount_minor"] for item in items)
    payload = {
        "contract_version": BATCH_RECEIPT_CONTRACT_VERSION,
        "currency": draft.currency,
        "expires_at": expires_at,
        "items": items,
        "nonce": nonce,
        "original_text": draft.original_text,
        "receipt_id": receipt_id,
        "request_id": draft.request_id,
        "source_account_id": draft.source_account_id,
        "total_minor": total_minor,
        "user_id": draft.user_id,
    }
    serialized = _canonical_json(payload)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return {
        **payload,
        "canonical_payload": payload,
        "canonical_json": serialized,
        "receipt_digest": digest,
        "status": "AWAITING_CONFIRMATION",
        "created_at": created_at,
    }

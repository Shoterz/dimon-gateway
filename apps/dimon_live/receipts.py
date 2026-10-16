"""Immutable receipt primitives for the synthetic transfer gateway."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


DEMO_USER_ID = "demo_user_01"
RECEIPT_CONTRACT_VERSION = 2
RECEIPT_TTL = timedelta(minutes=5)


@dataclass(frozen=True)
class ReceiptDraft:
    request_id: str
    original_text: str
    user_id: str
    source_account_id: str
    beneficiary_id: str
    recipient_display_name: str
    amount_minor: int
    currency: str
    destination_kind: str = "SAVED_BENEFICIARY"
    destination_reference: str | None = None
    destination_account_id: str | None = None
    lookup_reference: str | None = None
    evidence_bundle: tuple[Mapping[str, Any], ...] = ()
    contract_version: int = RECEIPT_CONTRACT_VERSION


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest_canonical(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_expected_receipt_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct the exact canonical fields for a persisted receipt version."""

    version = row["contract_version"]
    base = {
        "amount_minor": row["amount_minor"],
        "beneficiary_id": row["beneficiary_id"],
        "contract_version": version,
        "currency": row["currency"],
        "expires_at": row["expires_at"],
        "nonce": row["nonce"],
        "original_text": row["original_text"],
        "receipt_id": row["receipt_id"],
        "recipient_display_name": row["recipient_display_name"],
        "request_id": row["request_id"],
        "source_account_id": row["source_account_id"],
        "user_id": row["user_id"],
    }
    if version == 1:
        return base
    if version != 2:
        raise ValueError("RECEIPT_INVALID_VERSION")

    try:
        evidence_bundle = json.loads(row["evidence_bundle_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("RECEIPT_INVALID_EVIDENCE") from exc
    if not isinstance(evidence_bundle, list) or any(
        not isinstance(item, dict) for item in evidence_bundle
    ):
        raise ValueError("RECEIPT_INVALID_EVIDENCE")
    expected = {
        **base,
        "destination_kind": row["destination_kind"],
        "destination_reference": row["destination_reference"],
        "evidence_bundle": evidence_bundle,
        "lookup_reference": row["lookup_reference"],
    }
    if row["destination_kind"] in ("PAYNOW_MOBILE", "UTILITY_BILL"):
        expected["destination_account_id"] = row["destination_account_id"]
    return expected


def build_receipt_record(
    draft: ReceiptDraft,
    *,
    now: datetime,
    receipt_id: str,
    nonce: str,
) -> dict[str, Any]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if type(draft.amount_minor) is not int or draft.amount_minor <= 0:
        raise ValueError("amount_minor must be a positive integer")
    if draft.currency != "SGD":
        raise ValueError("currency must be SGD")
    if draft.contract_version not in (1, 2):
        raise ValueError("unsupported receipt contract version")

    created_at = now.astimezone(timezone.utc).isoformat()
    expires_at = (now.astimezone(timezone.utc) + RECEIPT_TTL).isoformat()
    payload = {
        "amount_minor": draft.amount_minor,
        "beneficiary_id": draft.beneficiary_id,
        "contract_version": draft.contract_version,
        "currency": draft.currency,
        "expires_at": expires_at,
        "nonce": nonce,
        "original_text": draft.original_text,
        "receipt_id": receipt_id,
        "recipient_display_name": draft.recipient_display_name,
        "request_id": draft.request_id,
        "source_account_id": draft.source_account_id,
        "user_id": draft.user_id,
    }
    record_fields: dict[str, Any] = {}
    if draft.contract_version == 2:
        if draft.destination_kind not in ("SAVED_BENEFICIARY", "PAYNOW_MOBILE", "UTILITY_BILL"):
            raise ValueError("invalid destination_kind")
        destination_reference = (
            draft.destination_reference
            if draft.destination_reference is not None
            else draft.beneficiary_id
        )
        if not destination_reference:
            raise ValueError("destination_reference is required")
        if draft.destination_kind == "SAVED_BENEFICIARY" and draft.lookup_reference:
            raise ValueError("saved beneficiary cannot have lookup_reference")
        if draft.destination_kind == "PAYNOW_MOBILE" and not draft.lookup_reference:
            raise ValueError("PayNow receipt requires lookup_reference")
        if draft.destination_kind == "PAYNOW_MOBILE" and not draft.destination_account_id:
            raise ValueError("PayNow receipt requires destination_account_id")
        if draft.destination_kind == "UTILITY_BILL" and not draft.destination_account_id:
            raise ValueError("utility bill receipt requires destination_account_id")
        evidence_bundle = [dict(item) for item in draft.evidence_bundle]
        canonical_json(evidence_bundle)
        payload.update(
            {
                "destination_kind": draft.destination_kind,
                "destination_reference": destination_reference,
                "evidence_bundle": evidence_bundle,
                "lookup_reference": draft.lookup_reference,
            }
        )
        if draft.destination_kind in ("PAYNOW_MOBILE", "UTILITY_BILL"):
            payload["destination_account_id"] = draft.destination_account_id
        record_fields = {
            "destination_kind": draft.destination_kind,
            "destination_reference": destination_reference,
            "destination_account_id": draft.destination_account_id,
            "evidence_bundle_json": canonical_json(evidence_bundle),
            "lookup_reference": draft.lookup_reference,
        }
    serialized = canonical_json(payload)
    return {
        **payload,
        **record_fields,
        "canonical_payload": payload,
        "canonical_json": serialized,
        "receipt_digest": digest_canonical(serialized),
        "status": "AWAITING_CONFIRMATION",
        "created_at": created_at,
    }

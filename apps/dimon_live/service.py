"""Deterministic guarded-autopilot orchestration for Dimon Live."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .batch_grounding import bind_batch_evidence
from .batch_receipts import BatchReceiptDraft, BatchReceiptItemDraft
from .database import (
    ActionAttempt,
    ClarificationRejected,
    Database,
    IdempotencyConflict,
    InsufficientFunds,
    ReceiptRejected,
    TransferCommand,
)
from .grounding import (
    derive_amount_from_text,
    derive_amount_mentions_from_text,
    GroundingError,
    is_grounded,
    normalize_evidence,
    verify_optional_reference,
)
from .model_adapter import Extractor, InferenceFailure
from .paynow import (
    PayNowLookupError,
    SyntheticPayNowDirectory,
    derive_mobile_proxy_from_text,
    normalize_mobile_proxy,
)
from .receipts import DEMO_USER_ID, ReceiptDraft


NEGATED_ACTION_PATTERN = re.compile(
    r"\b(?:do\s+not|don't|dont|never|cancel|stop)\b",
    re.IGNORECASE,
)

_BATCH_UNSUPPORTED_LANGUAGE_PATTERN = re.compile(
    r"%|"
    r"\b(?:each|every|total|subtotal|split|equally|remainder|remaining|"
    r"percent|percentage|half|quarter|twice)\b|"
    r"\b(?:today|tomorrow|tonight|later)\b|"
    r"\bnext\s+(?:day|week|month|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)\b|"
    r"\bon\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"\d{1,2}(?:st|nd|rd|th)?(?:\s+[a-z]+)?)\b|"
    r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\b",
    re.IGNORECASE,
)
_BATCH_PARTIAL_NEGATION_PATTERN = re.compile(r"\bnot\b", re.IGNORECASE)
_BATCH_PAYNOW_PATTERN = re.compile(r"\bpay\s*now\b", re.IGNORECASE)
_BATCH_SOURCE_CLAUSE_PATTERN = re.compile(
    r"\bfrom\s+(?:(?:my|the)\s+)?(?P<account>[a-z][a-z0-9_-]*)\b",
    re.IGNORECASE,
)
_BATCH_COMMAND_PATTERN = re.compile(
    r"^\s*(?:please\s+)?(?:send|pay|transfer)\b",
    re.IGNORECASE,
)


def _decimal_amount(amount_minor: int) -> str:
    major, minor = divmod(amount_minor, 100)
    return f"{major}.{minor:02d}"


def _same_grounded_value(text: str, value: str, evidence: str) -> bool:
    return is_grounded(text, evidence) and normalize_evidence(value) in normalize_evidence(
        evidence
    )


def _has_affirmative_target_slot(
    text: str,
    recipient: str,
    amount_evidence: str,
    source_label: str,
) -> bool:
    normalized_text = normalize_evidence(text)
    normalized_recipient = normalize_evidence(recipient)
    normalized_amount = normalize_evidence(amount_evidence)
    normalized_source = normalize_evidence(source_label)
    command = re.match(
        r"^(?:please\s+)?(?:send|pay|transfer)\s+",
        normalized_text,
    )
    if command is None:
        return False
    tail = normalized_text[command.end() :].rstrip(".!")
    optional_source = rf"(?:\s+from\s+{re.escape(normalized_source)})?"
    recipient_first = re.fullmatch(
        rf"{re.escape(normalized_recipient)}\s+"
        rf"{re.escape(normalized_amount)}{optional_source}",
        tail,
    )
    amount_first = re.fullmatch(
        rf"{re.escape(normalized_amount)}\s+to\s+"
        rf"{re.escape(normalized_recipient)}{optional_source}",
        tail,
    )
    return recipient_first is not None or amount_first is not None


def _evidence_bundle(text: str, intent: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    item: dict[str, Any] = {"turn": 1, "text": text}
    for output_key, evidence_key in (
        ("source", "source_evidence"),
        ("recipient", "recipient_evidence"),
        ("proxy", "proxy_evidence"),
        ("amount", "amount_evidence"),
        ("reference", "reference_evidence"),
    ):
        if intent.get(evidence_key) is not None:
            item[output_key] = intent[evidence_key]
    return (item,)


class ActionService:
    def __init__(self, database: Database, extractor: Extractor):
        self.database = database
        self.extractor = extractor
        self.paynow_directory = SyntheticPayNowDirectory(database)

    def process(self, request_id: str, text: str) -> dict[str, Any]:
        existing = self.database.get_attempt(request_id)
        if existing is not None:
            if existing["original_text"] != text:
                return {
                    "outcome": "BLOCKED",
                    "request_id": request_id,
                    "reason_code": "IDEMPOTENCY_CONFLICT",
                    "trace": [{"stage": "IDEMPOTENCY_CONFLICT"}],
                    "state": self.database.get_state(),
                }
            return {
                **existing["response"],
                "trace": [{"stage": "IDEMPOTENT_REPLAY"}],
                "idempotent_replay": True,
                "state": self.database.get_state(),
            }

        trace: list[dict[str, Any]] = []
        try:
            extracted = self.extractor.extract(text)
        except InferenceFailure as exc:
            return self._nonexecuting(
                request_id,
                text,
                "FAILED",
                exc.reason_code,
                trace,
            )

        intent = extracted.intent
        trace.append(
            {
                "stage": "MODEL_RESPONSE_RECEIVED",
                "latency_ms": extracted.latency_ms,
            }
        )
        trace.append({"stage": "INTENT_VALIDATED"})

        recovered_batch = self._recover_exact_saved_batch(text, intent)
        if recovered_batch is not None:
            intent = recovered_batch
            extracted = type(extracted)(
                intent, extracted.raw_output, extracted.latency_ms
            )
            trace.append({"stage": "BATCH_RECIPIENTS_RECOVERED_FROM_DIRECTORY"})

        if intent.get("kind") not in (
            "BENEFICIARY_TRANSFER",
            "PAYNOW_MOBILE_TRANSFER",
            "BENEFICIARY_TRANSFER_BATCH",
        ):
            return self._nonexecuting(
                request_id,
                text,
                "BLOCKED",
                "UNSUPPORTED_ACTION",
                trace,
                extracted=extracted,
            )

        source_ref = intent["source_account_ref"]
        source_evidence = intent["source_evidence"]
        if source_ref is not None and not _same_grounded_value(
            text, source_ref, source_evidence
        ):
            return self._nonexecuting(
                request_id,
                text,
                "BLOCKED",
                "UNGROUNDED_SOURCE",
                trace,
                extracted=extracted,
            )

        if intent.get("kind") == "BENEFICIARY_TRANSFER_BATCH":
            return self._process_beneficiary_batch(
                request_id, text, trace, extracted, source_ref
            )

        try:
            verified_amount, verified_amount_evidence = derive_amount_from_text(text)
        except GroundingError as exc:
            return self._nonexecuting(
                request_id,
                text,
                "BLOCKED",
                exc.reason_code,
                trace,
                extracted=extracted,
            )

        grounded_intent = {
            **intent,
            "amount_minor": verified_amount,
            "amount_evidence": verified_amount_evidence,
        }
        grounded_intent["payment_reference"] = verify_optional_reference(
            text,
            intent["payment_reference"],
            intent["reference_evidence"],
        )
        if grounded_intent["payment_reference"] is None:
            grounded_intent["reference_evidence"] = None
        intent = grounded_intent
        extracted = type(extracted)(intent, extracted.raw_output, extracted.latency_ms)

        try:
            derived_proxy, derived_proxy_evidence = derive_mobile_proxy_from_text(text)
        except PayNowLookupError as exc:
            return self._nonexecuting(
                request_id,
                text,
                "BLOCKED",
                exc.reason_code,
                trace,
                extracted=extracted,
            )
        if derived_proxy is not None:
            intent = {
                key: value
                for key, value in intent.items()
                if key not in ("recipient_label", "recipient_evidence")
            }
            intent.update(
                {
                    "kind": "PAYNOW_MOBILE_TRANSFER",
                    "mobile_proxy": derived_proxy,
                    "proxy_evidence": derived_proxy_evidence,
                }
            )
            trace.append(
                {
                    "stage": "PAYNOW_PROXY_DERIVED",
                    "canonical_proxy": derived_proxy,
                }
            )
            extracted = type(extracted)(
                intent,
                extracted.raw_output,
                extracted.latency_ms,
            )

        if NEGATED_ACTION_PATTERN.search(text):
            return self._nonexecuting(
                request_id,
                text,
                "BLOCKED",
                "NEGATED_ACTION",
                trace,
                extracted=extracted,
            )

        if source_ref is None:
            user_sources = [
                account
                for account in self.database.get_state()["accounts"]
                if account["is_user_source"]
            ]
            if len(user_sources) != 1:
                return self._nonexecuting(
                    request_id,
                    text,
                    "CLARIFICATION_REQUEST",
                    "MISSING_SOURCE_ACCOUNT",
                    trace,
                    extracted=extracted,
                    extra={"question": "Which account should fund this transfer?"},
                )
            source = user_sources[0]
            trace.append(
                {
                    "stage": "SOURCE_DEFAULTED",
                    "source_account_id": source["account_id"],
                }
            )
        else:
            source = self.database.resolve_source(source_ref)
            if source is None:
                return self._nonexecuting(
                    request_id,
                    text,
                    "BLOCKED",
                    "UNKNOWN_SOURCE_ACCOUNT",
                    trace,
                    extracted=extracted,
                )
            trace.append(
                {
                    "stage": "SOURCE_RESOLVED",
                    "source_account_id": source["account_id"],
                }
            )

        if intent.get("kind") == "PAYNOW_MOBILE_TRANSFER":
            proxy = intent["mobile_proxy"]
            evidence = intent["proxy_evidence"]
            if proxy is None:
                return self._nonexecuting(
                    request_id,
                    text,
                    "CLARIFICATION_REQUEST",
                    "MISSING_PAYNOW_PROXY",
                    trace,
                    extracted=extracted,
                )
            if not is_grounded(text, evidence):
                return self._nonexecuting(
                    request_id,
                    text,
                    "BLOCKED",
                    "UNGROUNDED_RECIPIENT",
                    trace,
                    extracted=extracted,
                )
            try:
                canonical_proxy = normalize_mobile_proxy(evidence)
                proposed_proxy = normalize_mobile_proxy(proxy)
                if proposed_proxy != canonical_proxy:
                    raise PayNowLookupError("PAYNOW_PROXY_EVIDENCE_MISMATCH")
                lookup = self.paynow_directory.lookup_mobile(
                    canonical_proxy,
                    request_id,
                )
            except PayNowLookupError as exc:
                return self._nonexecuting(
                    request_id,
                    text,
                    "BLOCKED",
                    exc.reason_code,
                    trace,
                    extracted=extracted,
                )
            trace.append(
                {
                    "stage": "PAYNOW_PROXY_RESOLVED",
                    "lookup_reference": lookup.lookup_reference,
                }
            )
            if verified_amount is None:
                try:
                    pending = self.database.create_amount_clarification(
                        request_id=request_id,
                        original_text=text,
                        user_id=DEMO_USER_ID,
                        partial_action={
                            "kind": "PAYNOW_MOBILE_TRANSFER",
                            "source_account_id": source["account_id"],
                            "beneficiary_ids": [lookup.beneficiary_id],
                            "recipient_display_names": [lookup.masked_name],
                            "currency": intent["currency"],
                            "payment_reference": intent["payment_reference"],
                            "destination_kind": "PAYNOW_MOBILE",
                            "destination_reference": lookup.canonical_proxy,
                            "destination_account_id": lookup.destination_account_id,
                            "lookup_reference": lookup.lookup_reference,
                            "reason_code": "MISSING_AMOUNT",
                        },
                        evidence_bundle=_evidence_bundle(text, intent),
                        raw_model_output=extracted.raw_output,
                        validated_intent=intent,
                    )
                except (IdempotencyConflict, sqlite3.Error) as exc:
                    code = (
                        "IDEMPOTENCY_CONFLICT"
                        if isinstance(exc, IdempotencyConflict)
                        else "DATABASE_ERROR"
                    )
                    return {
                        "outcome": "BLOCKED" if code == "IDEMPOTENCY_CONFLICT" else "FAILED",
                        "request_id": request_id,
                        "reason_code": code,
                        "trace": trace + [{"stage": code}],
                        "state": self.database.get_state(),
                    }
                return {
                    **pending,
                    "trace": trace + [{"stage": "MISSING_AMOUNT"}],
                    "state": self.database.get_state(),
                }
            if verified_amount > source["balance_minor"]:
                return self._nonexecuting(
                    request_id,
                    text,
                    "BLOCKED",
                    "INSUFFICIENT_FUNDS",
                    trace,
                    extracted=extracted,
                )
            return self._paynow_receipt_confirmation(
                request_id,
                text,
                trace,
                extracted,
                source,
                lookup,
            )

        recipient_label = intent["recipient_label"]
        recipient_evidence = intent["recipient_evidence"]
        if recipient_label is None:
            return self._nonexecuting(
                request_id,
                text,
                "CLARIFICATION_REQUEST",
                "MISSING_RECIPIENT",
                trace,
                extracted=extracted,
            )
        if not _same_grounded_value(text, recipient_label, recipient_evidence):
            return self._nonexecuting(
                request_id,
                text,
                "BLOCKED",
                "UNGROUNDED_RECIPIENT",
                trace,
                extracted=extracted,
            )

        resolution = self.database.resolve_beneficiary(intent["recipient_label"])
        exact_mentions = tuple(
            candidate
            for candidate in self.database.list_saved_beneficiaries()
            if is_grounded(text, candidate["display_name"])
            or is_grounded(text, candidate["normalized_alias"])
        )
        if resolution.kind == "exact" and (
            len(exact_mentions) != 1
            or exact_mentions[0]["beneficiary_id"]
            != resolution.matches[0]["beneficiary_id"]
        ):
            return self._nonexecuting(
                request_id,
                text,
                "BLOCKED",
                "AMBIGUOUS_RECIPIENT",
                trace,
                extracted=extracted,
            )
        if verified_amount is None:
            if resolution.kind == "exact":
                beneficiary = resolution.matches[0]
                try:
                    pending = self.database.create_amount_clarification(
                        request_id=request_id,
                        original_text=text,
                        user_id=DEMO_USER_ID,
                        partial_action={
                            "kind": "BENEFICIARY_TRANSFER",
                            "source_account_id": source["account_id"],
                            "beneficiary_ids": [beneficiary["beneficiary_id"]],
                            "recipient_display_names": [beneficiary["display_name"]],
                            "currency": intent["currency"],
                            "payment_reference": intent["payment_reference"],
                            "reason_code": "MISSING_AMOUNT",
                        },
                        evidence_bundle=_evidence_bundle(text, intent),
                        raw_model_output=extracted.raw_output,
                        validated_intent=intent,
                    )
                except IdempotencyConflict:
                    return {
                        "outcome": "BLOCKED",
                        "request_id": request_id,
                        "reason_code": "IDEMPOTENCY_CONFLICT",
                        "trace": [{"stage": "IDEMPOTENCY_CONFLICT"}],
                        "state": self.database.get_state(),
                    }
                except sqlite3.Error as exc:
                    return self._nonexecuting(
                        request_id,
                        text,
                        "FAILED",
                        "DATABASE_ERROR",
                        trace,
                        extracted=extracted,
                        detail=str(exc),
                    )
                return {
                    **pending,
                    "resolved_recipient_display_name": beneficiary["display_name"],
                    "resolved_source_label": source["label"],
                    "trace": trace + [{"stage": "MISSING_AMOUNT"}],
                    "state": self.database.get_state(),
                }
            return self._nonexecuting(
                request_id,
                text,
                "BLOCKED",
                "UNKNOWN_RECIPIENT" if resolution.kind == "missing" else "MISSING_AMOUNT",
                trace,
                extracted=extracted,
            )
        if resolution.kind != "missing" and verified_amount > source["balance_minor"]:
            return self._nonexecuting(
                request_id,
                text,
                "BLOCKED",
                "INSUFFICIENT_FUNDS",
                trace,
                extracted=extracted,
            )
        if resolution.kind == "close":
            display_names = [match["display_name"] for match in resolution.matches]
            question = (
                f"Did you mean {display_names[0]}?"
                if len(display_names) == 1
                else "Which saved recipient did you mean?"
            )
            return self._receipt_confirmation(
                request_id,
                text,
                "CLOSE_RECIPIENT_MATCH",
                trace,
                extracted,
                source,
                resolution.matches,
                question,
            )
        if resolution.kind == "candidates":
            return self._receipt_confirmation(
                request_id,
                text,
                "AMBIGUOUS_RECIPIENT",
                trace,
                extracted,
                source,
                resolution.matches,
                f"Which saved {intent['recipient_label'].strip()} do you mean?",
            )
        if resolution.kind == "phonetic":
            display_names = [match["display_name"] for match in resolution.matches]
            question = (
                f"Did you mean {display_names[0]}?"
                if len(display_names) == 1
                else "Which possible saved recipient did you mean?"
            )
            return self._receipt_confirmation(
                request_id,
                text,
                "PHONETIC_RECIPIENT_MATCH",
                trace,
                extracted,
                source,
                resolution.matches,
                question,
            )
        if resolution.kind == "missing":
            return self._nonexecuting(
                request_id,
                text,
                "BLOCKED",
                "UNKNOWN_RECIPIENT",
                trace,
                extracted=extracted,
            )

        beneficiary = resolution.matches[0]
        trace.append(
            {
                "stage": "RECIPIENT_RESOLVED",
                "beneficiary_id": beneficiary["beneficiary_id"],
            }
        )
        if not _has_affirmative_target_slot(
            text,
            beneficiary["display_name"],
            intent["amount_evidence"],
            source["label"],
        ):
            amount = _decimal_amount(intent["amount_minor"])
            return self._receipt_confirmation(
                request_id,
                text,
                "ACTION_CONFIRMATION_REQUIRED",
                trace,
                extracted,
                source,
                (beneficiary,),
                (
                    f"Send S${amount} to {beneficiary['display_name']} "
                    f"from {source['label']}?"
                ),
            )

        resolved_action = {
            "kind": "BENEFICIARY_TRANSFER",
            "source_account_id": source["account_id"],
            "beneficiary_id": beneficiary["beneficiary_id"],
            "destination_account_id": beneficiary["destination_account_id"],
            "recipient_display_name": beneficiary["display_name"],
            "masked_destination": beneficiary["masked_destination"],
            "amount_minor": intent["amount_minor"],
            "currency": intent["currency"],
        }
        trace.append({"stage": "ELIGIBILITY_PASSED"})
        try:
            execution = self.database.execute_policy_transfer(
                TransferCommand(
                    request_id=request_id,
                    original_text=text,
                    source_account_id=source["account_id"],
                    beneficiary_id=beneficiary["beneficiary_id"],
                    amount_minor=intent["amount_minor"],
                    currency=intent["currency"],
                    raw_model_output=extracted.raw_output,
                    validated_intent=intent,
                    evidence_bundle=_evidence_bundle(text, intent),
                )
            )
        except InsufficientFunds:
            return self._nonexecuting(
                request_id,
                text,
                "BLOCKED",
                "INSUFFICIENT_FUNDS",
                trace,
                extracted=extracted,
            )
        except IdempotencyConflict:
            return {
                "outcome": "BLOCKED",
                "request_id": request_id,
                "reason_code": "IDEMPOTENCY_CONFLICT",
                "trace": trace + [{"stage": "IDEMPOTENCY_CONFLICT"}],
                "state": self.database.get_state(),
            }
        except sqlite3.Error as exc:
            return self._nonexecuting(
                request_id,
                text,
                "FAILED",
                "DATABASE_ERROR",
                trace,
                extracted=extracted,
                detail=str(exc),
            )

        trace.append({"stage": "SQLITE_COMMITTED"})
        return {
            **execution,
            "validated_intent": intent,
            "resolved_action": resolved_action,
            "trace": trace,
            "state": self.database.get_state(),
        }

    def _recover_exact_saved_batch(
        self,
        text: str,
        intent: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Preserve explicit saved names when a model collapses a clear batch.

        This recovery is deliberately narrower than natural-language inference:
        the text must begin with a transfer command, contain two to four complete
        saved display names exactly once, and the model's proposed recipient must
        be one of those names. Amounts remain entirely server-grounded later.
        """

        if (
            intent.get("kind") != "BENEFICIARY_TRANSFER"
            or _BATCH_COMMAND_PATTERN.search(text) is None
            or intent.get("payment_reference") is not None
        ):
            return None

        mentions: list[tuple[int, str, str]] = []
        for beneficiary in self.database.list_saved_beneficiaries():
            display_name = beneficiary["display_name"]
            matches = list(
                re.finditer(
                    rf"(?<!\w){re.escape(display_name)}(?!\w)",
                    text,
                    re.IGNORECASE,
                )
            )
            if len(matches) > 1:
                return None
            if matches:
                match = matches[0]
                mentions.append((match.start(), display_name, match.group(0)))

        if not 2 <= len(mentions) <= 4:
            return None
        try:
            amount_mentions = derive_amount_mentions_from_text(text)
        except GroundingError:
            return None
        if len(amount_mentions) != len(mentions):
            return None
        mentions.sort(key=lambda item: item[0])
        proposed_recipient = normalize_evidence(intent.get("recipient_label") or "")
        if proposed_recipient not in {
            normalize_evidence(display_name) for _, display_name, _ in mentions
        }:
            return None

        return {
            "contract_version": 4,
            "kind": "BENEFICIARY_TRANSFER_BATCH",
            "source_account_ref": intent.get("source_account_ref"),
            "source_evidence": intent.get("source_evidence"),
            "items": [
                {
                    "recipient_label": display_name,
                    "recipient_evidence": evidence,
                }
                for _, display_name, evidence in mentions
            ],
            "currency": intent.get("currency"),
            "requested_execution_date": intent.get("requested_execution_date"),
        }

    def _process_beneficiary_batch(
        self,
        request_id: str,
        text: str,
        trace: list[dict[str, Any]],
        extracted: Any,
        source_ref: str | None,
    ) -> dict[str, Any]:
        if NEGATED_ACTION_PATTERN.search(text):
            return self._nonexecuting(
                request_id, text, "BLOCKED", "NEGATED_ACTION", trace,
                extracted=extracted,
            )
        if _BATCH_PARTIAL_NEGATION_PATTERN.search(text):
            return self._nonexecuting(
                request_id, text, "BLOCKED", "AMBIGUOUS_BATCH_BINDING", trace,
                extracted=extracted,
            )
        if re.search(r"\b(?:if|unless|when|before|after)\b", text, re.IGNORECASE):
            return self._nonexecuting(
                request_id, text, "BLOCKED", "AMBIGUOUS_BATCH_BINDING", trace,
                extracted=extracted,
            )
        if (
            _BATCH_PAYNOW_PATTERN.search(text)
            or _BATCH_UNSUPPORTED_LANGUAGE_PATTERN.search(text)
        ):
            return self._nonexecuting(
                request_id, text, "BLOCKED", "AMBIGUOUS_BATCH_BINDING", trace,
                extracted=extracted,
            )

        if source_ref is None:
            user_sources = [
                account
                for account in self.database.get_state()["accounts"]
                if account["is_user_source"]
            ]
            if len(user_sources) != 1:
                return self._nonexecuting(
                    request_id, text, "CLARIFICATION_REQUEST",
                    "MISSING_SOURCE_ACCOUNT", trace, extracted=extracted,
                    extra={"question": "Which account should fund this transfer?"},
                )
            source = user_sources[0]
            trace.append({
                "stage": "SOURCE_DEFAULTED",
                "source_account_id": source["account_id"],
            })
        else:
            source = self.database.resolve_source(source_ref)
            if source is None:
                return self._nonexecuting(
                    request_id, text, "BLOCKED", "UNKNOWN_SOURCE_ACCOUNT", trace,
                    extracted=extracted,
                )
            trace.append({
                "stage": "SOURCE_RESOLVED",
                "source_account_id": source["account_id"],
            })

        source_mentions = [
            normalize_evidence(match.group("account"))
            for match in _BATCH_SOURCE_CLAUSE_PATTERN.finditer(text)
        ]
        if any(
            mention != normalize_evidence(source["label"])
            for mention in source_mentions
        ):
            return self._nonexecuting(
                request_id, text, "BLOCKED", "AMBIGUOUS_BATCH_BINDING", trace,
                extracted=extracted,
            )

        items = extracted.intent.get("items")
        try:
            recipient_evidences = [item["recipient_evidence"] for item in items]
            bound = bind_batch_evidence(text, recipient_evidences)
        except (KeyError, TypeError, GroundingError) as exc:
            reason = (
                exc.reason_code
                if isinstance(exc, GroundingError)
                else "BATCH_RECIPIENT_COUNT_MISMATCH"
            )
            return self._nonexecuting(
                request_id, text, "BLOCKED", reason, trace, extracted=extracted,
            )
        items_by_evidence = {
            item["recipient_evidence"]: item
            for item in items
            if isinstance(item, dict) and isinstance(item.get("recipient_evidence"), str)
        }
        if len(items_by_evidence) != len(items):
            return self._nonexecuting(
                request_id, text, "BLOCKED", "AMBIGUOUS_BATCH_BINDING", trace,
                extracted=extracted,
            )
        ordered_items = [
            items_by_evidence.get(binding.recipient_evidence) for binding in bound
        ]
        if any(item is None for item in ordered_items):
            return self._nonexecuting(
                request_id, text, "BLOCKED", "AMBIGUOUS_BATCH_BINDING", trace,
                extracted=extracted,
            )

        grounded_items: list[dict[str, Any]] = []
        resolved: list[dict[str, Any]] = []
        seen_beneficiary_ids: set[str] = set()
        for item, binding in zip(ordered_items, bound):
            if normalize_evidence(item.get("recipient_label", "")) != normalize_evidence(
                binding.recipient_evidence
            ):
                return self._nonexecuting(
                    request_id, text, "BLOCKED", "AMBIGUOUS_BATCH_BINDING", trace,
                    extracted=extracted,
                )
            resolution = self.database.resolve_beneficiary(item["recipient_label"])
            if resolution.kind == "missing":
                return self._nonexecuting(
                    request_id, text, "BLOCKED", "UNKNOWN_RECIPIENT", trace,
                    extracted=extracted,
                )
            if resolution.kind != "exact" or len(resolution.matches) != 1:
                return self._nonexecuting(
                    request_id, text, "BLOCKED", "AMBIGUOUS_BATCH_BINDING", trace,
                    extracted=extracted,
                )
            beneficiary = resolution.matches[0]
            beneficiary_id = beneficiary["beneficiary_id"]
            if beneficiary_id in seen_beneficiary_ids:
                return self._nonexecuting(
                    request_id, text, "BLOCKED", "DUPLICATE_BATCH_RECIPIENT", trace,
                    extracted=extracted,
                )
            seen_beneficiary_ids.add(beneficiary_id)
            grounded_items.append({
                **item,
                "amount_minor": binding.amount_minor,
                "amount_evidence": binding.amount_evidence,
            })
            resolved.append(beneficiary)
        trace.append({"stage": "BATCH_EVIDENCE_BOUND"})
        trace.append({
            "stage": "BATCH_RECIPIENTS_RESOLVED",
            "beneficiary_ids": [item["beneficiary_id"] for item in resolved],
        })
        total_minor = sum(item["amount_minor"] for item in grounded_items)
        if total_minor > source["balance_minor"]:
            return self._nonexecuting(
                request_id, text, "BLOCKED", "INSUFFICIENT_FUNDS", trace,
                extracted=extracted,
            )
        grounded_intent = {**extracted.intent, "items": grounded_items}
        grounded_extracted = type(extracted)(
            grounded_intent, extracted.raw_output, extracted.latency_ms
        )
        draft = BatchReceiptDraft(
            request_id=request_id,
            original_text=text,
            user_id=DEMO_USER_ID,
            source_account_id=source["account_id"],
            currency=grounded_intent["currency"],
            items=tuple(
                BatchReceiptItemDraft(
                    position,
                    beneficiary["beneficiary_id"],
                    beneficiary["destination_account_id"],
                    beneficiary["display_name"],
                    item["amount_minor"],
                    item["recipient_evidence"],
                    item["amount_evidence"],
                )
                for position, (item, beneficiary) in enumerate(zip(grounded_items, resolved))
            ),
        )
        try:
            pending = self.database.create_pending_batch_receipt(
                draft,
                raw_model_output=extracted.raw_output,
                validated_intent=grounded_intent,
            )
        except InsufficientFunds:
            return self._nonexecuting(
                request_id, text, "BLOCKED", "INSUFFICIENT_FUNDS", trace,
                extracted=grounded_extracted,
            )
        except IdempotencyConflict:
            return {
                "outcome": "BLOCKED",
                "request_id": request_id,
                "reason_code": "IDEMPOTENCY_CONFLICT",
                "trace": [{"stage": "IDEMPOTENCY_CONFLICT"}],
                "state": self.database.get_state(),
            }
        except sqlite3.Error as exc:
            return self._nonexecuting(
                request_id, text, "FAILED", "DATABASE_ERROR", trace,
                extracted=grounded_extracted, detail=str(exc),
            )
        trace.append({"stage": "BATCH_CONFIRMATION_REQUIRED"})
        return {
            **pending,
            "validated_intent": grounded_intent,
            "trace": trace,
            "state": self.database.get_state(),
        }

    def respond_to_clarification(
        self,
        clarification_id: str,
        request_id: str,
        text: str,
        *,
        user_id: str = DEMO_USER_ID,
    ) -> dict[str, Any]:
        trace = [{"stage": "CLARIFICATION_RESPONSE_RECEIVED"}]
        try:
            response = self.database.complete_amount_clarification(
                clarification_id,
                request_id,
                text,
                user_id,
            )
        except ClarificationRejected as exc:
            return {
                "outcome": "BLOCKED",
                "request_id": request_id,
                "reason_code": exc.reason_code,
                "trace": trace + [{"stage": exc.reason_code}],
                "state": self.database.get_state(),
            }
        except IdempotencyConflict:
            return {
                "outcome": "BLOCKED",
                "request_id": request_id,
                "reason_code": "IDEMPOTENCY_CONFLICT",
                "trace": trace + [{"stage": "IDEMPOTENCY_CONFLICT"}],
                "state": self.database.get_state(),
            }
        except sqlite3.Error as exc:
            return {
                "outcome": "FAILED",
                "request_id": request_id,
                "reason_code": "DATABASE_ERROR",
                "detail": str(exc),
                "trace": trace + [{"stage": "DATABASE_ERROR"}],
                "state": self.database.get_state(),
            }
        return {
            **response,
            "trace": trace + [{"stage": "AMOUNT_CLARIFICATION_COMPLETED"}],
            "state": self.database.get_state(),
        }

    def approve(
        self,
        receipt_id: str,
        receipt_digest: str,
        *,
        user_id: str = DEMO_USER_ID,
    ) -> dict[str, Any]:
        trace = [{"stage": "RECEIPT_APPROVAL_RECEIVED"}]
        try:
            execution = self.database.approve_receipt(
                receipt_id,
                receipt_digest,
                user_id,
            )
        except ReceiptRejected as exc:
            return {
                "outcome": "BLOCKED",
                "reason_code": exc.reason_code,
                "trace": trace + [{"stage": exc.reason_code}],
                "state": self.database.get_state(),
            }
        except InsufficientFunds:
            return {
                "outcome": "BLOCKED",
                "reason_code": "INSUFFICIENT_FUNDS",
                "trace": trace + [{"stage": "INSUFFICIENT_FUNDS"}],
                "state": self.database.get_state(),
            }
        except sqlite3.Error as exc:
            return {
                "outcome": "FAILED",
                "reason_code": "DATABASE_ERROR",
                "detail": str(exc),
                "trace": trace + [{"stage": "DATABASE_ERROR"}],
                "state": self.database.get_state(),
            }

        return {
            **execution,
            "trace": trace
            + [
                {"stage": "RECEIPT_REVALIDATED"},
                {"stage": "SQLITE_COMMITTED"},
            ],
            "state": self.database.get_state(),
        }

    def approve_batch(
        self,
        receipt_id: str,
        receipt_digest: str,
        *,
        user_id: str = DEMO_USER_ID,
    ) -> dict[str, Any]:
        trace = [{"stage": "BATCH_RECEIPT_APPROVAL_RECEIVED"}]
        try:
            execution = self.database.approve_batch_receipt(
                receipt_id, receipt_digest, user_id
            )
        except ReceiptRejected as exc:
            return {
                "outcome": "BLOCKED",
                "reason_code": exc.reason_code,
                "trace": trace + [{"stage": exc.reason_code}],
                "state": self.database.get_state(),
            }
        except InsufficientFunds:
            return {
                "outcome": "BLOCKED",
                "reason_code": "INSUFFICIENT_FUNDS",
                "trace": trace + [{"stage": "INSUFFICIENT_FUNDS"}],
                "state": self.database.get_state(),
            }
        except sqlite3.Error as exc:
            return {
                "outcome": "FAILED",
                "reason_code": "DATABASE_ERROR",
                "detail": str(exc),
                "trace": trace + [{"stage": "DATABASE_ERROR"}],
                "state": self.database.get_state(),
            }
        return {
            **execution,
            "trace": trace + [
                {"stage": "BATCH_RECEIPT_REVALIDATED"},
                {"stage": "SQLITE_BATCH_COMMITTED"},
            ],
            "state": self.database.get_state(),
        }

    def _receipt_confirmation(
        self,
        request_id: str,
        text: str,
        reason_code: str,
        trace: list[dict[str, Any]],
        extracted: Any,
        source: dict[str, Any],
        beneficiaries: tuple[dict[str, Any], ...],
        question: str,
    ) -> dict[str, Any]:
        drafts = tuple(
            ReceiptDraft(
                request_id=request_id,
                original_text=text,
                user_id=DEMO_USER_ID,
                source_account_id=source["account_id"],
                beneficiary_id=beneficiary["beneficiary_id"],
                recipient_display_name=beneficiary["display_name"],
                amount_minor=extracted.intent["amount_minor"],
                currency=extracted.intent["currency"],
                evidence_bundle=_evidence_bundle(text, extracted.intent),
            )
            for beneficiary in beneficiaries
        )
        response_base = {
            "outcome": "CLARIFICATION_REQUEST",
            "question": question,
            "trace": trace + [{"stage": reason_code}],
            "state": self.database.get_state(),
        }
        try:
            return self.database.create_pending_receipts(
                drafts,
                raw_model_output=extracted.raw_output,
                validated_intent=extracted.intent,
                reason_code=reason_code,
                response_base=response_base,
            )
        except IdempotencyConflict:
            return {
                "outcome": "BLOCKED",
                "request_id": request_id,
                "reason_code": "IDEMPOTENCY_CONFLICT",
                "trace": [{"stage": "IDEMPOTENCY_CONFLICT"}],
                "state": self.database.get_state(),
            }
        except sqlite3.Error as exc:
            return self._nonexecuting(
                request_id,
                text,
                "FAILED",
                "DATABASE_ERROR",
                trace,
                extracted=extracted,
                detail=str(exc),
            )

    def _paynow_receipt_confirmation(
        self,
        request_id: str,
        text: str,
        trace: list[dict[str, Any]],
        extracted: Any,
        source: dict[str, Any],
        lookup: Any,
    ) -> dict[str, Any]:
        draft = ReceiptDraft(
            request_id=request_id,
            original_text=text,
            user_id=DEMO_USER_ID,
            source_account_id=source["account_id"],
            beneficiary_id=lookup.beneficiary_id,
            recipient_display_name=lookup.masked_name,
            amount_minor=extracted.intent["amount_minor"],
            currency=extracted.intent["currency"],
            destination_kind="PAYNOW_MOBILE",
            destination_reference=lookup.canonical_proxy,
            destination_account_id=lookup.destination_account_id,
            lookup_reference=lookup.lookup_reference,
            evidence_bundle=_evidence_bundle(text, extracted.intent),
        )
        amount = _decimal_amount(extracted.intent["amount_minor"])
        response_base = {
            "outcome": "CLARIFICATION_REQUEST",
            "question": (
                f"Send S${amount} by PayNow to {lookup.masked_name} "
                f"({lookup.canonical_proxy}) from {source['label']}?"
            ),
            "trace": trace + [{"stage": "PAYNOW_CONFIRMATION_REQUIRED"}],
            "state": self.database.get_state(),
        }
        try:
            return self.database.create_pending_receipts(
                (draft,),
                raw_model_output=extracted.raw_output,
                validated_intent=extracted.intent,
                reason_code="PAYNOW_CONFIRMATION_REQUIRED",
                response_base=response_base,
            )
        except IdempotencyConflict:
            return {
                "outcome": "BLOCKED",
                "request_id": request_id,
                "reason_code": "IDEMPOTENCY_CONFLICT",
                "trace": [{"stage": "IDEMPOTENCY_CONFLICT"}],
                "state": self.database.get_state(),
            }
        except sqlite3.Error as exc:
            return self._nonexecuting(
                request_id,
                text,
                "FAILED",
                "DATABASE_ERROR",
                trace,
                extracted=extracted,
                detail=str(exc),
            )

    def _nonexecuting(
        self,
        request_id: str,
        text: str,
        outcome: str,
        reason_code: str,
        trace: list[dict[str, Any]],
        *,
        extracted: Any = None,
        detail: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "outcome": outcome,
            "request_id": request_id,
            "reason_code": reason_code,
            "trace": trace + [{"stage": reason_code}],
            "state": self.database.get_state(),
        }
        if detail:
            response["detail"] = detail
        if extracted is not None:
            response["validated_intent"] = extracted.intent
        if extra:
            response.update(extra)
        attempt = ActionAttempt(
            request_id=request_id,
            original_text=text,
            raw_model_output=extracted.raw_output if extracted is not None else None,
            validated_intent=extracted.intent if extracted is not None else None,
            outcome=outcome,
            reason_code=reason_code,
            response=response,
        )
        try:
            return self.database.record_attempt(attempt)
        except IdempotencyConflict:
            return {
                "outcome": "BLOCKED",
                "request_id": request_id,
                "reason_code": "IDEMPOTENCY_CONFLICT",
                "trace": [{"stage": "IDEMPOTENCY_CONFLICT"}],
                "state": self.database.get_state(),
            }

"""Deterministic guarded-autopilot orchestration for Dimon Live."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from decimal import Decimal
from typing import Any

from . import (
    bill_payment_grounding,
    input_firewall,
    investing,
    market,
    multi_intent,
    no_approval,
    payee_additions,
    rule_parser,
    scam_guard,
    stock_market,
    stock_request,
    voice_dialog,
)
from .approval_gateway import ApprovalContext, DeviceAssertion
from .batch_grounding import bind_batch_evidence
from .batch_receipts import BatchReceiptDraft, BatchReceiptItemDraft
from .database import (
    ActionAttempt,
    ClarificationRejected,
    Database,
    IdempotencyConflict,
    InsufficientFunds,
    ReceiptRejected,
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
from .multi_action import UnresolvedAction, prepare_multi_action
from .paynow import (
    PayNowLookupError,
    SyntheticPayNowDirectory,
    derive_mobile_proxy_from_text,
    normalize_mobile_proxy,
)
from .receipts import DEMO_USER_ID, ReceiptDraft
from .risk_policy import authentication_for_amount_minor


logger = logging.getLogger(__name__)

_SOCIAL_UTTERANCES = {
    "thank you": "You're welcome. What else can I help with?",
    "thanks": "You're welcome. What else can I help with?",
    "thanks dimon": "You're welcome. What else can I help with?",
    "youre welcome": "Thank you. What would you like help with?",
    "welcome": "Thank you. What would you like help with?",
    "how are you": "I'm ready to help. What would you like to do?",
    "how was your day": "I'm ready to help. What would you like to do?",
    "hello": "Hi. What would you like help with?",
    "hi": "Hi. What would you like help with?",
}
_CANCEL_WITHOUT_PENDING = {"cancel", "never mind", "nevermind", "stop", "forget it"}


def conversational_answer(text: str) -> str | None:
    """Handle bounded assistant chatter before an intent model can invent work."""

    normalized = " ".join(re.findall(
        r"[a-z0-9]+", text.casefold().replace("’", "").replace("'", "")
    ))
    if normalized in _CANCEL_WITHOUT_PENDING:
        return "There isn't a pending request to cancel. Nothing was sent."
    return _SOCIAL_UTTERANCES.get(normalized)


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
_MIXED_BILL_TRANSFER_PATTERN = re.compile(
    r"\b(?:and|also|then|plus)\s+(?:also\s+)?(?:send|transfer|pay)\b|"
    r"\b(?:send|transfer)\s+(?:[a-z][\w'-]*|s?\$|\d)",
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


def _money_label(minor: int) -> str:
    return f"S${minor / 100:,.2f}"


def _payee_options(matches: Any, last_payment: Any = None) -> str:
    """'John Smith ending in 4321 (paid S$20 6 days ago) or John Tan ending in
    2468 (last paid 6 months ago)' — the account ending and payment history
    let the user tell same-named payees apart without guessing."""

    phrases = []
    for match in matches:
        digits = re.sub(r"\D", "", match.get("masked_destination") or "")
        phrase = f"{match['display_name']} ending in {digits[-4:]}" if digits else match["display_name"]
        history = last_payment(match) if last_payment and match.get("beneficiary_id") else None
        phrases.append(f"{phrase} ({history})" if history else phrase)
    if len(phrases) <= 2:
        return " or ".join(phrases)
    return f"{', '.join(phrases[:-1])}, or {phrases[-1]}"


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


def _receipt_evidence_bundle(text: str, intent: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    evidence = list(_evidence_bundle(text, intent))
    utility_amount = intent.get("read_only_utility_amount_minor")
    if utility_amount is not None:
        evidence.append(
            {
                "kind": "READ_ONLY_UTILITY_AMOUNT",
                "amount_minor": utility_amount,
                "amount_evidence": intent.get("read_only_utility_amount_evidence"),
            }
        )
    return tuple(evidence)


def _mentions_bill(text: str) -> bool:
    """A bill in the sentence, by the word "bill" or by a biller's name
    ("pay my Singtel", "settle PUB")."""

    from .bill_payment_directory import SYNTHETIC_BILLERS

    if bill_payment_grounding.looks_like_bill_payment_request(text):
        return True
    if not re.search(r"\b(?:pay|settle)\b", text, re.IGNORECASE):
        return False
    aliases = {alias for biller in SYNTHETIC_BILLERS for alias in biller.aliases if alias.casefold() != "water"}
    return any(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text, re.IGNORECASE) for alias in aliases)


class ActionService:
    def __init__(
        self,
        database: Database,
        extractor: Extractor,
        *,
        stock_extractor: Extractor | None = None,
        bill_payment_extractor: Extractor | None = None,
        multi_action_extractor: Extractor | None = None,
        stock_provider: stock_market.MarketProvider | None = None,
        conversation_service: Any | None = None,
        session_scope: str = "legacy",
        user_id: str = DEMO_USER_ID,
    ):
        self.database = database
        self.extractor = extractor
        self.stock_extractor = stock_extractor
        self.bill_payment_extractor = bill_payment_extractor
        self.multi_action_extractor = multi_action_extractor
        self.stock_provider = stock_provider or stock_market.YFinanceMarketProvider()
        self.conversation_service = conversation_service
        self.session_scope = session_scope
        self.user_id = user_id
        self.paynow_directory = SyntheticPayNowDirectory(database)

    def process(
        self,
        request_id: str,
        text: str,
        *,
        source: str = "typed",
        voice: dict[str, Any] | None = None,
        nested: bool = False,
    ) -> dict[str, Any]:
        response = self._with_validation(
            self._process(request_id, text, source=source, voice=voice, allow_split=not nested)
        )
        if not response.get("idempotent_replay"):
            self.database.audit_request(
                "REQUEST_DRAFTED", request_id, text, response, source=source, voice=voice
            )
        return response

    def _with_validation(self, response: dict[str, Any]) -> dict[str, Any]:
        """Attach the independent validation agent's verdict to every draft.

        A FROZEN verdict means the compiled draft contradicts the user's own
        words; the gateway refuses to challenge or execute it, and the user is
        alerted here instead of being shown a signable receipt.
        """

        audits: list[dict[str, Any]] = []
        for choice in response.get("receipt_choices") or ():
            if choice.get("receipt_id"):
                choice["validation"] = self.database.audit_receipt("single", choice["receipt_id"])
                audits.append(choice["validation"])
        batch = response.get("batch_receipt")
        if batch and batch.get("receipt_id"):
            batch["validation"] = self.database.audit_receipt("batch", batch["receipt_id"])
            audits.append(batch["validation"])
        if not audits:
            return response
        verdicts = {audit["verdict"] for audit in audits}
        stage = (
            "VALIDATION_PASSED" if verdicts == {"PASS"}
            else "VALIDATION_FROZEN" if verdicts == {"FROZEN"}
            else "VALIDATION_ATTENTION"
        )
        response = {
            **response,
            "trace": [
                *(response.get("trace") or ()),
                {"stage": stage, "validator": audits[0]["validator_version"]},
            ],
        }
        if verdicts != {"FROZEN"}:
            return self._with_policy_preview(response)
        return {
            **response,
            "outcome": "BLOCKED",
            "reason_code": "VALIDATION_FROZEN",
            "discrepancies": [
                field
                for audit in audits
                for field in audit["fields"]
                if field["verdict"] == "MISMATCH"
            ],
            "receipt_choices": [],
            "batch_receipt": None,
        }

    def _payee_options(self, matches: Any) -> str:
        def last_payment(match: Any) -> str | None:
            insight = getattr(self.database, "payee_insight", None)
            return insight(match["beneficiary_id"])["last_payment"] if insight else None

        return _payee_options(matches, last_payment)

    def _with_policy_preview(self, response: dict[str, Any]) -> dict[str, Any]:
        """Show KYC, limit, and velocity outcomes before the user signs.

        This is advisory; the gateway re-evaluates the same policy inside the
        executing transaction.
        """

        choices = response.get("receipt_choices") or []
        batch = response.get("batch_receipt")
        if batch and batch.get("receipt_id"):
            previews = [self.database.policy_for_receipt("batch", batch["receipt_id"])]
            batch["policy"] = previews[0]
        elif choices:
            previews = []
            for choice in choices:
                choice["policy"] = self.database.policy_for_receipt("single", choice["receipt_id"])
                if choice.get("beneficiary_id") and choice.get("destination_kind") != "UTILITY_BILL":
                    choice["payee"] = self.database.payee_insight(
                        choice["beneficiary_id"], choice["amount_minor"]
                    )
                previews.append(choice["policy"])
        else:
            return response
        trace = [*(response.get("trace") or ())]
        allowed = [preview for preview in previews if preview["allowed"]]
        if not allowed:
            reason = previews[0]["reason_code"]
            return {
                **response,
                "outcome": "BLOCKED",
                "reason_code": reason,
                "trace": trace + [{"stage": reason}],
                "receipt_choices": [],
                "batch_receipt": None,
            }
        if batch:
            response = {**response, "policy": previews[0]}
        elif len(allowed) != len(previews):
            response = {**response, "receipt_choices": [c for c in choices if c["policy"]["allowed"]]}
        strongest = max(
            allowed,
            key=lambda preview: (preview["requires_out_of_band"], preview["requires_read_back"]),
        )
        if strongest["requires_out_of_band"]:
            stage = "OUT_OF_BAND_REQUIRED"
        elif strongest["requires_read_back"]:
            stage = "READ_BACK_REQUIRED"
        else:
            stage = "RISK_POLICY_PASSED"
        trace = trace + [{"stage": stage, "tier": strongest["tier"]}]
        unusual = [
            choice for choice in response.get("receipt_choices") or ()
            if (choice.get("payee") or {}).get("unusual_amount")
        ]
        if len(unusual) == 1 and len(response.get("receipt_choices") or ()) == 1:
            payee = unusual[0]["payee"]
            trace.append({"stage": "UNUSUAL_AMOUNT_FLAGGED"})
            response = {
                **response,
                "question": (
                    f"You usually send {unusual[0]['recipient_display_name']} "
                    f"{_money_label(payee['typical_min_minor'])}–{_money_label(payee['typical_max_minor'])}. "
                    f"Is {_money_label(unusual[0]['amount_minor'])} right?"
                ),
                "unusual_amount": payee["unusual_amount"],
            }
        return {**response, "trace": trace}

    def _process_multi_action(
        self, request_id: str, text: str, trace: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Preflight every grounded domain span, then create review-only children."""
        try:
            extracted = self.multi_action_extractor.extract(text)
        except InferenceFailure as exc:
            return self._nonexecuting(request_id, text, "FAILED", exc.reason_code, trace)
        trace.append({"stage": "MULTI_ACTION_INTENT_EXTRACTED", "latency_ms": extracted.latency_ms})
        candidates = extracted.intent.get("items", [])
        domain_results: dict[tuple[str, str], Any] = {}

        def prepare_bill(evidence: str) -> dict[str, Any] | None:
            if self.bill_payment_extractor is None:
                return None
            parsed = self.bill_payment_extractor.extract(evidence)
            prepared = bill_payment_grounding.prepare_bill_payment(evidence, parsed.intent)
            if prepared.status != "READY" or prepared.amount_minor is None:
                return None
            domain_results[("BILL_PAYMENT", evidence)] = parsed
            return {"amount_minor": prepared.amount_minor, "intent": parsed.intent}

        def prepare_transfer(evidence: str) -> dict[str, Any] | None:
            parsed = self.extractor.extract(evidence)
            intent = parsed.intent
            if intent.get("kind") not in {"BENEFICIARY_TRANSFER", "PAYNOW_MOBILE_TRANSFER"}:
                return None
            amount, amount_evidence = derive_amount_from_text(evidence)
            source = self.database.resolve_source(intent.get("source_account_ref") or "savings")
            if source is None or source["balance_minor"] < amount:
                return None
            recipient = intent.get("recipient_label") or intent.get("mobile_proxy")
            if not isinstance(recipient, str) or not recipient:
                return UnresolvedAction(
                    "recipient", f"Who should receive {_money_label(amount)}?", evidence
                )
            if intent["kind"] == "BENEFICIARY_TRANSFER":
                if self.database.resolve_beneficiary(recipient).kind != "exact":
                    return UnresolvedAction(
                        "recipient", f"Who should receive {_money_label(amount)}?",
                        recipient,
                    )
            domain_results[("TRANSFER", evidence)] = parsed
            return {"amount_minor": amount, "amount_evidence": amount_evidence, "intent": intent}

        def prepare_stock(evidence: str) -> dict[str, Any] | None:
            if self.stock_extractor is None:
                return None
            parsed = self.stock_extractor.extract(evidence)
            if parsed.intent.get("kind") != "STOCK_INVESTMENT":
                return None
            label = stock_market.extract_model_stock_label(evidence, parsed.intent)
            raw_label = parsed.intent.get("instrument_label")
            def unresolved_instrument() -> UnresolvedAction:
                candidate = label[0] if label else raw_label
                normalized = (
                    re.sub(r"[^a-z0-9]", "", candidate.casefold())
                    if isinstance(candidate, str) else ""
                )
                suggestion = "NVIDIA" if normalized in {
                    "vds", "vda", "nvida", "nvidea",
                } else None
                question = f"Did you mean {suggestion}?" if suggestion else "Which stock did you mean?"
                evidence_token = (
                    raw_label if isinstance(raw_label, str) and raw_label in evidence
                    else label[0] if label and label[0] in evidence
                    else evidence
                )
                return UnresolvedAction(
                    "instrument", question, evidence_token, suggestion,
                )
            if label is None:
                return unresolved_instrument()
            sized = stock_market.raw_intent_from_model_size(label[0], label[1], parsed.intent)
            if sized is None:
                if parsed.intent.get("size_kind") == "UNKNOWN":
                    instrument_evidence = parsed.intent.get("instrument_evidence")
                    if isinstance(instrument_evidence, str) and instrument_evidence in evidence:
                        return UnresolvedAction(
                            "stock_size",
                            (
                                f"How many shares of {label[0]} would you like, "
                                "or what amount would you like to invest?"
                            ),
                            instrument_evidence,
                            label[0],
                        )
                return None
            try:
                stock_market.build_investment_brief(
                    sized, self.stock_provider, captured_at=self.database._clock(),
                    funding_currency="SGD", request_id=request_id,
                )
            except Exception:
                return unresolved_instrument()
            domain_results[("STOCK_INVESTMENT", evidence)] = parsed
            return {"intent": parsed.intent, "instrument_label": label[0]}

        try:
            preparation = prepare_multi_action(
                text, extracted.intent,
                bill_preparer=prepare_bill,
                transfer_preparer=prepare_transfer,
                stock_preparer=prepare_stock,
            )
        except Exception:
            preparation = None
        if preparation is None or preparation.reason_code:
            response = self._nonexecuting(
                request_id, text, "CLARIFICATION_REQUEST",
                preparation.reason_code if preparation else "MULTI_ACTION_ITEM_UNRESOLVED",
                trace + [{"stage": "MULTI_ACTION_PREFLIGHT_FAILED"}],
                extra={
                    "answer": preparation.clarification if preparation else
                              "I couldn’t verify every part of that request. Please clarify it.",
                    "multi_action_clarification": {
                        "resolved_items": [
                            {"kind": item.kind, "evidence": item.evidence, "status": "UNDERSTOOD"}
                            for item in (preparation.resolved_items if preparation else ())
                        ],
                        "issues": [
                            {
                                "kind": next((candidate["kind"] for candidate in candidates
                                              if issue.evidence in candidate["evidence"]
                                              or candidate["evidence"] in issue.evidence), None),
                                "field": issue.field,
                                "question": issue.question,
                                "evidence": issue.evidence,
                                "suggested_value": issue.suggested_value,
                                "status": "NEEDS_CLARIFICATION",
                            }
                            for issue in (preparation.issues if preparation else ())
                        ],
                    },
                },
            )
            if preparation and preparation.issues:
                public = response["multi_action_clarification"]
                stored = self.database.create_multi_action_clarification(
                    request_id=request_id,
                    original_text=text,
                    intent=extracted.intent,
                    resolved_items=public["resolved_items"],
                    issues=public["issues"],
                    user_id=self.user_id,
                )
                response["clarification"] = stored
                response["multi_action_clarification"] = {
                    **public, "expires_at": stored["expires_at"],
                }
                self.database.replace_unlinked_multi_action_attempt_response(
                    request_id, text, response
                )
            return response

        # All candidates have passed deterministic grounding and resolution.
        # Existing handlers still create only pending reviews or read-only quotes.
        children: list[dict[str, Any] | None] = [None] * len(candidates)
        child_refs: list[dict[str, str] | None] = [None] * len(candidates)
        payment_total = sum(
            int(item.intent["amount_minor"])
            for item in preparation.items
            if item.kind in {"BILL_PAYMENT", "BENEFICIARY_TRANSFER", "PAYNOW_MOBILE_TRANSFER"}
        )
        processing_order = sorted(
            range(len(candidates)),
            key=lambda index: 0 if candidates[index]["kind"] == "STOCK_INVESTMENT" else 1,
        )
        child_request_ids: list[str] = []
        for ordinal in processing_order:
            candidate = candidates[ordinal]
            kind, evidence = candidate["kind"], candidate["evidence"]
            prepared = next(
                item for item in preparation.items
                if item.kind == kind and item.evidence == evidence
            )
            child_request_id = f"{request_id}-multi-{ordinal + 1}"
            child_request_ids.append(child_request_id)
            override_kind = "TRANSFER" if kind in {
                "BENEFICIARY_TRANSFER", "PAYNOW_MOBILE_TRANSFER"
            } else kind
            try:
                child_result = self._with_validation(self._process(
                    child_request_id, evidence,
                    extracted_overrides={
                        override_kind: domain_results[(override_kind, evidence)]
                    },
                    allow_split=False,
                ))
            except Exception as exc:
                logger.warning(
                    "multi_action_child_preparation_failed request_id=%s child=%s error=%s",
                    request_id, ordinal + 1, type(exc).__name__,
                )
                self.database.supersede_multi_action_drafts(child_request_ids)
                return self._nonexecuting(
                    request_id, text, "BLOCKED", "MULTI_ACTION_CHILD_PREPARATION_FAILED",
                    trace + [{"stage": "MULTI_ACTION_CHILD_PREPARATION_FAILED"}],
                    extra={"answer": "I couldn’t prepare every requested action. Nothing was approved or executed; please try again."},
                )
            if child_result.get("outcome") in {"BLOCKED", "FAILED"}:
                self.database.supersede_multi_action_drafts(child_request_ids)
                return self._nonexecuting(
                    request_id, text, "BLOCKED", "MULTI_ACTION_CHILD_PREPARATION_FAILED",
                    trace + [{"stage": "MULTI_ACTION_CHILD_PREPARATION_FAILED"}],
                    extra={"answer": "I couldn’t prepare every requested action. Nothing was approved or executed; please clarify and try again."},
                )
            receipts = child_result.get("receipt_choices") or []
            batch = child_result.get("batch_receipt")
            paper_order = child_result.get("paper_order")
            conversation = child_result.get("conversation") or {}
            child_type = (
                "receipt" if receipts else "batch_receipt" if batch
                else "paper_order" if paper_order
                else "conversation" if conversation.get("conversation_id")
                else "action"
            )
            child_id = (
                receipts[0]["receipt_id"] if receipts
                else batch["receipt_id"] if batch
                else paper_order["order_id"] if paper_order
                else conversation.get("conversation_id", child_request_id)
            )
            child_status = (
                "AWAITING_APPROVAL" if receipts or batch or paper_order
                else "AWAITING_USER" if conversation.get("conversation_id")
                else child_result.get("outcome", "PREPARED")
            )
            children[ordinal] = {
                "kind": kind,
                "evidence": evidence,
                "status": child_status,
                "result": child_result,
            }
            child_refs[ordinal] = {
                "kind": kind,
                "child_type": child_type,
                "child_id": child_id,
                "status": child_status if child_status in {"AWAITING_APPROVAL", "AWAITING_USER"} else "COMPLETED",
            }

        ordered_children = [child for child in children if child is not None]
        ordered_refs = [child for child in child_refs if child is not None]

        stock_child = next(
            (child["result"] for child in ordered_children if child["kind"] == "STOCK_INVESTMENT"),
            None,
        )
        response = {
            "outcome": "MULTI_ACTION_CONFIRMATION_REQUEST",
            "request_id": request_id,
            "answer": "I’ve prepared each requested action. Review and approve them individually.",
            "trace": trace + [{"stage": "MULTI_ACTION_GROUP_PREPARED"}],
            "items": ordered_children,
            "payment_authentication": authentication_for_amount_minor(payment_total),
            "state": self.database.get_state(),
        }
        if stock_child and stock_child.get("conversation"):
            response.update({
                "conversation": stock_child["conversation"],
                "question": stock_child.get("question"),
                "allowed_choices": stock_child.get("allowed_choices", []),
                "conversation_trace": stock_child.get("conversation_trace", []),
            })
        self.database.record_attempt(ActionAttempt(
            request_id=request_id, original_text=text,
            raw_model_output=extracted.raw_output,
            validated_intent=extracted.intent,
            outcome="MULTI_ACTION_CONFIRMATION_REQUEST", reason_code=None,
            response=response,
        ))
        try:
            group = self.database.create_pending_multi_action_group(
                request_id, text, ordered_refs,
                payment_auth_amount_minor=payment_total, user_id=self.user_id,
            )
        except Exception as exc:
            logger.warning(
                "multi_action_group_persistence_failed request_id=%s error=%s",
                request_id, type(exc).__name__,
            )
            self.database.supersede_multi_action_drafts(child_request_ids)
            failure = {
                "outcome": "FAILED", "request_id": request_id,
                "reason_code": "DATABASE_ERROR",
                "answer": "I couldn’t save the grouped review. No requested action was approved or executed.",
                "trace": trace + [{"stage": "MULTI_ACTION_GROUP_PERSISTENCE_FAILED"}],
                "state": self.database.get_state(),
            }
            self.database.replace_unlinked_multi_action_attempt_response(
                request_id, text, failure
            )
            return failure
        return {**response, "multi_action_group": group}

    def _process(
        self,
        request_id: str,
        text: str,
        *,
        extracted_overrides: Mapping[str, Any] | None = None,
        source: str = "typed",
        voice: dict[str, Any] | None = None,
        allow_split: bool = True,
    ) -> dict[str, Any]:
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
        extracted_overrides = extracted_overrides or {}
        pre_extracted = extracted_overrides.get("TRANSFER")
        mixed_utility_amount_minor: int | None = None
        mixed_utility_amount_evidence: str | None = None
        mixed_transfer_amount_minor: int | None = None
        mixed_transfer_amount_evidence: str | None = None
        mixed_utility_proposal: dict[str, Any] | None = None
        verdict = input_firewall.inspect(text, source="user_request")
        if not verdict.allowed:
            # The planner never sees blocked text; the attempt is recorded.
            return self._nonexecuting(
                request_id,
                text,
                "BLOCKED",
                "PROMPT_INJECTION_BLOCKED",
                trace,
                extra={"firewall": verdict.to_dict()},
            )
        trace.append({"stage": "INPUT_FIREWALL_PASSED"})
        social_answer = conversational_answer(text)
        if social_answer is not None:
            return self._nonexecuting(
                request_id, text, "ANSWERED", "CONVERSATIONAL", trace,
                extra={"answer": social_answer},
            )
        scam = scam_guard.assess(text)
        if scam.held:
            # Held, not drafted: stricter-only rules mean no layer can undo this.
            return self._nonexecuting(
                request_id,
                text,
                "BLOCKED",
                "SCAM_SUSPECTED",
                trace,
                extra={"scam": scam.to_dict(), "warning": scam_guard.WARNING},
            )
        woken, request_after_wake = voice_dialog.wake(text)
        if woken and not request_after_wake:
            # Just "Hey Dimon": a greeting, not a request for the model.
            return self._nonexecuting(
                request_id, text, "ANSWERED", "GREETING", trace,
                extra={"answer": "Hi, I'm Dimon. How can I help you?"},
            )
        from .multi_action import looks_like_multi_action_request
        # Several requests in one sentence ("send John $20 and buy 2 Apple
        # shares") are split by fixed rules and each part runs the whole
        # pipeline on its own, with its own receipt and approval (by voice
        # under S$200). This needs no model, so it works when the model is
        # busy, and one unclear part never blocks the others.
        #
        # A bill paid alongside a transfer is the exception: the group path
        # sets one approval tier from their combined total, so two payments
        # under S$200 that add up to more can't each pass on voice alone.
        parts = multi_intent.split(text) if allow_split else [text]
        with_bill = _mentions_bill(text)
        if (
            allow_split
            and self.multi_action_extractor is not None
            and looks_like_multi_action_request(text)
            and (with_bill or len(parts) < 2)
        ):
            grouped = self._process_multi_action(request_id, text, trace)
            if with_bill or grouped.get("outcome") not in {"FAILED", "CLARIFICATION_REQUEST"}:
                return grouped
            # Not a bill and the plan couldn't be verified: handled the
            # ordinary way below.
            trace.append({"stage": "MULTI_ACTION_FALLBACK", "reason_code": grouped.get("reason_code")})
        if len(parts) > 1:
            return self._process_multi(request_id, text, parts, trace, source=source, voice=voice)
        if (
            self.bill_payment_extractor is not None
            and bill_payment_grounding.looks_like_bill_payment_request(text)
        ):
            try:
                bill_extracted = (
                    extracted_overrides.get("BILL_PAYMENT")
                    or self.bill_payment_extractor.extract(text)
                )
            except InferenceFailure as exc:
                return self._nonexecuting(
                    request_id,
                    text,
                    "FAILED",
                    exc.reason_code,
                    trace,
                )
            trace.append(
                {
                    "stage": "BILL_PAYMENT_INTENT_EXTRACTED",
                    "latency_ms": bill_extracted.latency_ms,
                }
            )
            try:
                prepared_bill = bill_payment_grounding.prepare_bill_payment(
                    text,
                    bill_extracted.intent,
                    allow_multiple_amounts=bool(
                        _MIXED_BILL_TRANSFER_PATTERN.search(text)
                    ),
                )
            except bill_payment_grounding.BillPaymentGroundingError as exc:
                return self._nonexecuting(
                    request_id,
                    text,
                    "BLOCKED",
                    exc.reason_code,
                    trace,
                    extracted=bill_extracted,
                )
            if prepared_bill.status != "NOT_BILL_PAYMENT":
                if prepared_bill.status != "READY":
                    return self._nonexecuting(
                        request_id,
                        text,
                        "CLARIFICATION_REQUEST",
                        prepared_bill.status,
                        trace,
                        extracted=bill_extracted,
                        extra={
                            "answer": prepared_bill.question,
                            "question": prepared_bill.question,
                            "bill_payment_resolution": {
                                "status": prepared_bill.status,
                                "biller_evidence": (
                                    prepared_bill.proposal.biller_evidence
                                    if prepared_bill.proposal
                                    else None
                                ),
                                "candidates": [
                                    {
                                        "biller_id": candidate.biller_id,
                                        "display_name": candidate.display_name,
                                    }
                                    for candidate in prepared_bill.biller_candidates
                                ],
                            }
                            if prepared_bill.status
                            in {"BILLER_AMBIGUOUS", "BILLER_UNSUPPORTED"}
                            else None,
                        },
                    )
                proposal = prepared_bill.proposal
                assert prepared_bill.amount_minor is not None
                assert prepared_bill.resolved_biller is not None
                if proposal is not None and proposal.requested_execution_date is not None:
                    return self._nonexecuting(
                        request_id,
                        text,
                        "CLARIFICATION_REQUEST",
                        "SCHEDULED_BILL_PAYMENT_UNSUPPORTED",
                        trace + [{"stage": "SCHEDULED_BILL_PAYMENT_UNSUPPORTED"}],
                        extracted=bill_extracted,
                        extra={
                            "answer": (
                                "Scheduled bill payments are not available in this demo. "
                                "I can prepare it for payment now if you want."
                            ),
                            "question": "Would you like to pay this bill now?",
                        },
                    )
                transfer_candidate = None
                if _MIXED_BILL_TRANSFER_PATTERN.search(text):
                    try:
                        transfer_candidate = self.extractor.extract(text)
                    except InferenceFailure:
                        transfer_candidate = None
                candidate_kind = (
                    transfer_candidate.intent.get("kind")
                    if transfer_candidate is not None
                    else None
                )
                if proposal is None and candidate_kind in {
                    "BENEFICIARY_TRANSFER",
                    "PAYNOW_MOBILE_TRANSFER",
                    "BENEFICIARY_TRANSFER_BATCH",
                }:
                    return self._nonexecuting(
                        request_id,
                        text,
                        "BLOCKED",
                        "MIXED_BILL_TRANSFER_UNRESOLVED",
                        trace + [{"stage": "MIXED_BILL_TRANSFER_UNRESOLVED"}],
                        extracted=transfer_candidate,
                    )
                if candidate_kind in {
                    "BENEFICIARY_TRANSFER",
                    "PAYNOW_MOBILE_TRANSFER",
                } and proposal is not None:
                    try:
                        amount_mentions = derive_amount_mentions_from_text(text)
                        bill_evidence = normalize_evidence(proposal.amount_evidence)
                        recipient_evidence = transfer_candidate.intent.get(
                            "recipient_evidence"
                        ) or transfer_candidate.intent.get("proxy_evidence")
                        recipient_start = (
                            text.casefold().find(recipient_evidence.casefold())
                            if isinstance(recipient_evidence, str)
                            else -1
                        )
                        bill_mentions = [
                            mention for mention in amount_mentions
                            if normalize_evidence(mention.evidence) == bill_evidence
                            and 0 <= mention.start < recipient_start
                        ]
                        transfer_mentions = [
                            mention for mention in amount_mentions
                            if recipient_start >= 0 and mention.start >= recipient_start
                        ]
                    except GroundingError:
                        bill_mentions, transfer_mentions = [], []
                    if len(bill_mentions) != 1 or len(transfer_mentions) != 1:
                        return self._nonexecuting(
                            request_id,
                            text,
                            "BLOCKED",
                            "AMBIGUOUS_AMOUNT",
                            trace + [{"stage": "MIXED_PAYMENT_AMOUNTS_AMBIGUOUS"}],
                            extracted=transfer_candidate,
                        )
                    pre_extracted = transfer_candidate
                    mixed_utility_amount_minor = prepared_bill.amount_minor
                    mixed_utility_amount_evidence = proposal.amount_evidence
                    mixed_transfer_amount_minor = transfer_mentions[0].amount_minor
                    mixed_transfer_amount_evidence = transfer_mentions[0].evidence
                    mixed_utility_proposal = {
                        **proposal._asdict(),
                        "biller_id": prepared_bill.resolved_biller.biller_id,
                        "biller_display_name": prepared_bill.resolved_biller.display_name,
                        "amount_minor": prepared_bill.amount_minor,
                        "required_authentication": authentication_for_amount_minor(
                            prepared_bill.amount_minor + mixed_transfer_amount_minor
                        ),
                        "read_only": True,
                    }
                    trace.append({"stage": "MIXED_BILL_AND_TRANSFER_GROUNDED"})
                elif candidate_kind == "BENEFICIARY_TRANSFER_BATCH":
                    return self._nonexecuting(
                        request_id,
                        text,
                        "BLOCKED",
                        "MIXED_BILL_BATCH_UNSUPPORTED",
                        trace + [{"stage": "MIXED_BILL_BATCH_UNSUPPORTED"}],
                        extracted=transfer_candidate,
                    )
                elif _MIXED_BILL_TRANSFER_PATTERN.search(text):
                    return self._nonexecuting(
                        request_id,
                        text,
                        "BLOCKED",
                        "MIXED_BILL_TRANSFER_UNRESOLVED",
                        trace + [{"stage": "MIXED_BILL_TRANSFER_UNRESOLVED"}],
                        extracted=transfer_candidate or bill_extracted,
                    )
                else:
                    source = self.database.resolve_source(
                        (proposal.source_account_label if proposal else prepared_bill.source_account_label)
                        or "savings"
                    )
                    biller = self.database.resolve_utility_biller(
                        prepared_bill.resolved_biller.biller_id
                    )
                    if source is None or biller is None:
                        return self._nonexecuting(
                            request_id, text, "BLOCKED", "BILL_PAYMENT_RESOLUTION_FAILED",
                            trace + [{"stage": "BILL_PAYMENT_RESOLUTION_FAILED"}],
                            extracted=bill_extracted,
                        )
                    if source["balance_minor"] < prepared_bill.amount_minor:
                        return self._nonexecuting(
                            request_id, text, "BLOCKED", "INSUFFICIENT_FUNDS",
                            trace + [{"stage": "INSUFFICIENT_FUNDS"}],
                            extracted=bill_extracted,
                        )
                    bill_intent = {
                        **(proposal._asdict() if proposal else bill_extracted.intent),
                        "kind": "UTILITY_BILL_PAYMENT",
                        "biller_id": prepared_bill.resolved_biller.biller_id,
                        "biller_display_name": prepared_bill.resolved_biller.display_name,
                        "amount_minor": prepared_bill.amount_minor,
                        "currency": "SGD",
                    }
                    bill_evidence = (
                        proposal.biller_evidence
                        if proposal is not None
                        else prepared_bill.biller_evidence
                    )
                    amount_evidence = proposal.amount_evidence if proposal is not None else None
                    amount_source = prepared_bill.amount_source or "USER_TRANSCRIPT"
                    bill_intent["amount_source"] = amount_source
                    draft = ReceiptDraft(
                        request_id=request_id,
                        original_text=text,
                        user_id=DEMO_USER_ID,
                        source_account_id=source["account_id"],
                        beneficiary_id=biller["beneficiary_id"],
                        recipient_display_name=biller["display_name"],
                        amount_minor=prepared_bill.amount_minor,
                        currency="SGD",
                        destination_kind="UTILITY_BILL",
                        destination_reference=biller["beneficiary_id"],
                        destination_account_id=biller["destination_account_id"],
                        evidence_bundle=(
                            {"turn": 1, "text": text},
                            {
                                "kind": "UTILITY_BILL_PAYMENT",
                                "biller_id": biller["beneficiary_id"],
                                "biller_evidence": bill_evidence,
                                "amount_evidence": amount_evidence,
                                "amount_minor": prepared_bill.amount_minor,
                                "amount_source": amount_source,
                                "requested_execution_date": (
                                    proposal.requested_execution_date if proposal else None
                                ),
                            },
                        ),
                    )
                    amount_label = _money_label(prepared_bill.amount_minor)
                    question = (
                        f"I’m ready to pay your {biller['display_name']} bill for "
                        f"{amount_label} from {source['label']}. Review the details and "
                        "confirm on your device."
                    )
                    try:
                        pending = self.database.create_pending_receipts(
                            (draft,),
                            raw_model_output=bill_extracted.raw_output,
                            validated_intent=bill_intent,
                            reason_code="BILL_PAYMENT_CONFIRMATION_REQUIRED",
                            response_base={
                                "outcome": "CLARIFICATION_REQUEST",
                                "question": question,
                                "trace": trace + [
                                    {"stage": "BILL_PAYMENT_PROPOSAL_GROUNDED"},
                                    {"stage": "BILLER_DIRECTORY_MATCHED", "biller_id": biller["beneficiary_id"]},
                                    {"stage": "BILL_PAYMENT_CONFIRMATION_REQUIRED"},
                                ],
                            },
                        )
                    except IdempotencyConflict:
                        return self._nonexecuting(
                            request_id, text, "BLOCKED", "IDEMPOTENCY_CONFLICT", trace,
                            extracted=bill_extracted,
                        )
                    except sqlite3.Error as exc:
                        return self._nonexecuting(
                            request_id, text, "FAILED", "DATABASE_ERROR", trace,
                            extracted=bill_extracted, detail=str(exc),
                        )
                    return {**pending, "state": self.database.get_state()}
            trace.append({"stage": "BILL_PAYMENT_MODEL_NOT_APPLICABLE"})
        model_stock_label: str | None = None
        model_stock_objective = "GENERAL_ACQUISITION"
        model_stock_kind: str | None = None
        catalog_mentioned = bool(investing.find_instruments(text)) or bool(
            investing.find_plan_template(text)
        )
        if catalog_mentioned:
            try:
                synthetic_invest_intent = investing.understand(text)
            except investing.InvestRejected as exc:
                return self._nonexecuting(request_id, text, "BLOCKED", exc.reason_code, trace)
            if synthetic_invest_intent is not None:
                return self._investing(request_id, text, trace, synthetic_invest_intent)
        rule_stock = None if catalog_mentioned else stock_request.parse(text)
        if rule_stock is not None and rule_stock.action == stock_request.SELL:
            return self._real_stock_sell(request_id, text, trace, rule_stock)
        if (
            self.stock_extractor is not None
            and not catalog_mentioned
            and stock_market.looks_like_stock_request(text)
        ):
            try:
                stock_extracted = (
                    extracted_overrides.get("STOCK_INVESTMENT")
                    or self.stock_extractor.extract(text)
                )
            except InferenceFailure as exc:
                logger.warning(json.dumps({
                    "event": "stock_intent_extraction_failed",
                    "request_id": request_id,
                    "provider": type(self.stock_extractor).__name__,
                    "reason_code": exc.reason_code,
                }, sort_keys=True, separators=(",", ":")))
                trace.append(
                    {
                        "stage": "STOCK_MODEL_UNAVAILABLE",
                        "reason_code": exc.reason_code,
                    }
                )
            else:
                trace.append(
                    {
                        "stage": "STOCK_INTENT_EXTRACTED",
                        "latency_ms": stock_extracted.latency_ms,
                    }
                )
                model_stock = stock_market.extract_model_stock_label(
                    text, stock_extracted.intent
                )
                if model_stock is not None:
                    model_stock_label, model_stock_objective = model_stock
                    model_stock_kind = stock_extracted.intent.get("kind")
                    trace.append(
                        {
                            "stage": "STOCK_INSTRUMENT_MENTION_GROUNDED",
                            "instrument_label": model_stock_label,
                        }
                    )
                else:
                    trace.append({"stage": "STOCK_MODEL_NOT_APPLICABLE"})
        if model_stock_kind == "STOCK_RESEARCH" and model_stock_label is not None:
            return self._real_stock_research(
                request_id,
                text,
                trace,
                model_stock_label,
            )
        if model_stock_kind == "STOCK_INVESTMENT" and model_stock_label is not None:
            model_sized_intent = stock_market.raw_intent_from_model_size(
                model_stock_label, model_stock_objective, stock_extracted.intent
            )
            if model_sized_intent is not None:
                return self._real_stock_brief(
                    request_id, text, trace, model_sized_intent,
                    explicit_market_order=stock_market.has_explicit_market_order(text),
                )
            return self._start_stock_size_conversation(
                request_id,
                text,
                trace,
                model_stock_label,
                model_stock_objective,
            )
        stock_intent = None if catalog_mentioned else stock_market.parse_raw_investment_intent(
            text,
            instrument_label=model_stock_label,
            objective=model_stock_objective,
        )
        if stock_intent is not None:
            return self._real_stock_brief(
                request_id, text, trace, stock_intent,
                explicit_market_order=stock_market.has_explicit_market_order(text),
            )
        try:
            invest_intent = investing.understand(text)
        except investing.InvestRejected as exc:
            if rule_stock is None:
                return self._nonexecuting(request_id, text, "BLOCKED", exc.reason_code, trace)
            invest_intent = None
        if rule_stock is not None and (invest_intent is None or invest_intent.kind in {"TRENDING", "QUOTE"}):
            trace.append({"stage": "STOCK_REQUEST_RULES_MATCHED", "action": rule_stock.action})
            if rule_stock.action == stock_request.RESEARCH:
                return self._real_stock_research(request_id, text, trace, model_stock_label or rule_stock.label)
            if rule_stock.quantity or rule_stock.allocation:
                return self._real_stock_brief(
                    request_id, text, trace,
                    stock_market.RawInvestmentIntent(
                        instrument_label=model_stock_label or rule_stock.label,
                        allocation=rule_stock.allocation,
                        explicit_currency=rule_stock.explicit_currency,
                        bare_dollar=rule_stock.bare_dollar,
                        quantity=rule_stock.quantity if rule_stock.allocation is None else None,
                        objective=model_stock_objective,
                    ),
                    explicit_market_order=stock_market.has_explicit_market_order(text),
                )
            return self._start_stock_size_conversation(
                request_id, text, trace, model_stock_label or rule_stock.label, model_stock_objective,
            )
        stock_label = model_stock_label or stock_market.extract_direct_stock_label(text)
        if (
            stock_label is not None
            and not investing.find_instruments(text)
            and (invest_intent is None or invest_intent.kind == "TRENDING")
        ):
            return self._start_stock_size_conversation(
                request_id, text, trace, stock_label, model_stock_objective,
            )
        if invest_intent is not None:
            return self._investing(request_id, text, trace, invest_intent)
        addition = payee_additions.understand(text)
        if addition is not None:
            return self._payee_addition(request_id, text, trace, addition)
        if no_approval.is_balance_inquiry(text) or no_approval.looks_like_money_request(text):
            return self._answer_without_approval(request_id, text, trace)
        extracted = None if pre_extracted is not None else rule_parser.parse(text)
        if extracted is None:
            try:
                extracted = pre_extracted or self.extractor.extract(text)
            except InferenceFailure as exc:
                return self._nonexecuting(
                    request_id,
                    text,
                    "FAILED",
                    exc.reason_code,
                    trace,
                )
            model_trace = {
                "stage": "MODEL_RESPONSE_RECEIVED",
                "latency_ms": extracted.latency_ms,
            }
        else:
            # A plain one-recipient transfer: built by fixed rules, then
            # checked by the same grounding and policy as model output.
            model_trace = {"stage": "RULE_PARSER_MATCHED", "latency_ms": 0.0}

        intent = extracted.intent
        if extracted.fixture_case_id is not None:
            model_trace["fixture_case_id"] = extracted.fixture_case_id
        trace.append(model_trace)
        trace.append({"stage": "INTENT_VALIDATED"})

        recovered_batch = self._recover_exact_saved_batch(text, intent)
        if recovered_batch is not None:
            if intent.get("payment_reference") and not re.search(r"\d", intent["payment_reference"]):
                trace.append({"stage": "BATCH_REFERENCE_NOTED", "reference": intent["payment_reference"]})
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
        amount_text = text
        if derived_proxy_evidence:
            amount_text = text.replace(derived_proxy_evidence, " ", 1)

        try:
            if mixed_utility_amount_minor is None:
                verified_amount, verified_amount_evidence = derive_amount_from_text(amount_text)
            else:
                if mixed_transfer_amount_minor is None or not mixed_transfer_amount_evidence:
                    raise GroundingError("AMBIGUOUS_AMOUNT")
                verified_amount = mixed_transfer_amount_minor
                verified_amount_evidence = mixed_transfer_amount_evidence
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
        if mixed_utility_amount_minor is not None:
            grounded_intent.update(
                {
                    "read_only_utility_amount_minor": mixed_utility_amount_minor,
                    "read_only_utility_amount_evidence": mixed_utility_amount_evidence,
                    "read_only_utility_proposal": mixed_utility_proposal,
                }
            )
        grounded_intent["payment_reference"] = verify_optional_reference(
            text,
            intent["payment_reference"],
            intent["reference_evidence"],
        )
        if grounded_intent["payment_reference"] is None:
            grounded_intent["reference_evidence"] = None
        intent = grounded_intent
        extracted = type(extracted)(intent, extracted.raw_output, extracted.latency_ms)

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
                    extra=self._source_question(user_sources),
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
            or any(is_grounded(text, alias) for alias in candidate.get("aliases", ()))
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
            # A unique first-name match (for example, "Alicia" when only
            # Alicia Powell is saved) is grounded in the user's request and
            # can be carried into the amount clarification. Without saving
            # this locked recipient, the next amount-only voice turn is
            # classified as a brand-new payment and asks for the recipient
            # again. Multiple candidates remain unresolved and are never
            # selected implicitly.
            beneficiary = (
                resolution.matches[0]
                if resolution.kind == "exact"
                or (resolution.kind == "candidates" and len(resolution.matches) == 1)
                else None
            )
            if beneficiary is not None:
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
            return self._receipt_confirmation(
                request_id,
                text,
                "CLOSE_RECIPIENT_MATCH",
                trace,
                extracted,
                source,
                resolution.matches,
                f"Did you mean {self._payee_options(resolution.matches)}?",
            )
        if resolution.kind == "candidates":
            label = intent["recipient_label"].strip()
            return self._receipt_confirmation(
                request_id,
                text,
                "AMBIGUOUS_RECIPIENT",
                trace,
                extracted,
                source,
                resolution.matches,
                (
                    f"I see {len(resolution.matches)} saved payees named {label}. "
                    if len(resolution.matches) > 1
                    else ""
                )
                + f"Did you mean {self._payee_options(resolution.matches)}?",
            )
        if resolution.kind == "phonetic":
            return self._receipt_confirmation(
                request_id,
                text,
                "PHONETIC_RECIPIENT_MATCH",
                trace,
                extracted,
                source,
                resolution.matches,
                f"Did you mean {self._payee_options(resolution.matches)}?",
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
        # Zero-trust boundary: the agent only drafts. Even a fully specified
        # command becomes an immutable receipt that executes solely on a
        # device signature from the user; there is no auto-execution path.
        fully_specified = _has_affirmative_target_slot(
            text,
            beneficiary["display_name"],
            intent["amount_evidence"],
            source["label"],
        )
        amount = _decimal_amount(intent["amount_minor"])
        return self._receipt_confirmation(
            request_id,
            text,
            "SIGNED_APPROVAL_REQUIRED" if fully_specified else "ACTION_CONFIRMATION_REQUIRED",
            trace,
            extracted,
            source,
            (beneficiary,),
            f"Send S${amount} to {beneficiary['display_name']} from {source['label']}?",
        )

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
        ):
            return None
        # A reference is allowed only as a closing clause that covers the
        # whole batch ("... for the flowers"): after every name and amount,
        # and with no numbers of its own.
        reference_start = None
        try:
            stated_amounts = {
                normalize_evidence(mention.evidence)
                for mention in derive_amount_mentions_from_text(text)
            }
        except GroundingError:
            stated_amounts = set()
        if normalize_evidence(intent.get("reference_evidence") or "") in stated_amounts:
            # The model labelled one of the amounts as the reference ("S$6.50");
            # an amount is never a reference, and amounts are grounded later.
            intent = {**intent, "payment_reference": None, "reference_evidence": None}
        if intent.get("payment_reference") is not None:
            evidence = intent.get("reference_evidence") or ""
            reference_start = text.casefold().rfind(evidence.casefold()) if evidence else -1
            if reference_start < 0 or re.search(r"\d", evidence):
                return None

        # Each payee may be named by full name, a saved nickname ("my mum"),
        # or a first name that belongs to exactly one saved payee ("Alicia").
        # The batch receipt then shows every full name and account ending.
        saved = self.database.list_saved_beneficiaries()
        first_names: dict[str, int] = {}
        for beneficiary in saved:
            first = beneficiary["display_name"].split()[0].casefold()
            first_names[first] = first_names.get(first, 0) + 1
        mentions: list[tuple[int, str, str]] = []
        spans: list[tuple[int, int]] = []
        for beneficiary in saved:
            display_name = beneficiary["display_name"]
            first = display_name.split()[0]
            phrases = [display_name, *(beneficiary.get("aliases") or ())]
            if first_names[first.casefold()] == 1 and first != display_name:
                phrases.append(first)
            found: list[re.Match[str]] = []
            for phrase in sorted(phrases, key=len, reverse=True):
                for match in re.finditer(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE):
                    if not any(start < match.end() and match.start() < end for start, end in [*spans, *((m.start(), m.end()) for m in found)]):
                        found.append(match)
            if len(found) > 1:
                return None
            if found:
                match = found[0]
                spans.append((match.start(), match.end()))
                mentions.append((match.start(), display_name, match.group(0)))

        if not 2 <= len(mentions) <= 4:
            return None
        try:
            amount_mentions = derive_amount_mentions_from_text(text)
        except GroundingError:
            return None
        if len(amount_mentions) != len(mentions):
            return None
        if reference_start is not None and reference_start < max(
            [mention.end for mention in amount_mentions]
            + [start + len(evidence) for start, _, evidence in mentions]
        ):
            return None
        mentions.sort(key=lambda item: item[0])
        proposed_recipient = normalize_evidence(intent.get("recipient_label") or "")
        if proposed_recipient not in {
            normalize_evidence(name)
            for _, display_name, evidence in mentions
            for name in (display_name, evidence)
        }:
            return None

        return {
            "contract_version": 4,
            "kind": "BENEFICIARY_TRANSFER_BATCH",
            "source_account_ref": intent.get("source_account_ref"),
            "source_evidence": intent.get("source_evidence"),
            "items": [
                {
                    "recipient_label": evidence,
                    "recipient_evidence": evidence,
                }
                for _, _, evidence in mentions
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
                    extra=self._source_question(user_sources),
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
            # A unique first name ("Alicia") is accepted: the batch receipt
            # shows every full name and account ending before the signature,
            # and the validator marks partial names for confirmation.
            unique = len(resolution.matches) == 1 and resolution.kind in {"exact", "candidates"}
            if not unique:
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
        for public_item, beneficiary in zip(pending["batch_receipt"]["items"], resolved):
            public_item["masked_destination"] = beneficiary["masked_destination"]
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
        verdict = input_firewall.inspect(text, source="clarification_reply")
        if not verdict.allowed:
            response = {
                "outcome": "BLOCKED",
                "request_id": request_id,
                "reason_code": "PROMPT_INJECTION_BLOCKED",
                "firewall": verdict.to_dict(),
                "trace": [
                    {"stage": "CLARIFICATION_RESPONSE_RECEIVED"},
                    {"stage": "PROMPT_INJECTION_BLOCKED"},
                ],
                "state": self.database.get_state(),
            }
        else:
            response = self._with_validation(
                self._respond_to_clarification(
                    clarification_id, request_id, text, user_id=user_id
                )
            )
        if not response.get("idempotent_replay"):
            self.database.audit_request(
                "CLARIFICATION_ANSWERED",
                request_id,
                text,
                {**response, "clarification_id": clarification_id},
                source="clarification_reply",
            )
        return response

    def _respond_to_clarification(
        self,
        clarification_id: str,
        request_id: str,
        text: str,
        *,
        user_id: str = DEMO_USER_ID,
    ) -> dict[str, Any]:
        trace = [{"stage": "CLARIFICATION_RESPONSE_RECEIVED"}]
        if clarification_id.startswith("multi_clarification_"):
            return self._respond_to_multi_action_clarification(
                clarification_id, request_id, text, user_id=user_id,
            )
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

    def _respond_to_multi_action_clarification(
        self, clarification_id: str, request_id: str, text: str, *, user_id: str,
    ) -> dict[str, Any]:
        trace = [{"stage": "MULTI_ACTION_CLARIFICATION_RESPONSE_RECEIVED"}]
        try:
            session = self.database.get_multi_action_clarification(
                clarification_id, user_id=user_id
            )
        except ClarificationRejected as exc:
            return {
                "outcome": "BLOCKED", "request_id": request_id,
                "reason_code": exc.reason_code,
                "trace": trace + [{"stage": exc.reason_code}],
                "state": self.database.get_state(),
            }

        normalized = re.sub(r"[^a-z0-9 ]", " ", text.casefold())
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if normalized in {"cancel", "cancel it", "never mind", "nevermind", "stop"}:
            self.database.finish_multi_action_clarification(
                clarification_id, request_id, cancelled=True
            )
            response = {
                "outcome": "CANCELLED", "request_id": request_id,
                "answer": "I’ve cancelled that grouped request. Nothing was prepared or sent.",
                "trace": trace + [{"stage": "MULTI_ACTION_CLARIFICATION_CANCELLED"}],
                "state": self.database.get_state(),
            }
            self.database.record_attempt(ActionAttempt(
                request_id=request_id, original_text=text, raw_model_output=None,
                validated_intent=None, outcome="CANCELLED", reason_code=None,
                response=response,
            ))
            return response

        replacements: list[tuple[str, str]] = []
        remaining: list[dict[str, Any]] = []
        beneficiaries = self.database.list_saved_beneficiaries()
        for issue in session["issues"]:
            if issue.get("field") == "recipient":
                mentioned = [
                    row for row in beneficiaries
                    if row["display_name"].casefold() in text.casefold()
                ]
                if len(mentioned) == 1:
                    replacements.append((issue["evidence"], mentioned[0]["display_name"]))
                else:
                    remaining.append(issue)
            elif issue.get("field") == "instrument":
                suggestion = issue.get("suggested_value")
                affirmative = bool(re.search(r"\b(?:yes|yeah|correct|right)\b", normalized))
                explicit = isinstance(suggestion, str) and suggestion.casefold() in text.casefold()
                if suggestion and (affirmative or explicit):
                    replacements.append((issue["evidence"], suggestion))
                else:
                    remaining.append(issue)
            elif issue.get("field") == "stock_size":
                try:
                    size = stock_request.parse_size(text, allow_bare_count=True)
                except ValueError:
                    size = None
                if size is None or not (size.quantity or size.allocation):
                    remaining.append(issue)
                    continue
                size_evidence = size.evidence.strip() if isinstance(size.evidence, str) else ""
                if size.quantity and not re.search(r"\b(?:shares?|stocks?)\b", size_evidence, re.I):
                    size_evidence = f"{size_evidence or size.quantity} shares"
                if not size_evidence:
                    remaining.append(issue)
                    continue
                instrument_evidence = issue.get("evidence")
                if not isinstance(instrument_evidence, str) or not instrument_evidence:
                    remaining.append(issue)
                    continue
                replacements.append((
                    instrument_evidence,
                    f"{size_evidence} of {instrument_evidence}",
                ))
            else:
                remaining.append(issue)

        if remaining:
            question = ", and ".join(
                [item["question"].rstrip("?.") for item in remaining]
            ) + "? Nothing has been prepared."
            return {
                "outcome": "CLARIFICATION_REQUEST", "request_id": request_id,
                "reason_code": "MULTI_ACTION_ITEM_UNRESOLVED",
                "answer": question,
                "clarification": {
                    "clarification_id": clarification_id,
                    "kind": "MULTI_ACTION",
                    "expires_at": session["expires_at"],
                    "issues": remaining,
                    "resolved_items": session["resolved_items"],
                },
                "multi_action_clarification": {
                    "issues": remaining,
                    "resolved_items": session["resolved_items"],
                    "expires_at": session["expires_at"],
                },
                "trace": trace + [{"stage": "MULTI_ACTION_CLARIFICATION_INCOMPLETE"}],
                "state": self.database.get_state(),
            }

        corrected = session["original_text"]
        for old, new in replacements:
            if old not in corrected:
                return {
                    "outcome": "BLOCKED", "request_id": request_id,
                    "reason_code": "MULTI_ACTION_EVIDENCE_INVALID",
                    "answer": "I couldn’t safely apply that clarification. Nothing was prepared.",
                    "trace": trace + [{"stage": "MULTI_ACTION_EVIDENCE_INVALID"}],
                    "state": self.database.get_state(),
                }
            corrected = corrected.replace(old, new, 1)
        result = self._process_multi_action(request_id, corrected, trace)
        if result.get("outcome") != "CLARIFICATION_REQUEST":
            self.database.finish_multi_action_clarification(
                clarification_id, request_id
            )
        return result

    def _audit_refusal(self, receipt_id: str, receipt_digest: str, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("outcome") in ("BLOCKED", "FAILED"):
            self.database.audit_event(
                "APPROVAL_REFUSED",
                {
                    "receipt_digest": receipt_digest,
                    "outcome": result["outcome"],
                    "reason_code": result.get("reason_code"),
                },
                receipt_id=receipt_id,
            )
        return result

    def approve(self, receipt_id: str, receipt_digest: str, **kwargs: Any) -> dict[str, Any]:
        return self._audit_refusal(
            receipt_id, receipt_digest, self._approve(receipt_id, receipt_digest, **kwargs)
        )

    def approve_batch(self, receipt_id: str, receipt_digest: str, **kwargs: Any) -> dict[str, Any]:
        return self._audit_refusal(
            receipt_id, receipt_digest, self._approve_batch(receipt_id, receipt_digest, **kwargs)
        )

    def _approve(
        self,
        receipt_id: str,
        receipt_digest: str,
        *,
        assertion: DeviceAssertion | None,
        context: ApprovalContext | None,
        out_of_band_code: str | None = None,
        read_back_played: bool = False,
        unusual_amount_confirmed: bool = False,
        voice_confirmation: str | None = None,
        user_id: str = DEMO_USER_ID,
    ) -> dict[str, Any]:
        trace = [{"stage": "RECEIPT_APPROVAL_RECEIVED"}]
        try:
            execution = self.database.approve_receipt(
                receipt_id,
                receipt_digest,
                user_id,
                assertion=assertion,
                context=context,
                out_of_band_code=out_of_band_code,
                read_back_played=read_back_played,
                unusual_amount_confirmed=unusual_amount_confirmed,
                voice_confirmation=voice_confirmation,
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

        approval = execution.get("approval") or {}
        stages = []
        if approval.get("singpass_auth_id"):
            stages.append({"stage": "SINGPASS_AUTHORISATION_CONSUMED"})
        if approval.get("face_similarity") is not None:
            stages.append({"stage": "FACE_MATCH_VERIFIED"})
        if approval.get("authentication_method") == "MOCK_VOICE_AFFIRMATION_AND_DEVICE_SIGNATURE":
            stages.append({"stage": "MOCK_VOICE_AFFIRMATION_ACCEPTED"})
        stages.extend(
            [{"stage": "DEVICE_SIGNATURE_VERIFIED"}, {"stage": "RECEIPT_REVALIDATED"}, {"stage": "SQLITE_COMMITTED"}]
        )
        return {**execution, "trace": trace + stages, "state": self.database.get_state()}

    def _approve_batch(
        self,
        receipt_id: str,
        receipt_digest: str,
        *,
        assertion: DeviceAssertion | None,
        context: ApprovalContext | None,
        out_of_band_code: str | None = None,
        read_back_played: bool = False,
        unusual_amount_confirmed: bool = False,
        voice_confirmation: str | None = None,
        user_id: str = DEMO_USER_ID,
    ) -> dict[str, Any]:
        trace = [{"stage": "BATCH_RECEIPT_APPROVAL_RECEIVED"}]
        try:
            execution = self.database.approve_batch_receipt(
                receipt_id,
                receipt_digest,
                user_id,
                assertion=assertion,
                context=context,
                out_of_band_code=out_of_band_code,
                read_back_played=read_back_played,
                unusual_amount_confirmed=unusual_amount_confirmed,
                voice_confirmation=voice_confirmation,
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
        approval = execution.get("approval") or {}
        stages = []
        if approval.get("singpass_auth_id"):
            stages.append({"stage": "SINGPASS_AUTHORISATION_CONSUMED"})
        if approval.get("face_similarity") is not None:
            stages.append({"stage": "FACE_MATCH_VERIFIED"})
        if approval.get("authentication_method") == "MOCK_VOICE_AFFIRMATION_AND_DEVICE_SIGNATURE":
            stages.append({"stage": "MOCK_VOICE_AFFIRMATION_ACCEPTED"})
        stages.extend(
            [{"stage": "DEVICE_SIGNATURE_VERIFIED"}, {"stage": "BATCH_RECEIPT_REVALIDATED"}, {"stage": "SQLITE_BATCH_COMMITTED"}]
        )
        return {**execution, "trace": trace + stages, "state": self.database.get_state()}

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
                evidence_bundle=_receipt_evidence_bundle(text, extracted.intent),
            )
            for beneficiary in beneficiaries
        )
        response_base = {
            "outcome": "CLARIFICATION_REQUEST",
            "question": question,
            "trace": trace + [{"stage": reason_code}],
            "state": self.database.get_state(),
        }
        if extracted.intent.get("read_only_utility_proposal") is not None:
            response_base["read_only_bill_payment"] = extracted.intent[
                "read_only_utility_proposal"
            ]
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
            evidence_bundle=_receipt_evidence_bundle(text, extracted.intent),
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
        if extracted.intent.get("read_only_utility_proposal") is not None:
            response_base["read_only_bill_payment"] = extracted.intent[
                "read_only_utility_proposal"
            ]
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

    _INVEST_QUESTIONS = {
        "MISSING_QUANTITY": "How many units, or how much in dollars? For example: buy 20 shares, or buy S$500 of it.",
        "MISSING_MONTHLY_AMOUNT": "How much a month? For example: S$300 a month.",
        "PLAN_NOT_CHOSEN": "Which plan or fund should the monthly amount go into?",
        "NOT_HELD": "You don't hold that investment.",
        "SELL_MORE_THAN_HELD": "That's more than you hold.",
        "AMOUNT_BELOW_ONE_UNIT": "That amount is less than the price of one unit.",
        "AMOUNT_BELOW_MINIMUM": "That amount is too small to cover the trading fee.",
        "PLAN_AMOUNT_OUT_OF_RANGE": "Monthly plans run from S$50 to S$2,000 a month.",
        "ORDER_LIMIT_EXCEEDED": "A single order can be up to S$5,000.",
        "DAILY_TRADING_LIMIT_EXCEEDED": "That would pass the S$10,000 daily trading limit.",
        "INSUFFICIENT_FUNDS": "There isn't enough in savings for this order.",
        "ONE_INSTRUMENT_PER_ORDER": "Please name one investment per order.",
        "AMBIGUOUS_AMOUNT": "Please say one amount.",
    }

    def _real_stock_brief(
        self,
        request_id: str,
        text: str,
        trace: list[dict[str, Any]],
        intent: stock_market.RawInvestmentIntent,
        *,
        explicit_market_order: bool = False,
    ) -> dict[str, Any]:
        trace.append({"stage": "REAL_STOCK_REQUEST_UNDERSTOOD"})
        try:
            brief = stock_market.build_investment_brief(
                intent,
                self.stock_provider,
                captured_at=self.database._clock(),
                funding_currency="SGD",
                request_id=request_id,
            )
        except stock_market.StockMarketError as exc:
            question = {
                "INSTRUMENT_NOT_FOUND": f"I couldn't find a listed company called {intent.instrument_label}. What's the company name or ticker?",
                "INSTRUMENT_AMBIGUOUS": f"I found more than one company matching {intent.instrument_label}. Which one, or what's its ticker?",
            }.get(exc.reason_code, "I couldn't get a live price for that stock right now. Please try again in a moment.")
            return self._nonexecuting(
                request_id,
                text,
                "BLOCKED",
                exc.reason_code,
                trace,
                extra={"question": question} if question else None,
            )
        funding_accounts = [
            account
            for account in self.database.get_state()["accounts"]
            if account["is_user_source"] and account["currency"] == brief.funding_currency
        ]
        if len(funding_accounts) == 1:
            account = funding_accounts[0]
            brief = stock_market.assess_funding(
                brief,
                account_label=account["label"],
                account_id=account["account_id"],
                account_currency=account["currency"],
                balance=Decimal(account["balance_minor"]) / 100,
            )
            trace.append(
                {
                    "stage": "FUNDING_ACCOUNT_CHECKED",
                    "source_account_id": account["account_id"],
                    "sufficient": brief.funding_shortfall == 0,
                }
            )
        trace.append(
            {
                "stage": "REAL_MARKET_SNAPSHOT_VERIFIED",
                "symbol": brief.instrument.symbol,
                "source": brief.market.source,
                "captured_at": brief.market.captured_at.isoformat(),
            }
        )
        answer = stock_market.brief_response(brief)
        response = self._nonexecuting(
            request_id,
            text,
            "ANSWERED",
            "REAL_STOCK_BRIEF",
            trace,
            extra={
                "answer": answer,
                "question": brief.next_question,
                "investing": {
                    "view": "REAL_STOCK_BRIEF",
                    "brief": brief.to_dict(),
                    "disclaimer": stock_market.DISCLAIMER,
                },
            },
        )
        if self.conversation_service is None:
            return response
        try:
            conversation = self.conversation_service.start(
                domain="INVESTMENT",
                session_scope=self.session_scope,
                user_id=self.user_id,
                payload={
                    "brief": brief,
                    "initial_order_type": "MARKET" if explicit_market_order else None,
                },
            )
        except Exception:
            response["trace"] = [
                *(response.get("trace") or ()),
                {"stage": "CONVERSATION_START_BEST_EFFORT_FAILED"},
            ]
            return response
        response.update(
            {
                "answer": conversation.get("answer") or response.get("answer"),
                "question": conversation.get("question", response.get("question")),
                "allowed_choices": conversation.get("allowed_choices", []),
                "conversation": conversation.get("conversation"),
                "conversation_trace": conversation.get("trace", []),
            }
        )
        return response

    def _real_stock_research(
        self,
        request_id: str,
        text: str,
        trace: list[dict[str, Any]],
        instrument_label: str,
    ) -> dict[str, Any]:
        trace.append({"stage": "REAL_STOCK_RESEARCH_UNDERSTOOD"})
        try:
            instrument, market = stock_market.build_stock_research(
                instrument_label,
                self.stock_provider,
                captured_at=self.database._clock(),
                request_id=request_id,
            )
        except stock_market.StockMarketError as exc:
            question = {
                "INSTRUMENT_NOT_FOUND": f"I couldn't find a listed company called {instrument_label}. What's the company name or ticker?",
                "INSTRUMENT_AMBIGUOUS": f"I found more than one company matching {instrument_label}. Which one, or what's its ticker?",
            }.get(exc.reason_code, "I couldn't get a live price for that stock right now. Please try again in a moment.")
            return self._nonexecuting(
                request_id,
                text,
                "BLOCKED",
                exc.reason_code,
                trace,
                extra={"question": question} if question else None,
            )
        trace.append(
            {
                "stage": "REAL_MARKET_SNAPSHOT_VERIFIED",
                "symbol": instrument.symbol,
                "source": market.source,
                "captured_at": market.captured_at.isoformat(),
            }
        )
        return self._nonexecuting(
            request_id,
            text,
            "ANSWERED",
            "REAL_STOCK_RESEARCH",
            trace,
            extra={
                "answer": stock_market.stock_research_response(instrument, market),
                "investing": {
                    "view": "REAL_STOCK_RESEARCH",
                    "research": stock_market.stock_research_to_dict(instrument, market),
                    "disclaimer": stock_market.DISCLAIMER,
                },
            },
        )

    def _start_stock_size_conversation(
        self,
        request_id: str,
        text: str,
        trace: list[dict[str, Any]],
        instrument_label: str,
        objective: str,
    ) -> dict[str, Any]:
        # Name the verified company and its price, and refuse a name that is
        # not a listed company instead of asking "how many shares of lunch".
        answer = None
        try:
            instrument, quote = stock_market.build_stock_research(
                instrument_label,
                self.stock_provider,
                captured_at=self.database._clock(),
                request_id=request_id,
            )
        except stock_market.StockMarketError as exc:
            if exc.reason_code in {"INSTRUMENT_NOT_FOUND", "INSTRUMENT_AMBIGUOUS", "UNSUPPORTED_INSTRUMENT"}:
                return self._instrument_not_found(request_id, text, trace, instrument_label, exc.reason_code)
        except Exception:
            pass   # the quote is checked again once the size is known
        else:
            instrument_label = instrument.symbol
            answer = (
                f"{instrument.name} ({instrument.symbol}) is "
                f"{stock_market._amount_label(instrument.quote_currency, quote.price)}."
            )
        question = (
            f"How many shares of {instrument_label} would you like, "
            "or what amount would you like to invest?"
        )
        response = self._nonexecuting(
            request_id,
            text,
            "ANSWERED",
            "REAL_STOCK_ALLOCATION_REQUIRED",
            trace + [{"stage": "STOCK_SIZE_CLARIFICATION_REQUIRED"}],
            extra={
                "answer": f"{answer} {question}" if answer else question,
                "question": question,
                "instrument_label": instrument_label,
            },
        )
        if self.conversation_service is None:
            return response
        try:
            conversation = self.conversation_service.start(
                domain="INVESTMENT",
                session_scope=self.session_scope,
                user_id=self.user_id,
                payload={
                    "stock_request": {
                        "instrument_label": instrument_label,
                        "objective": objective,
                    }
                },
            )
        except Exception:
            response["trace"] = [
                *(response.get("trace") or ()),
                {"stage": "CONVERSATION_START_BEST_EFFORT_FAILED"},
            ]
            return response
        response.update(
            {
                "question": conversation.get("question", question),
                "allowed_choices": conversation.get("allowed_choices", []),
                "conversation": conversation.get("conversation"),
                "conversation_trace": conversation.get("trace", []),
            }
        )
        return response

    def _instrument_not_found(
        self, request_id: str, text: str, trace: list[dict[str, Any]], label: str, reason_code: str
    ) -> dict[str, Any]:
        question = (
            f"I found more than one company matching {label}. Which one, or what's its ticker?"
            if reason_code == "INSTRUMENT_AMBIGUOUS"
            else f"I couldn't find a listed company called {label}. What's the company name or ticker?"
        )
        return self._nonexecuting(
            request_id, text, "BLOCKED", reason_code, trace, extra={"question": question}
        )

    def _held_stocks(self) -> list[dict[str, Any]]:
        from .paper_equity_store import PaperEquityStore

        store = PaperEquityStore(self.database.path, clock=self.database._clock)
        return [h for h in store.portfolio(self.user_id)["holdings"] if int(h["quantity"]) > 0]

    def _real_stock_sell(
        self, request_id: str, text: str, trace: list[dict[str, Any]], request: stock_request.StockRequest
    ) -> dict[str, Any]:
        """Selling shares bought earlier: the same conversation, receipt and
        Singpass + face approval as a purchase, against what is actually held."""

        from .investment_conversation import sell_state

        trace.append({"stage": "REAL_STOCK_SELL_UNDERSTOOD"})
        held_stocks = self._held_stocks()
        owned = ", ".join(f"{int(h['quantity']):,} {h['name']}" for h in held_stocks)
        wanted = stock_market._normalized(request.label)
        held = next(
            (h for h in held_stocks
             if wanted and (wanted == stock_market._normalized(h["symbol"])
                            or wanted in stock_market._normalized(h["name"]))),
            None,
        )
        now = self.database._clock()
        try:
            instrument, quote = stock_market.build_stock_research(
                held["symbol"] if held else request.label,
                self.stock_provider,
                captured_at=now,
                request_id=request_id,
            )
        except stock_market.StockMarketError as exc:
            if held is None:
                return self._instrument_not_found(request_id, text, trace, request.label, exc.reason_code)
            return self._nonexecuting(
                request_id, text, "BLOCKED", exc.reason_code, trace,
                extra={"question": "I couldn't get a live price for that stock right now. Please try again in a moment."},
            )
        if held is None:
            held = next((h for h in held_stocks if h["symbol"] == instrument.symbol), None)
        if held is None:
            answer = (
                f"You don't own any {instrument.name} shares."
                + (f" You hold {owned}." if owned else " You haven't bought any shares yet.")
            )
            return self._nonexecuting(
                request_id, text, "ANSWERED", "NOT_HELD", trace,
                extra={"answer": answer},
            )
        rate, pair, source, captured = Decimal("1"), None, None, None
        if instrument.quote_currency != "SGD":
            try:
                fx = self.stock_provider.fx_estimate(instrument.quote_currency, "SGD", quote.price, now)
            except Exception:
                return self._nonexecuting(
                    request_id, text, "BLOCKED", "FX_UNAVAILABLE", trace,
                    extra={"question": "I couldn't get the exchange rate right now. Please try again in a moment."},
                )
            rate, pair, source, captured = fx.rate, fx.pair, fx.source, fx.captured_at.isoformat()
        state = sell_state(
            instrument=instrument, market=quote, fx_rate=rate, fx_pair=pair, fx_source=source,
            fx_captured_at=captured, held_quantity=int(held["quantity"]),
        )
        size = None
        if request.quantity or request.allocation or request.fraction:
            size = {
                "quantity": request.quantity,
                "allocation": str(request.allocation) if request.allocation is not None else None,
                "explicit_currency": request.explicit_currency,
                "bare_dollar": request.bare_dollar,
                "fraction": request.fraction,
            }
        trace.append({"stage": "REAL_STOCK_HOLDING_FOUND", "symbol": instrument.symbol})
        if self.conversation_service is None:
            return self._nonexecuting(
                request_id, text, "ANSWERED", "REAL_STOCK_SELL", trace,
                extra={"answer": f"You hold {int(held['quantity']):,} {instrument.name} shares."},
            )
        conversation = self.conversation_service.start(
            domain="INVESTMENT",
            session_scope=self.session_scope,
            user_id=self.user_id,
            payload={"sell": state, "size": size},
        )
        response = self._nonexecuting(
            request_id, text, "ANSWERED", "REAL_STOCK_SELL", trace,
            extra={"answer": conversation.get("answer"), "question": conversation.get("question")},
        )
        response.update(
            {
                "allowed_choices": conversation.get("allowed_choices", []),
                "conversation": conversation.get("conversation"),
                "conversation_trace": conversation.get("trace", []),
                "public_view": conversation.get("public_view"),
            }
        )
        return response

    def _investing(
        self, request_id: str, text: str, trace: list[dict[str, Any]], intent: investing.InvestIntent
    ) -> dict[str, Any]:
        """Portfolio, suggestions and quotes are answered at once; orders and
        plans become signed receipts. The model is not used for either."""

        trace.append({"stage": "INVESTMENT_REQUEST_UNDERSTOOD", "kind": intent.kind})
        now = self.database._clock()
        investor = self.database.investor_profile()
        risk = investor["risk_score"] if investor else None
        if intent.kind == "PORTFOLIO":
            overview = self.database.investing_overview()
            answer = (
                f"Your investments are worth {investing.money(overview['total_value_minor'])} "
                f"({'up' if overview['total_gain_minor'] >= 0 else 'down'} {investing.money(abs(overview['total_gain_minor']))} "
                f"on what you paid), with {len(overview['plans']) or 'no'} monthly plan{'' if len(overview['plans']) == 1 else 's'} running."
                if overview["holdings"] else "You don't hold any investments yet. Ask what's trending, or for long-term plans."
            )
            return self._nonexecuting(request_id, text, "ANSWERED", "INVESTMENT_PORTFOLIO", trace,
                                      extra={"answer": answer, "investing": {"view": "PORTFOLIO", **overview}})
        if intent.kind == "TRENDING":
            picks = investing.trending(now, risk)
            names = ", ".join(f"{p['name']} (up {p['change_1m_pct']:.1f}% this month)" for p in picks["trending"][:3])
            answer = f"Trending now: {names}." if names else "Nothing is trending upwards right now."
            return self._nonexecuting(request_id, text, "ANSWERED", "INVESTMENT_TRENDING", trace,
                                      extra={"answer": answer, "investing": {"view": "TRENDING", "profile": investor, **picks}})
        if intent.kind == "PLANS":
            plans = investing.plan_suggestions(risk, investor["horizon_years"] if investor else None,
                                               intent.amount_minor, intent.years)
            best = next(p for p in plans["plans"] if p["best_fit"])
            answer = (
                f"Best fit{' for your profile' if investor else ''}: {best['name']}. "
                f"{investing.money(best['monthly_minor'])} a month for {best['years']} years "
                f"({investing.money(best['contributed_minor'])} paid in) could grow to about "
                f"{investing.money(best['projection_minor']['middle'])}, within a range of "
                f"{investing.money(best['projection_minor']['cautious'])} to {investing.money(best['projection_minor']['strong'])}."
            )
            return self._nonexecuting(request_id, text, "ANSWERED", "INVESTMENT_PLANS", trace,
                                      extra={"answer": answer, "investing": {"view": "PLANS", "profile": investor, **plans}})
        if intent.kind == "QUOTE":
            snap = market.snapshot(intent.instrument.symbol, now)
            answer = (f"{snap['name']} ({snap['symbol']}): {investing.money(snap['price_minor'])}, "
                      f"{'up' if snap['change_1d_pct'] >= 0 else 'down'} {abs(snap['change_1d_pct']):.2f}% today, "
                      f"{snap['change_1y_pct']:+.1f}% from one year ago.")
            return self._nonexecuting(request_id, text, "ANSWERED", "INVESTMENT_QUOTE", trace,
                                      extra={"answer": answer, "investing": {"view": "QUOTE", "instrument": snap,
                                             "history": market.history(snap["symbol"], now), "profile": investor,
                                             "disclaimer": market.DISCLAIMER}})
        try:
            order = self.database.draft_investment_order(request_id, text, intent)
        except investing.InvestRejected as exc:
            if exc.reason_code == "INVESTOR_PROFILE_REQUIRED":
                return self._nonexecuting(
                    request_id, text, "CLARIFICATION_REQUEST", "INVESTOR_PROFILE_REQUIRED", trace,
                    extra={"question": "Before your first investment, answer four quick questions about your goals and risk.",
                           "questions": list(investing.QUESTIONS)},
                )
            return self._nonexecuting(request_id, text, "BLOCKED", exc.reason_code, trace,
                                      extra={"question": self._INVEST_QUESTIONS.get(exc.reason_code)})
        trace.append({"stage": "ORDER_DRAFTED", "order_id": order["order_id"]})
        return self._nonexecuting(
            request_id, text, "ORDER_CONFIRMATION_REQUEST", "SIGNED_APPROVAL_REQUIRED", trace,
            extra={"order": order, "question": f"{order['title']}. Confirm with your face?"},
        )

    def approve_order(self, order_id: str, order_digest: str, **kwargs: Any) -> dict[str, Any]:
        trace = [{"stage": "ORDER_APPROVAL_RECEIVED"}]
        try:
            result = self.database.approve_order(order_id, order_digest, **kwargs)
        except ReceiptRejected as exc:
            result = {
                "outcome": "BLOCKED",
                "reason_code": exc.reason_code,
                "trace": trace + [{"stage": exc.reason_code}],
                "state": self.database.get_state(),
            }
        return self._audit_refusal(order_id, order_digest, result)

    @staticmethod
    def _source_question(user_sources: list[dict[str, Any]]) -> dict[str, Any]:
        """Ask which account pays. The answer is the user's own words: the
        browser re-sends the request with "from <account>" added."""

        labels = [account["label"] for account in user_sources]
        return {
            "question": f"Which account should fund this transfer: {' or '.join(labels)}?",
            "source_options": [
                {"label": account["label"], "balance_minor": account["balance_minor"]}
                for account in user_sources
            ],
        }

    def _process_multi(
        self,
        request_id: str,
        text: str,
        parts: list[str],
        trace: list[dict[str, Any]],
        *,
        source: str,
        voice: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Several requests in one sentence. Each part runs through the whole
        pipeline on its own and gets its own receipt and signed approval, so
        approving one never approves another. The firewall and scam check
        have already passed on the whole sentence."""

        trace.append({"stage": "MULTIPLE_REQUESTS_FOUND", "count": len(parts)})
        if len(parts) > multi_intent.MAX_PARTS:
            return self._nonexecuting(
                request_id, text, "BLOCKED", "TOO_MANY_REQUESTS", trace,
                extra={"question": f"Please ask for up to {multi_intent.MAX_PARTS} things at a time."},
            )
        if multi_intent.has_negation(text):
            # "send John $5 and don't buy NVIDIA": rather than guess which part
            # a "don't" cancels, nothing is drafted.
            return self._nonexecuting(
                request_id, text, "BLOCKED", "MULTI_REQUEST_NEGATION", trace,
                extra={"question": "Please say only the things you want done, without 'don't' or 'cancel'."},
            )
        steps = []
        for index, part in enumerate(parts):
            part_request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"dimon:{request_id}:part:{index}"))
            result = self.process(part_request_id, part, source=source, voice=voice, nested=True)
            steps.append({"index": index, "text": part, "request_id": part_request_id, "result": result})
        drafted = sum(
            1 for step in steps
            if step["result"].get("outcome") not in {"BLOCKED", "FAILED"}
        )
        return self._nonexecuting(
            request_id, text, "MULTI_REQUEST", "MULTIPLE_REQUESTS", trace,
            extra={
                "steps": steps,
                "question": (
                    f"I found {len(steps)} requests. Each one is checked and approved on its own"
                    + (f"; {len(steps) - drafted} could not be drafted." if drafted < len(steps) else ".")
                ),
            },
        )

    def _payee_addition(
        self,
        request_id: str,
        text: str,
        trace: list[dict[str, Any]],
        addition: dict[str, str | None],
    ) -> dict[str, Any]:
        trace.append({"stage": "PAYEE_ADDITION_UNDERSTOOD"})
        if not is_grounded(text, addition["proxy_evidence"]):
            return self._nonexecuting(request_id, text, "BLOCKED", "UNGROUNDED_RECIPIENT", trace)
        try:
            lookup = self.paynow_directory.lookup_mobile(addition["proxy"], request_id)
        except PayNowLookupError as exc:
            question = {
                "PAYNOW_PROXY_NOT_FOUND": "That number isn't registered for PayNow, so it can't be added.",
                "PAYNOW_PROXY_INACTIVE": "That PayNow number is no longer active.",
            }.get(exc.reason_code)
            return self._nonexecuting(request_id, text, "BLOCKED", exc.reason_code, trace,
                                      extra={"question": question} if question else None)
        trace.append({"stage": "PAYNOW_PROXY_RESOLVED", "lookup_reference": lookup.lookup_reference})
        masked = payee_additions.masked_proxy(lookup.canonical_proxy)
        nickname = addition["nickname"]
        try:
            self.database.payee_addition_check(lookup.destination_account_id, nickname)
            if nickname is None:
                return self._nonexecuting(
                    request_id, text, "CLARIFICATION_REQUEST", "PAYEE_NICKNAME_REQUIRED", trace,
                    extra={
                        "question": f"{masked} is registered to {lookup.masked_name}. What should I save them as?",
                        "payee_lookup": {"masked_name": lookup.masked_name, "masked_proxy": masked},
                    },
                )
            draft = self.database.draft_payee_addition(request_id, text, lookup, nickname)
        except payee_additions.PayeeAdditionRejected as exc:
            question = {
                "PAYEE_ALREADY_SAVED": f"{masked} ({lookup.masked_name}) is already one of your payees.",
                "NICKNAME_IN_USE": f"You already have a payee called {nickname}. Choose another name.",
                "INVALID_NICKNAME": "Payee names can use letters, spaces, hyphens and apostrophes (3 to 40 characters).",
            }.get(exc.reason_code)
            return self._nonexecuting(request_id, text, "BLOCKED", exc.reason_code, trace,
                                      extra={"question": question})
        trace.append({"stage": "PAYEE_ADDITION_DRAFTED", "addition_id": draft["addition_id"]})
        return self._nonexecuting(
            request_id, text, "PAYEE_ADDITION_REQUEST", "SIGNED_APPROVAL_REQUIRED", trace,
            extra={
                "payee_addition": draft,
                "question": f"Save {masked} ({draft['masked_name']}) as {draft['nickname']}? Confirm with your face.",
            },
        )

    def approve_payee_addition(self, addition_id: str, addition_digest: str, **kwargs: Any) -> dict[str, Any]:
        trace = [{"stage": "PAYEE_ADDITION_APPROVAL_RECEIVED"}]
        try:
            result = self.database.approve_payee_addition(addition_id, addition_digest, **kwargs)
        except ReceiptRejected as exc:
            result = {
                "outcome": "BLOCKED",
                "reason_code": exc.reason_code,
                "trace": trace + [{"stage": exc.reason_code}],
                "state": self.database.get_state(),
            }
        return self._audit_refusal(addition_id, addition_digest, result)

    def _answer_without_approval(
        self, request_id: str, text: str, trace: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Balances and payment requests: nothing leaves the user's accounts,
        so there is no receipt and no signature, and the model is not used."""

        trace.append({"stage": "NO_APPROVAL_NEEDED"})
        if no_approval.is_balance_inquiry(text):
            accounts = [
                account for account in self.database.get_state()["accounts"]
                if account["is_user_source"]
            ]
            answer = ". ".join(
                f"{account['label'][:1].upper()}{account['label'][1:]}: {_money_label(account['balance_minor'])}" for account in accounts
            )
            return self._nonexecuting(
                request_id, text, "ANSWERED", "BALANCE_INQUIRY", trace,
                extra={"answer": f"{answer}.", "balances": accounts},
            )
        parsed = no_approval.parse_money_request(text, self.database.list_saved_beneficiaries())
        if parsed.reason_code is not None:
            question = {
                "UNKNOWN_RECIPIENT": "Who should I ask for the money? Say a saved payee's name.",
                "AMBIGUOUS_RECIPIENT": "Which payee should I ask? Say their full name.",
                "MISSING_AMOUNT": "How much should I ask for?",
            }.get(parsed.reason_code, "Please say one amount and one saved payee.")
            return self._nonexecuting(
                request_id, text, "BLOCKED", parsed.reason_code, trace,
                extra={"question": question},
            )
        beneficiary = parsed.beneficiary
        money_request_id = self.database.request_money(
            request_id, beneficiary["beneficiary_id"], parsed.amount_minor
        )
        return self._nonexecuting(
            request_id, text, "ANSWERED", "MONEY_REQUEST_SENT", trace,
            extra={
                "answer": (
                    f"Asked {beneficiary['display_name']} to pay you "
                    f"{_money_label(parsed.amount_minor)}. Nothing left your account."
                ),
                "money_request": {
                    "money_request_id": money_request_id,
                    "recipient_display_name": beneficiary["display_name"],
                    "masked_destination": beneficiary["masked_destination"],
                    "amount_minor": parsed.amount_minor,
                },
            },
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

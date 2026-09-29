"""Deterministic guarded-autopilot orchestration for Dimon Live."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import input_firewall, no_approval, scam_guard
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


class ActionService:
    def __init__(self, database: Database, extractor: Extractor):
        self.database = database
        self.extractor = extractor
        self.paynow_directory = SyntheticPayNowDirectory(database)

    def process(
        self,
        request_id: str,
        text: str,
        *,
        source: str = "typed",
        voice: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._with_validation(self._process(request_id, text))
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
                if choice.get("beneficiary_id"):
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

    def _process(self, request_id: str, text: str) -> dict[str, Any]:
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
        if no_approval.is_balance_inquiry(text) or no_approval.looks_like_money_request(text):
            return self._answer_without_approval(request_id, text, trace)
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
        model_trace = {
            "stage": "MODEL_RESPONSE_RECEIVED",
            "latency_ms": extracted.latency_ms,
        }
        if extracted.fixture_case_id is not None:
            model_trace["fixture_case_id"] = extracted.fixture_case_id
        trace.append(model_trace)
        trace.append({"stage": "INTENT_VALIDATED"})

        recovered_batch = self._recover_exact_saved_batch(text, intent)
        if recovered_batch is not None:
            if intent.get("payment_reference"):
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
                {"stage": "FACE_MATCH_VERIFIED"},
                {"stage": "DEVICE_SIGNATURE_VERIFIED"},
                {"stage": "RECEIPT_REVALIDATED"},
                {"stage": "SQLITE_COMMITTED"},
            ],
            "state": self.database.get_state(),
        }

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
                {"stage": "FACE_MATCH_VERIFIED"},
                {"stage": "DEVICE_SIGNATURE_VERIFIED"},
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

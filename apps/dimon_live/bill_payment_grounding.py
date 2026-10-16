"""Ground untrusted bill-payment proposals in the user's own transcript."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal, Mapping

from .bill_payment_directory import Biller, resolve_biller
from .bill_payment_contract import (
    BillPaymentContractError,
    BillPaymentProposal,
    parse_bill_payment_proposal,
)
from .grounding import (
    GroundingError,
    derive_amount_from_text,
    derive_amount_mentions_from_text,
    is_grounded,
    normalize_evidence,
)


class BillPaymentGroundingError(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


PreparationStatus = Literal[
    "READY",
    "BILLER_REQUIRED",
    "AMOUNT_REQUIRED",
    "CURRENCY_REQUIRED",
    "BILLER_AMBIGUOUS",
    "BILLER_UNSUPPORTED",
    "NOT_BILL_PAYMENT",
]


@dataclass(frozen=True)
class BillPaymentPreparation:
    status: PreparationStatus
    proposal: BillPaymentProposal | None = None
    amount_minor: int | None = None
    question: str | None = None
    resolved_biller: Biller | None = None
    biller_candidates: tuple[Biller, ...] = ()
    biller_evidence: str | None = None
    source_account_label: str | None = None
    amount_source: str | None = None


_QUESTIONS: dict[PreparationStatus, str] = {
    "BILLER_REQUIRED": "Which bill would you like to pay?",
    "AMOUNT_REQUIRED": "How much should I pay?",
    "CURRENCY_REQUIRED": "Which currency should I use?",
}

_BILL_ACTION = re.compile(r"\b(?:pay|settle)\b", re.IGNORECASE)
_BILL_TARGET = re.compile(
    r"\b(?:bills?|electricity|utility|utilities|phone|mobile|internet|"
    r"insurance|premium|credit\s+card|tax|tuition)\b",
    re.IGNORECASE,
)


def looks_like_bill_payment_request(text: str) -> bool:
    return bool(_BILL_ACTION.search(text) and _BILL_TARGET.search(text))


def _clarification(status: PreparationStatus) -> BillPaymentPreparation:
    return BillPaymentPreparation(status=status, question=_QUESTIONS[status])


def _grounded_pair(text: str, label: str, evidence: str, reason: str) -> None:
    if not is_grounded(text, evidence) or not is_grounded(evidence, label):
        raise BillPaymentGroundingError(reason)


def _grounded_biller_pair(text: str, label: str, evidence: str, reason: str) -> None:
    """Ground a model's canonical biller name through its registered alias.

    The user may say "water bill" while the model returns the directory's
    canonical name "PUB Water". Accept that only when the model's evidence is
    present in the user's text and matches an alias of the exact resolved
    directory entry.
    """

    if not is_grounded(text, evidence):
        raise BillPaymentGroundingError(reason)
    resolution = resolve_biller(label)
    if resolution.status != "MATCH" or resolution.biller is None:
        if not is_grounded(evidence, label):
            raise BillPaymentGroundingError(reason)
        return
    if is_grounded(evidence, label) or any(
        is_grounded(evidence, alias) for alias in resolution.biller.aliases
    ):
        return
    raise BillPaymentGroundingError(reason)


def prepare_bill_payment(
    original_text: str,
    raw_proposal: Mapping[str, Any],
    *,
    allow_multiple_amounts: bool = False,
) -> BillPaymentPreparation:
    """Return a clarification or a complete, transcript-grounded proposal.

    This function creates no receipt and holds no approval or execution
    authority. Explicit amounts come from ``original_text``; a known demo
    statement total is used only for a grounded biller with no explicit amount.
    """

    if raw_proposal.get("kind") == "NOT_BILL_PAYMENT":
        return BillPaymentPreparation(status="NOT_BILL_PAYMENT")

    if raw_proposal.get("biller_label") is None or raw_proposal.get("biller_evidence") is None:
        return _clarification("BILLER_REQUIRED")
    if raw_proposal.get("amount_evidence") is None:
        label = raw_proposal.get("biller_label")
        evidence = raw_proposal.get("biller_evidence")
        if (
            isinstance(label, str)
            and isinstance(evidence, str)
            and is_grounded(original_text, evidence)
            and raw_proposal.get("requested_execution_date") is None
            and raw_proposal.get("currency") in {None, "SGD"}
        ):
            try:
                _grounded_biller_pair(
                    original_text, label, evidence, "UNGROUNDED_BILLER"
                )
            except BillPaymentGroundingError:
                return _clarification("AMOUNT_REQUIRED")
            resolution = resolve_biller(label)
            biller = resolution.biller
            if (
                resolution.status == "MATCH"
                and biller is not None
                and biller.statement_amount_minor
            ):
                source_label = raw_proposal.get("source_account_label")
                source_evidence = raw_proposal.get("source_account_evidence")
                if source_label is not None:
                    if not isinstance(source_label, str) or not isinstance(source_evidence, str):
                        raise BillPaymentGroundingError("UNGROUNDED_SOURCE_ACCOUNT")
                    _grounded_pair(
                        original_text,
                        source_label,
                        source_evidence,
                        "UNGROUNDED_SOURCE_ACCOUNT",
                    )
                return BillPaymentPreparation(
                    status="READY",
                    amount_minor=biller.statement_amount_minor,
                    resolved_biller=biller,
                    biller_evidence=evidence,
                    source_account_label=source_label,
                    amount_source="SYNTHETIC_STATEMENT",
                )
        return _clarification("AMOUNT_REQUIRED")
    if raw_proposal.get("currency") is None or raw_proposal.get("currency_evidence") is None:
        return _clarification("CURRENCY_REQUIRED")

    grounded_proposal = dict(raw_proposal)
    currency_evidence = grounded_proposal.get("currency_evidence")
    amount_evidence = grounded_proposal.get("amount_evidence")
    if (
        isinstance(currency_evidence, str)
        and not is_grounded(original_text, currency_evidence)
        and isinstance(amount_evidence, str)
        and is_grounded(original_text, amount_evidence)
    ):
        currency_marker = re.search(r"(?i)s\$|\$", amount_evidence)
        if currency_marker is not None:
            grounded_proposal["currency_evidence"] = currency_marker.group(0)

    try:
        proposal = parse_bill_payment_proposal(grounded_proposal)
    except BillPaymentContractError as exc:
        raise BillPaymentGroundingError("INVALID_BILL_PAYMENT_PROPOSAL") from exc

    _grounded_biller_pair(
        original_text,
        proposal.biller_label,
        proposal.biller_evidence,
        "UNGROUNDED_BILLER",
    )

    if not is_grounded(original_text, proposal.amount_evidence):
        raise BillPaymentGroundingError("UNGROUNDED_AMOUNT")
    if not allow_multiple_amounts:
        try:
            amount_mentions = derive_amount_mentions_from_text(original_text)
        except GroundingError as exc:
            raise BillPaymentGroundingError(exc.reason_code) from exc
        if (
            len(amount_mentions) != 1
            or normalize_evidence(amount_mentions[0].evidence)
            != normalize_evidence(proposal.amount_evidence)
        ):
            raise BillPaymentGroundingError("AMBIGUOUS_AMOUNT")
    try:
        # A mixed bill + transfer request can contain one amount for each
        # action. Ground the bill amount from the bill model's exact evidence
        # instead of requiring the full transcript to contain only one amount.
        amount_minor, amount_evidence = derive_amount_from_text(
            proposal.amount_evidence
        )
    except GroundingError as exc:
        raise BillPaymentGroundingError(exc.reason_code) from exc
    if amount_minor is None or normalize_evidence(amount_evidence or "") != normalize_evidence(
        proposal.amount_evidence
    ):
        raise BillPaymentGroundingError("UNGROUNDED_AMOUNT")

    normalized_currency_evidence = normalize_evidence(proposal.currency_evidence)
    normalized_amount_evidence = normalize_evidence(proposal.amount_evidence)
    currency_is_grounded = (
        normalized_currency_evidence in {"$", "s$"}
        and normalized_currency_evidence in normalized_amount_evidence
    ) or is_grounded(original_text, proposal.currency_evidence)
    if not currency_is_grounded:
        raise BillPaymentGroundingError("UNGROUNDED_CURRENCY")

    if proposal.source_account_label is not None:
        _grounded_pair(
            original_text,
            proposal.source_account_label,
            proposal.source_account_evidence or "",
            "UNGROUNDED_SOURCE_ACCOUNT",
        )

    if proposal.requested_execution_date is not None:
        evidence = proposal.execution_date_evidence or ""
        if not is_grounded(original_text, evidence) or normalize_evidence(
            evidence
        ) != normalize_evidence(proposal.requested_execution_date):
            raise BillPaymentGroundingError("UNGROUNDED_EXECUTION_DATE")

    resolution = resolve_biller(proposal.biller_label)
    if resolution.status == "AMBIGUOUS":
        names = ", ".join(biller.display_name for biller in resolution.candidates)
        return BillPaymentPreparation(
            status="BILLER_AMBIGUOUS",
            proposal=proposal,
            amount_minor=amount_minor,
            question=f"I found more than one supported biller ({names}). Which one did you mean?",
            biller_candidates=resolution.candidates,
        )
    if resolution.status == "UNSUPPORTED":
        return BillPaymentPreparation(
            status="BILLER_UNSUPPORTED",
            proposal=proposal,
            amount_minor=amount_minor,
            question=(
                f"I couldn't match {proposal.biller_label} to a supported demo biller. "
                "Which biller should I use?"
            ),
        )

    return BillPaymentPreparation(
        status="READY",
        proposal=proposal,
        amount_minor=amount_minor,
        resolved_biller=resolution.biller,
    )

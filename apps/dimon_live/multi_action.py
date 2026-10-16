"""Validation and pure grounding helpers for bounded multi-action proposals."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .model_adapter import InvalidModelOutput


SUPPORTED_ACTIONS = frozenset({
    "BILL_PAYMENT",
    "BENEFICIARY_TRANSFER",
    "STOCK_INVESTMENT",
})

_MULTI_ACTION_CUES = (
    re.compile(r"\b(?:bill|biller|utilities|water|electricity|phone bill)\b", re.I),
    re.compile(r"\b(?:send|transfer)\b.*\b(?:dollars?|\$|S\$|SGD)\b", re.I),
    re.compile(r"\b(?:buy|purchase|invest in|shares? of|stocks? of)\b", re.I),
)


def looks_like_multi_action_request(text: str) -> bool:
    """Cheap gate for invoking Qwen; this does not classify or authorize actions."""
    if sum(bool(pattern.search(text)) for pattern in _MULTI_ACTION_CUES) >= 2:
        return True
    action_verbs = re.findall(r"\b(?:pay|send|transfer|buy|purchase)\b", text, re.I)
    return len(action_verbs) >= 2 and bool(re.search(r"[,;]|\band\b|\balso\b", text, re.I))


def validate_multi_action_intent(raw_output: str, *, text: str) -> dict[str, Any]:
    """Validate ordered action spans; all executable values come from domain parsers."""
    try:
        intent = json.loads(raw_output)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidModelOutput("model output is not valid JSON") from exc
    if not isinstance(intent, dict) or set(intent) != {"contract_version", "kind", "items"}:
        raise InvalidModelOutput("multi-action fields are invalid")
    if isinstance(intent["contract_version"], bool) or intent["contract_version"] != 1:
        raise InvalidModelOutput("contract_version must be integer 1")
    if intent["kind"] != "MULTI_ACTION":
        raise InvalidModelOutput("unsupported multi-action kind")
    items = intent["items"]
    if not isinstance(items, list) or not 2 <= len(items) <= 3:
        raise InvalidModelOutput("items must contain two or three actions")
    offsets: list[tuple[int, int]] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"kind", "evidence"}:
            raise InvalidModelOutput("multi-action item fields are invalid")
        if item["kind"] not in SUPPORTED_ACTIONS:
            raise InvalidModelOutput("unsupported multi-action item kind")
        evidence = item["evidence"]
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 500:
            raise InvalidModelOutput("action evidence must be a bounded non-empty string")
        start = text.find(evidence)
        if start < 0:
            raise InvalidModelOutput("action evidence is not present in the request")
        end = start + len(evidence)
        if offsets and start < offsets[-1][1]:
            raise InvalidModelOutput("actions must preserve transcript order")
        offsets.append((start, end))
    return intent


@dataclass(frozen=True)
class PreparedAction:
    """Immutable deterministic draft; no payment or trade has been approved."""

    kind: str
    evidence: str
    intent: Mapping[str, Any]


@dataclass(frozen=True)
class UnresolvedAction:
    """A bounded missing detail; never an executable action proposal."""

    field: str
    question: str
    evidence: str
    suggested_value: str | None = None


@dataclass(frozen=True)
class MultiActionPreparation:
    items: tuple[PreparedAction, ...] = ()
    resolved_items: tuple[PreparedAction, ...] = ()
    issues: tuple[UnresolvedAction, ...] = ()
    reason_code: str | None = None
    clarification: str | None = None


def _combined_clarification(issues: list[UnresolvedAction]) -> str:
    questions = [issue.question.strip().rstrip("?.") for issue in issues]
    if not questions:
        return "I couldn’t verify every requested action. Nothing has been prepared."
    if len(questions) == 1:
        combined = questions[0]
    else:
        combined = ", and ".join([questions[0], *[item[:1].lower() + item[1:] for item in questions[1:]]])
    return f"{combined}? Nothing has been prepared."


def prepare_multi_action(
    text: str,
    intent: Mapping[str, Any],
    *,
    bill_preparer: Callable[[str], Any],
    transfer_preparer: Callable[[str], Any],
    stock_preparer: Callable[[str], Any],
) -> MultiActionPreparation:
    """Prepare every item from its own exact transcript span, failing as a unit."""
    try:
        validated = validate_multi_action_intent(
            json.dumps(dict(intent), ensure_ascii=False), text=text
        )
    except (TypeError, ValueError):
        return MultiActionPreparation(
            reason_code="MULTI_ACTION_EVIDENCE_INVALID",
            clarification="I couldn’t safely match every part of that request. Please rephrase it.",
        )

    preparers: dict[str, Callable[[str], Any]] = {
        "BILL_PAYMENT": bill_preparer,
        "BENEFICIARY_TRANSFER": transfer_preparer,
        "PAYNOW_MOBILE_TRANSFER": transfer_preparer,
        "STOCK_INVESTMENT": stock_preparer,
    }
    prepared: list[PreparedAction] = []
    issues: list[UnresolvedAction] = []
    unresolved_by_kind = {
        "BENEFICIARY_TRANSFER": (
            "recipient", "Who should receive the transfer?"
        ),
        "STOCK_INVESTMENT": (
            "stock_request", "Which stock and how many shares would you like to buy?"
        ),
        "BILL_PAYMENT": (
            "bill_request", "Which bill would you like to pay?"
        ),
    }
    for item in validated["items"]:
        try:
            proposal = preparers[item["kind"]](item["evidence"])
        except Exception:
            proposal = None
        if proposal is None:
            field, question = unresolved_by_kind[item["kind"]]
            issues.append(UnresolvedAction(field, question, item["evidence"]))
            continue
        if isinstance(proposal, UnresolvedAction):
            issues.append(proposal)
            continue
        if hasattr(proposal, "intent"):
            proposal = proposal.intent
        if not isinstance(proposal, Mapping):
            field, question = unresolved_by_kind[item["kind"]]
            issues.append(UnresolvedAction(
                field, question, item["evidence"]
            ))
            continue
        prepared.append(PreparedAction(item["kind"], item["evidence"], dict(proposal)))
    if issues:
        return MultiActionPreparation(
            resolved_items=tuple(prepared),
            issues=tuple(issues),
            reason_code="MULTI_ACTION_ITEM_UNRESOLVED",
            clarification=_combined_clarification(issues),
        )
    return MultiActionPreparation(tuple(prepared))

"""Throwaway harness for the local structured-action feasibility spike."""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO
from urllib.request import Request, urlopen


TRANSFER_INTENT_FIELDS = {
    "kind",
    "source_account_ref",
    "recipient_label",
    "amount_minor",
    "currency",
    "requested_execution_date",
}
EXPERIMENT_DIR = Path(__file__).resolve().parent
TRANSFER_INTENT_SCHEMA = json.loads(
    (EXPERIMENT_DIR / "transfer_draft.schema.json").read_text(encoding="utf-8")
)

SYSTEM_PROMPT = """You extract one beneficiary-transfer intent from a user request.
Return only the JSON object required by the response schema.
Amounts are integer minor units: S$50 is 5000.
Use the user's account label and saved-beneficiary label verbatim, without inventing IDs.
If the user does not name a source account, set source_account_ref to null. Never guess it.
Never emit Python, shell commands, tool names, approval claims, or prose.
This output is an untrusted proposal for server review and never executes a transfer."""

GOLDEN_REQUEST = "Send John Smith S$50 from my savings account."
GOLDEN_INTENT = {
    "kind": "BENEFICIARY_TRANSFER",
    "source_account_ref": "savings",
    "recipient_label": "John Smith",
    "amount_minor": 5000,
    "currency": "SGD",
    "requested_execution_date": None,
}


def build_chat_request(user_request: str, model: str) -> dict[str, Any]:
    return {
        "model": model,
        "temperature": 0,
        "max_tokens": 256,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_request},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "raw_beneficiary_transfer_intent",
                "strict": True,
                "schema": TRANSFER_INTENT_SCHEMA,
            },
        },
    }


def validate_transfer_draft_json(raw_output: str) -> dict[str, Any]:
    intent = json.loads(raw_output)
    if not isinstance(intent, dict):
        raise ValueError("model output must be a JSON object")
    missing_fields = TRANSFER_INTENT_FIELDS - set(intent)
    if missing_fields:
        raise ValueError(f"missing fields: {sorted(missing_fields)}")
    unexpected_fields = set(intent) - TRANSFER_INTENT_FIELDS
    if unexpected_fields:
        raise ValueError(f"unexpected fields: {sorted(unexpected_fields)}")
    amount_minor = intent.get("amount_minor")
    if type(amount_minor) is not int or amount_minor <= 0:
        raise ValueError("amount_minor must be a positive integer")
    if intent["kind"] != "BENEFICIARY_TRANSFER":
        raise ValueError("kind must be BENEFICIARY_TRANSFER")
    source_ref = intent["source_account_ref"]
    if source_ref is not None and (
        not isinstance(source_ref, str) or not source_ref.strip()
    ):
        raise ValueError("source_account_ref must be null or a non-empty string")
    recipient = intent["recipient_label"]
    if not isinstance(recipient, str) or not recipient.strip():
        raise ValueError("recipient_label must be a non-empty string")
    if intent["currency"] != "SGD":
        raise ValueError("currency must be SGD")
    if intent["requested_execution_date"] is not None:
        raise ValueError("requested_execution_date must be null")
    return intent


def create_reviewable_draft(
    intent: Mapping[str, Any],
    synthetic_state: Mapping[str, Any],
    *,
    action_id: str,
    source_evidence: str,
) -> dict[str, Any]:
    account = synthetic_state["accounts"].get(intent["source_account_ref"])
    if account is None:
        raise ValueError("source account could not be resolved")
    beneficiary = synthetic_state["beneficiaries"].get(intent["recipient_label"])
    if beneficiary is None:
        raise ValueError("recipient could not be resolved")
    if intent["amount_minor"] > account["balance_minor"]:
        raise ValueError("amount exceeds available balance")
    return {
        "action_id": action_id,
        "kind": intent["kind"],
        "source_account_id": account["account_id"],
        "destination": {
            "beneficiary_id": beneficiary["beneficiary_id"],
            "display_name": intent["recipient_label"],
            "masked_destination": beneficiary["masked_destination"],
        },
        "amount_minor": intent["amount_minor"],
        "currency": intent["currency"],
        "requested_execution_date": intent["requested_execution_date"],
        "status": "READY_FOR_REVIEW",
        "ledger_effect": "NONE",
        "source_evidence": source_evidence,
    }


def resolve_transfer_intent(
    intent: Mapping[str, Any],
    synthetic_state: Mapping[str, Any],
    *,
    action_id: str,
    source_evidence: str,
) -> dict[str, Any]:
    """Resolve an untrusted intent without allowing the model to choose IDs."""
    requested_label = intent["recipient_label"].strip()
    normalized_label = requested_label.casefold()
    beneficiary_names = synthetic_state["beneficiaries"].keys()

    exact_matches = sorted(
        (name for name in beneficiary_names if name.casefold() == normalized_label),
        key=str.casefold,
    )
    if exact_matches:
        candidate_names = exact_matches
    else:
        candidate_names = sorted(
            (
                name
                for name in beneficiary_names
                if name.casefold().startswith(normalized_label)
            ),
            key=str.casefold,
        )

    if len(candidate_names) > 1:
        return {
            "outcome": "CLARIFICATION_REQUEST",
            "field": "recipient_label",
            "question": f"Which saved {requested_label} do you mean?",
            "candidate_display_names": candidate_names,
            "ledger_effect": "NONE",
        }
    if not candidate_names:
        raise ValueError("recipient could not be resolved")

    canonical_intent = dict(intent)
    canonical_intent["recipient_label"] = candidate_names[0]
    draft = create_reviewable_draft(
        canonical_intent,
        synthetic_state,
        action_id=action_id,
        source_evidence=source_evidence,
    )
    return {
        "outcome": "ACTION_DRAFT",
        "draft": draft,
        "ledger_effect": "NONE",
    }


def post_json(url: str, payload: Mapping[str, Any], timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def run_probe(
    user_request: str,
    *,
    api_base: str,
    model: str,
    synthetic_state: Mapping[str, Any],
    action_id: str,
    transport: Callable[[str, Mapping[str, Any], float], dict[str, Any]] = post_json,
    timeout: float = 120,
) -> dict[str, Any]:
    payload = build_chat_request(user_request, model)
    started = time.perf_counter()
    response = transport(
        f"{api_base.rstrip('/')}/chat/completions",
        payload,
        timeout,
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    choice = response["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError(
            f"model finish_reason must be stop, got {choice.get('finish_reason')!r}"
        )
    raw_model_output = choice["message"]["content"]
    intent = validate_transfer_draft_json(raw_model_output)
    server_outcome = resolve_transfer_intent(
        intent,
        synthetic_state,
        action_id=action_id,
        source_evidence=user_request,
    )
    report = {
        "user_request": user_request,
        "latency_ms": latency_ms,
        "raw_model_output": raw_model_output,
        "validated_intent": intent,
        "golden_fields_match": intent == GOLDEN_INTENT,
        "server_outcome": server_outcome,
    }
    if server_outcome["outcome"] == "ACTION_DRAFT":
        report["reviewable_draft"] = server_outcome["draft"]
    return report


def evaluate_expected_draft(
    report: Mapping[str, Any],
    expected_intent: Mapping[str, Any],
) -> dict[str, bool]:
    """Score a probe without treating schema validity as proof of safety."""
    server_outcome = report.get("server_outcome")
    outcome_is_reviewable_draft = (
        isinstance(server_outcome, Mapping)
        and server_outcome.get("outcome") == "ACTION_DRAFT"
        and isinstance(server_outcome.get("draft"), Mapping)
    )
    draft = server_outcome["draft"] if outcome_is_reviewable_draft else {}
    checks = {
        "intent_matches_expected": report.get("validated_intent")
        == dict(expected_intent),
        "outcome_is_reviewable_draft": outcome_is_reviewable_draft,
        "draft_status_is_ready_for_review": draft.get("status")
        == "READY_FOR_REVIEW",
        "ledger_effect_is_none": (
            server_outcome.get("ledger_effect") == "NONE"
            and draft.get("ledger_effect") == "NONE"
        )
        if isinstance(server_outcome, Mapping)
        else False,
    }
    return {**checks, "passed": all(checks.values())}


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Callable[[str, Mapping[str, Any], float], dict[str, Any]] = post_json,
    stdout: TextIO = sys.stdout,
) -> int:
    parser = argparse.ArgumentParser(
        description="Run the throwaway local structured-action spike.",
    )
    parser.add_argument("--api-base", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--model", default="qwen35-9b-abliterated-q5")
    parser.add_argument("--request", default=GOLDEN_REQUEST)
    parser.add_argument("--action-id", default=f"action-{uuid.uuid4()}")
    parser.add_argument(
        "--state",
        type=Path,
        default=EXPERIMENT_DIR / "synthetic_state.json",
    )
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    synthetic_state = json.loads(args.state.read_text(encoding="utf-8"))
    report = run_probe(
        args.request,
        api_base=args.api_base,
        model=args.model,
        synthetic_state=synthetic_state,
        action_id=args.action_id,
        transport=transport,
        timeout=args.timeout,
    )
    serialized = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output is not None:
        args.output.write_text(f"{serialized}\n", encoding="utf-8")
    print(serialized, file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

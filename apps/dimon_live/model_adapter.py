"""Bounded local-Qwen adapter for beneficiary-transfer extraction."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen


class InvalidModelOutput(ValueError):
    """The model responded, but not with a valid bounded intent."""


class ModelUnavailable(RuntimeError):
    """The local model endpoint could not produce a response."""


@dataclass(frozen=True)
class ExtractedIntent:
    intent: dict[str, Any]
    raw_output: str
    latency_ms: float


class Extractor(Protocol):
    def extract(self, text: str) -> ExtractedIntent: ...


Transport = Callable[[str, Mapping[str, Any], float], dict[str, Any]]

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = Path(__file__).with_name("action_intent.schema.json")
TRANSFER_INTENT_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
COMMON_FIELDS = {
    "contract_version",
    "kind",
    "source_account_ref",
    "source_evidence",
    "currency",
    "payment_reference",
    "reference_evidence",
    "requested_execution_date",
}
SAVED_FIELDS = COMMON_FIELDS | {"recipient_label", "recipient_evidence"}
PAYNOW_FIELDS = COMMON_FIELDS | {"mobile_proxy", "proxy_evidence"}
BATCH_FIELDS = {
    "contract_version",
    "kind",
    "source_account_ref",
    "source_evidence",
    "items",
    "currency",
    "requested_execution_date",
}

SYSTEM_PROMPT = """You extract one transfer proposal or one saved-beneficiary transfer batch.
Return only the JSON object required by the response schema.
Choose BENEFICIARY_TRANSFER for a saved name or PAYNOW_MOBILE_TRANSFER for a mobile PayNow proxy.
Use BENEFICIARY_TRANSFER_BATCH only when the user names two to four distinct saved recipients with one explicit amount expression for each. For batch items, copy only recipient_label and recipient_evidence; never return amounts. Preserve recipient order from the user's text.
Copy every evidence value as an exact substring of the user's text.
Do not calculate, copy, or return an amount. Deterministic server code derives money directly from the original user text.
Use null for an absent value and its evidence. Never infer or default a source, recipient, proxy, or reference.
SOURCE RULE: source_account_ref and source_evidence are non-null only when the user explicitly names a funding account in the request. Otherwise both must be null. Never output current, current account, default account, a PayNow proxy, or a recipient as the source.
REFERENCE RULE: payment_reference and reference_evidence are non-null only for an explicit payment purpose or message, such as words introduced by "for" or "reference". A recipient plus amount is not a payment reference.
PAYNOW RULE: a mobile number belongs only in mobile_proxy and proxy_evidence. It is never a source account.
Never output a masked recipient name, canonical account ID, beneficiary ID, receipt, approval, Python, shell command, tool name, or prose.
This output is an untrusted proposal; deterministic server code decides whether a synthetic demo transfer is eligible."""


def _valid_optional_string(value: Any) -> bool:
    return value is None or (isinstance(value, str) and bool(value.strip()))


def _require_paired(
    intent: Mapping[str, Any],
    value_key: str,
    evidence_key: str,
) -> None:
    value = intent[value_key]
    evidence = intent[evidence_key]
    if (value is None) != (evidence is None):
        raise InvalidModelOutput(f"{value_key} and {evidence_key} must be paired")
    if not _valid_optional_string(evidence):
        raise InvalidModelOutput(f"{evidence_key} must be null or a non-empty string")


def post_json(url: str, payload: Mapping[str, Any], timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def validate_intent(raw_output: str) -> dict[str, Any]:
    try:
        intent = json.loads(raw_output)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidModelOutput("model output is not valid JSON") from exc
    if not isinstance(intent, dict):
        raise InvalidModelOutput("model output must be a JSON object")
    kind = intent.get("kind")
    if kind == "BENEFICIARY_TRANSFER":
        expected_fields = SAVED_FIELDS
    elif kind == "PAYNOW_MOBILE_TRANSFER":
        expected_fields = PAYNOW_FIELDS
    elif kind == "BENEFICIARY_TRANSFER_BATCH":
        expected_fields = BATCH_FIELDS
    else:
        raise InvalidModelOutput("unsupported intent kind")
    missing_fields = expected_fields - set(intent)
    if missing_fields:
        raise InvalidModelOutput(f"missing fields: {sorted(missing_fields)}")
    unexpected_fields = set(intent) - expected_fields
    if unexpected_fields:
        raise InvalidModelOutput(f"unexpected fields: {sorted(unexpected_fields)}")
    expected_version = 4 if kind == "BENEFICIARY_TRANSFER_BATCH" else 3
    if intent["contract_version"] != expected_version:
        raise InvalidModelOutput(f"contract_version must be {expected_version}")
    _require_paired(intent, "source_account_ref", "source_evidence")
    if not _valid_optional_string(intent["source_account_ref"]):
        raise InvalidModelOutput("source_account_ref must be null or a non-empty string")
    if kind != "BENEFICIARY_TRANSFER_BATCH":
        _require_paired(intent, "payment_reference", "reference_evidence")
        if not _valid_optional_string(intent["payment_reference"]):
            raise InvalidModelOutput("payment_reference must be null or a non-empty string")
    if kind == "BENEFICIARY_TRANSFER":
        _require_paired(intent, "recipient_label", "recipient_evidence")
        if not _valid_optional_string(intent["recipient_label"]):
            raise InvalidModelOutput("recipient_label must be null or a non-empty string")
    elif kind == "PAYNOW_MOBILE_TRANSFER":
        _require_paired(intent, "mobile_proxy", "proxy_evidence")
        if not _valid_optional_string(intent["mobile_proxy"]):
            raise InvalidModelOutput("mobile_proxy must be null or a non-empty string")
    else:
        items = intent["items"]
        if not isinstance(items, list) or not 2 <= len(items) <= 4:
            raise InvalidModelOutput("items must contain two to four entries")
        for item in items:
            if not isinstance(item, dict):
                raise InvalidModelOutput("batch items must be objects")
            if set(item) != {"recipient_label", "recipient_evidence"}:
                raise InvalidModelOutput("batch item fields are invalid")
            _require_paired(item, "recipient_label", "recipient_evidence")
            if not isinstance(item["recipient_label"], str) or not item["recipient_label"].strip():
                raise InvalidModelOutput("recipient_label must be a non-empty string")
    if intent["currency"] != "SGD":
        raise InvalidModelOutput("currency must be SGD")
    if intent["requested_execution_date"] is not None:
        raise InvalidModelOutput("requested_execution_date must be null")
    return intent


class QwenTransferExtractor:
    def __init__(
        self,
        *,
        api_base: str = "http://127.0.0.1:8080/v1",
        model: str = "qwen35-9b-abliterated-q5",
        timeout: float = 120,
        transport: Transport = post_json,
    ):
        self.api_base = api_base.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.transport = transport

    def extract(self, text: str) -> ExtractedIntent:
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 512,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
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
        started = time.perf_counter()
        try:
            response = self.transport(
                f"{self.api_base}/chat/completions",
                payload,
                self.timeout,
            )
        except (TimeoutError, URLError, OSError) as exc:
            raise ModelUnavailable(f"MODEL_UNAVAILABLE: {exc}") from exc
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            choice = response["choices"][0]
            finish_reason = choice.get("finish_reason")
            raw_output = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise InvalidModelOutput("model response envelope is invalid") from exc
        if finish_reason != "stop":
            raise InvalidModelOutput(
                f"model finish_reason must be stop, got {finish_reason!r}"
            )
        intent = validate_intent(raw_output)
        return ExtractedIntent(intent, raw_output, latency_ms)

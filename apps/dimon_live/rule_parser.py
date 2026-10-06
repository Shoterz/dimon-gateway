"""Deterministic parser for plain one-recipient transfers.

"send 2 dollars to alicia on my second account", "pay John Smith S$50 from
savings for lunch". When a request has exactly this shape, the proposal is
built here instead of by the language model: faster, identical every time, and
available when the model is down. When the shape is anything else the parser
returns None and the model is asked, exactly as before.

The output is only a proposal in the same contract the model produces, with
every value copied from the user's text. It goes through the same grounding,
directory resolution, policy, receipt, and independent validator as model
output, so the parser cannot grant anything the model could not.
"""

from __future__ import annotations

import json
import re

from .grounding import GroundingError, derive_amount_mentions_from_text
from .model_adapter import ExtractedIntent, validate_intent

_VERB = r"(?:(?:please|pls|plz|kindly|can\s+you|could\s+you)\s+)?(?:send|pay|transfer|give)"
_ACCOUNT = r"(?:(?:my|the)\s+)?(?:savings|everyday|first|second|primary|main|1st|2nd)(?:\s+(?:account|acc|acct|a/c))?"


def _source(group: str) -> str:
    return rf"(?:(?:from|on|using|with|via|out\s+of)\s+(?P<{group}>{_ACCOUNT}))"


_NAME = r"(?P<name>[A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*){0,2}?)"
_REFERENCE = r"(?:(?:for|reference|ref|memo)\s+(?P<reference>[A-Za-z0-9][\w '\-]{0,40}?))"
_TAIL = rf"(?:\s+{_source('source')})?(?:\s+{_REFERENCE})?(?:\s+{_source('late_source')})?"
_END = r"\s*[.!]?\s*"

# The amount is replaced by a placeholder before matching, so these patterns
# only describe where the amount sits, never what it is.
_AMOUNT_FIRST = re.compile(rf"^{_VERB}\s+AMOUNT\s+to\s+{_NAME}{_TAIL}{_END}$", re.IGNORECASE)
_NAME_FIRST = re.compile(rf"^{_VERB}\s+{_NAME}\s+AMOUNT{_TAIL}{_END}$", re.IGNORECASE)

# Words that can never be a payee name in these shapes.
_NOT_A_NAME = frozenset({
    "me", "my", "him", "her", "them", "it", "money", "cash", "someone", "everyone",
    "paynow", "pay", "now", "savings", "everyday", "account", "the", "a", "an",
    "and", "to", "from", "on", "for", "dollars", "dollar", "bucks", "sgd",
})


def _strip_article(value: str) -> str:
    return re.sub(r"^(?:my|the)\s+", "", value.strip(), flags=re.IGNORECASE)


def parse(text: str) -> ExtractedIntent | None:
    if re.search(r"\bpay\s*now\b|\d{4}\s?\d{4}", text, re.IGNORECASE):
        return None   # PayNow numbers keep their own derivation path
    try:
        mentions = derive_amount_mentions_from_text(text)
    except GroundingError:
        return None
    if len(mentions) != 1:
        return None
    mention = mentions[0]
    shaped = f"{text[:mention.start]}AMOUNT{text[mention.end:]}"
    match = _AMOUNT_FIRST.match(shaped) or _NAME_FIRST.match(shaped)
    if match is None:
        return None
    name = match.group("name")
    words = name.split()
    if words[0].casefold() == "my" and len(words) > 1:
        words = words[1:]        # "my mum" is a saved alias
    if any(word.casefold() in _NOT_A_NAME for word in words):
        return None
    source = match.group("source") or match.group("late_source")
    reference = match.group("reference")
    intent = {
        "contract_version": 3,
        "kind": "BENEFICIARY_TRANSFER",
        "source_account_ref": _strip_article(source) if source else None,
        "source_evidence": source,
        "recipient_label": name,
        "recipient_evidence": name,
        "currency": "SGD",
        "payment_reference": reference.strip() if reference else None,
        "reference_evidence": reference.strip() if reference else None,
        "requested_execution_date": None,
    }
    raw_output = json.dumps(intent, separators=(",", ":"))
    return ExtractedIntent(validate_intent(raw_output), raw_output, 0.0)

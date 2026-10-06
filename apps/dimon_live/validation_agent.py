"""Independent, read-only validation agent (dual-engine discrepancy audit).

The drafting pipeline (model proposal -> grounding -> receipt) is one engine.
This is the second: it never sees model output or the drafting pipeline's
evidence spans. It re-reads the user's own words (every turn, including
clarification replies) with its own parser, looks names up in the
server-owned directory, and audits the compiled receipt field by field.

Verdicts per field:
  MATCH       the transcript states this value
  MISMATCH    the transcript states a different value -> the draft is FROZEN
  UNVERIFIED  the transcript does not settle it (e.g. a partial or phonetic
              name) -> ATTENTION: approval is allowed only after the overlay
              shows the user exactly what could not be confirmed

It has no write access to the ledger and no approval capability.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Sequence

VALIDATOR_VERSION = "dimon-validator-1"

_CURRENCY_CODES = ("usd", "eur", "gbp", "aud", "jpy", "rmb", "cny", "myr", "hkd", "inr")
_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1000}
_NUMBER_WORD = "|".join(sorted([*_UNITS, *_TENS, *_SCALES, "a", "and"], key=len, reverse=True))
_MONEY_NOUN = r"(?:singapore\s+)?(?:dollars?|bucks?|sgd)"
# Unmarked amounts count only right after a name-like word in a payment
# sentence ("pay alicia 10", "transfer my mum fifty"); this is the validator's
# own reading, written separately from the drafting pipeline's.
_PAY_VERB = re.compile(r"\b(send|pay|transfer|give|pass|bank in)\b")
_UNMARKED = re.compile(
    r"\b(?P<word>[a-z]+)\s+(?P<num>\d{1,6}(?:\.\d{1,2})?|(?:(?:" + "|".join([*_UNITS, *_TENS])
    + r")(?:[\s-]+(?:" + "|".join([*_UNITS, *_TENS, "hundred", "thousand", "and"]) + r"))*))"
    r"(?=\s+(?P<next>[a-z%]+)|\s*[.,!?]|\s*$)"
)
_NOT_NAMES = {
    "for", "of", "at", "by", "with", "the", "a", "an", "in", "on", "to", "and", "or", "than",
    "about", "around", "only", "just", "me", "help", "can", "lah", "paynow", "number", "no",
    "block", "unit", "floor", "room", "table", "bus", "send", "pay", "transfer", "give", "pass",
}
_NOT_MONEY_NEXT = {"cent", "cents", "x", "times", "pax", "people", "persons", "percent", "%", "pieces", "pcs", "items"}
_DIGIT_MONEY = re.compile(
    r"(?:(?:s\$|\$|sgd)\s*(?P<pre>\d[\d,]*(?:\.\d{1,2})?)(?P<prek>k)?)"
    r"|(?:(?P<post>\d[\d,]*(?:\.\d{1,2})?)(?P<postk>k)?\s*" + _MONEY_NOUN + r")"
)
_WORD_MONEY = re.compile(
    r"(?<![a-z])(?P<words>(?:(?:" + _NUMBER_WORD + r")[\s-]+)+)" + _MONEY_NOUN
)
_GRAND = re.compile(r"(?<![a-z])(?:a|one)\s+grand(?![a-z])")


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _words_to_number(words: str) -> int | None:
    total = 0
    current = 0
    seen = False
    for token in re.split(r"[\s-]+", words.strip()):
        if not token or token == "and":
            continue
        if token == "a":
            current = max(current, 1)
            continue
        if token in _UNITS:
            current += _UNITS[token]
        elif token in _TENS:
            current += _TENS[token]
        elif token == "hundred":
            current = max(current, 1) * 100
        elif token == "thousand":
            total += max(current, 1) * 1000
            current = 0
        else:
            return None
        seen = True
    return total + current if seen else None


def transcript_amounts(text: str) -> set[int]:
    """Every SGD amount the user stated, in minor units, by an independent parser."""

    normalized = normalize(text)
    amounts: set[int] = set()
    for match in _DIGIT_MONEY.finditer(normalized):
        number = (match.group("pre") or match.group("post")).replace(",", "")
        scale = 1000 if (match.group("prek") or match.group("postk")) else 1
        whole, _, cents = number.partition(".")
        amounts.add((int(whole) * 100 + int((cents + "00")[:2])) * scale)
    for match in _WORD_MONEY.finditer(normalized):
        value = _words_to_number(match.group("words"))
        if value:
            amounts.add(value * 100)
    if _GRAND.search(normalized):
        amounts.add(100000)
    if not amounts and _PAY_VERB.search(normalized):
        for match in _UNMARKED.finditer(normalized):
            word, following = match.group("word"), match.group("next") or ""
            verb_first = _PAY_VERB.fullmatch(word) is not None and following == "to"
            if (word in _NOT_NAMES and not verb_first) or following in _NOT_MONEY_NEXT:
                continue
            number = match.group("num")
            if number[0].isdigit():
                whole, _, cents = number.partition(".")
                amounts.add(int(whole) * 100 + int((cents + "00")[:2]))
            else:
                value = _words_to_number(number)
                if value:
                    amounts.add(value * 100)
    return amounts


_ACCOUNT_POSITION = re.compile(r"(?<![\w])(first|1st|primary|main|second|2nd) (?:account|acc|acct|a/c)(?![\w])")


def _mentions(text: str, phrase: str) -> bool:
    phrase = normalize(phrase)
    return bool(phrase) and re.search(rf"(?<![\w]){re.escape(phrase)}(?![\w])", text) is not None


@dataclass(frozen=True)
class DirectoryEntry:
    beneficiary_id: str
    display_name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class DraftItem:
    beneficiary_id: str
    recipient_display_name: str
    amount_minor: int
    paynow_proxy: str | None = None


@dataclass(frozen=True)
class DraftUnderAudit:
    source_label: str
    currency: str
    items: tuple[DraftItem, ...]


@dataclass(frozen=True)
class FieldAudit:
    field: str
    verdict: str
    draft_value: str
    observed: str
    note: str


@dataclass(frozen=True)
class ValidationReport:
    verdict: str
    fields: tuple[FieldAudit, ...]
    transcript_sha256: str
    validator_version: str = VALIDATOR_VERSION

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "validator_version": self.validator_version,
            "transcript_sha256": self.transcript_sha256,
            "fields": [asdict(field) for field in self.fields],
        }


def _audit_recipient(
    text: str,
    index: int,
    item: DraftItem,
    directory: Sequence[DirectoryEntry],
) -> FieldAudit:
    field = f"items[{index}].recipient"
    if item.paynow_proxy:
        digits = re.sub(r"\D", "", text)
        proxy = re.sub(r"\D", "", item.paynow_proxy)
        if proxy and proxy in digits:
            return FieldAudit(field, "MATCH", item.paynow_proxy, item.paynow_proxy, "PayNow number stated by the user")
        return FieldAudit(field, "MISMATCH", item.paynow_proxy, "not stated", "PayNow number differs from the user's words")
    if _mentions(text, item.recipient_display_name):
        return FieldAudit(field, "MATCH", item.recipient_display_name, item.recipient_display_name, "full name stated")
    own = next((entry for entry in directory if entry.beneficiary_id == item.beneficiary_id), None)
    nickname = next((alias for alias in (own.aliases if own else ()) if _mentions(text, alias)), None)
    if nickname:
        return FieldAudit(field, "MATCH", item.recipient_display_name, nickname, "saved nickname stated")
    others = [
        entry.display_name
        for entry in directory
        if entry.beneficiary_id != item.beneficiary_id
        and (_mentions(text, entry.display_name) or any(_mentions(text, alias) for alias in entry.aliases))
    ]
    tokens = [token for token in normalize(item.recipient_display_name).split() if len(token) > 1]
    partial = [token for token in tokens if _mentions(text, token)]
    if others and not partial:
        return FieldAudit(field, "MISMATCH", item.recipient_display_name, ", ".join(others), "the user named a different saved payee")
    if partial:
        return FieldAudit(field, "UNVERIFIED", item.recipient_display_name, " ".join(partial), "only part of the name was stated; confirm the payee")
    return FieldAudit(field, "UNVERIFIED", item.recipient_display_name, "no exact name", "name inferred from a close or phonetic match; confirm the payee")


def audit_draft(
    utterances: Sequence[str],
    draft: DraftUnderAudit,
    *,
    directory: Sequence[DirectoryEntry],
    source_labels: Sequence[str],
) -> ValidationReport:
    transcript = "\n".join(utterances)
    text = normalize(transcript)
    fields: list[FieldAudit] = []

    stated_codes = [code for code in _CURRENCY_CODES if _mentions(text, code)]
    if draft.currency != "SGD" or stated_codes:
        fields.append(FieldAudit("currency", "MISMATCH", draft.currency, ", ".join(stated_codes) or "SGD", "currency differs from the user's words"))
    else:
        fields.append(FieldAudit("currency", "MATCH", "SGD", "SGD", "Singapore dollars"))

    named_sources = [label for label in source_labels if _mentions(text, label)]
    # "my second account" names an account by its place in source_labels,
    # which the store lists in the bank's own order (savings first).
    for match in _ACCOUNT_POSITION.finditer(text):
        position = 0 if match.group(1) in {"first", "1st", "primary", "main"} else 1
        if position < len(source_labels) and source_labels[position] not in named_sources:
            named_sources.append(source_labels[position])
    if _mentions(text, draft.source_label) or named_sources == [draft.source_label]:
        fields.append(FieldAudit("source_account", "MATCH", draft.source_label, draft.source_label, "source account stated"))
    elif named_sources:
        fields.append(FieldAudit("source_account", "MISMATCH", draft.source_label, ", ".join(named_sources), "the user named a different source account"))
    elif len(source_labels) == 1:
        fields.append(FieldAudit("source_account", "MATCH", draft.source_label, "not stated", "defaulted: the only eligible account"))
    else:
        fields.append(FieldAudit("source_account", "UNVERIFIED", draft.source_label, "not stated", "source account was not stated"))

    stated_amounts = transcript_amounts(transcript)
    for index, item in enumerate(draft.items):
        label = f"S${item.amount_minor / 100:,.2f}"
        if item.amount_minor in stated_amounts:
            fields.append(FieldAudit(f"items[{index}].amount", "MATCH", label, label, "amount stated"))
        elif stated_amounts:
            observed = ", ".join(f"S${value / 100:,.2f}" for value in sorted(stated_amounts))
            fields.append(FieldAudit(f"items[{index}].amount", "MISMATCH", label, observed, "amount differs from every amount the user stated"))
        else:
            fields.append(FieldAudit(f"items[{index}].amount", "UNVERIFIED", label, "no amount parsed", "the validator could not read an amount; check it"))
        fields.append(_audit_recipient(text, index, item, directory))

    verdicts = {field.verdict for field in fields}
    verdict = "FROZEN" if "MISMATCH" in verdicts else "ATTENTION" if "UNVERIFIED" in verdicts else "PASS"
    return ValidationReport(
        verdict=verdict,
        fields=tuple(fields),
        transcript_sha256=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
    )

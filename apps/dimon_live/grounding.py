"""Deterministic evidence grounding for execution-critical transfer fields."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class GroundingError(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


_SMALL_NUMBERS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_NUMBER_WORDS = set(_SMALL_NUMBERS) | set(_TENS) | {
    "and",
    "hundred",
    "thousand",
}
# Spoken cents after the dollars: "two dollars fifty", "2 dollars and 50
# cents". Only 1-99, so "ten dollars fifty times" stays out of reach.
_CENTS_NUMBER = (
    r"(?:\d{1,2}|(?:" + "|".join(sorted(_TENS, key=len, reverse=True)) + r")(?:[\s-]+(?:"
    + "|".join(k for k, v in sorted(_SMALL_NUMBERS.items(), key=lambda kv: -len(kv[0])) if 0 < v < 10)
    + r"))?|(?:" + "|".join(k for k, v in sorted(_SMALL_NUMBERS.items(), key=lambda kv: -len(kv[0])) if v > 0) + r"))"
)
_CENTS_TAIL = (
    r"(?:(?:\s+and)?\s+" + _CENTS_NUMBER
    + r"(?:\s+cents?|(?!\s+(?:times|x|pax|people|persons|pieces|pcs|items|percent)\b)))?"
)
_CENTS_ONLY = re.compile(r"(?<![\w$.])" + _CENTS_NUMBER + r"\s+cents?(?!\w)", re.IGNORECASE)
_NUMERIC_AMOUNT = re.compile(
    r"^(?P<prefix>(?:s\s*)?\$)?\s*"
    r"(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"\s*(?P<suffix>sgd|dollars?|dollar|bucks?)?$",
    re.IGNORECASE,
)
_ANY_NUMERIC_AMOUNT_MENTION = re.compile(
    r"(?<!\w)(?:-\s*)?(?:(?:(?:s\s*)?\$|sgd|usd|eur|gbp|aud|jpy)\s*"
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|"
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*"
    r"(?:sgd|usd|eur|gbp|aud|jpy|dollars?|dollar|bucks?)" + _CENTS_TAIL + r")(?!\w)",
    re.IGNORECASE,
)
_ANY_WORD_AMOUNT_MENTION = re.compile(
    r"(?<!\w)(?:negative\s+)?(?:(?:" + "|".join(sorted(_NUMBER_WORDS - {"and"})) + r")"
    r"(?:[\s-]+(?:" + "|".join(sorted(_NUMBER_WORDS)) + r"))*"
    r"\s+(?:sgd|usd|eur|gbp|aud|jpy|dollars?|dollar|bucks?)" + _CENTS_TAIL + r")(?!\w)",
    re.IGNORECASE,
)

# Bare amounts ("pay alicia 10", "transfer my mum fifty", "send john twenty
# can?") are accepted only in a payment slot: a sentence with a payment verb,
# the number directly after a name-like word (or "<verb> <number> to"), and
# never after words such as "for" that introduce counts ("for 2 cakes").
_PAYMENT_VERB = re.compile(r"\b(?:send|pay|transfer|give|bank\s+in|pass)\b", re.IGNORECASE)
_BARE_NUMBER = (
    r"(?:\d{1,3}(?:,\d{3})+|\d{1,6})(?:\.\d{1,2})?"
    r"|(?:" + "|".join(sorted(_NUMBER_WORDS - {"and"}, key=len, reverse=True)) + r")"
    r"(?:[\s-]+(?:" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r"))*"
)
_BARE_SLOT = re.compile(
    r"(?<![\w$])(?P<before>[a-z]+)\s+(?P<amount>" + _BARE_NUMBER + r")(?P<after>\s+[a-z%]+|\s*[.,!?]|\s*$)",
    re.IGNORECASE,
)
_NOT_A_PAYEE_WORD = {
    "for", "of", "at", "by", "with", "the", "a", "an", "in", "on", "to", "and", "or",
    "than", "about", "around", "only", "just", "maybe", "me", "help", "can", "lah",
    "paynow", "number", "no", "block", "unit", "floor", "room", "table", "bus",
} | set(_NUMBER_WORDS)
_NOT_AN_AMOUNT_FOLLOWER = {"cent", "cents", "x", "times", "pax", "people", "persons", "percent", "%", "pieces", "pcs", "items"}


def _bare_slot_matches(value: str) -> list[tuple[str, int, int]]:
    if not _PAYMENT_VERB.search(value):
        return []
    slots = []
    for match in _BARE_SLOT.finditer(value):
        before = match.group("before").casefold()
        after = match.group("after").strip().casefold()
        if after in _NOT_AN_AMOUNT_FOLLOWER:
            continue
        verb_first = _PAYMENT_VERB.fullmatch(before) and after == "to"
        if before in _NOT_A_PAYEE_WORD and not verb_first:
            continue
        if _PAYMENT_VERB.fullmatch(before) and not verb_first:
            continue
        slots.append((match.group("amount"), match.start("amount"), match.end("amount")))
    return slots


@dataclass(frozen=True)
class AmountMention:
    amount_minor: int
    evidence: str
    start: int
    end: int


def normalize_evidence(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def is_grounded(original_text: str, evidence: str) -> bool:
    normalized_evidence = normalize_evidence(evidence)
    if not normalized_evidence:
        return False
    normalized_original = normalize_evidence(original_text)
    return re.search(
        rf"(?<!\w){re.escape(normalized_evidence)}(?!\w)",
        normalized_original,
    ) is not None


def _amount_matches(value: str) -> list[re.Match[str]]:
    normalized = unicodedata.normalize("NFKC", value)
    matches = [
        match
        for pattern in (_ANY_NUMERIC_AMOUNT_MENTION, _ANY_WORD_AMOUNT_MENTION, _CENTS_ONLY)
        for match in pattern.finditer(normalized)
    ]
    matches.sort(key=lambda item: (item.start(), -(item.end() - item.start())))
    non_overlapping = []
    for match in matches:
        if non_overlapping and match.start() < non_overlapping[-1].end():
            continue
        non_overlapping.append(match)
    return non_overlapping


def _amount_mentions(value: str) -> list[str]:
    return [match.group(0) for match in _amount_matches(value)]


def derive_amount_mentions_from_text(value: str) -> tuple[AmountMention, ...]:
    matches = _amount_matches(value)
    if matches:
        return tuple(
            AmountMention(
                parse_sgd_amount(match.group(0), require_full_match=True),
                match.group(0),
                match.start(),
                match.end(),
            )
            for match in matches
        )
    # No amount has a currency marker: fall back to bare amounts in a clear
    # payment slot, read as Singapore dollars.
    return tuple(
        AmountMention(_bare_amount_minor(evidence), evidence, start, end)
        for evidence, start, end in _bare_slot_matches(unicodedata.normalize("NFKC", value))
    )


def _bare_amount_minor(evidence: str) -> int:
    normalized = normalize_evidence(evidence).replace("-", " ")
    if re.fullmatch(r"[\d,]+(?:\.\d{1,2})?", normalized):
        amount = _minor_units_from_decimal(normalized.replace(",", ""))
    else:
        amount = _major_units_from_words(normalized.split()) * 100
    if amount <= 0:
        raise GroundingError("INVALID_AMOUNT")
    return amount


def derive_amount_from_text(value: str) -> tuple[int | None, str | None]:
    """Derive one exact SGD amount from user-owned text, never model output."""

    mentions = derive_amount_mentions_from_text(value)
    if not mentions:
        return None, None
    if len(mentions) != 1:
        raise GroundingError("AMBIGUOUS_AMOUNT")
    return mentions[0].amount_minor, mentions[0].evidence


def _minor_units_from_decimal(value: str) -> int:
    if "." in value and len(value.rsplit(".", 1)[1]) > 2:
        raise GroundingError("INVALID_AMOUNT")
    try:
        major = Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise GroundingError("INVALID_AMOUNT") from exc
    minor = major * 100
    if major <= 0 or minor != minor.to_integral_value():
        raise GroundingError("INVALID_AMOUNT")
    return int(minor)


def _major_units_from_words(tokens: list[str]) -> int:
    total = 0
    current = 0
    saw_number = False
    saw_thousand = False
    for token in tokens:
        if token == "and":
            if not saw_number:
                raise GroundingError("INVALID_AMOUNT")
            continue
        # Within each hundred: tens then units ("fifty two"). "two fifty"
        # (2.50? 250?) or "five six" is not a number; ask instead of adding.
        below_hundred = current % 100
        if token in _SMALL_NUMBERS:
            if below_hundred and (below_hundred < 20 or _SMALL_NUMBERS[token] >= 10 or below_hundred % 10):
                raise GroundingError("AMBIGUOUS_AMOUNT")
            current += _SMALL_NUMBERS[token]
            saw_number = True
            continue
        if token in _TENS:
            if below_hundred:
                raise GroundingError("AMBIGUOUS_AMOUNT")
            current += _TENS[token]
            saw_number = True
            continue
        if token == "hundred":
            if current == 0:
                current = 1
            current *= 100
            saw_number = True
            continue
        if token == "thousand":
            if saw_thousand:
                raise GroundingError("INVALID_AMOUNT")
            if current == 0:
                current = 1
            total += current * 1000
            current = 0
            saw_number = True
            saw_thousand = True
            continue
        raise GroundingError("CLARIFICATION_SCOPE_VIOLATION")
    result = total + current
    if not saw_number or result <= 0:
        raise GroundingError("INVALID_AMOUNT")
    return result


def parse_sgd_amount(value: str, *, require_full_match: bool = False) -> int:
    normalized_input = normalize_evidence(value)
    if not normalized_input:
        raise GroundingError("INVALID_AMOUNT")
    if normalized_input.startswith("-") or re.search(
        r"\bnegative\b",
        normalized_input,
    ):
        raise GroundingError("INVALID_AMOUNT")
    normalized = normalized_input.replace("-", " ")
    if re.search(r"\b(?:usd|eur|gbp|aud|jpy)\b", normalized):
        raise GroundingError("UNSUPPORTED_CURRENCY")
    if re.search(r"\bor\b", normalized):
        raise GroundingError("AMBIGUOUS_AMOUNT")

    cents_only = re.fullmatch(r"(?P<minor>.+?)\s+cents?", normalized)
    if cents_only and _CENTS_ONLY.fullmatch(normalized):
        return _cents(cents_only.group("minor"))
    with_cents = re.fullmatch(
        r"(?P<major>.+?\s*(?:sgd|dollars?|bucks?))(?:\s+and)?\s+(?P<minor>.+?)(?:\s+cents?)?",
        normalized,
    )
    if with_cents and re.fullmatch(_CENTS_NUMBER, with_cents.group("minor")):
        major = parse_sgd_amount(with_cents.group("major"), require_full_match=require_full_match)
        return major + _cents(with_cents.group("minor"))

    numeric = _NUMERIC_AMOUNT.fullmatch(normalized)
    if numeric is not None:
        if numeric.group("prefix") is None and numeric.group("suffix") is None:
            if require_full_match:
                raise GroundingError("CLARIFICATION_SCOPE_VIOLATION")
            raise GroundingError("INVALID_AMOUNT")
        return _minor_units_from_decimal(numeric.group("number"))

    tokens = normalized.split()
    if tokens and tokens[-1] in {"sgd", "dollar", "dollars", "buck", "bucks"}:
        number_tokens = tokens[:-1]
        if number_tokens and all(token in _NUMBER_WORDS for token in number_tokens):
            return _major_units_from_words(number_tokens) * 100

    if require_full_match:
        raise GroundingError("CLARIFICATION_SCOPE_VIOLATION")
    raise GroundingError("INVALID_AMOUNT")


def _cents(value: str) -> int:
    value = value.strip()
    cents = int(value) if value.isdigit() else _major_units_from_words(value.replace("-", " ").split())
    if not 0 < cents < 100:
        raise GroundingError("INVALID_AMOUNT")
    return cents


def verify_amount(
    original_text: str,
    amount_minor: int | None,
    evidence: str | None,
) -> int | None:
    if amount_minor is None and evidence is None:
        return None
    if (
        type(amount_minor) is not int
        or evidence is None
        or not is_grounded(original_text, evidence)
    ):
        raise GroundingError("UNGROUNDED_AMOUNT")
    if len(_amount_mentions(original_text)) != 1:
        raise GroundingError("AMBIGUOUS_AMOUNT")
    try:
        parsed = parse_sgd_amount(evidence, require_full_match=True)
    except GroundingError as exc:
        if exc.reason_code == "CLARIFICATION_SCOPE_VIOLATION":
            raise GroundingError("UNGROUNDED_AMOUNT") from exc
        raise
    if parsed != amount_minor:
        raise GroundingError("AMOUNT_EVIDENCE_MISMATCH")
    return parsed


def verify_optional_reference(
    original_text: str,
    reference: str | None,
    evidence: str | None,
) -> str | None:
    if reference is None and evidence is None:
        return None
    if (
        not isinstance(reference, str)
        or not reference.strip()
        or not isinstance(evidence, str)
        or not is_grounded(original_text, evidence)
        or not is_grounded(evidence, reference)
    ):
        return None
    return reference.strip()

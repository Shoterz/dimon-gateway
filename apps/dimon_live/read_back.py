"""Spoken read-back of a draft, generated from the same stored review data
that fills the confirmation window, so what the user hears and what they see
cannot disagree.

Example: "Ten dollars to Alicia Powell, ending 9876, from savings. Approve
with your fingerprint, face, or PIN."
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def _below_thousand(number: int) -> str:
    parts = []
    hundreds, rest = divmod(number, 100)
    if hundreds:
        parts.append(f"{_ONES[hundreds]} hundred")
    if rest:
        if hundreds:
            parts.append("and")
        if rest < 20:
            parts.append(_ONES[rest])
        else:
            tens, ones = divmod(rest, 10)
            parts.append(_TENS[tens] + (f"-{_ONES[ones]}" if ones else ""))
    return " ".join(parts) or "zero"


def number_words(number: int) -> str:
    if number < 1000:
        return _below_thousand(number)
    groups = []
    for scale in ("", " thousand", " million"):
        number, chunk = divmod(number, 1000)
        if chunk:
            groups.append(_below_thousand(chunk) + scale)
        if not number:
            break
    return ", ".join(reversed(groups))


def amount_words(minor: int) -> str:
    """1050 -> 'ten dollars and fifty cents'. Spoken words cannot be misread
    the way '10.50' vs '1050' can."""

    dollars, cents = divmod(int(minor), 100)
    parts = []
    if dollars:
        parts.append(f"{number_words(dollars)} dollar{'s' if dollars != 1 else ''}")
    if cents:
        parts.append(f"{number_words(cents)} cent{'s' if cents != 1 else ''}")
    return " and ".join(parts) or "zero dollars"


def _ending(masked: str | None) -> str:
    digits = re.sub(r"\D", "", masked or "")
    return f", ending {' '.join(digits[-4:])}" if digits else ""


def build_read_back(review: Mapping[str, Any]) -> str:
    items = list(review.get("items") or ())
    phrases = [
        f"{amount_words(item['amount_minor'])} to {item['recipient_display_name']}{_ending(item.get('masked_destination'))}"
        for item in items
    ]
    sentence = phrases[0] if len(phrases) == 1 else "; ".join(phrases[:-1]) + f"; and {phrases[-1]}"
    source = review.get("source_label")
    text = sentence[0].upper() + sentence[1:] + (f", from {source}." if source else ".")
    if len(items) > 1:
        text += f" Total {amount_words(review.get('total_minor', 0))}."
    for item in items:
        if item.get("new_payee"):
            text += f" {item['recipient_display_name']} is a new payee."
        if item.get("unusual_amount"):
            text += f" This is more than you usually send to {item['recipient_display_name']}."
    policy = review.get("policy") or {}
    if policy.get("requires_out_of_band"):
        text += " You will also need the code from your hardware token."
    text += " If this is right, approve with your fingerprint, face, or PIN."
    return text

"""Understanding short spoken replies in a hands-free conversation.

After Dimon asks something ("Which account should pay?", "Shall I go ahead?",
"What should I call them?"), the answer is interpreted here by fixed rules.
Anything unclear returns None and Dimon asks again; nothing is guessed. A
reply can only choose between the options Dimon offered; it can never start
a payment, change an amount, or approve anything by itself.
"""

from __future__ import annotations

import re
from typing import Sequence

WAKE = re.compile(
    r"^\W*(?:hey|hi|hay|hello|okay|ok|oi|ay)\W+"
    # Speech recognisers spell the made-up name many ways: Dimon, Dimmon,
    # Dimin, Daimon, Demon, Diamond, Simon.
    r"(?:d[aeiy]{1,2}m{1,2}[aeiouy]{0,2}n|diamond|dimond|daemon|simon|dyman|di\s+mon|the\s+mon)\b"
    r"[\s,.!?:-]*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)

_YES = re.compile(
    r"\b(?:yes|yeah|yep|yup|ya|correct|confirm(?:ed)?|go\s+ahead|do\s+it|proceed|approve|"
    r"sure|okay|ok|right|that'?s\s+right|send\s+it|please\s+do|can|shiok)\b",
    re.IGNORECASE,
)
_NO = re.compile(
    r"\b(?:no|nope|nah|don'?t|do\s+not|cancel|stop|wait|wrong|skip|never\s*mind|not)\b",
    re.IGNORECASE,
)
_ORDINALS = {
    "first": 0, "1st": 0, "1": 0,   # not "one": "the second one"
    "second": 1, "2nd": 1, "two": 1, "2": 1,
    "third": 2, "3rd": 2, "three": 2, "3": 2,
    "fourth": 3, "4th": 3, "four": 3, "4": 3,
    "last": -1,
}
_ACCOUNTS = {
    "savings": "savings", "saving": "savings", "first": "savings", "1st": "savings", "primary": "savings",
    "main": "savings", "everyday": "everyday", "every day": "everyday", "second": "everyday",
    "2nd": "everyday", "daily": "everyday",
}
_NAME_LEAD = re.compile(
    r"^\W*(?:(?:please\s+)?(?:call|name|save)\s+(?:him|her|them|it|this\s+one)?\s*(?:as\s+)?|"
    r"(?:save\s+)?as\s+|it'?s\s+|they'?re\s+|his\s+name\s+is\s+|her\s+name\s+is\s+)",
    re.IGNORECASE,
)


def wake(transcript: str) -> tuple[bool, str]:
    """(woken, the command said after the wake phrase, possibly empty)."""

    match = WAKE.match(transcript.strip())
    if match is None:
        return False, ""
    return True, match.group("rest").strip(" ,.!?")


def confirm(text: str) -> bool | None:
    """True for yes, False for no, None when unclear. "No" wins any tie, so
    "yes, no wait" is a no."""

    if _NO.search(text):
        return False
    if _YES.search(text):
        return True
    return None


def account(text: str, labels: Sequence[str] = ("savings", "everyday")) -> str | None:
    lowered = text.casefold()
    found = {label for word, label in _ACCOUNTS.items() if re.search(rf"\b{re.escape(word)}\b", lowered)}
    found &= set(labels)
    return found.pop() if len(found) == 1 else None


def choice(text: str, options: Sequence[str]) -> int | None:
    """Pick one of the offered names, by name or by position ("the first one")."""

    lowered = text.casefold()
    by_name = [
        index for index, option in enumerate(options)
        if all(re.search(rf"\b{re.escape(token)}\b", lowered) for token in option.casefold().split()[:2])
    ]
    if len(by_name) == 1:
        return by_name[0]
    surnames = [
        index for index, option in enumerate(options)
        if len(option.split()) > 1 and re.search(rf"\b{re.escape(option.split()[-1].casefold())}\b", lowered)
    ]
    if len(surnames) == 1:
        return surnames[0]
    positions = {_ORDINALS[word] for word in re.findall(r"[a-z0-9]+", lowered) if word in _ORDINALS}
    if len(positions) == 1:
        position = positions.pop()
        index = position if position >= 0 else len(options) - 1
        return index if index < len(options) else None
    return None


def nickname(text: str) -> str | None:
    """A payee name from "call them Uncle Tan" or just "Uncle Tan"."""

    name = _NAME_LEAD.sub("", text.strip()).strip(" .!?,")
    name = re.sub(r"\s+please$", "", name, flags=re.IGNORECASE)
    if not re.fullmatch(r"[A-Za-z][A-Za-z '\-]{1,38}[A-Za-z]", name):
        return None
    return " ".join(word.capitalize() if word.islower() else word for word in name.split())


def interpret(expect: str, text: str, options: Sequence[str] = ()) -> object:
    if expect == "confirm":
        return confirm(text)
    if expect == "account":
        return account(text, options or ("savings", "everyday"))
    if expect == "choice":
        return choice(text, options)
    if expect == "name":
        return nickname(text)
    return None

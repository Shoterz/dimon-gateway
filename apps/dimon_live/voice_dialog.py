"""Understanding short spoken replies in a hands-free conversation.

After Dimon asks something ("Which account should pay?", "Shall I go ahead?",
"What should I call them?"), the answer is interpreted here by fixed rules.
Anything unclear returns None and Dimon asks again; nothing is guessed. A
reply can only choose between the options Dimon offered; it can never start
a payment, change an amount, or approve anything by itself.
"""

from __future__ import annotations

import difflib
import re
from typing import Sequence

# Speech recognisers spell the made-up name many ways: Dimon, Dimmon, Dimin,
# Daimon, Damon, Demon, Diamond, Simon, and split or blur its first sound:
# "Hey D Mon", "He Ji Man", "Hegemon".
_NAME = (r"(?:[dtjg]\W*[aeiy]{0,2}\W*m{1,2}[aeiouy]{1,2}n"
         r"|diamond|dimond|daemon|demon|simon|dyman|di\s+mon|the\s+mon)")
# Said without a greeting, only spellings that are not everyday first names
# wake Dimon, so "Damon, are you coming?" or "Simon, ..." stay ignored.
_BARE_NAME = r"(?:d[iy]m{1,2}[aeiouy]{0,2}n|daim{1,2}on|daemon)"
_FILLER = r"(?:(?:um+|uh+|er+|erm|so|oh|and|okay|ok)\W+)?"
# The greeting may be run into the name: "Hydiman", "Hademon", "High Dimmon".
_GREETING = r"(?:hey|hi|high|hay|hai|hae|hei|he|ha|hy|hello|okay|ok|oi|ay|yo)"
WAKE = re.compile(
    r"^\W*" + _FILLER
    + r"(?:" + _GREETING + r"\W*" + _NAME + r"|" + _BARE_NAME + r")\b"
    r"[\s,.!?:-]*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)

# Live partials are acted on before the authoritative Whisper transcript is
# available. Keep that early path deliberately narrower than WAKE: accepting
# only a greeting followed by one of the expected renderings avoids waking on
# ordinary speech such as "the demon in the film".
LIVE_WAKE = re.compile(
    r"^\s*(?:hey|hi)\s+(?:dimon|diamon|diamond)\b"
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


def live_wake(transcript: str) -> tuple[bool, str]:
    """Conservative wake matching for incomplete streaming transcripts."""

    match = LIVE_WAKE.match(transcript.strip())
    if match is None:
        return False, ""
    return True, match.group("rest").strip(" ,.!?")


# Short commands that steer whatever confirmation is on screen instead of
# starting a new request. Only the whole command counts, so "cancel my
# monthly plan" is still a request, not a control.
_CONTROL_CANCEL = re.compile(r"^(?:cancel|stop|never\s*mind|close|close\s+it|go\s+back|forget\s+it|don'?t)$")
_CONTROL_CONFIRM = re.compile(
    r"^(?:(?:yes\s+)?confirm|approve|continue|go\s+ahead|proceed|next|scan|scan\s+(?:my\s+)?face|"
    r"open\s+singpass|singpass|verify|verify\s+me|start)$"
)


def control(command: str) -> str | None:
    """'cancel' or 'confirm' for a bare control command, else None."""

    words = re.sub(r"[^a-z'\s]", " ", command.casefold())
    words = " ".join(word for word in words.split() if word not in {"please", "now", "it", "the"} or words.split() == [word])
    if _CONTROL_CANCEL.match(words):
        return "cancel"
    if _CONTROL_CONFIRM.match(words):
        return "confirm"
    return None


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
    return _sounds_like(lowered, options)


_DIGIT_WORDS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six",
                "7": "seven", "8": "eight", "9": "nine", "10": "ten", "11": "eleven", "12": "twelve"}


def _skeleton(word: str) -> str:
    """Consonants after the first letter: "tan" and "ten" are both "tn"."""

    return word[:1] + re.sub(r"[aeiouyh]", "", word[1:])


def _sounds_like(lowered: str, options: Sequence[str]) -> int | None:
    """Recognisers turn names into near-homophones: "John Tan" -> "John 10".
    Compare only the words that tell the options apart, and answer only when
    exactly one option is a clear match."""

    heard = [_DIGIT_WORDS.get(word, word) for word in re.findall(r"[a-z0-9]+", lowered)]
    joined = "".join(heard)
    words = [option.casefold().split() for option in options]
    shared = set.intersection(*(set(option) for option in words)) if words else set()
    scores = []
    for option in words:
        distinct = [word for word in option if word not in shared] or option
        # Run together, as in "Jontan" for "John Tan".
        best = difflib.SequenceMatcher(None, joined, "".join(option)).ratio() if len(joined) >= 4 else 0.0
        for said in heard:
            for word in distinct:
                ratio = difflib.SequenceMatcher(None, said, word).ratio()
                if len(word) >= 3 and _skeleton(said) == _skeleton(word):
                    ratio = max(ratio, 0.9)
                best = max(best, ratio)
        scores.append(best)
    if not scores:
        return None
    ranked = sorted(range(len(scores)), key=scores.__getitem__, reverse=True)
    top = ranked[0]
    runner_up = scores[ranked[1]] if len(ranked) > 1 else 0.0
    return top if scores[top] >= 0.75 and scores[top] - runner_up >= 0.25 else None


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

"""Split one utterance into separate requests.

"send 2 dollars to alicia on my second account buy $100 of nvidia shares and
add 88008800 to my paynow" is three requests. Each part is then processed on
its own through the full pipeline (firewall, scam check, grounding, policy,
validator) and gets its own receipt and its own signed approval. Nothing here
grants authority; splitting can only turn one request into several smaller,
separately approved ones.

The split is deterministic and conservative. A new request starts only at an
action verb ("send", "buy", "add", ...) that follows a separator ("and",
"then", a comma) or that follows a request already under way, and never after
words that make the verb part of the same sentence ("to buy", "for", "my").
A batch such as "send John S$6.50 and Alicia S$4" is not split, because
"Alicia" is not a verb; it stays one atomic batch receipt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_PARTS = 4

ACTION_VERBS = frozenset({
    "send", "pay", "transfer", "give", "paynow",
    "buy", "purchase", "invest", "sell",
    "add", "save", "register",
    "request", "ask",
    "check", "show",
})
# Only these start a new request without an "and", "then" or comma in front
# ("...on my second account buy $100 of NVIDIA"). Weaker words such as
# "paynow" ("John's PayNow"), "add" or "ask" need a separator.
STRONG_VERBS = frozenset({"send", "pay", "transfer", "buy", "purchase", "sell", "invest"})
# A correction ("send Alicia $10 and send John instead") is one request
# being changed, not two requests; it is never split.
CORRECTION = re.compile(
    r"\b(?:instead|actually|rather|i\s+mean|no\s+wait|wait\s+no|sorry|scratch\s+that|"
    r"change\s+(?:that|it)|make\s+(?:that|it))\b",
    re.IGNORECASE,
)
SEPARATORS = frozenset({"and", "then", "also", "plus", "&", "next", "after", "that"})
# A verb right after one of these belongs to the sentence already under way:
# "to buy flowers", "for", "my paynow", "I want to send", "can you pay".
BLOCKERS = frozenset({
    "to", "for", "of", "i", "we", "you", "me", "my", "your", "our", "the", "a",
    "an", "by", "via", "on", "in", "with", "through", "can", "could", "would",
    "will", "should", "must", "want", "wanna", "need", "let", "lets", "let's",
    "help", "not", "don't", "dont", "never", "do", "please", "pls", "plz",
    "kindly", "go", "just", "and", "then", "also",
})
LEAD_INS = frozenset({"please", "pls", "plz", "kindly", "also", "then", "and"})
# "... and don't buy NVIDIA" starts a part too, so the negation is seen and
# the whole sentence is refused instead of a "don't" being silently dropped.
NEGATIVE_LEADS = frozenset({"don't", "dont", "do", "not", "never"})
REFERS_BACK = frozenset({"her", "him", "them", "it", "that", "this", "those", "these"})
NEGATION = re.compile(r"\b(?:do\s+not|don't|dont|never|cancel|stop|not)\b", re.IGNORECASE)

_TOKEN = re.compile(r"\S+")
_EDGE_PUNCT = ".,;:!?\"'()[]"


@dataclass(frozen=True)
class Token:
    word: str      # lower-cased, edge punctuation stripped
    start: int
    end: int
    trailing: str  # punctuation that ended the raw token, e.g. ","


def _tokens(text: str) -> list[Token]:
    tokens = []
    for match in _TOKEN.finditer(text):
        raw = match.group(0)
        stripped = raw.strip(_EDGE_PUNCT)
        trailing = raw[len(raw.rstrip(_EDGE_PUNCT)):]
        tokens.append(Token(stripped.casefold(), match.start(), match.end(), trailing))
    return tokens


def _is_verb(tokens: list[Token], index: int) -> bool:
    word = tokens[index].word
    if word not in ACTION_VERBS:
        return False
    following = tokens[index + 1].word if index + 1 < len(tokens) else ""
    # "pay for dinner" is a reference, "pay now" at the end is the PayNow rail.
    if word == "pay" and following in {"for", "now"}:
        return following == "now" and index + 2 < len(tokens)
    if word in {"check", "show"} and following not in {"my", "balance", "balances", "portfolio"}:
        return False
    # "..., pay her from everyday" refers back to the request before it.
    if following in REFERS_BACK:
        return False
    return True


def split(text: str) -> list[str]:
    """Return the request parts, in order. One part means a single request."""

    if CORRECTION.search(text):
        return [text.strip()]
    tokens = _tokens(text)
    cuts: list[int] = []          # token index where each new part starts
    clause_has_verb = bool(tokens) and _is_verb(tokens, 0)
    if tokens and tokens[0].word in LEAD_INS and len(tokens) > 1 and _is_verb(tokens, 1):
        clause_has_verb = True
    for index in range(1, len(tokens)):
        if not _is_verb(tokens, index):
            continue
        # Walk back over lead-ins ("and please send", "then also buy").
        start = index
        while start - 1 > (cuts[-1] if cuts else 0) and tokens[start - 1].word in LEAD_INS | NEGATIVE_LEADS:
            start -= 1
        previous = tokens[start - 1]
        separated = (
            start != index
            or previous.trailing[:1] in {",", ".", ";", "!", "?"}
            or previous.word in SEPARATORS
        )
        unseparated_ok = (
            clause_has_verb
            and tokens[index].word in STRONG_VERBS
            and tokens[index - 1].word not in BLOCKERS
        )
        if separated or unseparated_ok:
            if start - (cuts[-1] if cuts else 0) >= 2:
                cuts.append(start)
        clause_has_verb = True
    if not cuts:
        return [text.strip()]

    bounds = [0, *cuts, len(tokens)]
    parts = []
    for position, (begin, end) in enumerate(zip(bounds, bounds[1:])):
        words = tokens[begin:end]
        # Drop leading lead-ins and trailing separators: "and", "then", ",".
        while words and words[0].word in LEAD_INS and len(words) > 1:
            words = words[1:]
        while words and words[-1].word in SEPARATORS:
            words = words[:-1]
        if not words:
            continue
        part = text[words[0].start:words[-1].end].rstrip(",;: ")
        if position < len(bounds) - 2:
            part = part.rstrip(".!?,;: ")   # a full stop that ended this part
        if part:
            parts.append(part)
    return parts


def has_negation(text: str) -> bool:
    return NEGATION.search(text) is not None

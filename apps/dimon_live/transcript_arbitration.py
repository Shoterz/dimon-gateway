"""Conservative arbitration between live and authoritative speech transcripts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher


_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10",
}
_BILL = {"bill", "bills", "water", "utility", "utilities", "pub", "electricity", "internet"}
_STOCK = {"stock", "stocks", "share", "shares", "buy", "sell", "market", "ticker", "invest"}
_TRANSFER = {"pay", "send", "transfer", "recipient", "friend", "dollar", "dollars"}
_CONVERSATION = {"how", "hello", "hi", "thanks", "thank", "welcome"}


@dataclass(frozen=True)
class TranscriptDecision:
    status: str
    confirmed_text: str | None


def _tokens(text: str) -> list[str]:
    values = re.findall(r"[a-z0-9]+", text.lower())
    return [_NUMBER_WORDS.get(value, value) for value in values]


def _domains(tokens: list[str]) -> set[str]:
    values = set(tokens)
    domains: set[str] = set()
    if values & _BILL:
        domains.add("bill")
    if values & _STOCK:
        domains.add("stock")
    if values & _TRANSFER:
        domains.add("transfer")
    return domains


def arbitrate_transcripts(live_text: str, final_text: str) -> TranscriptDecision:
    """Use Whisper unless the live Nemotron text materially contradicts it.

    Nemotron never silently overrides the final transcript. A disagreement
    blocks dispatch and asks the user to confirm the live interpretation.
    """

    live = _tokens(live_text)
    final = _tokens(final_text)
    if not live or not final:
        return TranscriptDecision("USE_FINAL", final_text)
    if live == final or live == final[: len(live)]:
        return TranscriptDecision("USE_FINAL", final_text)

    live_joined = " ".join(live)
    final_joined = " ".join(final)
    similarity = SequenceMatcher(None, live_joined, final_joined).ratio()
    live_domains = _domains(live)
    final_domains = _domains(final)

    # Non-financial conversation is not forced through financial routing.
    if not live_domains and not final_domains:
        return TranscriptDecision("USE_FINAL", final_text)
    if similarity >= 0.70:
        return TranscriptDecision("USE_FINAL", final_text)

    # A complete financial live interpretation disappearing from the final,
    # or changing domains, is unsafe to route without user confirmation.
    if len(live) >= 3 and (
        (live_domains and not final_domains)
        or (not live_domains and final_domains and bool(set(live) & _CONVERSATION))
        or (live_domains and final_domains and live_domains.isdisjoint(final_domains))
    ):
        return TranscriptDecision("CONFIRM_LIVE", None)

    return TranscriptDecision("USE_FINAL", final_text)

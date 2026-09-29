"""Deterministic intake firewall: layers 0 and 1 of the prompt-injection
defence in docs/features/prompt-injection-research.md.

It runs on every piece of user-owned text (typed request, voice transcript,
clarification reply) before any model sees it. Being plain code, it cannot
itself be talked out of its rules. A block returns a reason code and the
matched categories; the raw text never reaches the planner.

Blocking here is defence in depth, not the safety boundary: even text that
passes can only ever produce a draft that the validator audits and the
user's device must sign.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_TEXT_CHARS = 500
ISOLATION_TAG = "untrusted_user_request"

_FLAGS = re.IGNORECASE
_INSTRUCTION_OVERRIDE = tuple(
    re.compile(pattern, _FLAGS)
    for pattern in (
        r"\b(ignore|disregard|forget|override|overrule)\b.{0,40}\b(instructions?|rules?|prompts?|system|guardrails?|polic(y|ies)|previous|prior|above|safeguards?|review)\b",
        r"\bsystem\s+(prompt|message|instructions?)\b",
        r"\byou\s+are\s+now\b",
        r"\b(act|behave|respond)\s+as\s+(an?\s+)?(admin|administrator|developer|system|root|unrestricted)\b",
        r"\b(developer|debug|god|admin)\s+mode\b",
        r"\bjail\s*break\w*\b",
        r"\bnew\s+instructions?\b",
    )
)
_APPROVAL_BYPASS = tuple(
    re.compile(pattern, _FLAGS)
    for pattern in (
        r"\b(skip|bypass|disable|avoid|without|no\s+need\s+(for|to))\b.{0,30}\b(confirm\w*|approv\w*|verif\w*|review|signature|sign(ing)?|biometric\w*|otp|token|2fa|mfa|limits?)\b",
        r"\b(pre-?approved|already\s+approved|auto-?approv\w*|self-?approv\w*)\b",
        r"\b(output|return|mark|treat)\b.{0,25}\bapproved\b",
        r"\bapprove\s+(this|it|that|all|every|any|the\s+(transfer|payment))\b",
        r"\bexecute\s+(it|this|that|now|immediately|directly)\b",
    )
)
# Tool words alone are legitimate ("for the Python course"); asking to run
# them, or naming secrets and dangerous primitives, is not.
_TOOL_REQUEST = tuple(
    re.compile(pattern, _FLAGS)
    for pattern in (
        r"\b(run|execute|exec|call|use|using|invoke|eval)\b.{0,20}\b(python|javascript|shell|bash|powershell|sql|script|command|terminal|tool|function)\b",
        r"\b(os\.system|subprocess|rm\s+-rf|drop\s+table|select\s+\*\s+from|union\s+select)\b",
        r"\b(api\s*key|private\s+key|signing\s+key|password|passcode|pin\s+number|otp\s+code)\b",
    )
)
_ROLE_MARKUP = tuple(
    re.compile(pattern, _FLAGS | re.MULTILINE)
    for pattern in (
        r"<\|[^|>]{1,32}\|>",
        r"\[/?(inst|sys|system)\]",
        r"(^|[\s\"'(\[{:;,.])(system|assistant|developer)\s*:",
        r"#{2,}\s*(system|instruction|developer)",
        rf"</?\s*{ISOLATION_TAG}\s*>",
    )
)
_BASE64_RUN = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/=])")
_HEX_RUN = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])")


@dataclass(frozen=True)
class FirewallVerdict:
    allowed: bool
    categories: tuple[str, ...]
    source: str

    @property
    def reason_code(self) -> str | None:
        return None if self.allowed else "PROMPT_INJECTION_BLOCKED"

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "categories": list(self.categories), "source": self.source}


def _hidden_characters(text: str) -> bool:
    for character in text:
        code = ord(character)
        if character in "\n\r\t":
            continue
        category = unicodedata.category(character)
        # Cf: zero-width / bidi overrides; Cc: control; Co/Cn: private or
        # unassigned; U+E0000 block: invisible "tag" characters (ASCII smuggling).
        if category in {"Cf", "Cc", "Co", "Cn"} or 0xE0000 <= code <= 0xE007F:
            return True
    return False


def _looks_encoded(text: str) -> bool:
    for match in _BASE64_RUN.finditer(text):
        run = match.group(0)
        if re.search(r"[A-Z]", run) and re.search(r"[a-z]", run) and re.search(r"\d", run):
            return True
    return bool(_HEX_RUN.search(text))


def inspect(text: str, *, source: str = "user_request") -> FirewallVerdict:
    """Classify untrusted text; any category blocks it."""

    categories: list[str] = []
    if len(text) > MAX_TEXT_CHARS:
        categories.append("OVERSIZED_INPUT")
    if _hidden_characters(text):
        categories.append("HIDDEN_CHARACTERS")
    normalized = " ".join(unicodedata.normalize("NFKC", text).split())
    if any(pattern.search(text) for pattern in _ROLE_MARKUP):
        categories.append("ROLE_MARKUP")
    if any(pattern.search(normalized) for pattern in _INSTRUCTION_OVERRIDE):
        categories.append("INSTRUCTION_OVERRIDE")
    if any(pattern.search(normalized) for pattern in _APPROVAL_BYPASS):
        categories.append("APPROVAL_BYPASS")
    if any(pattern.search(normalized) for pattern in _TOOL_REQUEST):
        categories.append("TOOL_OR_SECRET_REQUEST")
    if _looks_encoded(unicodedata.normalize("NFKC", text)):
        categories.append("ENCODED_PAYLOAD")
    return FirewallVerdict(allowed=not categories, categories=tuple(categories), source=source)


_TAG_LOOKALIKE = re.compile(rf"<\s*/?\s*{ISOLATION_TAG}\s*>", _FLAGS)


def isolate(text: str) -> str:
    """Wrap user text as data for the planner (structured instruction isolation).

    Tag look-alikes are neutralised here as well as blocked by inspect(), so
    callers that skip the firewall (e.g. the remote inference gateway) still
    cannot close the data block and speak as an instruction.
    """

    safe = _TAG_LOOKALIKE.sub(lambda match: match.group(0).replace("<", "‹").replace(">", "›"), text)
    return f"<{ISOLATION_TAG}>\n{safe}\n</{ISOLATION_TAG}>"

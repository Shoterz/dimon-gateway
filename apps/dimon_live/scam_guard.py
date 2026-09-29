"""Scam-pattern hold.

Some requests parse perfectly and are still dangerous: "the police officer
told me to move my savings to a safe account" is a clean transfer request and
a textbook government-impersonation scam. This deterministic check looks for
the patterns Singapore's anti-scam messaging warns about and holds the
request with a warning instead of drafting it.

Rule: every layer, including the AI, may make a request stricter; none may
make it looser. This check only ever adds a hold.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_FLAGS = re.IGNORECASE
_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "AUTHORITY_IMPERSONATION": tuple(
        re.compile(pattern, _FLAGS)
        for pattern in (
            r"\b(police|police officer|inspector|investigator|monetary authority|spf|ica|iras|interpol|customs officer|government official|government officer|bank officer|bank staff)\b.{0,60}\b(told|asked|instructed|said|want(s|ed)?|need(s|ed)?|order(ed)?|require[sd]?)\b",
            r"\b(told|asked|instructed|ordered)\s+(me|us)\b.{0,40}\b(by|from)\s+(the\s+)?(police|officer|bank|government|mas|court)\b",
        )
    ),
    "SAFE_ACCOUNT": tuple(
        re.compile(pattern, _FLAGS)
        for pattern in (
            r"\b(safe|safety|secure|protected|holding|government|official|escrow)\s+account\b",
            r"\b(protect|secure|safeguard)\s+(my|our|the)\s+(money|savings|funds|account)\b",
        )
    ),
    "SECRECY": tuple(
        re.compile(pattern, _FLAGS)
        for pattern in (
            r"\b(don'?t|do not|never)\s+(tell|inform|call|contact)\s+(the\s+)?(bank|anyone|family|police)\b",
            r"\b(keep|kept)\s+(this|it)\s+(a\s+)?secret\b",
        )
    ),
    "THREAT_URGENCY": tuple(
        re.compile(pattern, _FLAGS)
        for pattern in (
            r"\b(arrest(ed)?|warrant|money laundering|under investigation|account (will be )?(frozen|suspended|blocked))\b",
        )
    ),
    "INVESTMENT_OR_JOB_SCAM": tuple(
        re.compile(pattern, _FLAGS)
        for pattern in (
            r"\b(guaranteed|sure[- ]win|risk[- ]free)\s+(return|returns|profit|profits|income)\b",
            r"\b(double|triple)\s+(my|your|the)\s+money\b",
            r"\b(unlock|release)\s+(my|the)\s+(commission|earnings|profits|withdrawal)\b",
            r"\b(task|tasks)\b.{0,30}\b(commission|top[- ]?up|deposit)\b",
        )
    ),
    "REMOTE_CONTROL_OR_CODES": tuple(
        re.compile(pattern, _FLAGS)
        for pattern in (
            r"\b(anydesk|teamviewer|remote access|screen shar(e|ing))\b",
            r"\b(give|share|send|read out)\b.{0,20}\b(otp|one[- ]time password|token code|pin)\b",
        )
    ),
}

WARNING = (
    "This looks like a common scam. The police, MAS, government agencies and "
    "banks will never ask you to move money to a 'safe account', keep a "
    "transfer secret, or share codes. Nothing has been sent. If someone asked "
    "you to do this, stop and call the ScamShield Helpline on 1799."
)


@dataclass(frozen=True)
class ScamAssessment:
    held: bool
    signals: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"held": self.held, "signals": list(self.signals), "warning": WARNING if self.held else None}


# "MAS" only in capitals: the lowercase word appears in ordinary text.
_MAS = re.compile(r"\bMAS\b.{0,60}\b(told|asked|instructed|said|wants?|needs?|ordered)\b")


def assess(text: str) -> ScamAssessment:
    normalized = " ".join(unicodedata.normalize("NFKC", text).split())
    signals = [
        name for name, patterns in _PATTERNS.items() if any(p.search(normalized) for p in patterns)
    ]
    if _MAS.search(normalized) and "AUTHORITY_IMPERSONATION" not in signals:
        signals.insert(0, "AUTHORITY_IMPERSONATION")
    return ScamAssessment(held=bool(signals), signals=tuple(signals))

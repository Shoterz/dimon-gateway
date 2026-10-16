"""Conservative corrections for recurring speech-recognition homophones.

Rules here must be phrase-bound and safe for financial requests. In
particular, do not add a global ``by`` -> ``buy`` replacement: ``paid by
card`` and similar phrases need intent context before they can be changed.
"""

from __future__ import annotations

import re


_LETS_BY = re.compile(r"(\blet(?:['’]?s|\s+us)\s+)by\b", re.IGNORECASE)


def normalize_spoken_request(transcript: str) -> str:
    """Return the text that intent parsing should receive for voice input."""

    return _LETS_BY.sub(r"\1buy", transcript)

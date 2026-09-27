"""Deterministic binding of batch recipient evidence to amount mentions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from apps.dimon_live.grounding import (
    GroundingError,
    derive_amount_mentions_from_text,
)


@dataclass(frozen=True)
class BoundBatchEvidence:
    recipient_evidence: str
    recipient_start: int
    amount_minor: int
    amount_evidence: str
    amount_start: int


def bind_batch_evidence(
    text: str,
    recipient_evidences: Sequence[str],
) -> tuple[BoundBatchEvidence, ...]:
    if not 2 <= len(recipient_evidences) <= 4:
        raise GroundingError("BATCH_RECIPIENT_COUNT_MISMATCH")

    recipient_spans: list[tuple[int, int, str]] = []
    for evidence in recipient_evidences:
        if not isinstance(evidence, str) or not evidence:
            raise GroundingError("BATCH_RECIPIENT_COUNT_MISMATCH")
        start = text.find(evidence)
        if start < 0 or text.find(evidence, start + 1) >= 0:
            raise GroundingError("AMBIGUOUS_BATCH_BINDING")
        recipient_spans.append((start, start + len(evidence), evidence))

    ordered_recipients = sorted(recipient_spans, key=lambda item: item[0])
    for previous, current in zip(ordered_recipients, ordered_recipients[1:]):
        if current[0] < previous[1]:
            raise GroundingError("AMBIGUOUS_BATCH_BINDING")

    try:
        amount_mentions = derive_amount_mentions_from_text(text)
    except GroundingError as exc:
        raise GroundingError("AMBIGUOUS_BATCH_BINDING") from exc
    if len(amount_mentions) != len(ordered_recipients):
        raise GroundingError("BATCH_RECIPIENT_COUNT_MISMATCH")

    ordered_amounts = sorted(amount_mentions, key=lambda item: item.start)
    return tuple(
        BoundBatchEvidence(
            recipient_evidence=recipient[2],
            recipient_start=recipient[0],
            amount_minor=amount.amount_minor,
            amount_evidence=amount.evidence,
            amount_start=amount.start,
        )
        for recipient, amount in zip(ordered_recipients, ordered_amounts)
    )

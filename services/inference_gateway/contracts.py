"""Versioned public request contracts for bounded inference."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class IntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    text: Annotated[str, Field(min_length=1, max_length=8192)]
    contract_version: Literal[4]

    @field_validator("text")
    @classmethod
    def validate_text_bytes(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        if len(value.encode("utf-8")) > 8192:
            raise ValueError("text must be at most 8192 UTF-8 bytes")
        return value


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    status: Literal[
        "EXECUTED_SYNTHETIC_TRANSFER",
        "EXECUTED_SYNTHETIC_BATCH",
    ]
    action_kind: Literal[
        "BENEFICIARY_TRANSFER",
        "PAYNOW_MOBILE_TRANSFER",
        "BENEFICIARY_TRANSFER_BATCH",
    ]
    amount: Annotated[int, Field(strict=True, gt=0, le=1_000_000_000)]
    currency: Literal["SGD"]
    recipient_display: Annotated[str, Field(min_length=1, max_length=100)]
    reference: Annotated[str | None, Field(max_length=140)] = None

    @field_validator("recipient_display", "reference")
    @classmethod
    def reject_control_characters(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("display text contains control characters")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def require_matching_status_and_action(self) -> "SpeechRequest":
        batch = self.action_kind == "BENEFICIARY_TRANSFER_BATCH"
        if batch != (self.status == "EXECUTED_SYNTHETIC_BATCH"):
            raise ValueError("status and action_kind do not match")
        return self

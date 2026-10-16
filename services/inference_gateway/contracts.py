"""Versioned public request contracts for bounded inference."""

from __future__ import annotations

import math
import re
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


class FollowUpContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: Annotated[str, Field(min_length=1, max_length=64)]
    question: Annotated[str, Field(min_length=1, max_length=512)]
    symbol: Annotated[str | None, Field(max_length=24)] = None
    price: str | int | float | None = None
    quote_currency: Annotated[str | None, Field(max_length=8)] = None

    @field_validator("price")
    @classmethod
    def validate_price(cls, value: str | int | float | None) -> str | int | float | None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("price must be finite")
        return value


class QwenTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    task: Literal["transfer", "stock", "bill_payment", "investment_follow_up", "multi_action"]
    text: Annotated[str, Field(min_length=1, max_length=8192)]
    context: FollowUpContext | None = None

    @field_validator("text")
    @classmethod
    def validate_text_bytes(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        if len(value.encode("utf-8")) > 8192:
            raise ValueError("text must be at most 8192 UTF-8 bytes")
        return value

    @model_validator(mode="after")
    def require_context_for_follow_up(self) -> "QwenTaskRequest":
        if (self.task == "investment_follow_up") != (self.context is not None):
            raise ValueError("context is required only for investment follow-up")
        return self


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    status: Literal[
        "EXECUTED_SYNTHETIC_TRANSFER",
        "EXECUTED_SYNTHETIC_BATCH",
        "REAL_STOCK_ALLOCATION_REQUIRED",
        "REAL_STOCK_BRIEF",
        "PREPARED_ORDER_REVIEW",
        "CONVERSATION_TURN_APPLIED",
        "PROMPT_INJECTION_BLOCKED",
    ]
    action_kind: Literal[
        "BENEFICIARY_TRANSFER",
        "PAYNOW_MOBILE_TRANSFER",
        "BENEFICIARY_TRANSFER_BATCH",
    ] | None = None
    amount: Annotated[int, Field(strict=True, gt=0, le=1_000_000_000)] | None = None
    currency: Literal["SGD"] | None = None
    recipient_display: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    reference: Annotated[str | None, Field(max_length=140)] = None
    instrument_label: Annotated[str | None, Field(min_length=1, max_length=80)] = None
    speech_text: Annotated[str | None, Field(min_length=1, max_length=1_600)] = None

    @field_validator("recipient_display", "reference", "instrument_label", "speech_text")
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
        if self.status == "REAL_STOCK_ALLOCATION_REQUIRED":
            if (
                self.instrument_label is None
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9 .&'()/\-]{0,79}",
                    self.instrument_label,
                )
                or any(
                    value is not None
                    for value in (
                        self.action_kind,
                        self.amount,
                        self.currency,
                        self.recipient_display,
                        self.reference,
                        self.speech_text,
                    )
                )
            ):
                raise ValueError("stock clarification fields are invalid")
            return self
        if self.status == "REAL_STOCK_BRIEF":
            if (
                self.speech_text is None
                or any(
                    value is not None
                    for value in (
                        self.action_kind,
                        self.amount,
                        self.currency,
                        self.recipient_display,
                        self.reference,
                        self.instrument_label,
                    )
                )
            ):
                raise ValueError("stock brief speech fields are invalid")
            return self
        if self.status in {"PREPARED_ORDER_REVIEW", "PROMPT_INJECTION_BLOCKED"}:
            if any(
                value is not None
                for value in (
                    self.action_kind,
                    self.amount,
                    self.currency,
                    self.recipient_display,
                    self.reference,
                    self.instrument_label,
                    self.speech_text,
                )
            ):
                raise ValueError("fieldless speech status fields are invalid")
            return self
        if self.status == "CONVERSATION_TURN_APPLIED":
            if (
                self.speech_text is None
                or any(
                    value is not None
                    for value in (
                        self.action_kind,
                        self.amount,
                        self.currency,
                        self.recipient_display,
                        self.reference,
                        self.instrument_label,
                    )
                )
            ):
                raise ValueError("conversation speech fields are invalid")
            return self
        if (
            self.instrument_label is not None
            or self.speech_text is not None
            or self.action_kind is None
            or self.amount is None
            or self.currency is None
            or self.recipient_display is None
        ):
            raise ValueError("transaction speech fields are incomplete")
        batch = self.action_kind == "BENEFICIARY_TRANSFER_BATCH"
        if batch != (self.status == "EXECUTED_SYNTHETIC_BATCH"):
            raise ValueError("status and action_kind do not match")
        return self

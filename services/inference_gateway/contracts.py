"""Versioned public request contracts for bounded inference."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


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

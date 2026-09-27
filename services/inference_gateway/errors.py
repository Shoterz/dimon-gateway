"""Stable public errors for the inference gateway."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse


@dataclass(eq=False)
class GatewayError(Exception):
    code: str
    message: str
    status_code: int
    request_id: str | None = None

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)


async def gateway_error_response(request: Request, exc: GatewayError) -> JSONResponse:
    request.state.outcome_code = exc.code
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": exc.request_id,
            }
        },
    )

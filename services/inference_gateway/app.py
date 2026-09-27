"""FastAPI composition root for the bounded inference gateway."""

from __future__ import annotations

import os

from fastapi import FastAPI

from .auth import AccessTokenVerifier
from .config import GatewayConfig
from .errors import GatewayError, gateway_error_response


def create_gateway_app(
    config: GatewayConfig,
    *,
    verifier: AccessTokenVerifier | None = None,
) -> FastAPI:
    application = FastAPI(
        title="Dimon Inference Gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.config = config
    application.state.access_verifier = verifier or AccessTokenVerifier(config)
    application.add_exception_handler(GatewayError, gateway_error_response)
    return application


def application_factory() -> FastAPI:
    return create_gateway_app(GatewayConfig.from_env(os.environ))

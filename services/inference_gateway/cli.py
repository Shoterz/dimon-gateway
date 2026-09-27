"""Loopback-only inference gateway entry point."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from .config import GatewayConfig


ServerRunner = Callable[..., Any]


def main(
    *,
    environ: Mapping[str, str] | None = None,
    runner: ServerRunner | None = None,
) -> None:
    settings = os.environ if environ is None else environ
    config = GatewayConfig.from_env(settings)
    if runner is None:
        import uvicorn

        runner = uvicorn.run
    runner(
        "services.inference_gateway.app:application_factory",
        factory=True,
        host=config.bind_host,
        port=config.bind_port,
    )


if __name__ == "__main__":
    main()

"""Command-line entry point for the Dimon application."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any


ServerRunner = Callable[..., Any]


def _port_from_environment(environ: Mapping[str, str]) -> int:
    raw_port = environ.get("DIMON_PORT", "8890")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("DIMON_PORT must be an integer between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError("DIMON_PORT must be an integer between 1 and 65535")
    return port


def main(
    *,
    environ: Mapping[str, str] | None = None,
    runner: ServerRunner | None = None,
) -> None:
    """Start the web application without managing any model process."""

    settings = os.environ if environ is None else environ
    if runner is None:
        import uvicorn

        runner = uvicorn.run

    runner(
        "apps.dimon_live.app:app",
        host=settings.get("DIMON_HOST", "127.0.0.1"),
        port=_port_from_environment(settings),
    )


if __name__ == "__main__":
    main()

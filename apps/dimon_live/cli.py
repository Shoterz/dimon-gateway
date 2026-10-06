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


def _workers_from_environment(environ: Mapping[str, str]) -> int:
    raw = environ.get("DIMON_WORKERS", "1").strip() or "1"
    try:
        workers = int(raw)
    except ValueError as exc:
        raise ValueError("DIMON_WORKERS must be an integer between 1 and 64") from exc
    if not 1 <= workers <= 64:
        raise ValueError("DIMON_WORKERS must be an integer between 1 and 64")
    return workers


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

    options: dict[str, Any] = {}
    workers = _workers_from_environment(settings)
    if workers > 1:
        # Several processes: each user's ledger is its own file and the
        # registry is shared through SQLite WAL, so workers need no affinity.
        options["workers"] = workers
    runner(
        "apps.dimon_live.app:app",
        host=settings.get("DIMON_HOST", "127.0.0.1"),
        port=_port_from_environment(settings),
        **options,
    )


def dev_main(
    *,
    environ: Mapping[str, str] | None = None,
    runner: ServerRunner | None = None,
) -> None:
    """Start Dimon with fixture inference unless a mode was selected explicitly."""

    settings = dict(os.environ if environ is None else environ)
    mode = settings.get("DIMON_INFERENCE_MODE") or "fixture"
    settings["DIMON_INFERENCE_MODE"] = mode

    missing = object()
    previous: str | object = os.environ.get("DIMON_INFERENCE_MODE", missing)
    os.environ["DIMON_INFERENCE_MODE"] = mode
    try:
        main(environ=settings, runner=runner)
    finally:
        if previous is missing:
            os.environ.pop("DIMON_INFERENCE_MODE", None)
        else:
            os.environ["DIMON_INFERENCE_MODE"] = str(previous)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Verify that Nemotron lifecycle leases always return to an idle count of zero."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.dimon_live.voice import NemotronLifecycleClient  # noqa: E402


class LifecycleVerificationError(RuntimeError):
    """A privacy-safe lifecycle acceptance failure."""


def _active_sessions(status: object) -> int:
    if not isinstance(status, dict):
        raise LifecycleVerificationError("invalid lifecycle status")
    value = status.get("active_sessions")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LifecycleVerificationError("invalid lifecycle status")
    return value


async def _wait_for_idle(
    client: NemotronLifecycleClient,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        try:
            active_sessions = _active_sessions(await client.status())
        except LifecycleVerificationError:
            raise
        except Exception:
            raise LifecycleVerificationError("lifecycle status request failed") from None
        if active_sessions == 0:
            return
        if loop.time() >= deadline:
            raise LifecycleVerificationError(
                "Nemotron active sessions did not return to zero"
            )
        await asyncio.sleep(poll_interval_seconds)


async def verify_lifecycle(
    client: NemotronLifecycleClient,
    *,
    cycles: int = 100,
    settle_timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.05,
) -> dict[str, int]:
    """Acquire/release bounded leases and prove the aggregate count returns to zero."""

    if isinstance(cycles, bool) or not isinstance(cycles, int) or not 1 <= cycles <= 1000:
        raise LifecycleVerificationError("cycles must be between 1 and 1000")
    if settle_timeout_seconds <= 0 or poll_interval_seconds < 0:
        raise LifecycleVerificationError("invalid lifecycle verification timing")

    for _cycle in range(cycles):
        try:
            lease = await client.acquire()
        except Exception:
            raise LifecycleVerificationError("lifecycle acquire failed") from None
        try:
            # Give cancellation a deterministic boundary after ownership transfers.
            await asyncio.sleep(0)
        finally:
            try:
                await asyncio.shield(lease.release())
            except Exception:
                raise LifecycleVerificationError("lifecycle release failed") from None
        await _wait_for_idle(
            client,
            timeout_seconds=settle_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    await _wait_for_idle(
        client,
        timeout_seconds=settle_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    return {"cycles": cycles, "final_active_sessions": 0}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=100)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[Path], NemotronLifecycleClient] = NemotronLifecycleClient,
) -> int:
    args = _parser().parse_args(argv)
    try:
        client = client_factory(args.socket)
        result = asyncio.run(verify_lifecycle(client, cycles=args.cycles))
    except (LifecycleVerificationError, ValueError, OSError):
        print(
            json.dumps(
                {"error": "lifecycle_verification_failed", "ok": False},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps({"ok": True, **result}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

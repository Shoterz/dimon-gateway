"""Bounded identity-scoped idempotency for gateway requests."""

from __future__ import annotations

import asyncio
import copy
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import GatewayError


_MISSING = object()


@dataclass
class _Entry:
    fingerprint: str
    expires_at: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    response: Any = _MISSING
    error: BaseException | None = None


@dataclass(frozen=True)
class IdempotencyDecision:
    state: Literal["new", "replay", "conflict"]
    _entry: _Entry | None = None

    async def resolve(self) -> Any:
        if self.state != "replay" or self._entry is None:
            raise RuntimeError("only a replay decision can be resolved")
        await self._entry.event.wait()
        if self._entry.error is not None:
            raise self._entry.error
        return copy.deepcopy(self._entry.response)


class IdempotencyStore:
    def __init__(
        self,
        ttl_seconds: float = 600,
        max_entries: int = 512,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        if ttl_seconds <= 0 or max_entries <= 0:
            raise ValueError("idempotency bounds must be positive")
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self.clock = clock
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()

    @property
    def entry_count(self) -> int:
        self._prune()
        return len(self._entries)

    def _prune(self) -> None:
        now = self.clock()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            entry = self._entries.pop(key)
            if not entry.event.is_set():
                entry.error = GatewayError(
                    "UPSTREAM_TIMEOUT",
                    "idempotent request expired before completion",
                    504,
                )
                entry.event.set()

    def _make_room(self) -> None:
        self._prune()
        while len(self._entries) >= self.max_entries:
            completed_key = next(
                (key for key, entry in self._entries.items() if entry.event.is_set()),
                None,
            )
            if completed_key is None:
                raise GatewayError(
                    "CAPACITY_EXCEEDED",
                    "idempotency capacity is full",
                    429,
                )
            self._entries.pop(completed_key)

    def begin(self, identity: str, request_id: str, fingerprint: str) -> IdempotencyDecision:
        self._prune()
        key = (identity, request_id)
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
            if entry.fingerprint != fingerprint:
                return IdempotencyDecision("conflict")
            return IdempotencyDecision("replay", entry)

        self._make_room()
        entry = _Entry(
            fingerprint=fingerprint,
            expires_at=self.clock() + self.ttl_seconds,
        )
        self._entries[key] = entry
        return IdempotencyDecision("new", entry)

    def succeed(
        self,
        identity: str,
        request_id: str,
        fingerprint: str,
        response: Any,
    ) -> None:
        key = (identity, request_id)
        entry = self._entries.get(key)
        if entry is None or entry.fingerprint != fingerprint or entry.event.is_set():
            raise RuntimeError("idempotency completion does not match an active request")
        entry.response = copy.deepcopy(response)
        entry.expires_at = self.clock() + self.ttl_seconds
        entry.event.set()

    def abort(
        self,
        identity: str,
        request_id: str,
        fingerprint: str,
        error: BaseException,
    ) -> None:
        key = (identity, request_id)
        entry = self._entries.get(key)
        if entry is None or entry.fingerprint != fingerprint or entry.event.is_set():
            return
        self._entries.pop(key)
        entry.error = error
        entry.event.set()

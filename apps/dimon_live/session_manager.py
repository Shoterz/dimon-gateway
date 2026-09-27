"""Server-owned session and ledger selection for the public Dimon gateway."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from .database import Database


class SessionBoundaryError(ValueError):
    """A request is outside the configured public boundary."""

    def __init__(self, reason_code: str, status_code: int = 403):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.status_code = status_code


class OwnerAccessError(SessionBoundaryError):
    def __init__(self):
        super().__init__("OWNER_ACCESS_REQUIRED", 403)


@dataclass(frozen=True)
class SessionContext:
    session_id: str
    role: Literal["owner", "visitor"]
    database: Database


def _origin_host(origin: str | None) -> str | None:
    if not origin:
        return None
    parsed = urlparse(origin)
    return parsed.netloc.lower() or None


def _decode_access_payload(assertion: str) -> dict:
    try:
        parts = assertion.split(".")
        if len(parts) != 3:
            raise ValueError
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OwnerAccessError() from exc


class SessionManager:
    cookie_name = "dimon_session"

    def __init__(
        self,
        *,
        owner_path: Path,
        visitor_dir: Path,
        allowed_origins: set[str] | None = None,
        owner_origin: str = "https://dimon.stocksuite.app",
        owner_access_audience: str | None = None,
        visitor_ttl_seconds: int = 3600,
        max_visitor_sessions: int = 25,
        legacy_database: Database | None = None,
    ):
        self.owner_path = Path(owner_path)
        self.visitor_dir = Path(visitor_dir)
        self.allowed_origins = {
            value.strip().rstrip("/")
            for value in (allowed_origins or set())
            if value.strip()
        }
        self.owner_origin = owner_origin.rstrip("/")
        self.owner_host = _origin_host(self.owner_origin)
        self.owner_access_audience = owner_access_audience
        self.visitor_ttl_seconds = max(60, int(visitor_ttl_seconds))
        self.max_visitor_sessions = max(1, int(max_visitor_sessions))
        self.legacy_database = legacy_database
        self._owner_database: Database | None = None

    @classmethod
    def from_environment(cls, *, legacy_database: Database | None = None):
        root = Path(__file__).resolve().parents[2] / "runtime" / "ledgers"
        owner_path = Path(os.environ.get("DIMON_DB_OWNER_PATH", root / "owner.sqlite3"))
        visitor_dir = Path(os.environ.get("DIMON_VISITOR_DB_DIR", root / "visitors"))
        origins = {
            value.strip()
            for value in os.environ.get("DIMON_ALLOWED_ORIGINS", "").split(",")
            if value.strip()
        }
        owner_origin = os.environ.get("DIMON_OWNER_ORIGIN", "https://dimon.stocksuite.app")
        origins.update({owner_origin, "http://127.0.0.1:8890", "http://localhost:8890"})
        return cls(
            owner_path=owner_path,
            visitor_dir=visitor_dir,
            allowed_origins=origins,
            owner_origin=owner_origin,
            owner_access_audience=os.environ.get("DIMON_OWNER_ACCESS_AUDIENCE") or None,
            visitor_ttl_seconds=int(os.environ.get("DIMON_VISITOR_TTL_SECONDS", "3600")),
            max_visitor_sessions=int(os.environ.get("DIMON_MAX_VISITOR_SESSIONS", "25")),
            legacy_database=legacy_database,
        )

    def _active_visitor_files(self) -> list[Path]:
        self.visitor_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        active: list[Path] = []
        for path in self.visitor_dir.glob("*.sqlite3"):
            try:
                if now - path.stat().st_mtime > self.visitor_ttl_seconds:
                    path.unlink(missing_ok=True)
                    for sidecar in (Path(str(path) + "-wal"), Path(str(path) + "-shm")):
                        sidecar.unlink(missing_ok=True)
                else:
                    active.append(path)
            except OSError:
                continue
        return active

    def _touch(self, database: Database) -> None:
        try:
            os.utime(database.path, None)
        except FileNotFoundError:
            pass

    def create_visitor(self) -> SessionContext:
        active = self._active_visitor_files()
        if len(active) >= self.max_visitor_sessions:
            raise SessionBoundaryError("SESSION_CAP_REACHED", 429)
        session_id = secrets.token_urlsafe(24)
        database = Database(self.visitor_dir / f"{session_id}.sqlite3")
        database.initialize()
        self._touch(database)
        return SessionContext(session_id=session_id, role="visitor", database=database)

    def _owner_context(self) -> SessionContext:
        if self._owner_database is None:
            self._owner_database = Database(self.owner_path)
            self._owner_database.initialize()
        self._touch(self._owner_database)
        return SessionContext(session_id="owner", role="owner", database=self._owner_database)

    def _assert_origin(self, origin: str | None, host: str | None) -> None:
        if not self.allowed_origins:
            return
        if origin and origin.rstrip("/") not in self.allowed_origins:
            raise SessionBoundaryError("ORIGIN_NOT_ALLOWED", 403)
        if origin is None and host and host not in {
            _origin_host(value) for value in self.allowed_origins
        }:
            raise SessionBoundaryError("ORIGIN_NOT_ALLOWED", 403)

    def _assert_owner_access(self, assertion: str | None) -> None:
        if not assertion:
            raise OwnerAccessError()
        payload = _decode_access_payload(assertion)
        expires = payload.get("exp")
        if not isinstance(expires, (int, float)) or expires <= time.time():
            raise OwnerAccessError()
        audience = payload.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if not self.owner_access_audience or self.owner_access_audience not in audiences:
            raise OwnerAccessError()
        if not payload.get("email") and not payload.get("sub"):
            raise OwnerAccessError()

    def resolve_headers(
        self,
        *,
        host: str | None,
        origin: str | None,
        cookie: str | None,
        access_assertion: str | None,
    ) -> SessionContext:
        if self.legacy_database is not None:
            return SessionContext("legacy", "owner", self.legacy_database)
        self._assert_origin(origin, host)
        if (
            host
            and self.owner_host
            and host.lower() == self.owner_host
            and self.owner_access_audience
        ):
            self._assert_owner_access(access_assertion)
            return self._owner_context()
        if cookie and re.fullmatch(r"[A-Za-z0-9_-]{20,80}", cookie):
            candidate = self.visitor_dir / f"{cookie}.sqlite3"
            if candidate.exists() and candidate.parent == self.visitor_dir and candidate.name == f"{cookie}.sqlite3":
                database = Database(candidate)
                database.initialize()
                self._touch(database)
                return SessionContext(cookie, "visitor", database)
        return self.create_visitor()

    def cleanup(self) -> None:
        self._active_visitor_files()

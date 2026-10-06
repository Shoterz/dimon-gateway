"""Server-owned session and ledger selection for the public Dimon gateway."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from .database import Database, _schema_version

TOUCH_INTERVAL_SECONDS = 60
TEMPLATE_MAX_AGE_SECONDS = 3600


def _schema_stamp() -> int:
    return _schema_version(False)


class LedgerRegistry:
    """One small index of every user ledger: when it was last used and
    whether its owner completed KYC. Lookups are by primary key and expiry
    sweeps use an index, so both stay fast at a million rows."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS ledgers (
      session_id TEXT PRIMARY KEY,
      created_at REAL NOT NULL,
      last_seen REAL NOT NULL,
      registered INTEGER NOT NULL DEFAULT 0 CHECK (registered IN (0, 1))
    ) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS ledgers_expiry ON ledgers(registered, last_seen);
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One long-lived connection per process: the registry is shared by
        # every request and every worker, so it uses WAL (readers never wait
        # for a writer) and never pays connection setup per request.
        self._connection = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.executescript(self.SCHEMA)
        self._guard = threading.Lock()

    @contextlib.contextmanager
    def _connect(self):
        with self._guard:
            yield self._connection

    def close(self) -> None:
        with self._guard:
            self._connection.close()

    def add(self, session_id: str, now: float) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO ledgers (session_id, created_at, last_seen, registered) VALUES (?, ?, ?, 0)",
                (session_id, now, now),
            )

    def touch(self, session_id: str, now: float) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE ledgers SET last_seen = ? WHERE session_id = ?", (now, session_id))

    def mark_registered(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE ledgers SET registered = 1 WHERE session_id = ?", (session_id,))

    def expired(self, session_id: str, before: float) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_seen, registered FROM ledgers WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return False
        return not row[1] and row[0] < before

    def expired_batch(self, before: float, limit: int) -> list[str]:
        with self._connect() as connection:
            return [row[0] for row in connection.execute(
                "SELECT session_id FROM ledgers WHERE registered = 0 AND last_seen < ? LIMIT ?",
                (before, limit),
            )]

    def remove(self, session_ids: list[str]) -> None:
        if not session_ids:
            return
        with self._connect() as connection:
            connection.executemany("DELETE FROM ledgers WHERE session_id = ?", [(sid,) for sid in session_ids])

    def count_active(self, since: float) -> int:
        with self._connect() as connection:
            return connection.execute(
                "SELECT (SELECT COUNT(*) FROM ledgers WHERE registered = 0 AND last_seen >= ?)"
                " + (SELECT COUNT(*) FROM ledgers WHERE registered = 1)",
                (since,),
            ).fetchone()[0]

    def count(self) -> int:
        with self._connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM ledgers").fetchone()[0]


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
        ledger_cache_size: int = 4096,
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
        # 0 means no cap: the setting for a real deployment. The default keeps
        # a hobby host from being filled with throwaway sessions.
        self.max_visitor_sessions = max(0, int(max_visitor_sessions))
        self.legacy_database = legacy_database
        self._owner_database: Database | None = None
        # None means the installed OpenCV / ONNX engines; tests inject stand-ins.
        self.face_engine = None
        self.voice_engine = None
        self.ledger_cache_size = max(1, int(ledger_cache_size))
        self._ledgers: OrderedDict[str, Database] = OrderedDict()
        self._touched: dict[str, float] = {}
        self._lock = threading.Lock()
        self._registry: LedgerRegistry | None = None

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
            ledger_cache_size=int(os.environ.get("DIMON_LEDGER_CACHE_SIZE", "4096")),
        )

    # ------------------------------------------------------------ storage
    @property
    def registry(self) -> "LedgerRegistry":
        if self._registry is None:
            self._registry = LedgerRegistry(self.visitor_dir / "registry.sqlite3")
        return self._registry

    def ledger_path(self, session_id: str) -> Path:
        """Two levels of hashed folders (65,536 leaves): at a million users a
        folder holds ~15 ledgers instead of one folder holding them all."""

        digest = hashlib.sha256(session_id.encode()).hexdigest()
        return self.visitor_dir / digest[:2] / digest[2:4] / f"{session_id}.sqlite3"

    def _open(self, session_id: str, path: Path) -> Database:
        """A ledger from the in-process cache, or opened once and cached."""

        with self._lock:
            database = self._ledgers.get(session_id)
            if database is not None:
                self._ledgers.move_to_end(session_id)
                return database
        database = Database(path, face_engine=self.face_engine, voice_engine=self.voice_engine)
        database.initialize()   # a single PRAGMA read unless the code changed
        with self._lock:
            self._ledgers[session_id] = database
            while len(self._ledgers) > self.ledger_cache_size:
                self._ledgers.popitem(last=False)
        return database

    def _template(self) -> Path:
        """A freshly seeded ledger to copy for each new user: copying the
        file takes milliseconds, seeding a new one takes about a second.
        Rebuilt hourly so the seeded payment history stays current."""

        folder = self.visitor_dir / "_template"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"ledger-{_schema_stamp()}.sqlite3"
        if path.is_file() and time.time() - path.stat().st_mtime < TEMPLATE_MAX_AGE_SECONDS:
            return path
        with self._lock:
            if path.is_file() and time.time() - path.stat().st_mtime < TEMPLATE_MAX_AGE_SECONDS:
                return path
            staging = folder / f"building-{secrets.token_hex(6)}.sqlite3"
            Database(staging).initialize()
            connection = sqlite3.connect(staging, isolation_level=None)
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("PRAGMA journal_mode = DELETE")
            finally:
                connection.close()
            os.replace(staging, path)
            for leftover in folder.glob("building-*"):
                with contextlib.suppress(OSError):
                    leftover.unlink()
        return path

    def _touch(self, session_id: str) -> None:
        """Record activity at most once a minute per user, so a busy user
        does not turn every request into a registry write."""

        now = time.time()
        if now - self._touched.get(session_id, 0.0) < TOUCH_INTERVAL_SECONDS:
            return
        if len(self._touched) > 200_000:
            self._touched.clear()
        self._touched[session_id] = now
        self.registry.touch(session_id, now)

    def create_visitor(self) -> SessionContext:
        now = time.time()
        if self.max_visitor_sessions and (
            self.registry.count_active(now - self.visitor_ttl_seconds) >= self.max_visitor_sessions
        ):
            raise SessionBoundaryError("SESSION_CAP_REACHED", 429)
        session_id = secrets.token_urlsafe(24)
        path = self.ledger_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._template(), path)
        self.registry.add(session_id, now)
        self._touched[session_id] = now
        database = self._open(session_id, path)
        return SessionContext(session_id=session_id, role="visitor", database=database)

    def close(self) -> None:
        """Release the registry connection (shutdown, tests)."""

        if self._registry is not None:
            self._registry.close()
            self._registry = None
        with self._lock:
            self._ledgers.clear()

    def mark_registered(self, session_id: str) -> None:
        """A user who completed KYC keeps their ledger: it no longer expires."""

        self.registry.mark_registered(session_id)

    def _forget(self, session_id: str, path: Path) -> None:
        with self._lock:
            self._ledgers.pop(session_id, None)
        self._touched.pop(session_id, None)
        stem = path.with_suffix("")
        for file in (path, Path(f"{path}-wal"), Path(f"{path}-shm"),
                     Path(f"{stem}.faces.db"), Path(f"{stem}.voice.db"),
                     Path(f"{stem}.voice.db-wal"), Path(f"{stem}.voice.db-shm")):
            with contextlib.suppress(OSError):
                file.unlink(missing_ok=True)

    def _owner_context(self) -> SessionContext:
        if self._owner_database is None:
            self._owner_database = Database(self.owner_path, face_engine=self.face_engine,
                                            voice_engine=self.voice_engine)
            self._owner_database.initialize()
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
            candidate = self.ledger_path(cookie)
            if candidate.is_file():
                if self.registry.expired(cookie, time.time() - self.visitor_ttl_seconds):
                    self._forget(cookie, candidate)
                    self.registry.remove([cookie])
                else:
                    database = self._open(cookie, candidate)
                    self._touch(cookie)
                    return SessionContext(cookie, "visitor", database)
            legacy = self.visitor_dir / f"{cookie}.sqlite3"
            if legacy.is_file() and time.time() - legacy.stat().st_mtime <= self.visitor_ttl_seconds:
                # A ledger from before sharding: still served until it expires.
                database = self._open(cookie, legacy)
                with contextlib.suppress(OSError):
                    os.utime(legacy, None)
                return SessionContext(cookie, "visitor", database)
        return self.create_visitor()

    def cleanup(self, *, batch: int = 1000) -> int:
        """Delete anonymous ledgers idle longer than the TTL, a batch at a
        time from the registry index (never by scanning folders). Users who
        completed KYC are kept. Returns how many were removed."""

        before = time.time() - self.visitor_ttl_seconds
        expired = self.registry.expired_batch(before, batch)
        for session_id in expired:
            self._forget(session_id, self.ledger_path(session_id))
        self.registry.remove(expired)
        # Ledgers from before sharding sit flat in the folder; sweep those too.
        if self.visitor_dir.is_dir():
            for path in self.visitor_dir.glob("*.sqlite3"):
                if path.name == "registry.sqlite3":
                    continue
                with contextlib.suppress(OSError):
                    if time.time() - path.stat().st_mtime > self.visitor_ttl_seconds:
                        self._forget(path.stem, path)
        return len(expired)

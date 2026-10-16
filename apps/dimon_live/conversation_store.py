"""SQLite persistence for the optional server-owned conversation core."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from .conversation import (
    ConversationInput,
    ConversationRejected,
    ConversationStatus,
    ConversationThread,
    ConversationTransition,
    ConversationTurn,
)


CONVERSATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_threads (
  conversation_id TEXT PRIMARY KEY,
  session_scope TEXT NOT NULL,
  user_id TEXT NOT NULL,
  domain TEXT NOT NULL,
  handler_name TEXT NOT NULL,
  handler_version INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ('WAITING', 'READY_FOR_DRAFT', 'CANCELLED', 'SUPERSEDED', 'EXPIRED', 'CONSUMED')
  ),
  version INTEGER NOT NULL CHECK (version >= 1),
  state_json TEXT NOT NULL,
  last_response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS conversation_threads_one_active
  ON conversation_threads(session_scope, user_id, domain)
  WHERE status IN ('WAITING', 'READY_FOR_DRAFT');
CREATE TABLE IF NOT EXISTS conversation_turns (
  turn_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversation_threads(conversation_id),
  request_id TEXT NOT NULL,
  source TEXT NOT NULL,
  input_kind TEXT NOT NULL CHECK (input_kind IN ('TEXT', 'CHOICE')),
  input_json TEXT NOT NULL,
  before_version INTEGER NOT NULL,
  after_version INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (conversation_id, request_id)
);
"""


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ConversationRejected("CONVERSATION_TIMESTAMP_INVALID")
    return value.isoformat()


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ConversationRejected("CONVERSATION_STATE_INVALID") from exc
    if parsed.tzinfo is None:
        raise ConversationRejected("CONVERSATION_STATE_INVALID")
    return parsed


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex}"


class ConversationStore:
    def __init__(self, path: Path, clock: Callable[[], datetime]):
        self.path = Path(path)
        self.clock = clock

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 3000")
            yield connection
        finally:
            connection.close()

    def create_thread(
        self,
        *,
        session_scope: str,
        user_id: str,
        domain: str,
        handler_name: str,
        handler_version: int,
        transition: ConversationTransition,
        expires_at: datetime,
    ) -> tuple[ConversationThread, bool]:
        now = self.clock()
        conversation_id = _new_id("conversation_")
        response_json = transition.response_json()
        state_json = json.dumps(
            transition.state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        superseded = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_text = _timestamp(now)
                connection.execute(
                    """
                    UPDATE conversation_threads
                    SET status = 'EXPIRED', updated_at = ?
                    WHERE session_scope = ? AND user_id = ? AND domain = ?
                      AND status IN ('WAITING', 'READY_FOR_DRAFT')
                      AND expires_at <= ?
                    """,
                    (now_text, session_scope, user_id, domain, now_text),
                )
                superseded = connection.execute(
                    """
                    UPDATE conversation_threads
                    SET status = 'SUPERSEDED', updated_at = ?
                    WHERE session_scope = ? AND user_id = ? AND domain = ?
                      AND status IN ('WAITING', 'READY_FOR_DRAFT')
                    """,
                    (now_text, session_scope, user_id, domain),
                ).rowcount > 0
                connection.execute(
                    """
                    INSERT INTO conversation_threads (
                      conversation_id, session_scope, user_id, domain,
                      handler_name, handler_version, status, version,
                      state_json, last_response_json, created_at, updated_at,
                      expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        session_scope,
                        user_id,
                        domain,
                        handler_name,
                        handler_version,
                        transition.status.value,
                        state_json,
                        response_json,
                        now_text,
                        now_text,
                        _timestamp(expires_at),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            row = connection.execute(
                "SELECT * FROM conversation_threads WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return self._thread(row), superseded

    def get_thread(
        self,
        conversation_id: str,
        session_scope: str,
        user_id: str,
    ) -> ConversationThread:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM conversation_threads
                WHERE conversation_id = ? AND session_scope = ? AND user_id = ?
                """,
                (conversation_id, session_scope, user_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ConversationRejected("CONVERSATION_NOT_FOUND")
            thread = self._thread(row)
            if (
                thread.status in {ConversationStatus.WAITING, ConversationStatus.READY_FOR_DRAFT}
                and thread.expires_at <= self.clock()
            ):
                connection.execute(
                    "UPDATE conversation_threads SET status = 'EXPIRED', updated_at = ? WHERE conversation_id = ?",
                    (_timestamp(self.clock()), conversation_id),
                )
                connection.commit()
                raise ConversationRejected("CONVERSATION_EXPIRED")
            connection.commit()
            return thread

    def commit_turn(
        self,
        *,
        thread: ConversationThread,
        request_id: str,
        source: str,
        conversation_input: ConversationInput,
        transition: ConversationTransition,
        expires_at: datetime,
    ) -> tuple[ConversationThread, ConversationTurn, bool]:
        now = self.clock()
        input_json = conversation_input.canonical_json()
        response_json = transition.response_json()
        state_json = json.dumps(
            transition.state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT * FROM conversation_threads
                    WHERE conversation_id = ? AND session_scope = ? AND user_id = ?
                    """,
                    (thread.conversation_id, thread.session_scope, thread.user_id),
                ).fetchone()
                if row is None:
                    raise ConversationRejected("CONVERSATION_NOT_FOUND")
                stored = self._thread(row)
                existing = connection.execute(
                    """
                    SELECT * FROM conversation_turns
                    WHERE conversation_id = ? AND request_id = ?
                    """,
                    (thread.conversation_id, request_id),
                ).fetchone()
                if existing is not None:
                    if existing["input_json"] != input_json:
                        raise ConversationRejected("CONVERSATION_IDEMPOTENCY_CONFLICT")
                    connection.commit()
                    return stored, self._turn(existing), True
                if (
                    stored.status in {ConversationStatus.WAITING, ConversationStatus.READY_FOR_DRAFT}
                    and stored.expires_at <= now
                ):
                    connection.execute(
                        "UPDATE conversation_threads SET status = 'EXPIRED', updated_at = ? WHERE conversation_id = ?",
                        (_timestamp(now), thread.conversation_id),
                    )
                    raise ConversationRejected("CONVERSATION_EXPIRED")
                if stored.status is not ConversationStatus.WAITING:
                    raise ConversationRejected("CONVERSATION_TERMINAL")
                if stored.version != thread.version:
                    raise ConversationRejected("CONVERSATION_VERSION_CONFLICT")
                after_version = stored.version + 1 if transition.advance_version else stored.version
                next_expiry = expires_at if transition.advance_version else stored.expires_at
                changed = connection.execute(
                    """
                    UPDATE conversation_threads
                    SET status = ?, version = ?, state_json = ?,
                        last_response_json = ?, updated_at = ?, expires_at = ?
                    WHERE conversation_id = ? AND version = ?
                    """,
                    (
                        transition.status.value if transition.advance_version else stored.status.value,
                        after_version,
                        state_json if transition.advance_version else stored.state_json,
                        response_json,
                        _timestamp(now),
                        _timestamp(next_expiry),
                        thread.conversation_id,
                        stored.version,
                    ),
                )
                if changed.rowcount != 1:
                    raise ConversationRejected("CONVERSATION_VERSION_CONFLICT")
                turn_id = _new_id("turn_")
                connection.execute(
                    """
                    INSERT INTO conversation_turns (
                      turn_id, conversation_id, request_id, source, input_kind,
                      input_json, before_version, after_version, outcome,
                      response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        turn_id,
                        thread.conversation_id,
                        request_id,
                        source,
                        conversation_input.kind.value,
                        input_json,
                        stored.version,
                        after_version,
                        transition.outcome,
                        response_json,
                        _timestamp(now),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            updated_row = connection.execute(
                "SELECT * FROM conversation_threads WHERE conversation_id = ?",
                (thread.conversation_id,),
            ).fetchone()
            turn_row = connection.execute(
                """
                SELECT * FROM conversation_turns
                WHERE conversation_id = ? AND request_id = ?
                """,
                (thread.conversation_id, request_id),
            ).fetchone()
        return self._thread(updated_row), self._turn(turn_row), False

    def get_turn(self, conversation_id: str, request_id: str) -> ConversationTurn | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM conversation_turns
                WHERE conversation_id = ? AND request_id = ?
                """,
                (conversation_id, request_id),
            ).fetchone()
        return self._turn(row) if row is not None else None

    @staticmethod
    def _thread(row: sqlite3.Row) -> ConversationThread:
        if row is None:
            raise ConversationRejected("CONVERSATION_STATE_INVALID")
        try:
            status = ConversationStatus(row["status"])
        except ValueError as exc:
            raise ConversationRejected("CONVERSATION_STATE_INVALID") from exc
        return ConversationThread(
            conversation_id=row["conversation_id"],
            session_scope=row["session_scope"],
            user_id=row["user_id"],
            domain=row["domain"],
            handler_name=row["handler_name"],
            handler_version=row["handler_version"],
            status=status,
            version=row["version"],
            state_json=row["state_json"],
            last_response_json=row["last_response_json"],
            created_at=_parse_timestamp(row["created_at"]),
            updated_at=_parse_timestamp(row["updated_at"]),
            expires_at=_parse_timestamp(row["expires_at"]),
        )

    @staticmethod
    def _turn(row: sqlite3.Row) -> ConversationTurn:
        if row is None:
            raise ConversationRejected("CONVERSATION_STATE_INVALID")
        return ConversationTurn(
            turn_id=row["turn_id"],
            conversation_id=row["conversation_id"],
            request_id=row["request_id"],
            source=row["source"],
            input_kind=row["input_kind"],
            input_json=row["input_json"],
            before_version=row["before_version"],
            after_version=row["after_version"],
            outcome=row["outcome"],
            response_json=row["response_json"],
            created_at=_parse_timestamp(row["created_at"]),
        )

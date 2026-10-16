"""Append-only, hash-chained audit trail.

Every step of a conversational transaction is recorded as an event: the raw
request and firewall verdict, the voice capture (audio hash and transcript),
the model's raw draft, receipts, every validation report, challenges, the
verified device signature, policy refusals, and the ledger commit.

Each entry's hash covers the previous entry's hash, so rewriting or removing
history breaks the chain; SQLite triggers additionally refuse UPDATE and
DELETE on the table. verify_chain() recomputes the whole chain.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

GENESIS_HASH = "0" * 64

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
  sequence INTEGER PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE,
  occurred_at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  request_id TEXT,
  receipt_id TEXT,
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  previous_hash TEXT NOT NULL,
  entry_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS audit_events_receipt ON audit_events(receipt_id);
CREATE INDEX IF NOT EXISTS audit_events_request ON audit_events(request_id);
CREATE TRIGGER IF NOT EXISTS audit_events_append_only_update
BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'AUDIT_LOG_APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS audit_events_append_only_delete
BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'AUDIT_LOG_APPEND_ONLY'); END;
"""


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def entry_hash(
    *,
    previous_hash: str,
    sequence: int,
    occurred_at: str,
    event_type: str,
    request_id: str | None,
    receipt_id: str | None,
    payload_sha256: str,
) -> str:
    material = "\x1f".join(
        (
            previous_hash,
            str(sequence),
            occurred_at,
            event_type,
            request_id or "",
            receipt_id or "",
            payload_sha256,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def append(
    connection: sqlite3.Connection,
    event_type: str,
    payload: Any,
    *,
    now: datetime,
    request_id: str | None = None,
    receipt_id: str | None = None,
) -> str:
    """Append one event inside the caller's transaction; returns its hash."""

    last = connection.execute(
        "SELECT sequence, entry_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    sequence = (last[0] + 1) if last else 1
    previous = last[1] if last else GENESIS_HASH
    occurred_at = now.astimezone(timezone.utc).isoformat()
    payload_json = _canonical(payload)
    payload_sha = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    digest = entry_hash(
        previous_hash=previous,
        sequence=sequence,
        occurred_at=occurred_at,
        event_type=event_type,
        request_id=request_id,
        receipt_id=receipt_id,
        payload_sha256=payload_sha,
    )
    connection.execute(
        """
        INSERT INTO audit_events (
          sequence, event_id, occurred_at, event_type, request_id, receipt_id,
          payload_json, payload_sha256, previous_hash, entry_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sequence,
            f"evt_{secrets.token_hex(12)}",
            occurred_at,
            event_type,
            request_id,
            receipt_id,
            payload_json,
            payload_sha,
            previous,
            digest,
        ),
    )
    return digest


def verify_chain(connection: sqlite3.Connection) -> dict[str, Any]:
    previous = GENESIS_HASH
    count = 0
    for row in connection.execute(
        """
        SELECT sequence, occurred_at, event_type, request_id, receipt_id,
               payload_json, payload_sha256, previous_hash, entry_hash
        FROM audit_events ORDER BY sequence
        """
    ):
        sequence, occurred_at, event_type, request_id, receipt_id, payload_json, payload_sha, stored_previous, stored_hash = row
        count += 1
        if sequence != count or stored_previous != previous:
            return {"valid": False, "events": count, "broken_at": sequence, "reason": "CHAIN_LINK_BROKEN"}
        if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != payload_sha:
            return {"valid": False, "events": count, "broken_at": sequence, "reason": "PAYLOAD_ALTERED"}
        expected = entry_hash(
            previous_hash=previous,
            sequence=sequence,
            occurred_at=occurred_at,
            event_type=event_type,
            request_id=request_id,
            receipt_id=receipt_id,
            payload_sha256=payload_sha,
        )
        if expected != stored_hash:
            return {"valid": False, "events": count, "broken_at": sequence, "reason": "ENTRY_ALTERED"}
        previous = stored_hash
    return {"valid": True, "events": count, "head": previous}


def events(
    connection: sqlite3.Connection,
    *,
    receipt_id: str | None = None,
    request_ids: tuple[str, ...] = (),
    limit: int = 200,
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if receipt_id is not None:
        clauses.append("receipt_id = ?")
        params.append(receipt_id)
    if request_ids:
        clauses.append(f"request_id IN ({','.join('?' for _ in request_ids)})")
        params.extend(request_ids)
    where = f"WHERE {' OR '.join(clauses)}" if clauses else ""
    rows = connection.execute(
        f"""
        SELECT sequence, occurred_at, event_type, request_id, receipt_id,
               payload_json, entry_hash, previous_hash
        FROM audit_events {where} ORDER BY sequence DESC LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [
        {
            "sequence": row[0],
            "occurred_at": row[1],
            "event_type": row[2],
            "request_id": row[3],
            "receipt_id": row[4],
            "payload": json.loads(row[5]),
            "entry_hash": row[6],
            "previous_hash": row[7],
        }
        for row in reversed(rows)
    ]

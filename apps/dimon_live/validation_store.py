"""Runs the independent validation agent over a stored receipt and records it.

The agent reads the receipt exactly as persisted plus the server-owned
directory, so it audits what would actually execute, not what the drafting
pipeline intended.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone

from . import audit_log
from .validation_agent import (
    DirectoryEntry,
    DraftItem,
    DraftUnderAudit,
    ValidationReport,
    audit_draft,
)

VALIDATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS validation_records (
  validation_id TEXT PRIMARY KEY,
  receipt_kind TEXT NOT NULL CHECK(receipt_kind IN ('single', 'batch')),
  receipt_id TEXT NOT NULL,
  receipt_digest TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('DRAFT', 'CHALLENGE', 'EXECUTION')),
  verdict TEXT NOT NULL CHECK(verdict IN ('PASS', 'ATTENTION', 'FROZEN')),
  report_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def _utterances(row: sqlite3.Row) -> list[str]:
    keys = row.keys()
    if "evidence_bundle_json" in keys and row["evidence_bundle_json"]:
        try:
            turns = json.loads(row["evidence_bundle_json"])
        except json.JSONDecodeError:
            turns = []
        texts = [turn["text"] for turn in turns if isinstance(turn, dict) and isinstance(turn.get("text"), str)]
        if texts:
            return texts
    return [row["original_text"]]


def audit_stored_receipt(
    connection: sqlite3.Connection, kind: str, receipt_id: str
) -> tuple[sqlite3.Row, ValidationReport]:
    table = {"single": "review_receipts", "batch": "batch_review_receipts"}[kind]
    row = connection.execute(f"SELECT * FROM {table} WHERE receipt_id = ?", (receipt_id,)).fetchone()
    if row is None:
        raise LookupError(receipt_id)
    source = connection.execute(
        "SELECT label FROM accounts WHERE account_id = ?", (row["source_account_id"],)
    ).fetchone()
    if kind == "single":
        paynow = (
            row["destination_reference"]
            if "destination_kind" in row.keys() and row["destination_kind"] == "PAYNOW_MOBILE"
            else None
        )
        items = (
            DraftItem(
                beneficiary_id=row["beneficiary_id"],
                recipient_display_name=row["recipient_display_name"],
                amount_minor=row["amount_minor"],
                paynow_proxy=paynow,
            ),
        )
    else:
        items = tuple(
            DraftItem(
                beneficiary_id=item["beneficiary_id"],
                recipient_display_name=item["recipient_display_name"],
                amount_minor=item["amount_minor"],
            )
            for item in connection.execute(
                """
                SELECT beneficiary_id, recipient_display_name, amount_minor
                FROM batch_receipt_items WHERE receipt_id = ? ORDER BY position
                """,
                (receipt_id,),
            ).fetchall()
        )
    aliases: dict[str, list[str]] = {}
    try:
        for alias, beneficiary_id in connection.execute("SELECT alias, beneficiary_id FROM beneficiary_aliases"):
            aliases.setdefault(beneficiary_id, []).append(alias)
    except sqlite3.OperationalError:
        pass
    directory = [
        DirectoryEntry(entry["beneficiary_id"], entry["display_name"], tuple(aliases.get(entry["beneficiary_id"], ())))
        for entry in connection.execute("SELECT beneficiary_id, display_name FROM beneficiaries")
    ]
    source_labels = [
        account["label"]
        # rowid order is the bank's account order (savings first), which
        # "my first/second account" refers to.
        for account in connection.execute("SELECT label FROM accounts WHERE is_user_source = 1 ORDER BY rowid")
    ]
    report = audit_draft(
        _utterances(row),
        DraftUnderAudit(
            source_label=source["label"] if source else "",
            currency=row["currency"],
            items=items,
        ),
        directory=directory,
        source_labels=source_labels,
    )
    return row, report


def record_validation(
    connection: sqlite3.Connection,
    *,
    kind: str,
    receipt_id: str,
    stage: str,
    now: datetime,
) -> ValidationReport:
    row, report = audit_stored_receipt(connection, kind, receipt_id)
    connection.execute(
        """
        INSERT INTO validation_records (
          validation_id, receipt_kind, receipt_id, receipt_digest, stage,
          verdict, report_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"val_{secrets.token_hex(16)}",
            kind,
            receipt_id,
            row["receipt_digest"],
            stage,
            report.verdict,
            json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True),
            now.astimezone(timezone.utc).isoformat(),
        ),
    )
    audit_log.append(
        connection,
        f"VALIDATION_{stage}",
        {"receipt_kind": kind, "receipt_digest": row["receipt_digest"], **report.to_dict()},
        now=now,
        receipt_id=receipt_id,
    )
    return report

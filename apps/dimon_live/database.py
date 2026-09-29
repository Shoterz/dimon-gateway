"""SQLite-backed synthetic ledger owned by the Dimon Live server."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .approval_gateway import ApprovalContext, ApprovalRejected, DeviceAssertion
from .batch_receipts import (
    BatchReceiptDraft,
    BatchReceiptItemDraft,
    build_batch_receipt_record,
)
from .grounding import GroundingError, parse_sgd_amount
from .name_resolution import (
    name_tokens,
    rank_name_candidates,
    rank_phonetic_candidates,
)
from .risk_policy import (
    POLICY_SCHEMA,
    PolicyRejected,
    evaluate as evaluate_policy,
    mock_token_code,
    seed_profile,
    verify_out_of_band,
)
from .validation_store import VALIDATION_SCHEMA, record_validation
from .signing_store import (
    SIGNING_SCHEMA,
    consume_approval,
    issue_challenge,
    list_credentials,
    prior_signature,
    register_credential,
)
from .receipts import (
    DEMO_USER_ID,
    ReceiptDraft,
    build_expected_receipt_payload,
    build_receipt_record,
    canonical_json,
    digest_canonical,
)


class InsufficientFunds(ValueError):
    """Raised when the source balance cannot fund a transfer."""


class IdempotencyConflict(ValueError):
    """Raised when a request ID is reused for different input."""


class ResolutionError(ValueError):
    """Raised when a command references missing server-owned state."""


class ReceiptRejected(ValueError):
    """Raised when a stored receipt cannot be authorized safely."""

    def __init__(self, reason_code: str, *, persist_status: bool = False):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.persist_status = persist_status


class ClarificationRejected(ValueError):
    """Raised when a stored clarification cannot be completed safely."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class Resolution:
    kind: str
    matches: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ActionAttempt:
    request_id: str
    original_text: str
    raw_model_output: str | None
    validated_intent: Mapping[str, Any] | None
    outcome: str
    reason_code: str | None
    response: Mapping[str, Any]


@dataclass(frozen=True)
class TransferCommand:
    request_id: str
    original_text: str
    source_account_id: str
    beneficiary_id: str
    amount_minor: int
    currency: str
    raw_model_output: str
    validated_intent: Mapping[str, Any]
    evidence_bundle: tuple[Mapping[str, Any], ...] = ()


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  account_id TEXT PRIMARY KEY,
  owner_display_name TEXT NOT NULL,
  label TEXT NOT NULL,
  normalized_label TEXT NOT NULL UNIQUE,
  currency TEXT NOT NULL CHECK (currency = 'SGD'),
  balance_minor INTEGER NOT NULL CHECK (balance_minor >= 0),
  is_user_source INTEGER NOT NULL CHECK (is_user_source IN (0, 1))
);
CREATE TABLE IF NOT EXISTS beneficiaries (
  beneficiary_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  normalized_alias TEXT NOT NULL UNIQUE,
  destination_account_id TEXT NOT NULL REFERENCES accounts(account_id),
  masked_destination TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_attempts (
  request_id TEXT PRIMARY KEY,
  original_text TEXT NOT NULL,
  raw_model_output TEXT,
  validated_intent_json TEXT,
  outcome TEXT NOT NULL,
  reason_code TEXT,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_transactions (
  transaction_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE REFERENCES action_attempts(request_id),
  kind TEXT NOT NULL CHECK (kind = 'BENEFICIARY_TRANSFER'),
  amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
  currency TEXT NOT NULL CHECK (currency = 'SGD'),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_entries (
  entry_id TEXT PRIMARY KEY,
  transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
  account_id TEXT NOT NULL REFERENCES accounts(account_id),
  delta_minor INTEGER NOT NULL CHECK (delta_minor != 0)
);
CREATE TABLE IF NOT EXISTS review_receipts (
  receipt_id TEXT PRIMARY KEY,
  clarification_group_id TEXT,
  request_id TEXT NOT NULL REFERENCES action_attempts(request_id),
  user_id TEXT NOT NULL,
  source_account_id TEXT NOT NULL REFERENCES accounts(account_id),
  beneficiary_id TEXT NOT NULL REFERENCES beneficiaries(beneficiary_id),
  recipient_display_name TEXT NOT NULL,
  amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
  currency TEXT NOT NULL CHECK (currency = 'SGD'),
  original_text TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  nonce TEXT NOT NULL UNIQUE,
  canonical_json TEXT NOT NULL,
  receipt_digest TEXT NOT NULL CHECK (
    length(receipt_digest) = 64
    AND receipt_digest NOT GLOB '*[^0-9a-f]*'
  ),
  status TEXT NOT NULL CHECK (
    status IN ('AWAITING_CONFIRMATION', 'EXECUTED', 'EXPIRED', 'SUPERSEDED')
  ),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authorization_records (
  authorization_id TEXT PRIMARY KEY,
  receipt_id TEXT NOT NULL UNIQUE REFERENCES review_receipts(receipt_id),
  receipt_digest TEXT NOT NULL,
  user_id TEXT NOT NULL,
  method TEXT NOT NULL CHECK (
    method IN ('EXPLICIT_COMMAND_POLICY', 'USER_CONFIRMATION')
  ),
  authorized_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipt_executions (
  receipt_id TEXT PRIMARY KEY REFERENCES review_receipts(receipt_id),
  transaction_id TEXT NOT NULL UNIQUE REFERENCES ledger_transactions(transaction_id),
  executed_at TEXT NOT NULL
);
"""

V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS clarification_sessions (
  clarification_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE REFERENCES action_attempts(request_id),
  response_request_id TEXT UNIQUE,
  response_text TEXT,
  response_json TEXT,
  completed_receipt_id TEXT REFERENCES review_receipts(receipt_id),
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind = 'BENEFICIARY_TRANSFER'),
  partial_intent_json TEXT NOT NULL,
  evidence_bundle_json TEXT NOT NULL,
  missing_field TEXT NOT NULL CHECK (missing_field = 'amount_minor'),
  status TEXT NOT NULL CHECK (
    status IN ('AWAITING_RESPONSE', 'COMPLETED', 'EXPIRED', 'SUPERSEDED')
  ),
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paynow_proxies (
  canonical_proxy TEXT PRIMARY KEY,
  is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
  masked_name TEXT NOT NULL,
  beneficiary_id TEXT NOT NULL UNIQUE REFERENCES beneficiaries(beneficiary_id)
);
CREATE TABLE IF NOT EXISTS paynow_lookups (
  lookup_reference TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE,
  canonical_proxy TEXT NOT NULL REFERENCES paynow_proxies(canonical_proxy),
  masked_name TEXT NOT NULL,
  beneficiary_id TEXT NOT NULL REFERENCES beneficiaries(beneficiary_id),
  destination_account_id TEXT NOT NULL REFERENCES accounts(account_id),
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

BATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS batch_review_receipts (
  receipt_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE REFERENCES action_attempts(request_id),
  user_id TEXT NOT NULL,
  source_account_id TEXT NOT NULL REFERENCES accounts(account_id),
  total_minor INTEGER NOT NULL CHECK(total_minor > 0),
  currency TEXT NOT NULL CHECK(currency = 'SGD'),
  original_text TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  nonce TEXT NOT NULL UNIQUE,
  canonical_json TEXT NOT NULL,
  receipt_digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN (
    'AWAITING_CONFIRMATION', 'EXECUTED', 'EXPIRED'
  )),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_receipt_items (
  receipt_id TEXT NOT NULL REFERENCES batch_review_receipts(receipt_id),
  position INTEGER NOT NULL,
  beneficiary_id TEXT NOT NULL REFERENCES beneficiaries(beneficiary_id),
  destination_account_id TEXT NOT NULL REFERENCES accounts(account_id),
  recipient_display_name TEXT NOT NULL,
  amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
  recipient_evidence TEXT NOT NULL,
  amount_evidence TEXT NOT NULL,
  PRIMARY KEY(receipt_id, position),
  UNIQUE(receipt_id, beneficiary_id)
);
CREATE TABLE IF NOT EXISTS batch_ledger_transactions (
  batch_transaction_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE REFERENCES action_attempts(request_id),
  total_minor INTEGER NOT NULL CHECK(total_minor > 0),
  currency TEXT NOT NULL CHECK(currency = 'SGD'),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_ledger_entries (
  entry_id TEXT PRIMARY KEY,
  batch_transaction_id TEXT NOT NULL
    REFERENCES batch_ledger_transactions(batch_transaction_id),
  account_id TEXT NOT NULL REFERENCES accounts(account_id),
  item_position INTEGER,
  delta_minor INTEGER NOT NULL CHECK(delta_minor != 0)
);
CREATE TABLE IF NOT EXISTS batch_receipt_executions (
  receipt_id TEXT PRIMARY KEY REFERENCES batch_review_receipts(receipt_id),
  batch_transaction_id TEXT NOT NULL UNIQUE
    REFERENCES batch_ledger_transactions(batch_transaction_id),
  executed_at TEXT NOT NULL
);
"""

SEED_ACCOUNTS = (
    ("acct_savings_01", "Demo User", "savings", "savings", "SGD", 100000, 1),
    (
        "acct_john_smith_01",
        "John Smith",
        "John Smith",
        "john smith",
        "SGD",
        10000,
        0,
    ),
    (
        "acct_alicia_powell_01",
        "Alicia Powell",
        "Alicia Powell",
        "alicia powell",
        "SGD",
        7500,
        0,
    ),
    ("acct_john_tan_01", "John Tan", "John Tan", "john tan", "SGD", 7500, 0),
)

SEED_BENEFICIARIES = (
    (
        "beneficiary_john_smith_01",
        "John Smith",
        "john smith",
        "acct_john_smith_01",
        "•••• 4321",
    ),
    (
        "beneficiary_alicia_powell_01",
        "Alicia Powell",
        "alicia powell",
        "acct_alicia_powell_01",
        "•••• 9876",
    ),
    (
        "beneficiary_john_tan_01",
        "John Tan",
        "john tan",
        "acct_john_tan_01",
        "•••• 2468",
    ),
)

PAYNOW_ACCOUNT = (
    "acct_paynow_jxxx_01",
    "JXXX SXXXX",
    "JXXX SXXXX",
    "paynow 86952933",
    "SGD",
    0,
    0,
)
PAYNOW_BENEFICIARY = (
    "beneficiary_paynow_jxxx_01",
    "JXXX SXXXX",
    "paynow:86952933",
    "acct_paynow_jxxx_01",
    "PayNow •••• 2933",
    "PAYNOW",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized(value: str) -> str:
    return value.strip().casefold()


class Database:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # sqlite3's own context manager commits but never closes; close
        # explicitly so Windows releases the file lock on the ledger.
        connection = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 3000")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            connection.executescript(V2_SCHEMA)
            connection.executescript(BATCH_SCHEMA)
            connection.executescript(SIGNING_SCHEMA)
            connection.executescript(VALIDATION_SCHEMA)
            connection.executescript(POLICY_SCHEMA)
            seed_profile(connection, DEMO_USER_ID, self._clock())
            self._apply_v2_migrations(connection)
            count = connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            if count == 0:
                self._insert_seed(connection)
            else:
                self._insert_paynow_seed(connection)

    @staticmethod
    def _apply_v2_migrations(connection: sqlite3.Connection) -> None:
        migrations = {
            "beneficiaries": (
                (
                    "directory_kind",
                    "ALTER TABLE beneficiaries ADD COLUMN directory_kind "
                    "TEXT NOT NULL DEFAULT 'SAVED'",
                ),
            ),
            "review_receipts": (
                (
                    "contract_version",
                    "ALTER TABLE review_receipts ADD COLUMN contract_version "
                    "INTEGER NOT NULL DEFAULT 1",
                ),
                (
                    "destination_kind",
                    "ALTER TABLE review_receipts ADD COLUMN destination_kind TEXT",
                ),
                (
                    "destination_reference",
                    "ALTER TABLE review_receipts ADD COLUMN destination_reference TEXT",
                ),
                (
                    "destination_account_id",
                    "ALTER TABLE review_receipts ADD COLUMN destination_account_id TEXT",
                ),
                (
                    "lookup_reference",
                    "ALTER TABLE review_receipts ADD COLUMN lookup_reference TEXT",
                ),
                (
                    "evidence_bundle_json",
                    "ALTER TABLE review_receipts ADD COLUMN evidence_bundle_json TEXT",
                ),
            ),
            "paynow_lookups": (
                (
                    "destination_account_id",
                    "ALTER TABLE paynow_lookups ADD COLUMN destination_account_id TEXT",
                ),
            ),
        }
        for table, additions in migrations.items():
            present = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for column, statement in additions:
                if column not in present:
                    connection.execute(statement)
        connection.execute(
            """
            UPDATE paynow_lookups
            SET destination_account_id = (
              SELECT b.destination_account_id FROM beneficiaries b
              WHERE b.beneficiary_id = paynow_lookups.beneficiary_id
            )
            WHERE destination_account_id IS NULL
            """
        )

    def _insert_seed(self, connection: sqlite3.Connection) -> None:
        connection.executemany(
            """
            INSERT INTO accounts (
              account_id, owner_display_name, label, normalized_label,
              currency, balance_minor, is_user_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            SEED_ACCOUNTS,
        )
        connection.executemany(
            """
            INSERT INTO beneficiaries (
              beneficiary_id, display_name, normalized_alias,
              destination_account_id, masked_destination
            ) VALUES (?, ?, ?, ?, ?)
            """,
            SEED_BENEFICIARIES,
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(beneficiaries)")
        }
        if "directory_kind" in columns:
            self._insert_paynow_seed(connection)

    @staticmethod
    def _insert_paynow_seed(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO accounts (
              account_id, owner_display_name, label, normalized_label,
              currency, balance_minor, is_user_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            PAYNOW_ACCOUNT,
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO beneficiaries (
              beneficiary_id, display_name, normalized_alias,
              destination_account_id, masked_destination, directory_kind
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            PAYNOW_BENEFICIARY,
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO paynow_proxies (
              canonical_proxy, is_active, masked_name, beneficiary_id
            ) VALUES ('86952933', 1, 'JXXX SXXXX', 'beneficiary_paynow_jxxx_01')
            """
        )

    def reset(self) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Enrolled device credentials survive a demo reset; the
                # challenges and signatures belong to the receipts removed here.
                connection.execute("DELETE FROM approval_signatures")
                connection.execute("DELETE FROM approval_challenges")
                connection.execute("DELETE FROM validation_records")
                connection.execute("DELETE FROM out_of_band_attempts")
                connection.execute("DELETE FROM batch_ledger_entries")
                connection.execute("DELETE FROM batch_receipt_executions")
                connection.execute("DELETE FROM batch_ledger_transactions")
                connection.execute("DELETE FROM batch_receipt_items")
                connection.execute("DELETE FROM batch_review_receipts")
                connection.execute("DELETE FROM ledger_entries")
                connection.execute("DELETE FROM receipt_executions")
                connection.execute("DELETE FROM ledger_transactions")
                connection.execute("DELETE FROM authorization_records")
                connection.execute("DELETE FROM clarification_sessions")
                connection.execute("DELETE FROM paynow_lookups")
                connection.execute("DELETE FROM paynow_proxies")
                connection.execute("DELETE FROM review_receipts")
                connection.execute("DELETE FROM action_attempts")
                connection.execute("DELETE FROM beneficiaries")
                connection.execute("DELETE FROM accounts")
                self._insert_seed(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_state()

    def get_state(self) -> dict[str, Any]:
        with self._connect() as connection:
            accounts = connection.execute(
                """
                SELECT a.account_id, a.owner_display_name, a.label, a.currency,
                       a.balance_minor, a.is_user_source,
                       (
                         SELECT substr(p.canonical_proxy, 1, 4) || ' ' ||
                                substr(p.canonical_proxy, 5, 4)
                         FROM beneficiaries b
                         JOIN paynow_proxies p
                           ON p.beneficiary_id = b.beneficiary_id
                         WHERE b.destination_account_id = a.account_id
                           AND p.is_active = 1
                         ORDER BY p.canonical_proxy
                         LIMIT 1
                       ) AS paynow_proxy
                FROM accounts a
                ORDER BY a.is_user_source DESC, a.label COLLATE NOCASE
                """
            ).fetchall()
            transactions = connection.execute(
                """
                SELECT t.transaction_id, t.request_id, t.kind, t.amount_minor,
                       t.currency, t.created_at,
                       source.label AS source_label,
                       destination.label AS destination_label
                FROM ledger_transactions t
                JOIN ledger_entries source_entry
                  ON source_entry.transaction_id = t.transaction_id
                 AND source_entry.delta_minor < 0
                JOIN accounts source
                  ON source.account_id = source_entry.account_id
                JOIN ledger_entries destination_entry
                  ON destination_entry.transaction_id = t.transaction_id
                 AND destination_entry.delta_minor > 0
                JOIN accounts destination
                  ON destination.account_id = destination_entry.account_id
                ORDER BY t.created_at DESC
                """
            ).fetchall()
            batch_transactions = connection.execute(
                """
                SELECT bt.batch_transaction_id AS transaction_id, bt.request_id,
                       'BENEFICIARY_TRANSFER_BATCH' AS kind,
                       bri.amount_minor, bt.currency, bt.created_at,
                       source.label AS source_label,
                       destination.label AS destination_label
                FROM batch_ledger_transactions bt
                JOIN batch_ledger_entries source_entry
                  ON source_entry.batch_transaction_id = bt.batch_transaction_id
                 AND source_entry.delta_minor < 0
                JOIN accounts source
                  ON source.account_id = source_entry.account_id
                JOIN batch_receipt_items bri
                  ON bri.receipt_id = (
                       SELECT bre.receipt_id
                       FROM batch_receipt_executions bre
                       WHERE bre.batch_transaction_id = bt.batch_transaction_id
                     )
                JOIN accounts destination
                  ON destination.account_id = bri.destination_account_id
                WHERE EXISTS (
                  SELECT 1 FROM batch_ledger_entries credit
                  WHERE credit.batch_transaction_id = bt.batch_transaction_id
                    AND credit.item_position = bri.position
                )
                ORDER BY bt.created_at DESC, bri.position
                """
            ).fetchall()
            transactions.extend(batch_transactions)
            entries = connection.execute(
                """
                SELECT entry_id, transaction_id, account_id, delta_minor
                FROM ledger_entries
                ORDER BY rowid
                """
            ).fetchall()
            batch_entries = connection.execute(
                """
                SELECT entry_id, batch_transaction_id AS transaction_id,
                       account_id, delta_minor
                FROM batch_ledger_entries
                ORDER BY rowid
                """
            ).fetchall()
            entries.extend(batch_entries)
        return {
            "accounts": [dict(row) for row in accounts],
            "transactions": [dict(row) for row in transactions],
            "ledger_entries": [dict(row) for row in entries],
        }

    def resolve_source(self, label: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT account_id, owner_display_name, label, currency,
                       balance_minor
                FROM accounts
                WHERE normalized_label = ? AND is_user_source = 1
                """,
                (_normalized(label),),
            ).fetchone()
        return dict(row) if row is not None else None

    def resolve_beneficiary(self, label: str) -> Resolution:
        normalized = _normalized(label)
        if not normalized:
            return Resolution("missing", ())
        with self._connect() as connection:
            exact = connection.execute(
                """
                SELECT beneficiary_id, display_name, destination_account_id,
                       masked_destination
                FROM beneficiaries
                WHERE directory_kind = 'SAVED'
                  AND (normalized_alias = ? OR lower(display_name) = ?)
                ORDER BY display_name COLLATE NOCASE
                """,
                (normalized, normalized),
            ).fetchall()
            if exact:
                return Resolution("exact", tuple(dict(row) for row in exact))
            saved = connection.execute(
                """
                SELECT beneficiary_id, display_name, normalized_alias,
                       destination_account_id, masked_destination
                FROM beneficiaries
                WHERE directory_kind = 'SAVED'
                """
            ).fetchall()
            query_tokens = name_tokens(normalized)
            if len(query_tokens) == 1:
                token_matches = []
                for row in saved:
                    if query_tokens[0] in name_tokens(row["normalized_alias"]):
                        match = dict(row)
                        match.pop("normalized_alias")
                        token_matches.append(match)
                if token_matches:
                    token_matches.sort(key=lambda item: item["display_name"].casefold())
                    return Resolution("candidates", tuple(token_matches))

            close_matches = rank_name_candidates(normalized, saved)
            if close_matches:
                matches = []
                for row in close_matches:
                    row.pop("normalized_alias")
                    matches.append(row)
                return Resolution("close", tuple(matches))
            candidates = connection.execute(
                """
                SELECT beneficiary_id, display_name, destination_account_id,
                       masked_destination
                FROM beneficiaries
                WHERE directory_kind = 'SAVED' AND lower(display_name) LIKE ?
                ORDER BY display_name COLLATE NOCASE
                """,
                (f"{normalized}%",),
            ).fetchall()
        if candidates:
            return Resolution("candidates", tuple(dict(row) for row in candidates))
        phonetic_matches = rank_phonetic_candidates(normalized, saved)
        if phonetic_matches:
            matches = []
            for row in phonetic_matches:
                row.pop("normalized_alias")
                matches.append(row)
            return Resolution("phonetic", tuple(matches))
        return Resolution("missing", ())

    def list_saved_beneficiaries(self) -> tuple[dict[str, Any], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT beneficiary_id, display_name, normalized_alias,
                       destination_account_id, masked_destination
                FROM beneficiaries
                WHERE directory_kind = 'SAVED'
                ORDER BY display_name COLLATE NOCASE
                """
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def get_attempt(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT original_text, response_json FROM action_attempts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "original_text": row["original_text"],
            "response": json.loads(row["response_json"]),
        }

    def get_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_batch_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM batch_review_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            if row is None:
                return None
            items = connection.execute(
                """
                SELECT position, beneficiary_id, destination_account_id,
                       recipient_display_name, amount_minor,
                       recipient_evidence, amount_evidence
                FROM batch_receipt_items
                WHERE receipt_id = ? ORDER BY position
                """,
                (receipt_id,),
            ).fetchall()
        result = dict(row)
        result["items"] = [dict(item) for item in items]
        return result

    def create_pending_batch_receipt(
        self,
        draft: BatchReceiptDraft,
        *,
        raw_model_output: str,
        validated_intent: Mapping[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        created_at = now or self._clock()
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT original_text, response_json FROM action_attempts WHERE request_id = ?",
                    (draft.request_id,),
                ).fetchone()
                if existing is not None:
                    if existing["original_text"] != draft.original_text:
                        raise IdempotencyConflict("IDEMPOTENCY_CONFLICT")
                    connection.rollback()
                    return json.loads(existing["response_json"])
                source = connection.execute(
                    """
                    SELECT account_id, label, currency, balance_minor
                    FROM accounts
                    WHERE account_id = ? AND is_user_source = 1
                    """,
                    (draft.source_account_id,),
                ).fetchone()
                if source is None or source["currency"] != draft.currency:
                    raise ResolutionError("server-owned source resolution failed")
                server_items: list[BatchReceiptItemDraft] = []
                for item in draft.items:
                    beneficiary = connection.execute(
                        """
                        SELECT beneficiary_id, display_name, destination_account_id
                        FROM beneficiaries
                        WHERE beneficiary_id = ? AND directory_kind = 'SAVED'
                        """,
                        (item.beneficiary_id,),
                    ).fetchone()
                    if beneficiary is None:
                        raise ResolutionError(
                            "server-owned beneficiary resolution failed"
                        )
                    if item.destination_account_id != beneficiary["destination_account_id"]:
                        raise ResolutionError("server-owned destination resolution failed")
                    server_items.append(
                        BatchReceiptItemDraft(
                            item.position,
                            beneficiary["beneficiary_id"],
                            beneficiary["destination_account_id"],
                            beneficiary["display_name"],
                            item.amount_minor,
                            item.recipient_evidence,
                            item.amount_evidence,
                        )
                    )
                server_draft = BatchReceiptDraft(
                    request_id=draft.request_id,
                    original_text=draft.original_text,
                    user_id=draft.user_id,
                    source_account_id=source["account_id"],
                    currency=source["currency"],
                    items=tuple(server_items),
                )
                record = build_batch_receipt_record(
                    server_draft,
                    now=created_at,
                    receipt_id=f"batch_receipt_{uuid.uuid4().hex}",
                    nonce=f"batch_nonce_{uuid.uuid4().hex}",
                )
                if source["balance_minor"] < record["total_minor"]:
                    raise InsufficientFunds("INSUFFICIENT_FUNDS")
                public_receipt = {
                    "receipt_id": record["receipt_id"],
                    "receipt_digest": record["receipt_digest"],
                    "source_label": source["label"],
                    "items": [
                        {
                            "recipient_display_name": item["recipient_display_name"],
                            "amount_minor": item["amount_minor"],
                        }
                        for item in record["items"]
                    ],
                    "total_minor": record["total_minor"],
                    "currency": record["currency"],
                    "expires_at": record["expires_at"],
                }
                response = {
                    "outcome": "BATCH_CONFIRMATION_REQUEST",
                    "request_id": draft.request_id,
                    "reason_code": "BATCH_CONFIRMATION_REQUIRED",
                    "validated_intent": dict(validated_intent),
                    "batch_receipt": public_receipt,
                }
                connection.execute(
                    """
                    INSERT INTO action_attempts (
                      request_id, original_text, raw_model_output,
                      validated_intent_json, outcome, reason_code,
                      response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft.request_id,
                        draft.original_text,
                        raw_model_output,
                        json.dumps(validated_intent, ensure_ascii=False),
                        response["outcome"],
                        response["reason_code"],
                        json.dumps(response, ensure_ascii=False),
                        record["created_at"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO batch_review_receipts (
                      receipt_id, request_id, user_id, source_account_id,
                      total_minor, currency, original_text, expires_at, nonce,
                      canonical_json, receipt_digest, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["receipt_id"], draft.request_id, draft.user_id,
                        record["source_account_id"], record["total_minor"],
                        record["currency"], record["original_text"],
                        record["expires_at"], record["nonce"],
                        record["canonical_json"], record["receipt_digest"],
                        record["status"], record["created_at"],
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO batch_receipt_items (
                      receipt_id, position, beneficiary_id,
                      destination_account_id, recipient_display_name,
                      amount_minor, recipient_evidence, amount_evidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            record["receipt_id"], item["position"],
                            item["beneficiary_id"], item["destination_account_id"],
                            item["recipient_display_name"], item["amount_minor"],
                            item["recipient_evidence"], item["amount_evidence"],
                        )
                        for item in record["items"]
                    ],
                )
                connection.commit()
                return response
            except Exception:
                connection.rollback()
                raise

    def approve_batch_receipt(
        self,
        receipt_id: str,
        receipt_digest: str,
        user_id: str = DEMO_USER_ID,
        *,
        assertion: DeviceAssertion | None,
        context: ApprovalContext | None,
        out_of_band_code: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                approved_at = now or self._clock()
                if approved_at.tzinfo is None or approved_at.utcoffset() is None:
                    raise ValueError("now must be timezone-aware")
                receipt = connection.execute(
                    "SELECT * FROM batch_review_receipts WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()
                if receipt is None:
                    raise ReceiptRejected("BATCH_RECEIPT_NOT_FOUND")
                row = dict(receipt)
                if row["user_id"] != user_id:
                    raise ReceiptRejected("BATCH_RECEIPT_USER_MISMATCH")
                items = connection.execute(
                    """
                    SELECT position, beneficiary_id, destination_account_id,
                           recipient_display_name, amount_minor,
                           recipient_evidence, amount_evidence
                    FROM batch_receipt_items
                    WHERE receipt_id = ? ORDER BY position
                    """,
                    (receipt_id,),
                ).fetchall()
                item_dicts = [dict(item) for item in items]
                positions = [item["position"] for item in item_dicts]
                expected_payload = {
                    "contract_version": 1,
                    "currency": row["currency"],
                    "expires_at": row["expires_at"],
                    "items": item_dicts,
                    "nonce": row["nonce"],
                    "original_text": row["original_text"],
                    "receipt_id": row["receipt_id"],
                    "request_id": row["request_id"],
                    "source_account_id": row["source_account_id"],
                    "total_minor": row["total_minor"],
                    "user_id": row["user_id"],
                }
                try:
                    payload = json.loads(row["canonical_json"])
                except (TypeError, json.JSONDecodeError):
                    payload = None
                rebuilt = canonical_json(expected_payload)
                if (
                    payload != expected_payload
                    or row["canonical_json"] != rebuilt
                    or row["receipt_digest"] != digest_canonical(rebuilt)
                    or not 2 <= len(item_dicts) <= 4
                    or positions != list(range(len(item_dicts)))
                    or len({item["beneficiary_id"] for item in item_dicts})
                    != len(item_dicts)
                ):
                    raise ReceiptRejected("BATCH_RECEIPT_TAMPERED")
                if row["receipt_digest"] != receipt_digest:
                    raise ReceiptRejected("BATCH_RECEIPT_DIGEST_MISMATCH")
                execution = connection.execute(
                    """
                    SELECT e.batch_transaction_id, a.response_json
                    FROM batch_receipt_executions e
                    JOIN action_attempts a ON a.request_id = ?
                    WHERE e.receipt_id = ?
                    """,
                    (row["request_id"], receipt_id),
                ).fetchone()
                approval = self._require_device_signature(
                    connection,
                    assertion,
                    context,
                    purpose="APPROVE_BATCH",
                    receipt_id=receipt_id,
                    receipt_digest=receipt_digest,
                    user_id=user_id,
                    executed=execution is not None,
                    now=approved_at,
                )
                if execution is not None:
                    replay = json.loads(execution["response_json"])
                    replay["idempotent_replay"] = True
                    connection.rollback()
                    return replay
                if row["status"] == "EXPIRED":
                    raise ReceiptRejected("BATCH_RECEIPT_EXPIRED")
                if row["status"] != "AWAITING_CONFIRMATION":
                    raise ReceiptRejected("BATCH_RECEIPT_INVALID_STATUS")
                expires_at = datetime.fromisoformat(row["expires_at"])
                if approved_at.astimezone(timezone.utc) >= expires_at.astimezone(
                    timezone.utc
                ):
                    connection.execute(
                        "UPDATE batch_review_receipts SET status = 'EXPIRED' WHERE receipt_id = ?",
                        (receipt_id,),
                    )
                    raise ReceiptRejected(
                        "BATCH_RECEIPT_EXPIRED", persist_status=True
                    )
                validation = record_validation(
                    connection,
                    kind="batch",
                    receipt_id=receipt_id,
                    stage="EXECUTION",
                    now=approved_at,
                )
                if validation.verdict == "FROZEN":
                    raise ReceiptRejected("VALIDATION_FROZEN", persist_status=True)
                self._enforce_policy(
                    connection,
                    user_id=user_id,
                    receipt_id=receipt_id,
                    receipt_digest=row["receipt_digest"],
                    item_amounts=tuple(item["amount_minor"] for item in item_dicts),
                    out_of_band_code=out_of_band_code,
                    now=approved_at,
                )
                source = connection.execute(
                    """
                    SELECT account_id, label, balance_minor, currency
                    FROM accounts
                    WHERE account_id = ? AND is_user_source = 1
                    """,
                    (row["source_account_id"],),
                ).fetchone()
                if source is None or source["currency"] != row["currency"]:
                    raise ReceiptRejected("BATCH_RECEIPT_TAMPERED")
                if sum(item["amount_minor"] for item in item_dicts) != row["total_minor"]:
                    raise ReceiptRejected("BATCH_RECEIPT_TAMPERED")
                resolved_actions: list[dict[str, Any]] = []
                for item in item_dicts:
                    beneficiary = connection.execute(
                        """
                        SELECT beneficiary_id, destination_account_id,
                               display_name, masked_destination
                        FROM beneficiaries
                        WHERE beneficiary_id = ? AND directory_kind = 'SAVED'
                        """,
                        (item["beneficiary_id"],),
                    ).fetchone()
                    if (
                        beneficiary is None
                        or beneficiary["destination_account_id"]
                        != item["destination_account_id"]
                        or beneficiary["display_name"]
                        != item["recipient_display_name"]
                    ):
                        raise ReceiptRejected("BATCH_RECEIPT_TAMPERED")
                    resolved_actions.append(
                        {
                            "kind": "BENEFICIARY_TRANSFER",
                            "source_account_id": row["source_account_id"],
                            "beneficiary_id": item["beneficiary_id"],
                            "destination_account_id": item["destination_account_id"],
                            "recipient_display_name": item["recipient_display_name"],
                            "masked_destination": beneficiary["masked_destination"],
                            "amount_minor": item["amount_minor"],
                            "currency": row["currency"],
                        }
                    )
                if source["balance_minor"] < row["total_minor"]:
                    raise InsufficientFunds("INSUFFICIENT_FUNDS")
                executed_iso = approved_at.astimezone(timezone.utc).isoformat()
                batch_transaction_id = f"batch_txn_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO batch_ledger_transactions (
                      batch_transaction_id, request_id, total_minor,
                      currency, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        batch_transaction_id, row["request_id"],
                        row["total_minor"], row["currency"], executed_iso,
                    ),
                )
                connection.execute(
                    "UPDATE accounts SET balance_minor = balance_minor - ? WHERE account_id = ?",
                    (row["total_minor"], row["source_account_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO batch_ledger_entries (
                      entry_id, batch_transaction_id, account_id,
                      item_position, delta_minor
                    ) VALUES (?, ?, ?, NULL, ?)
                    """,
                    (
                        f"batch_entry_{uuid.uuid4().hex}", batch_transaction_id,
                        row["source_account_id"], -row["total_minor"],
                    ),
                )
                for item in item_dicts:
                    connection.execute(
                        "UPDATE accounts SET balance_minor = balance_minor + ? WHERE account_id = ?",
                        (item["amount_minor"], item["destination_account_id"]),
                    )
                    connection.execute(
                        """
                        INSERT INTO batch_ledger_entries (
                          entry_id, batch_transaction_id, account_id,
                          item_position, delta_minor
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            f"batch_entry_{uuid.uuid4().hex}", batch_transaction_id,
                            item["destination_account_id"], item["position"],
                            item["amount_minor"],
                        ),
                    )
                entry_sum = connection.execute(
                    "SELECT SUM(delta_minor) FROM batch_ledger_entries WHERE batch_transaction_id = ?",
                    (batch_transaction_id,),
                ).fetchone()[0]
                if entry_sum != 0:
                    raise sqlite3.IntegrityError("batch ledger entries are not balanced")
                connection.execute(
                    """
                    INSERT INTO batch_receipt_executions (
                      receipt_id, batch_transaction_id, executed_at
                    ) VALUES (?, ?, ?)
                    """,
                    (receipt_id, batch_transaction_id, executed_iso),
                )
                connection.execute(
                    "UPDATE batch_review_receipts SET status = 'EXECUTED' WHERE receipt_id = ?",
                    (receipt_id,),
                )
                attempt = connection.execute(
                    "SELECT validated_intent_json FROM action_attempts WHERE request_id = ?",
                    (row["request_id"],),
                ).fetchone()
                validated_intent = (
                    json.loads(attempt["validated_intent_json"])
                    if attempt is not None and attempt["validated_intent_json"]
                    else {}
                )
                response = {
                    "outcome": "EXECUTED_SYNTHETIC_BATCH",
                    "request_id": row["request_id"],
                    "batch_transaction_id": batch_transaction_id,
                    "amount_minor": row["total_minor"],
                    "currency": row["currency"],
                    "validated_intent": validated_intent,
                    "resolved_actions": resolved_actions,
                }
                if approval is not None:
                    response["approval"] = dict(approval)
                connection.execute(
                    """
                    UPDATE action_attempts
                    SET outcome = ?, reason_code = NULL, response_json = ?
                    WHERE request_id = ?
                    """,
                    (
                        response["outcome"],
                        json.dumps(response, ensure_ascii=False),
                        row["request_id"],
                    ),
                )
                connection.commit()
                return response
            except ReceiptRejected as exc:
                if exc.persist_status:
                    connection.commit()
                else:
                    connection.rollback()
                raise
            except Exception:
                connection.rollback()
                raise

    def create_pending_receipts(
        self,
        drafts: tuple[ReceiptDraft, ...],
        *,
        raw_model_output: str,
        validated_intent: Mapping[str, Any],
        reason_code: str,
        response_base: Mapping[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not drafts:
            raise ValueError("at least one receipt draft is required")
        first = drafts[0]
        common = (
            first.request_id,
            first.original_text,
            first.user_id,
            first.source_account_id,
            first.amount_minor,
            first.currency,
        )
        if any(
            (
                draft.request_id,
                draft.original_text,
                draft.user_id,
                draft.source_account_id,
                draft.amount_minor,
                draft.currency,
            )
            != common
            for draft in drafts[1:]
        ):
            raise ValueError("receipt candidates must describe one request")

        created_at = now or self._clock()
        group_id = f"group_{uuid.uuid4().hex}" if len(drafts) > 1 else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT original_text, response_json
                    FROM action_attempts WHERE request_id = ?
                    """,
                    (first.request_id,),
                ).fetchone()
                if existing is not None:
                    if existing["original_text"] != first.original_text:
                        raise IdempotencyConflict("IDEMPOTENCY_CONFLICT")
                    connection.rollback()
                    return json.loads(existing["response_json"])

                source = connection.execute(
                    """
                    SELECT account_id, label, currency
                    FROM accounts
                    WHERE account_id = ? AND is_user_source = 1
                    """,
                    (first.source_account_id,),
                ).fetchone()
                if source is None or source["currency"] != first.currency:
                    raise ResolutionError("server-owned source resolution failed")

                records: list[dict[str, Any]] = []
                for draft in drafts:
                    beneficiary = connection.execute(
                        """
                        SELECT beneficiary_id, display_name
                        FROM beneficiaries WHERE beneficiary_id = ?
                        """,
                        (draft.beneficiary_id,),
                    ).fetchone()
                    if beneficiary is None:
                        raise ResolutionError(
                            "server-owned beneficiary resolution failed"
                        )
                    server_draft = ReceiptDraft(
                        request_id=draft.request_id,
                        original_text=draft.original_text,
                        user_id=draft.user_id,
                        source_account_id=draft.source_account_id,
                        beneficiary_id=draft.beneficiary_id,
                        recipient_display_name=beneficiary["display_name"],
                        amount_minor=draft.amount_minor,
                        currency=draft.currency,
                        destination_kind=draft.destination_kind,
                        destination_reference=draft.destination_reference,
                        destination_account_id=draft.destination_account_id,
                        lookup_reference=draft.lookup_reference,
                        evidence_bundle=draft.evidence_bundle,
                        contract_version=draft.contract_version,
                    )
                    record = build_receipt_record(
                        server_draft,
                        now=created_at,
                        receipt_id=f"receipt_{uuid.uuid4().hex}",
                        nonce=f"nonce_{uuid.uuid4().hex}",
                    )
                    record["clarification_group_id"] = group_id
                    records.append(record)

                choices = [
                    {
                        "receipt_id": record["receipt_id"],
                        "receipt_digest": record["receipt_digest"],
                        "source_label": source["label"],
                        "recipient_display_name": record[
                            "recipient_display_name"
                        ],
                        "amount_minor": record["amount_minor"],
                        "currency": record["currency"],
                        "expires_at": record["expires_at"],
                        "destination_kind": record.get("destination_kind"),
                        "destination_reference": record.get(
                            "destination_reference"
                        ),
                        "lookup_reference": record.get("lookup_reference"),
                        "payment_reference": validated_intent.get(
                            "payment_reference"
                        ),
                    }
                    for record in records
                ]
                response = {
                    **dict(response_base),
                    "request_id": first.request_id,
                    "reason_code": reason_code,
                    "validated_intent": dict(validated_intent),
                    "receipt_choices": choices,
                }
                serialized_response = json.dumps(response, ensure_ascii=False)
                connection.execute(
                    """
                    INSERT INTO action_attempts (
                      request_id, original_text, raw_model_output,
                      validated_intent_json, outcome, reason_code,
                      response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        first.request_id,
                        first.original_text,
                        raw_model_output,
                        json.dumps(validated_intent, ensure_ascii=False),
                        response["outcome"],
                        reason_code,
                        serialized_response,
                        created_at.astimezone(timezone.utc).isoformat(),
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO review_receipts (
                      receipt_id, clarification_group_id, request_id, user_id,
                      source_account_id, beneficiary_id, recipient_display_name,
                      amount_minor, currency, original_text, expires_at, nonce,
                      canonical_json, receipt_digest, status, created_at,
                      contract_version, destination_kind, destination_reference,
                      destination_account_id, lookup_reference, evidence_bundle_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            record["receipt_id"],
                            record["clarification_group_id"],
                            record["request_id"],
                            record["user_id"],
                            record["source_account_id"],
                            record["beneficiary_id"],
                            record["recipient_display_name"],
                            record["amount_minor"],
                            record["currency"],
                            record["original_text"],
                            record["expires_at"],
                            record["nonce"],
                            record["canonical_json"],
                            record["receipt_digest"],
                            record["status"],
                            record["created_at"],
                            record["contract_version"],
                            record.get("destination_kind"),
                            record.get("destination_reference"),
                            record.get("destination_account_id"),
                            record.get("lookup_reference"),
                            record.get("evidence_bundle_json"),
                        )
                        for record in records
                    ],
                )
                connection.commit()
                return response
            except Exception:
                connection.rollback()
                raise

    def create_amount_clarification(
        self,
        *,
        request_id: str,
        original_text: str,
        user_id: str,
        partial_action: Mapping[str, Any],
        evidence_bundle: Sequence[Mapping[str, Any]],
        raw_model_output: str,
        validated_intent: Mapping[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        created_at = now or self._clock()
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT original_text, response_json FROM action_attempts WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if existing is not None:
                    if existing["original_text"] != original_text:
                        raise IdempotencyConflict("IDEMPOTENCY_CONFLICT")
                    connection.rollback()
                    return json.loads(existing["response_json"])

                source = connection.execute(
                    """
                    SELECT account_id, label, currency FROM accounts
                    WHERE account_id = ? AND is_user_source = 1
                    """,
                    (partial_action.get("source_account_id"),),
                ).fetchone()
                if source is None:
                    raise ResolutionError("server-owned source resolution failed")
                beneficiary_ids = partial_action.get("beneficiary_ids")
                if not isinstance(beneficiary_ids, list) or not beneficiary_ids:
                    raise ResolutionError("server-owned beneficiary resolution failed")
                placeholders = ",".join("?" for _ in beneficiary_ids)
                rows = connection.execute(
                    f"""
                    SELECT beneficiary_id, display_name FROM beneficiaries
                    WHERE beneficiary_id IN ({placeholders})
                    """,
                    tuple(beneficiary_ids),
                ).fetchall()
                by_id = {row["beneficiary_id"]: row for row in rows}
                if set(by_id) != set(beneficiary_ids):
                    raise ResolutionError("server-owned beneficiary resolution failed")
                display_names = [by_id[item]["display_name"] for item in beneficiary_ids]

                clarification_id = f"clarification_{uuid.uuid4().hex}"
                created_iso = created_at.astimezone(timezone.utc).isoformat()
                expires_at = (
                    created_at.astimezone(timezone.utc) + timedelta(minutes=5)
                ).isoformat()
                stored_partial = {
                    "kind": partial_action.get("kind", "BENEFICIARY_TRANSFER"),
                    "source_account_id": source["account_id"],
                    "beneficiary_ids": list(beneficiary_ids),
                    "recipient_display_names": display_names,
                    "currency": source["currency"],
                    "payment_reference": partial_action.get("payment_reference"),
                    "reason_code": "MISSING_AMOUNT",
                }
                for key in (
                    "destination_kind",
                    "destination_reference",
                    "destination_account_id",
                    "lookup_reference",
                ):
                    if partial_action.get(key) is not None:
                        stored_partial[key] = partial_action[key]
                clarification = {
                    "clarification_id": clarification_id,
                    "missing_field": "amount_minor",
                    "locked_facts": {
                        "source_label": source["label"],
                        "recipient_display_names": display_names,
                        "currency": source["currency"],
                        **(
                            {
                                "destination_reference": partial_action[
                                    "destination_reference"
                                ]
                            }
                            if partial_action.get("destination_reference")
                            else {}
                        ),
                    },
                    "expires_at": expires_at,
                }
                response = {
                    "outcome": "CLARIFICATION_REQUEST",
                    "request_id": request_id,
                    "reason_code": "MISSING_AMOUNT",
                    "question": "How much would you like to send?",
                    "validated_intent": dict(validated_intent),
                    "clarification": clarification,
                }
                response_json = json.dumps(response, ensure_ascii=False)
                connection.execute(
                    """
                    INSERT INTO action_attempts (
                      request_id, original_text, raw_model_output,
                      validated_intent_json, outcome, reason_code,
                      response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        original_text,
                        raw_model_output,
                        json.dumps(validated_intent, ensure_ascii=False),
                        response["outcome"],
                        response["reason_code"],
                        response_json,
                        created_iso,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO clarification_sessions (
                      clarification_id, request_id, user_id, kind,
                      partial_intent_json, evidence_bundle_json, missing_field,
                      status, expires_at, created_at
                    ) VALUES (?, ?, ?, 'BENEFICIARY_TRANSFER', ?, ?,
                              'amount_minor', 'AWAITING_RESPONSE', ?, ?)
                    """,
                    (
                        clarification_id,
                        request_id,
                        user_id,
                        json.dumps(stored_partial, ensure_ascii=False),
                        canonical_json(list(evidence_bundle)),
                        expires_at,
                        created_iso,
                    ),
                )
                connection.commit()
                return response
            except Exception:
                connection.rollback()
                raise

    def complete_amount_clarification(
        self,
        clarification_id: str,
        response_request_id: str,
        response_text: str,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                completed_at = now or self._clock()
                if completed_at.tzinfo is None or completed_at.utcoffset() is None:
                    raise ValueError("now must be timezone-aware")
                existing = connection.execute(
                    "SELECT original_text, response_json FROM action_attempts WHERE request_id = ?",
                    (response_request_id,),
                ).fetchone()
                if existing is not None:
                    if existing["original_text"] != response_text:
                        raise IdempotencyConflict("IDEMPOTENCY_CONFLICT")
                    connection.rollback()
                    return json.loads(existing["response_json"])

                session = connection.execute(
                    "SELECT * FROM clarification_sessions WHERE clarification_id = ?",
                    (clarification_id,),
                ).fetchone()
                if session is None:
                    raise ClarificationRejected("CLARIFICATION_NOT_FOUND")
                if session["user_id"] != user_id:
                    raise ClarificationRejected("CLARIFICATION_USER_MISMATCH")
                if session["status"] != "AWAITING_RESPONSE":
                    reason = {
                        "COMPLETED": "CLARIFICATION_ALREADY_COMPLETED",
                        "EXPIRED": "CLARIFICATION_EXPIRED",
                        "SUPERSEDED": "CLARIFICATION_SUPERSEDED",
                    }[session["status"]]
                    raise ClarificationRejected(reason)
                expires_at = datetime.fromisoformat(session["expires_at"])
                if completed_at.astimezone(timezone.utc) >= expires_at.astimezone(
                    timezone.utc
                ):
                    connection.execute(
                        "UPDATE clarification_sessions SET status = 'EXPIRED' WHERE clarification_id = ?",
                        (clarification_id,),
                    )
                    connection.commit()
                    raise ClarificationRejected("CLARIFICATION_EXPIRED")
                try:
                    amount_minor = parse_sgd_amount(
                        response_text,
                        require_full_match=True,
                    )
                except GroundingError as exc:
                    raise ClarificationRejected(exc.reason_code) from exc

                partial = json.loads(session["partial_intent_json"])
                evidence = json.loads(session["evidence_bundle_json"])
                evidence.append(
                    {"turn": 2, "text": response_text, "amount": response_text}
                )
                source = connection.execute(
                    "SELECT account_id, label, currency FROM accounts WHERE account_id = ? AND is_user_source = 1",
                    (partial["source_account_id"],),
                ).fetchone()
                if source is None or source["currency"] != partial["currency"]:
                    raise ResolutionError("server-owned source resolution failed")

                records: list[dict[str, Any]] = []
                for beneficiary_id in partial["beneficiary_ids"]:
                    beneficiary = connection.execute(
                        "SELECT beneficiary_id, display_name FROM beneficiaries WHERE beneficiary_id = ?",
                        (beneficiary_id,),
                    ).fetchone()
                    if beneficiary is None:
                        raise ResolutionError("server-owned beneficiary resolution failed")
                    destination_kind = partial.get(
                        "destination_kind", "SAVED_BENEFICIARY"
                    )
                    if destination_kind == "PAYNOW_MOBILE":
                        lookup = self._get_valid_paynow_lookup(
                            connection,
                            lookup_reference=partial.get("lookup_reference"),
                            destination_reference=partial.get(
                                "destination_reference"
                            ),
                            recipient_display_name=beneficiary["display_name"],
                            beneficiary_id=beneficiary["beneficiary_id"],
                            destination_account_id=partial.get(
                                "destination_account_id"
                            ),
                            now=completed_at,
                        )
                        if lookup is None:
                            raise ClarificationRejected("PAYNOW_LOOKUP_MISMATCH")
                    record = build_receipt_record(
                        ReceiptDraft(
                            request_id=response_request_id,
                            original_text=response_text,
                            user_id=user_id,
                            source_account_id=source["account_id"],
                            beneficiary_id=beneficiary["beneficiary_id"],
                            recipient_display_name=beneficiary["display_name"],
                            amount_minor=amount_minor,
                            currency=source["currency"],
                            destination_kind=destination_kind,
                            destination_reference=partial.get(
                                "destination_reference"
                            ),
                            destination_account_id=partial.get(
                                "destination_account_id"
                            ),
                            lookup_reference=partial.get("lookup_reference"),
                            evidence_bundle=tuple(evidence),
                        ),
                        now=completed_at,
                        receipt_id=f"receipt_{uuid.uuid4().hex}",
                        nonce=f"nonce_{uuid.uuid4().hex}",
                    )
                    records.append(record)

                group_id = f"group_{uuid.uuid4().hex}" if len(records) > 1 else None
                choices = [
                    {
                        "receipt_id": record["receipt_id"],
                        "receipt_digest": record["receipt_digest"],
                        "source_label": source["label"],
                        "recipient_display_name": record["recipient_display_name"],
                        "amount_minor": record["amount_minor"],
                        "currency": record["currency"],
                        "expires_at": record["expires_at"],
                        "destination_kind": record["destination_kind"],
                        "destination_reference": record["destination_reference"],
                        "lookup_reference": record["lookup_reference"],
                        "payment_reference": partial.get("payment_reference"),
                    }
                    for record in records
                ]
                combined_intent = {
                    **partial,
                    "amount_minor": amount_minor,
                    "amount_evidence": response_text,
                }
                response = {
                    "outcome": "CLARIFICATION_REQUEST",
                    "request_id": response_request_id,
                    "reason_code": "AMOUNT_CLARIFICATION_COMPLETED",
                    "question": "Confirm this exact transfer?",
                    "validated_intent": combined_intent,
                    "receipt_choices": choices,
                }
                response_json = json.dumps(response, ensure_ascii=False)
                completed_iso = completed_at.astimezone(timezone.utc).isoformat()
                connection.execute(
                    """
                    INSERT INTO action_attempts (
                      request_id, original_text, raw_model_output,
                      validated_intent_json, outcome, reason_code,
                      response_json, created_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        response_request_id,
                        response_text,
                        json.dumps(combined_intent, ensure_ascii=False),
                        response["outcome"],
                        response["reason_code"],
                        response_json,
                        completed_iso,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO review_receipts (
                      receipt_id, clarification_group_id, request_id, user_id,
                      source_account_id, beneficiary_id, recipient_display_name,
                      amount_minor, currency, original_text, expires_at, nonce,
                      canonical_json, receipt_digest, status, created_at,
                      contract_version, destination_kind, destination_reference,
                      destination_account_id, lookup_reference, evidence_bundle_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            record["receipt_id"], group_id, record["request_id"],
                            record["user_id"], record["source_account_id"],
                            record["beneficiary_id"], record["recipient_display_name"],
                            record["amount_minor"], record["currency"],
                            record["original_text"], record["expires_at"],
                            record["nonce"], record["canonical_json"],
                            record["receipt_digest"], record["status"],
                            record["created_at"], record["contract_version"],
                            record["destination_kind"],
                            record["destination_reference"],
                            record["destination_account_id"],
                            record["lookup_reference"], record["evidence_bundle_json"],
                        )
                        for record in records
                    ],
                )
                connection.execute(
                    """
                    UPDATE clarification_sessions
                    SET status = 'COMPLETED', response_request_id = ?,
                        response_text = ?, response_json = ?, completed_receipt_id = ?
                    WHERE clarification_id = ?
                    """,
                    (
                        response_request_id,
                        response_text,
                        response_json,
                        records[0]["receipt_id"],
                        clarification_id,
                    ),
                )
                connection.commit()
                return response
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _get_valid_paynow_lookup(
        connection: sqlite3.Connection,
        *,
        lookup_reference: str | None,
        destination_reference: str | None,
        recipient_display_name: str,
        beneficiary_id: str,
        destination_account_id: str | None,
        now: datetime,
    ) -> sqlite3.Row | None:
        if not lookup_reference:
            return None
        lookup = connection.execute(
            """
            SELECT l.*, p.is_active, p.masked_name AS current_masked_name,
                   p.beneficiary_id AS current_beneficiary_id,
                   b.destination_account_id AS current_destination_account_id
            FROM paynow_lookups l
            JOIN paynow_proxies p ON p.canonical_proxy = l.canonical_proxy
            JOIN beneficiaries b ON b.beneficiary_id = p.beneficiary_id
            WHERE l.lookup_reference = ?
            """,
            (lookup_reference,),
        ).fetchone()
        if lookup is None:
            return None
        if now.astimezone(timezone.utc) >= datetime.fromisoformat(
            lookup["expires_at"]
        ).astimezone(timezone.utc):
            raise ClarificationRejected("PAYNOW_LOOKUP_EXPIRED")
        if (
            not lookup["is_active"]
            or lookup["canonical_proxy"] != destination_reference
            or lookup["masked_name"] != recipient_display_name
            or lookup["beneficiary_id"] != beneficiary_id
            or lookup["destination_account_id"] != destination_account_id
            or lookup["current_masked_name"] != recipient_display_name
            or lookup["current_beneficiary_id"] != beneficiary_id
            or lookup["current_destination_account_id"] != destination_account_id
        ):
            return None
        return lookup

    def record_attempt(self, attempt: ActionAttempt) -> dict[str, Any]:
        serialized = json.dumps(dict(attempt.response), ensure_ascii=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT original_text, response_json FROM action_attempts WHERE request_id = ?",
                (attempt.request_id,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                if existing["original_text"] != attempt.original_text:
                    raise IdempotencyConflict("IDEMPOTENCY_CONFLICT")
                return json.loads(existing["response_json"])
            connection.execute(
                """
                INSERT INTO action_attempts (
                  request_id, original_text, raw_model_output,
                  validated_intent_json, outcome, reason_code,
                  response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.request_id,
                    attempt.original_text,
                    attempt.raw_model_output,
                    json.dumps(attempt.validated_intent, ensure_ascii=False)
                    if attempt.validated_intent is not None
                    else None,
                    attempt.outcome,
                    attempt.reason_code,
                    serialized,
                    _now(),
                ),
            )
            connection.commit()
        return dict(attempt.response)

    def _require_device_signature(
        self,
        connection: sqlite3.Connection,
        assertion: DeviceAssertion | None,
        context: ApprovalContext | None,
        *,
        purpose: str,
        receipt_id: str,
        receipt_digest: str,
        user_id: str,
        executed: bool,
        now: datetime,
    ) -> dict[str, Any] | None:
        """Gate every ledger movement on a device signature over this receipt.

        Returns None only for an exact resubmission of an already-accepted
        signature on an executed receipt, so an interrupted client can safely
        retrieve the stored outcome without moving money again.
        """

        if assertion is None or context is None:
            raise ReceiptRejected("SIGNED_APPROVAL_REQUIRED")
        prior = prior_signature(connection, assertion)
        if prior is not None:
            if executed and prior["purpose"] == purpose and prior["receipt_id"] == receipt_id:
                return None
            raise ReceiptRejected("APPROVAL_CHALLENGE_REPLAYED")
        if executed:
            raise ReceiptRejected("RECEIPT_ALREADY_EXECUTED")
        try:
            return consume_approval(
                connection,
                assertion,
                purpose=purpose,
                receipt_id=receipt_id,
                receipt_digest=receipt_digest,
                user_id=user_id,
                context=context,
                now=now,
            )
        except ApprovalRejected as exc:
            # Persist the spent challenge: a failed signature burns it.
            raise ReceiptRejected(exc.reason_code, persist_status=True) from None

    def issue_registration_challenge(
        self, user_id: str = DEMO_USER_ID, *, now: datetime | None = None
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            challenge = issue_challenge(
                connection, purpose="REGISTER", user_id=user_id, now=now or self._clock()
            )
            connection.commit()
            return challenge

    def register_signing_credential(
        self,
        *,
        challenge_id: str,
        credential_kind: str,
        credential_id: str,
        public_key_spki: bytes,
        algorithm: int,
        client_data_json: bytes,
        authenticator_data: bytes,
        signature: bytes | None,
        context: ApprovalContext,
        user_id: str = DEMO_USER_ID,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = register_credential(
                    connection,
                    challenge_id=challenge_id,
                    user_id=user_id,
                    credential_kind=credential_kind,
                    credential_id=credential_id,
                    public_key_spki=public_key_spki,
                    algorithm=algorithm,
                    client_data_json=client_data_json,
                    authenticator_data=authenticator_data,
                    signature=signature,
                    context=context,
                    now=now or self._clock(),
                )
            except ApprovalRejected:
                connection.commit()  # the registration challenge stays spent
                raise
            connection.commit()
            return result

    @staticmethod
    def _enforce_policy(
        connection: sqlite3.Connection,
        *,
        user_id: str,
        receipt_id: str,
        receipt_digest: str,
        item_amounts: tuple[int, ...],
        out_of_band_code: str | None,
        now: datetime,
    ) -> None:
        """Authoritative KYC, limit, velocity, and out-of-band check."""

        try:
            decision = evaluate_policy(
                connection, user_id=user_id, item_amounts=item_amounts, now=now
            )
            if decision.requires_out_of_band:
                verify_out_of_band(
                    connection,
                    user_id=user_id,
                    receipt_id=receipt_id,
                    receipt_digest=receipt_digest,
                    code=out_of_band_code,
                )
        except PolicyRejected as exc:
            raise ReceiptRejected(exc.reason_code, persist_status=exc.persist) from None

    def policy_preview(
        self, item_amounts: tuple[int, ...], *, user_id: str = DEMO_USER_ID
    ) -> dict[str, Any]:
        """Early, non-authoritative check shown with a draft."""

        with self._connect() as connection:
            try:
                decision = evaluate_policy(
                    connection, user_id=user_id, item_amounts=item_amounts, now=self._clock()
                )
            except PolicyRejected as exc:
                return {"allowed": False, "reason_code": exc.reason_code}
            return {"allowed": True, **decision.to_dict()}

    def customer_profile(self, user_id: str = DEMO_USER_ID) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT kyc_status, kyc_expires_at, per_transaction_limit_minor,
                       daily_limit_minor, out_of_band_threshold_minor,
                       velocity_max_transfers, micro_transfer_ceiling_minor,
                       velocity_max_micro_transfers
                FROM customer_profiles WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            return dict(row) if row else {}

    def mock_token_display(
        self, *, user_id: str = DEMO_USER_ID, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """What the simulated hardware token shows: one code per high-value
        receipt awaiting approval, alongside the amount and payee it covers."""

        current = (now or self._clock()).astimezone(timezone.utc)
        with self._connect() as connection:
            threshold = connection.execute(
                "SELECT out_of_band_threshold_minor FROM customer_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if threshold is None:
                return []
            rows = [
                *connection.execute(
                    """
                    SELECT receipt_id, receipt_digest, amount_minor AS total_minor,
                           recipient_display_name AS payee, expires_at
                    FROM review_receipts
                    WHERE user_id = ? AND status = 'AWAITING_CONFIRMATION' AND amount_minor >= ?
                    """,
                    (user_id, threshold[0]),
                ),
                *connection.execute(
                    """
                    SELECT receipt_id, receipt_digest, total_minor,
                           'batch of payees' AS payee, expires_at
                    FROM batch_review_receipts
                    WHERE user_id = ? AND status = 'AWAITING_CONFIRMATION' AND total_minor >= ?
                    """,
                    (user_id, threshold[0]),
                ),
            ]
            return [
                {
                    "receipt_id": row["receipt_id"],
                    "payee": row["payee"],
                    "total_minor": row["total_minor"],
                    "code": mock_token_code(
                        connection, user_id, row["receipt_id"], row["receipt_digest"]
                    ),
                }
                for row in rows
                if datetime.fromisoformat(row["expires_at"]) > current
            ]

    def audit_receipt(
        self, kind: str, receipt_id: str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """Run the independent validation agent over a freshly drafted receipt."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            report = record_validation(
                connection,
                kind=kind,
                receipt_id=receipt_id,
                stage="DRAFT",
                now=now or self._clock(),
            )
            connection.commit()
            return report.to_dict()

    def signing_credentials(self, rp_id: str, user_id: str = DEMO_USER_ID) -> list[dict]:
        with self._connect() as connection:
            return list_credentials(connection, user_id, rp_id)

    def issue_approval_challenge(
        self,
        kind: str,
        receipt_id: str,
        receipt_digest: str,
        *,
        rp_id: str,
        user_id: str = DEMO_USER_ID,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Issue a one-time challenge bound to an awaiting receipt.

        The review facts come from the stored receipt, never from model
        output, so the confirmation overlay shows exactly what is signed.
        """

        issued_at = now or self._clock()
        table, purpose = {
            "single": ("review_receipts", "APPROVE_RECEIPT"),
            "batch": ("batch_review_receipts", "APPROVE_BATCH"),
        }[kind]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    f"SELECT * FROM {table} WHERE receipt_id = ?", (receipt_id,)
                ).fetchone()
                if row is None:
                    raise ReceiptRejected("RECEIPT_NOT_FOUND")
                if row["user_id"] != user_id:
                    raise ReceiptRejected("RECEIPT_USER_MISMATCH")
                if row["receipt_digest"] != receipt_digest:
                    raise ReceiptRejected("RECEIPT_DIGEST_MISMATCH")
                if row["status"] == "EXECUTED":
                    raise ReceiptRejected("RECEIPT_ALREADY_EXECUTED")
                if row["status"] != "AWAITING_CONFIRMATION":
                    raise ReceiptRejected(f"RECEIPT_{row['status']}")
                if issued_at.astimezone(timezone.utc) >= datetime.fromisoformat(
                    row["expires_at"]
                ).astimezone(timezone.utc):
                    raise ReceiptRejected("RECEIPT_EXPIRED")
                validation = record_validation(
                    connection, kind=kind, receipt_id=receipt_id, stage="CHALLENGE", now=issued_at
                )
                if validation.verdict == "FROZEN":
                    connection.commit()  # keep the audit record of the freeze
                    raise ReceiptRejected("VALIDATION_FROZEN")
                credentials = list_credentials(connection, user_id, rp_id)
                if not credentials:
                    raise ReceiptRejected("SIGNING_CREDENTIAL_REQUIRED")
                source = connection.execute(
                    "SELECT label FROM accounts WHERE account_id = ?",
                    (row["source_account_id"],),
                ).fetchone()
                if kind == "single":
                    masked = connection.execute(
                        "SELECT masked_destination FROM beneficiaries WHERE beneficiary_id = ?",
                        (row["beneficiary_id"],),
                    ).fetchone()
                    items = [
                        {
                            "recipient_display_name": row["recipient_display_name"],
                            "masked_destination": masked["masked_destination"] if masked else None,
                            "amount_minor": row["amount_minor"],
                        }
                    ]
                    total_minor = row["amount_minor"]
                else:
                    items = [
                        dict(item)
                        for item in connection.execute(
                            """
                            SELECT i.recipient_display_name, b.masked_destination,
                                   i.amount_minor
                            FROM batch_receipt_items i
                            JOIN beneficiaries b ON b.beneficiary_id = i.beneficiary_id
                            WHERE i.receipt_id = ? ORDER BY i.position
                            """,
                            (receipt_id,),
                        ).fetchall()
                    ]
                    total_minor = row["total_minor"]
                try:
                    decision = evaluate_policy(
                        connection,
                        user_id=user_id,
                        item_amounts=tuple(item["amount_minor"] for item in items),
                        now=issued_at,
                    )
                except PolicyRejected as exc:
                    raise ReceiptRejected(exc.reason_code) from None
                challenge = issue_challenge(
                    connection,
                    purpose=purpose,
                    user_id=user_id,
                    now=issued_at,
                    receipt_id=receipt_id,
                    receipt_digest=receipt_digest,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            **challenge,
            "allow_credentials": credentials,
            "review": {
                "receipt_id": receipt_id,
                "receipt_digest": receipt_digest,
                "source_label": source["label"] if source else None,
                "currency": row["currency"],
                "total_minor": total_minor,
                "items": items,
                "receipt_expires_at": row["expires_at"],
                "validation": validation.to_dict(),
                "requires_out_of_band": decision.requires_out_of_band,
            },
        }

    def approve_receipt(
        self,
        receipt_id: str,
        receipt_digest: str,
        user_id: str,
        *,
        assertion: DeviceAssertion | None,
        context: ApprovalContext | None,
        out_of_band_code: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                approved_at = now or self._clock()
                receipt = connection.execute(
                    "SELECT * FROM review_receipts WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()
                if receipt is None:
                    raise ReceiptRejected("RECEIPT_NOT_FOUND")
                executed = connection.execute(
                    "SELECT 1 FROM receipt_executions WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone() is not None
                approval = self._require_device_signature(
                    connection,
                    assertion,
                    context,
                    purpose="APPROVE_RECEIPT",
                    receipt_id=receipt_id,
                    receipt_digest=receipt_digest,
                    user_id=user_id,
                    executed=executed,
                    now=approved_at,
                )
                response = self._execute_stored_receipt(
                    connection,
                    receipt,
                    supplied_digest=receipt_digest,
                    user_id=user_id,
                    method="USER_CONFIRMATION",
                    now=approved_at,
                    approval=approval,
                    out_of_band_code=out_of_band_code,
                )
                connection.commit()
                return response
            except ReceiptRejected as exc:
                if exc.persist_status:
                    connection.commit()
                else:
                    connection.rollback()
                raise
            except Exception:
                connection.rollback()
                raise

    def _execute_stored_receipt(
        self,
        connection: sqlite3.Connection,
        receipt: sqlite3.Row,
        *,
        supplied_digest: str,
        user_id: str,
        method: str,
        now: datetime,
        approval: Mapping[str, Any] | None = None,
        out_of_band_code: str | None = None,
    ) -> dict[str, Any]:
        row = dict(receipt)
        if row["user_id"] != user_id:
            raise ReceiptRejected("RECEIPT_USER_MISMATCH")
        if row["receipt_digest"] != supplied_digest:
            raise ReceiptRejected("RECEIPT_DIGEST_MISMATCH")

        try:
            payload = json.loads(row["canonical_json"])
        except (TypeError, json.JSONDecodeError):
            raise ReceiptRejected("RECEIPT_DIGEST_MISMATCH") from None
        if not isinstance(payload, dict) or "contract_version" not in payload:
            raise ReceiptRejected("RECEIPT_DIGEST_MISMATCH")
        version = payload["contract_version"]
        if version not in (1, 2):
            raise ReceiptRejected("RECEIPT_INVALID_VERSION")
        if row["contract_version"] != version:
            raise ReceiptRejected("RECEIPT_DIGEST_MISMATCH")
        try:
            expected_payload = build_expected_receipt_payload(row)
        except ValueError as exc:
            if str(exc) == "RECEIPT_INVALID_VERSION":
                raise ReceiptRejected("RECEIPT_INVALID_VERSION") from None
            raise ReceiptRejected("RECEIPT_DIGEST_MISMATCH") from None
        rebuilt_canonical = canonical_json(payload)
        if (
            payload != expected_payload
            or rebuilt_canonical != row["canonical_json"]
            or digest_canonical(rebuilt_canonical) != row["receipt_digest"]
        ):
            raise ReceiptRejected("RECEIPT_DIGEST_MISMATCH")

        existing_execution = connection.execute(
            """
            SELECT e.transaction_id, a.method, t.response_json
            FROM receipt_executions e
            JOIN authorization_records a ON a.receipt_id = e.receipt_id
            JOIN action_attempts t ON t.request_id = ?
            WHERE e.receipt_id = ?
            """,
            (row["request_id"], row["receipt_id"]),
        ).fetchone()
        if existing_execution is not None:
            if existing_execution["method"] != method:
                raise ReceiptRejected("RECEIPT_ALREADY_EXECUTED")
            response = json.loads(existing_execution["response_json"])
            response["idempotent_replay"] = True
            return response

        if version == 2 and row["destination_kind"] == "PAYNOW_MOBILE":
            try:
                lookup = self._get_valid_paynow_lookup(
                    connection,
                    lookup_reference=row["lookup_reference"],
                    destination_reference=row["destination_reference"],
                    recipient_display_name=row["recipient_display_name"],
                    beneficiary_id=row["beneficiary_id"],
                    destination_account_id=row["destination_account_id"],
                    now=now,
                )
            except ClarificationRejected as exc:
                raise ReceiptRejected(exc.reason_code) from None
            if lookup is None:
                raise ReceiptRejected("PAYNOW_LOOKUP_MISMATCH")

        status_codes = {
            "EXPIRED": "RECEIPT_EXPIRED",
            "SUPERSEDED": "RECEIPT_SUPERSEDED",
        }
        if row["status"] in status_codes:
            raise ReceiptRejected(status_codes[row["status"]])
        if row["status"] != "AWAITING_CONFIRMATION":
            raise ReceiptRejected("RECEIPT_INVALID_STATUS")

        expires_at = datetime.fromisoformat(row["expires_at"])
        if now.astimezone(timezone.utc) >= expires_at.astimezone(timezone.utc):
            connection.execute(
                """
                UPDATE review_receipts SET status = 'EXPIRED'
                WHERE receipt_id = ? AND status = 'AWAITING_CONFIRMATION'
                """,
                (row["receipt_id"],),
            )
            raise ReceiptRejected("RECEIPT_EXPIRED", persist_status=True)

        # Second engine: re-audit the receipt as stored against the user's
        # own words inside the executing transaction.
        validation = record_validation(
            connection, kind="single", receipt_id=row["receipt_id"], stage="EXECUTION", now=now
        )
        if validation.verdict == "FROZEN":
            raise ReceiptRejected("VALIDATION_FROZEN", persist_status=True)
        self._enforce_policy(
            connection,
            user_id=user_id,
            receipt_id=row["receipt_id"],
            receipt_digest=row["receipt_digest"],
            item_amounts=(row["amount_minor"],),
            out_of_band_code=out_of_band_code,
            now=now,
        )

        source = connection.execute(
            """
            SELECT account_id, label, balance_minor, currency
            FROM accounts
            WHERE account_id = ? AND is_user_source = 1
            """,
            (row["source_account_id"],),
        ).fetchone()
        beneficiary = connection.execute(
            """
            SELECT beneficiary_id, destination_account_id, display_name,
                   masked_destination
            FROM beneficiaries WHERE beneficiary_id = ?
            """,
            (row["beneficiary_id"],),
        ).fetchone()
        if source is None or beneficiary is None:
            raise ResolutionError("server-owned account resolution failed")
        destination_account_id = beneficiary["destination_account_id"]
        if version == 2 and row["destination_kind"] == "PAYNOW_MOBILE":
            destination_account_id = row["destination_account_id"]
        if source["currency"] != row["currency"] or row["currency"] != "SGD":
            raise ValueError("currency must be SGD")
        if type(row["amount_minor"]) is not int or row["amount_minor"] <= 0:
            raise ValueError("amount_minor must be a positive integer")
        if source["balance_minor"] < row["amount_minor"]:
            raise InsufficientFunds("INSUFFICIENT_FUNDS")

        authorized_at = now.astimezone(timezone.utc).isoformat()
        transaction_id = f"txn_{uuid.uuid4().hex}"
        connection.execute(
            """
            INSERT INTO authorization_records (
              authorization_id, receipt_id, receipt_digest, user_id,
              method, authorized_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"auth_{uuid.uuid4().hex}",
                row["receipt_id"],
                row["receipt_digest"],
                user_id,
                method,
                authorized_at,
            ),
        )
        connection.execute(
            """
            UPDATE accounts SET balance_minor = balance_minor - ?
            WHERE account_id = ?
            """,
            (row["amount_minor"], row["source_account_id"]),
        )
        connection.execute(
            """
            UPDATE accounts SET balance_minor = balance_minor + ?
            WHERE account_id = ?
            """,
            (row["amount_minor"], destination_account_id),
        )
        connection.execute(
            """
            INSERT INTO ledger_transactions (
              transaction_id, request_id, kind, amount_minor,
              currency, created_at
            ) VALUES (?, ?, 'BENEFICIARY_TRANSFER', ?, ?, ?)
            """,
            (
                transaction_id,
                row["request_id"],
                row["amount_minor"],
                row["currency"],
                authorized_at,
            ),
        )
        connection.executemany(
            """
            INSERT INTO ledger_entries (
              entry_id, transaction_id, account_id, delta_minor
            ) VALUES (?, ?, ?, ?)
            """,
            (
                (
                    f"entry_{uuid.uuid4().hex}",
                    transaction_id,
                    row["source_account_id"],
                    -row["amount_minor"],
                ),
                (
                    f"entry_{uuid.uuid4().hex}",
                    transaction_id,
                    destination_account_id,
                    row["amount_minor"],
                ),
            ),
        )
        entry_sum = connection.execute(
            """
            SELECT SUM(delta_minor) FROM ledger_entries
            WHERE transaction_id = ?
            """,
            (transaction_id,),
        ).fetchone()[0]
        if entry_sum != 0:
            raise sqlite3.IntegrityError("ledger entries are not balanced")

        connection.execute(
            """
            INSERT INTO receipt_executions (receipt_id, transaction_id, executed_at)
            VALUES (?, ?, ?)
            """,
            (row["receipt_id"], transaction_id, authorized_at),
        )
        connection.execute(
            "UPDATE review_receipts SET status = 'EXECUTED' WHERE receipt_id = ?",
            (row["receipt_id"],),
        )
        if row["clarification_group_id"] is not None:
            connection.execute(
                """
                UPDATE review_receipts SET status = 'SUPERSEDED'
                WHERE clarification_group_id = ? AND receipt_id != ?
                  AND status = 'AWAITING_CONFIRMATION'
                """,
                (row["clarification_group_id"], row["receipt_id"]),
            )

        attempt = connection.execute(
            """
            SELECT validated_intent_json FROM action_attempts WHERE request_id = ?
            """,
            (row["request_id"],),
        ).fetchone()
        validated_intent = (
            json.loads(attempt["validated_intent_json"])
            if attempt is not None and attempt["validated_intent_json"]
            else {}
        )
        response = {
            "outcome": "EXECUTED_SYNTHETIC_TRANSFER",
            "request_id": row["request_id"],
            "transaction_id": transaction_id,
            "amount_minor": row["amount_minor"],
            "currency": row["currency"],
            "validated_intent": validated_intent,
            "receipt": {
                "receipt_id": row["receipt_id"],
                "receipt_digest": row["receipt_digest"],
                "source_label": source["label"],
                "recipient_display_name": beneficiary["display_name"],
                "amount_minor": row["amount_minor"],
                "currency": row["currency"],
                "expires_at": row["expires_at"],
                "status": "EXECUTED",
                "authorization_method": method,
            },
            "resolved_action": {
                "kind": "BENEFICIARY_TRANSFER",
                "source_account_id": row["source_account_id"],
                "beneficiary_id": row["beneficiary_id"],
                "destination_account_id": destination_account_id,
                "recipient_display_name": beneficiary["display_name"],
                "masked_destination": beneficiary["masked_destination"],
                "amount_minor": row["amount_minor"],
                "currency": row["currency"],
            },
        }
        if approval is not None:
            response["approval"] = dict(approval)
        connection.execute(
            """
            UPDATE action_attempts
            SET outcome = ?, reason_code = NULL, response_json = ?
            WHERE request_id = ?
            """,
            (
                response["outcome"],
                json.dumps(response, ensure_ascii=False),
                row["request_id"],
            ),
        )
        return response

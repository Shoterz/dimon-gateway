"""SQLite-backed synthetic ledger owned by the Dimon Live server."""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
import re
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
from . import app_lock, audit_log, investing, kyc, payee_additions, singpass
from .face_id import FaceRejected, FaceStore, default_engine, face_database_path
from .voiceprint import (
    VOICE_MAX_MINOR,
    VoiceRejected,
    VoiceStore,
    default_engine as default_speaker_engine,
    voice_database_path,
)
from .approval_gateway import approval_challenge, b64url_encode, verify_signature
from .audit_log import AUDIT_SCHEMA
from .risk_policy import (
    MOCK_VOICE_CONFIRMATION,
    POLICY_SCHEMA,
    PolicyDecision,
    PolicyRejected,
    check_kyc,
    evaluate as evaluate_policy,
    mock_token_code,
    seed_profile,
    verify_out_of_band,
    verify_requirements,
)
from .payees import (
    PAYEE_SCHEMA,
    aliases_for,
    describe_last_payment,
    describe_typical,
    history as payee_history,
    PayeeSaveRejected,
    is_new_payee,
    save_paynow_payee,
    saved_payees,
    seed_payees,
    unusual_amount,
)
from .no_approval import REQUESTS_SCHEMA, record_money_request
from .validation_store import VALIDATION_SCHEMA, record_validation
from .signing_store import (
    SIGNING_SCHEMA,
    activate_voice_key,
    add_pending_voice_key,
    consume_approval,
    issue_challenge,
    list_credentials,
    prior_signature,
    register_credential,
    voice_key,
)
from .receipts import (
    DEMO_USER_ID,
    ReceiptDraft,
    build_expected_receipt_payload,
    build_receipt_record,
    canonical_json,
    digest_canonical,
)
from .conversation_store import CONVERSATION_SCHEMA
from .paper_equity import PaperExecutionSnapshot
from .paper_equity_store import PAPER_EQUITY_SCHEMA, PaperEquityStore, migrate as paper_equity_migrate
from .bill_payment_directory import SYNTHETIC_BILLERS


MULTI_ACTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS multi_action_groups (
  group_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE REFERENCES action_attempts(request_id),
  user_id TEXT NOT NULL,
  original_text TEXT NOT NULL,
  payment_auth_amount_minor INTEGER NOT NULL CHECK (payment_auth_amount_minor >= 0),
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS multi_action_items (
  item_id TEXT PRIMARY KEY,
  group_id TEXT NOT NULL REFERENCES multi_action_groups(group_id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  kind TEXT NOT NULL,
  child_type TEXT NOT NULL,
  child_id TEXT NOT NULL,
  status TEXT NOT NULL,
  result_json TEXT,
  UNIQUE(group_id, ordinal),
  UNIQUE(group_id, child_type, child_id)
);
CREATE INDEX IF NOT EXISTS multi_action_items_by_group
  ON multi_action_items(group_id, ordinal);
CREATE TABLE IF NOT EXISTS multi_action_clarifications (
  clarification_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE REFERENCES action_attempts(request_id),
  response_request_id TEXT UNIQUE,
  user_id TEXT NOT NULL,
  original_text TEXT NOT NULL,
  intent_json TEXT NOT NULL,
  resolved_items_json TEXT NOT NULL,
  issues_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ('AWAITING_RESPONSE', 'COMPLETED', 'CANCELLED', 'EXPIRED', 'SUPERSEDED')
  ),
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


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
CREATE TABLE IF NOT EXISTS utility_bill_payments (
  transaction_id TEXT PRIMARY KEY REFERENCES ledger_transactions(transaction_id),
  receipt_id TEXT NOT NULL UNIQUE REFERENCES review_receipts(receipt_id),
  biller_id TEXT NOT NULL,
  biller_display_name TEXT NOT NULL,
  payment_reference TEXT
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

EVERYDAY_ACCOUNT = ("acct_everyday_01", "Demo User", "everyday", "everyday", "SGD", 250000, 1)

# The user's own accounts in the order they are numbered when someone says
# "my first account" or "my second account". Savings is first.
USER_ACCOUNT_ORDER = ("acct_savings_01", "acct_everyday_01")

LEDGER_PAGE_SIZE = 1024
# Every module whose code creates, migrates, or seeds ledger tables. Any edit
# to one of them gives existing ledgers a new stamp, so they are upgraded once.
_SCHEMA_MODULES = (
    "database.py", "payees.py", "investing.py", "singpass.py", "kyc.py", "payee_additions.py",
    "audit_log.py", "signing_store.py", "risk_policy.py", "validation_store.py", "no_approval.py",
    "conversation_store.py", "paper_equity_store.py", "app_lock.py",
)


@functools.lru_cache(maxsize=2)
def _schema_version(single_account: bool) -> int:
    digest = hashlib.sha256(f"single={single_account}".encode())
    here = Path(__file__).resolve().parent
    for name in _SCHEMA_MODULES:
        path = here / name
        if path.is_file():
            digest.update(name.encode() + path.read_bytes())
    return int(digest.hexdigest()[:7], 16)   # fits SQLite's 32-bit user_version


_SOURCE_POSITIONS = {
    **{f"{word} account": 0 for word in ("first", "1st", "primary", "main")},
    **{f"{word} account": 1 for word in ("second", "2nd")},
    "first": 0, "1st": 0, "second": 1, "2nd": 1,
}

SEED_ACCOUNTS = (
    ("acct_savings_01", "Demo User", "savings", "savings", "SGD", 200000, 1),
    EVERYDAY_ACCOUNT,
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


PAYNOW_DIRECTORY_EXTRA = (
    (
        ("acct_paynow_mxxx_01", "MXXX LXX", "MXXX LXX", "paynow 88008800", "SGD", 0, 0),
        ("beneficiary_paynow_mxxx_01", "MXXX LXX", "paynow:88008800", "acct_paynow_mxxx_01",
         "PayNow •••• 8800", "PAYNOW"),
        ("88008800", "MXXX LXX", "beneficiary_paynow_mxxx_01"),
    ),
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
        face_engine: Any = None,
        single_account: bool | None = None,
        voice_engine: Any = None,
    ):
        self.path = Path(path)
        # The banking UI shows one savings account. The second "everyday"
        # account (and "which account pays?" questions) is opt-in with
        # DIMON_EVERYDAY_ACCOUNT=1.
        if single_account is None:
            single_account = os.environ.get("DIMON_EVERYDAY_ACCOUNT", "0") != "1"
        self.single_account = single_account
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Faces live in their own database file, never in the ledger.
        self.face_engine = face_engine
        self.face_store = FaceStore(
            face_database_path(self.path), lambda: self.face_engine or default_engine()
        )
        # The voiceprint lives in its own file too. None means the installed
        # ONNX speaker model; tests inject a stand-in.
        self.voice_engine = voice_engine
        self.voice_store = VoiceStore(
            voice_database_path(self.path), lambda: self.voice_engine or default_speaker_engine()
        )

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
        """Create or upgrade the ledger. Runs the full schema only when the
        file was made by different code: the stamp in PRAGMA user_version
        makes every later call, in any process, a single read."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        version = _schema_version(self.single_account)
        with self._connect() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] == version:
                return
            if connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0:
                # Small pages: a fresh ledger is ~130 KB instead of ~450 KB,
                # which matters at a million users. Must precede any table.
                connection.execute(f"PRAGMA page_size = {LEDGER_PAGE_SIZE}")
            # A ledger has one user, and each operation opens and closes its
            # own connection, so SQLite's default rollback journal is cheaper
            # than WAL (which checkpoints and deletes its files on every close).
            self._create_or_upgrade(connection)
            connection.execute(f"PRAGMA user_version = {version}")

    def _create_or_upgrade(self, connection: sqlite3.Connection) -> None:
        connection.executescript(SCHEMA)
        connection.executescript(V2_SCHEMA)
        connection.executescript(BATCH_SCHEMA)
        connection.executescript(SIGNING_SCHEMA)
        connection.executescript(VALIDATION_SCHEMA)
        connection.executescript(POLICY_SCHEMA)
        connection.executescript(AUDIT_SCHEMA)
        connection.executescript(PAYEE_SCHEMA)
        connection.executescript(REQUESTS_SCHEMA)
        connection.executescript(investing.INVESTING_SCHEMA)
        connection.executescript(singpass.SINGPASS_SCHEMA)
        connection.executescript(CONVERSATION_SCHEMA)
        connection.executescript(PAPER_EQUITY_SCHEMA)
        connection.executescript(MULTI_ACTION_SCHEMA)
        connection.executescript(payee_additions.PAYEE_ADDITION_SCHEMA)
        connection.executescript(app_lock.APP_LOCK_SCHEMA)
        seed_profile(connection, DEMO_USER_ID, self._clock(), kyc_status="PENDING")
        kyc.migrate(connection)
        self._apply_v2_migrations(connection)
        count = connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        if count == 0:
            self._insert_seed(connection)
        else:
            if not self.single_account:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO accounts (
                      account_id, owner_display_name, label, normalized_label,
                      currency, balance_minor, is_user_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    EVERYDAY_ACCOUNT,
                )
            self._insert_paynow_seed(connection)
            seed_payees(connection, self._clock())
            self._insert_billers(connection)

    def create_pending_multi_action_group(
        self,
        request_id: str,
        original_text: str,
        children: Sequence[Mapping[str, Any]],
        *,
        payment_auth_amount_minor: int,
        user_id: str = DEMO_USER_ID,
    ) -> dict[str, Any]:
        """Atomically persist the ordered parent and its immutable child references."""
        if not 2 <= len(children) <= 3:
            raise ValueError("multi-action groups require two or three children")
        if type(payment_auth_amount_minor) is not int or payment_auth_amount_minor < 0:
            raise ValueError("payment_auth_amount_minor must be non-negative")
        for child in children:
            if not {"kind", "child_type", "child_id"}.issubset(child) or set(child) - {
                "kind", "child_type", "child_id", "status"
            } or not all(
                isinstance(child[key], str) and child[key]
                for key in ("kind", "child_type", "child_id")
            ):
                raise ValueError("multi-action child reference is invalid")
            if child.get("status", "AWAITING_APPROVAL") not in {
                "AWAITING_APPROVAL", "AWAITING_USER", "COMPLETED"
            }:
                raise ValueError("multi-action child status is invalid")
        group_id = f"multi_{uuid.uuid4().hex}"
        created_at = self._clock().astimezone(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                attempt = connection.execute(
                    "SELECT original_text FROM action_attempts WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if attempt is None or attempt["original_text"] != original_text:
                    raise ValueError("multi-action parent request is missing")
                connection.execute(
                    """INSERT INTO multi_action_groups
                    (group_id, request_id, user_id, original_text,
                     payment_auth_amount_minor, status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'AWAITING_APPROVAL', ?)""",
                    (group_id, request_id, user_id, original_text,
                     payment_auth_amount_minor, created_at),
                )
                for ordinal, child in enumerate(children):
                    connection.execute(
                        """INSERT INTO multi_action_items
                        (item_id, group_id, ordinal, kind, child_type, child_id, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (f"item_{uuid.uuid4().hex}", group_id, ordinal,
                         child["kind"], child["child_type"], child["child_id"],
                         child.get("status", "AWAITING_APPROVAL")),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_multi_action_group(group_id, user_id=user_id)

    def create_multi_action_clarification(
        self,
        *,
        request_id: str,
        original_text: str,
        intent: Mapping[str, Any],
        resolved_items: Sequence[Mapping[str, Any]],
        issues: Sequence[Mapping[str, Any]],
        user_id: str = DEMO_USER_ID,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist non-executable grouped context for one bounded follow-up."""
        created_at = now or self._clock()
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        clarification_id = f"multi_clarification_{uuid.uuid4().hex}"
        created_iso = created_at.astimezone(timezone.utc).isoformat()
        expires_at = (
            created_at.astimezone(timezone.utc) + timedelta(minutes=5)
        ).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO multi_action_clarifications (
                  clarification_id, request_id, user_id, original_text,
                  intent_json, resolved_items_json, issues_json, status,
                  expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'AWAITING_RESPONSE', ?, ?)
                """,
                (
                    clarification_id, request_id, user_id, original_text,
                    canonical_json(dict(intent)), canonical_json(list(resolved_items)),
                    canonical_json(list(issues)), expires_at, created_iso,
                ),
            )
        return {
            "clarification_id": clarification_id,
            "kind": "MULTI_ACTION",
            "expires_at": expires_at,
            "resolved_items": list(resolved_items),
            "issues": list(issues),
        }

    def get_multi_action_clarification(
        self, clarification_id: str, *, user_id: str = DEMO_USER_ID,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        checked_at = now or self._clock()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM multi_action_clarifications WHERE clarification_id = ?",
                (clarification_id,),
            ).fetchone()
            if row is None:
                raise ClarificationRejected("CLARIFICATION_NOT_FOUND")
            if row["user_id"] != user_id:
                raise ClarificationRejected("CLARIFICATION_USER_MISMATCH")
            if row["status"] != "AWAITING_RESPONSE":
                raise ClarificationRejected(f"CLARIFICATION_{row['status']}")
            if checked_at.astimezone(timezone.utc) >= datetime.fromisoformat(row["expires_at"]):
                connection.execute(
                    "UPDATE multi_action_clarifications SET status = 'EXPIRED' WHERE clarification_id = ?",
                    (clarification_id,),
                )
                raise ClarificationRejected("CLARIFICATION_EXPIRED")
            return {
                **dict(row),
                "intent": json.loads(row["intent_json"]),
                "resolved_items": json.loads(row["resolved_items_json"]),
                "issues": json.loads(row["issues_json"]),
            }

    def finish_multi_action_clarification(
        self, clarification_id: str, response_request_id: str, *, cancelled: bool = False,
    ) -> None:
        status = "CANCELLED" if cancelled else "COMPLETED"
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE multi_action_clarifications
                   SET status = ?, response_request_id = ?
                   WHERE clarification_id = ? AND status = 'AWAITING_RESPONSE'""",
                (status, response_request_id, clarification_id),
            )
            if cursor.rowcount != 1:
                raise ClarificationRejected("CLARIFICATION_STALE")

    def supersede_multi_action_drafts(self, request_ids: Sequence[str]) -> None:
        """Disable pending child reviews if a grouped preparation fails late."""
        identifiers = tuple(dict.fromkeys(item for item in request_ids if item))
        if not identifiers:
            return
        placeholders = ",".join("?" for _ in identifiers)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    f"UPDATE review_receipts SET status = 'SUPERSEDED' "
                    f"WHERE status = 'AWAITING_CONFIRMATION' AND request_id IN ({placeholders})",
                    identifiers,
                )
                connection.execute(
                    f"UPDATE batch_review_receipts SET status = 'EXPIRED' "
                    f"WHERE status = 'AWAITING_CONFIRMATION' AND request_id IN ({placeholders})",
                    identifiers,
                )
                connection.execute(
                    f"UPDATE investment_orders SET status = 'EXPIRED' "
                    f"WHERE status = 'AWAITING_CONFIRMATION' AND request_id IN ({placeholders})",
                    identifiers,
                )
                connection.execute(
                    f"UPDATE paper_equity_orders SET status = 'SUPERSEDED', "
                    f"reason_code = 'MULTI_ACTION_PREPARATION_FAILED' "
                    f"WHERE status = 'AWAITING_APPROVAL' AND request_id IN ({placeholders})",
                    identifiers,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def replace_unlinked_multi_action_attempt_response(
        self, request_id: str, original_text: str, response: Mapping[str, Any]
    ) -> None:
        """Replace an unlinked grouped envelope before a group or clarification is returned."""
        with self._connect() as connection:
            connection.execute(
                """UPDATE action_attempts SET outcome = ?, reason_code = ?, response_json = ?
                WHERE request_id = ? AND original_text = ?
                  AND outcome IN ('MULTI_ACTION_CONFIRMATION_REQUEST', 'CLARIFICATION_REQUEST')
                  AND NOT EXISTS (
                    SELECT 1 FROM multi_action_groups WHERE request_id = ?
                  )""",
                (response.get("outcome", "FAILED"), response.get("reason_code"),
                 canonical_json(dict(response)), request_id, original_text, request_id),
            )

    def get_multi_action_group(
        self, group_id: str, *, user_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            group = connection.execute(
                "SELECT * FROM multi_action_groups WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()
            if group is None:
                return None
            items = connection.execute(
                "SELECT * FROM multi_action_items WHERE group_id = ? ORDER BY ordinal",
                (group_id,),
            ).fetchall()
        return {
            **dict(group),
            "items": [
                {
                    **{key: row[key] for key in (
                        "item_id", "ordinal", "kind", "child_type", "child_id", "status"
                    )},
                    "result": json.loads(row["result_json"]) if row["result_json"] else None,
                }
                for row in items
            ],
        }

    def update_multi_action_item_status(
        self,
        group_id: str,
        item_id: str,
        status: str,
        result: Mapping[str, Any] | None = None,
        *,
        user_id: str = DEMO_USER_ID,
    ) -> dict[str, Any] | None:
        if status not in {"AWAITING_APPROVAL", "AWAITING_USER", "COMPLETED", "APPROVED", "REJECTED", "FAILED", "CANCELLED"}:
            raise ValueError("invalid multi-action item status")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """UPDATE multi_action_items SET status = ?, result_json = ?
                    WHERE group_id = ? AND item_id = ? AND EXISTS (
                      SELECT 1 FROM multi_action_groups
                      WHERE group_id = ? AND user_id = ?
                    )""",
                    (status, canonical_json(dict(result)) if result is not None else None,
                     group_id, item_id, group_id, user_id),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                counts = {
                    row["status"]: row["n"]
                    for row in connection.execute(
                    "SELECT status, COUNT(*) AS n FROM multi_action_items WHERE group_id = ? GROUP BY status",
                    (group_id,),
                    ).fetchall()
                }
                current = connection.execute(
                    "SELECT status FROM multi_action_items WHERE group_id = ? AND status IN ('AWAITING_APPROVAL', 'AWAITING_USER') LIMIT 1",
                    (group_id,),
                ).fetchone()
                group_status = "AWAITING_APPROVAL" if current else (
                    "PARTIALLY_COMPLETED" if any(counts.get(key, 0) for key in ("FAILED", "REJECTED", "CANCELLED"))
                    else "COMPLETED"
                )
                connection.execute(
                    "UPDATE multi_action_groups SET status = ? WHERE group_id = ?",
                    (group_status, group_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_multi_action_group(group_id, user_id=user_id)

    def update_multi_action_child_status(
        self,
        child_type: str,
        child_id: str,
        status: str,
        result: Mapping[str, Any] | None = None,
        *,
        user_id: str = DEMO_USER_ID,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT i.group_id, i.item_id FROM multi_action_items i
                JOIN multi_action_groups g ON g.group_id = i.group_id
                WHERE i.child_type = ? AND i.child_id = ? AND g.user_id = ?""",
                (child_type, child_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return self.update_multi_action_item_status(
            row["group_id"], row["item_id"], status, result, user_id=user_id
        )

    def bind_multi_action_child_reference(
        self,
        old_type: str,
        old_id: str,
        new_type: str,
        new_id: str,
        *,
        result: Mapping[str, Any] | None = None,
        user_id: str = DEMO_USER_ID,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE multi_action_items SET child_type = ?, child_id = ?,
                status = 'AWAITING_APPROVAL', result_json = COALESCE(?, result_json)
                WHERE child_type = ? AND child_id = ? AND group_id IN
                  (SELECT group_id FROM multi_action_groups WHERE user_id = ?)""",
                (new_type, new_id,
                 canonical_json(dict(result)) if result is not None else None,
                 old_type, old_id, user_id),
            )

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
        paper_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(paper_equity_orders)")
        }
        if paper_columns and "result_json" not in paper_columns:
            connection.execute("ALTER TABLE paper_equity_orders ADD COLUMN result_json TEXT")
        paper_equity_migrate(connection)
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
            [account for account in SEED_ACCOUNTS
             if not (self.single_account and account == EVERYDAY_ACCOUNT)],
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
            self._insert_billers(connection)
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'prior_payments'"
        ).fetchone():
            seed_payees(connection, self._clock())

    @staticmethod
    def _insert_billers(connection: sqlite3.Connection) -> None:
        for biller in SYNTHETIC_BILLERS:
            account_id = f"acct_{biller.biller_id}"
            normalized_label = f"synthetic settlement {biller.biller_id}"
            connection.execute(
                """
                INSERT OR IGNORE INTO accounts (
                  account_id, owner_display_name, label, normalized_label,
                  currency, balance_minor, is_user_source
                ) VALUES (?, ?, ?, ?, 'SGD', 0, 0)
                """,
                (account_id, biller.display_name, f"{biller.display_name} settlement", normalized_label),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO beneficiaries (
                  beneficiary_id, display_name, normalized_alias,
                  destination_account_id, masked_destination, directory_kind
                ) VALUES (?, ?, ?, ?, 'Synthetic biller', 'BILLER')
                """,
                (biller.biller_id, biller.display_name, normalized_label, account_id),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO beneficiary_aliases (alias, beneficiary_id) VALUES (?, ?)",
                [(alias.casefold(), biller.biller_id) for alias in biller.aliases],
            )

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
        # Synthetic demo proxies let users try exact-number PayNow lookup
        # against the same saved contacts shown in the Pay tab.
        connection.executemany(
            """
            INSERT OR IGNORE INTO paynow_proxies (
              canonical_proxy, is_active, masked_name, beneficiary_id
            ) VALUES (?, 1, ?, ?)
            """,
            (
                ("82352935", "John Smith", "beneficiary_john_smith_01"),
                ("81234567", "Alicia Powell", "beneficiary_alicia_powell_01"),
                ("87654321", "John Tan", "beneficiary_john_tan_01"),
            ),
        )
        # A second registered number, not yet saved as anyone's payee.
        for account, beneficiary, proxy in PAYNOW_DIRECTORY_EXTRA:
            connection.execute(
                """
                INSERT OR IGNORE INTO accounts (
                  account_id, owner_display_name, label, normalized_label,
                  currency, balance_minor, is_user_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                account,
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO beneficiaries (
                  beneficiary_id, display_name, normalized_alias,
                  destination_account_id, masked_destination, directory_kind
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                beneficiary,
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO paynow_proxies (
                  canonical_proxy, is_active, masked_name, beneficiary_id
                ) VALUES (?, 1, ?, ?)
                """,
                proxy,
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
                connection.execute("DELETE FROM utility_bill_payments")
                connection.execute("DELETE FROM receipt_executions")
                connection.execute("DELETE FROM ledger_transactions")
                connection.execute("DELETE FROM authorization_records")
                connection.execute("DELETE FROM clarification_sessions")
                connection.execute("DELETE FROM conversation_turns")
                connection.execute("DELETE FROM conversation_threads")
                connection.execute("DELETE FROM payee_additions")
                connection.execute("DELETE FROM paynow_lookups")
                connection.execute("DELETE FROM paynow_proxies")
                connection.execute("DELETE FROM review_receipts")
                connection.execute("DELETE FROM action_attempts")
                connection.execute("DELETE FROM money_requests")
                for table in ("singpass_authorisations", "investment_cash_entries", "trades", "holdings",
                              "plan_runs", "investment_plans", "investment_orders"):
                    connection.execute(f"DELETE FROM {table}")
                for table in (
                    "paper_investment_cash_entries",
                    "paper_equity_trades",
                    "paper_equity_holdings",
                    "paper_equity_orders",
                ):
                    connection.execute(f"DELETE FROM {table}")
                connection.execute("DELETE FROM beneficiary_aliases")
                connection.execute("DELETE FROM prior_payments")
                connection.execute("DELETE FROM payee_registrations")
                connection.execute("DELETE FROM beneficiaries")
                connection.execute("DELETE FROM accounts")
                self._insert_seed(connection)
                # The audit trail is append-only and survives a demo reset.
                audit_log.append(connection, "DEMO_RESET", {"seed": "restored"}, now=self._clock())
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
                ORDER BY a.is_user_source DESC,
                         -- the user's own accounts in bank order: savings, then everyday
                         CASE WHEN a.is_user_source = 1 THEN a.rowid END,
                         a.label COLLATE NOCASE
                """
            ).fetchall()
            transactions = connection.execute(
                """
                SELECT t.transaction_id, t.request_id,
                       CASE WHEN bill.transaction_id IS NOT NULL
                            THEN 'UTILITY_BILL_PAYMENT' ELSE t.kind END AS kind,
                       t.amount_minor, t.currency, t.created_at,
                       source.label AS source_label,
                       CASE WHEN bill.transaction_id IS NOT NULL
                            THEN bill.biller_display_name ELSE destination.label END
                         AS destination_label,
                       bill.biller_id, bill.biller_display_name,
                       bill.payment_reference
                FROM ledger_transactions t
                LEFT JOIN utility_bill_payments bill
                  ON bill.transaction_id = t.transaction_id
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
        """One of the user's own accounts, by label ("savings", "everyday
        account") or by position ("my second account"). Anything else is
        unknown; a source is never guessed."""

        normalized = " ".join(_normalized(label).split())
        normalized = re.sub(r"^(?:my|the)\s+", "", normalized)
        normalized = re.sub(r"\s+(?:acc|acct|a/c)$", " account", normalized)
        bare = re.sub(r"\s+account$", "", normalized)
        position = _SOURCE_POSITIONS.get(normalized)
        with self._connect() as connection:
            if position is not None:
                if position >= len(USER_ACCOUNT_ORDER):
                    return None
                where, value = "account_id = ?", USER_ACCOUNT_ORDER[position]
            else:
                where, value = "normalized_label = ?", bare
            row = connection.execute(
                f"""
                SELECT account_id, owner_display_name, label, currency,
                       balance_minor
                FROM accounts
                WHERE {where} AND is_user_source = 1
                """,
                (value,),
            ).fetchone()
        return dict(row) if row is not None else None

    def resolve_utility_biller(self, biller_id: str) -> dict[str, Any] | None:
        """Resolve a biller destination from the server-owned synthetic directory."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT b.beneficiary_id, b.display_name, b.destination_account_id,
                       b.masked_destination, a.label AS settlement_label
                FROM beneficiaries b
                JOIN accounts a ON a.account_id = b.destination_account_id
                WHERE b.beneficiary_id = ? AND b.directory_kind = 'BILLER'
                  AND a.is_user_source = 0
                """,
                (biller_id,),
            ).fetchone()
        return dict(row) if row else None

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
                  AND (
                    normalized_alias = :label OR lower(display_name) = :label
                    OR beneficiary_id IN (
                      SELECT beneficiary_id FROM beneficiary_aliases WHERE alias = :label
                    )
                  )
                ORDER BY display_name COLLATE NOCASE
                """,
                {"label": " ".join(normalized.split())},
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
                SELECT b.beneficiary_id, b.display_name, b.normalized_alias,
                       b.destination_account_id, b.masked_destination,
                       p.canonical_proxy AS paynow_proxy
                FROM beneficiaries b
                LEFT JOIN paynow_proxies p ON p.beneficiary_id = b.beneficiary_id
                WHERE b.directory_kind = 'SAVED'
                ORDER BY b.display_name COLLATE NOCASE
                """
            ).fetchall()
            return tuple(
                {**dict(row), "aliases": aliases_for(connection, row["beneficiary_id"])}
                for row in rows
            )

    def payee_insight(self, beneficiary_id: str, amount_minor: int | None = None) -> dict[str, Any]:
        """History-based context shown with a draft: last payment, the usual
        range, whether the payee is new, and whether this amount is unusual."""

        now = self._clock()
        with self._connect() as connection:
            record = payee_history(connection, beneficiary_id)
        return {
            **record.to_dict(),
            "last_payment": describe_last_payment(record, now),
            "typical": describe_typical(record),
            "new_payee": is_new_payee(record, now),
            "unusual_amount": (
                unusual_amount(record, amount_minor) if amount_minor is not None else None
            ),
        }

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
        read_back_played: bool = False,
        unusual_amount_confirmed: bool = False,
        voice_confirmation: str | None = None,
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
                decision = None
                uses_voiceprint = False
                if execution is None:
                    decision = self._evaluate_receipt_policy(
                        connection,
                        kind="batch",
                        receipt_id=receipt_id,
                        user_id=user_id,
                        now=approved_at,
                    )
                    uses_voiceprint = decision.requires_voice_confirmation and self._voiceprint_matched(
                        assertion, receipt_digest, user_id
                    )
                    self._require_receipt_authentication(
                        connection,
                        decision=decision,
                        receipt_id=receipt_id,
                        receipt_digest=receipt_digest,
                        user_id=user_id,
                        voice_confirmation=voice_confirmation,
                        now=approved_at,
                        voiceprint_matched=uses_voiceprint,
                    )
                approval = self._require_device_signature(
                    connection,
                    assertion,
                    context,
                    purpose="APPROVE_BATCH",
                    receipt_id=receipt_id,
                    receipt_digest=receipt_digest,
                    user_id=user_id,
                    executed=execution is not None,
                    require_face=bool(decision and decision.requires_face),
                    now=approved_at,
                    voice_allowed=row["total_minor"] < VOICE_MAX_MINOR,
                    require_voice=uses_voiceprint,
                )
                if execution is not None:
                    replay = json.loads(execution["response_json"])
                    replay["idempotent_replay"] = True
                    connection.rollback()
                    return replay
                if decision and decision.requires_singpass:
                    approval = self._consume_receipt_singpass(
                        connection,
                        receipt_id=receipt_id,
                        receipt_digest=receipt_digest,
                        user_id=user_id,
                        approval=approval,
                        now=approved_at,
                    )
                elif decision and self._voice_confirmed(approval):
                    approval = {
                        **approval,
                        "authentication_method": "VOICEPRINT_AND_DEVICE_SIGNATURE",
                    }
                    # A voiceprint match is stronger than the spoken
                    # attestation it replaces; settlement re-checks it as one.
                    voice_confirmation = MOCK_VOICE_CONFIRMATION
                elif decision:
                    self._audit_mock_voice_confirmation(
                        connection,
                        receipt_id=receipt_id,
                        receipt_digest=receipt_digest,
                        now=approved_at,
                    )
                    approval = {
                        **(approval or {}),
                        "authentication_method": "MOCK_VOICE_AFFIRMATION_AND_DEVICE_SIGNATURE",
                    }
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
                    kind="batch",
                    user_id=user_id,
                    receipt_id=receipt_id,
                    receipt_digest=row["receipt_digest"],
                    items=tuple(
                        (item["beneficiary_id"], item["amount_minor"]) for item in item_dicts
                    ),
                    out_of_band_code=out_of_band_code,
                    read_back_played=read_back_played,
                    unusual_amount_confirmed=unusual_amount_confirmed,
                    voice_confirmation=voice_confirmation,
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
                audit_log.append(
                    connection,
                    "LEDGER_COMMITTED",
                    {
                        "batch_transaction_id": batch_transaction_id,
                        "receipt_digest": row["receipt_digest"],
                        "source_account_id": row["source_account_id"],
                        "total_minor": row["total_minor"],
                        "items": [
                            {
                                "destination_account_id": item["destination_account_id"],
                                "amount_minor": item["amount_minor"],
                            }
                            for item in item_dicts
                        ],
                        "signature_id": (approval or {}).get("signature_id"),
                    },
                    now=approved_at,
                    request_id=row["request_id"],
                    receipt_id=receipt_id,
                )
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
                    SELECT beneficiary_id, display_name, destination_account_id,
                           directory_kind
                    FROM beneficiaries WHERE beneficiary_id = ?
                        """,
                        (draft.beneficiary_id,),
                    ).fetchone()
                    if beneficiary is None:
                        raise ResolutionError(
                            "server-owned beneficiary resolution failed"
                        )
                    if draft.destination_kind == "UTILITY_BILL":
                        if (
                            beneficiary["directory_kind"] != "BILLER"
                            or draft.destination_reference != beneficiary["beneficiary_id"]
                            or draft.destination_account_id != beneficiary["destination_account_id"]
                            or draft.lookup_reference is not None
                        ):
                            raise ResolutionError("server-owned biller resolution failed")
                    elif beneficiary["directory_kind"] == "BILLER":
                        raise ResolutionError("biller destination requires utility bill receipt")
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

                masked = {
                    row["beneficiary_id"]: row["masked_destination"]
                    for row in connection.execute(
                        f"""
                        SELECT beneficiary_id, masked_destination FROM beneficiaries
                        WHERE beneficiary_id IN ({','.join('?' for _ in records)})
                        """,
                        tuple(record["beneficiary_id"] for record in records),
                    )
                }
                choices = [
                    {
                        "receipt_id": record["receipt_id"],
                        "receipt_digest": record["receipt_digest"],
                        "beneficiary_id": record["beneficiary_id"],
                        "source_label": source["label"],
                        "recipient_display_name": record[
                            "recipient_display_name"
                        ],
                        "masked_destination": masked.get(record["beneficiary_id"]),
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
                        "biller_id": (
                            record.get("destination_reference")
                            if record.get("destination_kind") == "UTILITY_BILL"
                            else None
                        ),
                        "payment_kind": (
                            "UTILITY_BILL_PAYMENT"
                            if record.get("destination_kind") == "UTILITY_BILL"
                            else "BENEFICIARY_TRANSFER"
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
                        "beneficiary_id": record["beneficiary_id"],
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
        require_face: bool = True,
        now: datetime,
        voice_allowed: bool = False,
        require_voice: bool = False,
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
            approval = consume_approval(
                connection,
                assertion,
                purpose=purpose,
                receipt_id=receipt_id,
                receipt_digest=receipt_digest,
                user_id=user_id,
                context=context,
                now=now,
                voice_key_allowed=voice_allowed,
            )
        except ApprovalRejected as exc:
            # Persist the spent challenge: a failed signature burns it.
            raise ReceiptRejected(exc.reason_code, persist_status=True) from None
        if not require_face:
            # Under S$200 the face is not asked for. A spoken confirmation
            # that matched the registered voiceprint is spent here when the
            # caller relies on it, and a signature from the voice-approval
            # key is never accepted without one.
            if require_voice or approval.get("signed_by_voice_key"):
                return self._consume_voice_match(
                    connection, assertion, approval,
                    receipt_id=receipt_id, receipt_digest=receipt_digest,
                    user_id=user_id, now=now,
                    missing_reason="VOICE_CONFIRMATION_REQUIRED",
                )
            return approval
        # The registered face must have confirmed this exact challenge too.
        # Under S$200 a spoken confirmation that matched the registered
        # voiceprint may stand in for the face; the device signature above
        # is required either way. A signature from the voice-approval key is
        # only ever accepted together with that voice match.
        try:
            if approval.get("signed_by_voice_key"):
                raise FaceRejected("FACE_NOT_VERIFIED")
            face = self.face_store.consume(
                user_id=user_id,
                challenge_id=assertion.challenge_id,
                receipt_digest=receipt_digest,
                now=now,
            )
        except FaceRejected as exc:
            if not (voice_allowed and exc.reason_code == "FACE_NOT_VERIFIED"):
                raise ReceiptRejected(exc.reason_code, persist_status=True) from None
            return self._consume_voice_match(
                connection, assertion, approval,
                receipt_id=receipt_id, receipt_digest=receipt_digest,
                user_id=user_id, now=now, missing_reason="FACE_NOT_VERIFIED",
            )
        audit_log.append(
            connection,
            "FACE_MATCH_CONSUMED",
            {
                "scan_id": face["scan_id"],
                "challenge_id": assertion.challenge_id,
                "receipt_digest": receipt_digest,
                "similarity": face["similarity"],
            },
            now=now,
            receipt_id=receipt_id,
        )
        return {
            **approval,
            "confirmed_by": "FACE",
            "face_scan_id": face["scan_id"],
            "face_similarity": face["similarity"],
        }

    def _consume_voice_match(
        self,
        connection: sqlite3.Connection,
        assertion: DeviceAssertion,
        approval: dict[str, Any],
        *,
        receipt_id: str,
        receipt_digest: str,
        user_id: str,
        now: datetime,
        missing_reason: str,
    ) -> dict[str, Any]:
        try:
            voice = self.voice_store.consume(
                user_id=user_id,
                challenge_id=assertion.challenge_id,
                receipt_digest=receipt_digest,
                now=now,
            )
        except VoiceRejected as exc:
            reason = missing_reason if exc.reason_code == "VOICE_NOT_VERIFIED" else exc.reason_code
            raise ReceiptRejected(reason, persist_status=True) from None
        audit_log.append(
            connection,
            "VOICE_MATCH_CONSUMED",
            {
                "sample_id": voice["sample_id"],
                "challenge_id": assertion.challenge_id,
                "receipt_digest": receipt_digest,
                "similarity": voice["similarity"],
            },
            now=now,
            receipt_id=receipt_id,
        )
        return {
            **approval,
            "confirmed_by": "VOICE",
            "voice_sample_id": voice["sample_id"],
            "voice_similarity": voice["similarity"],
        }

    def _voiceprint_matched(
        self, assertion: DeviceAssertion | None, receipt_digest: str, user_id: str
    ) -> bool:
        """An unspent voiceprint match for this signature's challenge: the
        real form of the spoken "yes, confirm" affirmation."""

        if assertion is None:
            return False
        try:
            return self.voice_store.has_match(
                user_id=user_id,
                challenge_id=assertion.challenge_id,
                receipt_digest=receipt_digest,
            )
        except sqlite3.Error:
            return False

    @staticmethod
    def _voice_confirmed(approval: Mapping[str, Any] | None) -> bool:
        return bool(approval and approval.get("confirmed_by") == "VOICE")

    @staticmethod
    def _evaluate_receipt_policy(
        connection: sqlite3.Connection,
        *,
        kind: str,
        receipt_id: str,
        user_id: str,
        now: datetime,
    ) -> PolicyDecision:
        try:
            return evaluate_policy(
                connection,
                user_id=user_id,
                items=Database._receipt_items(connection, kind, receipt_id),
                now=now,
                authentication_total_minor=Database._receipt_authentication_total_minor(
                    connection, kind, receipt_id
                ),
                check_payee_risk=not Database._is_utility_bill_receipt(
                    connection, kind, receipt_id
                ),
            )
        except PolicyRejected as exc:
            raise ReceiptRejected(exc.reason_code, persist_status=exc.persist) from None

    @staticmethod
    def _require_receipt_authentication(
        connection: sqlite3.Connection,
        *,
        decision: PolicyDecision,
        receipt_id: str,
        receipt_digest: str,
        user_id: str,
        voice_confirmation: str | None,
        now: datetime,
        voiceprint_matched: bool = False,
    ) -> None:
        if (
            decision.requires_voice_confirmation
            and voice_confirmation != MOCK_VOICE_CONFIRMATION
            and not voiceprint_matched
        ):
            raise ReceiptRejected("VOICE_CONFIRMATION_REQUIRED")
        if decision.requires_singpass:
            try:
                singpass.require_approved(
                    connection,
                    user_id=user_id,
                    order_id=receipt_id,
                    order_digest=receipt_digest,
                    now=now,
                )
            except singpass.SingpassRejected as exc:
                raise ReceiptRejected(exc.reason_code) from None

    @staticmethod
    def _consume_receipt_singpass(
        connection: sqlite3.Connection,
        *,
        receipt_id: str,
        receipt_digest: str,
        user_id: str,
        approval: Mapping[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        try:
            authorisation = singpass.consume(
                connection,
                user_id=user_id,
                order_id=receipt_id,
                order_digest=receipt_digest,
                now=now,
            )
        except singpass.SingpassRejected as exc:
            raise ReceiptRejected(exc.reason_code, persist_status=True) from None
        audit_log.append(
            connection,
            "SINGPASS_AUTHORISATION_CONSUMED",
            {**authorisation, "order_digest": receipt_digest},
            now=now,
            receipt_id=receipt_id,
        )
        return {
            **(approval or {}),
            "authentication_method": "SINGPASS_FACE_AND_DEVICE_SIGNATURE",
            "singpass_auth_id": authorisation["auth_id"],
            "singpass_uinfin": authorisation["uinfin_masked"],
        }

    @staticmethod
    def _audit_mock_voice_confirmation(
        connection: sqlite3.Connection,
        *,
        receipt_id: str,
        receipt_digest: str,
        now: datetime,
    ) -> None:
        audit_log.append(
            connection,
            "MOCK_VOICE_AFFIRMATION_ACCEPTED",
            {
                "phrase": MOCK_VOICE_CONFIRMATION,
                "mode": "USER_ATTESTATION_NO_SPEAKER_MATCH",
                "receipt_digest": receipt_digest,
            },
            now=now,
            receipt_id=receipt_id,
        )

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
        kind: str,
        user_id: str,
        receipt_id: str,
        receipt_digest: str,
        items: tuple[tuple[str, int], ...],
        out_of_band_code: str | None,
        read_back_played: bool,
        unusual_amount_confirmed: bool,
        voice_confirmation: str | None = None,
        now: datetime,
    ) -> None:
        """Authoritative KYC, limit, velocity, and friction-tier check."""

        try:
            decision = evaluate_policy(
                connection,
                user_id=user_id,
                items=items,
                now=now,
                authentication_total_minor=Database._receipt_authentication_total_minor(
                    connection, kind, receipt_id
                ),
                check_payee_risk=not Database._is_utility_bill_receipt(
                    connection, kind, receipt_id
                ),
            )
            verify_requirements(
                decision,
                read_back_played=read_back_played,
                unusual_amount_confirmed=unusual_amount_confirmed,
                voice_confirmation=voice_confirmation,
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

    @staticmethod
    def _receipt_items(connection: sqlite3.Connection, kind: str, receipt_id: str) -> tuple[tuple[str, int], ...]:
        if kind == "single":
            row = connection.execute(
                "SELECT beneficiary_id, amount_minor FROM review_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            return ((row["beneficiary_id"], row["amount_minor"]),) if row else ()
        return tuple(
            (row["beneficiary_id"], row["amount_minor"])
            for row in connection.execute(
                "SELECT beneficiary_id, amount_minor FROM batch_receipt_items WHERE receipt_id = ? ORDER BY position",
                (receipt_id,),
            )
        )

    @staticmethod
    def _is_utility_bill_receipt(
        connection: sqlite3.Connection, kind: str, receipt_id: str
    ) -> bool:
        if kind != "single":
            return False
        row = connection.execute(
            "SELECT destination_kind FROM review_receipts WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchone()
        return bool(row and row["destination_kind"] == "UTILITY_BILL")

    @staticmethod
    def _receipt_authentication_total_minor(
        connection: sqlite3.Connection,
        kind: str,
        receipt_id: str,
    ) -> int:
        """Include grounded read-only bill amounts in the auth threshold only."""
        items = Database._receipt_items(connection, kind, receipt_id)
        utility_amounts: list[int] = []
        if kind == "single":
            row = connection.execute(
                "SELECT contract_version, original_text, evidence_bundle_json FROM review_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            if row is not None:
                if row["evidence_bundle_json"] is None and row["contract_version"] == 1:
                    return sum(amount for _, amount in items)
                try:
                    evidence = json.loads(row["evidence_bundle_json"])
                except (TypeError, json.JSONDecodeError):
                    raise ReceiptRejected("RECEIPT_INVALID_EVIDENCE") from None
                if not isinstance(evidence, list):
                    raise ReceiptRejected("RECEIPT_INVALID_EVIDENCE")
                for entry in evidence:
                    if not isinstance(entry, dict):
                        raise ReceiptRejected("RECEIPT_INVALID_EVIDENCE")
                    if entry.get("kind") != "READ_ONLY_UTILITY_AMOUNT":
                        continue
                    amount = entry.get("amount_minor")
                    amount_evidence = entry.get("amount_evidence")
                    if (
                        type(amount) is not int
                        or amount <= 0
                        or not isinstance(amount_evidence, str)
                    ):
                        raise ReceiptRejected("RECEIPT_INVALID_EVIDENCE")
                    from .grounding import GroundingError, derive_amount_from_text, is_grounded

                    try:
                        derived_amount, _ = derive_amount_from_text(amount_evidence)
                    except GroundingError:
                        raise ReceiptRejected("RECEIPT_INVALID_EVIDENCE") from None
                    if (
                        derived_amount != amount
                        or not is_grounded(row["original_text"], amount_evidence)
                    ):
                        raise ReceiptRejected("RECEIPT_INVALID_EVIDENCE")
                    utility_amounts.append(amount)
        if len(utility_amounts) > 1:
            raise ReceiptRejected("RECEIPT_INVALID_EVIDENCE")
        grouped_total = connection.execute(
            """SELECT MAX(g.payment_auth_amount_minor) AS total
            FROM multi_action_items i
            JOIN multi_action_groups g ON g.group_id = i.group_id
            WHERE i.child_type = ? AND i.child_id = ? AND i.kind IN
              ('BILL_PAYMENT', 'BENEFICIARY_TRANSFER', 'PAYNOW_MOBILE_TRANSFER')""",
            ("receipt" if kind == "single" else "batch_receipt", receipt_id),
        ).fetchone()["total"]
        grounded_total = sum(amount for _, amount in items) + sum(utility_amounts)
        return max(grounded_total, int(grouped_total or 0))

    def policy_for_receipt(
        self, kind: str, receipt_id: str, *, user_id: str = DEMO_USER_ID
    ) -> dict[str, Any]:
        """Early, non-authoritative check shown with a draft. KYC is checked at
        signing time, so a user can explore drafts before onboarding."""

        with self._connect() as connection:
            items = self._receipt_items(connection, kind, receipt_id)
            try:
                decision = evaluate_policy(
                    connection,
                    user_id=user_id,
                    items=items,
                    now=self._clock(),
                    require_kyc=False,
                    authentication_total_minor=self._receipt_authentication_total_minor(
                        connection, kind, receipt_id
                    ),
                    check_payee_risk=not self._is_utility_bill_receipt(
                        connection, kind, receipt_id
                    ),
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
        """What the hardware token shows: one code per awaiting receipt whose
        tier needs it (new payee or high value), with the amount and payee."""

        current = (now or self._clock()).astimezone(timezone.utc)
        with self._connect() as connection:
            rows = [
                ("single", row)
                for row in connection.execute(
                    """
                    SELECT receipt_id, receipt_digest, amount_minor AS total_minor,
                           recipient_display_name AS payee, expires_at
                    FROM review_receipts
                    WHERE user_id = ? AND status = 'AWAITING_CONFIRMATION'
                    """,
                    (user_id,),
                )
            ] + [
                ("batch", row)
                for row in connection.execute(
                    """
                    SELECT receipt_id, receipt_digest, total_minor,
                           'batch of payees' AS payee, expires_at
                    FROM batch_review_receipts
                    WHERE user_id = ? AND status = 'AWAITING_CONFIRMATION'
                    """,
                    (user_id,),
                )
            ]
            shown = [
                {
                    "receipt_id": row["order_id"],
                    "payee": investing.order_title(row),
                    "total_minor": row["total_minor"],
                    "code": mock_token_code(connection, user_id, row["order_id"], row["order_digest"]),
                }
                for row in connection.execute(
                    "SELECT * FROM investment_orders WHERE user_id = ? AND status = 'AWAITING_CONFIRMATION' AND expires_at > ?",
                    (user_id, current.isoformat()),
                )
                if row["total_minor"] > investing.TOKEN_ABOVE_MINOR
            ]
            for kind, row in rows:
                if datetime.fromisoformat(row["expires_at"]) <= current:
                    continue
                try:
                    decision = evaluate_policy(
                        connection,
                        user_id=user_id,
                        items=self._receipt_items(connection, kind, row["receipt_id"]),
                        now=current,
                        require_kyc=False,
                        authentication_total_minor=self._receipt_authentication_total_minor(
                            connection, kind, row["receipt_id"]
                        ),
                        check_payee_risk=not self._is_utility_bill_receipt(
                            connection, kind, row["receipt_id"]
                        ),
                    )
                except PolicyRejected:
                    continue
                if decision.requires_out_of_band:
                    shown.append(
                        {
                            "receipt_id": row["receipt_id"],
                            "payee": row["payee"],
                            "total_minor": row["total_minor"],
                            "code": mock_token_code(
                                connection, user_id, row["receipt_id"], row["receipt_digest"]
                            ),
                        }
                    )
            return shown

    def audit_event(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        request_id: str | None = None,
        receipt_id: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            audit_log.append(
                connection,
                event_type,
                dict(payload),
                now=self._clock(),
                request_id=request_id,
                receipt_id=receipt_id,
            )
            connection.commit()

    def audit_request(
        self,
        event_type: str,
        request_id: str,
        text: str,
        response: Mapping[str, Any],
        *,
        source: str,
        voice: Mapping[str, Any] | None = None,
    ) -> None:
        """Record what the user said and everything drafted from it."""

        with self._connect() as connection:
            attempt = connection.execute(
                "SELECT raw_model_output, validated_intent_json FROM action_attempts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        payload: dict[str, Any] = {
            "source": source,
            "text": text,
            "outcome": response.get("outcome"),
            "reason_code": response.get("reason_code"),
            "firewall": response.get("firewall"),
            "trace": [step.get("stage") for step in response.get("trace") or ()],
            "raw_model_output": attempt["raw_model_output"] if attempt else None,
            "validated_intent": (
                json.loads(attempt["validated_intent_json"])
                if attempt and attempt["validated_intent_json"]
                else None
            ),
            "receipts": [
                {
                    "receipt_id": choice.get("receipt_id"),
                    "receipt_digest": choice.get("receipt_digest"),
                    "recipient_display_name": choice.get("recipient_display_name"),
                    "amount_minor": choice.get("amount_minor"),
                }
                for choice in response.get("receipt_choices") or ()
            ],
            "batch_receipt": (
                {
                    "receipt_id": response["batch_receipt"].get("receipt_id"),
                    "receipt_digest": response["batch_receipt"].get("receipt_digest"),
                    "total_minor": response["batch_receipt"].get("total_minor"),
                }
                if response.get("batch_receipt")
                else None
            ),
            "discrepancies": response.get("discrepancies"),
            "policy": response.get("policy"),
        }
        if voice:
            payload["voice"] = dict(voice)
        self.audit_event(event_type, payload, request_id=request_id)

    def audit_overview(self, *, limit: int = 100) -> dict[str, Any]:
        with self._connect() as connection:
            return {
                "chain": audit_log.verify_chain(connection),
                "events": audit_log.events(connection, limit=limit),
            }

    def audit_bundle(self, receipt_id: str) -> dict[str, Any] | None:
        """Everything a regulator needs for one receipt, re-verified now."""

        with self._connect() as connection:
            kind = "single"
            receipt = connection.execute(
                "SELECT * FROM review_receipts WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            if receipt is None:
                kind = "batch"
                receipt = connection.execute(
                    "SELECT * FROM batch_review_receipts WHERE receipt_id = ?", (receipt_id,)
                ).fetchone()
            if receipt is None:
                return None
            request_ids = [receipt["request_id"]]
            origin = connection.execute(
                "SELECT request_id FROM clarification_sessions WHERE response_request_id = ?",
                (receipt["request_id"],),
            ).fetchone()
            if origin is not None:
                request_ids.append(origin["request_id"])
            recomputed = digest_canonical(receipt["canonical_json"])
            signatures = []
            for signature in connection.execute(
                """
                SELECT s.*, c.public_key_spki, c.algorithm, ch.nonce
                FROM approval_signatures s
                JOIN signing_credentials c ON c.credential_id = s.credential_id
                JOIN approval_challenges ch ON ch.challenge_id = s.challenge_id
                WHERE s.receipt_id = ?
                """,
                (receipt_id,),
            ):
                client_data = base64.b64decode(signature["client_data_json"])
                expected_challenge = b64url_encode(
                    approval_challenge(
                        signature["purpose"],
                        signature["challenge_id"],
                        signature["receipt_digest"],
                        signature["nonce"],
                    )
                )
                try:
                    verify_signature(
                        base64.b64decode(signature["public_key_spki"]),
                        signature["algorithm"],
                        signature["credential_kind"],
                        base64.b64decode(signature["authenticator_data"]),
                        client_data,
                        base64.b64decode(signature["signature"]),
                    )
                    signature_valid = True
                except ApprovalRejected:
                    signature_valid = False
                signatures.append(
                    {
                        "signature_id": signature["signature_id"],
                        "credential_id": signature["credential_id"],
                        "credential_kind": signature["credential_kind"],
                        "verified_at": signature["verified_at"],
                        "public_key_spki": signature["public_key_spki"],
                        "client_data_json": signature["client_data_json"],
                        "authenticator_data": signature["authenticator_data"],
                        "signature": signature["signature"],
                        "signature_valid_now": signature_valid,
                        "binds_this_receipt": (
                            signature["receipt_digest"] == receipt["receipt_digest"]
                            and json.loads(client_data).get("challenge") == expected_challenge
                        ),
                    }
                )
            return {
                "receipt_kind": kind,
                "receipt_id": receipt_id,
                "status": receipt["status"],
                "canonical_json": json.loads(receipt["canonical_json"]),
                "receipt_digest": receipt["receipt_digest"],
                "digest_matches_canonical_json": recomputed == receipt["receipt_digest"],
                "signatures": signatures,
                "chain": audit_log.verify_chain(connection),
                "events": audit_log.events(
                    connection, receipt_id=receipt_id, request_ids=tuple(request_ids)
                ),
            }

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

    def list_payees(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Saved payees with their nicknames and payment history."""

        with self._connect() as connection:
            return saved_payees(connection, now or self._clock())

    def save_payee(self, receipt_id: str, nickname: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Save a settled PayNow recipient under a nickname (new-payee rules apply)."""

        now = now or self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            saved = save_paynow_payee(connection, receipt_id=receipt_id, nickname=nickname, now=now)
            audit_log.append(
                connection,
                "PAYEE_SAVED",
                {"beneficiary_id": saved["beneficiary_id"], "origin": "PAYNOW"},
                now=now,
                receipt_id=receipt_id,
            )
            connection.commit()
            return saved

    def request_money(self, request_id: str, beneficiary_id: str, amount_minor: int) -> str:
        """Record a request for someone to pay the user. Nothing is debited."""

        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            money_request_id = record_money_request(
                connection, request_id=request_id, beneficiary_id=beneficiary_id,
                amount_minor=amount_minor, now=now,
            )
            audit_log.append(
                connection,
                "MONEY_REQUESTED",
                {"money_request_id": money_request_id, "beneficiary_id": beneficiary_id, "amount_minor": amount_minor},
                now=now,
                request_id=request_id,
            )
            connection.commit()
            return money_request_id

    # ------------------------------------------------------------ investing
    def investing_overview(self, user_id: str = DEMO_USER_ID, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            runs = investing.run_due_plans(connection, user_id, now)
            overview = investing.portfolio(connection, user_id, now)
            connection.commit()
        return {**overview, "plan_runs": runs}

    def investor_profile(self, user_id: str = DEMO_USER_ID) -> dict[str, Any] | None:
        with self._connect() as connection:
            return investing.profile(connection, user_id)

    def save_investor_profile(
        self, answers: dict[str, int], acknowledged: bool, user_id: str = DEMO_USER_ID
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            saved = investing.save_profile(connection, user_id, answers, acknowledged, self._clock())
            connection.commit()
            return saved

    def draft_investment_order(
        self, request_id: str, text: str, intent: Any, user_id: str = DEMO_USER_ID
    ) -> dict[str, Any]:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            order = investing.draft_order(
                connection, user_id=user_id, request_id=request_id, text=text, intent=intent, now=now
            )
            connection.commit()
            return order

    # ------------------------------------------------------ paper-equity auth
    def _paper_equity_awaiting(
        self,
        connection: sqlite3.Connection,
        order_id: str,
        order_digest: str,
        user_id: str,
        now: datetime,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM paper_equity_orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            raise ReceiptRejected("PAPER_EQUITY_ORDER_NOT_FOUND")
        if row["user_id"] != user_id:
            raise ReceiptRejected("PAPER_EQUITY_USER_MISMATCH")
        if row["order_digest"] != order_digest:
            raise ReceiptRejected("PAPER_EQUITY_DIGEST_MISMATCH")
        if row["status"] != "AWAITING_APPROVAL":
            raise ReceiptRejected("PAPER_EQUITY_ORDER_TERMINAL")
        try:
            receipt = json.loads(row["receipt_json"])
            expires_at = datetime.fromisoformat(receipt["approval_expires_at"])
        except (TypeError, KeyError, json.JSONDecodeError, ValueError) as exc:
            raise ReceiptRejected("PAPER_EQUITY_RECEIPT_INVALID") from exc
        if now.astimezone(timezone.utc) >= expires_at.astimezone(timezone.utc):
            connection.execute(
                "UPDATE paper_equity_orders SET status = 'EXPIRED', reason_code = 'APPROVAL_EXPIRED' WHERE order_id = ?",
                (order_id,),
            )
            raise ReceiptRejected("PAPER_EQUITY_APPROVAL_EXPIRED", persist_status=True)
        return row

    def request_paper_equity_singpass_authorisation(
        self,
        order_id: str,
        order_digest: str,
        user_id: str = DEMO_USER_ID,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._paper_equity_awaiting(
                    connection, order_id, order_digest, user_id, now
                )
                receipt = json.loads(row["receipt_json"])
                request = singpass.request(
                    connection,
                    user_id=user_id,
                    order_id=order_id,
                    order_digest=order_digest,
                    title=(
                        f"{'Sell' if receipt.get('side') == 'SELL' else 'Buy'} {receipt['quantity']} "
                        f"{'share' if int(receipt['quantity']) == 1 else 'shares'} of "
                        f"{receipt['instrument']['symbol']} (paper)"
                    ),
                    total_minor=int(
                        receipt["estimated_funding_minor"]
                        if receipt.get("side") == "SELL"
                        else receipt["max_funding_debit_minor"]
                    ),
                    now=now,
                )
                audit_log.append(
                    connection,
                    "PAPER_EQUITY_SINGPASS_REQUESTED",
                    {"auth_id": request["auth_id"], "order_digest": order_digest},
                    now=now,
                    receipt_id=order_id,
                )
                connection.commit()
                return request
            except singpass.SingpassRejected as exc:
                connection.rollback()
                raise ReceiptRejected(exc.reason_code) from None
            except ReceiptRejected as exc:
                if exc.persist_status:
                    connection.commit()
                else:
                    connection.rollback()
                raise
            except Exception:
                connection.rollback()
                raise

    def issue_paper_equity_challenge(
        self,
        order_id: str,
        order_digest: str,
        *,
        rp_id: str,
        user_id: str = DEMO_USER_ID,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._paper_equity_awaiting(
                    connection, order_id, order_digest, user_id, now
                )
                try:
                    singpass.require_approved(
                        connection,
                        user_id=user_id,
                        order_id=order_id,
                        order_digest=order_digest,
                        now=now,
                    )
                except singpass.SingpassRejected as exc:
                    raise ReceiptRejected(exc.reason_code) from None
                credentials = list_credentials(connection, user_id, rp_id)
                if not credentials:
                    raise ReceiptRejected("SIGNING_CREDENTIAL_REQUIRED")
                challenge = issue_challenge(
                    connection,
                    purpose="APPROVE_RECEIPT",
                    user_id=user_id,
                    now=now,
                    receipt_id=order_id,
                    receipt_digest=order_digest,
                )
                connection.commit()
                return {
                    **challenge,
                    "receipt_id": order_id,
                    "receipt_digest": order_digest,
                    "allow_credentials": credentials,
                }
            except ReceiptRejected as exc:
                if exc.persist_status:
                    connection.commit()
                else:
                    connection.rollback()
                raise

    def approve_paper_equity_order(
        self,
        order_id: str,
        order_digest: str,
        *,
        assertion: DeviceAssertion,
        context: ApprovalContext,
        execution: PaperExecutionSnapshot,
        user_id: str = DEMO_USER_ID,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Consume Singpass + face/device approval and settle one paper order.

        The approval records and the isolated paper-equity ledger movement are
        committed in the same SQLite transaction, so a failed execution cannot
        spend the user's approval or partially mutate cash/holdings.
        """

        now = now or self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._paper_equity_awaiting(
                    connection, order_id, order_digest, user_id, now
                )
                self._require_device_signature(
                    connection,
                    assertion,
                    context,
                    purpose="APPROVE_RECEIPT",
                    receipt_id=order_id,
                    receipt_digest=order_digest,
                    user_id=user_id,
                    executed=False,
                    now=now,
                )
                try:
                    singpass.consume(
                        connection,
                        user_id=user_id,
                        order_id=order_id,
                        order_digest=order_digest,
                        now=now,
                    )
                except singpass.SingpassRejected as exc:
                    raise ReceiptRejected(exc.reason_code, persist_status=True) from None
                connection.execute(
                    "UPDATE paper_equity_orders SET status = 'AUTHORIZED', authorized_at = ? WHERE order_id = ? AND status = 'AWAITING_APPROVAL'",
                    (now.astimezone(timezone.utc).isoformat(), order_id),
                )
                store = PaperEquityStore(self.path, clock=self._clock)
                result = store._apply_execution_on_connection(
                    connection,
                    order_id,
                    order_digest,
                    execution,
                    user_id=user_id,
                    now=now,
                )
                audit_log.append(
                    connection,
                    "PAPER_EQUITY_ORDER_EXECUTED",
                    {"status": result["status"], "reason_code": result.get("reason_code")},
                    now=now,
                    receipt_id=order_id,
                )
                connection.commit()
                return result
            except ReceiptRejected as exc:
                if exc.persist_status:
                    connection.commit()
                else:
                    connection.rollback()
                raise
            except Exception:
                connection.rollback()
                raise

    def cancel_investment_plan(self, plan_id: str, user_id: str = DEMO_USER_ID) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            investing.cancel_plan(connection, user_id, plan_id, self._clock())
            connection.commit()

    def _awaiting_order(self, connection: sqlite3.Connection, order_id: str, digest: str, user_id: str, now: datetime):
        row = connection.execute("SELECT * FROM investment_orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            raise ReceiptRejected("RECEIPT_NOT_FOUND")
        if row["user_id"] != user_id:
            raise ReceiptRejected("RECEIPT_USER_MISMATCH")
        if row["order_digest"] != digest:
            raise ReceiptRejected("RECEIPT_DIGEST_MISMATCH")
        if row["status"] == "EXECUTED":
            raise ReceiptRejected("RECEIPT_ALREADY_EXECUTED")
        if row["status"] != "AWAITING_CONFIRMATION":
            raise ReceiptRejected(f"ORDER_{row['status']}")
        if now.astimezone(timezone.utc) >= datetime.fromisoformat(row["expires_at"]):
            connection.execute("UPDATE investment_orders SET status = 'EXPIRED' WHERE order_id = ?", (order_id,))
            raise ReceiptRejected("RECEIPT_EXPIRED", persist_status=True)
        return row

    def request_singpass_authorisation(
        self, order_id: str, order_digest: str, user_id: str = DEMO_USER_ID, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """Send one exact investment order to the customer's Singpass app."""

        now = now or self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._awaiting_order(connection, order_id, order_digest, user_id, now)
                request = singpass.request(
                    connection, user_id=user_id, order_id=order_id, order_digest=order_digest,
                    title=investing.order_title(row), total_minor=row["total_minor"], now=now,
                )
            except singpass.SingpassRejected as exc:
                connection.rollback()
                raise ReceiptRejected(exc.reason_code) from None
            except ReceiptRejected as exc:
                if exc.persist_status:
                    connection.commit()
                else:
                    connection.rollback()
                raise
            audit_log.append(connection, "SINGPASS_AUTHORISATION_REQUESTED",
                             {"auth_id": request["auth_id"], "order_digest": order_digest}, now=now, receipt_id=order_id)
            connection.commit()
            return request

    def request_receipt_singpass_authorisation(
        self,
        kind: str,
        receipt_id: str,
        receipt_digest: str,
        user_id: str = DEMO_USER_ID,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Request Singpass for one stored transfer receipt at the high tier."""

        if kind not in {"single", "batch"}:
            raise ValueError("kind must be single or batch")
        requested_at = now or self._clock()
        table = "review_receipts" if kind == "single" else "batch_review_receipts"
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
                if row["status"] != "AWAITING_CONFIRMATION":
                    raise ReceiptRejected(f"RECEIPT_{row['status']}")
                if requested_at.astimezone(timezone.utc) >= datetime.fromisoformat(
                    row["expires_at"]
                ).astimezone(timezone.utc):
                    raise ReceiptRejected("RECEIPT_EXPIRED")
                decision = self._evaluate_receipt_policy(
                    connection,
                    kind=kind,
                    receipt_id=receipt_id,
                    user_id=user_id,
                    now=requested_at,
                )
                if not decision.requires_singpass:
                    raise ReceiptRejected("SINGPASS_NOT_REQUIRED")
                if kind == "single":
                    total_minor = row["amount_minor"]
                    title = f"Transfer to {row['recipient_display_name']}"
                else:
                    total_minor = row["total_minor"]
                    item_count = connection.execute(
                        "SELECT COUNT(*) FROM batch_receipt_items WHERE receipt_id = ?",
                        (receipt_id,),
                    ).fetchone()[0]
                    title = f"Transfer to {item_count} friends"
                request = singpass.request(
                    connection,
                    user_id=user_id,
                    order_id=receipt_id,
                    order_digest=receipt_digest,
                    title=title,
                    total_minor=total_minor,
                    now=requested_at,
                )
                audit_log.append(
                    connection,
                    "TRANSFER_SINGPASS_REQUESTED",
                    {"auth_id": request["auth_id"], "receipt_digest": receipt_digest},
                    now=requested_at,
                    receipt_id=receipt_id,
                )
                connection.commit()
                return request
            except singpass.SingpassRejected as exc:
                connection.rollback()
                raise ReceiptRejected(exc.reason_code) from None
            except PolicyRejected as exc:
                connection.rollback()
                raise ReceiptRejected(exc.reason_code, persist_status=exc.persist) from None
            except ReceiptRejected:
                connection.rollback()
                raise
            except Exception:
                connection.rollback()
                raise

    def singpass_authorisation(self, auth_id: str, user_id: str = DEMO_USER_ID) -> dict[str, Any]:
        with self._connect() as connection:
            return singpass.status(connection, user_id, auth_id, self._clock())

    def singpass_pending(self, user_id: str = DEMO_USER_ID) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return singpass.pending(connection, user_id, self._clock())

    def singpass_decide(self, auth_id: str, approve: bool, user_id: str = DEMO_USER_ID) -> dict[str, Any]:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            decision = singpass.decide(connection, user_id=user_id, auth_id=auth_id, approve=approve, now=now)
            audit_log.append(connection, "SINGPASS_APPROVED" if approve else "SINGPASS_REJECTED",
                             {"auth_id": auth_id}, now=now, receipt_id=decision["order_id"])
            connection.commit()
            return decision

    def issue_order_challenge(
        self, order_id: str, order_digest: str, *, rp_id: str, user_id: str = DEMO_USER_ID,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """A one-time challenge bound to an awaiting investment order."""

        now = now or self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._awaiting_order(connection, order_id, order_digest, user_id, now)
                credentials = list_credentials(connection, user_id, rp_id)
                if not credentials:
                    raise ReceiptRejected("SIGNING_CREDENTIAL_REQUIRED")
                review = investing.order_review(connection, row, now)
                review.update(
                    {
                        "authentication_tier": "SINGPASS_AND_FACE",
                        "requires_voice_confirmation": False,
                        "requires_singpass": True,
                        "requires_face": True,
                    }
                )
                challenge = issue_challenge(
                    connection, purpose="APPROVE_RECEIPT", user_id=user_id, now=now,
                    receipt_id=order_id, receipt_digest=order_digest,
                )
                connection.commit()
            except ReceiptRejected as exc:
                if exc.persist_status:
                    connection.commit()
                else:
                    connection.rollback()
                raise
        return {**challenge, "allow_credentials": credentials, "review": review}

    def approve_order(
        self,
        order_id: str,
        order_digest: str,
        user_id: str = DEMO_USER_ID,
        *,
        assertion: DeviceAssertion | None,
        context: ApprovalContext | None,
        out_of_band_code: str | None = None,
        read_back_played: bool = False,
        risk_acknowledged: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Execute a signed order: KYC, read-back, token, risk acknowledgement,
        the registered face and the device signature are all checked here,
        inside the transaction that fills it."""

        now = now or self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._awaiting_order(connection, order_id, order_digest, user_id, now)
                try:
                    check_kyc(connection, user_id, now)
                    if row["total_minor"] > investing.TOKEN_ABOVE_MINOR:
                        verify_out_of_band(connection, user_id=user_id, receipt_id=order_id,
                                           receipt_digest=order_digest, code=out_of_band_code)
                except PolicyRejected as exc:
                    raise ReceiptRejected(exc.reason_code, persist_status=True) from None
                if row["total_minor"] >= investing.READ_BACK_MINOR and not read_back_played:
                    raise ReceiptRejected("READ_BACK_REQUIRED")
                if row["risk_ack_required"] and not risk_acknowledged:
                    raise ReceiptRejected("RISK_ACKNOWLEDGEMENT_REQUIRED")
                # Investment transactions also need Singpass: checked first
                # without spending, so a missing approval burns nothing.
                try:
                    singpass.require_approved(connection, user_id=user_id, order_id=order_id,
                                              order_digest=order_digest, now=now)
                except singpass.SingpassRejected as exc:
                    raise ReceiptRejected(exc.reason_code) from None
                approval = self._require_device_signature(
                    connection, assertion, context, purpose="APPROVE_RECEIPT", receipt_id=order_id,
                    receipt_digest=order_digest, user_id=user_id, executed=False, now=now,
                    # Every investment needs Singpass and the face, whatever the amount.
                    voice_allowed=False,
                )
                try:
                    authorisation = singpass.consume(connection, user_id=user_id, order_id=order_id,
                                                     order_digest=order_digest, now=now)
                except singpass.SingpassRejected as exc:
                    raise ReceiptRejected(exc.reason_code, persist_status=True) from None
                audit_log.append(connection, "SINGPASS_AUTHORISATION_CONSUMED",
                                 {**authorisation, "order_digest": order_digest}, now=now, receipt_id=order_id)
                approval = {**approval, "singpass_auth_id": authorisation["auth_id"],
                            "singpass_uinfin": authorisation["uinfin_masked"]}
                try:
                    result = investing.execute_order(connection, row, now)
                except investing.InvestRejected as exc:
                    connection.execute(
                        "UPDATE investment_orders SET status = 'REJECTED', reason_code = ? WHERE order_id = ?",
                        (exc.reason_code, order_id),
                    )
                    raise ReceiptRejected(exc.reason_code, persist_status=True) from None
                connection.execute(
                    "UPDATE investment_orders SET status = 'EXECUTED', executed_at = ? WHERE order_id = ?",
                    (now.astimezone(timezone.utc).isoformat(), order_id),
                )
                connection.commit()
            except ReceiptRejected as exc:
                if exc.persist_status:
                    connection.commit()
                else:
                    connection.rollback()
                raise
        return {
            "outcome": "EXECUTED_INVESTMENT_PLAN" if row["kind"] == "PLAN" else "EXECUTED_SYNTHETIC_TRADE",
            "order_id": order_id,
            "order_kind": row["kind"],
            "title": investing.order_title(row),
            "approval": approval,
            **result,
            "trace": [
                {"stage": "ORDER_APPROVAL_RECEIVED"},
                {"stage": "SINGPASS_AUTHORISED"},
                {"stage": "FACE_MATCH_VERIFIED"},
                {"stage": "DEVICE_SIGNATURE_VERIFIED"},
                {"stage": "ORDER_FILLED" if row["kind"] != "PLAN" else "PLAN_STARTED"},
            ],
            "state": self.get_state(),
            "portfolio": self.investing_overview(user_id),
        }

    # ------------------------------------------------------------ PayNow payee additions
    def draft_payee_addition(
        self, request_id: str, text: str, lookup: Any, nickname: str, user_id: str = DEMO_USER_ID
    ) -> dict[str, Any]:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                addition = payee_additions.draft(
                    connection, user_id=user_id, request_id=request_id, text=text,
                    lookup=lookup, nickname=nickname, now=now,
                )
            except payee_additions.PayeeAdditionRejected:
                connection.rollback()
                raise
            audit_log.append(
                connection, "PAYEE_ADDITION_DRAFTED",
                {"addition_digest": addition["receipt_digest"], "masked_proxy": addition["masked_proxy"],
                 "masked_name": addition["masked_name"]},
                now=now, request_id=request_id, receipt_id=addition["addition_id"],
            )
            connection.commit()
            return addition

    def payee_addition_check(self, destination_account_id: str, nickname: str | None) -> None:
        """Advisory: refuse early when the payee or nickname already exists."""

        with self._connect() as connection:
            payee_additions.check_not_saved(connection, destination_account_id)
            if nickname is not None:
                payee_additions.check_nickname(connection, nickname)

    def issue_payee_addition_challenge(
        self, addition_id: str, addition_digest: str, *, rp_id: str, user_id: str = DEMO_USER_ID,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = payee_additions.awaiting(connection, addition_id, addition_digest, user_id, now)
                credentials = list_credentials(connection, user_id, rp_id)
                if not credentials:
                    raise ReceiptRejected("SIGNING_CREDENTIAL_REQUIRED")
                challenge = issue_challenge(
                    connection, purpose="APPROVE_RECEIPT", user_id=user_id, now=now,
                    receipt_id=addition_id, receipt_digest=addition_digest,
                )
                connection.commit()
            except payee_additions.PayeeAdditionRejected as exc:
                connection.rollback()
                raise ReceiptRejected(exc.reason_code) from None
            except ReceiptRejected:
                connection.rollback()
                raise
        return {**challenge, "allow_credentials": credentials, "review": payee_additions.review(row)}

    def approve_payee_addition(
        self,
        addition_id: str,
        addition_digest: str,
        user_id: str = DEMO_USER_ID,
        *,
        assertion: DeviceAssertion | None,
        context: ApprovalContext | None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Save the payee only with KYC, the registered face, and a device
        signature over this exact draft, all checked in the saving transaction."""

        now = now or self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                try:
                    row = payee_additions.awaiting(connection, addition_id, addition_digest, user_id, now)
                except payee_additions.PayeeAdditionRejected as exc:
                    if exc.reason_code == "RECEIPT_EXPIRED":
                        connection.execute(
                            "UPDATE payee_additions SET status = 'EXPIRED' WHERE addition_id = ?", (addition_id,)
                        )
                        raise ReceiptRejected(exc.reason_code, persist_status=True) from None
                    raise ReceiptRejected(exc.reason_code) from None
                try:
                    check_kyc(connection, user_id, now)
                except PolicyRejected as exc:
                    raise ReceiptRejected(exc.reason_code) from None
                approval = self._require_device_signature(
                    connection, assertion, context, purpose="APPROVE_RECEIPT", receipt_id=addition_id,
                    receipt_digest=addition_digest, user_id=user_id, executed=False, now=now,
                )
                try:
                    saved = payee_additions.save(connection, row, now)
                except payee_additions.PayeeAdditionRejected as exc:
                    raise ReceiptRejected(exc.reason_code, persist_status=True) from None
                audit_log.append(
                    connection, "PAYEE_SAVED",
                    {"beneficiary_id": saved["beneficiary_id"], "origin": "PAYNOW_ADDITION",
                     "addition_digest": addition_digest},
                    now=now, receipt_id=addition_id,
                )
                connection.commit()
            except ReceiptRejected as exc:
                if exc.persist_status:
                    connection.commit()
                else:
                    connection.rollback()
                raise
        return {
            "outcome": "EXECUTED_PAYEE_ADDED",
            "addition_id": addition_id,
            "payee": saved,
            "approval": approval,
            "trace": [
                {"stage": "PAYEE_ADDITION_APPROVAL_RECEIVED"},
                {"stage": "FACE_MATCH_VERIFIED"},
                {"stage": "DEVICE_SIGNATURE_VERIFIED"},
                {"stage": "PAYEE_SAVED"},
            ],
            "state": self.get_state(),
        }

    def start_myinfo(self, user_id: str = DEMO_USER_ID, *, now: datetime | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = kyc.retrieve_myinfo(connection, user_id=user_id, now=now or self._clock())
            connection.commit()
            return result

    def complete_kyc_onboarding(
        self,
        *,
        consent_id: str,
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
        # KYC binds a face as well as a device: the face must be registered
        # under this same Myinfo consent first.
        if not self.face_store.enrolled_for_consent(user_id, consent_id):
            raise ApprovalRejected("KYC_FACE_REQUIRED")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = kyc.complete_onboarding(
                    connection,
                    user_id=user_id,
                    consent_id=consent_id,
                    registration={
                        "challenge_id": challenge_id,
                        "credential_kind": credential_kind,
                        "credential_id": credential_id,
                        "public_key_spki": public_key_spki,
                        "algorithm": algorithm,
                        "client_data_json": client_data_json,
                        "authenticator_data": authenticator_data,
                        "signature": signature,
                    },
                    context=context,
                    now=now or self._clock(),
                )
            except ApprovalRejected:
                connection.commit()  # spent challenges and consents stay spent
                raise
            connection.commit()
        self.face_store.activate(user_id, consent_id, now or self._clock())
        return {**result, **self.face_store.status(user_id)}

    def kyc_status(self, user_id: str = DEMO_USER_ID) -> dict[str, Any]:
        with self._connect() as connection:
            status = kyc.status(connection, user_id)
        return {**status, **self.face_store.status(user_id)}

    def register_face(
        self, consent_id: str, frames: list[bytes], *, user_id: str = DEMO_USER_ID, now: datetime | None = None
    ) -> dict[str, Any]:
        """Register the account's face during KYC, under a live Myinfo consent."""

        now = now or self._clock()
        with self._connect() as connection:
            consent = connection.execute(
                "SELECT * FROM kyc_consents WHERE consent_id = ? AND user_id = ?", (consent_id, user_id)
            ).fetchone()
        if consent is None:
            raise FaceRejected("KYC_CONSENT_UNKNOWN")
        if consent["used_at"] is not None:
            raise FaceRejected("KYC_CONSENT_USED")
        if now.astimezone(timezone.utc) >= datetime.fromisoformat(consent["expires_at"]):
            raise FaceRejected("KYC_CONSENT_EXPIRED")
        try:
            scan = self.face_store.enroll(user_id=user_id, consent_id=consent_id, frames=frames, now=now)
        except FaceRejected as exc:
            self._audit_face_scan("FACE_REGISTRATION_REJECTED", exc.scan, now)
            raise
        self._audit_face_scan("FACE_REGISTERED", scan, now)
        return {"scan_id": scan["scan_id"], "verdict": scan["verdict"], **self.face_store.status(user_id)}

    def verify_face(
        self, challenge_id: str, frames: list[bytes], *, user_id: str = DEMO_USER_ID, now: datetime | None = None
    ) -> dict[str, Any]:
        """Match a live face scan for one issued, unexpired approval challenge."""

        now = now or self._clock()
        with self._connect() as connection:
            challenge = connection.execute(
                "SELECT * FROM approval_challenges WHERE challenge_id = ?", (challenge_id,)
            ).fetchone()
        if challenge is None or challenge["purpose"] not in {"APPROVE_RECEIPT", "APPROVE_BATCH"}:
            raise FaceRejected("APPROVAL_CHALLENGE_UNKNOWN")
        if challenge["user_id"] != user_id:
            raise FaceRejected("APPROVAL_USER_MISMATCH")
        if challenge["status"] != "ISSUED":
            raise FaceRejected("APPROVAL_CHALLENGE_REPLAYED")
        if now.astimezone(timezone.utc) >= datetime.fromisoformat(challenge["expires_at"]):
            raise FaceRejected("APPROVAL_CHALLENGE_EXPIRED")
        try:
            scan = self.face_store.verify(
                user_id=user_id,
                challenge_id=challenge_id,
                receipt_digest=challenge["receipt_digest"],
                frames=frames,
                now=now,
            )
        except FaceRejected as exc:
            self._audit_face_scan("FACE_SCAN_REJECTED", exc.scan, now, receipt_id=challenge["receipt_id"])
            raise
        self._audit_face_scan("FACE_SCAN_MATCHED", scan, now, receipt_id=challenge["receipt_id"])
        return {"scan_id": scan["scan_id"], "verdict": scan["verdict"], "similarity": scan["similarity"]}

    # ------------------------------------------------------------ app lock
    def lock_status(self, user_id: str = DEMO_USER_ID) -> dict[str, Any]:
        with self._connect() as connection:
            return app_lock.status(connection, user_id)

    def lock_app(self, *, user_id: str = DEMO_USER_ID, now: datetime | None = None) -> dict[str, Any]:
        """Log out: lock the account until the registered face unlocks it."""

        if not self.face_store.status(user_id)["face_registered"]:
            # Without a registered face nothing could ever unlock it again.
            raise app_lock.LockRejected("FACE_NOT_REGISTERED")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            locked = app_lock.lock(connection, user_id, now or self._clock())
            connection.commit()
            return locked

    def issue_unlock_challenge(self, *, user_id: str = DEMO_USER_ID, now: datetime | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            challenge = app_lock.issue_challenge(connection, user_id, now or self._clock())
            connection.commit()
            return challenge

    def unlock_with_face(
        self, challenge_id: str, frames: list[bytes], *, user_id: str = DEMO_USER_ID, now: datetime | None = None
    ) -> dict[str, Any]:
        """Match a live face scan for one unlock challenge, then lift the lock."""

        now = now or self._clock()
        with self._connect() as connection:
            app_lock.live_challenge(connection, challenge_id, user_id, now)
        digest = app_lock.unlock_digest(challenge_id)
        try:
            scan = self.face_store.verify(
                user_id=user_id, challenge_id=challenge_id, receipt_digest=digest, frames=frames, now=now
            )
        except FaceRejected as exc:
            self._audit_face_scan("FACE_SCAN_REJECTED", exc.scan, now)
            raise
        self._audit_face_scan("FACE_SCAN_MATCHED", scan, now)
        self.face_store.consume(user_id=user_id, challenge_id=challenge_id, receipt_digest=digest, now=now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            unlocked = app_lock.unlock(connection, challenge_id, user_id, now)
            connection.commit()
        return {**unlocked, "similarity": scan["similarity"]}

    # ------------------------------------------------------------ voiceprint
    def voice_status(self, user_id: str = DEMO_USER_ID) -> dict[str, Any]:
        with self._connect() as connection:
            key = voice_key(connection, user_id)
        return {**self.voice_store.status(user_id), "voice_key_credential_id": key}

    @staticmethod
    def _challenge_amount(connection: sqlite3.Connection, receipt_id: str | None) -> int | None:
        """What a voice-eligible approval would move, or None: payee additions,
        voice registration and every investment order always need the face."""

        for query in (
            "SELECT amount_minor FROM review_receipts WHERE receipt_id = ?",
            "SELECT total_minor FROM batch_review_receipts WHERE receipt_id = ?",
        ):
            row = connection.execute(query, (receipt_id,)).fetchone()
            if row is not None:
                return int(row[0])
        return None

    def _voice_challenge(self, connection: sqlite3.Connection, challenge_id: str, user_id: str, now: datetime) -> sqlite3.Row:
        challenge = connection.execute(
            "SELECT * FROM approval_challenges WHERE challenge_id = ?", (challenge_id,)
        ).fetchone()
        if challenge is None or challenge["purpose"] not in {"APPROVE_RECEIPT", "APPROVE_BATCH"}:
            raise VoiceRejected("APPROVAL_CHALLENGE_UNKNOWN")
        if challenge["user_id"] != user_id:
            raise VoiceRejected("APPROVAL_USER_MISMATCH")
        if challenge["status"] != "ISSUED":
            raise VoiceRejected("APPROVAL_CHALLENGE_REPLAYED")
        if now.astimezone(timezone.utc) >= datetime.fromisoformat(challenge["expires_at"]):
            raise VoiceRejected("APPROVAL_CHALLENGE_EXPIRED")
        amount = self._challenge_amount(connection, challenge["receipt_id"])
        if amount is None or amount >= VOICE_MAX_MINOR:
            raise VoiceRejected("VOICE_NOT_ALLOWED_FOR_THIS_APPROVAL")
        return challenge

    def issue_voice_phrase(
        self, challenge_id: str, *, user_id: str = DEMO_USER_ID, now: datetime | None = None
    ) -> dict[str, Any]:
        """The confirmation phrase bound to this approval (under S$200)."""

        now = now or self._clock()
        with self._connect() as connection:
            challenge = self._voice_challenge(connection, challenge_id, user_id, now)
        phrase = self.voice_store.issue_phrase(
            user_id=user_id, challenge_id=challenge_id, receipt_digest=challenge["receipt_digest"], now=now
        )
        return {"challenge_id": challenge_id, "phrase": phrase}

    def verify_voice(
        self, challenge_id: str, audio: bytes, transcript: str, *, user_id: str = DEMO_USER_ID,
        now: datetime | None = None, prior_request_audio: bytes | None = None,
    ) -> dict[str, Any]:
        if self.lock_status(user_id)["locked"]:
            raise VoiceRejected("APP_LOCKED")
        now = now or self._clock()
        with self._connect() as connection:
            challenge = self._voice_challenge(connection, challenge_id, user_id, now)
        try:
            sample = self.voice_store.verify(
                user_id=user_id, challenge_id=challenge_id, receipt_digest=challenge["receipt_digest"],
                audio=audio, transcript=transcript, now=now, prior_request_audio=prior_request_audio,
            )
        except VoiceRejected as exc:
            self._audit_voice("VOICE_SAMPLE_REJECTED", exc.sample, now, receipt_id=challenge["receipt_id"])
            raise
        self._audit_voice("VOICE_SAMPLE_MATCHED", sample, now, receipt_id=challenge["receipt_id"])
        return {"sample_id": sample["sample_id"], "verdict": sample["verdict"], "similarity": sample["similarity"]}

    def _audit_voice(self, event_type: str, sample: dict[str, Any] | None, now: datetime, *, receipt_id: str | None = None) -> None:
        if sample is None:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            audit_log.append(
                connection, event_type,
                {key: sample.get(key) for key in (
                    "sample_id", "purpose", "challenge_id", "receipt_digest", "similarity",
                    "phrase_expected", "phrase_heard", "verdict", "reason_code", "audio_sha256", "seconds",
                )},
                now=now, receipt_id=receipt_id,
            )
            connection.commit()

    def start_voice_enrollment(
        self,
        user_id: str = DEMO_USER_ID,
        *,
        voice_key: dict[str, Any] | None = None,
        context: ApprovalContext | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Registering a voice needs a verified identity and a registered face,
        because the registration itself is approved with the face.

        A browser key for voice approvals may be registered with it; it stays
        PENDING until this enrollment is approved with the face and device."""

        if self.lock_status(user_id)["locked"]:
            raise VoiceRejected("APP_LOCKED")
        now = now or self._clock()
        with self._connect() as connection:
            try:
                check_kyc(connection, user_id, now)
            except PolicyRejected as exc:
                raise VoiceRejected(exc.reason_code) from None
        if not self.face_store.status(user_id)["face_registered"]:
            raise VoiceRejected("FACE_NOT_REGISTERED")
        self.voice_store.engine()
        enrollment = self.voice_store.start_enrollment(user_id, now)
        if voice_key is not None:
            if voice_key.get("credential_kind") != "MOCK_DEVICE_KEY" or context is None:
                raise VoiceRejected("VOICE_KEY_KIND_UNSUPPORTED")
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    register_credential(connection, user_id=user_id, context=context, now=now, **voice_key)
                    add_pending_voice_key(
                        connection, enrollment_id=enrollment["enrollment_id"], user_id=user_id,
                        credential_id=voice_key["credential_id"], now=now,
                    )
                except ApprovalRejected as exc:
                    connection.commit()  # the registration challenge stays spent
                    raise VoiceRejected(exc.reason_code) from None
                connection.commit()
            enrollment["voice_key_credential_id"] = voice_key["credential_id"]
        return enrollment

    def add_voice_enrollment_sample(
        self, enrollment_id: str, audio: bytes, *, user_id: str = DEMO_USER_ID, now: datetime | None = None
    ) -> dict[str, Any]:
        if self.lock_status(user_id)["locked"]:
            raise VoiceRejected("APP_LOCKED")
        return self.voice_store.add_enrollment_sample(
            user_id=user_id, enrollment_id=enrollment_id, audio=audio, now=now or self._clock()
        )

    def issue_voice_enrollment_challenge(
        self, enrollment_id: str, enrollment_digest: str, *, rp_id: str, user_id: str = DEMO_USER_ID,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or self._clock()
        try:
            enrollment = self.voice_store.awaiting_enrollment(enrollment_id, enrollment_digest, user_id, now)
        except VoiceRejected as exc:
            raise ReceiptRejected(exc.reason_code) from None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            credentials = list_credentials(connection, user_id, rp_id)
            if not credentials:
                connection.rollback()
                raise ReceiptRejected("SIGNING_CREDENTIAL_REQUIRED")
            challenge = issue_challenge(
                connection, purpose="APPROVE_RECEIPT", user_id=user_id, now=now,
                receipt_id=enrollment_id, receipt_digest=enrollment_digest,
            )
            connection.commit()
        review = {
            "kind": "VOICE",
            "receipt_id": enrollment_id,
            "receipt_digest": enrollment_digest,
            "source_label": "—",
            "currency": "SGD",
            "total_minor": 0,
            "items": [{
                "recipient_display_name": "Register your voiceprint",
                "masked_destination": f"{enrollment['sample_count']} recordings · voice can confirm payments under S$200",
                "amount_minor": 0,
            }],
            "receipt_expires_at": enrollment["expires_at"],
            "validation": {"verdict": "PASS", "fields": [],
                           "note": "A new way to confirm payments, so it must be approved with your face and device."},
            "requires_out_of_band": False,
            "requires_read_back": False,
            "requires_unusual_confirmation": False,
            # Always the face and device: the screen must run the face scan.
            "requires_face": True,
            "requires_voice_confirmation": False,
            "requires_singpass": False,
            "read_back": {"text": "Register your voice to confirm payments under two hundred dollars. "
                                  "Larger payments will still need your face.", "audio_artifact_id": None},
        }
        return {**challenge, "allow_credentials": credentials, "review": review}

    def approve_voice_enrollment(
        self,
        enrollment_id: str,
        enrollment_digest: str,
        user_id: str = DEMO_USER_ID,
        *,
        assertion: DeviceAssertion | None,
        context: ApprovalContext | None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or self._clock()
        try:
            self.voice_store.awaiting_enrollment(enrollment_id, enrollment_digest, user_id, now)
        except VoiceRejected as exc:
            raise ReceiptRejected(exc.reason_code) from None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                try:
                    check_kyc(connection, user_id, now)
                except PolicyRejected as exc:
                    raise ReceiptRejected(exc.reason_code) from None
                # Face and device only: a voice can never approve its own registration.
                approval = self._require_device_signature(
                    connection, assertion, context, purpose="APPROVE_RECEIPT", receipt_id=enrollment_id,
                    receipt_digest=enrollment_digest, user_id=user_id, executed=False, now=now,
                )
                try:
                    registered = self.voice_store.activate(
                        enrollment_id=enrollment_id, digest=enrollment_digest, user_id=user_id, now=now
                    )
                except VoiceRejected as exc:
                    raise ReceiptRejected(exc.reason_code, persist_status=True) from None
                activate_voice_key(connection, enrollment_id=enrollment_id, user_id=user_id, now=now)
                audit_log.append(
                    connection, "VOICEPRINT_REGISTERED",
                    {"enrollment_id": enrollment_id, "template_sha256": registered["template_sha256"]},
                    now=now, receipt_id=enrollment_id,
                )
                connection.commit()
            except ReceiptRejected as exc:
                if exc.persist_status:
                    connection.commit()
                else:
                    connection.rollback()
                raise
        return {
            "outcome": "EXECUTED_VOICE_REGISTERED",
            "approval": approval,
            "voice": self.voice_store.status(user_id),
            "trace": [
                {"stage": "VOICE_REGISTRATION_APPROVAL_RECEIVED"},
                {"stage": "FACE_MATCH_VERIFIED"},
                {"stage": "DEVICE_SIGNATURE_VERIFIED"},
                {"stage": "VOICEPRINT_REGISTERED"},
            ],
            "state": self.get_state(),
        }

    def _audit_face_scan(
        self, event_type: str, scan: dict[str, Any] | None, now: datetime, *, receipt_id: str | None = None
    ) -> None:
        if scan is None:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            audit_log.append(
                connection,
                event_type,
                {
                    key: scan[key]
                    for key in (
                        "scan_id", "purpose", "challenge_id", "receipt_digest", "frame_count",
                        "face_frames", "yaw_range", "similarity", "verdict", "reason_code", "engine_version",
                    )
                },
                now=now,
                receipt_id=receipt_id,
            )
            connection.commit()

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
                            "beneficiary_id": row["beneficiary_id"],
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
                            SELECT i.beneficiary_id, i.recipient_display_name,
                                   b.masked_destination, i.amount_minor
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
                        items=tuple((item["beneficiary_id"], item["amount_minor"]) for item in items),
                        now=issued_at,
                        authentication_total_minor=self._receipt_authentication_total_minor(
                            connection, kind, receipt_id
                        ),
                        check_payee_risk=not self._is_utility_bill_receipt(
                            connection, kind, receipt_id
                        ),
                    )
                except PolicyRejected as exc:
                    raise ReceiptRejected(exc.reason_code) from None
                for item in items:
                    item["destination_kind"] = row["destination_kind"] if kind == "single" else None
                    item["new_payee"] = item["beneficiary_id"] in decision.new_payees
                    item["unusual_amount"] = next(
                        (
                            flag for flag in decision.unusual_amounts
                            if flag["beneficiary_id"] == item["beneficiary_id"]
                        ),
                        None,
                    )
                challenge = issue_challenge(
                    connection,
                    purpose=purpose,
                    user_id=user_id,
                    now=issued_at,
                    receipt_id=receipt_id,
                    receipt_digest=receipt_digest,
                )
                read_back = None
                if kind == "single" and decision.requires_read_back:
                    whole, cents = divmod(row["amount_minor"], 100)
                    read_back = {
                        "text": (
                            f"Pay S${whole}.{cents:02d} to {row['recipient_display_name']} "
                            f"from {source['label']}."
                        )
                    }
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
                "kind": (
                    "UTILITY_BILL_PAYMENT"
                    if kind == "single" and row["destination_kind"] == "UTILITY_BILL"
                    else "BENEFICIARY_TRANSFER" if kind == "single" else "BATCH"
                ),
                "source_label": source["label"] if source else None,
                "currency": row["currency"],
                "total_minor": total_minor,
                "items": items,
                "read_back": read_back,
                "receipt_expires_at": row["expires_at"],
                "validation": validation.to_dict(),
                "requires_out_of_band": decision.requires_out_of_band,
                "requires_read_back": decision.requires_read_back,
                "requires_unusual_confirmation": decision.requires_unusual_confirmation,
                "authentication_tier": decision.authentication_tier,
                "requires_voice_confirmation": decision.requires_voice_confirmation,
                "requires_singpass": decision.requires_singpass,
                "requires_face": decision.requires_face,
                "policy": decision.to_dict(),
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
        read_back_played: bool = False,
        unusual_amount_confirmed: bool = False,
        voice_confirmation: str | None = None,
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
                if receipt["user_id"] != user_id:
                    raise ReceiptRejected("APPROVAL_USER_MISMATCH")
                executed = connection.execute(
                    "SELECT 1 FROM receipt_executions WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone() is not None
                decision = None
                uses_voiceprint = False
                if not executed:
                    decision = self._evaluate_receipt_policy(
                        connection,
                        kind="single",
                        receipt_id=receipt_id,
                        user_id=user_id,
                        now=approved_at,
                    )
                    uses_voiceprint = decision.requires_voice_confirmation and self._voiceprint_matched(
                        assertion, receipt_digest, user_id
                    )
                    self._require_receipt_authentication(
                        connection,
                        decision=decision,
                        receipt_id=receipt_id,
                        receipt_digest=receipt_digest,
                        user_id=user_id,
                        voice_confirmation=voice_confirmation,
                        now=approved_at,
                        voiceprint_matched=uses_voiceprint,
                    )
                approval = self._require_device_signature(
                    connection,
                    assertion,
                    context,
                    purpose="APPROVE_RECEIPT",
                    receipt_id=receipt_id,
                    receipt_digest=receipt_digest,
                    user_id=user_id,
                    executed=executed,
                    require_face=bool(decision and decision.requires_face),
                    now=approved_at,
                    voice_allowed=receipt["amount_minor"] < VOICE_MAX_MINOR,
                    require_voice=uses_voiceprint,
                )
                if decision and decision.requires_singpass:
                    approval = self._consume_receipt_singpass(
                        connection,
                        receipt_id=receipt_id,
                        receipt_digest=receipt_digest,
                        user_id=user_id,
                        approval=approval,
                        now=approved_at,
                    )
                elif decision and self._voice_confirmed(approval):
                    approval = {
                        **approval,
                        "authentication_method": "VOICEPRINT_AND_DEVICE_SIGNATURE",
                    }
                    # A voiceprint match is stronger than the spoken
                    # attestation it replaces; settlement re-checks it as one.
                    voice_confirmation = MOCK_VOICE_CONFIRMATION
                elif decision:
                    self._audit_mock_voice_confirmation(
                        connection,
                        receipt_id=receipt_id,
                        receipt_digest=receipt_digest,
                        now=approved_at,
                    )
                    approval = {
                        **(approval or {}),
                        "authentication_method": "MOCK_VOICE_AFFIRMATION_AND_DEVICE_SIGNATURE",
                    }
                response = self._execute_stored_receipt(
                    connection,
                    receipt,
                    supplied_digest=receipt_digest,
                    user_id=user_id,
                    method="USER_CONFIRMATION",
                    now=approved_at,
                    approval=approval,
                    out_of_band_code=out_of_band_code,
                    read_back_played=read_back_played,
                    unusual_amount_confirmed=unusual_amount_confirmed,
                    voice_confirmation=voice_confirmation,
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
        read_back_played: bool = False,
        unusual_amount_confirmed: bool = False,
        voice_confirmation: str | None = None,
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
            kind="single",
            user_id=user_id,
            receipt_id=row["receipt_id"],
            receipt_digest=row["receipt_digest"],
            items=((row["beneficiary_id"], row["amount_minor"]),),
            out_of_band_code=out_of_band_code,
            read_back_played=read_back_played,
            unusual_amount_confirmed=unusual_amount_confirmed,
            voice_confirmation=voice_confirmation,
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
                   masked_destination, directory_kind
            FROM beneficiaries WHERE beneficiary_id = ?
            """,
            (row["beneficiary_id"],),
        ).fetchone()
        if source is None or beneficiary is None:
            raise ResolutionError("server-owned account resolution failed")
        destination_account_id = beneficiary["destination_account_id"]
        if version == 2 and row["destination_kind"] == "PAYNOW_MOBILE":
            destination_account_id = row["destination_account_id"]
        is_bill_payment = version == 2 and row["destination_kind"] == "UTILITY_BILL"
        if is_bill_payment:
            if (
                beneficiary["directory_kind"] != "BILLER"
                or row["destination_reference"] != beneficiary["beneficiary_id"]
                or row["destination_account_id"] != beneficiary["destination_account_id"]
            ):
                raise ReceiptRejected("BILLER_DESTINATION_MISMATCH")
            destination_account_id = row["destination_account_id"]
        elif beneficiary["directory_kind"] == "BILLER":
            raise ReceiptRejected("BILLER_DESTINATION_MISMATCH")
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
        if is_bill_payment:
            connection.execute(
                """
                INSERT INTO utility_bill_payments (
                  transaction_id, receipt_id, biller_id,
                  biller_display_name, payment_reference
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    transaction_id,
                    row["receipt_id"],
                    row["destination_reference"],
                    beneficiary["display_name"],
                    row["lookup_reference"],
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
        source_balance_after = source["balance_minor"] - row["amount_minor"]
        response = {
            "outcome": (
                "EXECUTED_SYNTHETIC_BILL_PAYMENT"
                if is_bill_payment
                else "EXECUTED_SYNTHETIC_TRANSFER"
            ),
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
                "payment_kind": "UTILITY_BILL_PAYMENT" if is_bill_payment else "BENEFICIARY_TRANSFER",
            },
            "resolved_action": {
                "kind": "UTILITY_BILL_PAYMENT" if is_bill_payment else "BENEFICIARY_TRANSFER",
                "source_account_id": row["source_account_id"],
                "beneficiary_id": row["beneficiary_id"],
                "destination_account_id": destination_account_id,
                "recipient_display_name": beneficiary["display_name"],
                "masked_destination": beneficiary["masked_destination"],
                "amount_minor": row["amount_minor"],
                "currency": row["currency"],
                "destination_kind": row["destination_kind"] if "destination_kind" in row.keys() else None,
                "biller_id": row["destination_reference"] if is_bill_payment else None,
                "biller_display_name": beneficiary["display_name"] if is_bill_payment else None,
                "payment_reference": row["lookup_reference"] if is_bill_payment else None,
                "balance_before_minor": source["balance_minor"] if is_bill_payment else None,
                "balance_after_minor": source_balance_after if is_bill_payment else None,
            },
        }
        if approval is not None:
            response["approval"] = dict(approval)
        audit_log.append(
            connection,
            "LEDGER_COMMITTED",
            {
                "transaction_id": transaction_id,
                "receipt_digest": row["receipt_digest"],
                "source_account_id": row["source_account_id"],
                "destination_account_id": destination_account_id,
                "amount_minor": row["amount_minor"],
                "currency": row["currency"],
                "signature_id": (approval or {}).get("signature_id"),
            },
            now=now,
            request_id=row["request_id"],
            receipt_id=row["receipt_id"],
        )
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

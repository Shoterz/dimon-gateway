"""Payee knowledge the server owns: nicknames, payment history, how new a
payee is, and whether an amount is unusual for that payee.

Everything here is read from the ledger and the payee directory, never from
model output, so it can only ever add caution: it helps tell same-named
payees apart, flags amounts that do not fit the history ("fifteen" heard as
"fifty"), and marks payees that are new to the user.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

NEW_PAYEE_WINDOW = timedelta(hours=24)
UNUSUAL_MULTIPLE = 3          # more than 3x the largest past payment is unusual
UNUSUAL_MIN_HISTORY = 2       # needs at least two past payments to judge

PAYEE_SCHEMA = """
CREATE TABLE IF NOT EXISTS beneficiary_aliases (
  alias TEXT PRIMARY KEY,
  beneficiary_id TEXT NOT NULL REFERENCES beneficiaries(beneficiary_id)
);
CREATE TABLE IF NOT EXISTS prior_payments (
  payment_id TEXT PRIMARY KEY,
  beneficiary_id TEXT NOT NULL REFERENCES beneficiaries(beneficiary_id),
  amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
  paid_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payee_registrations (
  beneficiary_id TEXT PRIMARY KEY REFERENCES beneficiaries(beneficiary_id),
  added_at TEXT NOT NULL,
  origin TEXT NOT NULL
);
"""

# The user's family contact and nicknames. Singaporeans say "my mum", "mummy",
# "ma"; each resolves to the same saved payee.
# Her name shares no word with another payee, so "Tan" or "John" stay unambiguous.
MUM_ACCOUNT = ("acct_mum_01", "Lim Siew Hua", "Lim Siew Hua", "lim siew hua", "SGD", 20000, 0)
MUM_BENEFICIARY = ("beneficiary_mum_01", "Lim Siew Hua", "lim siew hua", "acct_mum_01", "•••• 1357")
MUM_ALIASES = ("mum", "my mum", "mom", "my mom", "mummy", "my mummy", "mother", "my mother", "ma", "mama")

# Payments made before this ledger existed, as (days ago, amount in cents).
SEED_HISTORY = {
    "beneficiary_alicia_powell_01": ((3, 1200), (11, 800), (26, 1500), (40, 500)),
    "beneficiary_john_smith_01": ((6, 2000), (35, 5000)),
    "beneficiary_john_tan_01": ((180, 3000),),
    "beneficiary_mum_01": ((5, 5000), (35, 5000), (65, 5000)),
}


def seed_payees(connection: sqlite3.Connection, now: datetime) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO accounts (
          account_id, owner_display_name, label, normalized_label,
          currency, balance_minor, is_user_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        MUM_ACCOUNT,
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO beneficiaries (
          beneficiary_id, display_name, normalized_alias,
          destination_account_id, masked_destination
        ) VALUES (?, ?, ?, ?, ?)
        """,
        MUM_BENEFICIARY,
    )
    connection.executemany(
        "INSERT OR IGNORE INTO beneficiary_aliases (alias, beneficiary_id) VALUES (?, ?)",
        [(alias, MUM_BENEFICIARY[0]) for alias in MUM_ALIASES],
    )
    if connection.execute("SELECT COUNT(*) FROM prior_payments").fetchone()[0]:
        return
    for beneficiary_id, payments in SEED_HISTORY.items():
        for days_ago, amount_minor in payments:
            connection.execute(
                "INSERT INTO prior_payments (payment_id, beneficiary_id, amount_minor, paid_at) VALUES (?, ?, ?, ?)",
                (
                    f"prior_{secrets.token_hex(8)}",
                    beneficiary_id,
                    amount_minor,
                    (now - timedelta(days=days_ago)).astimezone(timezone.utc).isoformat(),
                ),
            )


def alias_target(connection: sqlite3.Connection, label: str) -> str | None:
    row = connection.execute(
        "SELECT beneficiary_id FROM beneficiary_aliases WHERE alias = ?",
        (" ".join(label.casefold().split()),),
    ).fetchone()
    return row[0] if row else None


def aliases_for(connection: sqlite3.Connection, beneficiary_id: str) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT alias FROM beneficiary_aliases WHERE beneficiary_id = ? ORDER BY alias",
            (beneficiary_id,),
        )
    ]


def _payments(connection: sqlite3.Connection, beneficiary_id: str) -> list[tuple[str, int]]:
    """Every completed payment to this payee as (UTC timestamp, cents)."""

    rows = connection.execute(
        """
        SELECT paid_at, amount_minor FROM prior_payments WHERE beneficiary_id = :id
        UNION ALL
        SELECT e.executed_at, r.amount_minor
        FROM receipt_executions e JOIN review_receipts r ON r.receipt_id = e.receipt_id
        WHERE r.beneficiary_id = :id
        UNION ALL
        SELECT e.executed_at, i.amount_minor
        FROM batch_receipt_executions e JOIN batch_receipt_items i ON i.receipt_id = e.receipt_id
        WHERE i.beneficiary_id = :id
        """,
        {"id": beneficiary_id},
    ).fetchall()
    return sorted((row[0], row[1]) for row in rows)


@dataclass(frozen=True)
class PayeeHistory:
    beneficiary_id: str
    count: int
    last_paid_at: str | None
    last_amount_minor: int | None
    min_amount_minor: int | None
    max_amount_minor: int | None
    first_seen_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "last_paid_at": self.last_paid_at,
            "last_amount_minor": self.last_amount_minor,
            "typical_min_minor": self.min_amount_minor,
            "typical_max_minor": self.max_amount_minor,
        }


def history(connection: sqlite3.Connection, beneficiary_id: str) -> PayeeHistory:
    payments = _payments(connection, beneficiary_id)
    registration = connection.execute(
        "SELECT added_at FROM payee_registrations WHERE beneficiary_id = ?", (beneficiary_id,)
    ).fetchone()
    moments = [paid_at for paid_at, _ in payments] + ([registration[0]] if registration else [])
    amounts = [amount for _, amount in payments]
    return PayeeHistory(
        beneficiary_id=beneficiary_id,
        count=len(payments),
        last_paid_at=payments[-1][0] if payments else None,
        last_amount_minor=payments[-1][1] if payments else None,
        min_amount_minor=min(amounts) if amounts else None,
        max_amount_minor=max(amounts) if amounts else None,
        first_seen_at=min(moments) if moments else None,
    )


def is_new_payee(record: PayeeHistory, now: datetime) -> bool:
    """New = never paid before, or first paid/saved within the last 24 hours."""

    if record.first_seen_at is None:
        return True
    first_seen = datetime.fromisoformat(record.first_seen_at)
    return now.astimezone(timezone.utc) - first_seen.astimezone(timezone.utc) < NEW_PAYEE_WINDOW


def unusual_amount(record: PayeeHistory, amount_minor: int) -> dict[str, int] | None:
    if record.count < UNUSUAL_MIN_HISTORY or record.max_amount_minor is None:
        return None
    if amount_minor > UNUSUAL_MULTIPLE * record.max_amount_minor:
        return {
            "amount_minor": amount_minor,
            "typical_min_minor": record.min_amount_minor,
            "typical_max_minor": record.max_amount_minor,
        }
    return None


def _money(minor: int) -> str:
    return f"S${minor / 100:,.2f}" if minor % 100 else f"S${minor // 100:,}"


def describe_last_payment(record: PayeeHistory, now: datetime) -> str | None:
    """'paid S$20 6 days ago' / 'last paid 6 months ago' / None."""

    if record.last_paid_at is None:
        return None
    paid = datetime.fromisoformat(record.last_paid_at).astimezone(timezone.utc)
    days = max(0, (now.astimezone(timezone.utc) - paid).days)
    if days == 0:
        when = "today"
    elif days == 1:
        when = "yesterday"
    elif days < 14:
        when = f"{days} days ago"
    elif days < 60:
        when = f"{days // 7} weeks ago"
    else:
        when = f"{days // 30} months ago"
    if days < 60:
        return f"paid {_money(record.last_amount_minor)} {when}"
    return f"last paid {when}"


def describe_typical(record: PayeeHistory) -> str | None:
    if record.count < UNUSUAL_MIN_HISTORY:
        return None
    low, high = record.min_amount_minor, record.max_amount_minor
    return f"usually {_money(low)}" if low == high else f"usually {_money(low)}–{_money(high)}"


class PayeeSaveRejected(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


_NICKNAME = re.compile(r"[A-Za-z][A-Za-z '\-]{1,38}[A-Za-z]")


def save_paynow_payee(
    connection: sqlite3.Connection, *, receipt_id: str, nickname: str, now: datetime
) -> dict[str, Any]:
    """Save the recipient of a settled PayNow transfer as a named payee.

    The payee points at the destination PayNow already confirmed and is
    registered as new, so for its first 24 hours every payment to it needs
    the one-time code and stays under the new-payee cap.
    """

    nickname = " ".join(nickname.split())
    if not _NICKNAME.fullmatch(nickname):
        raise PayeeSaveRejected("INVALID_NICKNAME")
    receipt = connection.execute(
        """
        SELECT r.receipt_id, r.destination_account_id, r.destination_reference
        FROM review_receipts r JOIN receipt_executions e ON e.receipt_id = r.receipt_id
        WHERE r.receipt_id = ? AND r.destination_kind = 'PAYNOW_MOBILE'
        """,
        (receipt_id,),
    ).fetchone()
    if receipt is None:
        raise PayeeSaveRejected("PAYNOW_RECEIPT_NOT_SETTLED")
    if connection.execute(
        "SELECT 1 FROM beneficiaries WHERE destination_account_id = ? AND directory_kind = 'SAVED'",
        (receipt["destination_account_id"],),
    ).fetchone():
        raise PayeeSaveRejected("PAYEE_ALREADY_SAVED")
    normalized = nickname.casefold()
    taken = connection.execute(
        """
        SELECT 1 FROM beneficiaries WHERE normalized_alias = :n OR lower(display_name) = :n
        UNION SELECT 1 FROM beneficiary_aliases WHERE alias = :n
        """,
        {"n": normalized},
    ).fetchone()
    if taken:
        raise PayeeSaveRejected("NICKNAME_IN_USE")
    digits = re.sub(r"\D", "", receipt["destination_reference"] or "")
    beneficiary_id = f"beneficiary_saved_{secrets.token_hex(8)}"
    connection.execute(
        """
        INSERT INTO beneficiaries (
          beneficiary_id, display_name, normalized_alias,
          destination_account_id, masked_destination, directory_kind
        ) VALUES (?, ?, ?, ?, ?, 'SAVED')
        """,
        (beneficiary_id, nickname, normalized, receipt["destination_account_id"], f"PayNow •••• {digits[-4:]}"),
    )
    connection.execute(
        "INSERT INTO payee_registrations (beneficiary_id, added_at, origin) VALUES (?, ?, 'PAYNOW')",
        (beneficiary_id, now.astimezone(timezone.utc).isoformat()),
    )
    return {
        "beneficiary_id": beneficiary_id,
        "display_name": nickname,
        "masked_destination": f"PayNow •••• {digits[-4:]}",
        "new_payee_until": (now + NEW_PAYEE_WINDOW).astimezone(timezone.utc).isoformat(),
    }

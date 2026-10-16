"""Deterministic KYC, value, velocity, and friction-tier policy for the gateway.

Evaluated twice: once when a draft is shown (early, non-authoritative) and
again inside the executing ledger transaction (authoritative), so no change
between review and approval can slip a movement past the rules.

Friction is matched to risk. Every executable transfer needs the user's
device signature; more risk adds more checks, and signals can only ever add
checks, never remove them:

  transfer receipt, under S$200      mock “yes, confirm” + device signature
  transfer receipt, S$200+           Singpass + face + device signature
  multi-payee batch                  threshold uses the combined receipt total
  transfer at S$200+                 + spoken read-back of the draft
  new payee, or over S$2,000         + hardware-token code (and read-back)
  amount unusual for this payee      + explicit "yes, that amount" confirmation
  new payee in its first 24 hours    capped at S$100 per transfer
  more than 5 transfers in 10 min    paused

The hardware-token code is an HMAC over the exact receipt digest (dynamic
linking): a code for one receipt cannot confirm another.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .payees import history as payee_history, is_new_payee, unusual_amount

SGT = timezone(timedelta(hours=8))
VELOCITY_WINDOW = timedelta(minutes=10)
OOB_MAX_FAILURES = 3
TRANSFER_SINGPASS_THRESHOLD_MINOR = 20_000  # S$200, inclusive
MOCK_VOICE_CONFIRMATION = "yes, confirm"


def authentication_for_amount_minor(total_minor: int) -> dict[str, object]:
    """Return the common SGD payment authentication tier for an amount."""

    requires_singpass = total_minor >= TRANSFER_SINGPASS_THRESHOLD_MINOR
    return {
        "authentication_tier": (
            "SINGPASS_AND_FACE" if requires_singpass else "MOCK_VOICE_CONFIRMATION"
        ),
        "requires_voice_confirmation": not requires_singpass,
        "requires_singpass": requires_singpass,
        "requires_face": requires_singpass,
        "voice_confirmation_phrase": (
            None if requires_singpass else MOCK_VOICE_CONFIRMATION
        ),
    }

POLICY_SCHEMA = """
CREATE TABLE IF NOT EXISTS customer_profiles (
  user_id TEXT PRIMARY KEY,
  kyc_status TEXT NOT NULL CHECK(kyc_status IN ('VERIFIED', 'PENDING', 'EXPIRED', 'REJECTED')),
  kyc_verified_at TEXT,
  kyc_expires_at TEXT,
  per_transaction_limit_minor INTEGER NOT NULL CHECK(per_transaction_limit_minor > 0),
  daily_limit_minor INTEGER NOT NULL CHECK(daily_limit_minor > 0),
  out_of_band_threshold_minor INTEGER NOT NULL CHECK(out_of_band_threshold_minor > 0),
  velocity_max_transfers INTEGER NOT NULL CHECK(velocity_max_transfers > 0),
  micro_transfer_ceiling_minor INTEGER NOT NULL CHECK(micro_transfer_ceiling_minor > 0),
  velocity_max_micro_transfers INTEGER NOT NULL CHECK(velocity_max_micro_transfers > 0),
  token_secret TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS out_of_band_attempts (
  receipt_id TEXT PRIMARY KEY,
  failures INTEGER NOT NULL CHECK(failures >= 0)
);
"""

PROFILE_DEFAULTS = {
    "per_transaction_limit_minor": 500_000,     # S$5,000 per movement
    "daily_limit_minor": 1_000_000,             # S$10,000 per Singapore calendar day
    "out_of_band_threshold_minor": 200_000,     # over S$2,000 needs the hardware token
    "read_back_threshold_minor": 20_000,        # S$200 and above is read back aloud
    "new_payee_cap_minor": 10_000,              # S$100 per transfer while a payee is new
    "velocity_max_transfers": 5,                # per 10 minutes, then paused
    "micro_transfer_ceiling_minor": 1_000,      # "small" = under S$10
    "velocity_max_micro_transfers": 3,          # small transfers per 10 minutes
}

# Columns added after the first release, with their defaults.
_ADDED_COLUMNS = {
    "read_back_threshold_minor": PROFILE_DEFAULTS["read_back_threshold_minor"],
    "new_payee_cap_minor": PROFILE_DEFAULTS["new_payee_cap_minor"],
}
# The first release's limits; profiles still on them move to the tiered defaults.
_FIRST_RELEASE_LIMITS = (50_000, 80_000, 20_000)


class PolicyRejected(Exception):
    def __init__(self, reason_code: str, *, persist: bool = False):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.persist = persist


@dataclass(frozen=True)
class PolicyDecision:
    tier: str
    requires_out_of_band: bool
    requires_read_back: bool
    authentication_tier: str = "MOCK_VOICE_CONFIRMATION"
    requires_voice_confirmation: bool = True
    requires_singpass: bool = False
    requires_face: bool = False
    unusual_amounts: tuple[dict, ...] = field(default_factory=tuple)
    new_payees: tuple[str, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)
    kyc_status: str = "VERIFIED"
    per_transaction_limit_minor: int = 0
    daily_remaining_minor: int = 0

    @property
    def requires_unusual_confirmation(self) -> bool:
        return bool(self.unusual_amounts)

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "requires_out_of_band": self.requires_out_of_band,
            "requires_read_back": self.requires_read_back,
            "authentication_tier": self.authentication_tier,
            "requires_voice_confirmation": self.requires_voice_confirmation,
            "requires_singpass": self.requires_singpass,
            "requires_face": self.requires_face,
            "requires_unusual_confirmation": self.requires_unusual_confirmation,
            "unusual_amounts": list(self.unusual_amounts),
            "new_payees": list(self.new_payees),
            "reasons": list(self.reasons),
            "kyc_status": self.kyc_status,
            "per_transaction_limit_minor": self.per_transaction_limit_minor,
            "daily_remaining_minor": self.daily_remaining_minor,
        }


def migrate_profiles(connection: sqlite3.Connection) -> None:
    present = {row[1] for row in connection.execute("PRAGMA table_info(customer_profiles)")}
    for column, default in _ADDED_COLUMNS.items():
        if column not in present:
            connection.execute(
                f"ALTER TABLE customer_profiles ADD COLUMN {column} INTEGER NOT NULL DEFAULT {int(default)}"
            )
    old_txn, old_daily, old_oob = _FIRST_RELEASE_LIMITS
    connection.execute(
        """
        UPDATE customer_profiles
        SET per_transaction_limit_minor = ?, daily_limit_minor = ?, out_of_band_threshold_minor = ?
        WHERE per_transaction_limit_minor = ? AND daily_limit_minor = ? AND out_of_band_threshold_minor = ?
        """,
        (
            PROFILE_DEFAULTS["per_transaction_limit_minor"],
            PROFILE_DEFAULTS["daily_limit_minor"],
            PROFILE_DEFAULTS["out_of_band_threshold_minor"],
            old_txn,
            old_daily,
            old_oob,
        ),
    )


def seed_profile(connection: sqlite3.Connection, user_id: str, now: datetime, *, kyc_status: str = "VERIFIED") -> None:
    migrate_profiles(connection)
    verified = kyc_status == "VERIFIED"
    connection.execute(
        """
        INSERT OR IGNORE INTO customer_profiles (
          user_id, kyc_status, kyc_verified_at, kyc_expires_at,
          per_transaction_limit_minor, daily_limit_minor,
          out_of_band_threshold_minor, velocity_max_transfers,
          micro_transfer_ceiling_minor, velocity_max_micro_transfers, token_secret,
          read_back_threshold_minor, new_payee_cap_minor
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            kyc_status,
            now.astimezone(timezone.utc).isoformat() if verified else None,
            (now + timedelta(days=365)).astimezone(timezone.utc).isoformat() if verified else None,
            PROFILE_DEFAULTS["per_transaction_limit_minor"],
            PROFILE_DEFAULTS["daily_limit_minor"],
            PROFILE_DEFAULTS["out_of_band_threshold_minor"],
            PROFILE_DEFAULTS["velocity_max_transfers"],
            PROFILE_DEFAULTS["micro_transfer_ceiling_minor"],
            PROFILE_DEFAULTS["velocity_max_micro_transfers"],
            secrets.token_hex(32),
            PROFILE_DEFAULTS["read_back_threshold_minor"],
            PROFILE_DEFAULTS["new_payee_cap_minor"],
        ),
    )


def _profile(connection: sqlite3.Connection, user_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM customer_profiles WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        raise PolicyRejected("KYC_PROFILE_MISSING")
    return row


def _executions_since(connection: sqlite3.Connection, since: datetime) -> list[int]:
    """Amounts of every executed movement since `since`, one entry per payee."""

    cutoff = since.astimezone(timezone.utc).isoformat()
    single = [
        row[0]
        for row in connection.execute(
            "SELECT amount_minor FROM ledger_transactions WHERE created_at >= ?", (cutoff,)
        )
    ]
    batch = [
        row[0]
        for row in connection.execute(
            """
            SELECT e.delta_minor FROM batch_ledger_entries e
            JOIN batch_ledger_transactions t ON t.batch_transaction_id = e.batch_transaction_id
            WHERE t.created_at >= ? AND e.item_position IS NOT NULL
            """,
            (cutoff,),
        )
    ]
    return single + batch


def check_kyc(connection: sqlite3.Connection, user_id: str, now: datetime) -> sqlite3.Row:
    profile = _profile(connection, user_id)
    if profile["kyc_status"] != "VERIFIED":
        raise PolicyRejected(f"KYC_{profile['kyc_status']}")
    expires = profile["kyc_expires_at"]
    if not expires or now.astimezone(timezone.utc) >= datetime.fromisoformat(expires):
        raise PolicyRejected("KYC_EXPIRED")
    return profile


def evaluate(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    items: tuple[tuple[str, int], ...],
    now: datetime,
    require_kyc: bool = True,
    authentication_total_minor: int | None = None,
    check_payee_risk: bool = True,
) -> PolicyDecision:
    """Raise PolicyRejected unless this movement is allowed; otherwise return
    the checks it needs. `items` is ((beneficiary_id, amount_minor), ...)."""

    profile = check_kyc(connection, user_id, now) if require_kyc else _profile(connection, user_id)
    amounts = tuple(amount for _, amount in items)
    total = sum(amounts)
    if any(amount > profile["per_transaction_limit_minor"] for amount in amounts) or (
        total > profile["per_transaction_limit_minor"]
    ):
        raise PolicyRejected("AMOUNT_LIMIT_EXCEEDED")

    new_payees: list[str] = []
    unusual: list[dict] = []
    if check_payee_risk:
        for beneficiary_id, amount in items:
            record = payee_history(connection, beneficiary_id)
            if is_new_payee(record, now):
                new_payees.append(beneficiary_id)
                if amount > profile["new_payee_cap_minor"]:
                    raise PolicyRejected("NEW_PAYEE_LIMIT_EXCEEDED")
            flagged = unusual_amount(record, amount)
            if flagged:
                unusual.append({"beneficiary_id": beneficiary_id, **flagged})

    day_start = now.astimezone(SGT).replace(hour=0, minute=0, second=0, microsecond=0)
    spent_today = sum(_executions_since(connection, day_start))
    if spent_today + total > profile["daily_limit_minor"]:
        raise PolicyRejected("DAILY_LIMIT_EXCEEDED")

    recent = _executions_since(connection, now - VELOCITY_WINDOW)
    if len(recent) + len(amounts) > profile["velocity_max_transfers"]:
        raise PolicyRejected("VELOCITY_LIMIT_EXCEEDED")
    ceiling = profile["micro_transfer_ceiling_minor"]
    new_micro = sum(1 for amount in amounts if amount < ceiling)
    if new_micro and (
        sum(1 for amount in recent if amount < ceiling) + new_micro
        > profile["velocity_max_micro_transfers"]
    ):
        raise PolicyRejected("MICRO_TRANSFER_RATE_LIMITED")

    # Friction only ever goes up: each signal adds checks.
    reasons: list[str] = []
    requires_out_of_band = False
    requires_read_back = False
    if total > profile["out_of_band_threshold_minor"]:
        requires_out_of_band = True
        reasons.append("HIGH_VALUE")
    if new_payees:
        requires_out_of_band = True
        reasons.append("NEW_PAYEE")
    if total >= profile["read_back_threshold_minor"]:
        requires_read_back = True
        reasons.append("READ_BACK_AMOUNT")
    if unusual:
        requires_read_back = True
        reasons.append("UNUSUAL_AMOUNT")
    tier = "SIGN_AND_TOKEN" if requires_out_of_band else "SIGN_AND_READ_BACK" if requires_read_back else "SIGN"
    authentication_total = (
        total if authentication_total_minor is None else authentication_total_minor
    )
    if type(authentication_total) is not int or authentication_total < total:
        raise PolicyRejected("INVALID_AUTHENTICATION_TOTAL")
    authentication = authentication_for_amount_minor(authentication_total)
    requires_singpass = bool(authentication["requires_singpass"])
    return PolicyDecision(
        tier=tier,
        requires_out_of_band=requires_out_of_band,
        requires_read_back=requires_read_back,
        authentication_tier=str(authentication["authentication_tier"]),
        requires_voice_confirmation=bool(authentication["requires_voice_confirmation"]),
        requires_singpass=requires_singpass,
        requires_face=requires_singpass,
        unusual_amounts=tuple(unusual),
        new_payees=tuple(new_payees),
        reasons=tuple(reasons),
        kyc_status=profile["kyc_status"],
        per_transaction_limit_minor=profile["per_transaction_limit_minor"],
        daily_remaining_minor=profile["daily_limit_minor"] - spent_today,
    )


def token_code(secret_hex: str, receipt_id: str, receipt_digest: str) -> str:
    """The 6-digit code the hardware token displays for one receipt."""

    mac = hmac.new(
        bytes.fromhex(secret_hex),
        f"dimon-oob-v1:{receipt_id}:{receipt_digest}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{int.from_bytes(mac[:4], 'big') % 1_000_000:06d}"


def mock_token_code(
    connection: sqlite3.Connection, user_id: str, receipt_id: str, receipt_digest: str
) -> str:
    return token_code(_profile(connection, user_id)["token_secret"], receipt_id, receipt_digest)


def verify_out_of_band(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    receipt_id: str,
    receipt_digest: str,
    code: str | None,
) -> None:
    attempts = connection.execute(
        "SELECT failures FROM out_of_band_attempts WHERE receipt_id = ?", (receipt_id,)
    ).fetchone()
    failures = attempts["failures"] if attempts else 0
    if failures >= OOB_MAX_FAILURES:
        raise PolicyRejected("OUT_OF_BAND_LOCKED")
    if code is None:
        raise PolicyRejected("OUT_OF_BAND_REQUIRED")
    expected = mock_token_code(connection, user_id, receipt_id, receipt_digest)
    if not hmac.compare_digest(expected, code):
        connection.execute(
            """
            INSERT INTO out_of_band_attempts (receipt_id, failures) VALUES (?, 1)
            ON CONFLICT(receipt_id) DO UPDATE SET failures = failures + 1
            """,
            (receipt_id,),
        )
        raise PolicyRejected("OUT_OF_BAND_CODE_INVALID", persist=True)


def verify_requirements(
    decision: PolicyDecision,
    *,
    read_back_played: bool,
    unusual_amount_confirmed: bool,
    voice_confirmation: str | None = None,
) -> None:
    if decision.requires_voice_confirmation and voice_confirmation != MOCK_VOICE_CONFIRMATION:
        raise PolicyRejected("VOICE_CONFIRMATION_REQUIRED")
    if decision.requires_unusual_confirmation and not unusual_amount_confirmed:
        raise PolicyRejected("UNUSUAL_AMOUNT_UNCONFIRMED")
    if decision.requires_read_back and not read_back_played:
        raise PolicyRejected("READ_BACK_REQUIRED")

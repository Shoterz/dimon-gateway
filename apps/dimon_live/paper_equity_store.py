"""SQLite persistence for isolated real-equity paper orders."""

from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator

from .paper_equity import (
    PaperEquityDraft,
    PaperEquityReceipt,
    PaperFxSnapshot,
    PaperInstrument,
    PaperOrderStatus,
    PaperOrderType,
    PaperQuoteSnapshot,
    PaperTimeInForce,
    build_receipt,
    decide_fill,
    receipt_from_dict,
)


PAPER_EQUITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_equity_orders (
  order_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  conversation_version INTEGER NOT NULL,
  request_id TEXT NOT NULL,
  session_scope TEXT NOT NULL,
  user_id TEXT NOT NULL,
  order_digest TEXT NOT NULL UNIQUE,
  receipt_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN (
    'AWAITING_APPROVAL', 'AUTHORIZED', 'QUEUED', 'OPEN', 'FILLED',
    'CANCELLED', 'EXPIRED', 'REJECTED', 'REQUOTE_REQUIRED', 'SUPERSEDED'
  )),
  created_at TEXT NOT NULL,
  authorized_at TEXT,
  executed_at TEXT,
  reason_code TEXT,
  result_json TEXT,
  UNIQUE(conversation_id, conversation_version)
);
CREATE INDEX IF NOT EXISTS paper_equity_orders_user_status
  ON paper_equity_orders(user_id, status);
CREATE TABLE IF NOT EXISTS paper_equity_trades (
  trade_id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL UNIQUE REFERENCES paper_equity_orders(order_id),
  user_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
  quantity INTEGER NOT NULL CHECK(quantity > 0),
  fill_price_minor INTEGER NOT NULL CHECK(fill_price_minor > 0),
  quote_currency TEXT NOT NULL,
  funding_debit_minor INTEGER NOT NULL CHECK(funding_debit_minor > 0),
  funding_currency TEXT NOT NULL,
  fee_funding_minor INTEGER NOT NULL CHECK(fee_funding_minor >= 0),
  executed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_equity_holdings (
  user_id TEXT NOT NULL,
  instrument_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  name TEXT NOT NULL,
  exchange TEXT NOT NULL,
  quote_currency TEXT NOT NULL,
  quantity INTEGER NOT NULL CHECK(quantity >= 0),
  cost_quote_minor INTEGER NOT NULL CHECK(cost_quote_minor >= 0),
  cost_funding_minor INTEGER NOT NULL CHECK(cost_funding_minor >= 0),
  updated_at TEXT NOT NULL,
  PRIMARY KEY(user_id, instrument_id)
);
CREATE TABLE IF NOT EXISTS paper_investment_cash_entries (
  entry_id TEXT PRIMARY KEY,
  trade_id TEXT NOT NULL REFERENCES paper_equity_trades(trade_id),
  user_id TEXT NOT NULL,
  account_ref TEXT NOT NULL,
  delta_minor INTEGER NOT NULL CHECK(delta_minor != 0),
  currency TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS paper_equity_cash_by_user
  ON paper_investment_cash_entries(user_id, account_ref);
"""


def migrate(connection: sqlite3.Connection) -> None:
    """Ledgers made before sales were possible only allowed BUY trades."""

    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'paper_equity_trades'"
    ).fetchone()
    if row is None or "CHECK(side = 'BUY')" not in (row[0] or ""):
        return
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ALTER TABLE paper_equity_trades RENAME TO paper_equity_trades_buy_only")
        connection.execute(
            row[0].replace("CHECK(side = 'BUY')", "CHECK(side IN ('BUY', 'SELL'))")
        )
        connection.execute("INSERT INTO paper_equity_trades SELECT * FROM paper_equity_trades_buy_only")
        connection.execute("DROP TABLE paper_equity_trades_buy_only")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _minor(value: object, field: str) -> int:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} invalid") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field} invalid")
    return int((parsed * 100).to_integral_value())


def _state_fx(state: dict[str, Any], *, quote_currency: str, funding_currency: str) -> PaperFxSnapshot:
    pair = state.get("fx_pair")
    rate = state.get("fx_rate")
    source = state.get("fx_source") or state.get("quote_source") or "SERVER_SNAPSHOT"
    captured_at = state.get("fx_captured_at") or state.get("quote_captured_at")
    if not pair or rate is None or captured_at is None:
        pair = f"{quote_currency}/{funding_currency}"
        rate = "1"
        captured_at = state["quote_captured_at"]
    pair = str(pair).upper()
    expected = f"{quote_currency.upper()}/{funding_currency.upper()}"
    if pair != expected:
        inverse = f"{funding_currency.upper()}/{quote_currency.upper()}"
        if pair != inverse:
            raise ValueError("PAPER_EQUITY_FX_PAIR_MISMATCH")
        rate = Decimal("1") / Decimal(str(rate))
        pair = expected
    return PaperFxSnapshot(
        pair=pair,
        rate=Decimal(str(rate)),
        captured_at=datetime.fromisoformat(str(captured_at)),
        source=str(source),
    )


class PaperEquityStore:
    def __init__(self, path: Path, *, clock: Callable[[], datetime]):
        self.path = Path(path)
        self.clock = clock
        self.fail_after_receipt_insert = False
        self.fail_after_trade_insert = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 3000")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _receipt_json(receipt: PaperEquityReceipt) -> str:
        payload = receipt.to_dict()
        payload["canonical_json"] = receipt.canonical_json
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _draft_from_state(
        self,
        state: dict[str, Any],
        *,
        user_id: str,
        conversation_id: str,
        conversation_version: int,
    ) -> PaperEquityDraft:
        quote_currency = str(state["quote_currency"]).upper()
        funding_currency = str(state["funding_currency"]).upper()
        instrument = PaperInstrument(
            instrument_id=str(state.get("instrument_id") or f"YF:{state['symbol']}"),
            symbol=str(state["symbol"]),
            name=str(state["name"]),
            exchange=str(state["exchange"]),
            quote_currency=quote_currency,
        )
        order_type = PaperOrderType(str(state.get("order_type") or ""))
        tif = PaperTimeInForce(str(state.get("time_in_force") or ""))
        quote_captured_at = datetime.fromisoformat(str(state["quote_captured_at"]))
        quote = PaperQuoteSnapshot(
            price_minor=_minor(state["price"], "price"),
            market_state=state.get("market_state", "UNKNOWN"),
            captured_at=quote_captured_at,
            source=str(state.get("quote_source") or "SERVER_SNAPSHOT"),
        )
        return PaperEquityDraft(
            user_id=user_id,
            conversation_id=conversation_id,
            conversation_version=conversation_version,
            funding_account_id=str(state.get("funding_account_id") or "acct_savings_01"),
            instrument=instrument,
            quantity=int(state["whole_shares"]),
            allocation_minor=_minor(state["requested_allocation"], "requested_allocation"),
            allocation_currency=str(state["allocation_currency"]).upper(),
            funding_currency=funding_currency,
            order_type=order_type,
            limit_price_minor=(
                _minor(state["limit_price"], "limit_price")
                if state.get("limit_price") is not None
                else None
            ),
            time_in_force=tif,
            quote_snapshot=quote,
            fx_snapshot=_state_fx(
                state,
                quote_currency=quote_currency,
                funding_currency=funding_currency,
            ),
            side="SELL" if state.get("side") == "SELL" else "BUY",
            # A share-count request records an estimated allocation for display,
            # not a user-selected spending ceiling. Amount-based requests remain
            # hard caps exactly as spoken.
            allocation_is_cap=state.get("requested_quantity") is None,
        )

    @staticmethod
    def _held(connection: sqlite3.Connection, user_id: str, instrument_id: str) -> int:
        row = connection.execute(
            "SELECT quantity FROM paper_equity_holdings WHERE user_id = ? AND instrument_id = ?",
            (user_id, instrument_id),
        ).fetchone()
        return int(row["quantity"]) if row else 0

    @staticmethod
    def _account_balance(connection: sqlite3.Connection, account_id: str, currency: str) -> int:
        row = connection.execute(
            "SELECT balance_minor, currency FROM accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None or row["currency"] != currency:
            raise ValueError("PAPER_EQUITY_FUNDING_ACCOUNT_INVALID")
        return int(row["balance_minor"])

    def consume_ready_conversation(
        self,
        *,
        conversation_id: str,
        conversation_version: int,
        session_scope: str,
        user_id: str,
        request_id: str,
    ) -> PaperEquityReceipt:
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM paper_equity_orders WHERE conversation_id = ? AND conversation_version = ?",
                    (conversation_id, conversation_version),
                ).fetchone()
                if existing is not None:
                    if existing["session_scope"] != session_scope or existing["user_id"] != user_id:
                        raise ValueError("CONVERSATION_NOT_FOUND")
                    connection.commit()
                    return receipt_from_dict(json.loads(existing["receipt_json"]))

                thread = connection.execute(
                    "SELECT * FROM conversation_threads WHERE conversation_id = ? AND session_scope = ? AND user_id = ?",
                    (conversation_id, session_scope, user_id),
                ).fetchone()
                if thread is None:
                    raise ValueError("CONVERSATION_NOT_FOUND")
                if int(thread["version"]) != conversation_version:
                    raise ValueError("CONVERSATION_VERSION_CONFLICT")
                if thread["status"] != "READY_FOR_DRAFT":
                    raise ValueError("CONVERSATION_TERMINAL")
                state = json.loads(thread["state_json"])
                draft = self._draft_from_state(
                    state,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    conversation_version=conversation_version,
                )
                receipt = build_receipt(
                    draft,
                    order_id=f"paper_order_{secrets.token_hex(12)}",
                    nonce=f"paper_nonce_{secrets.token_hex(12)}",
                    now=now,
                )
                balance = self._account_balance(
                    connection,
                    draft.funding_account_id,
                    draft.funding_currency,
                )
                if receipt.side == "SELL":
                    if self._held(connection, user_id, receipt.instrument.instrument_id) < receipt.quantity:
                        raise ValueError("PAPER_EQUITY_NOT_ENOUGH_SHARES")
                elif balance < receipt.max_funding_debit_minor:
                    raise ValueError("PAPER_EQUITY_INSUFFICIENT_FUNDS")
                receipt_json = self._receipt_json(receipt)
                connection.execute(
                    """
                    INSERT INTO paper_equity_orders (
                      order_id, conversation_id, conversation_version, request_id,
                      session_scope, user_id, order_digest, receipt_json, status,
                      created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.order_id,
                        receipt.conversation_id,
                        receipt.conversation_version,
                        request_id,
                        session_scope,
                        user_id,
                        receipt.digest,
                        receipt_json,
                        receipt.status.value,
                        _utc(now),
                    ),
                )
                if self.fail_after_receipt_insert:
                    raise RuntimeError("PAPER_EQUITY_TEST_ROLLBACK")
                changed = connection.execute(
                    """
                    UPDATE conversation_threads
                    SET status = 'CONSUMED', updated_at = ?
                    WHERE conversation_id = ? AND status = 'READY_FOR_DRAFT' AND version = ?
                    """,
                    (_utc(now), conversation_id, conversation_version),
                )
                if changed.rowcount != 1:
                    raise ValueError("CONVERSATION_VERSION_CONFLICT")
                connection.commit()
                return receipt
            except Exception:
                connection.rollback()
                raise

    def get_order(
        self,
        order_id: str,
        *,
        user_id: str | None = None,
        session_scope: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM paper_equity_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if row is None:
            raise ValueError("PAPER_EQUITY_ORDER_NOT_FOUND")
        if user_id is not None and row["user_id"] != user_id:
            raise ValueError("PAPER_EQUITY_ORDER_NOT_FOUND")
        if session_scope is not None and row["session_scope"] != session_scope:
            raise ValueError("PAPER_EQUITY_ORDER_NOT_FOUND")
        payload = json.loads(row["receipt_json"])
        payload["status"] = row["status"]
        payload["order_digest"] = row["order_digest"]
        payload["request_id"] = row["request_id"]
        return payload

    def cancel_order(
        self,
        order_id: str,
        digest: str,
        *,
        user_id: str,
        session_scope: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM paper_equity_orders WHERE order_id = ? AND user_id = ? AND session_scope = ?",
                    (order_id, user_id, session_scope),
                ).fetchone()
                if row is None:
                    raise ValueError("PAPER_EQUITY_ORDER_NOT_FOUND")
                if row["order_digest"] != digest:
                    raise ValueError("PAPER_EQUITY_DIGEST_MISMATCH")
                if row["status"] in {"FILLED", "REJECTED", "EXPIRED", "CANCELLED", "SUPERSEDED"}:
                    if row["result_json"]:
                        connection.commit()
                        return json.loads(row["result_json"])
                    raise ValueError("PAPER_EQUITY_ORDER_TERMINAL")
                result = {"status": "CANCELLED", "reason_code": "USER_CANCELLED"}
                connection.execute(
                    "UPDATE paper_equity_orders SET status = 'CANCELLED', reason_code = ?, result_json = ? WHERE order_id = ?",
                    (result["reason_code"], json.dumps(result, sort_keys=True), order_id),
                )
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def supersede_for_revision(self, order_id: str, digest: str, *, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT order_digest, user_id, status FROM paper_equity_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise ValueError("PAPER_EQUITY_ORDER_NOT_FOUND")
            if row["user_id"] != user_id or row["order_digest"] != digest:
                raise ValueError("PAPER_EQUITY_DIGEST_MISMATCH")
            if row["status"] not in {"AWAITING_APPROVAL", "AUTHORIZED", "OPEN", "QUEUED"}:
                raise ValueError("PAPER_EQUITY_ORDER_TERMINAL")
            connection.execute(
                "UPDATE paper_equity_orders SET status = 'SUPERSEDED', reason_code = 'REVISED' WHERE order_id = ?",
                (order_id,),
            )
            connection.commit()

    def _apply_execution_on_connection(
        self,
        connection: sqlite3.Connection,
        order_id: str,
        digest: str,
        execution: Any,
        *,
        user_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM paper_equity_orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            raise ValueError("PAPER_EQUITY_ORDER_NOT_FOUND")
        if row["user_id"] != user_id:
            raise ValueError("PAPER_EQUITY_USER_MISMATCH")
        if row["order_digest"] != digest:
            raise ValueError("PAPER_EQUITY_DIGEST_MISMATCH")
        terminal = {"FILLED", "REJECTED", "EXPIRED", "REQUOTE_REQUIRED", "CANCELLED", "SUPERSEDED"}
        if row["status"] in terminal:
            if row["result_json"]:
                return json.loads(row["result_json"])
            raise ValueError("PAPER_EQUITY_ORDER_TERMINAL")
        if row["status"] not in {"AUTHORIZED", "OPEN", "QUEUED"}:
            raise ValueError("PAPER_EQUITY_NOT_AUTHORIZED")

        receipt = receipt_from_dict(json.loads(row["receipt_json"]))
        decision = decide_fill(receipt, execution, now=now)
        result: dict[str, Any] = {
            "status": decision.status.value,
            "reason_code": decision.reason_code,
        }
        status = decision.status.value
        if decision.status.value == "FILLED" and receipt.side == "SELL":
            status, result = self._settle_sale(connection, receipt, decision, user_id=user_id, now=now)
        elif decision.status.value == "FILLED":
            balance = self._account_balance(connection, receipt.funding_account_id, receipt.funding_currency)
            assert decision.funding_debit_minor is not None
            if balance < decision.funding_debit_minor:
                status = "REJECTED"
                result = {
                    "status": status,
                    "reason_code": "PAPER_EQUITY_INSUFFICIENT_FUNDS",
                }
            else:
                trade_id = f"paper_trade_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO paper_equity_trades (
                      trade_id, order_id, user_id, symbol, side, quantity,
                      fill_price_minor, quote_currency, funding_debit_minor,
                      funding_currency, fee_funding_minor, executed_at
                    ) VALUES (?, ?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_id, receipt.order_id, user_id, receipt.instrument.symbol,
                        receipt.quantity, decision.fill_price_minor,
                        receipt.instrument.quote_currency, decision.funding_debit_minor,
                        receipt.funding_currency, decision.fee_funding_minor, _utc(now),
                    ),
                )
                if self.fail_after_trade_insert:
                    raise RuntimeError("PAPER_EQUITY_TEST_EXECUTION_ROLLBACK")
                connection.execute(
                    "UPDATE accounts SET balance_minor = balance_minor - ? WHERE account_id = ? AND balance_minor >= ?",
                    (decision.funding_debit_minor, receipt.funding_account_id, decision.funding_debit_minor),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ValueError("PAPER_EQUITY_INSUFFICIENT_FUNDS")
                connection.execute(
                    """
                    INSERT INTO paper_investment_cash_entries (
                      entry_id, trade_id, user_id, account_ref, delta_minor,
                      currency, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"paper_cash_{uuid.uuid4().hex}", trade_id, user_id,
                        receipt.funding_account_id, -decision.funding_debit_minor,
                        receipt.funding_currency, _utc(now),
                    ),
                )
                existing = connection.execute(
                    "SELECT quantity, cost_quote_minor, cost_funding_minor FROM paper_equity_holdings WHERE user_id = ? AND instrument_id = ?",
                    (user_id, receipt.instrument.instrument_id),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO paper_equity_holdings (
                          user_id, instrument_id, symbol, name, exchange,
                          quote_currency, quantity, cost_quote_minor,
                          cost_funding_minor, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            user_id, receipt.instrument.instrument_id, receipt.instrument.symbol,
                            receipt.instrument.name, receipt.instrument.exchange,
                            receipt.instrument.quote_currency, receipt.quantity,
                            decision.fill_price_minor * receipt.quantity,
                            decision.funding_debit_minor, _utc(now),
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE paper_equity_holdings
                        SET quantity = quantity + ?,
                            cost_quote_minor = cost_quote_minor + ?,
                            cost_funding_minor = cost_funding_minor + ?,
                            updated_at = ?
                        WHERE user_id = ? AND instrument_id = ?
                        """,
                        (
                            receipt.quantity, decision.fill_price_minor * receipt.quantity,
                            decision.funding_debit_minor, _utc(now), user_id,
                            receipt.instrument.instrument_id,
                        ),
                    )
                result = {
                    "status": "FILLED",
                    "reason_code": decision.reason_code,
                    "trade": {
                        "trade_id": trade_id,
                        "symbol": receipt.instrument.symbol,
                        "quantity": receipt.quantity,
                        "fill_price_minor": decision.fill_price_minor,
                        "quote_currency": receipt.instrument.quote_currency,
                        "funding_debit_minor": decision.funding_debit_minor,
                        "fee_funding_minor": decision.fee_funding_minor,
                        "funding_currency": decision.funding_currency,
                        "executed_at": _utc(now),
                    },
                }

        result["execution_evidence"] = {
            "demo_override": bool(getattr(execution, "demo_override", False)),
            "actual_market_state": (
                getattr(execution, "actual_market_state", None) or execution.market_state
            ),
            "effective_market_state": execution.market_state,
            "quote_captured_at": execution.captured_at.isoformat(),
            "quote_source": execution.source,
        }

        connection.execute(
            """
            UPDATE paper_equity_orders
            SET status = ?, executed_at = CASE WHEN ? IN ('FILLED', 'REJECTED', 'EXPIRED') THEN ? ELSE executed_at END,
                reason_code = ?, result_json = ?
            WHERE order_id = ?
            """,
            (status, status, _utc(now), result.get("reason_code"), json.dumps(result, sort_keys=True), order_id),
        )
        return result

    def _settle_sale(
        self,
        connection: sqlite3.Connection,
        receipt: PaperEquityReceipt,
        decision: Any,
        *,
        user_id: str,
        now: datetime,
    ) -> tuple[str, dict[str, Any]]:
        """Shares out of the holding, proceeds into the funding account, in
        the caller's transaction."""

        holding = connection.execute(
            "SELECT quantity, cost_quote_minor, cost_funding_minor FROM paper_equity_holdings WHERE user_id = ? AND instrument_id = ?",
            (user_id, receipt.instrument.instrument_id),
        ).fetchone()
        held = int(holding["quantity"]) if holding else 0
        if held < receipt.quantity:
            return "REJECTED", {"status": "REJECTED", "reason_code": "PAPER_EQUITY_NOT_ENOUGH_SHARES"}
        self._account_balance(connection, receipt.funding_account_id, receipt.funding_currency)
        credit = int(decision.funding_credit_minor)
        trade_id = f"paper_trade_{uuid.uuid4().hex}"
        connection.execute(
            """
            INSERT INTO paper_equity_trades (
              trade_id, order_id, user_id, symbol, side, quantity,
              fill_price_minor, quote_currency, funding_debit_minor,
              funding_currency, fee_funding_minor, executed_at
            ) VALUES (?, ?, ?, ?, 'SELL', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id, receipt.order_id, user_id, receipt.instrument.symbol,
                receipt.quantity, decision.fill_price_minor,
                receipt.instrument.quote_currency, credit,
                receipt.funding_currency, decision.fee_funding_minor, _utc(now),
            ),
        )
        connection.execute(
            "UPDATE accounts SET balance_minor = balance_minor + ? WHERE account_id = ?",
            (credit, receipt.funding_account_id),
        )
        connection.execute(
            """
            INSERT INTO paper_investment_cash_entries (
              entry_id, trade_id, user_id, account_ref, delta_minor,
              currency, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"paper_cash_{uuid.uuid4().hex}", trade_id, user_id,
                receipt.funding_account_id, credit, receipt.funding_currency, _utc(now),
            ),
        )
        # The cost of what remains is the same share of the original cost.
        remaining = held - receipt.quantity
        cost_quote = int(holding["cost_quote_minor"]) * remaining // held
        cost_funding = int(holding["cost_funding_minor"]) * remaining // held
        connection.execute(
            """
            UPDATE paper_equity_holdings
            SET quantity = ?, cost_quote_minor = ?, cost_funding_minor = ?, updated_at = ?
            WHERE user_id = ? AND instrument_id = ?
            """,
            (remaining, cost_quote, cost_funding, _utc(now), user_id, receipt.instrument.instrument_id),
        )
        return "FILLED", {
            "status": "FILLED",
            "reason_code": decision.reason_code,
            "trade": {
                "trade_id": trade_id,
                "side": "SELL",
                "symbol": receipt.instrument.symbol,
                "quantity": receipt.quantity,
                "fill_price_minor": decision.fill_price_minor,
                "quote_currency": receipt.instrument.quote_currency,
                "funding_credit_minor": credit,
                "fee_funding_minor": decision.fee_funding_minor,
                "funding_currency": decision.funding_currency,
                "executed_at": _utc(now),
            },
        }

    def apply_execution(
        self,
        order_id: str,
        digest: str,
        execution: Any,
        *,
        user_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = self._apply_execution_on_connection(
                    connection, order_id, digest, execution, user_id=user_id, now=current
                )
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def portfolio(self, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM paper_equity_holdings WHERE user_id = ? AND quantity > 0 ORDER BY symbol",
                (user_id,),
            ).fetchall()
            pending = connection.execute(
                """
                SELECT order_id, order_digest, receipt_json, status, reason_code, created_at
                FROM paper_equity_orders
                WHERE user_id = ? AND status IN ('AUTHORIZED', 'QUEUED', 'OPEN')
                ORDER BY created_at
                """,
                (user_id,),
            ).fetchall()
        orders = []
        for row in pending:
            receipt = json.loads(row["receipt_json"])
            orders.append({
                "order_id": row["order_id"],
                "digest": row["order_digest"],
                "status": row["status"],
                "reason_code": row["reason_code"],
                "side": receipt.get("side", "BUY"),
                "quantity": receipt["quantity"],
                "instrument": receipt["instrument"],
                "order_type": receipt["order_type"],
                "limit_price_minor": receipt.get("limit_price_minor"),
                "time_in_force": receipt["time_in_force"],
                "order_expires_at": receipt["order_expires_at"],
                "created_at": row["created_at"],
            })
        return {"holdings": [dict(row) for row in rows], "pending_orders": orders}

    def pending_order_ids(self, user_id: str, session_scope: str | None = None) -> list[tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT order_id, order_digest, session_scope FROM paper_equity_orders "
                "WHERE user_id = ? AND status IN ('AUTHORIZED', 'QUEUED', 'OPEN') ORDER BY created_at",
                (user_id,),
            ).fetchall()
        return [
            (row["order_id"], row["order_digest"])
            for row in rows
            if session_scope is None or row["session_scope"] == session_scope
        ]

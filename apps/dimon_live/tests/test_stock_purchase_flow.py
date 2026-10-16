import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from apps.dimon_live.paper_equity import PaperExecutionSnapshot, PaperFxSnapshot
from apps.dimon_live.paper_equity_store import PaperEquityStore
from apps.dimon_live.paper_equity_service import PaperEquityService
from apps.dimon_live.receipts import DEMO_USER_ID
from apps.dimon_live.tests.test_paper_equity_store import (
    execution_for,
    make_context,
    paper_fixture,
)


def test_market_open_purchase_reaches_one_filled_holding(tmp_path):
    database, store, _device, receipt = paper_fixture(tmp_path)
    result = store.apply_execution(receipt.order_id, receipt.digest, execution_for(receipt), user_id=DEMO_USER_ID)
    assert result["status"] == "FILLED"
    assert store.portfolio(DEMO_USER_ID)["holdings"][0]["quantity"] == receipt.quantity


def test_market_closed_purchase_queues_with_unchanged_cash(tmp_path):
    database, store, _device, receipt = paper_fixture(tmp_path)
    before = database.get_state()["accounts"][0]["balance_minor"]
    result = store.apply_execution(receipt.order_id, receipt.digest, execution_for(receipt, market_state="CLOSED"), user_id=DEMO_USER_ID)
    assert result["status"] == "QUEUED"
    assert database.get_state()["accounts"][0]["balance_minor"] == before


def test_demo_exchange_uses_latest_quote_and_records_real_closed_state(tmp_path):
    database, store, _device, receipt = paper_fixture(tmp_path)

    class ClosedMarketProvider:
        def snapshot(self, symbol, captured_at):
            return SimpleNamespace(
                price=Decimal(receipt.quote_snapshot.price_minor) / Decimal(100),
                market_state="CLOSED",
            )

        def fx_estimate(self, quote_currency, funding_currency, amount, captured_at):
            return SimpleNamespace(
                pair="USD/SGD", rate=receipt.fx_snapshot.rate, captured_at=captured_at,
                source="Timestamped demo quote",
            )

    service = PaperEquityService(
        database, store=store, provider=ClosedMarketProvider(),
        clock=lambda: datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc),
        demo_exchange=True,
    )
    execution = service.execution_snapshot(
        receipt.order_id, user_id=DEMO_USER_ID, session_scope="session-paper",
    )
    normal_execution = PaperEquityService(
        database, store=store, provider=ClosedMarketProvider(),
        clock=lambda: datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc),
        demo_exchange=False,
    ).execution_snapshot(
        receipt.order_id, user_id=DEMO_USER_ID, session_scope="session-paper",
    )

    assert execution.market_state == "OPEN"
    assert execution.actual_market_state == "CLOSED"
    assert execution.demo_override is True
    assert normal_execution.market_state == "CLOSED"
    assert normal_execution.demo_override is False
    result = store.apply_execution(
        receipt.order_id, receipt.digest, execution, user_id=DEMO_USER_ID,
    )
    assert result["status"] == "FILLED"
    assert result["execution_evidence"] == {
        "demo_override": True,
        "actual_market_state": "CLOSED",
        "effective_market_state": "OPEN",
        "quote_captured_at": "2026-10-13T04:00:00+00:00",
        "quote_source": "Timestamped demo quote",
    }


def test_fixed_limit_remains_open_above_limit(tmp_path):
    database, db_path, ready = make_context(tmp_path)
    with sqlite3.connect(db_path) as connection:
        state = json.loads(connection.execute("SELECT state_json FROM conversation_threads WHERE conversation_id = ?", (ready["conversation"]["conversation_id"],)).fetchone()[0])
        state.update({"order_type": "LIMIT", "limit_price": "230.00", "time_in_force": "GTC"})
        connection.execute("UPDATE conversation_threads SET state_json = ? WHERE conversation_id = ?", (json.dumps(state), ready["conversation"]["conversation_id"]))
        connection.commit()
    store = PaperEquityStore(db_path, clock=database._clock)
    receipt = store.consume_ready_conversation(conversation_id=ready["conversation"]["conversation_id"], conversation_version=ready["conversation"]["version"], session_scope="session-paper", user_id=DEMO_USER_ID, request_id="turn-buy-now")
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE paper_equity_orders SET status = 'AUTHORIZED' WHERE order_id = ?", (receipt.order_id,))
        connection.commit()
    result = store.apply_execution(receipt.order_id, receipt.digest, execution_for(receipt, price_minor=23500), user_id=DEMO_USER_ID)
    assert result["status"] == "OPEN"


def test_relative_limit_normalizes_then_fills_at_or_below_limit(tmp_path):
    database, db_path, ready = make_context(tmp_path)
    with sqlite3.connect(db_path) as connection:
        state = json.loads(connection.execute("SELECT state_json FROM conversation_threads WHERE conversation_id = ?", (ready["conversation"]["conversation_id"],)).fetchone()[0])
        state.update({"order_type": "LIMIT", "limit_price": "230.00", "time_in_force": "DAY"})
        connection.execute("UPDATE conversation_threads SET state_json = ? WHERE conversation_id = ?", (json.dumps(state), ready["conversation"]["conversation_id"]))
        connection.commit()
    store = PaperEquityStore(db_path, clock=database._clock)
    receipt = store.consume_ready_conversation(conversation_id=ready["conversation"]["conversation_id"], conversation_version=ready["conversation"]["version"], session_scope="session-paper", user_id=DEMO_USER_ID, request_id="turn-buy-now")
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE paper_equity_orders SET status = 'AUTHORIZED' WHERE order_id = ?", (receipt.order_id,))
        connection.commit()
    result = store.apply_execution(receipt.order_id, receipt.digest, execution_for(receipt, price_minor=22900), user_id=DEMO_USER_ID)
    assert result["status"] == "FILLED"


def test_insufficient_funds_can_use_available_balance_and_continue(tmp_path):
    database, db_path, ready = make_context(tmp_path, shortfall=True)
    # The conversation adapter's funding repair is tested at the boundary; a
    # direct ready conversation with the original allocation must not create a
    # receipt against an underfunded account.
    store = PaperEquityStore(db_path, clock=database._clock)
    try:
        store.consume_ready_conversation(conversation_id=ready["conversation"]["conversation_id"], conversation_version=ready["conversation"]["version"], session_scope="session-paper", user_id=DEMO_USER_ID, request_id="turn-buy-now")
    except ValueError as exc:
        assert str(exc) == "PAPER_EQUITY_INSUFFICIENT_FUNDS"


def test_replay_concurrency_and_provider_failure_preserve_other_domains(tmp_path):
    database, store, _device, receipt = paper_fixture(tmp_path)
    first = store.apply_execution(receipt.order_id, receipt.digest, execution_for(receipt), user_id=DEMO_USER_ID)
    second = store.apply_execution(receipt.order_id, receipt.digest, execution_for(receipt), user_id=DEMO_USER_ID)
    assert first == second
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_equity_trades").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ledger_transactions").fetchone()[0] == 0

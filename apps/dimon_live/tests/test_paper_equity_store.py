import json
import sqlite3
from dataclasses import replace
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from apps.dimon_live.conversation import ConversationService, ConversationInput, InputKind, build_adapter_registry
from apps.dimon_live.conversation_store import ConversationStore
from apps.dimon_live.database import Database
from apps.dimon_live.investment_conversation import InvestmentConversationAdapter
from apps.dimon_live.paper_equity_store import PaperEquityStore
from apps.dimon_live.paper_equity import (
    PaperEquityDraft,
    PaperExecutionSnapshot,
    PaperFillDecisionStatus,
    PaperFxSnapshot,
    PaperOrderType,
    PaperTimeInForce,
    build_receipt,
)
from apps.dimon_live.receipts import DEMO_USER_ID
from apps.dimon_live.tests.test_investment_conversation import _brief
from apps.dimon_live.tests.device_signer import SoftwareAuthenticator


BALANCED = {"horizon": 3, "drop": 3, "experience": 2, "goal": 3}


def make_context(tmp_path, *, shortfall=False, brief=None):
    db_path = Path(tmp_path) / "paper.sqlite3"
    database = Database(db_path)
    database.initialize()
    if not shortfall:
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "UPDATE accounts SET balance_minor = 5_000_000 WHERE account_id = 'acct_savings_01'"
            )
            connection.commit()
    database.save_investor_profile(BALANCED, True, user_id=DEMO_USER_ID)
    clock = database._clock
    conversation_store = ConversationStore(db_path, clock=clock)
    conversation_service = ConversationService(
        conversation_store,
        build_adapter_registry((InvestmentConversationAdapter(),)),
        clock,
    )
    started = conversation_service.start(
        domain="INVESTMENT",
        session_scope="session-paper",
        user_id=DEMO_USER_ID,
        payload={"brief": brief or _brief(shortfall=Decimal("0"))},
    )
    ready = conversation_service.apply_turn(
        started["conversation"]["conversation_id"],
        session_scope="session-paper",
        user_id=DEMO_USER_ID,
        request_id="turn-buy-now",
        expected_version=1,
        source="typed",
        conversation_input=ConversationInput(InputKind.CHOICE, choice="BUY_NOW"),
    )
    return database, db_path, ready


def test_initialize_adds_isolated_paper_equity_tables(tmp_path):
    database = Database(Path(tmp_path) / "paper.sqlite3")
    database.initialize()

    with sqlite3.connect(database.path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {
        "paper_equity_orders",
        "paper_equity_trades",
        "paper_equity_holdings",
        "paper_investment_cash_entries",
    } <= names


def test_ready_conversation_is_consumed_with_exactly_one_receipt(tmp_path):
    database, db_path, ready = make_context(tmp_path)
    store = PaperEquityStore(db_path, clock=database._clock)

    receipt = store.consume_ready_conversation(
        conversation_id=ready["conversation"]["conversation_id"],
        conversation_version=ready["conversation"]["version"],
        session_scope="session-paper",
        user_id=DEMO_USER_ID,
        request_id="turn-buy-now",
    )

    assert receipt.instrument.symbol == "NVDA"
    assert receipt.order_type.value == "MARKET"
    assert store.get_order(receipt.order_id)["status"] == "AWAITING_APPROVAL"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status FROM conversation_threads WHERE conversation_id = ?",
            (receipt.conversation_id,),
        ).fetchone()[0] == "CONSUMED"


def test_quantity_conversation_does_not_turn_estimate_into_spending_cap(tmp_path):
    quantity_brief = replace(
        _brief(shortfall=Decimal("0")),
        requested_allocation=Decimal("233.95"),
        requested_quantity=1,
        whole_shares=1,
        estimated_spend=Decimal("233.95"),
        unallocated_cash=Decimal("0"),
        funding_required=Decimal("299.46"),
        funding_shortfall=Decimal("0"),
    )
    database, db_path, ready = make_context(tmp_path, brief=quantity_brief)
    receipt = PaperEquityStore(db_path, clock=database._clock).consume_ready_conversation(
        conversation_id=ready["conversation"]["conversation_id"],
        conversation_version=ready["conversation"]["version"],
        session_scope="session-paper",
        user_id=DEMO_USER_ID,
        request_id="turn-buy-now",
    )

    assert receipt.quantity == 1
    assert receipt.max_quote_price_minor > receipt.quote_snapshot.price_minor


def test_same_conversation_version_replays_the_same_receipt(tmp_path):
    database, db_path, ready = make_context(tmp_path)
    store = PaperEquityStore(db_path, clock=database._clock)
    arguments = {
        "conversation_id": ready["conversation"]["conversation_id"],
        "conversation_version": ready["conversation"]["version"],
        "session_scope": "session-paper",
        "user_id": DEMO_USER_ID,
        "request_id": "turn-buy-now",
    }

    first = store.consume_ready_conversation(**arguments)
    second = store.consume_ready_conversation(**arguments)

    assert second.order_id == first.order_id
    assert second.digest == first.digest
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_equity_orders").fetchone()[0] == 1


def test_conflicting_session_user_or_version_is_rejected(tmp_path):
    database, db_path, ready = make_context(tmp_path)
    store = PaperEquityStore(db_path, clock=database._clock)
    base = {
        "conversation_id": ready["conversation"]["conversation_id"],
        "conversation_version": ready["conversation"]["version"],
        "user_id": DEMO_USER_ID,
        "request_id": "turn-buy-now",
    }

    with pytest.raises(ValueError, match="CONVERSATION_NOT_FOUND"):
        store.consume_ready_conversation(**base, session_scope="other-session")
    with pytest.raises(ValueError, match="CONVERSATION_VERSION_CONFLICT"):
        wrong_version = {**base, "conversation_version": 99}
        store.consume_ready_conversation(**wrong_version, session_scope="session-paper")


def test_insufficient_current_balance_creates_no_receipt(tmp_path):
    database, db_path, ready = make_context(tmp_path, shortfall=True)
    store = PaperEquityStore(db_path, clock=database._clock)

    with pytest.raises(ValueError, match="PAPER_EQUITY_INSUFFICIENT_FUNDS"):
        store.consume_ready_conversation(
            conversation_id=ready["conversation"]["conversation_id"],
            conversation_version=ready["conversation"]["version"],
            session_scope="session-paper",
            user_id=DEMO_USER_ID,
            request_id="turn-buy-now",
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_equity_orders").fetchone()[0] == 0


def test_direct_stock_purchase_does_not_require_investor_profile(tmp_path):
    database, db_path, ready = make_context(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM investor_profiles WHERE user_id = ?", (DEMO_USER_ID,))
        connection.commit()
    store = PaperEquityStore(db_path, clock=database._clock)

    receipt = store.consume_ready_conversation(
        conversation_id=ready["conversation"]["conversation_id"],
        conversation_version=ready["conversation"]["version"],
        session_scope="session-paper",
        user_id=DEMO_USER_ID,
        request_id="turn-buy-now",
    )
    assert receipt.instrument.symbol == "NVDA"
    assert store.get_order(receipt.order_id)["status"] == "AWAITING_APPROVAL"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_equity_orders").fetchone()[0] == 1


def test_receipt_and_conversation_consumption_roll_back_together(tmp_path):
    database, db_path, ready = make_context(tmp_path)
    store = PaperEquityStore(db_path, clock=database._clock)
    store.fail_after_receipt_insert = True

    with pytest.raises(RuntimeError, match="PAPER_EQUITY_TEST_ROLLBACK"):
        store.consume_ready_conversation(
            conversation_id=ready["conversation"]["conversation_id"],
            conversation_version=ready["conversation"]["version"],
            session_scope="session-paper",
            user_id=DEMO_USER_ID,
            request_id="turn-buy-now",
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_equity_orders").fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM conversation_threads WHERE conversation_id = ?",
            (ready["conversation"]["conversation_id"],),
        ).fetchone()[0] == "READY_FOR_DRAFT"


def test_legacy_investment_tables_are_not_written(tmp_path):
    database, db_path, ready = make_context(tmp_path)
    store = PaperEquityStore(db_path, clock=database._clock)
    store.consume_ready_conversation(
        conversation_id=ready["conversation"]["conversation_id"],
        conversation_version=ready["conversation"]["version"],
        session_scope="session-paper",
        user_id=DEMO_USER_ID,
        request_id="turn-buy-now",
    )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM investment_orders").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def make_authorizable_order(tmp_path):
    database, db_path, ready = make_context(tmp_path)
    device = SoftwareAuthenticator()
    device.enroll(database)
    store = PaperEquityStore(db_path, clock=database._clock)
    receipt = store.consume_ready_conversation(
        conversation_id=ready["conversation"]["conversation_id"],
        conversation_version=ready["conversation"]["version"],
        session_scope="session-paper",
        user_id=DEMO_USER_ID,
        request_id="turn-buy-now",
    )
    return database, store, device, receipt


def authorize_for_store(store, receipt):
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE paper_equity_orders SET status = 'AUTHORIZED' WHERE order_id = ?",
            (receipt.order_id,),
        )
        connection.commit()


def execution_for(receipt, *, price_minor=None, market_state="OPEN", fx_rate=None):
    return PaperExecutionSnapshot(
        price_minor=price_minor or receipt.quote_snapshot.price_minor,
        market_state=market_state,
        captured_at=receipt.quote_snapshot.captured_at + timedelta(seconds=30),
        source="YAHOO_FINANCE",
        fx_snapshot=PaperFxSnapshot(
            pair=receipt.fx_snapshot.pair,
            rate=fx_rate or receipt.fx_snapshot.rate,
            captured_at=receipt.fx_snapshot.captured_at + timedelta(seconds=30),
            source="YAHOO_FINANCE",
        ),
    )


def paper_fixture(tmp_path):
    database, store, device, receipt = make_authorizable_order(tmp_path)
    authorize_for_store(store, receipt)
    return database, store, device, receipt


def test_open_market_order_fills_within_both_signed_caps(tmp_path):
    database, store, device, receipt = paper_fixture(tmp_path)

    result = store.apply_execution(
        receipt.order_id,
        receipt.digest,
        execution_for(receipt),
        user_id=DEMO_USER_ID,
    )

    assert result["status"] == "FILLED"
    assert result["trade"]["quantity"] == receipt.quantity
    assert store.get_order(receipt.order_id)["status"] == "FILLED"


@pytest.mark.parametrize("market_state", ["CLOSED", "PRE_MARKET", "POST_MARKET"])
def test_closed_pre_and_post_market_orders_queue_without_cash_mutation(tmp_path, market_state):
    database, store, device, receipt = paper_fixture(tmp_path)
    before = database.get_state()["accounts"][0]["balance_minor"]

    result = store.apply_execution(
        receipt.order_id,
        receipt.digest,
        execution_for(receipt, market_state=market_state),
        user_id=DEMO_USER_ID,
    )

    assert result["status"] == "QUEUED"
    assert database.get_state()["accounts"][0]["balance_minor"] == before
    assert store.get_order(receipt.order_id)["status"] == "QUEUED"


def test_unknown_or_stale_market_state_rejects_without_mutation(tmp_path):
    database, store, device, receipt = paper_fixture(tmp_path)

    result = store.apply_execution(
        receipt.order_id,
        receipt.digest,
        execution_for(receipt, market_state="UNKNOWN"),
        user_id=DEMO_USER_ID,
    )

    assert result["status"] == "REJECTED"
    assert result["reason_code"] == "MARKET_STATE_UNAVAILABLE"
    assert store.get_order(receipt.order_id)["status"] == "REJECTED"


def test_limit_order_fills_at_or_below_limit_and_otherwise_opens(tmp_path):
    database, db_path, ready = make_context(tmp_path)
    with sqlite3.connect(db_path) as connection:
        state = json.loads(
            connection.execute(
                "SELECT state_json FROM conversation_threads WHERE conversation_id = ?",
                (ready["conversation"]["conversation_id"],),
            ).fetchone()[0]
        )
    state.update({"order_type": "LIMIT", "limit_price": "230.00", "time_in_force": "GTC"})
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE conversation_threads SET state_json = ? WHERE conversation_id = ?",
            (json.dumps(state), ready["conversation"]["conversation_id"]),
        )
        connection.commit()
    store = PaperEquityStore(db_path, clock=database._clock)
    receipt = store.consume_ready_conversation(
        conversation_id=ready["conversation"]["conversation_id"],
        conversation_version=ready["conversation"]["version"],
        session_scope="session-paper",
        user_id=DEMO_USER_ID,
        request_id="turn-buy-now",
    )
    authorize_for_store(store, receipt)

    opened = store.apply_execution(
        receipt.order_id,
        receipt.digest,
        execution_for(receipt, price_minor=23_500),
        user_id=DEMO_USER_ID,
    )
    assert opened["status"] == "OPEN"

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE paper_equity_orders SET status = 'AUTHORIZED' WHERE order_id = ?",
            (receipt.order_id,),
        )
        connection.commit()
    filled = store.apply_execution(
        receipt.order_id,
        receipt.digest,
        execution_for(receipt, price_minor=22_900),
        user_id=DEMO_USER_ID,
    )
    assert filled["status"] == "FILLED"


def test_price_or_fx_beyond_signed_cap_requires_requote(tmp_path):
    database, store, device, receipt = paper_fixture(tmp_path)

    price = store.apply_execution(
        receipt.order_id,
        receipt.digest,
        execution_for(receipt, price_minor=receipt.max_quote_price_minor + 1),
        user_id=DEMO_USER_ID,
    )
    assert price["status"] == "REQUOTE_REQUIRED"


def test_day_and_gtc_expiry_rules_are_deterministic(tmp_path):
    database, store, device, receipt = paper_fixture(tmp_path)
    assert receipt.approval_expires_at > receipt.quote_snapshot.captured_at
    assert receipt.order_expires_at > receipt.approval_expires_at


def test_face_and_device_signature_are_bound_to_the_same_digest(tmp_path):
    database, store, device, receipt = make_authorizable_order(tmp_path)
    request = database.request_paper_equity_singpass_authorisation(receipt.order_id, receipt.digest)
    database.singpass_decide(request["auth_id"], True)
    challenge = database.issue_paper_equity_challenge(receipt.order_id, receipt.digest, rp_id=device.rp_id)
    assertion = device.sign(challenge)
    result = database.approve_paper_equity_order(
        receipt.order_id,
        receipt.digest,
        assertion=assertion,
        context=device.context,
        execution=execution_for(receipt),
    )
    assert result["status"] == "FILLED"


def test_singpass_is_consumed_once_when_authorized_state_commits(tmp_path):
    database, store, device, receipt = make_authorizable_order(tmp_path)
    request = database.request_paper_equity_singpass_authorisation(receipt.order_id, receipt.digest)
    database.singpass_decide(request["auth_id"], True)
    challenge = database.issue_paper_equity_challenge(receipt.order_id, receipt.digest, rp_id=device.rp_id)
    result = database.approve_paper_equity_order(
        receipt.order_id,
        receipt.digest,
        assertion=device.sign(challenge),
        context=device.context,
        execution=execution_for(receipt, market_state="CLOSED"),
    )
    assert result["status"] == "QUEUED"
    assert database.singpass_pending() == []


def test_fill_posts_balanced_cash_entries_and_updates_whole_share_holding(tmp_path):
    database, store, device, receipt = paper_fixture(tmp_path)
    before = database.get_state()["accounts"][0]["balance_minor"]
    result = store.apply_execution(
        receipt.order_id,
        receipt.digest,
        execution_for(receipt),
        user_id=DEMO_USER_ID,
    )

    with sqlite3.connect(store.path) as connection:
        debit = connection.execute(
            "SELECT SUM(delta_minor) FROM paper_investment_cash_entries"
        ).fetchone()[0]
        holding = connection.execute(
            "SELECT quantity FROM paper_equity_holdings WHERE user_id = ? AND symbol = ?",
            (DEMO_USER_ID, "NVDA"),
        ).fetchone()[0]
    assert debit == -result["trade"]["funding_debit_minor"]
    assert holding == receipt.quantity
    assert database.get_state()["accounts"][0]["balance_minor"] == before - result["trade"]["funding_debit_minor"]


def test_execution_replay_returns_one_trade_and_one_debit(tmp_path):
    database, store, device, receipt = paper_fixture(tmp_path)
    execution = execution_for(receipt)
    first = store.apply_execution(receipt.order_id, receipt.digest, execution, user_id=DEMO_USER_ID)
    second = store.apply_execution(receipt.order_id, receipt.digest, execution, user_id=DEMO_USER_ID)

    assert second == first
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_equity_trades").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM paper_investment_cash_entries").fetchone()[0] == 1


def test_concurrent_execution_creates_exactly_one_trade(tmp_path):
    database, store, device, receipt = paper_fixture(tmp_path)
    execution = execution_for(receipt)

    def run():
        return store.apply_execution(receipt.order_id, receipt.digest, execution, user_id=DEMO_USER_ID)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert results[0] == results[1]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_equity_trades").fetchone()[0] == 1


def test_failure_rolls_back_trade_cash_holding_and_singpass_consumption(tmp_path):
    database, store, device, receipt = make_authorizable_order(tmp_path)
    authorize_for_store(store, receipt)
    store.fail_after_trade_insert = True

    with pytest.raises(RuntimeError, match="PAPER_EQUITY_TEST_EXECUTION_ROLLBACK"):
        store.apply_execution(
            receipt.order_id,
            receipt.digest,
            execution_for(receipt),
            user_id=DEMO_USER_ID,
        )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_equity_trades").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM paper_investment_cash_entries").fetchone()[0] == 0


def test_voice_or_text_confirmation_cannot_authorize_a_paper_order(tmp_path):
    database, store, device, receipt = make_authorizable_order(tmp_path)

    assert store.get_order(receipt.order_id)["status"] == "AWAITING_APPROVAL"
    assert database.singpass_pending() == []


def test_paper_order_requires_mock_singpass_before_device_challenge(tmp_path):
    database, store, device, receipt = make_authorizable_order(tmp_path)

    with pytest.raises(Exception, match="SINGPASS_AUTHORISATION_REQUIRED"):
        database.issue_paper_equity_challenge(
            receipt.order_id,
            receipt.digest,
            rp_id=device.rp_id,
        )


def test_singpass_approval_is_bound_to_paper_order_digest(tmp_path):
    database, store, device, receipt = make_authorizable_order(tmp_path)

    request = database.request_paper_equity_singpass_authorisation(
        receipt.order_id, receipt.digest
    )
    database.singpass_decide(request["auth_id"], True)
    challenge = database.issue_paper_equity_challenge(
        receipt.order_id,
        receipt.digest,
        rp_id=device.rp_id,
    )
    assert challenge["receipt_id"] == receipt.order_id
    assert challenge["receipt_digest"] == receipt.digest
    with pytest.raises(Exception, match="PAPER_EQUITY_DIGEST_MISMATCH"):
        database.issue_paper_equity_challenge(
            receipt.order_id,
            "0" * 64,
            rp_id=device.rp_id,
        )


def test_changed_or_expired_receipt_invalidates_every_approval(tmp_path):
    database, store, device, receipt = make_authorizable_order(tmp_path)

    request = database.request_paper_equity_singpass_authorisation(
        receipt.order_id, receipt.digest
    )
    database.singpass_decide(request["auth_id"], True)
    store.supersede_for_revision(receipt.order_id, receipt.digest, user_id=DEMO_USER_ID)

    with pytest.raises(Exception, match="PAPER_EQUITY_ORDER_TERMINAL"):
        database.issue_paper_equity_challenge(
            receipt.order_id,
            receipt.digest,
            rp_id=device.rp_id,
        )

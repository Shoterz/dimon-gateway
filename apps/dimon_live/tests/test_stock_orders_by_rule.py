"""Buying and selling shares by plain words, without the language model."""

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from apps.dimon_live import stock_request
from apps.dimon_live.conversation import ConversationInput, ConversationService, InputKind, build_adapter_registry
from apps.dimon_live.conversation_store import ConversationStore
from apps.dimon_live.investment_conversation import InvestmentConversationAdapter, sell_state
from apps.dimon_live.paper_equity_store import PaperEquityStore, migrate
from apps.dimon_live.receipts import DEMO_USER_ID
from apps.dimon_live.tests.test_investment_conversation import _brief
from apps.dimon_live.tests.test_paper_equity_store import (
    authorize_for_store,
    execution_for,
    make_context,
    paper_fixture,
)


# ------------------------------------------------------------ requests
@pytest.mark.parametrize("text, action, label, quantity, allocation, currency", [
    ("buy S$500 of Tesla", "BUY", "Tesla", None, Decimal("500"), "SGD"),
    ("buy Tesla", "BUY", "Tesla", None, None, None),
    ("buy 5 Apple shares", "BUY", "Apple", 5, None, None),
    ("get me 3 shares of nvidia", "BUY", "nvidia", 3, None, None),
    ("I want 10 shares of Microsoft", "BUY", "Microsoft", 10, None, None),
    ("buy two hundred dollars worth of apple", "BUY", "apple", None, Decimal("200"), None),
    ("buy 2 nvidia", "BUY", "nvidia", 2, None, None),
    ("buy 1k of apple", "BUY", "apple", None, Decimal("1000"), None),
    ("invest 300 in apple", "BUY", "apple", None, Decimal("300"), None),
    ("buy a share of Google", "BUY", "Google", 1, None, None),
    ("buy twenty shares of coca cola please", "BUY", "coca cola", 20, None, None),
    ("buy US$300 worth of Microsoft stock", "BUY", "Microsoft", None, Decimal("300"), "USD"),
    ("sell 2 apple shares", "SELL", "apple", 2, None, None),
    ("sell my tesla", "SELL", "tesla", None, None, None),
    ("what's apple's price", "RESEARCH", "apple", None, None, None),
    ("how is nvidia doing", "RESEARCH", "nvidia", None, None, None),
])
def test_everyday_stock_requests_are_understood(text, action, label, quantity, allocation, currency):
    request = stock_request.parse(text)
    assert request is not None
    assert (request.action, request.label, request.quantity, request.allocation, request.explicit_currency) == (
        action, label, quantity, allocation, currency)


@pytest.mark.parametrize("text, fraction", [
    ("sell all my apple shares", "ALL"),
    ("sell half of my nvidia", "HALF"),
])
def test_sell_all_or_half(text, fraction):
    assert stock_request.parse(text).fraction == fraction


@pytest.mark.parametrize("text", [
    "send $50 to John",
    "pay my bill",
    "what should I buy",
    "show my portfolio",
    "I want to check my balance",
    "invest $300 a month in apple",   # a monthly plan, not a one-off order
    "what is my net worth",
])
def test_payments_plans_and_questions_are_not_stock_orders(text):
    assert stock_request.parse(text) is None


def test_two_different_sizes_are_not_guessed():
    request = stock_request.parse("buy 5 shares for $500 of apple")
    assert request.quantity is None and request.allocation is None


# ------------------------------------------------------------ plain replies
class ModelMustNotRun:
    def interpret(self, *args, **kwargs):
        raise AssertionError("a plain answer must not need the model")


@pytest.fixture
def conversations(tmp_path):
    from apps.dimon_live.database import Database

    path = Path(tmp_path) / "conversation.sqlite3"
    from apps.dimon_live.tests.test_stock_market import NOW

    database = Database(path, clock=lambda: NOW)
    database.initialize()
    store = ConversationStore(path, clock=database._clock)
    adapter = InvestmentConversationAdapter(followup_interpreter=ModelMustNotRun())
    return ConversationService(store, build_adapter_registry((adapter,)), database._clock)


def _start(service, payload):
    return service.start(domain="INVESTMENT", session_scope="s", user_id="u", payload=payload)


def _say(service, current, text, n=[0]):
    n[0] += 1
    return service.apply_turn(
        current["conversation"]["conversation_id"], session_scope="s", user_id="u",
        request_id=f"turn-{n[0]}", expected_version=current["conversation"]["version"],
        source="voice", conversation_input=ConversationInput(InputKind.TEXT, text=text),
    )


@pytest.mark.parametrize("reply", ["yes", "Yes please.", "go ahead", "buy it now", "ok, at market", "sure"])
def test_a_plain_yes_buys_at_market(conversations, reply):
    started = _start(conversations, {"brief": _brief()})
    ready = _say(conversations, started, reply)
    assert ready["conversation"]["status"] == "READY_FOR_DRAFT"
    assert ready["public_view"]["order_type"] == "MARKET"


def test_a_limit_order_by_plain_words(conversations):
    started = _start(conversations, {"brief": _brief()})
    asked = _say(conversations, started, "set a limit")
    assert asked["question"] == "What maximum price should I use?"
    priced = _say(conversations, asked, "230")
    assert priced["public_view"]["limit_price"] == "230.00"
    ready = _say(conversations, priced, "until cancelled")
    assert ready["conversation"]["status"] == "READY_FOR_DRAFT"
    assert ready["public_view"]["time_in_force"] == "GTC"


def test_a_limit_with_its_price_in_one_breath(conversations):
    started = _start(conversations, {"brief": _brief()})
    priced = _say(conversations, started, "maximum 225 dollars")
    assert priced["public_view"]["limit_price"] == "225.00"
    ready = _say(conversations, priced, "just today")
    assert ready["public_view"]["time_in_force"] == "DAY"


def test_a_limit_far_from_the_price_is_questioned(conversations):
    started = _start(conversations, {"brief": _brief()})
    asked = _say(conversations, started, "set a limit")
    again = _say(conversations, asked, "23")
    assert "far from the current price" in again["answer"]
    assert again["public_view"].get("limit_price") is None


@pytest.mark.parametrize("reply", ["no", "cancel", "never mind", "don't buy it"])
def test_no_cancels(conversations, reply):
    started = _start(conversations, {"brief": _brief()})
    assert _say(conversations, started, reply)["conversation"]["status"] == "CANCELLED"


def test_an_amount_below_one_share_asks_again(conversations):
    from dataclasses import replace

    brief = replace(_brief(), requested_allocation=Decimal("100"), whole_shares=0,
                    estimated_spend=Decimal("0"), unallocated_cash=Decimal("100"))
    started = _start(conversations, {"brief": brief})
    assert started["conversation"]["status"] == "WAITING"
    assert "isn't enough for one whole share" in started["answer"]
    assert started["public_view"]["phase"] == "SIZE"


# ------------------------------------------------------------ selling
def _sell_payload(held=10, size=None):
    brief = _brief()
    state = sell_state(instrument=brief.instrument, market=brief.market, fx_rate=Decimal("1.28"),
                       fx_pair="USD/SGD", fx_source="Yahoo Finance", fx_captured_at=brief.market.captured_at.isoformat(),
                       held_quantity=held)
    return {"sell": state, "size": size}


def test_sell_asks_how_many_and_takes_all(conversations):
    started = _start(conversations, _sell_payload(held=10))
    assert started["question"] == "You have 10 shares of NVIDIA Corporation. How many would you like to sell?"
    assert started["allowed_choices"] == ["SELL_ALL", "CANCEL"]
    sized = _say(conversations, started, "all of them")
    assert sized["public_view"]["whole_shares"] == 10
    assert sized["question"] == "Sell now at market, or set a minimum price?"
    ready = _say(conversations, sized, "sell now")
    assert ready["conversation"]["status"] == "READY_FOR_DRAFT"
    assert ready["public_view"]["side"] == "SELL"


def test_selling_more_than_held_asks_again(conversations):
    started = _start(conversations, _sell_payload(held=3, size={"quantity": 5}))
    assert "you can sell up to 3" in started["question"]
    sized = _say(conversations, started, "2")
    assert sized["public_view"]["whole_shares"] == 2


def _bought(tmp_path):
    database, store, _device, receipt = paper_fixture(tmp_path)
    filled = store.apply_execution(receipt.order_id, receipt.digest, execution_for(receipt), user_id=DEMO_USER_ID)
    assert filled["status"] == "FILLED"
    return database, store, receipt


def _ready_sale(database, quantity, *, limit=None):
    store = ConversationStore(database.path, clock=database._clock)
    service = ConversationService(store, build_adapter_registry((InvestmentConversationAdapter(),)), database._clock)
    held = PaperEquityStore(database.path, clock=database._clock).portfolio(DEMO_USER_ID)["holdings"][0]["quantity"]
    started = service.start(domain="INVESTMENT", session_scope="session-paper", user_id=DEMO_USER_ID,
                            payload=_sell_payload(held=held, size={"quantity": quantity}))
    step = lambda current, text, rid: service.apply_turn(  # noqa: E731
        current["conversation"]["conversation_id"], session_scope="session-paper", user_id=DEMO_USER_ID,
        request_id=rid, expected_version=current["conversation"]["version"], source="typed",
        conversation_input=ConversationInput(InputKind.TEXT, text=text))
    if limit is None:
        return step(started, "yes", "sell-1")
    priced = step(started, f"minimum {limit}", "sell-1")
    return step(priced, "today", "sell-2")


def _consume(store, ready):
    return store.consume_ready_conversation(
        conversation_id=ready["conversation"]["conversation_id"],
        conversation_version=ready["conversation"]["version"],
        session_scope="session-paper", user_id=DEMO_USER_ID, request_id="sell-receipt",
    )


def test_a_sale_settles_shares_out_and_cash_in(tmp_path):
    database, store, bought = _bought(tmp_path)
    before = database.get_state()["accounts"][0]["balance_minor"]
    receipt = _consume(store, _ready_sale(database, 30))
    assert receipt.side == "SELL" and receipt.quantity == 30
    assert receipt.to_dict()["side"] == "SELL"
    authorize_for_store(store, receipt)
    result = store.apply_execution(receipt.order_id, receipt.digest, execution_for(receipt), user_id=DEMO_USER_ID)
    assert result["status"] == "FILLED"
    assert result["trade"]["side"] == "SELL"
    credit = result["trade"]["funding_credit_minor"]
    assert credit > 0
    assert database.get_state()["accounts"][0]["balance_minor"] == before + credit
    holding = store.portfolio(DEMO_USER_ID)["holdings"][0]
    assert holding["quantity"] == bought.quantity - 30
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT delta_minor FROM paper_investment_cash_entries ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()[0] == credit


def test_a_sale_of_shares_no_longer_held_is_refused(tmp_path):
    database, store, bought = _bought(tmp_path)
    receipt = _consume(store, _ready_sale(database, bought.quantity))
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE paper_equity_holdings SET quantity = 1")
        connection.commit()
    authorize_for_store(store, receipt)
    result = store.apply_execution(receipt.order_id, receipt.digest, execution_for(receipt), user_id=DEMO_USER_ID)
    assert (result["status"], result["reason_code"]) == (
        "REJECTED", "PAPER_EQUITY_NOT_ENOUGH_SHARES",
    )


def test_a_closed_market_queues_the_sale_and_a_limit_waits_for_its_price(tmp_path):
    database, store, _bought_receipt = _bought(tmp_path)
    queued = _consume(store, _ready_sale(database, 5))
    authorize_for_store(store, queued)
    assert store.apply_execution(queued.order_id, queued.digest, execution_for(queued, market_state="CLOSED"),
                                 user_id=DEMO_USER_ID)["status"] == "QUEUED"
    assert [o["side"] for o in store.portfolio(DEMO_USER_ID)["pending_orders"]] == ["SELL"]


def test_a_limit_sale_never_fills_below_its_limit(tmp_path):
    database, store, _bought_receipt = _bought(tmp_path)
    receipt = _consume(store, _ready_sale(database, 5, limit="240"))
    authorize_for_store(store, receipt)
    below = store.apply_execution(receipt.order_id, receipt.digest, execution_for(receipt, price_minor=23_900),
                                  user_id=DEMO_USER_ID)
    assert below["status"] == "OPEN"
    at = store.apply_execution(receipt.order_id, receipt.digest, execution_for(receipt, price_minor=24_100),
                               user_id=DEMO_USER_ID)
    assert at["status"] == "FILLED" and at["trade"]["fill_price_minor"] == 24_100


def test_a_market_sale_never_fills_below_its_signed_price_floor(tmp_path):
    database, store, _bought_receipt = _bought(tmp_path)
    receipt = _consume(store, _ready_sale(database, 5))
    authorize_for_store(store, receipt)
    before = database.get_state()["accounts"][0]["balance_minor"]
    held_before = store.portfolio(DEMO_USER_ID)["holdings"][0]["quantity"]

    result = store.apply_execution(
        receipt.order_id,
        receipt.digest,
        execution_for(receipt, price_minor=receipt.max_quote_price_minor - 1),
        user_id=DEMO_USER_ID,
    )

    assert (result["status"], result["reason_code"]) == (
        "REQUOTE_REQUIRED", "PRICE_MOVED_BEYOND_LIMIT",
    )
    assert database.get_state()["accounts"][0]["balance_minor"] == before
    assert store.portfolio(DEMO_USER_ID)["holdings"][0]["quantity"] == held_before


def test_a_sale_never_fills_below_its_signed_funding_credit(tmp_path):
    database, store, _bought_receipt = _bought(tmp_path)
    receipt = _consume(store, _ready_sale(database, 5))
    authorize_for_store(store, receipt)
    before = database.get_state()["accounts"][0]["balance_minor"]
    held_before = store.portfolio(DEMO_USER_ID)["holdings"][0]["quantity"]

    result = store.apply_execution(
        receipt.order_id,
        receipt.digest,
        execution_for(receipt, fx_rate=receipt.fx_snapshot.rate / Decimal("2")),
        user_id=DEMO_USER_ID,
    )

    assert (result["status"], result["reason_code"]) == (
        "REQUOTE_REQUIRED", "FUNDING_MOVED_BEYOND_LIMIT",
    )
    assert database.get_state()["accounts"][0]["balance_minor"] == before
    assert store.portfolio(DEMO_USER_ID)["holdings"][0]["quantity"] == held_before


def test_older_ledgers_are_upgraded_to_allow_sales(tmp_path):
    path = Path(tmp_path) / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE paper_equity_trades (trade_id TEXT PRIMARY KEY, side TEXT NOT NULL CHECK(side = 'BUY'))")
        connection.execute("INSERT INTO paper_equity_trades VALUES ('t1', 'BUY')")
    connection = sqlite3.connect(path, isolation_level=None)
    migrate(connection)
    connection.execute("INSERT INTO paper_equity_trades VALUES ('t2', 'SELL')")
    assert connection.execute("SELECT COUNT(*) FROM paper_equity_trades").fetchone()[0] == 2
    connection.close()


# ------------------------------------------------------------ several requests
class ModelBusy:
    """The rate-limited remote model: every call fails."""

    def extract(self, text):
        from apps.dimon_live.model_adapter import ModelUnavailable

        raise ModelUnavailable("rate limited")


def _service(tmp_path, *, multi_action=None):
    from apps.dimon_live.database import Database
    from apps.dimon_live.service import ActionService
    from apps.dimon_live.tests.test_stock_market import NOW, NvidiaMarketProvider

    database = Database(Path(tmp_path) / "ledger.sqlite3", clock=lambda: NOW)
    database.initialize()
    return ActionService(database, ModelBusy(), stock_extractor=ModelBusy(),
                         multi_action_extractor=multi_action, stock_provider=NvidiaMarketProvider())


@pytest.mark.parametrize("text", [
    "send $20 to John Tan and buy 2 shares of NVIDIA",
    "buy 2 shares of NVIDIA and send $20 to John Tan",
])
def test_a_transfer_and_a_stock_are_split_without_the_model(tmp_path, text):
    result = _service(tmp_path, multi_action=ModelBusy()).process("00000000-0000-4000-8000-000000000001", text)
    assert result["outcome"] == "MULTI_REQUEST"
    by_text = {step["text"]: step["result"] for step in result["steps"]}
    transfer = by_text["send $20 to John Tan"]
    assert transfer["receipt_choices"][0]["amount_minor"] == 2000
    # Under S$200 the transfer is approved by voice ("Yes Confirm").
    policy = transfer["receipt_choices"][0]["policy"]
    assert policy["requires_voice_confirmation"] is True and policy["requires_face"] is False
    stock = by_text["buy 2 shares of NVIDIA"]
    assert stock["reason_code"] == "REAL_STOCK_BRIEF"


def test_a_bill_with_a_transfer_keeps_one_combined_approval_tier(tmp_path):
    from apps.dimon_live.service import _mentions_bill

    assert _mentions_bill("transfer 10 to John Tan and pay my singtel")
    assert _mentions_bill("Please pay my popwater bill, send Alicia Powell $45")
    assert not _mentions_bill("send $20 to John Tan and buy 2 Apple shares")

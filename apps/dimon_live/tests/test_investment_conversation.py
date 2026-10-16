import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from apps.dimon_live.conversation import (
    ConversationInput,
    ConversationService,
    ConversationStatus,
    InputKind,
    build_adapter_registry,
)
from apps.dimon_live.conversation_store import ConversationStore
from apps.dimon_live.investment_conversation import InvestmentConversationAdapter
from apps.dimon_live.stock_market import (
    DailyBar,
    FxEstimate,
    InvestmentBrief,
    MarketDataSnapshot,
    ResolvedInstrument,
)


NOW = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)


def _brief(*, shortfall: Decimal = Decimal("0")) -> InvestmentBrief:
    instrument = ResolvedInstrument(
        symbol="NVDA",
        name="NVIDIA Corporation",
        exchange="NasdaqGS",
        instrument_type="EQUITY",
        quote_currency="USD",
        source="Yahoo Finance",
    )
    market = MarketDataSnapshot(
        captured_at=NOW,
        source="Yahoo Finance",
        price=Decimal("233.95"),
        market_state="CLOSED",
        trailing_pe=Decimal("29.6"),
        two_week_high=Decimal("237.88"),
        two_week_low=Decimal("221.09"),
        percent_below_two_week_high=Decimal("1.65"),
        percent_above_two_week_low=Decimal("5.81"),
        one_year_close=Decimal("187.65"),
        one_year_change_pct=Decimal("24.68"),
    )
    fx = FxEstimate(
        pair="USD/SGD",
        rate=Decimal("1.28"),
        captured_at=NOW,
        source="Yahoo Finance",
        converted_amount=Decimal("25600.00"),
        warning="Indicative only",
    )
    return InvestmentBrief(
        instrument=instrument,
        market=market,
        requested_allocation=Decimal("20000"),
        allocation_currency="USD",
        allocation_assumption="I'm treating your $20,000 as USD.",
        funding_currency="SGD",
        fx_estimate=fx,
        whole_shares=85,
        estimated_spend=Decimal("19885.75"),
        unallocated_cash=Decimal("114.25"),
        warnings=(),
        next_question="Buy now at market, or set a maximum price?",
        funding_account_label="Savings",
        funding_balance=Decimal("1000"),
        funding_required=Decimal("25600"),
        funding_shortfall=shortfall,
    )


class CountingAdapter(InvestmentConversationAdapter):
    pass


class MarketLanguageInterpreter:
    def interpret(self, text, *, phase, question, state):
        self.last = (text, phase, question, state)
        from apps.dimon_live.model_adapter import FollowUpIntent

        return FollowUpIntent(("BUY_NOW",), "{}", 0.1)


class UnknownLanguageInterpreter:
    def interpret(self, text, *, phase, question, state):
        from apps.dimon_live.model_adapter import FollowUpIntent

        return FollowUpIntent(("UNKNOWN",), "{}", 0.1)


class UnavailableLanguageInterpreter:
    def interpret(self, text, *, phase, question, state):
        from apps.dimon_live.model_adapter import ModelUnavailable

        raise ModelUnavailable("local follow-up model is offline")


class CompoundFundingMarketInterpreter:
    def interpret(self, text, *, phase, question, state):
        from apps.dimon_live.model_adapter import FollowUpIntent

        return FollowUpIntent(("USE_AVAILABLE_FUNDS", "BUY_NOW"), "{}", 0.1)


class InvestmentConversationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        path = Path(self.temporary.name) / "conversation.sqlite3"
        from apps.dimon_live.database import Database

        Database(path, clock=lambda: NOW).initialize()
        self.store = ConversationStore(path, clock=lambda: NOW)
        self.adapter = CountingAdapter()
        self.service = ConversationService(
            self.store, build_adapter_registry((self.adapter,)), lambda: NOW
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _start(self, brief=None):
        return self.service.start(
            domain="INVESTMENT",
            session_scope="session-a",
            user_id="demo-user",
            payload={"brief": brief or _brief(shortfall=Decimal("0"))},
        )

    def test_explicit_market_order_starts_at_review_ready_without_redundant_question(self):
        started = self.service.start(
            domain="INVESTMENT",
            session_scope="session-a",
            user_id="demo-user",
            payload={"brief": _brief(), "initial_order_type": "MARKET"},
        )
        self.assertEqual(started["conversation"]["status"], "READY_FOR_DRAFT")
        self.assertIsNone(started["question"])
        self.assertEqual(started["allowed_choices"], [])
        self.assertIn("Market order draft prepared for review", started["answer"])

    def _turn(self, started, text=None, choice=None, version=None, request_id="req-1"):
        input_value = (
            ConversationInput(InputKind.TEXT, text=text)
            if text is not None
            else ConversationInput(InputKind.CHOICE, choice=choice)
        )
        return self.service.apply_turn(
            started["conversation"]["conversation_id"],
            session_scope="session-a",
            user_id="demo-user",
            request_id=request_id,
            expected_version=version or started["conversation"]["version"],
            source="typed",
            conversation_input=input_value,
        )

    def test_model_interprets_natural_market_reply_before_deterministic_transition(self):
        interpreter = MarketLanguageInterpreter()
        adapter = InvestmentConversationAdapter(followup_interpreter=interpreter)
        service = ConversationService(
            self.store, build_adapter_registry((adapter,)), lambda: NOW
        )
        started = service.start(
            domain="INVESTMENT",
            session_scope="session-a",
            user_id="demo-user",
            payload={"brief": _brief(shortfall=Decimal("0"))},
        )

        result = service.apply_turn(
            started["conversation"]["conversation_id"],
            session_scope="session-a",
            user_id="demo-user",
            request_id="natural-market-reply",
            expected_version=started["conversation"]["version"],
            source="typed",
            conversation_input=ConversationInput(
                InputKind.TEXT, text="execute it at the next opening bell"
            ),
        )

        self.assertEqual(result["conversation"]["status"], "READY_FOR_DRAFT")
        self.assertEqual(interpreter.last[1], "ORDER_STYLE")

    def test_start_keeps_snapshot_and_asks_for_order_style(self):
        result = self._start()
        self.assertEqual(result["conversation"]["status"], "WAITING")
        self.assertEqual(result["question"], "Buy now at market, or set a maximum price?")
        self.assertEqual(result["allowed_choices"], ["BUY_NOW", "SET_LIMIT", "CANCEL"])
        self.assertEqual(result["public_view"]["symbol"], "NVDA")
        self.assertEqual(result["public_view"]["price"], "233.95")

    def test_buy_now_creates_draft_ready_state_without_execution_fields(self):
        result = self._turn(self._start(), choice="BUY_NOW")
        self.assertEqual(result["conversation"]["status"], "READY_FOR_DRAFT")
        self.assertEqual(result["public_view"]["order_type"], "MARKET")
        self.assertEqual(result["public_view"]["time_in_force"], "DAY")
        self.assertNotIn("execution", result["public_view"])
        self.assertNotIn("receipt", result["public_view"])

    def test_ready_state_preserves_provider_and_funding_evidence_for_receipt(self):
        result = self._turn(self._start(), choice="BUY_NOW")
        conversation_id = result["conversation"]["conversation_id"]
        thread = self.store.get_thread(conversation_id, "session-a", "demo-user")
        state = json.loads(thread.state_json)

        self.assertEqual(state["instrument_id"], "YF:NVDA")
        self.assertEqual(state["market_state"], "CLOSED")
        self.assertEqual(state["quote_source"], "Yahoo Finance")
        self.assertEqual(state["quote_captured_at"], NOW.isoformat())
        self.assertEqual(state["fx_pair"], "USD/SGD")
        self.assertEqual(state["fx_rate"], "1.28")
        self.assertEqual(state["fx_source"], "Yahoo Finance")
        self.assertEqual(state["fx_captured_at"], NOW.isoformat())
        self.assertEqual(state["funding_account_id"], "acct_savings_01")
        self.assertNotIn("state_json", result["conversation"])

    def test_spoken_buy_now_at_market_follow_up_creates_market_draft(self):
        result = self._turn(
            self._start(),
            text="Just buy now at market",
        )
        self.assertEqual(result["conversation"]["status"], "READY_FOR_DRAFT")
        self.assertEqual(result["public_view"]["order_type"], "MARKET")

    def test_market_when_open_follow_up_creates_market_draft(self):
        result = self._turn(
            self._start(),
            text="Buy at market when market opens",
        )
        self.assertEqual(result["conversation"]["status"], "READY_FOR_DRAFT")
        self.assertEqual(result["public_view"]["order_type"], "MARKET")

    def test_valid_model_unknown_does_not_guess_ambiguous_language(self):
        adapter = InvestmentConversationAdapter(
            followup_interpreter=UnknownLanguageInterpreter()
        )
        service = ConversationService(
            self.store, build_adapter_registry((adapter,)), lambda: NOW
        )
        started = service.start(
            domain="INVESTMENT",
            session_scope="session-a",
            user_id="demo-user",
            payload={"brief": _brief(shortfall=Decimal("0"))},
        )

        result = service.apply_turn(
            started["conversation"]["conversation_id"],
            session_scope="session-a",
            user_id="demo-user",
            request_id="unknown-market-reply",
            expected_version=started["conversation"]["version"],
            source="typed",
            conversation_input=ConversationInput(
                InputKind.TEXT, text="do whatever you think is best"
            ),
        )

        self.assertEqual(result["conversation"]["status"], "WAITING")
        self.assertEqual(result["conversation"]["version"], 1)
        self.assertEqual(result["trace"][0]["stage"], "CONVERSATION_REASKED")

    def test_model_unavailable_uses_bounded_market_phrase_fallback(self):
        adapter = InvestmentConversationAdapter(
            followup_interpreter=UnavailableLanguageInterpreter()
        )
        service = ConversationService(
            self.store, build_adapter_registry((adapter,)), lambda: NOW
        )
        started = service.start(
            domain="INVESTMENT",
            session_scope="session-a",
            user_id="demo-user",
            payload={"brief": _brief(shortfall=Decimal("0"))},
        )

        result = service.apply_turn(
            started["conversation"]["conversation_id"],
            session_scope="session-a",
            user_id="demo-user",
            request_id="fallback-market-reply",
            expected_version=started["conversation"]["version"],
            source="typed",
            conversation_input=ConversationInput(
                InputKind.TEXT, text="buy at market when market opens"
            ),
        )

        self.assertEqual(result["conversation"]["status"], "READY_FOR_DRAFT")
        self.assertEqual(result["public_view"]["order_type"], "MARKET")

    def test_set_limit_then_price_and_time_in_force(self):
        started = self._start()
        asked = self._turn(started, choice="SET_LIMIT")
        self.assertEqual(asked["conversation"]["version"], 2)
        self.assertEqual(asked["allowed_choices"], ["CANCEL"])
        priced = self._turn(asked, text="5% below", version=2, request_id="req-2")
        self.assertEqual(priced["conversation"]["version"], 3)
        self.assertEqual(priced["public_view"]["limit_price"], "222.25")
        self.assertEqual(priced["allowed_choices"], ["DAY", "GTC", "CANCEL"])
        ready = self._turn(priced, choice="GTC", version=3, request_id="req-3")
        self.assertEqual(ready["conversation"]["status"], "READY_FOR_DRAFT")
        self.assertEqual(ready["public_view"]["time_in_force"], "GTC")

    def test_unrecognized_follow_up_reasks_without_provider_or_state_change(self):
        started = self._start()
        result = self._turn(started, text="maybe", request_id="req-1")
        self.assertEqual(result["conversation"]["version"], 1)
        self.assertEqual(result["trace"][0]["stage"], "CONVERSATION_REASKED")
        self.assertEqual(result["conversation"]["expires_at"], started["conversation"]["expires_at"])

    def test_funding_shortfall_can_use_available_balance(self):
        started = self._start(_brief(shortfall=Decimal("24600")))
        # Savings is the only account that can pay, so there is no "another
        # account" choice to offer.
        self.assertEqual(started["allowed_choices"], ["USE_AVAILABLE_FUNDS", "CANCEL"])
        result = self._turn(started, choice="USE_AVAILABLE_FUNDS")
        self.assertEqual(result["conversation"]["status"], "WAITING")
        self.assertEqual(result["conversation"]["version"], 2)
        self.assertEqual(result["public_view"]["requested_allocation"], "780.63")
        self.assertIn("buy now", result["question"].lower())

    def test_unknown_model_recovers_clear_funding_repair_language(self):
        adapter = InvestmentConversationAdapter(
            followup_interpreter=UnknownLanguageInterpreter()
        )
        service = ConversationService(
            self.store, build_adapter_registry((adapter,)), lambda: NOW
        )

        for index, reply in enumerate(
            (
                "Yes, lower the amount just nice to what I can afford.",
                "Use whatever is available in my balance.",
                "Reduce it to what I have in savings.",
            )
        ):
            with self.subTest(reply=reply):
                started = service.start(
                    domain="INVESTMENT",
                    session_scope="session-a",
                    user_id="demo-user",
                    payload={"brief": _brief(shortfall=Decimal("24600"))},
                )
                result = service.apply_turn(
                    started["conversation"]["conversation_id"],
                    session_scope="session-a",
                    user_id="demo-user",
                    request_id=f"funding-recovery-{index}",
                    expected_version=started["conversation"]["version"],
                    source="typed",
                    conversation_input=ConversationInput(InputKind.TEXT, text=reply),
                )

                self.assertEqual(result["conversation"]["version"], 2)
                self.assertEqual(result["public_view"]["requested_allocation"], "780.63")
                self.assertEqual(
                    result["allowed_choices"], ["BUY_NOW", "SET_LIMIT", "CANCEL"]
                )

    def test_compound_funding_and_market_reply_prepares_draft_in_one_turn(self):
        adapter = InvestmentConversationAdapter(
            followup_interpreter=CompoundFundingMarketInterpreter()
        )
        service = ConversationService(
            self.store, build_adapter_registry((adapter,)), lambda: NOW
        )
        started = service.start(
            domain="INVESTMENT",
            session_scope="session-a",
            user_id="demo-user",
            payload={"brief": _brief(shortfall=Decimal("24600"))},
        )

        result = service.apply_turn(
            started["conversation"]["conversation_id"],
            session_scope="session-a",
            user_id="demo-user",
            request_id="compound-funding-market",
            expected_version=started["conversation"]["version"],
            source="voice",
            conversation_input=ConversationInput(
                InputKind.TEXT,
                text="Lower it to what I can afford and execute it on market.",
            ),
        )

        self.assertEqual(result["conversation"]["status"], "READY_FOR_DRAFT")
        self.assertEqual(result["public_view"]["requested_allocation"], "780.63")
        self.assertEqual(result["public_view"]["order_type"], "MARKET")
        self.assertEqual(result["public_view"]["time_in_force"], "DAY")
        self.assertEqual(result["allowed_choices"], [])

    def test_unknown_model_recovers_other_clear_phase_choices(self):
        scenarios = (
            ("FUNDING", "Switch to a different funding account", "CHANGE_FUNDING_ACCOUNT"),
            ("ORDER_STYLE", "Go ahead at the current market price", "BUY_NOW"),
            ("ORDER_STYLE", "Set a maximum price instead", "SET_LIMIT"),
            ("TIME_IN_FORCE", "Keep it open until I cancel it", "GTC"),
            ("TIME_IN_FORCE", "Only keep the order for today", "DAY"),
        )

        for phase, reply, expected in scenarios:
            with self.subTest(phase=phase, reply=reply):
                self.assertEqual(
                    InvestmentConversationAdapter._recover_bounded_choice(phase, reply),
                    expected,
                )

    def test_bounded_recovery_does_not_guess_ambiguous_language(self):
        for phase in ("FUNDING", "ORDER_STYLE", "LIMIT_PRICE", "TIME_IN_FORCE"):
            with self.subTest(phase=phase):
                self.assertIsNone(
                    InvestmentConversationAdapter._recover_bounded_choice(
                        phase, "Do whatever you think is best"
                    )
                )

    def test_cancel_ends_conversation(self):
        result = self._turn(self._start(), choice="CANCEL")
        self.assertEqual(result["conversation"]["status"], "CANCELLED")


if __name__ == "__main__":
    unittest.main()

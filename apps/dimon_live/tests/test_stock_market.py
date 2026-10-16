import json
import unittest
import tempfile
import sqlite3
import asyncio
import threading
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from apps.dimon_live import stock_market
from apps.dimon_live.database import Database
from apps.dimon_live.service import ActionService
from apps.dimon_live.speech import result_summary


NOW = datetime(2026, 10, 4, 4, 0, tzinfo=timezone.utc)


class FakeMarketProvider:
    def search(self, label):
        self.search_thread_id = threading.get_ident()
        self.searched = label
        return (
            stock_market.InstrumentCandidate(
                symbol="CTSH",
                name="Cognizant Technology Solutions Corporation",
                exchange="NasdaqGS",
                instrument_type="EQUITY",
            ),
            stock_market.InstrumentCandidate(
                symbol="CTSO",
                name="Catalyst Oncology, Inc.",
                exchange="NasdaqCM",
                instrument_type="EQUITY",
            ),
        )

    def describe(self, symbol):
        self.described = symbol
        return stock_market.ResolvedInstrument(
            symbol="CTSH",
            name="Cognizant Technology Solutions Corporation",
            exchange="NasdaqGS",
            instrument_type="EQUITY",
            quote_currency="USD",
            source="Yahoo Finance",
        )

    def snapshot(self, symbol, captured_at):
        self.snapshotted = (symbol, captured_at)
        return stock_market.ProviderMarketData(
            price=Decimal("60.00"),
            market_state="OPEN",
            trailing_pe=Decimal("9.0"),
            bars=(
                stock_market.DailyBar(date(2025, 10, 3), Decimal("74"), Decimal("71"), Decimal("75")),
                stock_market.DailyBar(date(2026, 9, 19), Decimal("80"), Decimal("40"), Decimal("55")),
                stock_market.DailyBar(date(2026, 9, 20), Decimal("79"), Decimal("41"), Decimal("56")),
                stock_market.DailyBar(date(2026, 9, 21), Decimal("64"), Decimal("58"), Decimal("61")),
                stock_market.DailyBar(date(2026, 9, 28), Decimal("63"), Decimal("57"), Decimal("59")),
                stock_market.DailyBar(date(2026, 10, 2), Decimal("62"), Decimal("59"), Decimal("60")),
            ),
        )

    def fx_estimate(self, base_currency, quote_currency, amount, captured_at):
        self.fx_requested = (base_currency, quote_currency, amount, captured_at)
        return stock_market.FxEstimate(
            pair="USD/SGD",
            rate=Decimal("1.28"),
            captured_at=captured_at,
            source="Yahoo Finance",
            converted_amount=amount * Decimal("1.28"),
            warning="Indicative only; your broker's executed rate can differ.",
        )


class NvidiaMarketProvider(FakeMarketProvider):
    def search(self, label):
        self.search_thread_id = threading.get_ident()
        self.searched = label
        return (
            stock_market.InstrumentCandidate(
                symbol="NVDA",
                name="NVIDIA Corporation",
                exchange="NasdaqGS",
                instrument_type="EQUITY",
            ),
        )

    def describe(self, symbol):
        self.described = symbol
        return stock_market.ResolvedInstrument(
            symbol="NVDA",
            name="NVIDIA Corporation",
            exchange="NasdaqGS",
            instrument_type="EQUITY",
            quote_currency="USD",
            source="Yahoo Finance",
        )


class RecordingConversationService:
    def __init__(self):
        self.calls = []

    def start(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "conversation": {
                "conversation_id": "conversation_test",
                "domain": "INVESTMENT",
                "status": "WAITING",
                "version": 1,
                "question": "Buy now at market, or set a maximum price?",
                "allowed_choices": ["BUY_NOW", "SET_LIMIT", "CANCEL"],
                "expires_at": "2026-10-05T08:15:00+00:00",
            },
            "question": "Buy now at market, or set a maximum price?",
            "allowed_choices": ["BUY_NOW", "SET_LIMIT", "CANCEL"],
            "trace": [{"stage": "CONVERSATION_STARTED"}],
        }


class StockMarketContractTests(unittest.TestCase):
    def test_model_stock_label_drops_generic_stock_price_suffix(self):
        from apps.dimon_live.stock_market import extract_model_stock_label

        for model_label in ("NVIDIA stock", "NVIDIA stock price", "NVIDIA shares"):
            with self.subTest(model_label=model_label):
                result = extract_model_stock_label(
                    f"What is {model_label} right now?",
                    {
                        "kind": "STOCK_RESEARCH",
                        "instrument_label": model_label,
                        "instrument_evidence": model_label,
                        "objective": "NOT_APPLICABLE",
                    },
                )

                self.assertIsNotNone(result)
                self.assertEqual(result[0], "NVIDIA")

    def test_scheduled_market_state_fallback_handles_missing_yahoo_session(self):
        closed = datetime(2026, 10, 6, 5, 37, tzinfo=timezone.utc)
        open_time = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)

        self.assertEqual(
            stock_market._scheduled_market_state("NasdaqGS", closed), "CLOSED"
        )
        self.assertEqual(
            stock_market._scheduled_market_state("NasdaqGS", open_time), "OPEN"
        )

    def test_parser_preserves_bare_dollar_allocation_and_company_label(self):
        intent = stock_market.parse_raw_investment_intent(
            "I want to invest $2,000 in Cognizant Technology Solutions."
        )

        self.assertEqual(intent.instrument_label, "Cognizant Technology Solutions")
        self.assertEqual(intent.allocation, Decimal("2000"))
        self.assertIsNone(intent.explicit_currency)
        self.assertTrue(intent.bare_dollar)
        self.assertEqual(intent.objective, "GENERAL_ACQUISITION")

    def test_parser_accepts_spoken_put_amount_for_dynamic_stock_resolution(self):
        intent = stock_market.parse_raw_investment_intent(
            "I want to put two thousand dollars into NVIDIA."
        )

        self.assertIsNotNone(intent)
        self.assertEqual(intent.instrument_label, "NVIDIA")
        self.assertEqual(intent.allocation, Decimal("2000"))
        self.assertIsNone(intent.explicit_currency)
        self.assertTrue(intent.bare_dollar)

    def test_parser_accepts_ticker_first_request_with_abbreviated_allocation(self):
        intent = stock_market.parse_raw_investment_intent(
            "I want to buy NVDA shares I'm planning to put $10k in"
        )

        self.assertIsNotNone(intent)
        self.assertEqual(intent.instrument_label, "NVDA")
        self.assertEqual(intent.allocation, Decimal("10000"))
        self.assertIsNone(intent.explicit_currency)
        self.assertTrue(intent.bare_dollar)

    def test_parser_expands_supported_abbreviated_allocations(self):
        cases = (
            ("I want to put $10k into NVDA.", Decimal("10000")),
            ("Buy NVIDIA shares with USD 2.5k.", Decimal("2500.0")),
            ("Acquire Broadcom shares using $1m.", Decimal("1000000")),
        )

        for text, expected in cases:
            with self.subTest(text=text):
                intent = stock_market.parse_raw_investment_intent(text)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.allocation, expected)

    def test_parser_accepts_company_first_allocation_language(self):
        intent = stock_market.parse_raw_investment_intent(
            "Buy Cognizant Technology Solutions shares with $2.5k."
        )

        self.assertIsNotNone(intent)
        self.assertEqual(
            intent.instrument_label, "Cognizant Technology Solutions"
        )
        self.assertEqual(intent.allocation, Decimal("2500.0"))

    def test_parser_preserves_explicit_share_quantity(self):
        intent = stock_market.parse_raw_investment_intent(
            "Help me buy 1 NVIDIA share."
        )

        self.assertIsNotNone(intent)
        self.assertEqual(intent.instrument_label, "NVIDIA")
        self.assertIsNone(intent.allocation)
        self.assertEqual(intent.quantity, 1)

    def test_quantity_request_prices_exact_shares_without_allocation_prompt(self):
        intent = stock_market.parse_raw_investment_intent(
            "Help me buy 1 NVIDIA share."
        )

        brief = stock_market.build_investment_brief(
            intent, NvidiaMarketProvider(), captured_at=NOW, funding_currency="SGD"
        )

        self.assertEqual(brief.requested_quantity, 1)
        self.assertEqual(brief.whole_shares, 1)
        self.assertEqual(brief.requested_allocation, Decimal("60.00"))
        self.assertEqual(brief.estimated_spend, Decimal("60.00"))
        self.assertEqual(brief.unallocated_cash, Decimal("0.00"))
        self.assertIn("1 whole share", stock_market.brief_response(brief))
        self.assertNotIn("allocation", stock_market.brief_response(brief).lower())

    def test_parser_fallback_strips_dollar_worth_phrase_from_company_label(self):
        intent = stock_market.parse_raw_investment_intent(
            "I want you to help me buy five hundred dollars worth of NVIDIA stock."
        )

        self.assertIsNotNone(intent)
        self.assertEqual(intent.instrument_label, "NVIDIA")
        self.assertEqual(intent.allocation, Decimal("500"))

    def test_parser_accepts_allocated_amount_language(self):
        intent = stock_market.parse_raw_investment_intent(
            "I have allocated $2,000, and I feel like investing it in NVIDIA."
        )

        self.assertIsNotNone(intent)
        self.assertEqual(intent.instrument_label, "NVIDIA")
        self.assertEqual(intent.allocation, Decimal("2000"))

    def test_parser_does_not_treat_a_savings_deposit_as_a_stock_request(self):
        self.assertIsNone(
            stock_market.parse_raw_investment_intent(
                "I want to put two thousand dollars into savings."
            )
        )

    def test_parser_rejects_malformed_or_nonpositive_allocation_and_preserves_cents(self):
        self.assertIsNone(
            stock_market.parse_raw_investment_intent(
                "I want to invest $2,00 in Cognizant Technology Solutions."
            )
        )
        self.assertIsNone(
            stock_market.parse_raw_investment_intent(
                "I want to invest $0 in Cognizant Technology Solutions."
            )
        )
        intent = stock_market.parse_raw_investment_intent(
            "I want to invest $2,000.99 in Cognizant Technology Solutions."
        )
        self.assertEqual(intent.allocation, Decimal("2000.99"))

    def test_parser_rejects_malformed_separators_and_unsupported_amount_suffixes(self):
        for amount in ("$1 000", "$2abc", "$2kk", "$2-000", "$2/000", "$2'000"):
            with self.subTest(amount=amount):
                self.assertIsNone(
                    stock_market.parse_raw_investment_intent(
                        f"I want to invest {amount} in Cognizant Technology Solutions."
                    )
                )

    def test_parser_rejects_multiple_currency_amounts_in_one_request(self):
        self.assertIsNone(
            stock_market.parse_raw_investment_intent(
                "I want to invest $1 or $2,000 in Cognizant Technology Solutions."
            )
        )

    def test_parser_leaves_unrelated_payment_for_the_payment_pipeline(self):
        self.assertIsNone(
            stock_market.parse_raw_investment_intent(
                "Send John Smith $2,000 from savings."
            )
        )

    def test_resolver_prefers_supported_primary_exchange_over_foreign_cross_listing(self):
        selected = stock_market.resolve_candidate(
            "Cognizant Technology Solutions",
            (
                stock_market.InstrumentCandidate(
                    "CTSH", "Cognizant Technology Solutions Corporation", "NASDAQ", "EQUITY"
                ),
                stock_market.InstrumentCandidate(
                    "1CTSH.MI", "Cognizant Technology Solutions Corporation", "Milan", "EQUITY"
                ),
                stock_market.InstrumentCandidate(
                    "COZ.HA", "Cognizant Technology Solutions Corp", "Hanover", "EQUITY"
                ),
            ),
        )

        self.assertEqual(selected.symbol, "CTSH")

    def test_resolver_rejects_foreign_cross_listing_when_primary_listing_is_absent(self):
        with self.assertRaisesRegex(stock_market.StockMarketError, "INSTRUMENT_NOT_FOUND"):
            stock_market.resolve_candidate(
                "Cognizant Technology Solutions",
                (
                    stock_market.InstrumentCandidate(
                        "1CTSH.MI", "Cognizant Technology Solutions Corporation", "Milan", "EQUITY"
                    ),
                ),
            )

    def test_brief_uses_fourteen_calendar_days_and_preceding_year_session(self):
        provider = FakeMarketProvider()
        intent = stock_market.parse_raw_investment_intent(
            "I want to invest $2,000 in Cognizant Technology Solutions."
        )

        brief = stock_market.build_investment_brief(
            intent, provider, captured_at=NOW, funding_currency="SGD"
        )

        self.assertEqual(provider.searched, "Cognizant Technology Solutions")
        self.assertEqual(provider.described, "CTSH")
        self.assertEqual(brief.instrument.symbol, "CTSH")
        self.assertEqual(brief.allocation_currency, "USD")
        self.assertEqual(brief.market.two_week_high, Decimal("64"))
        self.assertEqual(brief.market.two_week_low, Decimal("57"))
        self.assertEqual(brief.market.percent_below_two_week_high, Decimal("6.25"))
        self.assertEqual(brief.market.percent_above_two_week_low, Decimal("5.26"))
        self.assertEqual(brief.market.one_year_change_pct, Decimal("-20.00"))
        self.assertEqual(brief.whole_shares, 33)
        self.assertEqual(brief.estimated_spend, Decimal("1980.00"))
        self.assertEqual(brief.unallocated_cash, Decimal("20.00"))
        self.assertEqual(brief.fx_estimate.converted_amount, Decimal("2560.00"))

    def test_two_week_window_uses_exchange_local_date_after_utc_midnight(self):
        intent = stock_market.parse_raw_investment_intent(
            "I want to invest $2,000 in Cognizant Technology Solutions."
        )

        brief = stock_market.build_investment_brief(
            intent,
            FakeMarketProvider(),
            captured_at=datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc),
            funding_currency="SGD",
        )

        self.assertEqual(brief.market.two_week_high, Decimal("79"))
        self.assertEqual(brief.market.two_week_low, Decimal("41"))

    def test_current_session_high_and_low_are_included_in_two_week_range(self):
        class IntradayProvider(FakeMarketProvider):
            def snapshot(self, symbol, captured_at):
                data = super().snapshot(symbol, captured_at)
                return replace(
                    data,
                    price=Decimal("70"),
                    day_high=Decimal("72"),
                    day_low=Decimal("59"),
                )

        intent = stock_market.parse_raw_investment_intent(
            "I want to invest $2,000 in Cognizant Technology Solutions."
        )
        brief = stock_market.build_investment_brief(
            intent, IntradayProvider(), captured_at=NOW, funding_currency="SGD"
        )

        self.assertEqual(brief.market.two_week_high, Decimal("72"))
        self.assertEqual(brief.market.two_week_low, Decimal("57"))
        self.assertEqual(brief.market.percent_below_two_week_high, Decimal("2.78"))

    def test_nonfinite_provider_price_fails_with_stable_reason(self):
        class NanProvider(FakeMarketProvider):
            def snapshot(self, symbol, captured_at):
                return replace(super().snapshot(symbol, captured_at), price=Decimal("NaN"))

        intent = stock_market.parse_raw_investment_intent(
            "I want to invest $2,000 in Cognizant Technology Solutions."
        )
        with self.assertRaisesRegex(stock_market.StockMarketError, "MARKET_DATA_UNAVAILABLE"):
            stock_market.build_investment_brief(
                intent, NanProvider(), captured_at=NOW, funding_currency="SGD"
            )

    def test_brief_response_is_concise_and_keeps_provenance_out_of_speech(self):
        intent = stock_market.parse_raw_investment_intent(
            "I want to invest $2,000 in Cognizant Technology Solutions."
        )

        brief = stock_market.build_investment_brief(
            intent, FakeMarketProvider(), captured_at=NOW, funding_currency="SGD"
        )
        response = stock_market.brief_response(brief)

        self.assertIn("CTSH is US$60.00", response)
        self.assertNotIn("Two-week range", response)
        self.assertNotIn("above the low", response)
        self.assertIn("down 20.0% from one year ago", response)
        self.assertIn("I treated your $2,000 as USD", response)
        self.assertIn("33 whole shares", response)
        self.assertIn("about S$2,560.00", response)
        self.assertNotIn("Yahoo Finance", response)
        self.assertNotIn("2026-10-04", response)
        self.assertNotIn("broker", response)
        self.assertLessEqual(len(response.split()), 75)
        self.assertTrue(response.endswith("Buy now at market, or set a maximum price?"))

    def test_insufficient_funding_brief_omits_nonactionable_market_detail(self):
        intent = stock_market.parse_raw_investment_intent(
            "I want to invest $2,000 in Cognizant Technology Solutions."
        )
        brief = stock_market.build_investment_brief(
            intent, FakeMarketProvider(), captured_at=NOW, funding_currency="SGD"
        )
        brief = stock_market.assess_funding(
            brief,
            account_label="savings",
            account_currency="SGD",
            balance=Decimal("1000.00"),
        )

        response = stock_market.brief_response(brief)

        self.assertEqual(
            response,
            "CTSH is US$60.00. Your US$2,000 allocation needs about S$2,560.00, "
            "but your savings balance is S$1,000.00. You're short S$1,560.00. "
            "Lower the amount?",
        )
        for omitted in ("P/E", "Two-week range", "one year", "whole shares"):
            self.assertNotIn(omitted, response)

    def test_action_service_returns_real_brief_without_model_or_order(self):
        class ModelMustNotRun:
            def extract(self, text):
                raise AssertionError("the model must not supply market facts")

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "demo.sqlite3", clock=lambda: NOW)
            database.initialize()
            service = ActionService(
                database, ModelMustNotRun(), stock_provider=FakeMarketProvider()
            )

            result = service.process(
                "00000000-0000-4000-8000-000000000910",
                "I want to invest $2,000 in Cognizant Technology Solutions.",
            )

            self.assertEqual(result["outcome"], "ANSWERED")
            self.assertEqual(result["reason_code"], "REAL_STOCK_BRIEF")
            self.assertEqual(result["investing"]["view"], "REAL_STOCK_BRIEF")
            self.assertEqual(result["investing"]["brief"]["instrument"]["symbol"], "CTSH")
            self.assertEqual(result["investing"]["brief"]["whole_shares"], 33)
            self.assertEqual(result["investing"]["brief"]["funding_account_label"], "savings")
            self.assertEqual(result["investing"]["brief"]["funding_balance"], 2000.0)
            self.assertEqual(result["investing"]["brief"]["funding_required"], 2560.0)
            self.assertEqual(result["investing"]["brief"]["funding_shortfall"], 560.0)
            self.assertIn("but your savings balance is S$2,000.00", result["answer"])
            self.assertIn("You're short S$560.00", result["answer"])
            self.assertTrue(result["answer"].endswith("Lower the amount?"))
            self.assertEqual(result["question"], "Lower the amount?")
            self.assertEqual(
                result["investing"]["brief"]["fx_estimate"]["source"], "Yahoo Finance"
            )
            self.assertEqual(result["state"]["transactions"], [])
            with sqlite3.connect(database.path) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM investment_orders").fetchone()[0],
                    0,
                )

    def test_real_stock_brief_starts_optional_conversation_without_changing_brief(self):
        class ModelMustNotRun:
            def extract(self, text):
                raise AssertionError("the model must not supply market facts")

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "demo.sqlite3", clock=lambda: NOW)
            database.initialize()
            conversation = RecordingConversationService()
            service = ActionService(
                database,
                ModelMustNotRun(),
                stock_provider=FakeMarketProvider(),
                conversation_service=conversation,
                session_scope="session-a",
                user_id="demo-user",
            )

            result = service.process(
                "00000000-0000-4000-8000-000000000911",
                "I want to invest $2,000 in Cognizant Technology Solutions.",
            )

        self.assertEqual(result["reason_code"], "REAL_STOCK_BRIEF")
        self.assertEqual(result["conversation"]["conversation_id"], "conversation_test")
        self.assertEqual(result["allowed_choices"], ["BUY_NOW", "SET_LIMIT", "CANCEL"])
        self.assertEqual(len(conversation.calls), 1)
        self.assertEqual(conversation.calls[0]["payload"]["brief"].instrument.symbol, "CTSH")

    def test_spoken_nvidia_request_uses_dynamic_provider_without_model_or_catalog(self):
        class ModelMustNotRun:
            def extract(self, text):
                raise AssertionError("the model must not reinterpret a verified stock request")

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "demo.sqlite3", clock=lambda: NOW)
            database.initialize()
            provider = NvidiaMarketProvider()
            service = ActionService(database, ModelMustNotRun(), stock_provider=provider)

            result = service.process(
                "00000000-0000-4000-8000-000000000912",
                "I want to put two thousand dollars into NVIDIA.",
            )

        self.assertEqual(result["outcome"], "ANSWERED")
        self.assertEqual(result["reason_code"], "REAL_STOCK_BRIEF")
        self.assertEqual(result["investing"]["brief"]["instrument"]["symbol"], "NVDA")
        self.assertEqual(provider.searched, "NVIDIA")
        self.assertEqual(provider.described, "NVDA")

    def test_stock_llm_extracts_company_before_yahoo_resolution(self):
        class StockExtractor:
            def __init__(self):
                self.calls = []

            def extract(self, text):
                from apps.dimon_live.model_adapter import ExtractedIntent

                self.calls.append(text)
                intent = {
                    "contract_version": 2,
                    "kind": "STOCK_INVESTMENT",
                    "instrument_label": "NVIDIA",
                    "instrument_evidence": "NVIDIA stock",
                    "objective": "GENERAL_ACQUISITION",
                    "size_kind": "ALLOCATION",
                    "quantity": None,
                    "quantity_evidence": None,
                    "allocation_evidence": "five hundred dollars",
                }
                return ExtractedIntent(intent, json.dumps(intent), 5.0)

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "demo.sqlite3", clock=lambda: NOW)
            database.initialize()
            provider = NvidiaMarketProvider()
            stock_extractor = StockExtractor()
            service = ActionService(
                database,
                object(),
                stock_provider=provider,
                stock_extractor=stock_extractor,
            )

            result = service.process(
                "00000000-0000-4000-8000-000000000916",
                "I want you to help me buy five hundred dollars worth of NVIDIA stock.",
            )

        self.assertEqual(result["reason_code"], "REAL_STOCK_BRIEF")
        self.assertEqual(result["investing"]["brief"]["instrument"]["symbol"], "NVDA")
        self.assertEqual(provider.searched, "NVIDIA")
        self.assertEqual(stock_extractor.calls, [
            "I want you to help me buy five hundred dollars worth of NVIDIA stock."
        ])

    def test_stock_price_question_uses_research_intent_without_transfer_recipient(self):
        class TransferMustNotRun:
            def extract(self, text):
                raise AssertionError("a stock price question must not enter transfer extraction")

        class StockResearchExtractor:
            def extract(self, text):
                from apps.dimon_live.model_adapter import ExtractedIntent

                intent = {
                    "contract_version": 1,
                    "kind": "STOCK_RESEARCH",
                    "instrument_label": "NVIDIA stock price",
                    "instrument_evidence": "NVIDIA stock price",
                    "objective": "NOT_APPLICABLE",
                }
                return ExtractedIntent(intent, json.dumps(intent), 5.0)

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "demo.sqlite3", clock=lambda: NOW)
            database.initialize()
            service = ActionService(
                database,
                TransferMustNotRun(),
                stock_provider=NvidiaMarketProvider(),
                stock_extractor=StockResearchExtractor(),
            )

            result = service.process(
                "00000000-0000-4000-8000-000000000918",
                "What is NVIDIA stock price right now?",
            )

        self.assertEqual(result["outcome"], "ANSWERED")
        self.assertEqual(result["reason_code"], "REAL_STOCK_RESEARCH")
        self.assertEqual(result["investing"]["view"], "REAL_STOCK_RESEARCH")
        self.assertEqual(result["investing"]["research"]["instrument"]["symbol"], "NVDA")
        self.assertIn("US$60.00", result["answer"])
        self.assertNotIn("recipient", result["answer"].casefold())
        self.assertNotIn("question", result)

    def test_stock_request_falls_back_to_deterministic_parser_when_model_is_unavailable(self):
        from apps.dimon_live.model_adapter import ModelUnavailable

        class UnavailableStockExtractor:
            def extract(self, text):
                raise ModelUnavailable("qwen unavailable")

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "demo.sqlite3", clock=lambda: NOW)
            database.initialize()
            service = ActionService(
                database,
                object(),
                stock_provider=NvidiaMarketProvider(),
                stock_extractor=UnavailableStockExtractor(),
            )

            result = service.process(
                "00000000-0000-4000-8000-000000000917",
                "I want you to help me buy five hundred dollars worth of NVIDIA stock.",
            )

        self.assertEqual(result["reason_code"], "REAL_STOCK_BRIEF")
        self.assertTrue(
            any(item["stage"] == "STOCK_MODEL_UNAVAILABLE" for item in result["trace"])
        )

    def test_ticker_first_abbreviated_request_bypasses_fictional_trending(self):
        class ModelMustNotRun:
            def extract(self, text):
                raise AssertionError("the model must not reinterpret a verified stock request")

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "demo.sqlite3", clock=lambda: NOW)
            database.initialize()
            provider = NvidiaMarketProvider()
            service = ActionService(database, ModelMustNotRun(), stock_provider=provider)

            result = service.process(
                "00000000-0000-4000-8000-000000000913",
                "I want to buy NVDA shares I'm planning to put $10k in",
            )

        self.assertEqual(result["outcome"], "ANSWERED")
        self.assertEqual(result["reason_code"], "REAL_STOCK_BRIEF")
        self.assertEqual(result["investing"]["brief"]["instrument"]["symbol"], "NVDA")
        self.assertEqual(result["investing"]["brief"]["requested_allocation"], 10000.0)
        self.assertNotEqual(result["investing"]["view"], "TRENDING")

    def test_named_real_stock_without_allocation_asks_for_amount_not_mock_trending(self):
        class ModelMustNotRun:
            def extract(self, text):
                raise AssertionError("the model must not reinterpret a named stock request")

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "demo.sqlite3", clock=lambda: NOW)
            database.initialize()
            service = ActionService(
                database, ModelMustNotRun(), stock_provider=NvidiaMarketProvider()
            )

            result = service.process(
                "00000000-0000-4000-8000-000000000914",
                "I want to buy NVDA shares",
            )

        self.assertEqual(result["outcome"], "ANSWERED")
        self.assertEqual(result["reason_code"], "REAL_STOCK_ALLOCATION_REQUIRED")
        self.assertEqual(
            result["question"],
            "How many shares of NVDA would you like, or what amount would you like to invest?",
        )
        self.assertNotIn("investing", result)

    def test_named_real_stock_with_explicit_quantity_does_not_ask_for_allocation(self):
        class ModelMustNotRun:
            def extract(self, text):
                raise AssertionError("the model must not supply market facts")

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "demo.sqlite3", clock=lambda: NOW)
            database.initialize()
            service = ActionService(
                database, ModelMustNotRun(), stock_provider=NvidiaMarketProvider()
            )

            result = service.process(
                "00000000-0000-4000-8000-000000000915",
                "Help me buy 1 NVIDIA share.",
            )

        self.assertEqual(result["reason_code"], "REAL_STOCK_BRIEF")
        self.assertEqual(result["investing"]["brief"]["requested_quantity"], 1)
        self.assertEqual(result["investing"]["brief"]["whole_shares"], 1)
        self.assertEqual(result["question"], "Buy now at market, or set a maximum price?")

    def test_answered_market_brief_is_available_to_the_voice_result_path(self):
        answer = "CTSH is trading at US$60. What is the highest price you want to pay?"

        self.assertEqual(
            result_summary(
                {
                    "outcome": "ANSWERED",
                    "reason_code": "REAL_STOCK_BRIEF",
                    "answer": answer,
                }
            ),
            answer,
        )
        self.assertIsNone(
            result_summary(
                {
                    "outcome": "ANSWERED",
                    "reason_code": "BALANCE_INQUIRY",
                    "answer": "Your savings balance is S$1,000.",
                }
            )
        )

    def test_stock_brief_voice_summary_pronounces_financial_notation(self):
        class SpokenNvidiaProvider(NvidiaMarketProvider):
            def snapshot(self, symbol, captured_at):
                self.snapshotted = (symbol, captured_at)
                return stock_market.ProviderMarketData(
                    price=Decimal("233.95"),
                    market_state="CLOSED",
                    trailing_pe=Decimal("29.6"),
                    bars=(
                        stock_market.DailyBar(
                            date(2025, 10, 3),
                            Decimal("190.00"),
                            Decimal("185.00"),
                            Decimal("187.61"),
                        ),
                        stock_market.DailyBar(
                            date(2026, 10, 2),
                            Decimal("237.88"),
                            Decimal("221.09"),
                            Decimal("233.00"),
                        ),
                    ),
                )

            def fx_estimate(self, base_currency, quote_currency, amount, captured_at):
                return stock_market.FxEstimate(
                    pair="USD/SGD",
                    rate=Decimal("1.2789"),
                    captured_at=captured_at,
                    source="Yahoo Finance",
                    converted_amount=Decimal("2557.80"),
                    warning="Indicative only; your broker's executed rate can differ.",
                )

        intent = stock_market.parse_raw_investment_intent(
            "I want to put two thousand dollars into NVIDIA."
        )
        brief = stock_market.build_investment_brief(
            intent, SpokenNvidiaProvider(), captured_at=NOW, funding_currency="SGD"
        )
        brief = stock_market.assess_funding(
            brief,
            account_label="savings",
            account_currency="SGD",
            balance=Decimal("1000.00"),
        )
        visual_answer = stock_market.brief_response(brief)

        spoken = result_summary(
            {
                "outcome": "ANSWERED",
                "reason_code": "REAL_STOCK_BRIEF",
                "answer": visual_answer,
                "investing": {
                    "view": "REAL_STOCK_BRIEF",
                    "brief": brief.to_dict(),
                    "disclaimer": stock_market.DISCLAIMER,
                },
            }
        )

        self.assertEqual(
            spoken,
            "N V D A is 233 U. S. dollars and 95 cents. Your allocation of "
            "2,000 U. S. dollars needs about 2,557 Singapore dollars and 80 cents, "
            "but your savings balance is 1,000 Singapore dollars. You're short "
            "1,557 Singapore dollars and 80 cents. Lower the amount?",
        )
        self.assertNotEqual(spoken, visual_answer)

    def test_stock_brief_uses_plain_market_session_language_without_two_week_range(self):
        provider = NvidiaMarketProvider()
        provider_data = provider.snapshot("NVDA", NOW)
        provider_data = stock_market.ProviderMarketData(
            price=provider_data.price,
            market_state="REGULAR",
            trailing_pe=provider_data.trailing_pe,
            bars=provider_data.bars,
            day_high=provider_data.day_high,
            day_low=provider_data.day_low,
        )
        class RegularProvider(NvidiaMarketProvider):
            def snapshot(self, symbol, captured_at):
                return provider_data

        intent = stock_market.parse_raw_investment_intent(
            "I want to put two thousand dollars into NVIDIA."
        )
        brief = stock_market.build_investment_brief(
            intent, RegularProvider(), captured_at=NOW, funding_currency="SGD"
        )
        visual_answer = stock_market.brief_response(brief)
        spoken = result_summary(
            {
                "outcome": "ANSWERED",
                "reason_code": "REAL_STOCK_BRIEF",
                "answer": visual_answer,
                "investing": {"view": "REAL_STOCK_BRIEF", "brief": brief.to_dict()},
            }
        )

        self.assertIn("Nasdaq is currently trading.", visual_answer)
        self.assertNotIn("regular", visual_answer.lower())
        self.assertNotIn("two-week", visual_answer.lower())
        self.assertIn("Nasdaq is currently trading.", spoken)
        self.assertNotIn("two weeks", spoken.lower())
        self.assertIn("price to earnings ratio", spoken)

    def test_unknown_market_status_uses_one_concise_order_caveat(self):
        provider = NvidiaMarketProvider()
        provider_data = provider.snapshot("NVDA", NOW)
        provider_data = stock_market.ProviderMarketData(
            price=provider_data.price,
            market_state="UNKNOWN",
            trailing_pe=provider_data.trailing_pe,
            bars=provider_data.bars,
            day_high=provider_data.day_high,
            day_low=provider_data.day_low,
        )

        class UnknownStatusProvider(NvidiaMarketProvider):
            def snapshot(self, symbol, captured_at):
                return provider_data

        intent = stock_market.parse_raw_investment_intent(
            "I want to put five hundred dollars into NVIDIA."
        )
        brief = stock_market.build_investment_brief(
            intent, UnknownStatusProvider(), captured_at=NOW, funding_currency="SGD"
        )
        visual_answer = stock_market.brief_response(brief)
        spoken = result_summary(
            {
                "outcome": "ANSWERED",
                "reason_code": "REAL_STOCK_BRIEF",
                "answer": visual_answer,
                "investing": {"view": "REAL_STOCK_BRIEF", "brief": brief.to_dict()},
            }
        )

        caveat = "Nasdaq status unavailable; market order must wait."
        self.assertIn(caveat, visual_answer)
        self.assertIn(caveat, spoken)
        self.assertNotIn("session status is unavailable", visual_answer)
        self.assertNotIn("next eligible session", visual_answer)

    def test_yahoo_repeated_pre_market_state_is_spoken_as_pre_market(self):
        provider = NvidiaMarketProvider()
        provider_data = provider.snapshot("NVDA", NOW)
        provider_data = stock_market.ProviderMarketData(
            price=provider_data.price,
            market_state="PREPRE",
            trailing_pe=provider_data.trailing_pe,
            bars=provider_data.bars,
            day_high=provider_data.day_high,
            day_low=provider_data.day_low,
        )

        class PreMarketProvider(NvidiaMarketProvider):
            def snapshot(self, symbol, captured_at):
                return provider_data

        intent = stock_market.parse_raw_investment_intent(
            "Help me buy 1 NVIDIA share."
        )
        brief = stock_market.build_investment_brief(
            intent, PreMarketProvider(), captured_at=NOW, funding_currency="SGD"
        )

        self.assertIn("Nasdaq is currently in pre-market.", stock_market.brief_response(brief))

    def test_actions_endpoint_uses_injected_real_market_provider(self):
        from apps.dimon_live.app import create_app

        class ModelMustNotRun:
            def extract(self, text):
                raise AssertionError("the model must not supply market facts")

        class RecordingSpeech:
            def __init__(self):
                self.results = []

            def queue_result(self, result, context):
                self.results.append(result)
                return "brief_audio"

        async def send(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(
                    "/api/actions",
                    json={
                        "request_id": "00000000-0000-4000-8000-000000000911",
                        "text": "I want to invest $2,000 in Cognizant Technology Solutions.",
                    },
                )

        with tempfile.TemporaryDirectory() as directory:
            speech = RecordingSpeech()
            app = create_app(
                Path(directory) / "demo.sqlite3",
                extractor=ModelMustNotRun(),
                stock_provider=FakeMarketProvider(),
                speech_service=speech,
            )
            app.state.database._clock = lambda: NOW

            response = asyncio.run(send(app))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["reason_code"], "REAL_STOCK_BRIEF")
        self.assertEqual(payload["audio_artifact_id"], "brief_audio")
        self.assertEqual(speech.results[0]["answer"], payload["answer"])
        self.assertNotEqual(app.state.stock_provider.search_thread_id, threading.get_ident())

    def test_voice_endpoint_runs_market_provider_off_realtime_event_loop(self):
        from apps.dimon_live.app import create_app

        class ModelMustNotRun:
            def extract(self, text):
                raise AssertionError("the model must not supply market facts")

        class DispatchingVoiceBridge:
            async def handle(self, websocket, context, dispatch):
                await websocket.accept()
                self.event_loop_thread_id = threading.get_ident()
                result = await dispatch(
                    "I want to invest $2,000 in Cognizant Technology Solutions.", {}
                )
                await websocket.send_json(result)
                await websocket.close()

        with tempfile.TemporaryDirectory() as directory:
            provider = FakeMarketProvider()
            bridge = DispatchingVoiceBridge()
            app = create_app(
                Path(directory) / "demo.sqlite3",
                extractor=ModelMustNotRun(),
                stock_provider=provider,
                voice_bridge=bridge,
            )
            app.state.database._clock = lambda: NOW

            with TestClient(app) as client:
                with client.websocket_connect("/api/voice/session") as websocket:
                    payload = websocket.receive_json()

        self.assertEqual(payload["reason_code"], "REAL_STOCK_BRIEF")
        self.assertNotEqual(provider.search_thread_id, bridge.event_loop_thread_id)

    def test_voice_endpoint_can_continue_a_persisted_conversation(self):
        from apps.dimon_live.app import create_app

        class ModelMustNotRun:
            def extract(self, text):
                raise AssertionError("the model must not supply market facts")

        class DispatchingVoiceBridge:
            async def handle(self, websocket, context, dispatch):
                await websocket.accept()
                result = await dispatch("buy now", {})
                await websocket.send_json(result)
                await websocket.close()

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                Path(directory) / "demo.sqlite3",
                extractor=ModelMustNotRun(),
                stock_provider=FakeMarketProvider(),
                voice_bridge=DispatchingVoiceBridge(),
            )
            app.state.database._clock = lambda: NOW
            with TestClient(app) as client:
                started = client.post(
                    "/api/actions",
                    json={
                        "request_id": "00000000-0000-4000-8000-000000000915",
                        "text": "I want to invest $500 in Cognizant Technology Solutions.",
                    },
                ).json()
                conversation_id = started["conversation"]["conversation_id"]
                with client.websocket_connect(
                    f"/api/voice/session?conversation_id={conversation_id}&expected_version=1"
                ) as websocket:
                    result = websocket.receive_json()

        self.assertEqual(result["conversation"]["status"], "READY_FOR_DRAFT")
        self.assertEqual(result["public_view"]["order_type"], "MARKET")

    def test_hands_free_market_order_receives_a_review_receipt(self):
        from apps.dimon_live.app import create_app

        class ModelMustNotRun:
            def extract(self, text):
                raise AssertionError("the model must not supply market facts")

        class NoSpeech:
            def queue_result(self, result, context):
                return None

        class DispatchingHandsFree:
            async def handle(self, websocket, context, dispatch):
                await websocket.accept()
                result = await dispatch("buy 2 nvidia shares at market", {})
                await websocket.send_json(result)
                await websocket.close()

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                Path(directory) / "demo.sqlite3",
                extractor=ModelMustNotRun(),
                stock_provider=NvidiaMarketProvider(),
                speech_service=NoSpeech(),
            )
            app.state.database._clock = lambda: NOW
            app.state.hands_free = DispatchingHandsFree()
            with TestClient(app) as client:
                with client.websocket_connect("/api/voice/hands-free") as websocket:
                    result = websocket.receive_json()

        self.assertEqual(result["outcome"], "ORDER_CONFIRMATION_REQUEST")
        self.assertEqual(result["reason_code"], "PAPER_EQUITY_APPROVAL_REQUIRED")
        self.assertEqual(result["paper_order"]["quantity"], 2)

    def test_hands_free_later_wake_continues_the_bound_investment_conversation(self):
        from apps.dimon_live.app import create_app

        class ModelMustNotRun:
            def extract(self, text):
                raise AssertionError("the model must not supply market facts")

        class NoSpeech:
            def queue_result(self, result, context):
                return None

        class DispatchingHandsFree:
            def __init__(self, conversation_id):
                self.conversation_id = conversation_id

            async def handle(self, websocket, context, dispatch):
                await websocket.accept()
                result = await dispatch("buy now", {
                    "hands_free": True,
                    "conversation_id": self.conversation_id,
                    "expected_version": 1,
                })
                await websocket.send_json(result)
                await websocket.close()

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                Path(directory) / "demo.sqlite3",
                extractor=ModelMustNotRun(),
                stock_provider=FakeMarketProvider(),
                speech_service=NoSpeech(),
            )
            app.state.database._clock = lambda: NOW
            with TestClient(app) as client:
                started = client.post(
                    "/api/actions",
                    json={
                        "request_id": "00000000-0000-4000-8000-000000000917",
                        "text": "I want to invest $500 in Cognizant Technology Solutions.",
                    },
                ).json()
                app.state.hands_free = DispatchingHandsFree(
                    started["conversation"]["conversation_id"]
                )
                with client.websocket_connect("/api/voice/hands-free") as websocket:
                    result = websocket.receive_json()

        self.assertEqual(result["conversation"]["status"], "READY_FOR_DRAFT")
        self.assertEqual(result["public_view"]["order_type"], "MARKET")
        self.assertNotEqual(result.get("category"), "payments")

    def test_split_market_order_child_receives_a_review_receipt(self):
        from apps.dimon_live.app import create_app

        class ModelMustNotRun:
            def extract(self, text):
                raise AssertionError("the model must not supply market facts")

        class NoSpeech:
            def queue_result(self, result, context):
                return None

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                Path(directory) / "demo.sqlite3",
                extractor=ModelMustNotRun(),
                stock_provider=NvidiaMarketProvider(),
                speech_service=NoSpeech(),
            )
            app.state.database._clock = lambda: NOW
            with TestClient(app) as client:
                response = client.post(
                    "/api/actions",
                    json={
                        "request_id": "00000000-0000-4000-8000-000000000916",
                        "text": "buy 2 nvidia shares at market and send John Smith S$5 from savings",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["outcome"], "MULTI_REQUEST")
        stock = next(step["result"] for step in result["steps"] if "nvidia" in step["text"].lower())
        self.assertEqual(stock["outcome"], "ORDER_CONFIRMATION_REQUEST")
        self.assertEqual(stock["paper_order"]["quantity"], 2)


if __name__ == "__main__":
    unittest.main()

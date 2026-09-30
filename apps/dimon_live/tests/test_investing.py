"""Investing: risk profile, orders confirmed by face, plans, suggestions."""

from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from apps.dimon_live import investing, market
from apps.dimon_live.database import Database, ReceiptRejected
from apps.dimon_live.model_adapter import ExtractedIntent
from apps.dimon_live.service import ActionService
from apps.dimon_live.tests.device_signer import SoftwareAuthenticator
from apps.dimon_live.tests.face_fake import FakeFaceEngine

BALANCED = {"horizon": 3, "drop": 3, "experience": 2, "goal": 3}      # risk 3


class CountingExtractor:
    def __init__(self):
        self.calls = []

    def extract(self, text):
        self.calls.append(text)
        intent = {"contract_version": 3, "kind": "BENEFICIARY_TRANSFER", "source_account_ref": None,
                  "source_evidence": None, "recipient_label": "Alicia Powell", "recipient_evidence": "Alicia Powell",
                  "currency": "SGD", "payment_reference": None, "reference_evidence": None,
                  "requested_execution_date": None}
        return ExtractedIntent(copy.deepcopy(intent), json.dumps(intent), 1.0)


class InvestingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "ledger.sqlite3", face_engine=FakeFaceEngine())
        self.database.initialize()
        self.device = SoftwareAuthenticator()
        self.device.enroll(self.database)
        self.extractor = CountingExtractor()
        self.service = ActionService(self.database, self.extractor)
        self.counter = 0

    def tearDown(self):
        self.temp_dir.cleanup()

    def ask(self, text):
        self.counter += 1
        return self.service.process(f"00000000-0000-4000-8000-{self.counter:012d}", text)

    def savings(self):
        return next(a["balance_minor"] for a in self.database.get_state()["accounts"] if a["account_id"] == "acct_savings_01")

    def order(self, text):
        result = self.ask(text)
        self.assertEqual(result["outcome"], "ORDER_CONFIRMATION_REQUEST", result)
        return result["order"]

    def approve(self, order, *, face=True, **kwargs):
        challenge = self.database.issue_order_challenge(order["order_id"], order["order_digest"], rp_id=self.device.rp_id)
        if face:
            assertion = self.device.sign(challenge)
        else:
            database, self.device.database = self.device.database, None
            assertion = self.device.sign(challenge)
            self.device.database = database
        return self.service.approve_order(
            order["order_id"], order["order_digest"], assertion=assertion, context=self.device.context,
            read_back_played=True, **kwargs,
        )

    def profile(self, answers=BALANCED):
        return self.database.save_investor_profile(answers, True)

    # ------------------------------------------------------------ profile
    def test_orders_wait_for_an_investor_profile(self):
        result = self.ask("buy 5 shares of straits telco")
        self.assertEqual((result["outcome"], result["reason_code"]), ("CLARIFICATION_REQUEST", "INVESTOR_PROFILE_REQUIRED"))
        self.assertEqual(len(result["questions"]), 4)
        with self.assertRaises(investing.InvestRejected):
            self.database.save_investor_profile(BALANCED, False)
        saved = self.profile()
        self.assertEqual((saved["risk_score"], saved["risk_label"], saved["horizon_years"]), (3, "Balanced", 10))

    def test_someone_who_would_sell_in_a_fall_is_never_rated_adventurous(self):
        self.assertEqual(investing.score_profile({"horizon": 4, "drop": 1, "experience": 4, "goal": 4})[0], 3)
        self.assertEqual(investing.score_profile({"horizon": 4, "drop": 4, "experience": 4, "goal": 4})[0], 5)

    # ------------------------------------------------------------ orders
    def test_buy_is_confirmed_by_face_and_settles_both_sides(self):
        self.profile()
        before = self.savings()
        order = self.order("buy 10 shares of straits telco")
        self.assertEqual((order["kind"], order["symbol"], order["quantity"]), ("BUY", "STR", "10"))
        self.assertEqual(self.savings(), before)                    # nothing moves on a draft
        result = self.approve(order)
        self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRADE", result)
        trade = result["trades"][0]
        self.assertEqual(self.savings(), before - trade["cash_minor"])
        self.assertLessEqual(trade["price_minor"], order["limit_price_minor"])
        self.assertIsNotNone(result["approval"]["face_scan_id"])
        holding = result["portfolio"]["holdings"][0]
        self.assertEqual((holding["symbol"], holding["quantity"]), ("STR", "10"))
        with closing(sqlite3.connect(self.database.path)) as connection:
            self.assertEqual(connection.execute("SELECT SUM(delta_minor) FROM investment_cash_entries").fetchone()[0], 0)
        self.assertEqual(self.extractor.calls, [])                  # the model never saw the order

    def test_buy_by_amount_and_sell_everything(self):
        self.profile()
        order = self.order("buy S$300 of the global world equity etf")
        self.assertLessEqual(order["value_minor"], 30_000)
        self.approve(order)
        after_buy = self.savings()
        sell = self.order("sell all my GWE")
        self.assertEqual(sell["quantity_e4"], order["quantity_e4"])
        result = self.approve(sell)
        self.assertEqual(result["outcome"], "EXECUTED_SYNTHETIC_TRADE")
        self.assertEqual(self.savings(), after_buy + result["trades"][0]["cash_minor"])
        self.assertEqual(result["portfolio"]["holdings"], [])

    def test_sells_are_limited_to_what_is_held(self):
        self.profile()
        self.assertEqual(self.ask("sell 5 shares of merlion bank")["reason_code"], "NOT_HELD")
        self.approve(self.order("buy 2 shares of merlion bank"))
        self.assertEqual(self.ask("sell 5 shares of merlion bank")["reason_code"], "SELL_MORE_THAN_HELD")

    def test_an_order_needs_the_face_as_well_as_the_device(self):
        self.profile()
        before = self.savings()
        result = self.approve(self.order("buy 3 shares of straits telco"), face=False)
        self.assertEqual((result["outcome"], result["reason_code"]), ("BLOCKED", "FACE_NOT_VERIFIED"))
        self.assertEqual(self.savings(), before)

    def test_riskier_than_profile_needs_an_explicit_acknowledgement(self):
        self.profile()
        order = self.order("buy 1 share of equator semiconductor")        # risk 5 vs profile 3
        self.assertTrue(order["risk_ack_required"])
        challenge = self.database.issue_order_challenge(order["order_id"], order["order_digest"], rp_id=self.device.rp_id)
        self.assertTrue(challenge["review"]["requires_unusual_confirmation"])
        self.assertIn("riskier than my profile", challenge["review"]["confirmation_text"])
        refused = self.service.approve_order(order["order_id"], order["order_digest"],
                                             assertion=self.device.sign(challenge), context=self.device.context,
                                             read_back_played=True)
        self.assertEqual(refused["reason_code"], "RISK_ACKNOWLEDGEMENT_REQUIRED")
        self.assertEqual(self.approve(order, risk_acknowledged=True)["outcome"], "EXECUTED_SYNTHETIC_TRADE")

    def test_larger_orders_must_be_read_back(self):
        self.profile()
        order = self.order("buy S$250 of straits telco")
        self.assertTrue(order["requires_read_back"])
        challenge = self.database.issue_order_challenge(order["order_id"], order["order_digest"], rp_id=self.device.rp_id)
        self.assertIn("Straits Telco", challenge["review"]["read_back"]["text"])
        refused = self.service.approve_order(order["order_id"], order["order_digest"],
                                             assertion=self.device.sign(challenge), context=self.device.context)
        self.assertEqual(refused["reason_code"], "READ_BACK_REQUIRED")

    def test_price_protection_refuses_a_fill_beyond_the_signed_limit(self):
        self.profile()
        before = self.savings()
        order = self.order("buy 10 shares of straits telco")
        spike = order["limit_price_minor"] + 5
        with mock.patch.object(investing.market, "price_minor", return_value=spike):
            result = self.approve(order)
        self.assertEqual(result["reason_code"], "PRICE_MOVED_BEYOND_LIMIT")
        self.assertEqual(self.savings(), before)
        self.assertEqual(self.database.investing_overview()["holdings"], [])

    def test_order_limits(self):
        self.profile()
        self.assertEqual(self.ask("buy S$1500 of bonds")["reason_code"], "INSUFFICIENT_FUNDS")
        self.assertEqual(self.ask("buy S$6000 of bonds")["reason_code"], "ORDER_LIMIT_EXCEEDED")
        self.assertEqual(self.ask("buy S$1 of merlion bank")["reason_code"], "AMOUNT_BELOW_ONE_UNIT")
        self.assertEqual(self.ask("buy straits telco")["reason_code"], "MISSING_QUANTITY")

    def test_a_tampered_order_digest_is_refused(self):
        self.profile()
        order = self.order("buy 2 shares of straits telco")
        with self.assertRaises(ReceiptRejected) as caught:
            self.database.issue_order_challenge(order["order_id"], "0" * 64, rp_id=self.device.rp_id)
        self.assertEqual(caught.exception.reason_code, "RECEIPT_DIGEST_MISMATCH")

    # ------------------------------------------------------------ plans
    def test_monthly_plan_invests_now_and_again_each_month_until_cancelled(self):
        self.profile()
        order = self.order("invest S$200 a month in the balanced growth plan")
        self.assertEqual(order["kind"], "PLAN")
        self.assertEqual([a["symbol"] for a in order["plan"]["allocations"]], ["GWE", "SDX", "SGB", "HCA"])
        result = self.approve(order)
        self.assertEqual(result["outcome"], "EXECUTED_INVESTMENT_PLAN", result)
        self.assertEqual(len(result["trades"]), 4)
        plan_id = result["plan"]["plan_id"]
        after_first = self.savings()
        later = datetime.now(timezone.utc) + timedelta(days=32)
        overview = self.database.investing_overview(now=later)
        self.assertEqual([run["status"] for run in overview["plan_runs"]], ["INVESTED"])
        self.assertLess(self.savings(), after_first)
        self.database.cancel_investment_plan(plan_id)
        self.assertEqual(self.database.investing_overview(now=later + timedelta(days=40))["plan_runs"], [])

    def test_plan_amount_bounds(self):
        self.profile()
        self.assertEqual(self.ask("invest S$10 a month in GWE")["reason_code"], "PLAN_AMOUNT_OUT_OF_RANGE")
        self.assertEqual(self.ask("invest a month in GWE monthly")["reason_code"], "MISSING_MONTHLY_AMOUNT")

    # ------------------------------------------------------------ suggestions
    def test_trending_and_plans_are_rule_based_and_show_risks(self):
        self.profile()
        trending = self.ask("what's trending")
        self.assertEqual(trending["outcome"], "ANSWERED")
        picks = trending["investing"]["trending"]
        self.assertTrue(all(p["change_1m_pct"] > 0 and p["change_3m_pct"] > 0 for p in picks))
        self.assertTrue(all(p["strengths"] and p["risks"] and p["fit_note"] for p in picks))
        self.assertTrue(all(p["risk_level"] <= 3 for p in trending["investing"]["income"]))
        plans = self.ask("suggest a long-term plan, S$500 a month for 20 years")
        best = next(p for p in plans["investing"]["plans"] if p["best_fit"])
        self.assertEqual((best["key"], best["monthly_minor"], best["years"]), ("BALANCED_GROWTH", 50_000, 20))
        projection = best["projection_minor"]
        self.assertLess(projection["cautious"], projection["middle"])
        self.assertLess(projection["middle"], projection["strong"])
        self.assertEqual(best["contributed_minor"], 50_000 * 12 * 20)
        text = json.dumps([trending, plans]).lower()
        self.assertNotIn("guarantee", text.replace("does not guarantee", ""))
        self.assertIn("not financial advice", text)
        self.assertEqual(self.extractor.calls, [])

    def test_short_horizons_are_steered_to_capital_protection(self):
        suggestions = investing.plan_suggestions(5, 2)
        self.assertEqual(next(p for p in suggestions["plans"] if p["best_fit"])["key"], "CAPITAL_KEEPER")

    def test_payments_are_not_mistaken_for_investing(self):
        self.assertIsNone(investing.understand("Send John Smith S$50 from savings."))
        self.assertIsNone(investing.understand("What's my balance?"))
        self.assertEqual(self.ask("Invest S$500, guaranteed returns next week")["reason_code"], "SCAM_SUSPECTED")

    def test_market_prices_are_deterministic(self):
        moment = datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(market.price_minor("MLB", moment), market.price_minor("MLB", moment))
        self.assertTrue(all(market.price_minor(i.symbol, moment) > 0 for i in market.CATALOG))


if __name__ == "__main__":
    unittest.main()

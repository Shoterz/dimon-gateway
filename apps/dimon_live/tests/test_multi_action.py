import json
from pathlib import Path
import tempfile
import unittest


class MultiActionContractTests(unittest.TestCase):
    def setUp(self):
        from apps.dimon_live.multi_action import validate_multi_action_intent

        self.validate = validate_multi_action_intent
        self.text = (
            "Pay my water bill, send Alicia forty five dollars for dinner, "
            "and buy two shares of NVIDIA at market."
        )
        self.intent = {
            "contract_version": 1,
            "kind": "MULTI_ACTION",
            "items": [
                {"kind": "BILL_PAYMENT", "evidence": "Pay my water bill"},
                {
                    "kind": "BENEFICIARY_TRANSFER",
                    "evidence": "send Alicia forty five dollars for dinner",
                },
                {
                    "kind": "STOCK_INVESTMENT",
                    "evidence": "buy two shares of NVIDIA at market",
                },
            ],
        }

    def test_multi_action_gate_keeps_single_bill_and_transfer_on_existing_routes(self):
        from apps.dimon_live.multi_action import looks_like_multi_action_request

        self.assertFalse(looks_like_multi_action_request(
            "Pay my Singapore Power bill $120 from savings."
        ))
        self.assertFalse(looks_like_multi_action_request(
            "Send Alicia Powell S$45 for dinner."
        ))
        self.assertTrue(looks_like_multi_action_request(
            "Pay my PUB Water bill and send Alicia S$45 for dinner."
        ))
        self.assertTrue(looks_like_multi_action_request(
            "Send Alicia S$45 and send John S$20."
        ))

    def test_multi_action_contract_preserves_item_order(self):
        intent = self.validate(json.dumps(self.intent), text=self.text)
        self.assertEqual(
            [item["kind"] for item in intent["items"]],
            ["BILL_PAYMENT", "BENEFICIARY_TRANSFER", "STOCK_INVESTMENT"],
        )

    def test_multi_action_contract_accepts_two_or_three_supported_items(self):
        two = dict(self.intent, items=self.intent["items"][:2])
        three = self.intent
        self.assertEqual(len(self.validate(json.dumps(two), text=self.text)["items"]), 2)
        self.assertEqual(len(self.validate(json.dumps(three), text=self.text)["items"]), 3)

    def test_multi_action_contract_rejects_extra_amount_and_invalid_evidence_fields(self):
        with self.assertRaises(ValueError):
            self.validate(
                json.dumps(dict(self.intent, amount_minor=4500)), text=self.text
            )
        invalid = dict(self.intent, items=[
            {"kind": "BENEFICIARY_TRANSFER", "evidence": "send Alicia", "amount": "S$45"},
            self.intent["items"][1],
        ])
        with self.assertRaises(ValueError):
            self.validate(json.dumps(invalid), text=self.text)

    def test_qwen_multi_action_extractor_uses_strict_ordered_span_contract(self):
        from apps.dimon_live.model_adapter import QwenMultiActionExtractor

        calls = []

        def transport(url, payload, timeout):
            calls.append((url, payload, timeout))
            return {"choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps(self.intent)},
            }]}

        extracted = QwenMultiActionExtractor(transport=transport).extract(self.text)
        self.assertEqual(extracted.intent["items"], self.intent["items"])
        _, payload, _ = calls[0]
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        schema = payload["response_format"]["json_schema"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["items"]["maxItems"], 3)

    def test_prepare_multi_action_biller_transfer_and_stock(self):
        from apps.dimon_live.multi_action import prepare_multi_action

        intent = self.validate(json.dumps(self.intent), text=self.text)
        prepared = prepare_multi_action(
            self.text,
            intent,
            bill_preparer=lambda evidence: {"biller": "PUB Water", "source": evidence},
            transfer_preparer=lambda evidence: {"recipient": "Alicia", "source": evidence},
            stock_preparer=lambda evidence: {"symbol": "NVDA", "source": evidence},
        )
        self.assertIsNone(prepared.reason_code)
        self.assertEqual([item.kind for item in prepared.items], [
            "BILL_PAYMENT", "BENEFICIARY_TRANSFER", "STOCK_INVESTMENT"
        ])

    def test_prepare_multi_action_binds_each_amount_to_its_action(self):
        from apps.dimon_live.multi_action import prepare_multi_action

        intent = self.validate(json.dumps(self.intent), text=self.text)
        seen = []

        def transfer(evidence):
            seen.append(evidence)
            return {"amount_evidence": "forty five dollars"}

        result = prepare_multi_action(
            self.text, intent,
            bill_preparer=lambda evidence: {"amount": "statement balance"},
            transfer_preparer=transfer,
            stock_preparer=lambda evidence: {"quantity_evidence": "two shares"},
        )
        self.assertEqual(seen, ["send Alicia forty five dollars for dinner"])
        self.assertIsNone(result.reason_code)

    def test_prepare_multi_action_keeps_explicit_market_order(self):
        from apps.dimon_live.multi_action import prepare_multi_action

        intent = self.validate(json.dumps(self.intent), text=self.text)
        result = prepare_multi_action(
            self.text, intent,
            bill_preparer=lambda evidence: {"kind": "bill"},
            transfer_preparer=lambda evidence: {"kind": "transfer"},
            stock_preparer=lambda evidence: {"order_style": "MARKET" if "at market" in evidence else None},
        )
        self.assertEqual(result.items[-1].intent["order_style"], "MARKET")

    def test_prepare_multi_action_collects_all_unresolved_items_without_drafts(self):
        from apps.dimon_live.multi_action import UnresolvedAction, prepare_multi_action

        intent = self.validate(json.dumps(self.intent), text=self.text)
        result = prepare_multi_action(
            self.text, intent,
            bill_preparer=lambda evidence: {"kind": "bill"},
            transfer_preparer=lambda evidence: UnresolvedAction(
                "recipient", "Who should receive S$45?", evidence,
            ),
            stock_preparer=lambda evidence: UnresolvedAction(
                "instrument", "Did you mean NVIDIA?", "VDS", "NVIDIA",
            ),
        )
        self.assertEqual(result.items, ())
        self.assertEqual(result.reason_code, "MULTI_ACTION_ITEM_UNRESOLVED")
        self.assertEqual([issue.field for issue in result.issues], ["recipient", "instrument"])
        self.assertEqual(result.resolved_items[0].kind, "BILL_PAYMENT")
        self.assertEqual(
            result.clarification,
            "Who should receive S$45, and did you mean NVIDIA? Nothing has been prepared.",
        )

    def test_prepare_multi_action_still_evaluates_items_after_first_failure(self):
        from apps.dimon_live.multi_action import UnresolvedAction, prepare_multi_action

        intent = self.validate(json.dumps(self.intent), text=self.text)
        calls = []
        result = prepare_multi_action(
            self.text, intent,
            bill_preparer=lambda evidence: {"kind": "bill"},
            transfer_preparer=lambda evidence: UnresolvedAction(
                "recipient", "Who should receive S$45?", evidence,
            ),
            stock_preparer=lambda evidence: calls.append(evidence) or {"kind": "stock"},
        )
        self.assertEqual(calls, ["buy two shares of NVIDIA at market"])
        self.assertEqual(result.items, ())
        self.assertEqual(len(result.resolved_items), 2)

    def test_failed_stock_parser_requests_stock_and_share_details(self):
        from apps.dimon_live.model_adapter import InvalidModelOutput
        from apps.dimon_live.multi_action import prepare_multi_action

        intent = self.validate(json.dumps(self.intent), text=self.text)

        def invalid_stock(_evidence):
            raise InvalidModelOutput("private parser detail")

        result = prepare_multi_action(
            self.text, intent,
            bill_preparer=lambda evidence: {"kind": "bill"},
            transfer_preparer=lambda evidence: {"kind": "transfer"},
            stock_preparer=invalid_stock,
        )

        self.assertEqual(result.issues[0].field, "stock_request")
        self.assertEqual(
            result.issues[0].question,
            "Which stock and how many shares would you like to buy?",
        )
        self.assertNotIn("private parser detail", result.clarification)


class MultiActionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        from apps.dimon_live.database import ActionAttempt, Database

        self.db = Database(Path(self.temp.name) / "ledger.sqlite3")
        self.db.initialize()
        self.db.record_attempt(ActionAttempt(
            request_id="request-1", original_text="multi action request",
            raw_model_output=None, validated_intent=None, outcome="REVIEW",
            reason_code=None, response={"outcome": "REVIEW"},
        ))

    def tearDown(self):
        self.temp.cleanup()

    def children(self):
        return [
            {"kind": "BILL_PAYMENT", "child_type": "receipt", "child_id": "bill-r"},
            {"kind": "BENEFICIARY_TRANSFER", "child_type": "receipt", "child_id": "transfer-r"},
            {"kind": "STOCK_INVESTMENT", "child_type": "paper_order", "child_id": "order-r"},
        ]

    def test_multi_action_group_persists_all_children_in_order(self):
        group = self.db.create_pending_multi_action_group(
            "request-1", "multi action request", self.children(),
            payment_auth_amount_minor=19_500,
        )
        self.assertEqual([item["kind"] for item in group["items"]], [
            "BILL_PAYMENT", "BENEFICIARY_TRANSFER", "STOCK_INVESTMENT"
        ])
        self.assertEqual(group["payment_auth_amount_minor"], 19_500)

    def test_multi_action_group_creation_rolls_back_all_children_on_failure(self):
        children = self.children()
        children[1]["child_id"] = children[0]["child_id"]
        with self.assertRaises(Exception):
            self.db.create_pending_multi_action_group(
                "request-1", "multi action request", children,
                payment_auth_amount_minor=100,
            )
        with self.db._connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM multi_action_groups"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM multi_action_items"
            ).fetchone()[0], 0)

    def test_multi_action_group_updates_only_approved_child(self):
        group = self.db.create_pending_multi_action_group(
            "request-1", "multi action request", self.children(),
            payment_auth_amount_minor=100,
        )
        first, second, _ = group["items"]
        updated = self.db.update_multi_action_item_status(
            group["group_id"], first["item_id"], "APPROVED"
        )
        self.assertEqual(updated["items"][0]["status"], "APPROVED")
        self.assertEqual(updated["items"][1]["status"], "AWAITING_APPROVAL")

    def test_multi_action_stock_order_binding_refreshes_group_review_payload(self):
        children = self.children()
        children[2]["child_type"] = "conversation"
        group = self.db.create_pending_multi_action_group(
            "request-1", "multi action request", children,
            payment_auth_amount_minor=100,
        )
        self.db.bind_multi_action_child_reference(
            "conversation", "order-r", "paper_order", "order-r",
            result={"outcome": "ORDER_CONFIRMATION_REQUEST", "paper_order": {"order_id": "order-r"}},
        )
        latest = self.db.get_multi_action_group(group["group_id"], user_id="demo_user_01")
        stock = latest["items"][2]
        self.assertEqual(stock["status"], "AWAITING_APPROVAL")
        self.assertEqual(stock["result"]["paper_order"]["order_id"], "order-r")

    def test_late_group_failure_supersedes_pending_payment_drafts(self):
        from apps.dimon_live.receipts import DEMO_USER_ID, ReceiptDraft

        source = next(account for account in self.db.get_state()["accounts"] if account["is_user_source"])
        with self.db._connect() as connection:
            beneficiary = connection.execute(
                "SELECT beneficiary_id, display_name FROM beneficiaries WHERE normalized_alias = 'john smith'"
            ).fetchone()
        draft = ReceiptDraft(
            request_id="multi-child-1", original_text="Send John Smith S$10",
            user_id=DEMO_USER_ID, source_account_id=source["account_id"],
            beneficiary_id=beneficiary["beneficiary_id"],
            recipient_display_name=beneficiary["display_name"],
            amount_minor=1000, currency="SGD",
        )
        response = self.db.create_pending_receipts(
            (draft,), raw_model_output="{}", validated_intent={"kind": "BENEFICIARY_TRANSFER"},
            reason_code="ACTION_CONFIRMATION_REQUIRED",
            response_base={"outcome": "TRANSFER_CONFIRMATION_REQUEST"},
        )
        receipt_id = response["receipt_choices"][0]["receipt_id"]
        self.db.supersede_multi_action_drafts(["multi-child-1"])
        self.assertEqual(self.db.get_receipt(receipt_id)["status"], "SUPERSEDED")

    def test_filled_paper_order_completes_only_its_group_item(self):
        from apps.dimon_live.app import multi_action_status_for_result

        self.assertEqual(multi_action_status_for_result({
            "outcome": "PAPER_ORDER_FILLED",
            "paper_order_result": {"status": "FILLED"},
        }), "APPROVED")

    def test_multi_action_group_uses_aggregate_bill_and_transfer_auth_tier(self):
        from apps.dimon_live.risk_policy import authentication_for_amount_minor

        total = 15_000 + 5_000
        group = self.db.create_pending_multi_action_group(
            "request-1", "multi action request", self.children(),
            payment_auth_amount_minor=total,
        )
        self.assertEqual(group["payment_auth_amount_minor"], total)
        self.assertEqual(
            authentication_for_amount_minor(total)["authentication_tier"],
            "SINGPASS_AND_FACE",
        )

    def test_grouped_clarification_is_expiring_scoped_and_non_executable(self):
        clarification = self.db.create_multi_action_clarification(
            request_id="request-1",
            original_text="pay water, send my friend $10, buy VDS",
            intent={"kind": "MULTI_ACTION", "items": []},
            resolved_items=[{"kind": "BILL_PAYMENT", "status": "UNDERSTOOD"}],
            issues=[{"field": "recipient"}, {"field": "instrument"}],
        )
        loaded = self.db.get_multi_action_clarification(
            clarification["clarification_id"]
        )
        self.assertEqual(len(loaded["issues"]), 2)
        with self.db._connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM review_receipts"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM paper_equity_orders"
            ).fetchone()[0], 0)

        with self.assertRaises(Exception):
            self.db.get_multi_action_clarification(
                clarification["clarification_id"], user_id="someone-else"
            )
        self.db.finish_multi_action_clarification(
            clarification["clarification_id"], "reply-1", cancelled=True
        )
        with self.assertRaises(Exception):
            self.db.get_multi_action_clarification(clarification["clarification_id"])


class MultiActionRoutingTests(unittest.TestCase):
    def test_missing_stock_size_keeps_the_grounded_instrument(self):
        from apps.dimon_live.database import Database
        from apps.dimon_live.model_adapter import ExtractedIntent
        from apps.dimon_live.service import ActionService

        text = "Buy Nvidia stock and pay my water bill."

        class MultiExtractor:
            def extract(self, _text):
                intent = {
                    "contract_version": 1,
                    "kind": "MULTI_ACTION",
                    "items": [
                        {"kind": "STOCK_INVESTMENT", "evidence": "Buy Nvidia stock"},
                        {"kind": "BILL_PAYMENT", "evidence": "pay my water bill"},
                    ],
                }
                return ExtractedIntent(intent, json.dumps(intent), 1.0)

        class BillExtractor:
            def extract(self, _text):
                intent = {
                    "contract_version": 1,
                    "kind": "BILL_PAYMENT",
                    "biller_label": "PUB Water",
                    "biller_evidence": "water",
                    "amount_evidence": None,
                    "currency": None,
                    "currency_evidence": None,
                    "source_account_label": None,
                    "source_account_evidence": None,
                    "requested_execution_date": None,
                    "execution_date_evidence": None,
                }
                return ExtractedIntent(intent, json.dumps(intent), 1.0)

        class StockExtractor:
            def extract(self, _text):
                intent = {
                    "contract_version": 2,
                    "kind": "STOCK_INVESTMENT",
                    "instrument_label": "Nvidia",
                    "instrument_evidence": "Nvidia",
                    "objective": "GENERAL_ACQUISITION",
                    "size_kind": "UNKNOWN",
                    "quantity": None,
                    "quantity_evidence": None,
                    "allocation_evidence": None,
                }
                return ExtractedIntent(intent, json.dumps(intent), 1.0)

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "ledger.sqlite3")
            database.initialize()
            bill_extractor = BillExtractor()
            service = ActionService(
                database,
                bill_extractor,
                bill_payment_extractor=bill_extractor,
                stock_extractor=StockExtractor(),
                multi_action_extractor=MultiExtractor(),
            )
            result = service.process("missing-stock-size", text)

        self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(
            result["answer"],
            "How many shares of Nvidia would you like, or what amount would you like to invest? "
            "Nothing has been prepared.",
        )
        self.assertEqual(result["multi_action_clarification"]["issues"], [{
            "kind": "STOCK_INVESTMENT",
            "field": "stock_size",
            "question": "How many shares of Nvidia would you like, or what amount would you like to invest?",
            "evidence": "Nvidia",
            "suggested_value": "Nvidia",
            "status": "NEEDS_CLARIFICATION",
        }])
        self.assertEqual(
            result["multi_action_clarification"]["resolved_items"][0]["kind"],
            "BILL_PAYMENT",
        )

    def test_stock_size_reply_is_added_to_the_original_grouped_request(self):
        from apps.dimon_live.service import ActionService

        class FakeDatabase:
            def __init__(self):
                self.finished = []

            def get_multi_action_clarification(self, clarification_id, user_id):
                return {
                    "original_text": "Buy Nvidia stock and pay my water bill.",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "resolved_items": [{"kind": "BILL_PAYMENT"}],
                    "issues": [{
                        "field": "stock_size",
                        "evidence": "Nvidia",
                        "question": (
                            "How many shares of Nvidia would you like, or what amount would you like to invest?"
                        ),
                        "suggested_value": "Nvidia",
                    }],
                }

            def list_saved_beneficiaries(self):
                return ()

            def finish_multi_action_clarification(self, clarification_id, request_id, cancelled=False):
                self.finished.append((clarification_id, request_id, cancelled))

            def get_state(self):
                return {}

        service = ActionService.__new__(ActionService)
        service.database = FakeDatabase()
        captured = []
        service._process_multi_action = lambda request_id, text, trace: (
            captured.append(text) or {"outcome": "MULTI_ACTION_CONFIRMATION_REQUEST"}
        )

        result = service._respond_to_multi_action_clarification(
            "multi_clarification_1",
            "reply-1",
            "one share",
            user_id="demo_user_01",
        )

        self.assertEqual(result["outcome"], "MULTI_ACTION_CONFIRMATION_REQUEST")
        self.assertEqual(captured, [
            "Buy one share of Nvidia stock and pay my water bill."
        ])
        self.assertEqual(service.database.finished, [
            ("multi_clarification_1", "reply-1", False)
        ])

    def test_targeted_reply_fills_recipient_and_confirms_stock_before_preparing(self):
        from apps.dimon_live.service import ActionService

        class FakeDatabase:
            def __init__(self):
                self.finished = []

            def get_multi_action_clarification(self, clarification_id, user_id):
                return {
                    "original_text": "pay my water bill and pay my friend $10 and buy one VDS stock",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "resolved_items": [{"kind": "BILL_PAYMENT"}],
                    "issues": [
                        {"field": "recipient", "evidence": "my friend", "question": "Who should receive S$10?"},
                        {"field": "instrument", "evidence": "VDS", "question": "Did you mean NVIDIA?", "suggested_value": "NVIDIA"},
                    ],
                }

            def list_saved_beneficiaries(self):
                return ({"display_name": "John Tan"}, {"display_name": "Alicia Powell"})

            def finish_multi_action_clarification(self, clarification_id, request_id, cancelled=False):
                self.finished.append((clarification_id, request_id, cancelled))

            def get_state(self):
                return {}

        service = ActionService.__new__(ActionService)
        service.database = FakeDatabase()
        captured = []
        service._process_multi_action = lambda request_id, text, trace: (
            captured.append(text) or {"outcome": "MULTI_ACTION_CONFIRMATION_REQUEST"}
        )
        result = service._respond_to_multi_action_clarification(
            "multi_clarification_1", "reply-1", "John Tan, and yes, NVIDIA",
            user_id="demo_user_01",
        )
        self.assertEqual(result["outcome"], "MULTI_ACTION_CONFIRMATION_REQUEST")
        self.assertEqual(captured, [
            "pay my water bill and pay John Tan $10 and buy one NVIDIA stock"
        ])
        self.assertEqual(service.database.finished, [
            ("multi_clarification_1", "reply-1", False)
        ])

    def test_targeted_reply_does_not_guess_an_unconfirmed_stock(self):
        from apps.dimon_live.service import ActionService

        class FakeDatabase:
            def get_multi_action_clarification(self, clarification_id, user_id):
                return {
                    "original_text": "pay my friend $10 and buy one VDS stock",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "resolved_items": [],
                    "issues": [
                        {"field": "recipient", "evidence": "my friend", "question": "Who should receive S$10?"},
                        {"field": "instrument", "evidence": "VDS", "question": "Did you mean NVIDIA?", "suggested_value": "NVIDIA"},
                    ],
                }

            def list_saved_beneficiaries(self):
                return ({"display_name": "John Tan"},)

            def get_state(self):
                return {}

        service = ActionService.__new__(ActionService)
        service.database = FakeDatabase()
        result = service._respond_to_multi_action_clarification(
            "multi_clarification_1", "reply-1", "John Tan",
            user_id="demo_user_01",
        )
        self.assertEqual(result["outcome"], "CLARIFICATION_REQUEST")
        self.assertEqual(result["answer"], "Did you mean NVIDIA? Nothing has been prepared.")

    def test_three_domain_request_routes_to_group_without_execution(self):
        from apps.dimon_live.database import Database
        from apps.dimon_live.model_adapter import ExtractedIntent
        from apps.dimon_live.service import ActionService
        from apps.dimon_live.tests.test_stock_market import NOW, NvidiaMarketProvider

        text = (
            "Please pay my popwater bill, send Alicia Powell forty five dollars for dining, "
            "and buy two shares of NVIDIA at market using my investment account."
        )
        spans = [
            ("BILL_PAYMENT", "pay my popwater bill"),
            ("BENEFICIARY_TRANSFER", "send Alicia Powell forty five dollars for dining"),
            ("STOCK_INVESTMENT", "buy two shares of NVIDIA at market using my investment account"),
        ]

        class MultiExtractor:
            def extract(self, _text):
                intent = {"contract_version": 1, "kind": "MULTI_ACTION", "items": [
                    {"kind": kind, "evidence": evidence} for kind, evidence in spans
                ]}
                return ExtractedIntent(intent, json.dumps(intent), 1.0)

        class DomainExtractor:
            def __init__(self):
                self.calls = []

            def extract(self, text):
                self.calls.append(text)
                if "popwater" in text.casefold():
                    intent = {
                        "contract_version": 1, "kind": "BILL_PAYMENT",
                        "biller_label": "PUB Water", "biller_evidence": "popwater",
                        "amount_evidence": None, "currency": None,
                        "currency_evidence": None, "source_account_label": None,
                        "source_account_evidence": None, "requested_execution_date": None,
                        "execution_date_evidence": None,
                    }
                else:
                    intent = {
                        "contract_version": 3, "kind": "BENEFICIARY_TRANSFER",
                        "source_account_ref": None, "source_evidence": None,
                        "recipient_label": "Alicia Powell", "recipient_evidence": "Alicia Powell",
                        "currency": "SGD", "payment_reference": "dining",
                        "reference_evidence": "for dining", "requested_execution_date": None,
                    }
                return ExtractedIntent(intent, json.dumps(intent), 1.0)

        class StockExtractor:
            def __init__(self):
                self.calls = []

            def extract(self, text):
                self.calls.append(text)
                intent = {
                    "contract_version": 2, "kind": "STOCK_INVESTMENT",
                    "instrument_label": "NVIDIA", "instrument_evidence": "NVIDIA",
                    "objective": "GENERAL_ACQUISITION", "size_kind": "QUANTITY",
                    "quantity": 2, "quantity_evidence": "two shares",
                    "allocation_evidence": None,
                }
                return ExtractedIntent(intent, json.dumps(intent), 1.0)

        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "ledger.sqlite3", clock=lambda: NOW)
            db.initialize()
            domain_extractor = DomainExtractor()
            stock_extractor = StockExtractor()
            service = ActionService(
                db, domain_extractor, bill_payment_extractor=domain_extractor,
                stock_extractor=stock_extractor, multi_action_extractor=MultiExtractor(),
                stock_provider=NvidiaMarketProvider(),
            )
            result = service.process("multi-request-1", text)
            state = db.get_state()
            payment_receipts = [
                child["result"]["receipt_choices"][0]["receipt_id"]
                for child in result["items"][:2]
            ]
            with db._connect() as connection:
                auth_totals = [
                    Database._receipt_authentication_total_minor(
                        connection, "single", receipt_id
                    )
                    for receipt_id in payment_receipts
                ]

        self.assertEqual(result["outcome"], "MULTI_ACTION_CONFIRMATION_REQUEST")
        self.assertEqual([item["kind"] for item in result["items"]], [
            "BILL_PAYMENT", "BENEFICIARY_TRANSFER", "STOCK_INVESTMENT"
        ])
        self.assertFalse(result["payment_authentication"]["requires_singpass"])
        self.assertEqual(len(result["multi_action_group"]["items"]), 3)
        self.assertEqual(auth_totals, [7_780, 7_780])
        self.assertEqual(domain_extractor.calls.count(spans[0][1]), 1)
        self.assertEqual(domain_extractor.calls.count(spans[1][1]), 1)
        self.assertEqual(stock_extractor.calls, [spans[2][1]])
        self.assertEqual(state["transactions"], [])

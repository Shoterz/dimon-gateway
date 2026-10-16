import json
import unittest


GOLDEN_REQUEST = "Send John Smith S$50 from savings."
GOLDEN_INTENT = {
    "contract_version": 3,
    "kind": "BENEFICIARY_TRANSFER",
    "source_account_ref": "savings",
    "source_evidence": "savings",
    "recipient_label": "John Smith",
    "recipient_evidence": "John Smith",
    "currency": "SGD",
    "payment_reference": None,
    "reference_evidence": None,
    "requested_execution_date": None,
}

MISSING_AMOUNT_INTENT = {
    "contract_version": 3,
    "kind": "BENEFICIARY_TRANSFER",
    "source_account_ref": None,
    "source_evidence": None,
    "recipient_label": "Alicia Powell",
    "recipient_evidence": "Alicia Powell",
    "currency": "SGD",
    "payment_reference": "flowers",
    "reference_evidence": "for flowers",
    "requested_execution_date": None,
}

PAYNOW_INTENT = {
    "contract_version": 3,
    "kind": "PAYNOW_MOBILE_TRANSFER",
    "source_account_ref": None,
    "source_evidence": None,
    "mobile_proxy": "86952933",
    "proxy_evidence": "8695 2933",
    "currency": "SGD",
    "payment_reference": "flowers",
    "reference_evidence": "for flowers",
    "requested_execution_date": None,
}

BATCH_INTENT = {
    "contract_version": 4,
    "kind": "BENEFICIARY_TRANSFER_BATCH",
    "source_account_ref": None,
    "source_evidence": None,
    "items": [
        {
            "recipient_label": "John Smith",
            "recipient_evidence": "John Smith",
        },
        {
            "recipient_label": "Alicia Powell",
            "recipient_evidence": "Alicia Powell",
        },
    ],
    "currency": "SGD",
    "requested_execution_date": None,
}


class QwenTransferExtractorTests(unittest.TestCase):
    def response_transport(self, content, *, finish_reason="stop", calls=None):
        def transport(url, payload, timeout):
            if calls is not None:
                calls.append((url, payload, timeout))
            return {
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {"role": "assistant", "content": content},
                    }
                ]
            }

        return transport

    def success_transport(self, calls):
        return self.response_transport(json.dumps(GOLDEN_INTENT), calls=calls)

    def test_extractor_sends_strict_schema_without_tools_and_disables_thinking(self):
        from apps.dimon_live.model_adapter import QwenTransferExtractor

        calls = []
        extractor = QwenTransferExtractor(transport=self.success_transport(calls))

        extractor.extract(GOLDEN_REQUEST)

        url, payload, timeout = calls[0]
        self.assertEqual(url, "http://127.0.0.1:8080/v1/chat/completions")
        self.assertEqual(timeout, 120)
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        variants = payload["response_format"]["json_schema"]["schema"]["oneOf"]
        self.assertTrue(all(not variant["additionalProperties"] for variant in variants))
        self.assertTrue(
            all("amount_minor" not in variant["properties"] for variant in variants)
        )
        self.assertTrue(
            all("amount_evidence" not in variant["properties"] for variant in variants)
        )
        system_prompt = payload["messages"][0]["content"]
        self.assertIn("Otherwise both must be null", system_prompt)
        self.assertIn("It is never a source account", system_prompt)
        self.assertIn("Deterministic server code derives money", system_prompt)

    def test_extractor_returns_the_validated_saved_variant(self):
        from apps.dimon_live.model_adapter import QwenTransferExtractor

        extractor = QwenTransferExtractor(transport=self.success_transport([]))

        result = extractor.extract(GOLDEN_REQUEST)

        self.assertEqual(result.intent, GOLDEN_INTENT)
        self.assertEqual(json.loads(result.raw_output), GOLDEN_INTENT)
        self.assertGreaterEqual(result.latency_ms, 0)

    def test_validation_accepts_amount_free_saved_and_paynow_variants(self):
        from apps.dimon_live.model_adapter import validate_intent

        self.assertEqual(
            validate_intent(json.dumps(MISSING_AMOUNT_INTENT)),
            MISSING_AMOUNT_INTENT,
        )
        self.assertEqual(validate_intent(json.dumps(PAYNOW_INTENT)), PAYNOW_INTENT)

    def test_validate_intent_accepts_bounded_beneficiary_batch(self):
        from apps.dimon_live.model_adapter import validate_intent

        self.assertEqual(validate_intent(json.dumps(BATCH_INTENT)), BATCH_INTENT)

    def test_validate_intent_rejects_invalid_batch_shapes(self):
        from apps.dimon_live.model_adapter import InvalidModelOutput, validate_intent

        cases = (
            {**BATCH_INTENT, "items": BATCH_INTENT["items"][:1]},
            {**BATCH_INTENT, "items": BATCH_INTENT["items"] * 3},
            {
                **BATCH_INTENT,
                "items": [
                    {**BATCH_INTENT["items"][0], "amount_minor": 650},
                    BATCH_INTENT["items"][1],
                ],
            },
            {
                **BATCH_INTENT,
                "items": [
                    {"mobile_proxy": "86952933", "proxy_evidence": "86952933"},
                    BATCH_INTENT["items"][1],
                ],
            },
        )
        for intent in cases:
            with self.subTest(intent=intent):
                with self.assertRaises(InvalidModelOutput):
                    validate_intent(json.dumps(intent))

    def test_validation_rejects_unpaired_or_wrong_branch_fields(self):
        from apps.dimon_live.model_adapter import InvalidModelOutput, validate_intent

        invalid = (
            {**MISSING_AMOUNT_INTENT, "amount_minor": 1000},
            {**GOLDEN_INTENT, "source_evidence": None},
            {**GOLDEN_INTENT, "mobile_proxy": "86952933"},
            {**PAYNOW_INTENT, "recipient_label": "John Smith"},
            {**GOLDEN_INTENT, "beneficiary_id": "server-owned"},
            {**GOLDEN_INTENT, "approved": True},
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(InvalidModelOutput):
                    validate_intent(json.dumps(value))

    def test_extractor_accepts_null_source_when_user_did_not_name_an_account(self):
        from apps.dimon_live.model_adapter import QwenTransferExtractor

        omitted_source = {
            **GOLDEN_INTENT,
            "source_account_ref": None,
            "source_evidence": None,
            "recipient_label": "Alicia Powell",
            "recipient_evidence": "Alicia Powell",
        }
        extractor = QwenTransferExtractor(
            transport=self.response_transport(json.dumps(omitted_source))
        )

        result = extractor.extract("I owe Alicia Powell five dollars.")

        self.assertIsNone(result.intent["source_account_ref"])
        self.assertEqual(result.intent["recipient_label"], "Alicia Powell")
        self.assertNotIn("amount_minor", result.intent)

    def test_extractor_rejects_finish_reason_length(self):
        from apps.dimon_live.model_adapter import (
            InvalidModelOutput,
            QwenTransferExtractor,
        )

        extractor = QwenTransferExtractor(
            transport=self.response_transport("{}", finish_reason="length")
        )

        with self.assertRaisesRegex(InvalidModelOutput, "finish_reason"):
            extractor.extract(GOLDEN_REQUEST)

    def test_extractor_rejects_extra_approval_or_command_fields(self):
        from apps.dimon_live.model_adapter import (
            InvalidModelOutput,
            QwenTransferExtractor,
        )

        unsafe = {**GOLDEN_INTENT, "approved": True, "command": "python transfer.py"}
        extractor = QwenTransferExtractor(
            transport=self.response_transport(json.dumps(unsafe))
        )

        with self.assertRaisesRegex(InvalidModelOutput, "unexpected fields"):
            extractor.extract(GOLDEN_REQUEST)

    def test_extractor_rejects_malformed_json(self):
        from apps.dimon_live.model_adapter import (
            InvalidModelOutput,
            QwenTransferExtractor,
        )

        extractor = QwenTransferExtractor(
            transport=self.response_transport("{not-json")
        )

        with self.assertRaises(InvalidModelOutput):
            extractor.extract(GOLDEN_REQUEST)

    def test_extractor_maps_timeout_to_model_unavailable(self):
        from apps.dimon_live.model_adapter import ModelUnavailable, QwenTransferExtractor

        def timeout_transport(url, payload, timeout):
            raise TimeoutError("timed out")

        extractor = QwenTransferExtractor(transport=timeout_transport)

        with self.assertRaisesRegex(ModelUnavailable, "MODEL_UNAVAILABLE"):
            extractor.extract(GOLDEN_REQUEST)


class QwenStockExtractorTests(unittest.TestCase):
    def test_stock_validation_expands_grounded_bare_count_to_exact_share_phrase(self):
        from apps.dimon_live.model_adapter import validate_stock_intent

        intent = {
            "contract_version": 2,
            "kind": "STOCK_INVESTMENT",
            "instrument_label": "NVIDIA",
            "instrument_evidence": "Nvidia",
            "objective": "GENERAL_ACQUISITION",
            "size_kind": "QUANTITY",
            "quantity": 1,
            "quantity_evidence": "one",
            "allocation_evidence": None,
        }

        validated = validate_stock_intent(
            json.dumps(intent), text="buy one stock in Nvidia"
        )

        self.assertEqual(validated["quantity"], 1)
        self.assertEqual(validated["quantity_evidence"], "one stock")

    def test_stock_extractor_returns_grounded_company_mention(self):
        from apps.dimon_live.model_adapter import QwenStockIntentExtractor

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
        calls = []

        def transport(url, payload, timeout):
            calls.append((url, payload, timeout))
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(intent)},
                }]
            }

        result = QwenStockIntentExtractor(transport=transport).extract(
            "I want you to help me buy five hundred dollars worth of NVIDIA stock."
        )

        self.assertEqual(result.intent, intent)
        self.assertEqual(calls[0][0], "http://127.0.0.1:8080/v1/chat/completions")
        schema = calls[0][1]["response_format"]["json_schema"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("instrument_evidence", schema["required"])
        self.assertIn("never return a ticker", calls[0][1]["messages"][0]["content"])

    def test_stock_validation_rejects_untrusted_extra_fields(self):
        from apps.dimon_live.model_adapter import InvalidModelOutput, validate_stock_intent

        invalid = {
            "contract_version": 1,
            "kind": "STOCK_INVESTMENT",
            "instrument_label": "NVDA",
            "instrument_evidence": "NVIDIA",
            "objective": "GENERAL_ACQUISITION",
            "size_kind": "UNKNOWN",
            "quantity": None,
            "quantity_evidence": None,
            "allocation_evidence": None,
            "symbol": "NVDA",
        }

        with self.assertRaisesRegex(InvalidModelOutput, "unexpected fields"):
            validate_stock_intent(json.dumps(invalid))


class QwenBillPaymentExtractorTests(unittest.TestCase):
    def test_extractor_returns_only_a_bounded_evidence_proposal(self):
        from apps.dimon_live.model_adapter import QwenBillPaymentExtractor

        intent = {
            "contract_version": 1,
            "kind": "BILL_PAYMENT",
            "biller_label": "Singapore Power",
            "biller_evidence": "Singapore Power bill",
            "amount_evidence": "$120",
            "currency": "SGD",
            "currency_evidence": "$",
            "source_account_label": "savings",
            "source_account_evidence": "savings",
            "requested_execution_date": None,
            "execution_date_evidence": None,
        }
        calls = []

        def transport(url, payload, timeout):
            calls.append((url, payload, timeout))
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(intent)},
                }]
            }

        result = QwenBillPaymentExtractor(transport=transport).extract(
            "Pay my Singapore Power bill $120 from savings."
        )

        self.assertEqual(result.intent, intent)
        schema = calls[0][1]["response_format"]["json_schema"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(intent))
        prompt = calls[0][1]["messages"][0]["content"]
        self.assertIn("exact substrings", prompt)
        self.assertIn("Never approve", prompt)

    def test_validation_allows_missing_biller_or_amount_for_clarification(self):
        from apps.dimon_live.model_adapter import validate_bill_payment_intent

        missing_biller = {
            "contract_version": 1,
            "kind": "BILL_PAYMENT",
            "biller_label": None,
            "biller_evidence": None,
            "amount_evidence": "$120",
            "currency": "SGD",
            "currency_evidence": "$",
            "source_account_label": None,
            "source_account_evidence": None,
            "requested_execution_date": None,
            "execution_date_evidence": None,
        }
        missing_amount = {**missing_biller, "biller_label": "SP", "biller_evidence": "SP"}
        missing_amount["amount_evidence"] = None

        self.assertEqual(validate_bill_payment_intent(json.dumps(missing_biller)), missing_biller)
        self.assertEqual(validate_bill_payment_intent(json.dumps(missing_amount)), missing_amount)

    def test_validation_rejects_authority_fields_and_unpaired_evidence(self):
        from apps.dimon_live.model_adapter import InvalidModelOutput, validate_bill_payment_intent

        base = {
            "contract_version": 1,
            "kind": "BILL_PAYMENT",
            "biller_label": "SP",
            "biller_evidence": "SP",
            "amount_evidence": "$120",
            "currency": "SGD",
            "currency_evidence": "$",
            "source_account_label": None,
            "source_account_evidence": None,
            "requested_execution_date": None,
            "execution_date_evidence": None,
        }
        with self.assertRaises(InvalidModelOutput):
            validate_bill_payment_intent(json.dumps({**base, "execute_now": True}))
        with self.assertRaises(InvalidModelOutput):
            validate_bill_payment_intent(json.dumps({**base, "biller_evidence": None}))


class QwenInvestmentFollowUpTests(unittest.TestCase):
    def test_follow_up_interpreter_maps_natural_market_language_to_bounded_action(self):
        from apps.dimon_live.model_adapter import QwenInvestmentFollowUpInterpreter

        output = {
            "contract_version": 3,
            "kind": "INVESTMENT_FOLLOW_UP",
            "actions": ["BUY_NOW"],
            "size_kind": "UNKNOWN",
            "quantity": None,
            "quantity_evidence": None,
            "allocation_evidence": None,
        }
        calls = []

        def transport(url, payload, timeout):
            calls.append((url, payload, timeout))
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(output)},
                }]
            }

        result = QwenInvestmentFollowUpInterpreter(transport=transport).interpret(
            "buy at market when market opens",
            phase="ORDER_STYLE",
            question="Buy now at market, or set a maximum price?",
            state={"symbol": "NVDA", "price": "238.90", "quote_currency": "USD"},
        )

        self.assertEqual(result.actions, ("BUY_NOW",))
        self.assertEqual(calls[0][0], "http://127.0.0.1:8080/v1/chat/completions")
        self.assertEqual(calls[0][1]["response_format"]["type"], "json_schema")
        self.assertIn("Never approve", calls[0][1]["messages"][0]["content"])

    def test_follow_up_interpreter_preserves_two_ordered_bounded_actions(self):
        from apps.dimon_live.model_adapter import QwenInvestmentFollowUpInterpreter

        output = {
            "contract_version": 3,
            "kind": "INVESTMENT_FOLLOW_UP",
            "actions": ["USE_AVAILABLE_FUNDS", "BUY_NOW"],
            "size_kind": "UNKNOWN",
            "quantity": None,
            "quantity_evidence": None,
            "allocation_evidence": None,
        }

        def transport(url, payload, timeout):
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(output)},
                }]
            }

        result = QwenInvestmentFollowUpInterpreter(transport=transport).interpret(
            "Lower it to what I can afford and execute it on market.",
            phase="FUNDING",
            question="Use the available balance, choose another funding account, or cancel?",
            state={"symbol": "NVDA", "price": "238.90", "quote_currency": "USD"},
        )

        self.assertEqual(result.actions, ("USE_AVAILABLE_FUNDS", "BUY_NOW"))

    def test_follow_up_validation_rejects_execution_authority(self):
        from apps.dimon_live.model_adapter import InvalidModelOutput, validate_follow_up_intent

        invalid = {
            "contract_version": 2,
            "kind": "INVESTMENT_FOLLOW_UP",
            "actions": ["EXECUTE_NOW"],
        }

        with self.assertRaises(InvalidModelOutput):
            validate_follow_up_intent(json.dumps(invalid))


if __name__ == "__main__":
    unittest.main()

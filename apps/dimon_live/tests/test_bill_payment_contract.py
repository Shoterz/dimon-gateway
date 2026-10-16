"""Focused tests for the read-only BILL_PAYMENT proposal contract.

These tests cover structure validation only. They do not claim transcript
grounding, receipt, approval, or execution integration.
"""

import unittest


class BillPaymentContractTests(unittest.TestCase):
    # A complete, well-formed proposal used as the base for mutation tests.
    BASE_PROPOSAL = {
        "contract_version": 1,
        "kind": "BILL_PAYMENT",
        "biller_label": "Singapore Power",
        "biller_evidence": "Singapore Power bill",
        "amount_evidence": "one hundred and twenty dollars",
        "currency": "SGD",
        "currency_evidence": "Singapore dollars",
        "source_account_label": "savings account",
        "source_account_evidence": "from my savings account",
        "requested_execution_date": "2026-10-15",
        "execution_date_evidence": "on the fifteenth of October",
    }

    # ------------------------------------------------------------------
    # 1. A complete proposal produces the expected immutable value.
    # ------------------------------------------------------------------
    def test_complete_proposal_produces_expected_immutable_value(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        result = parse_bill_payment_proposal(self.BASE_PROPOSAL)

        self.assertEqual(result.contract_version, 1)
        self.assertEqual(result.kind, "BILL_PAYMENT")
        self.assertEqual(result.biller_label, "Singapore Power")
        self.assertEqual(result.biller_evidence, "Singapore Power bill")
        self.assertEqual(result.amount_evidence, "one hundred and twenty dollars")
        self.assertEqual(result.currency, "SGD")
        self.assertEqual(result.currency_evidence, "Singapore dollars")
        self.assertEqual(result.source_account_label, "savings account")
        self.assertEqual(
            result.source_account_evidence, "from my savings account"
        )
        self.assertEqual(result.requested_execution_date, "2026-10-15")
        self.assertEqual(
            result.execution_date_evidence, "on the fifteenth of October"
        )
        # The value object is immutable.
        with self.assertRaises((AttributeError, TypeError)):
            result.kind = "OTHER"
        with self.assertRaises((AttributeError, TypeError)):
            result.contract_version = 2

    # ------------------------------------------------------------------
    # 2. Optional source-account fields may both be null.
    # ------------------------------------------------------------------
    def test_optional_source_account_fields_may_both_be_null(self):
        from apps.dimon_live.bill_payment_contract import (
            parse_bill_payment_proposal,
        )

        proposal = dict(self.BASE_PROPOSAL)
        proposal["source_account_label"] = None
        proposal["source_account_evidence"] = None

        result = parse_bill_payment_proposal(proposal)

        self.assertIsNone(result.source_account_label)
        self.assertIsNone(result.source_account_evidence)

    # ------------------------------------------------------------------
    # 3. Optional execution-date fields may both be null.
    # ------------------------------------------------------------------
    def test_optional_execution_date_fields_may_both_be_null(self):
        from apps.dimon_live.bill_payment_contract import (
            parse_bill_payment_proposal,
        )

        proposal = dict(self.BASE_PROPOSAL)
        proposal["requested_execution_date"] = None
        proposal["execution_date_evidence"] = None

        result = parse_bill_payment_proposal(proposal)

        self.assertIsNone(result.requested_execution_date)
        self.assertIsNone(result.execution_date_evidence)

    # ------------------------------------------------------------------
    # 4. Missing and extra keys fail.
    # ------------------------------------------------------------------
    def test_missing_key_fails(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        for missing in sorted(self.BASE_PROPOSAL):
            proposal = dict(self.BASE_PROPOSAL)
            del proposal[missing]
            with self.subTest(missing=missing):
                with self.assertRaises(BillPaymentContractError):
                    parse_bill_payment_proposal(proposal)

    def test_extra_key_fails(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        for extra in (
            "amount_minor",
            "biller_id",
            "approved",
            "approval_token",
            "receipt_digest",
            "execute_now",
            "tool_name",
            "shell_command",
            "sql_query",
            "ledger_entry_id",
            "canonical_biller_id",
        ):
            proposal = dict(self.BASE_PROPOSAL)
            proposal[extra] = "anything"
            with self.subTest(extra=extra):
                with self.assertRaises(BillPaymentContractError):
                    parse_bill_payment_proposal(proposal)

    # ------------------------------------------------------------------
    # 5. Empty or non-string evidence fails.
    # ------------------------------------------------------------------
    def test_empty_or_nonstring_evidence_fails(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        evidence_keys = (
            "biller_evidence",
            "amount_evidence",
            "currency_evidence",
        )
        for key in evidence_keys:
            for bad in ("", "   ", 42, None, True, []):
                proposal = dict(self.BASE_PROPOSAL)
                proposal[key] = bad
                with self.subTest(key=key, bad=bad):
                    with self.assertRaises(BillPaymentContractError):
                        parse_bill_payment_proposal(proposal)

    def test_empty_or_nonstring_biller_label_fails(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        for bad in ("", "   ", 42, None, True, []):
            proposal = dict(self.BASE_PROPOSAL)
            proposal["biller_label"] = bad
            with self.subTest(bad=bad):
                with self.assertRaises(BillPaymentContractError):
                    parse_bill_payment_proposal(proposal)

    # ------------------------------------------------------------------
    # 6. A source-account label/evidence mismatch fails.
    # ------------------------------------------------------------------
    def test_source_account_label_evidence_mismatch_fails(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        cases = (
            {"source_account_label": None, "source_account_evidence": "savings"},
            {"source_account_label": "savings", "source_account_evidence": None},
        )
        for override in cases:
            proposal = dict(self.BASE_PROPOSAL)
            proposal.update(override)
            with self.subTest(override=override):
                with self.assertRaises(BillPaymentContractError):
                    parse_bill_payment_proposal(proposal)

    # ------------------------------------------------------------------
    # 7. An execution-date/evidence mismatch fails.
    # ------------------------------------------------------------------
    def test_execution_date_evidence_mismatch_fails(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        cases = (
            {
                "requested_execution_date": None,
                "execution_date_evidence": "on Friday",
            },
            {
                "requested_execution_date": "2026-10-15",
                "execution_date_evidence": None,
            },
        )
        for override in cases:
            proposal = dict(self.BASE_PROPOSAL)
            proposal.update(override)
            with self.subTest(override=override):
                with self.assertRaises(BillPaymentContractError):
                    parse_bill_payment_proposal(proposal)

    # ------------------------------------------------------------------
    # 8. Invalid dates fail.
    # ------------------------------------------------------------------
    def test_invalid_dates_fail(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        for bad in (
            "2026-13-01",  # invalid month
            "2026-02-30",  # invalid day for February
            "2026-04-31",  # invalid day for April
            "2026/10/15",  # wrong separator
            "15-10-2026",  # wrong order
            "2026-1-5",    # not zero-padded
            "20261015",    # not ISO
            "not-a-date",
            20261015,
        ):
            proposal = dict(self.BASE_PROPOSAL)
            proposal["requested_execution_date"] = bad
            proposal["execution_date_evidence"] = "evidence"
            with self.subTest(bad=bad):
                with self.assertRaises(BillPaymentContractError):
                    parse_bill_payment_proposal(proposal)

    # ------------------------------------------------------------------
    # 9. Non-SGD currency fails.
    # ------------------------------------------------------------------
    def test_non_sgd_currency_fails(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        for bad in ("USD", "sgd", "Sgd", "SGD ", " SGD", "", None, 1, True, []):
            proposal = dict(self.BASE_PROPOSAL)
            proposal["currency"] = bad
            with self.subTest(bad=bad):
                with self.assertRaises(BillPaymentContractError):
                    parse_bill_payment_proposal(proposal)

    # ------------------------------------------------------------------
    # 10. Approval-, receipt-, execution-, tool-, SQL-, and ledger-shaped
    #     extra keys fail.
    # ------------------------------------------------------------------
    def test_dangerous_extra_keys_fail(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        dangerous = {
            "approved": True,
            "approval_token": "abc",
            "receipt_digest": "deadbeef",
            "receipt_id": "R-1",
            "execute_now": True,
            "execution_command": "run",
            "tool_name": "transfer_tool",
            "tool_call_id": "t-1",
            "shell_command": "rm -rf /",
            "sql_query": "UPDATE ledger SET ...",
            "ledger_entry_id": "L-9",
            "ledger_authority": "root",
            "canonical_biller_id": "B-123",
            "credential": "password",
            "identity_token": "jwt",
        }
        for key, value in dangerous.items():
            proposal = dict(self.BASE_PROPOSAL)
            proposal[key] = value
            with self.subTest(key=key):
                with self.assertRaises(BillPaymentContractError):
                    parse_bill_payment_proposal(proposal)

    # ------------------------------------------------------------------
    # 11. The production module does not import any DIMON database,
    #     service, model, receipt, approval, identity, execution, or
    #     ledger module.
    # ------------------------------------------------------------------
    def test_production_module_imports_no_dimon_internal_modules(self):
        import importlib
        import sys

        # Remove any cached copy so we observe a fresh import.
        sys.modules.pop("apps.dimon_live.bill_payment_contract", None)

        before = set(sys.modules)
        importlib.import_module("apps.dimon_live.bill_payment_contract")
        after = set(sys.modules)
        added = {m for m in (after - before)}

        forbidden_substrings = (
            "database",
            "service",
            "model_adapter",
            "receipt",
            "approval",
            "identity",
            "execution",
            "ledger",
            "singpass",
            "kyc",
            "paynow",
            "investing",
            "market",
            "signing_store",
            "audit_log",
            "validation_agent",
            "validation_store",
            "conversation",
            "session_manager",
            "risk_policy",
            "scam_guard",
            "read_back",
            "batch_receipts",
            "batch_grounding",
            "grounding",
            "provider_factory",
            "remote_inference",
            "fixture_inference",
            "inference",
            "face_id",
            "name_resolution",
            "payees",
            "stock_market",
            "paper_equity",
            "intel_voice",
            "tencent_tts",
            "speech",
            "voice",
            "cli",
            "app",
            "demo_preparation",
            "conversation_store",
            "gpu_overlord",
            "paper_equity_service",
            "paper_equity_store",
            "investment_conversation",
            "device_signer",
            "signing",
            "no_approval",
        )
        for module_name in added:
            # Allow the contract module itself and standard-library paths.
            if module_name == "apps.dimon_live.bill_payment_contract":
                continue
            if module_name.startswith("apps."):
                for forbidden in forbidden_substrings:
                    self.assertNotIn(
                        forbidden,
                        module_name,
                        f"contract imported forbidden module {module_name}",
                    )

    # ------------------------------------------------------------------
    # Additional boundary checks that strengthen the exact-key contract.
    # ------------------------------------------------------------------
    def test_boolean_contract_version_is_rejected(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        for bad in (True, False, 1.0, "1", 2, 0):
            proposal = dict(self.BASE_PROPOSAL)
            proposal["contract_version"] = bad
            with self.subTest(bad=bad):
                with self.assertRaises(BillPaymentContractError):
                    parse_bill_payment_proposal(proposal)

    def test_wrong_kind_is_rejected(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        for bad in ("BILL", "bill_payment", "BENEFICIARY_TRANSFER", "", None, 1):
            proposal = dict(self.BASE_PROPOSAL)
            proposal["kind"] = bad
            with self.subTest(bad=bad):
                with self.assertRaises(BillPaymentContractError):
                    parse_bill_payment_proposal(proposal)

    def test_non_mapping_input_is_rejected(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        for bad in (None, [], "not-a-mapping", 42, 3.14):
            with self.subTest(bad=bad):
                with self.assertRaises(BillPaymentContractError):
                    parse_bill_payment_proposal(bad)

    def test_amount_minor_numeric_amount_is_rejected_as_extra_key(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        proposal = dict(self.BASE_PROPOSAL)
        proposal["amount_minor"] = 12000
        with self.assertRaises(BillPaymentContractError):
            parse_bill_payment_proposal(proposal)

    def test_optional_source_account_evidence_must_be_nonempty_when_present(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        proposal = dict(self.BASE_PROPOSAL)
        proposal["source_account_evidence"] = "   "
        with self.assertRaises(BillPaymentContractError):
            parse_bill_payment_proposal(proposal)

    def test_optional_execution_date_evidence_must_be_nonempty_when_present(self):
        from apps.dimon_live.bill_payment_contract import (
            BillPaymentContractError,
            parse_bill_payment_proposal,
        )

        proposal = dict(self.BASE_PROPOSAL)
        proposal["requested_execution_date"] = "2026-10-15"
        proposal["execution_date_evidence"] = ""
        with self.assertRaises(BillPaymentContractError):
            parse_bill_payment_proposal(proposal)

    def test_only_one_contract_exception_is_exposed(self):
        import apps.dimon_live.bill_payment_contract as module

        exception_names = [
            name
            for name in dir(module)
            if isinstance(getattr(module, name), type)
            and issubclass(getattr(module, name), BaseException)
        ]
        self.assertEqual(
            exception_names,
            ["BillPaymentContractError"],
            f"expected exactly one contract exception, got {exception_names}",
        )


if __name__ == "__main__":
    unittest.main()

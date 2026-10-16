"""Strict, read-only BILL_PAYMENT proposal contract.

This module defines the *shape* of an untrusted model proposal for a bill
payment. It parses a mapping into an immutable value object and rejects
malformed input with one specific contract exception.

Scope boundaries (intentional):
  - This module validates structure only.
  - It does NOT resolve a real biller, calculate or move money, create a
    receipt, request approval, or connect to execution.
  - It does NOT import or call any DIMON database, service, model, receipt,
    approval, identity, execution, or ledger module.
  - It does NOT add SQL, shell access, tool calls, credentials, canonical
    biller IDs, receipt digests, approval fields, or ledger authority.
  - It does NOT claim that an evidence string is grounded in a transcript;
    deterministic transcript grounding is a later integration task.

Only the Python standard library is used.
"""

from __future__ import annotations

import datetime
from typing import Any, Mapping, NamedTuple

__all__ = ["BillPaymentContractError", "BillPaymentProposal", "parse_bill_payment_proposal"]


class BillPaymentContractError(Exception):
    """The single contract exception raised for all malformed input."""


class BillPaymentProposal(NamedTuple):
    """Immutable value object for a validated BILL_PAYMENT proposal.

    NamedTuple subclasses are immutable: field assignment raises
    ``AttributeError``. The fields expose only proposal shape; they carry no
    execution, approval, receipt, or ledger authority.
    """

    contract_version: int
    kind: str
    biller_label: str
    biller_evidence: str
    amount_evidence: str
    currency: str
    currency_evidence: str
    source_account_label: str | None
    source_account_evidence: str | None
    requested_execution_date: str | None
    execution_date_evidence: str | None


# The exact set of keys accepted by this contract. Any missing or extra key
# is rejected, which also rejects approval-, receipt-, credential-, canonical-
# ID-, execution-, tool-, shell-, SQL-, and ledger-shaped keys by virtue of
# the exact-key contract.
_ALLOWED_KEYS = frozenset(
    {
        "contract_version",
        "kind",
        "biller_label",
        "biller_evidence",
        "amount_evidence",
        "currency",
        "currency_evidence",
        "source_account_label",
        "source_account_evidence",
        "requested_execution_date",
        "execution_date_evidence",
    }
)


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_optional_non_empty_string(value: Any) -> bool:
    return value is None or _is_non_empty_string(value)


def _is_iso_date(value: Any) -> bool:
    # Require the strict YYYY-MM-DD form (with dashes), then validate the
    # calendar date. Python's date.fromisoformat also accepts the basic
    # YYYYMMDD form on 3.11+, which this contract rejects.
    if not isinstance(value, str):
        return False
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _fail(message: str) -> None:
    raise BillPaymentContractError(message)


def _require_paired_optional(
    proposal: Mapping[str, Any],
    value_key: str,
    evidence_key: str,
) -> None:
    value = proposal[value_key]
    evidence = proposal[evidence_key]
    if (value is None) != (evidence is None):
        _fail(f"{value_key} and {evidence_key} must both be null or both be present")
    if not _is_optional_non_empty_string(evidence):
        _fail(f"{evidence_key} must be null or a non-empty string")


def parse_bill_payment_proposal(proposal: Any) -> BillPaymentProposal:
    """Parse an untrusted mapping into an immutable ``BillPaymentProposal``.

    Raises ``BillPaymentContractError`` for any malformed input, including:
    missing keys, extra keys, empty strings, wrong types, unsupported
    currency, invalid calendar dates, and unpaired optional fields.
    """
    if not isinstance(proposal, Mapping):
        _fail("proposal must be a mapping")

    keys = set(proposal)
    missing = _ALLOWED_KEYS - keys
    if missing:
        _fail(f"missing keys: {sorted(missing)}")
    extra = keys - _ALLOWED_KEYS
    if extra:
        _fail(f"unexpected keys: {sorted(extra)}")

    version = proposal["contract_version"]
    # Booleans are a subclass of int in Python; reject them explicitly so that
    # True/False cannot satisfy the integer-1 requirement.
    if isinstance(version, bool) or not isinstance(version, int):
        _fail("contract_version must be integer 1")
    if version != 1:
        _fail("contract_version must be 1")

    kind = proposal["kind"]
    if kind != "BILL_PAYMENT":
        _fail("kind must be BILL_PAYMENT")

    biller_label = proposal["biller_label"]
    if not _is_non_empty_string(biller_label):
        _fail("biller_label must be a non-empty string")

    if not _is_non_empty_string(proposal["biller_evidence"]):
        _fail("biller_evidence must be a non-empty string")

    if not _is_non_empty_string(proposal["amount_evidence"]):
        _fail("amount_evidence must be a non-empty string")

    currency = proposal["currency"]
    if currency != "SGD":
        _fail("currency must be SGD")

    if not _is_non_empty_string(proposal["currency_evidence"]):
        _fail("currency_evidence must be a non-empty string")

    # Optional paired: source account label + evidence.
    _require_paired_optional(proposal, "source_account_label", "source_account_evidence")
    source_account_label = proposal["source_account_label"]
    if not _is_optional_non_empty_string(source_account_label):
        _fail("source_account_label must be null or a non-empty string")

    # Optional paired: execution date + evidence.
    _require_paired_optional(
        proposal, "requested_execution_date", "execution_date_evidence"
    )
    requested_execution_date = proposal["requested_execution_date"]
    if requested_execution_date is not None and not _is_iso_date(requested_execution_date):
        _fail("requested_execution_date must be null or a valid ISO YYYY-MM-DD date")

    return BillPaymentProposal(
        contract_version=version,
        kind=kind,
        biller_label=biller_label,
        biller_evidence=proposal["biller_evidence"],
        amount_evidence=proposal["amount_evidence"],
        currency=currency,
        currency_evidence=proposal["currency_evidence"],
        source_account_label=source_account_label,
        source_account_evidence=proposal["source_account_evidence"],
        requested_execution_date=requested_execution_date,
        execution_date_evidence=proposal["execution_date_evidence"],
    )

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apps.dimon_live.paper_equity import (
    PaperEquityDraft,
    PaperExecutionSnapshot,
    PaperFxSnapshot,
    PaperInstrument,
    PaperFillDecisionStatus,
    PaperOrderType,
    PaperQuoteSnapshot,
    PaperTimeInForce,
    build_receipt,
    decide_fill,
)


NOW = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)


def draft(
    *,
    order_type=PaperOrderType.MARKET,
    quantity=2,
    allocation_minor=50_000,
    price_minor=23_706,
    limit_price_minor=None,
    market_state="OPEN",
    fx_rate=Decimal("1.2802"),
    allocation_is_cap=True,
):
    return PaperEquityDraft(
        user_id="demo_user_01",
        conversation_id="conversation_test",
        conversation_version=2,
        funding_account_id="acct_savings_01",
        instrument=PaperInstrument(
            instrument_id="YF:NVDA",
            symbol="NVDA",
            name="NVIDIA Corporation",
            exchange="NasdaqGS",
            quote_currency="USD",
        ),
        quantity=quantity,
        allocation_minor=allocation_minor,
        allocation_currency="USD",
        funding_currency="SGD",
        order_type=order_type,
        limit_price_minor=limit_price_minor,
        time_in_force=(
            PaperTimeInForce.GTC
            if order_type is PaperOrderType.LIMIT
            else PaperTimeInForce.DAY
        ),
        quote_snapshot=PaperQuoteSnapshot(
            price_minor=price_minor,
            market_state=market_state,
            captured_at=NOW,
            source="YAHOO_FINANCE",
        ),
        fx_snapshot=PaperFxSnapshot(
            pair="USD/SGD",
            rate=fx_rate,
            captured_at=NOW,
            source="YAHOO_FINANCE",
        ),
        allocation_is_cap=allocation_is_cap,
    )


def execution(receipt, *, price_minor=None, market_state="OPEN", fx_rate=None):
    return PaperExecutionSnapshot(
        price_minor=price_minor or receipt.quote_snapshot.price_minor,
        market_state=market_state,
        captured_at=NOW + timedelta(seconds=30),
        source="YAHOO_FINANCE",
        fx_snapshot=PaperFxSnapshot(
            pair=receipt.fx_snapshot.pair,
            rate=fx_rate or receipt.fx_snapshot.rate,
            captured_at=NOW + timedelta(seconds=30),
            source="YAHOO_FINANCE",
        ),
    )


def test_market_receipt_binds_canonical_cross_currency_terms_and_digest():
    receipt = build_receipt(draft(), order_id="paper_order_1", nonce="nonce_1", now=NOW)

    assert receipt.quantity == 2
    assert receipt.quote_consideration_minor == 47_412
    assert receipt.fee_funding_minor == 50
    assert receipt.max_quote_price_minor == 24_180
    assert receipt.max_funding_debit_minor >= receipt.estimated_funding_minor
    assert receipt.approval_expires_at == NOW + timedelta(minutes=5)
    assert len(receipt.digest) == 64
    assert receipt.canonical_json

    equivalent = build_receipt(draft(), order_id="paper_order_1", nonce="nonce_1", now=NOW)
    assert equivalent.digest == receipt.digest


def test_market_protection_never_exceeds_allocation_per_share():
    receipt = build_receipt(
        draft(quantity=2, allocation_minor=47_412, price_minor=23_706),
        order_id="paper_order_2",
        nonce="nonce_2",
        now=NOW,
    )

    assert receipt.max_quote_price_minor <= 47_412 // 2


def test_quantity_market_order_keeps_price_protection_when_allocation_is_only_an_estimate():
    receipt = build_receipt(
        draft(
            quantity=1,
            allocation_minor=23_706,
            price_minor=23_706,
            allocation_is_cap=False,
        ),
        order_id="paper_order_quantity",
        nonce="nonce_quantity",
        now=NOW,
    )

    assert receipt.max_quote_price_minor == 24_180
    decision = decide_fill(
        receipt,
        execution(receipt, price_minor=23_707),
        now=NOW + timedelta(seconds=30),
    )
    assert decision.status is PaperFillDecisionStatus.FILLED


def test_limit_receipt_uses_the_explicit_limit_without_market_buffer():
    receipt = build_receipt(
        draft(
            order_type=PaperOrderType.LIMIT,
            allocation_minor=50_000,
            limit_price_minor=23_000,
        ),
        order_id="paper_order_3",
        nonce="nonce_3",
        now=NOW,
    )

    assert receipt.order_type is PaperOrderType.LIMIT
    assert receipt.limit_price_minor == 23_000
    assert receipt.max_quote_price_minor == 23_000


@pytest.mark.parametrize("quantity", [0, -1, 1.5, True])
def test_receipt_rejects_fractional_or_zero_quantity(quantity):
    with pytest.raises(ValueError, match="quantity"):
        build_receipt(
            draft(quantity=quantity),
            order_id="paper_order_bad_quantity",
            nonce="nonce_bad_quantity",
            now=NOW,
        )


def test_receipt_rejects_binary_float_money_and_naive_timestamps():
    with pytest.raises(ValueError, match="price_minor"):
        build_receipt(
            draft(price_minor=237.06),
            order_id="paper_order_bad_money",
            nonce="nonce_bad_money",
            now=NOW,
        )
    with pytest.raises(ValueError, match="timezone"):
        build_receipt(
            draft(),
            order_id="paper_order_bad_time",
            nonce="nonce_bad_time",
            now=NOW.replace(tzinfo=None),
        )


def test_fill_decision_never_compares_usd_minor_units_with_sgd_minor_units():
    receipt = build_receipt(draft(), order_id="paper_order_4", nonce="nonce_4", now=NOW)

    result = decide_fill(receipt, execution(receipt), now=NOW + timedelta(minutes=1))

    assert result.status is PaperFillDecisionStatus.FILLED
    assert result.fill_price_minor == 23_706
    assert result.funding_debit_minor > receipt.quote_consideration_minor
    assert result.funding_currency == "SGD"

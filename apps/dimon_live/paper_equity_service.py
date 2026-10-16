"""Application orchestration for the real-equity paper-order boundary."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .database import Database
from .approval_gateway import ApprovalContext, DeviceAssertion
from .paper_equity import (
    PaperEquityReceipt, PaperExecutionSnapshot, PaperFxSnapshot,
    normalize_market_state, receipt_from_dict,
)
from .stock_market import MarketProvider
from .paper_equity_store import PaperEquityStore


class PaperEquityService:
    """Keep stock order lifecycle calls separate from payment services."""

    def __init__(
        self,
        database: Database,
        *,
        store: PaperEquityStore | None = None,
        clock: Callable[[], datetime] | None = None,
        provider: MarketProvider | None = None,
        demo_exchange: bool = False,
    ) -> None:
        self.database = database
        self.clock = clock or database._clock
        self.provider = provider
        self.demo_exchange = demo_exchange
        self.store = store or PaperEquityStore(database.path, clock=self.clock)

    def create_order(
        self,
        *,
        conversation_id: str,
        conversation_version: int,
        session_scope: str,
        user_id: str,
        request_id: str,
    ) -> PaperEquityReceipt:
        return self.store.consume_ready_conversation(
            conversation_id=conversation_id,
            conversation_version=conversation_version,
            session_scope=session_scope,
            user_id=user_id,
            request_id=request_id,
        )

    def request_singpass(
        self,
        order_id: str,
        order_digest: str,
        *,
        user_id: str,
    ) -> dict[str, Any]:
        return self.database.request_paper_equity_singpass_authorisation(
            order_id,
            order_digest,
            user_id=user_id,
            now=self.clock(),
        )

    def issue_challenge(
        self,
        order_id: str,
        order_digest: str,
        *,
        user_id: str,
        rp_id: str,
    ) -> dict[str, Any]:
        return self.database.issue_paper_equity_challenge(
            order_id,
            order_digest,
            user_id=user_id,
            rp_id=rp_id,
            now=self.clock(),
        )

    def approve_and_execute(
        self,
        order_id: str,
        order_digest: str,
        *,
        assertion: DeviceAssertion,
        context: ApprovalContext,
        execution: PaperExecutionSnapshot,
        user_id: str,
    ) -> dict[str, Any]:
        return self.database.approve_paper_equity_order(
            order_id,
            order_digest,
            assertion=assertion,
            context=context,
            execution=execution,
            user_id=user_id,
            now=self.clock(),
        )

    def execution_snapshot(self, order_id: str, *, user_id: str, session_scope: str) -> PaperExecutionSnapshot:
        if self.provider is None:
            raise ValueError("PAPER_EQUITY_PROVIDER_UNAVAILABLE")
        payload = self.store.get_order(order_id, user_id=user_id, session_scope=session_scope)
        receipt = receipt_from_dict(payload)
        captured_at = self.clock()
        market = self.provider.snapshot(receipt.instrument.symbol, captured_at)
        actual_market_state = normalize_market_state(market.market_state)
        fx = self.provider.fx_estimate(
            receipt.instrument.quote_currency,
            receipt.funding_currency,
            Decimal(receipt.quote_consideration_minor) / Decimal(100),
            captured_at,
        )
        return PaperExecutionSnapshot(
            price_minor=int((Decimal(str(market.price)) * 100).quantize(Decimal("1"))),
            market_state=("OPEN" if self.demo_exchange and actual_market_state != "OPEN"
                          else actual_market_state),
            captured_at=captured_at,
            source=fx.source or market.__class__.__name__,
            fx_snapshot=PaperFxSnapshot(
                pair=fx.pair,
                rate=fx.rate,
                captured_at=fx.captured_at,
                source=fx.source,
            ),
            actual_market_state=actual_market_state,
            demo_override=self.demo_exchange and actual_market_state != "OPEN",
        )

"""Contracts and deterministic policy for real-equity paper orders.

This module has no database, network, browser, or approval side effects. It
turns server-owned research snapshots into an immutable receipt and decides
whether a refreshed paper quote can fill that receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from .receipts import canonical_json, digest_canonical


MONEY_QUANTUM = Decimal("0.01")
PRICE_PROTECTION_BP = 200
FEE_BP = 8
MIN_FEE_MINOR = 50
APPROVAL_TTL = timedelta(minutes=5)
DAY_ORDER_CAP = timedelta(days=7)
GTC_ORDER_TTL = timedelta(days=30)


class PaperOrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class PaperTimeInForce(str, Enum):
    DAY = "DAY"
    GTC = "GTC"


class PaperOrderStatus(str, Enum):
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AUTHORIZED = "AUTHORIZED"
    QUEUED = "QUEUED"
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    REQUOTE_REQUIRED = "REQUOTE_REQUIRED"
    SUPERSEDED = "SUPERSEDED"


class PaperFillDecisionStatus(str, Enum):
    FILLED = "FILLED"
    OPEN = "OPEN"
    QUEUED = "QUEUED"
    EXPIRED = "EXPIRED"
    REQUOTE_REQUIRED = "REQUOTE_REQUIRED"
    REJECTED = "REJECTED"


def normalize_market_state(value: object) -> str:
    normalized = str(value or "").strip().casefold().replace("_", "-")
    if normalized in {"open", "regular", "trading", "in-session", "in session"}:
        return "OPEN"
    if normalized in {"closed", "halted", "market-closed", "market closed"}:
        return "CLOSED"
    if normalized in {"pre", "pre-market", "premarket", "pre market"}:
        return "PRE_MARKET"
    if normalized in {"post", "post-market", "postmarket", "post market"}:
        return "POST_MARKET"
    return "UNKNOWN"


def _aware(value: datetime, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _minor(value: object, field: str, *, positive: bool = False) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer minor-unit value")
    if positive and value <= 0:
        raise ValueError(f"{field} must be positive")
    return value


def _decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, float):
        raise ValueError(f"{field} must not be a binary float")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"{field} must be a finite positive decimal")
    return result


def _quantize_money(value: Decimal, *, rounding=ROUND_HALF_UP) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=rounding)


def _convert_minor(base_minor: int, rate: Decimal) -> int:
    converted = (Decimal(base_minor) / Decimal(100)) * rate
    return int((_quantize_money(converted) * 100).to_integral_value())


def _fee_minor(funding_minor: int) -> int:
    calculated = (Decimal(funding_minor) * Decimal(FEE_BP) / Decimal(10_000)).quantize(
        Decimal("1"), rounding=ROUND_UP
    )
    return max(MIN_FEE_MINOR, int(calculated))


def _exchange_zone(exchange: str) -> ZoneInfo:
    lowered = exchange.casefold()
    if any(name in lowered for name in ("nasdaq", "nyse", "amex")):
        return ZoneInfo("America/New_York")
    if "sgx" in lowered or "singapore" in lowered:
        return ZoneInfo("Asia/Singapore")
    return ZoneInfo("UTC")


def _next_weekday(day: date) -> date:
    candidate = day + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _order_deadline(
    now: datetime,
    *,
    time_in_force: PaperTimeInForce,
    exchange: str,
    market_state: str,
) -> datetime:
    _aware(now, "now")
    if time_in_force is PaperTimeInForce.GTC:
        return now.astimezone(timezone.utc) + GTC_ORDER_TTL

    zone = _exchange_zone(exchange)
    local_now = now.astimezone(zone)
    close_time = time(16, 0)
    target_day = local_now.date()
    if normalize_market_state(market_state) != "OPEN" or local_now.time() >= close_time:
        target_day = _next_weekday(target_day)
    deadline = datetime.combine(target_day, close_time, tzinfo=zone).astimezone(timezone.utc)
    cap = now.astimezone(timezone.utc) + DAY_ORDER_CAP
    return min(deadline, cap)


@dataclass(frozen=True)
class PaperInstrument:
    instrument_id: str
    symbol: str
    name: str
    exchange: str
    quote_currency: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (
            self.instrument_id, self.symbol, self.name, self.exchange, self.quote_currency
        )):
            raise ValueError("instrument fields must be non-empty strings")


@dataclass(frozen=True)
class PaperQuoteSnapshot:
    price_minor: int
    market_state: str
    captured_at: datetime
    source: str

    def __post_init__(self) -> None:
        _minor(self.price_minor, "price_minor", positive=True)
        _aware(self.captured_at, "captured_at")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be non-empty")
        object.__setattr__(self, "market_state", normalize_market_state(self.market_state))


@dataclass(frozen=True)
class PaperFxSnapshot:
    pair: str
    rate: Decimal
    captured_at: datetime
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.pair, str) or "/" not in self.pair:
            raise ValueError("pair must be a currency pair")
        object.__setattr__(self, "pair", self.pair.upper())
        object.__setattr__(self, "rate", _decimal(self.rate, "rate", positive=True))
        _aware(self.captured_at, "captured_at")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be non-empty")


@dataclass(frozen=True)
class PaperEquityDraft:
    user_id: str
    conversation_id: str
    conversation_version: int
    funding_account_id: str
    instrument: PaperInstrument
    quantity: int
    allocation_minor: int
    allocation_currency: str
    funding_currency: str
    order_type: PaperOrderType
    limit_price_minor: int | None
    time_in_force: PaperTimeInForce
    quote_snapshot: PaperQuoteSnapshot
    fx_snapshot: PaperFxSnapshot
    side: str = "BUY"
    allocation_is_cap: bool = True

    def __post_init__(self) -> None:
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if type(self.allocation_is_cap) is not bool:
            raise ValueError("allocation_is_cap must be boolean")
        for field in ("user_id", "conversation_id", "funding_account_id", "allocation_currency", "funding_currency"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field).strip():
                raise ValueError(f"{field} must be non-empty")
        if type(self.conversation_version) is not int or self.conversation_version < 1:
            raise ValueError("conversation_version must be positive")
        _minor(self.quantity, "quantity", positive=True)
        _minor(self.allocation_minor, "allocation_minor", positive=True)
        if self.limit_price_minor is not None:
            _minor(self.limit_price_minor, "limit_price_minor", positive=True)
        if self.order_type is PaperOrderType.MARKET and self.limit_price_minor is not None:
            raise ValueError("market order cannot have a limit price")
        if self.order_type is PaperOrderType.LIMIT and self.limit_price_minor is None:
            raise ValueError("limit order requires a limit price")
        if self.order_type is PaperOrderType.MARKET and self.time_in_force is not PaperTimeInForce.DAY:
            raise ValueError("market order must be DAY")


@dataclass(frozen=True)
class PaperEquityReceipt:
    order_id: str
    nonce: str
    receipt_version: int
    user_id: str
    conversation_id: str
    conversation_version: int
    funding_account_id: str
    instrument: PaperInstrument
    quantity: int
    allocation_minor: int
    allocation_currency: str
    funding_currency: str
    order_type: PaperOrderType
    limit_price_minor: int | None
    time_in_force: PaperTimeInForce
    quote_snapshot: PaperQuoteSnapshot
    fx_snapshot: PaperFxSnapshot
    quote_consideration_minor: int
    estimated_funding_minor: int
    fee_funding_minor: int
    max_quote_price_minor: int
    max_funding_debit_minor: int
    price_protection_basis_points: int
    approval_expires_at: datetime
    order_expires_at: datetime
    status: PaperOrderStatus
    canonical_json: str
    digest: str
    side: str = "BUY"
    min_funding_credit_minor: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "order_id": self.order_id,
            "nonce": self.nonce,
            "receipt_version": self.receipt_version,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "conversation_version": self.conversation_version,
            "funding_account_id": self.funding_account_id,
            "instrument": {
                "instrument_id": self.instrument.instrument_id,
                "symbol": self.instrument.symbol,
                "name": self.instrument.name,
                "exchange": self.instrument.exchange,
                "quote_currency": self.instrument.quote_currency,
            },
            "quantity": self.quantity,
            "allocation_minor": self.allocation_minor,
            "allocation_currency": self.allocation_currency,
            "funding_currency": self.funding_currency,
            "order_type": self.order_type.value,
            "limit_price_minor": self.limit_price_minor,
            "time_in_force": self.time_in_force.value,
            "quote_snapshot": {
                "price_minor": self.quote_snapshot.price_minor,
                "market_state": self.quote_snapshot.market_state,
                "captured_at": self.quote_snapshot.captured_at.astimezone(timezone.utc).isoformat(),
                "source": self.quote_snapshot.source,
            },
            "fx_snapshot": {
                "pair": self.fx_snapshot.pair,
                "rate": str(self.fx_snapshot.rate),
                "captured_at": self.fx_snapshot.captured_at.astimezone(timezone.utc).isoformat(),
                "source": self.fx_snapshot.source,
            },
            "quote_consideration_minor": self.quote_consideration_minor,
            "estimated_funding_minor": self.estimated_funding_minor,
            "fee_funding_minor": self.fee_funding_minor,
            "max_quote_price_minor": self.max_quote_price_minor,
            "max_funding_debit_minor": self.max_funding_debit_minor,
            "price_protection_basis_points": self.price_protection_basis_points,
            "approval_expires_at": self.approval_expires_at.astimezone(timezone.utc).isoformat(),
            "order_expires_at": self.order_expires_at.astimezone(timezone.utc).isoformat(),
            "status": self.status.value,
            "digest": self.digest,
        }
        if self.side == "SELL":
            payload["side"] = "SELL"
            payload["min_funding_credit_minor"] = self.min_funding_credit_minor
        return payload


def receipt_from_dict(payload: dict[str, Any]) -> PaperEquityReceipt:
    """Rehydrate a server-owned receipt from its persisted public payload."""

    instrument = payload["instrument"]
    quote = payload["quote_snapshot"]
    fx = payload["fx_snapshot"]
    return PaperEquityReceipt(
        order_id=payload["order_id"],
        nonce=payload["nonce"],
        receipt_version=int(payload["receipt_version"]),
        user_id=payload["user_id"],
        conversation_id=payload["conversation_id"],
        conversation_version=int(payload["conversation_version"]),
        funding_account_id=payload["funding_account_id"],
        instrument=PaperInstrument(**instrument),
        quantity=int(payload["quantity"]),
        allocation_minor=int(payload["allocation_minor"]),
        allocation_currency=payload["allocation_currency"],
        funding_currency=payload["funding_currency"],
        order_type=PaperOrderType(payload["order_type"]),
        limit_price_minor=(
            int(payload["limit_price_minor"])
            if payload.get("limit_price_minor") is not None
            else None
        ),
        time_in_force=PaperTimeInForce(payload["time_in_force"]),
        quote_snapshot=PaperQuoteSnapshot(
            price_minor=int(quote["price_minor"]),
            market_state=quote["market_state"],
            captured_at=datetime.fromisoformat(quote["captured_at"]),
            source=quote["source"],
        ),
        fx_snapshot=PaperFxSnapshot(
            pair=fx["pair"],
            rate=Decimal(fx["rate"]),
            captured_at=datetime.fromisoformat(fx["captured_at"]),
            source=fx["source"],
        ),
        quote_consideration_minor=int(payload["quote_consideration_minor"]),
        estimated_funding_minor=int(payload["estimated_funding_minor"]),
        fee_funding_minor=int(payload["fee_funding_minor"]),
        max_quote_price_minor=int(payload["max_quote_price_minor"]),
        max_funding_debit_minor=int(payload["max_funding_debit_minor"]),
        price_protection_basis_points=int(payload["price_protection_basis_points"]),
        approval_expires_at=datetime.fromisoformat(payload["approval_expires_at"]),
        order_expires_at=datetime.fromisoformat(payload["order_expires_at"]),
        status=PaperOrderStatus(payload["status"]),
        canonical_json=str(payload.get("canonical_json", "")),
        digest=payload["digest"],
        side=str(payload.get("side") or "BUY"),
        min_funding_credit_minor=(
            int(payload["min_funding_credit_minor"])
            if payload.get("min_funding_credit_minor") is not None
            else None
        ),
    )


@dataclass(frozen=True)
class PaperExecutionSnapshot:
    price_minor: int
    market_state: str
    captured_at: datetime
    source: str
    fx_snapshot: PaperFxSnapshot
    actual_market_state: str | None = None
    demo_override: bool = False

    def __post_init__(self) -> None:
        _minor(self.price_minor, "price_minor", positive=True)
        object.__setattr__(self, "market_state", normalize_market_state(self.market_state))
        if self.actual_market_state is not None:
            object.__setattr__(
                self, "actual_market_state", normalize_market_state(self.actual_market_state)
            )
        _aware(self.captured_at, "captured_at")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be non-empty")


@dataclass(frozen=True)
class PaperFillDecision:
    status: PaperFillDecisionStatus
    reason_code: str
    fill_price_minor: int | None = None
    funding_debit_minor: int | None = None
    fee_funding_minor: int | None = None
    funding_currency: str | None = None
    funding_credit_minor: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason_code": self.reason_code,
            "fill_price_minor": self.fill_price_minor,
            "funding_debit_minor": self.funding_debit_minor,
            "fee_funding_minor": self.fee_funding_minor,
            "funding_currency": self.funding_currency,
            "funding_credit_minor": self.funding_credit_minor,
        }


def _receipt_payload(
    draft: PaperEquityDraft,
    *,
    order_id: str,
    nonce: str,
    quote_consideration_minor: int,
    estimated_funding_minor: int,
    fee_funding_minor: int,
    max_quote_price_minor: int,
    max_funding_debit_minor: int,
    approval_expires_at: datetime,
    order_expires_at: datetime,
    min_funding_credit_minor: int | None = None,
) -> dict[str, Any]:
    payload = {
        "order_id": order_id,
        "nonce": nonce,
        "receipt_version": 1,
        "user_id": draft.user_id,
        "conversation_id": draft.conversation_id,
        "conversation_version": draft.conversation_version,
        "funding_account_id": draft.funding_account_id,
        "instrument": {
            "instrument_id": draft.instrument.instrument_id,
            "symbol": draft.instrument.symbol,
            "name": draft.instrument.name,
            "exchange": draft.instrument.exchange,
            "quote_currency": draft.instrument.quote_currency,
        },
        "quantity": draft.quantity,
        "allocation_minor": draft.allocation_minor,
        "allocation_currency": draft.allocation_currency,
        "funding_currency": draft.funding_currency,
        "order_type": draft.order_type.value,
        "limit_price_minor": draft.limit_price_minor,
        "time_in_force": draft.time_in_force.value,
        "quote_snapshot": {
            "price_minor": draft.quote_snapshot.price_minor,
            "market_state": draft.quote_snapshot.market_state,
            "captured_at": draft.quote_snapshot.captured_at.astimezone(timezone.utc).isoformat(),
            "source": draft.quote_snapshot.source,
        },
        "fx_snapshot": {
            "pair": draft.fx_snapshot.pair,
            "rate": str(draft.fx_snapshot.rate),
            "captured_at": draft.fx_snapshot.captured_at.astimezone(timezone.utc).isoformat(),
            "source": draft.fx_snapshot.source,
        },
        "quote_consideration_minor": quote_consideration_minor,
        "estimated_funding_minor": estimated_funding_minor,
        "fee_funding_minor": fee_funding_minor,
        "max_quote_price_minor": max_quote_price_minor,
        "max_funding_debit_minor": max_funding_debit_minor,
        "price_protection_basis_points": PRICE_PROTECTION_BP,
        "approval_expires_at": approval_expires_at.astimezone(timezone.utc).isoformat(),
        "order_expires_at": order_expires_at.astimezone(timezone.utc).isoformat(),
        "status": PaperOrderStatus.AWAITING_APPROVAL.value,
    }
    if draft.side == "SELL":
        payload["side"] = "SELL"
        payload["min_funding_credit_minor"] = min_funding_credit_minor
    return payload


def build_receipt(
    draft: PaperEquityDraft,
    *,
    order_id: str,
    nonce: str,
    now: datetime,
) -> PaperEquityReceipt:
    _aware(now, "now")
    if not isinstance(order_id, str) or not order_id.strip():
        raise ValueError("order_id must be non-empty")
    if not isinstance(nonce, str) or not nonce.strip():
        raise ValueError("nonce must be non-empty")

    quote_price = draft.quote_snapshot.price_minor
    quote_consideration = quote_price * draft.quantity
    if draft.side == "SELL":
        return _build_sell_receipt(draft, order_id=order_id, nonce=nonce, now=now)
    if draft.order_type is PaperOrderType.LIMIT:
        max_quote_price = draft.limit_price_minor
    else:
        protected = int(
            (Decimal(quote_price) * (Decimal(10_000 + PRICE_PROTECTION_BP) / Decimal(10_000)))
            .to_integral_value(rounding=ROUND_DOWN)
        )
        max_quote_price = protected
        if draft.allocation_is_cap:
            allocation_cap = draft.allocation_minor // draft.quantity
            max_quote_price = min(max_quote_price, allocation_cap)
    if max_quote_price is None or max_quote_price <= 0:
        raise ValueError("max_quote_price_minor must be positive")

    estimated_funding = _convert_minor(quote_consideration, draft.fx_snapshot.rate)
    max_funding = _convert_minor(max_quote_price * draft.quantity, draft.fx_snapshot.rate)
    fee = _fee_minor(estimated_funding)
    max_fee = _fee_minor(max_funding)
    approval_expires_at = now.astimezone(timezone.utc) + APPROVAL_TTL
    order_expires_at = _order_deadline(
        now,
        time_in_force=draft.time_in_force,
        exchange=draft.instrument.exchange,
        market_state=draft.quote_snapshot.market_state,
    )
    payload = _receipt_payload(
        draft,
        order_id=order_id,
        nonce=nonce,
        quote_consideration_minor=quote_consideration,
        estimated_funding_minor=estimated_funding,
        fee_funding_minor=fee,
        max_quote_price_minor=max_quote_price,
        max_funding_debit_minor=max_funding + max_fee,
        approval_expires_at=approval_expires_at,
        order_expires_at=order_expires_at,
    )
    canonical = canonical_json(payload)
    return PaperEquityReceipt(
        order_id=order_id,
        nonce=nonce,
        receipt_version=1,
        user_id=draft.user_id,
        conversation_id=draft.conversation_id,
        conversation_version=draft.conversation_version,
        funding_account_id=draft.funding_account_id,
        instrument=draft.instrument,
        quantity=draft.quantity,
        allocation_minor=draft.allocation_minor,
        allocation_currency=draft.allocation_currency,
        funding_currency=draft.funding_currency,
        order_type=draft.order_type,
        limit_price_minor=draft.limit_price_minor,
        time_in_force=draft.time_in_force,
        quote_snapshot=draft.quote_snapshot,
        fx_snapshot=draft.fx_snapshot,
        quote_consideration_minor=quote_consideration,
        estimated_funding_minor=estimated_funding,
        fee_funding_minor=fee,
        max_quote_price_minor=max_quote_price,
        max_funding_debit_minor=max_funding + max_fee,
        price_protection_basis_points=PRICE_PROTECTION_BP,
        approval_expires_at=approval_expires_at,
        order_expires_at=order_expires_at,
        status=PaperOrderStatus.AWAITING_APPROVAL,
        canonical_json=canonical,
        digest=digest_canonical(canonical),
    )


def _build_sell_receipt(
    draft: PaperEquityDraft,
    *,
    order_id: str,
    nonce: str,
    now: datetime,
) -> PaperEquityReceipt:
    """A sale: the quantity is fixed and the price has a floor. A limit sale
    never fills below its limit; a market sale fills at the live price."""

    quote_price = draft.quote_snapshot.price_minor
    quote_consideration = quote_price * draft.quantity
    if draft.order_type is PaperOrderType.LIMIT:
        floor_price = draft.limit_price_minor
    else:
        floor_price = int(
            (Decimal(quote_price) * (Decimal(10_000 - PRICE_PROTECTION_BP) / Decimal(10_000)))
            .to_integral_value(rounding=ROUND_DOWN)
        )
    if floor_price is None or floor_price <= 0:
        raise ValueError("price floor must be positive")
    estimated_funding = _convert_minor(quote_consideration, draft.fx_snapshot.rate)
    fee = _fee_minor(estimated_funding)
    least = _convert_minor(floor_price * draft.quantity, draft.fx_snapshot.rate)
    least_credit = max(0, least - _fee_minor(least))
    approval_expires_at = now.astimezone(timezone.utc) + APPROVAL_TTL
    order_expires_at = _order_deadline(
        now,
        time_in_force=draft.time_in_force,
        exchange=draft.instrument.exchange,
        market_state=draft.quote_snapshot.market_state,
    )
    payload = _receipt_payload(
        draft,
        order_id=order_id,
        nonce=nonce,
        quote_consideration_minor=quote_consideration,
        estimated_funding_minor=estimated_funding,
        fee_funding_minor=fee,
        max_quote_price_minor=floor_price,
        max_funding_debit_minor=0,
        approval_expires_at=approval_expires_at,
        order_expires_at=order_expires_at,
        min_funding_credit_minor=least_credit,
    )
    canonical = canonical_json(payload)
    return PaperEquityReceipt(
        order_id=order_id,
        nonce=nonce,
        receipt_version=1,
        user_id=draft.user_id,
        conversation_id=draft.conversation_id,
        conversation_version=draft.conversation_version,
        funding_account_id=draft.funding_account_id,
        instrument=draft.instrument,
        quantity=draft.quantity,
        allocation_minor=draft.allocation_minor,
        allocation_currency=draft.allocation_currency,
        funding_currency=draft.funding_currency,
        order_type=draft.order_type,
        limit_price_minor=draft.limit_price_minor,
        time_in_force=draft.time_in_force,
        quote_snapshot=draft.quote_snapshot,
        fx_snapshot=draft.fx_snapshot,
        quote_consideration_minor=quote_consideration,
        estimated_funding_minor=estimated_funding,
        fee_funding_minor=fee,
        max_quote_price_minor=floor_price,
        max_funding_debit_minor=0,
        price_protection_basis_points=PRICE_PROTECTION_BP,
        approval_expires_at=approval_expires_at,
        order_expires_at=order_expires_at,
        status=PaperOrderStatus.AWAITING_APPROVAL,
        canonical_json=canonical,
        digest=digest_canonical(canonical),
        side="SELL",
        min_funding_credit_minor=least_credit,
    )


def decide_fill(
    receipt: PaperEquityReceipt,
    execution: PaperExecutionSnapshot,
    *,
    now: datetime,
) -> PaperFillDecision:
    _aware(now, "now")
    if now.astimezone(timezone.utc) >= receipt.order_expires_at.astimezone(timezone.utc):
        return PaperFillDecision(PaperFillDecisionStatus.EXPIRED, "ORDER_EXPIRED")

    if execution.market_state == "UNKNOWN":
        return PaperFillDecision(
            PaperFillDecisionStatus.REJECTED,
            "MARKET_STATE_UNAVAILABLE",
        )

    if execution.market_state != "OPEN":
        status = (
            PaperFillDecisionStatus.QUEUED
            if receipt.order_type is PaperOrderType.MARKET
            else PaperFillDecisionStatus.OPEN
        )
        return PaperFillDecision(status, "MARKET_NOT_OPEN")

    if receipt.side == "SELL":
        if (
            receipt.order_type is PaperOrderType.LIMIT
            and execution.price_minor < (receipt.limit_price_minor or 0)
        ):
            return PaperFillDecision(PaperFillDecisionStatus.OPEN, "LIMIT_NOT_REACHED")
        if execution.price_minor < receipt.max_quote_price_minor:
            return PaperFillDecision(
                PaperFillDecisionStatus.REQUOTE_REQUIRED,
                "PRICE_MOVED_BEYOND_LIMIT",
            )
        proceeds = _convert_minor(execution.price_minor * receipt.quantity, execution.fx_snapshot.rate)
        fee = _fee_minor(proceeds)
        funding_credit = proceeds - fee
        if funding_credit <= 0:
            return PaperFillDecision(PaperFillDecisionStatus.REJECTED, "PROCEEDS_BELOW_FEE")
        if (
            receipt.min_funding_credit_minor is not None
            and funding_credit < receipt.min_funding_credit_minor
        ):
            return PaperFillDecision(
                PaperFillDecisionStatus.REQUOTE_REQUIRED,
                "FUNDING_MOVED_BEYOND_LIMIT",
            )
        return PaperFillDecision(
            PaperFillDecisionStatus.FILLED,
            "PAPER_FILL_ELIGIBLE",
            fill_price_minor=execution.price_minor,
            fee_funding_minor=fee,
            funding_currency=receipt.funding_currency,
            funding_credit_minor=funding_credit,
        )

    if (
        receipt.order_type is PaperOrderType.LIMIT
        and execution.price_minor > (receipt.limit_price_minor or 0)
    ):
        return PaperFillDecision(PaperFillDecisionStatus.OPEN, "LIMIT_NOT_REACHED")

    funding_minor = _convert_minor(execution.price_minor * receipt.quantity, execution.fx_snapshot.rate)
    fee = _fee_minor(funding_minor)
    total = funding_minor + fee
    # The signed receipt caps the funding debit for every order style. A market
    # order may accept price movement, but it may not spend beyond that bound.
    if total > receipt.max_funding_debit_minor:
        return PaperFillDecision(PaperFillDecisionStatus.REQUOTE_REQUIRED, "FUNDING_MOVED_BEYOND_LIMIT")
    return PaperFillDecision(
        PaperFillDecisionStatus.FILLED,
        "PAPER_FILL_ELIGIBLE",
        fill_price_minor=execution.price_minor,
        funding_debit_minor=total,
        fee_funding_minor=fee,
        funding_currency=receipt.funding_currency,
    )

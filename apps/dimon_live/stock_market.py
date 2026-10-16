"""Verified real-market research briefs for conversational stock requests.

Yahoo Finance is an indicative research source, never an executable broker
quote.  The provider boundary keeps every calculation deterministic and lets
tests use complete, timestamped fixtures without network access.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from difflib import SequenceMatcher
from typing import Protocol
from zoneinfo import ZoneInfo

from .grounding import (
    GroundingError,
    derive_amount_mentions_from_text,
    is_grounded,
    normalize_evidence,
)

logger = logging.getLogger(__name__)

_EXPLICIT_MARKET_ORDER = re.compile(
    r"\b(?:at|using|as)\s+(?:the\s+)?market(?:\s+order|\s+price)?\b|"
    r"\bmarket\s+order\b",
    re.IGNORECASE,
)


def has_explicit_market_order(text: str) -> bool:
    """True only when the request explicitly chooses market execution."""
    return bool(_EXPLICIT_MARKET_ORDER.search(text))


MONEY = Decimal("0.01")
PERCENT = Decimal("0.01")
DISCLAIMER = (
    "Indicative Yahoo Finance research data, not an executable broker quote or financial advice. "
    "Prices and foreign-exchange rates can be delayed or differ at execution."
)


class StockMarketError(RuntimeError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class RawInvestmentIntent:
    instrument_label: str
    allocation: Decimal | None
    explicit_currency: str | None
    bare_dollar: bool
    quantity: int | None = None
    objective: str = "GENERAL_ACQUISITION"


@dataclass(frozen=True)
class InstrumentCandidate:
    symbol: str
    name: str
    exchange: str
    instrument_type: str


@dataclass(frozen=True)
class ResolvedInstrument:
    symbol: str
    name: str
    exchange: str
    instrument_type: str
    quote_currency: str
    source: str


@dataclass(frozen=True)
class DailyBar:
    day: date
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class ProviderMarketData:
    price: Decimal
    market_state: str
    trailing_pe: Decimal | None
    bars: tuple[DailyBar, ...]
    day_high: Decimal | None = None
    day_low: Decimal | None = None


@dataclass(frozen=True)
class MarketDataSnapshot:
    captured_at: datetime
    source: str
    price: Decimal
    market_state: str
    trailing_pe: Decimal | None
    two_week_high: Decimal
    two_week_low: Decimal
    percent_below_two_week_high: Decimal
    percent_above_two_week_low: Decimal
    one_year_close: Decimal
    one_year_change_pct: Decimal


@dataclass(frozen=True)
class FxEstimate:
    pair: str
    rate: Decimal
    captured_at: datetime
    source: str
    converted_amount: Decimal
    warning: str


@dataclass(frozen=True)
class InvestmentBrief:
    instrument: ResolvedInstrument
    market: MarketDataSnapshot
    requested_allocation: Decimal
    allocation_currency: str
    allocation_assumption: str | None
    funding_currency: str
    fx_estimate: FxEstimate | None
    whole_shares: int
    estimated_spend: Decimal
    unallocated_cash: Decimal
    warnings: tuple[str, ...]
    next_question: str
    funding_account_label: str | None = None
    funding_account_id: str | None = None
    funding_balance: Decimal | None = None
    funding_required: Decimal | None = None
    funding_shortfall: Decimal | None = None
    requested_quantity: int | None = None

    def to_dict(self) -> dict:
        def convert(value):
            if isinstance(value, Decimal):
                return float(value)
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            if isinstance(value, tuple):
                return [convert(item) for item in value]
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            if isinstance(value, list):
                return [convert(item) for item in value]
            return value

        return convert(asdict(self))


class MarketProvider(Protocol):
    def search(self, label: str) -> tuple[InstrumentCandidate, ...]: ...
    def describe(self, symbol: str) -> ResolvedInstrument: ...
    def snapshot(self, symbol: str, captured_at: datetime) -> ProviderMarketData: ...
    def fx_estimate(
        self,
        base_currency: str,
        quote_currency: str,
        amount: Decimal,
        captured_at: datetime,
    ) -> FxEstimate: ...


_INVESTMENT_LANGUAGE = re.compile(
    r"\b(?:invest|investing|buy|purchase|acquire|put)\b", re.IGNORECASE
)
_RESEARCH_LANGUAGE = re.compile(
    r"\b(?:price|quote|trading|trade|worth|valuation|p/?e|market\s+status)\b",
    re.IGNORECASE,
)
_RECURRING_LANGUAGE = re.compile(
    r"\b(?:a|per|each|every)\s+month\b|\bmonthly\b", re.IGNORECASE
)
_ALLOCATION = re.compile(
    r"(?<![-\w])(?P<currency>US\$|USD|S\$|SGD|\$)\s*"
    r"(?P<amount>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?P<scale>[kKmM])?"
    r"(?![\w'’/\-]|,(?=\d)|\.(?=\d)|\s+(?:\d|[kKmM]\b))",
    re.IGNORECASE,
)
_CURRENCY_AMOUNT_MARKER = re.compile(
    r"(?<![\w$])(?:US\$|USD|S\$|SGD|\$)(?=\s*\d)", re.IGNORECASE
)
_INSTRUMENT_AFTER_AMOUNT = re.compile(
    r"\b(?:in|into)\s+(?P<label>.+?)\s*[.!?]*$", re.IGNORECASE
)
_INSTRUMENT_AFTER_ACTION = re.compile(
    r"\b(?:invest|investing|put|buy|purchase|acquire)\b.*?"
    r"\b(?:in|into)\s+(?P<label>[^.!?]+?)\s*[.!?]*$",
    re.IGNORECASE,
)
_INSTRUMENT_BEFORE_SHARES = re.compile(
    r"\b(?:buy|purchase|acquire)\s+(?:(?P<quantity>\d{1,3}(?:,\d{3})*|one|two|three|four|five|six|seven|eight|nine|ten)\s+)?(?P<label>.+?)\s+(?:shares?|stocks?)\b",
    re.IGNORECASE,
)
_QUANTITY_FIRST_INSTRUMENT = re.compile(
    r"\b(?:buy|purchase|acquire)\s+(?P<quantity>\d{1,3}(?:,\d{3})*|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:shares?|units?)\s+(?:of|in)\s+(?P<label>[^.!?]+?)\s*[.!?]*$",
    re.IGNORECASE,
)
_INSTRUMENT_WORTH_OF = re.compile(
    r"\b(?:buy|purchase|acquire)\s+.+?\b(?:dollars?|bucks?)\s+worth\s+of\s+"
    r"(?P<label>.+?)\s+(?:shares?|stock)\b",
    re.IGNORECASE,
)
_WORD_DOLLAR = re.compile(r"\b(?:dollars?|bucks?)\b", re.IGNORECASE)
_NON_STOCK_DESTINATIONS = {
    "account",
    "bank account",
    "checking",
    "checking account",
    "current account",
    "savings",
    "savings account",
}
_GENERIC_INSTRUMENT_SUFFIX = re.compile(
    r"\s+(?:(?:stock|stocks|share|shares|equity)(?:\s+(?:price|quote))?|price|quote)$",
    re.IGNORECASE,
)
_INSTRUMENT_CLAUSE_BOUNDARY = re.compile(
    r",\s+(?=(?:so|but|because|then|also|please|help|which|while)\b)",
    re.IGNORECASE,
)


def _clean_instrument_label(label: str) -> str:
    """Keep a company mention separate from a following prose clause."""

    cleaned = _INSTRUMENT_CLAUSE_BOUNDARY.split(label, maxsplit=1)[0]
    cleaned = cleaned.strip(" .!?,;:\t\n")
    while True:
        without_suffix = _GENERIC_INSTRUMENT_SUFFIX.sub("", cleaned).strip()
        if without_suffix == cleaned:
            return cleaned
        cleaned = without_suffix


def _parse_allocation(text: str) -> tuple[Decimal, str | None, bool] | None:
    amount_matches = tuple(_ALLOCATION.finditer(text))
    currency_markers = tuple(_CURRENCY_AMOUNT_MARKER.finditer(text))
    if amount_matches or currency_markers:
        if len(amount_matches) != 1 or len(currency_markers) != 1:
            return None
        amount_match = amount_matches[0]
        token = amount_match.group("currency").upper()
        explicit_currency = (
            "USD" if token in {"USD", "US$"} else "SGD" if token in {"SGD", "S$"} else None
        )
        allocation = Decimal(amount_match.group("amount").replace(",", ""))
        scale = (amount_match.group("scale") or "").casefold()
        if scale == "k":
            allocation *= 1_000
        elif scale == "m":
            allocation *= 1_000_000
        return allocation, explicit_currency, token == "$"

    try:
        mentions = derive_amount_mentions_from_text(text)
    except GroundingError:
        return None
    if len(mentions) != 1 or not _WORD_DOLLAR.search(mentions[0].evidence):
        return None
    return Decimal(mentions[0].amount_minor) / 100, None, True


_QUANTITY_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_QUANTITY_SIZE_REPLY = re.compile(
    r"\b(?:\d{1,6}|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"\s+(?:whole\s+)?(?:shares?|stocks?)\b",
    re.IGNORECASE,
)


def _quantity_match(text: str) -> re.Match[str] | None:
    return _QUANTITY_FIRST_INSTRUMENT.search(text) or _INSTRUMENT_BEFORE_SHARES.search(text)


def _parse_quantity(text: str) -> int | None:
    match = _quantity_match(text)
    if match is None or not match.groupdict().get("quantity"):
        return None
    token = match.group("quantity").casefold()
    quantity = _QUANTITY_WORDS.get(token)
    if quantity is None:
        quantity = int(token.replace(",", ""))
    return quantity if quantity > 0 else None


def parse_size_evidence(
    kind: str, evidence: str
) -> tuple[Decimal | None, str | None, bool, int | None] | None:
    """Parse a Qwen-grounded size phrase without relying on stock-verb syntax."""

    if kind == "ALLOCATION":
        parsed = _parse_allocation(evidence)
        if parsed is None or parsed[0] <= 0:
            return None
        return parsed[0], parsed[1], parsed[2], None
    if kind == "QUANTITY":
        match = re.fullmatch(
            r"\s*(?P<quantity>\d{1,6}|one|two|three|four|five|six|seven|eight|nine|ten)"
            r"\s+(?:whole\s+)?(?:share|shares|stock|stocks)\b"
            r"(?:\s+(?:of|in)\s+[A-Za-z][A-Za-z0-9&.' -]*)?\s*[,!.]?\s*",
            evidence,
            re.IGNORECASE,
        )
        if match is None:
            return None
        token = match.group("quantity").casefold()
        quantity = _QUANTITY_WORDS.get(token)
        if quantity is None:
            quantity = int(token.replace(",", ""))
        return (None, None, False, quantity) if quantity > 0 else None
    return None


def extract_quantity_evidence(text: str) -> tuple[int, str] | None:
    """Return one unambiguous share-count phrase from a follow-up reply."""

    matches = tuple(_QUANTITY_SIZE_REPLY.finditer(text))
    if len(matches) != 1:
        return None
    evidence = matches[0].group(0)
    parsed = parse_size_evidence("QUANTITY", evidence)
    if parsed is None or parsed[3] is None:
        return None
    return parsed[3], evidence


def raw_intent_from_model_size(
    label: str, objective: str, model_intent: dict
) -> RawInvestmentIntent | None:
    """Convert only validated model size evidence into the existing quote input."""

    kind = model_intent.get("size_kind")
    if kind == "UNKNOWN":
        return None
    evidence = (
        model_intent.get("quantity_evidence")
        if kind == "QUANTITY"
        else model_intent.get("allocation_evidence")
    )
    if not isinstance(evidence, str):
        return None
    parsed = parse_size_evidence(kind, evidence)
    if parsed is None:
        return None
    allocation, explicit_currency, bare_dollar, quantity = parsed
    if kind == "QUANTITY" and model_intent.get("quantity") != quantity:
        return None
    return RawInvestmentIntent(
        instrument_label=label,
        allocation=allocation,
        explicit_currency=explicit_currency,
        bare_dollar=bare_dollar,
        quantity=quantity,
        objective=objective,
    )


def extract_direct_stock_label(text: str) -> str | None:
    match = (
        _INSTRUMENT_AFTER_AMOUNT.search(text)
        or _INSTRUMENT_AFTER_ACTION.search(text)
        or _INSTRUMENT_WORTH_OF.search(text)
        or _QUANTITY_FIRST_INSTRUMENT.search(text)
        or _INSTRUMENT_BEFORE_SHARES.search(text)
    )
    if match is None:
        return None
    label = _clean_instrument_label(match.group("label"))
    label = re.sub(r"^the\s+", "", label, flags=re.IGNORECASE)
    if not label or len(label) > 120:
        return None
    normalized_label = " ".join(re.findall(r"[a-z0-9]+", label.casefold()))
    if normalized_label in _NON_STOCK_DESTINATIONS:
        return None
    return label


def looks_like_stock_request(text: str) -> bool:
    """Cheap routing hint; it does not attempt to identify the instrument."""

    return bool(_INVESTMENT_LANGUAGE.search(text) or _RESEARCH_LANGUAGE.search(text))


def extract_model_stock_label(text: str, model_intent: dict) -> tuple[str, str] | None:
    """Accept only a grounded company mention from the stock model."""

    if model_intent.get("kind") not in {"STOCK_INVESTMENT", "STOCK_RESEARCH"}:
        return None
    label = model_intent.get("instrument_label")
    evidence = model_intent.get("instrument_evidence")
    if not isinstance(label, str) or not isinstance(evidence, str):
        return None
    if not is_grounded(text, evidence):
        return None
    if normalize_evidence(label) not in normalize_evidence(evidence):
        return None
    cleaned = _clean_instrument_label(label)
    if not cleaned or len(cleaned) > 120:
        return None
    direct_label = extract_direct_stock_label(text)
    if direct_label is not None:
        direct_normalized = _normalized(direct_label)
        label_normalized = _normalized(cleaned)
        if direct_normalized and f" {direct_normalized} " in f" {label_normalized} ":
            cleaned = direct_label
    objective = model_intent.get("objective", "GENERAL_ACQUISITION")
    if objective not in {"GENERAL_ACQUISITION", "DIVIDEND_INCOME"}:
        objective = "GENERAL_ACQUISITION"
    return cleaned, objective


def parse_raw_investment_intent(
    text: str,
    *,
    instrument_label: str | None = None,
    objective: str = "GENERAL_ACQUISITION",
) -> RawInvestmentIntent | None:
    """Recognise a one-time stock brief request with an allocation or quantity.

    The ticker or company may appear before or after the amount. Recurring
    plans and requests without an allocation or quantity intentionally use
    another path.
    """

    if not _INVESTMENT_LANGUAGE.search(text) or _RECURRING_LANGUAGE.search(text):
        return None
    parsed_allocation = _parse_allocation(text)
    quantity = _parse_quantity(text)
    label = instrument_label or extract_direct_stock_label(text)
    if label is None or (parsed_allocation is None and quantity is None):
        return None
    label = re.sub(
        r"\s+(?:today|now)(?:\s+please)?$", "", label, flags=re.IGNORECASE
    ).strip(" .!?\t\n")
    allocation, explicit_currency, bare_dollar = parsed_allocation or (None, None, False)
    if allocation is not None and allocation <= 0:
        return None
    return RawInvestmentIntent(
        instrument_label=label,
        allocation=allocation,
        explicit_currency=explicit_currency,
        bare_dollar=bare_dollar,
        quantity=quantity,
        objective=objective,
    )


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _supported_exchange(exchange: str) -> bool:
    normalized = exchange.strip().upper()
    return normalized in {"NMS", "NGM", "NCM", "NYQ", "NYSE", "NASDAQ", "SGX", "SES"} or any(
        marker in normalized for marker in ("NASDAQ", "NEW YORK", "SINGAPORE")
    )


def resolve_candidate(label: str, candidates: tuple[InstrumentCandidate, ...]) -> InstrumentCandidate:
    equities = tuple(
        candidate
        for candidate in candidates
        if candidate.instrument_type.upper() == "EQUITY" and _supported_exchange(candidate.exchange)
    )
    if not equities:
        raise StockMarketError("INSTRUMENT_NOT_FOUND")
    wanted = _normalized(label)

    def score(candidate: InstrumentCandidate) -> float:
        symbol = _normalized(candidate.symbol)
        name = _normalized(candidate.name)
        exchange = candidate.exchange.upper()
        primary_exchange = 25 if _supported_exchange(exchange) else 0
        if wanted == symbol or wanted == name:
            return 100.0 + primary_exchange
        similarity = SequenceMatcher(None, wanted, name).ratio() * 60
        wanted_tokens = set(wanted.split())
        name_tokens = set(name.split())
        overlap = len(wanted_tokens & name_tokens) / max(1, len(wanted_tokens)) * 35
        prefix = 10 if name.startswith(wanted) or wanted.startswith(name) else 0
        return similarity + overlap + prefix + primary_exchange

    ranked = sorted(((score(candidate), candidate) for candidate in equities), key=lambda item: item[0], reverse=True)
    best_score, best = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0
    if best_score < 50:
        raise StockMarketError("INSTRUMENT_NOT_FOUND")
    if runner_up >= best_score - 8:
        raise StockMarketError("INSTRUMENT_AMBIGUOUS")
    return best


def _search_and_resolve_candidate(
    label: str,
    provider: MarketProvider,
    *,
    request_id: str | None,
) -> InstrumentCandidate:
    try:
        candidates = provider.search(label)
    except Exception as exc:
        logger.warning(json.dumps({
            "event": "stock_instrument_search_failed",
            "request_id": request_id,
            "provider": type(provider).__name__,
            "instrument_label": label[:120],
            "error_type": type(exc).__name__,
            "reason_code": getattr(exc, "reason_code", "SEARCH_FAILED"),
        }, sort_keys=True, separators=(",", ":")))
        raise

    candidates = tuple(candidates)
    candidate_summary = [
        {
            "symbol": candidate.symbol[:24],
            "name": candidate.name[:120],
            "exchange": candidate.exchange[:48],
            "instrument_type": candidate.instrument_type[:24],
        }
        for candidate in candidates[:8]
    ]
    try:
        selected = resolve_candidate(label, candidates)
    except StockMarketError as exc:
        logger.warning(json.dumps({
            "event": "stock_instrument_resolution_failed",
            "request_id": request_id,
            "provider": type(provider).__name__,
            "instrument_label": label[:120],
            "candidate_count": len(candidates),
            "candidates": candidate_summary,
            "reason_code": exc.reason_code,
        }, sort_keys=True, separators=(",", ":")))
        raise
    logger.info(json.dumps({
        "event": "stock_instrument_resolved",
        "request_id": request_id,
        "provider": type(provider).__name__,
        "instrument_label": label[:120],
        "candidate_count": len(candidates),
        "candidates": candidate_summary,
        "selected_symbol": selected.symbol,
    }, sort_keys=True, separators=(",", ":")))
    return selected


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _percent(value: Decimal) -> Decimal:
    return value.quantize(PERCENT, rounding=ROUND_HALF_UP)


def _previous_year(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def _exchange_zone(exchange: str) -> ZoneInfo:
    normalized = exchange.upper()
    if "NASDAQ" in normalized or "NYSE" in normalized or normalized in {"NMS", "NGM", "NCM", "NYQ"}:
        return ZoneInfo("America/New_York")
    if "SINGAPORE" in normalized or normalized in {"SGX", "SES"}:
        return ZoneInfo("Asia/Singapore")
    return ZoneInfo("UTC")


def _scheduled_market_state(exchange: str, captured_at: datetime) -> str:
    """Return a conservative session fallback when Yahoo omits marketState.

    This is not a quote source and does not attempt holiday discovery. It only
    prevents a missing Yahoo session label from becoming an unexplained
    ``MARKET_STATE_UNAVAILABLE`` result: outside regular weekday hours a
    market order is safely queued, while a weekday inside regular hours may be
    checked again against the provider before settlement.
    """

    normalized = (exchange or "").upper()
    if "NASDAQ" in normalized or "NYSE" in normalized or normalized in {
        "NMS", "NGM", "NCM", "NYQ"
    }:
        zone = ZoneInfo("America/New_York")
        opening, closing = time(9, 30), time(16, 0)
    elif "SINGAPORE" in normalized or normalized in {"SGX", "SES"}:
        zone = ZoneInfo("Asia/Singapore")
        opening, closing = time(9, 0), time(17, 0)
    else:
        return "UNKNOWN"
    local = captured_at.astimezone(zone)
    if local.weekday() >= 5:
        return "CLOSED"
    return "OPEN" if opening <= local.time() < closing else "CLOSED"


def _positive_finite(value: Decimal | None) -> bool:
    return value is not None and value.is_finite() and value > 0


def _market_snapshot(
    instrument: ResolvedInstrument,
    provider_data: ProviderMarketData,
    captured_at: datetime,
) -> MarketDataSnapshot:
    if not _positive_finite(provider_data.price):
        raise StockMarketError("MARKET_DATA_UNAVAILABLE")
    current_day = captured_at.astimezone(_exchange_zone(instrument.exchange)).date()
    # Inclusive dates: today plus the preceding 13 calendar dates is exactly
    # the 14-calendar-day window described to the user.
    window_start = current_day - timedelta(days=13)
    valid_bars = [
        bar
        for bar in provider_data.bars
        if all(_positive_finite(value) for value in (bar.high, bar.low, bar.close))
    ]
    recent = [bar for bar in valid_bars if window_start <= bar.day <= current_day]
    if not recent:
        raise StockMarketError("MARKET_HISTORY_UNAVAILABLE")
    target = _previous_year(current_day)
    comparisons = [bar for bar in valid_bars if bar.day <= target]
    if not comparisons:
        raise StockMarketError("MARKET_HISTORY_UNAVAILABLE")
    comparison = max(comparisons, key=lambda bar: bar.day)
    price = provider_data.price
    highs = [bar.high for bar in recent] + [price]
    lows = [bar.low for bar in recent] + [price]
    if _positive_finite(provider_data.day_high):
        highs.append(provider_data.day_high)
    if _positive_finite(provider_data.day_low):
        lows.append(provider_data.day_low)
    high = max(highs)
    low = min(lows)
    trailing_pe = provider_data.trailing_pe
    if trailing_pe is not None and (not trailing_pe.is_finite() or trailing_pe <= 0):
        trailing_pe = None
    return MarketDataSnapshot(
        captured_at=captured_at,
        source=instrument.source,
        price=_money(price),
        market_state=_normalise_market_state(provider_data.market_state),
        trailing_pe=_percent(trailing_pe) if trailing_pe is not None else None,
        two_week_high=_money(high),
        two_week_low=_money(low),
        percent_below_two_week_high=_percent((high - price) / high * 100),
        percent_above_two_week_low=_percent((price - low) / low * 100),
        one_year_close=_money(comparison.close),
        one_year_change_pct=_percent((price - comparison.close) / comparison.close * 100),
    )


def build_investment_brief(
    intent: RawInvestmentIntent,
    provider: MarketProvider,
    *,
    captured_at: datetime | None = None,
    funding_currency: str = "SGD",
    request_id: str | None = None,
) -> InvestmentBrief:
    captured_at = captured_at or datetime.now(timezone.utc)
    candidate = _search_and_resolve_candidate(
        intent.instrument_label, provider, request_id=request_id
    )
    instrument = provider.describe(candidate.symbol)
    if (
        instrument.instrument_type.upper() != "EQUITY"
        or not instrument.quote_currency
        or not _supported_exchange(instrument.exchange)
    ):
        raise StockMarketError("UNSUPPORTED_INSTRUMENT")
    market = _market_snapshot(instrument, provider.snapshot(instrument.symbol, captured_at), captured_at)
    allocation_currency = intent.explicit_currency or instrument.quote_currency
    requested_allocation = intent.allocation
    if requested_allocation is None:
        if intent.quantity is None:
            raise StockMarketError("REAL_STOCK_ALLOCATION_REQUIRED")
        requested_allocation = _money(market.price * intent.quantity)
        allocation_currency = instrument.quote_currency
    assumption = None
    if intent.bare_dollar and intent.allocation is not None:
        allocation_label = f"{intent.allocation:,.2f}".removesuffix(".00")
        assumption = (
            f"I'm treating your ${allocation_label} allocation as "
            f"{_currency_symbol(instrument.quote_currency)}{allocation_label} because "
            f"{instrument.symbol} trades in {instrument.quote_currency}."
        )

    available_quote = requested_allocation
    conversion = None
    if allocation_currency != instrument.quote_currency:
        conversion = provider.fx_estimate(
            allocation_currency, instrument.quote_currency, requested_allocation, captured_at
        )
        available_quote = conversion.converted_amount
    elif funding_currency != instrument.quote_currency:
        conversion = provider.fx_estimate(
            instrument.quote_currency, funding_currency, requested_allocation, captured_at
        )

    shares = (
        intent.quantity
        if intent.quantity is not None
        else int((available_quote / market.price).to_integral_value(rounding=ROUND_DOWN))
    )
    spend = _money(market.price * shares)
    remainder = _money(available_quote - spend)
    return InvestmentBrief(
        instrument=instrument,
        market=market,
        requested_allocation=_money(requested_allocation),
        allocation_currency=allocation_currency,
        allocation_assumption=assumption,
        funding_currency=funding_currency,
        fx_estimate=conversion,
        whole_shares=shares,
        estimated_spend=spend,
        unallocated_cash=remainder,
        warnings=(DISCLAIMER,),
        next_question=(
            "Buy now at market, or set a maximum price?"
            if market.market_state == "OPEN"
            else f"Buy at market when {_exchange_label(instrument.exchange)} opens, or set a maximum price?"
        ),
        requested_quantity=intent.quantity,
    )


def assess_funding(
    brief: InvestmentBrief,
    *,
    account_label: str,
    account_id: str | None = None,
    account_currency: str,
    balance: Decimal,
) -> InvestmentBrief:
    """Attach a deterministic funding check without discarding provider provenance."""

    account_currency = account_currency.upper()
    if account_currency != brief.funding_currency:
        return brief
    required = None
    if brief.allocation_currency == account_currency:
        required = brief.requested_allocation
    elif (
        brief.fx_estimate is not None
        and brief.fx_estimate.pair == f"{brief.allocation_currency}/{account_currency}"
    ):
        required = brief.fx_estimate.converted_amount
    if required is None:
        return brief
    balance = _money(balance)
    required = _money(required)
    shortfall = _money(max(required - balance, Decimal("0")))
    return replace(
        brief,
        funding_account_label=account_label,
        funding_account_id=account_id,
        funding_balance=balance,
        funding_required=required,
        funding_shortfall=shortfall,
        next_question="Lower the amount?" if shortfall > 0 else brief.next_question,
    )


def _currency_symbol(currency: str) -> str:
    return {"USD": "US$", "SGD": "S$"}.get(currency, f"{currency} ")


def _amount_label(currency: str, amount: Decimal) -> str:
    return f"{_currency_symbol(currency)}{amount:,.2f}"


def _exchange_label(exchange: str) -> str:
    lowered = exchange.casefold()
    if "nasdaq" in lowered:
        return "Nasdaq"
    if "nyse" in lowered:
        return "NYSE"
    if "singapore" in lowered or exchange.upper() in {"SES", "SGX"}:
        return "SGX"
    return exchange


def _normalise_market_state(state: str) -> str:
    """Map Yahoo's session names to the small vocabulary used by the brief."""

    normalized = (state or "UNKNOWN").strip().upper().replace("-", "_")
    return {
        "REGULAR": "OPEN",
        "PRE": "PRE_MARKET",
        "PREPRE": "PRE_MARKET",
        "PREMARKET": "PRE_MARKET",
        "POST": "AFTER_HOURS",
        "POSTPOST": "AFTER_HOURS",
        "POSTMARKET": "AFTER_HOURS",
    }.get(normalized, normalized)


def _market_session_copy(exchange: str, state: str) -> tuple[str, str | None]:
    """Return natural-language session text and an optional order caveat."""

    if state == "OPEN":
        return f"{exchange} is currently trading.", None
    if state == "PRE_MARKET":
        return f"{exchange} is currently in pre-market.", "A market order would wait until the open."
    if state == "AFTER_HOURS":
        return f"{exchange} is in after-hours trading.", "A market order would wait until the next open."
    if state == "CLOSED":
        return f"{exchange} is currently closed.", "A market order would wait until it reopens."
    return f"{exchange} status unavailable; market order must wait.", None


def build_stock_research(
    instrument_label: str,
    provider: MarketProvider,
    *,
    captured_at: datetime | None = None,
    request_id: str | None = None,
) -> tuple[ResolvedInstrument, MarketDataSnapshot]:
    """Resolve one instrument and return read-only verified market research."""

    captured_at = captured_at or datetime.now(timezone.utc)
    candidate = _search_and_resolve_candidate(
        instrument_label, provider, request_id=request_id
    )
    instrument = provider.describe(candidate.symbol)
    if (
        instrument.instrument_type.upper() != "EQUITY"
        or not instrument.quote_currency
        or not _supported_exchange(instrument.exchange)
    ):
        raise StockMarketError("UNSUPPORTED_INSTRUMENT")
    market = _market_snapshot(
        instrument,
        provider.snapshot(instrument.symbol, captured_at),
        captured_at,
    )
    return instrument, market


def stock_research_response(
    instrument: ResolvedInstrument,
    market: MarketDataSnapshot,
) -> str:
    price = _amount_label(instrument.quote_currency, market.price)
    exchange = _exchange_label(instrument.exchange)
    market_copy, _ = _market_session_copy(
        exchange, _normalise_market_state(market.market_state)
    )
    pe = (
        f" Its P/E ratio is {market.trailing_pe:.1f} times."
        if market.trailing_pe is not None
        else ""
    )
    direction = "up" if market.one_year_change_pct >= 0 else "down"
    return (
        f"{instrument.symbol} is {price}. {market_copy}{pe} "
        f"It is {direction} {abs(market.one_year_change_pct):.1f}% from one year ago."
    )


def stock_research_to_dict(
    instrument: ResolvedInstrument,
    market: MarketDataSnapshot,
) -> dict:
    return {
        "instrument": asdict(instrument),
        "market": {
            key: (
                float(value)
                if isinstance(value, Decimal)
                else value.isoformat()
                if isinstance(value, (date, datetime))
                else value
            )
            for key, value in asdict(market).items()
        },
    }


def brief_response(brief: InvestmentBrief) -> str:
    instrument = brief.instrument
    market = brief.market
    price = _amount_label(instrument.quote_currency, market.price)
    if (
        brief.funding_shortfall is not None
        and brief.funding_shortfall > 0
        and brief.funding_required is not None
        and brief.funding_balance is not None
        and brief.funding_account_label is not None
    ):
        if brief.requested_quantity is not None:
            purchase = f"Buying {brief.requested_quantity:,} whole share" + (
                "" if brief.requested_quantity == 1 else "s"
            )
        else:
            purchase = (
                f"Your {_amount_label(brief.allocation_currency, brief.requested_allocation).removesuffix('.00')} allocation"
            )
        required = _amount_label(brief.funding_currency, brief.funding_required)
        balance = _amount_label(brief.funding_currency, brief.funding_balance)
        shortfall = _amount_label(brief.funding_currency, brief.funding_shortfall)
        return (
            f"{instrument.symbol} is {price}. {purchase} needs about "
            f"{required}, but your {brief.funding_account_label} balance is {balance}. "
            f"You're short {shortfall}. {brief.next_question}"
        )
    exchange = _exchange_label(instrument.exchange)
    market_copy, order_caveat = _market_session_copy(
        exchange, _normalise_market_state(market.market_state)
    )
    if order_caveat:
        market_copy += f" {order_caveat}"
    pe = (
        f"P/E: {market.trailing_pe:.1f}x."
        if market.trailing_pe is not None
        else "P/E unavailable."
    )
    direction = "up" if market.one_year_change_pct >= 0 else "down"
    year_copy = f"It's {direction} {abs(market.one_year_change_pct):.1f}% from one year ago."
    assumption = ""
    if brief.allocation_assumption:
        allocation = f"${brief.requested_allocation:,.2f}".removesuffix(".00")
        assumption = f" I treated your {allocation} as {brief.allocation_currency}."
    if brief.requested_quantity is not None:
        sizing = (
            f" Buying {brief.whole_shares:,} whole share"
            f"{'s' if brief.whole_shares != 1 else ''} costs "
            f"{_amount_label(instrument.quote_currency, brief.estimated_spend)}"
        )
        if brief.fx_estimate is not None:
            sizing += (
                f" (about {_amount_label(brief.funding_currency, brief.fx_estimate.converted_amount)})"
            )
        sizing += "."
    else:
        conversion = "That allocation"
        if brief.fx_estimate is not None:
            estimate = brief.fx_estimate
            conversion = (
                f"That allocation is about "
                f"{_amount_label(estimate.pair.split('/')[1], estimate.converted_amount)} and"
            )
        else:
            conversion = "That"
        sizing = (
            f" {conversion} buys {brief.whole_shares:,} whole share{'s' if brief.whole_shares != 1 else ''} for "
            f"{_amount_label(instrument.quote_currency, brief.estimated_spend)}, leaving "
            f"{_amount_label(instrument.quote_currency, brief.unallocated_cash)}."
        )
    funding = ""
    if brief.funding_shortfall is not None and brief.funding_shortfall > 0:
        funding = (
            f" Your {brief.funding_account_label} balance is "
            f"{_amount_label(brief.funding_currency, brief.funding_balance)}, so you're short "
            f"{_amount_label(brief.funding_currency, brief.funding_shortfall)}."
        )
    return (
        f"{instrument.symbol} is {price}. {market_copy} {pe} {year_copy}"
        f"{assumption}{sizing}{funding} {brief.next_question}"
    )


class YFinanceMarketProvider:
    """Thin yfinance adapter; imported lazily so application startup stays offline."""

    def _yf(self):
        try:
            import yfinance as yf
        except ImportError as exc:
            raise StockMarketError("MARKET_PROVIDER_UNAVAILABLE") from exc
        return yf

    def search(self, label: str) -> tuple[InstrumentCandidate, ...]:
        try:
            quotes = self._yf().Search(
                label, max_results=8, news_count=0, enable_fuzzy_query=True
            ).quotes
        except Exception as exc:
            raise StockMarketError("INSTRUMENT_SEARCH_UNAVAILABLE") from exc
        results = []
        for quote in quotes or ():
            symbol = quote.get("symbol")
            name = quote.get("longname") or quote.get("shortname")
            if not symbol or not name:
                continue
            results.append(
                InstrumentCandidate(
                    symbol=str(symbol).upper(),
                    name=str(name),
                    exchange=str(quote.get("exchDisp") or quote.get("exchange") or "Unknown"),
                    instrument_type=str(quote.get("quoteType") or "UNKNOWN").upper(),
                )
            )
        return tuple(results)

    def describe(self, symbol: str) -> ResolvedInstrument:
        try:
            info = self._yf().Ticker(symbol).get_info()
        except Exception as exc:
            raise StockMarketError("MARKET_DATA_UNAVAILABLE") from exc
        name = info.get("longName") or info.get("shortName")
        currency = info.get("currency")
        if not name or not currency:
            raise StockMarketError("MARKET_DATA_UNAVAILABLE")
        return ResolvedInstrument(
            symbol=symbol,
            name=str(name),
            exchange=str(info.get("fullExchangeName") or info.get("exchange") or "Unknown"),
            instrument_type=str(info.get("quoteType") or "EQUITY").upper(),
            quote_currency=str(currency).upper(),
            source="Yahoo Finance",
        )

    def snapshot(self, symbol: str, captured_at: datetime) -> ProviderMarketData:
        try:
            ticker = self._yf().Ticker(symbol)
            info = ticker.get_info()
            history = ticker.history(period="13mo", interval="1d", auto_adjust=False)
        except Exception as exc:
            raise StockMarketError("MARKET_DATA_UNAVAILABLE") from exc
        def finite_decimal(value) -> Decimal | None:
            if value is None:
                return None
            try:
                parsed = Decimal(str(value))
            except Exception:
                return None
            return parsed if parsed.is_finite() else None

        price = finite_decimal(info.get("currentPrice") or info.get("regularMarketPrice"))
        if price is None and not history.empty:
            price = finite_decimal(history.iloc[-1]["Close"])
        if price is None or price <= 0:
            raise StockMarketError("MARKET_DATA_UNAVAILABLE")
        bars = []
        for index, row in history.iterrows():
            values = (row.get("High"), row.get("Low"), row.get("Close"))
            if any(value is None or not math.isfinite(float(value)) for value in values):
                continue
            bars.append(
                DailyBar(
                    index.date(),
                    Decimal(str(values[0])),
                    Decimal(str(values[1])),
                    Decimal(str(values[2])),
                )
            )
        trailing = finite_decimal(info.get("trailingPE"))
        market_state = str(info.get("marketState") or "").upper()
        if not market_state or market_state == "UNKNOWN":
            market_state = _scheduled_market_state(
                str(info.get("fullExchangeName") or info.get("exchange") or ""),
                captured_at,
            )
        return ProviderMarketData(
            price=price,
            market_state=market_state,
            trailing_pe=trailing,
            bars=tuple(bars),
            day_high=finite_decimal(info.get("dayHigh") or info.get("regularMarketDayHigh")),
            day_low=finite_decimal(info.get("dayLow") or info.get("regularMarketDayLow")),
        )

    def fx_estimate(
        self,
        base_currency: str,
        quote_currency: str,
        amount: Decimal,
        captured_at: datetime,
    ) -> FxEstimate:
        pair = f"{base_currency}{quote_currency}=X"
        try:
            ticker = self._yf().Ticker(pair)
            info = ticker.get_info()
            rate_value = info.get("currentPrice") or info.get("regularMarketPrice")
            if rate_value is None:
                history = ticker.history(period="5d", interval="1d")
                rate_value = None if history.empty else history.iloc[-1]["Close"]
            rate = Decimal(str(rate_value))
        except Exception as exc:
            raise StockMarketError("FX_DATA_UNAVAILABLE") from exc
        if not rate.is_finite() or rate <= 0:
            raise StockMarketError("FX_DATA_UNAVAILABLE")
        return FxEstimate(
            pair=f"{base_currency}/{quote_currency}",
            rate=rate,
            captured_at=captured_at,
            source="Yahoo Finance",
            converted_amount=_money(amount * rate),
            warning="Indicative only; your broker's executed rate can differ.",
        )

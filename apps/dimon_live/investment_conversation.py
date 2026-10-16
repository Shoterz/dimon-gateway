"""Investment-domain conversation adapter for the verified stock brief flow.

The adapter owns only clarification state: which stock, buy or sell, how
many shares, market or limit, and for how long. Plain answers ("yes", "five
shares", "230", "just today", "all of them") are understood by fixed rules so
the conversation keeps working when the language model is busy; the model
is asked only about replies the rules can't place. Execution, approvals and
receipts happen elsewhere, from the READY state this adapter produces.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Mapping

from .conversation import (
    ConversationInput,
    ConversationRejected,
    ConversationStatus,
    ConversationTransition,
    StartContext,
    TurnContext,
)
from .model_adapter import FollowUpIntent, InferenceFailure
from . import stock_market, stock_request
from .stock_market import InvestmentBrief, brief_response


_MONEY = Decimal("0.01")
_PERCENT = re.compile(r"(?P<value>\d+(?:\.\d{1,2})?)\s*(?:%|percent|per\s+cent)", re.IGNORECASE)
_ABSOLUTE = re.compile(
    r"(?<![\w.])(?:US\$|USD|S\$|SGD|\$)?\s*(?P<value>\d+(?:\.\d{1,2})?)(?![\w.])",
    re.IGNORECASE,
)
_MARKET_BUY_PHRASES = {
    "buy now",
    "buy it now",
    "market",
    "market order",
    "buy now at market",
    "buy at market when market opens",
    "buy at market when the market opens",
    "buy at market when it opens",
    "buy at market on open",
    "buy now when market opens",
    "just buy now",
    "just buy now at market",
}
_FOLLOW_UP_ACTIONS = frozenset(
    {
        "BUY_NOW",
        "SET_LIMIT",
        "CANCEL",
        "USE_AVAILABLE_FUNDS",
        "CHANGE_FUNDING_ACCOUNT",
        "DAY",
        "GTC",
        "UNKNOWN",
    }
)

# Plain replies, matched against the whole (normalised) answer.
_CANCEL_REPLY = re.compile(
    r"(?:no|nope|nah|cancel|cancel (?:it|that|this|the order)|stop|never ?mind|forget it|don'?t|"
    r"no thanks|no thank you|don'?t (?:buy|sell)(?: it)?|leave it|not now|abort)"
)
_GO_REPLY = re.compile(
    r"(?:(?:ok|okay|yes|yeah|yep|yup|sure|alright|right|fine|great|good|correct|please|go|"
    r"go ahead|proceed|confirm|do it|place it|place (?:the|my|an?) order|let'?s go|let'?s do it|sounds good|"
    r"that'?s (?:fine|right|good)|(?:buy|sell)(?: it| them| the shares)?|now|right now|immediately|"
    r"(?:at )?(?:the )?market(?: price| order)?|at the current price|when (?:it|the market) opens|"
    r"(?:buy|sell) (?:it |them )?(?:now|at market|at the market price))[ ,.!]*)+"
)
_LIMIT_WORD = re.compile(
    r"\b(?:limit|max(?:imum)?|at most|no more than|not more than|up to|cap|ceiling|min(?:imum)?|at least|"
    r"no less than|not less than|floor|lowest|highest)\b|\b(?:at|for)\s+(?:us\$|s\$|\$)?\d"
)
_DAY_REPLY = re.compile(
    r"(?:(?:just |only |for )?(?:today|the day|day)(?: only)?|day order|end of (?:the )?day|"
    r"until (?:the )?close|until (?:the )?market closes|today'?s session)"
)
_GTC_REPLY = re.compile(
    r"(?:gtc|good (?:till|til|until) cancel+ed|(?:until|till|til) (?:i )?cancel+(?:ed)?(?: it)?|"
    r"(?:until|till) (?:it'?s )?filled|(?:until|till) it fills|keep it open|leave it open|"
    r"keep (?:it )?(?:going|open)|(?:for )?(?:30|thirty) days|(?:for )?a month|no expiry|open|"
    r"as long as (?:it takes|possible))"
)
_USE_FUNDS_REPLY = re.compile(
    r"(?:yes|yeah|yep|ok|okay|sure|fine|go ahead|please|"
    r"use (?:it|that|the balance|the available(?: balance| funds| money)?|what(?:ever)? i (?:have|can)|"
    r"all of it|my (?:available )?(?:balance|savings|money)|available(?: balance| funds)?)|"
    r"as much as (?:i|we) can|max|maximum|(?:lower|reduce|adjust) (?:it|the amount)|whatever i (?:have|can afford)|"
    r"buy what i can(?: afford)?|buy as many as i can)(?: please)?"
)
_SIZE_NOISE = frozenset(
    "actually make it just only please then instead how about what about let's lets do go with ok okay "
    "yes yeah i want would like i'd id to buy sell get of them it the shares share stock stocks whole "
    "and dollars dollar bucks worth in sgd usd us s singapore all half everything my".split()
)


def _decimal(value: object, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ConversationRejected("INVESTMENT_STATE_INVALID") from exc
    if not parsed.is_finite():
        raise ConversationRejected("INVESTMENT_STATE_INVALID")
    return parsed


def _money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY, rounding=ROUND_HALF_UP)


def _amount(value: Decimal) -> str:
    return f"{_money(value):.2f}"


def _label(currency: str, value: Decimal | str) -> str:
    symbol = {"USD": "US$", "SGD": "S$"}.get(str(currency).upper(), f"{currency} ")
    return f"{symbol}{_money(Decimal(str(value))):,.2f}"


def _name(state: Mapping) -> str:
    """The company name mid-sentence: "Tesla, Inc", never "Tesla, Inc.." at a full stop."""

    return str(state.get("name") or state.get("symbol") or "the company").rstrip(".")


def _shares(count: int) -> str:
    return f"{count:,} share" + ("" if count == 1 else "s")


def _normalise(text: str) -> str:
    return " ".join(text.casefold().replace("’", "'").split()).strip(" .!?,")


def sell_state(
    *,
    instrument: stock_market.ResolvedInstrument,
    market: stock_market.MarketDataSnapshot,
    fx_rate: Decimal,
    fx_pair: str | None,
    fx_source: str | None,
    fx_captured_at: str | None,
    held_quantity: int,
    funding_account_id: str = "acct_savings_01",
    funding_account_label: str = "savings",
) -> dict:
    """The conversation state for selling shares the user holds."""

    return {
        "phase": "SIZE",
        "side": "SELL",
        "instrument_id": f"YF:{instrument.symbol}",
        "instrument_label": instrument.symbol,
        "symbol": instrument.symbol,
        "name": instrument.name,
        "exchange": instrument.exchange,
        "quote_currency": instrument.quote_currency,
        "price": _amount(market.price),
        "market_state": market.market_state,
        "quote_source": market.source,
        "quote_captured_at": market.captured_at.isoformat(),
        "trailing_pe": _amount(market.trailing_pe) if market.trailing_pe is not None else None,
        "two_week_high": _amount(market.two_week_high),
        "two_week_low": _amount(market.two_week_low),
        "one_year_change_pct": _amount(market.one_year_change_pct),
        "requested_allocation": "0.00",
        "allocation_currency": instrument.quote_currency,
        "requested_quantity": None,
        "funding_currency": "SGD",
        "fx_rate": str(fx_rate),
        "fx_pair": fx_pair,
        "fx_source": fx_source,
        "fx_captured_at": fx_captured_at,
        "funding_account_id": funding_account_id,
        "funding_account_label": funding_account_label,
        "funding_balance": None,
        "funding_required": None,
        "funding_shortfall": None,
        "held_quantity": held_quantity,
        "whole_shares": 0,
        "estimated_spend": "0.00",
        "unallocated_cash": "0.00",
        "limit_price": None,
        "order_type": None,
        "time_in_force": None,
    }


class InvestmentConversationAdapter:
    domain = "INVESTMENT"
    name = "verified-stock-order"
    version = 2

    def __init__(
        self,
        followup_interpreter: Any | None = None,
        *,
        stock_provider: Any | None = None,
        database: Any | None = None,
    ):
        self.followup_interpreter = followup_interpreter
        self.stock_provider = stock_provider
        self.database = database

    # ------------------------------------------------------------ start
    def start(self, context: StartContext) -> ConversationTransition:
        if not isinstance(context.payload, Mapping):
            raise ConversationRejected("INVESTMENT_PAYLOAD_INVALID")
        selling = context.payload.get("sell")
        if selling is not None:
            return self._start_sell(dict(selling), context.payload.get("size"))
        brief = context.payload.get("brief")
        if brief is None:
            stock_request = context.payload.get("stock_request")
            if (
                not isinstance(stock_request, Mapping)
                or not isinstance(stock_request.get("instrument_label"), str)
                or not stock_request.get("instrument_label")
            ):
                raise ConversationRejected("INVESTMENT_PAYLOAD_INVALID")
            state = {
                "phase": "SIZE",
                "instrument_label": stock_request["instrument_label"],
                "objective": stock_request.get("objective", "GENERAL_ACQUISITION"),
            }
            question = (
                f"How many shares of {state['instrument_label']} would you like, "
                "or what amount would you like to invest?"
            )
            return self._transition(
                state,
                answer=question,
                question=question,
                choices=("CANCEL",),
                status=ConversationStatus.WAITING,
                outcome="SIZE_REQUESTED",
            )
        if not isinstance(brief, InvestmentBrief):
            raise ConversationRejected("INVESTMENT_PAYLOAD_INVALID")
        state = self._state_from_brief(brief)
        if brief.whole_shares < 1:
            return self._less_than_one_share(state, brief)
        explicit_market_order = context.payload.get("initial_order_type") == "MARKET"
        if explicit_market_order and not self._has_shortfall(state):
            state["phase"] = "READY"
            state["order_type"] = "MARKET"
            state["time_in_force"] = "DAY"
            question = None
            choices = ()
            status = ConversationStatus.READY_FOR_DRAFT
            outcome = "READY_FOR_DRAFT"
            answer = (
                f"{brief_response(brief)} Market order draft prepared for review. "
                "No order was created."
            )
        elif self._has_shortfall(state):
            question, choices = self._funding_question(state)
            state["phase"] = "FUNDING"
            status = ConversationStatus.WAITING
            outcome = "WAITING"
            answer = brief_response(brief)
        else:
            question = "Buy now at market, or set a maximum price?"
            choices = ("BUY_NOW", "SET_LIMIT", "CANCEL")
            state["phase"] = "ORDER_STYLE"
            status = ConversationStatus.WAITING
            outcome = "WAITING"
            answer = brief_response(brief)
        return self._transition(
            state,
            answer=answer,
            question=question,
            choices=choices,
            status=status,
            outcome=outcome,
        )

    def _start_sell(self, state: dict, size: Mapping | None) -> ConversationTransition:
        if state.get("side") != "SELL" or not state.get("symbol"):
            raise ConversationRejected("INVESTMENT_PAYLOAD_INVALID")
        held = int(state.get("held_quantity") or 0)
        if size:
            parsed = stock_request.Size(
                quantity=size.get("quantity"),
                allocation=Decimal(str(size["allocation"])) if size.get("allocation") is not None else None,
                explicit_currency=size.get("explicit_currency"),
                bare_dollar=bool(size.get("bare_dollar")),
                fraction=size.get("fraction"),
            )
            return self._size_sell(state, parsed, opening=True)
        question = f"You have {_shares(held)} of {_name(state)}. How many would you like to sell?"
        return self._transition(
            state,
            answer=f"{self._sell_quote_line(state)} {question}",
            question=question,
            choices=("SELL_ALL", "CANCEL"),
            status=ConversationStatus.WAITING,
            outcome="SIZE_REQUESTED",
        )

    # ------------------------------------------------------------ turns
    def apply_turn(
        self,
        state_json: str,
        conversation_input: ConversationInput,
        context: TurnContext,
    ) -> ConversationTransition:
        state = self._decode_state(state_json)
        phase = state.get("phase")
        selling = state.get("side") == "SELL"
        choice = (conversation_input.choice or "").upper()
        text = (conversation_input.text or "").strip()
        normalized = _normalise(text)
        interpreted_natural_language = False
        interpreted = None
        allowed_actions = self._allowed_actions_for_phase(phase)
        follow_up_actions: tuple[str, ...] = ()

        # 1. Plain answers, by rule: no model call, no waiting.
        if not choice and text:
            plain = self._plain_reply(phase, text, normalized, state)
            if plain is not None:
                kind, value = plain
                if kind == "SIZE":
                    return self._resize(state, value, context)
                if kind == "LIMIT":
                    return self._limit_set(state, value)
                choice = kind

        # 2. Anything else is put to the follow-up model, if there is one.
        if not choice and text and self.followup_interpreter is not None:
            try:
                interpreted = self.followup_interpreter.interpret(
                    text,
                    phase=phase,
                    question=self._question_for_phase(phase, state),
                    state=state,
                )
                proposed_actions = getattr(interpreted, "actions", ())
                if (
                    isinstance(proposed_actions, tuple)
                    and proposed_actions
                    and all(action in _FOLLOW_UP_ACTIONS for action in proposed_actions)
                ):
                    interpreted_natural_language = True
                    follow_up_actions = proposed_actions
                    candidate = proposed_actions[0]
                    if candidate in allowed_actions:
                        choice = candidate
                    else:
                        choice = self._recover_bounded_choice(phase, text) or candidate
            except InferenceFailure:
                # The rules below are the fallback when the model is busy.
                interpreted = None

        # 3. Wider rules for longer sentences.
        if not choice and text:
            choice = self._recover_bounded_choice(phase, text) or ""
        if (not choice or choice == "UNKNOWN") and text and phase in {"SIZE", "FUNDING", "ORDER_STYLE"}:
            size = self._size_in(text, phase, selling, loose=True)
            if size is not None and (interpreted is None or getattr(interpreted, "size_kind", "UNKNOWN") == "UNKNOWN"):
                return self._resize(state, size, context)

        if choice == "CANCEL" or normalized in {"cancel", "stop", "no"}:
            state["phase"] = "CANCELLED"
            return self._transition(
                state,
                answer=(
                    "Okay, I cancelled this sale. Nothing was sold."
                    if selling else "Okay, I cancelled this investment conversation. No order was created."
                ),
                question=None,
                choices=(),
                status=ConversationStatus.CANCELLED,
                outcome="CANCELLED",
            )

        if phase == "SIZE":
            if choice == "SELL_ALL" and selling:
                return self._resize(state, stock_request.Size(fraction="ALL"), context)
            if interpreted is not None and getattr(interpreted, "size_kind", "UNKNOWN") in {"QUANTITY", "ALLOCATION"} and not selling:
                return self._build_sized_brief(state, interpreted, context)
            if selling:
                return self._reask(
                    state,
                    f"How many {_name(state)} shares would you like to sell? You have {int(state.get('held_quantity') or 0):,}.",
                    ("SELL_ALL", "CANCEL"),
                )
            return self._reask(
                state,
                "How many shares, or how much would you like to invest? For example: 5 shares, or 500 dollars.",
                ("CANCEL",),
            )

        if phase == "FUNDING":
            if choice == "USE_AVAILABLE_FUNDS":
                self._use_available_funds(state)
                if int(state["whole_shares"]) < 1:
                    state["phase"] = "CANCELLED"
                    return self._transition(
                        state,
                        answer=(
                            f"Your available {_label(state['funding_currency'], state['funding_balance'])} isn't enough "
                            f"for one {_name(state)} share at {_label(state['quote_currency'], state['price'])}. "
                            "Nothing was ordered."
                        ),
                        question=None,
                        choices=(),
                        status=ConversationStatus.CANCELLED,
                        outcome="CANCELLED",
                    )
                if follow_up_actions[1:] == ("BUY_NOW",):
                    state["phase"] = "READY"
                    state["order_type"] = "MARKET"
                    state["time_in_force"] = "DAY"
                    return self._transition(
                        state,
                        answer=(
                            f"{self._funding_repaired_answer(state)} "
                            "Market order draft prepared for review. No order was created."
                        ),
                        question=None,
                        choices=(),
                        status=ConversationStatus.READY_FOR_DRAFT,
                        outcome="READY_FOR_DRAFT",
                    )
                if follow_up_actions[1:] == ("SET_LIMIT",):
                    state["phase"] = "LIMIT_PRICE"
                    return self._transition(
                        state,
                        answer=(
                            f"{self._funding_repaired_answer(state)} "
                            "What maximum price should I use?"
                        ),
                        question="What maximum price should I use?",
                        choices=("CANCEL",),
                        status=ConversationStatus.WAITING,
                        outcome="LIMIT_PRICE_REQUESTED",
                    )
                state["phase"] = "ORDER_STYLE"
                return self._transition(
                    state,
                    answer=f"{self._funding_repaired_answer(state)} Buy now at market, or set a maximum price?",
                    question="Buy now at market, or set a maximum price?",
                    choices=("BUY_NOW", "SET_LIMIT", "CANCEL"),
                    status=ConversationStatus.WAITING,
                    outcome="FUNDING_REPAIRED",
                )
            if choice == "CHANGE_FUNDING_ACCOUNT":
                question, choices = self._funding_question(state)
                return self._transition(
                    state,
                    answer=(
                        f"Shares are paid for from your {state.get('funding_account_label') or 'savings'} account; "
                        f"there's no other account to use. {question}"
                    ),
                    question=question,
                    choices=choices,
                    status=ConversationStatus.WAITING,
                    outcome="FUNDING_ACCOUNT_REQUESTED",
                    advance_version=False,
                )
            question, choices = self._funding_question(state)
            return self._reask(
                state,
                f"{question} You can also say a smaller number of shares or amount.",
                choices,
            )

        if phase == "ORDER_STYLE":
            if choice == "BUY_NOW" or (
                not interpreted_natural_language and normalized in _MARKET_BUY_PHRASES
            ):
                return self._ready_market(state)
            if choice == "SET_LIMIT":
                state["phase"] = "LIMIT_PRICE"
                question = self._question_for_phase("LIMIT_PRICE", state)
                return self._transition(
                    state,
                    answer=(
                        f"Okay. {question} {_name(state)} is {_label(state['quote_currency'], state['price'])} now. "
                        + ("You can say a price or a percentage above it." if selling
                           else "You can say a price or a percentage below the current price.")
                    ),
                    question=question,
                    choices=("CANCEL",),
                    status=ConversationStatus.WAITING,
                    outcome="LIMIT_PRICE_REQUESTED",
                )
            return self._reask(
                state,
                "Should I sell now at market, or set a minimum price? Or say cancel."
                if selling else "Should I buy now at market, or set a maximum price? Or say cancel.",
                ("BUY_NOW", "SET_LIMIT", "CANCEL"),
            )

        if phase == "LIMIT_PRICE":
            if choice == "BUY_NOW":
                return self._ready_market(state)
            parsed_limit = self._parse_limit(
                text, _decimal(state["price"], field="price")
            )
            if parsed_limit is None:
                return self._reask(
                    state,
                    "Please say the lowest price you'll accept, such as 230 dollars or 5% above the current price."
                    if selling else
                    "Please give a maximum price, such as 230 dollars or 5% below the current price.",
                    ("CANCEL",),
                )
            return self._limit_set(state, parsed_limit)

        if phase == "TIME_IN_FORCE":
            if choice in {"DAY", "GTC"} or normalized in {
                "day",
                "today",
                "gtc",
                "good till cancelled",
                "good till canceled",
            }:
                tif = (
                    "GTC"
                    if choice == "GTC"
                    or normalized
                    in {"gtc", "good till cancelled", "good till canceled"}
                    else "DAY"
                )
                state["time_in_force"] = tif
                state["order_type"] = "LIMIT"
                state["phase"] = "READY"
                verb = "Limit sell order" if selling else "Limit order"
                return self._transition(
                    state,
                    answer=f"{verb} draft prepared at {state['quote_currency']} {state['limit_price']} ({tif}). No order was created.",
                    question=None,
                    choices=(),
                    status=ConversationStatus.READY_FOR_DRAFT,
                    outcome="READY_FOR_DRAFT",
                )
            return self._reask(
                state,
                "Should the order last just for today, or stay open until it fills or you cancel it?",
                ("DAY", "GTC", "CANCEL"),
            )

        raise ConversationRejected("CONVERSATION_TERMINAL")

    # ------------------------------------------------------------ plain replies
    def _size_in(self, text: str, phase: object, selling: bool, *, loose: bool = False) -> stock_request.Size | None:
        try:
            size = stock_request.parse_size(text, allow_bare_count=phase == "SIZE")
        except ValueError:
            return None
        if size is None:
            return None
        if size.fraction and not selling and not (size.quantity or size.allocation):
            return None
        if not loose:
            # A plain answer is only the size: "5 shares", "500 dollars",
            # "make it 3", "all of them". Longer sentences go to the model.
            rest = text.casefold()
            for word in (size.evidence or "").casefold().split():
                rest = rest.replace(word, " ", 1)
            leftover = [w for w in re.findall(r"[a-z']+", rest) if w not in _SIZE_NOISE]
            if leftover:
                return None
        return size

    def _plain_reply(self, phase: object, text: str, normalized: str, state: dict) -> tuple[str, Any] | None:
        selling = state.get("side") == "SELL"
        if _CANCEL_REPLY.fullmatch(normalized):
            return "CANCEL", None
        if phase in {"SIZE", "FUNDING", "ORDER_STYLE"}:
            size = self._size_in(text, phase, selling)
            if size is not None:
                return "SIZE", size
        if phase == "FUNDING" and _USE_FUNDS_REPLY.fullmatch(normalized):
            return "USE_AVAILABLE_FUNDS", None
        if phase == "ORDER_STYLE":
            if _LIMIT_WORD.search(normalized):
                price = self._parse_limit(text, _decimal(state["price"], field="price"))
                return ("LIMIT", price) if price is not None else ("SET_LIMIT", None)
            if re.search(r"\b(?:set|choose|pick) (?:a |my )?price\b", normalized):
                return "SET_LIMIT", None
            # "Sell" never confirms a purchase, nor "buy" a sale.
            wrong_verb = r"\bbuy" if selling else r"\bsell"
            if _GO_REPLY.fullmatch(normalized) and not re.search(wrong_verb, normalized):
                return "BUY_NOW", None
        if phase == "LIMIT_PRICE":
            if re.fullmatch(r"(?:at |just )?(?:the )?market(?: price| order)?|(?:buy|sell) (?:it )?now|now", normalized):
                return "BUY_NOW", None
            price = self._parse_limit(text, _decimal(state["price"], field="price"))
            if price is not None:
                return "LIMIT", price
        if phase == "TIME_IN_FORCE":
            if _GTC_REPLY.fullmatch(normalized):
                return "GTC", None
            if _DAY_REPLY.fullmatch(normalized):
                return "DAY", None
        return None

    # ------------------------------------------------------------ sizing
    def _resize(self, state: dict, size: stock_request.Size, context: TurnContext) -> ConversationTransition:
        if state.get("side") == "SELL":
            return self._size_sell(state, size)
        if not (size.quantity or size.allocation):
            return self._reask(
                state,
                "How many shares, or how much would you like to invest?",
                ("CANCEL",),
            )
        label = state.get("symbol") or state.get("instrument_label") or state.get("name")
        intent = stock_market.RawInvestmentIntent(
            instrument_label=str(label),
            allocation=size.allocation,
            explicit_currency=size.explicit_currency,
            bare_dollar=size.bare_dollar,
            quantity=size.quantity if size.allocation is None else None,
            objective=state.get("objective", "GENERAL_ACQUISITION"),
        )
        return self._quote(state, intent, context)

    def _size_sell(self, state: dict, size: stock_request.Size, *, opening: bool = False) -> ConversationTransition:
        held = int(state.get("held_quantity") or 0)
        price = _decimal(state["price"], field="price")
        rate = _decimal(state.get("fx_rate") or "1", field="fx_rate")
        if size.fraction == "ALL" and not size.quantity:
            quantity = held
        elif size.fraction == "HALF" and not size.quantity:
            quantity = max(1, held // 2)
        elif size.quantity:
            quantity = size.quantity
        elif size.allocation:
            amount = size.allocation
            if size.explicit_currency and size.explicit_currency != state["quote_currency"] and rate > 0:
                amount = amount / rate      # S$ amount of a US$ stock
            quantity = int((amount / price).to_integral_value(rounding=ROUND_DOWN))
            if quantity < 1:
                question = f"That's less than one share at {_label(state['quote_currency'], price)}. How many shares would you like to sell?"
                return self._reask(state, question, ("SELL_ALL", "CANCEL")) if not opening else self._transition(
                    state, answer=question, question=question, choices=("SELL_ALL", "CANCEL"),
                    status=ConversationStatus.WAITING, outcome="SIZE_REQUESTED")
        else:
            quantity = 0
        if quantity < 1 or quantity > held:
            question = (
                f"You have {_shares(held)} of {_name(state)}, so you can sell up to {held:,}. How many would you like to sell?"
                if quantity > held else f"How many {_name(state)} shares would you like to sell?"
            )
            if opening:
                return self._transition(
                    state, answer=f"{self._sell_quote_line(state)} {question}", question=question,
                    choices=("SELL_ALL", "CANCEL"), status=ConversationStatus.WAITING, outcome="SIZE_REQUESTED",
                )
            return self._reask(state, question, ("SELL_ALL", "CANCEL"))
        value = _money(price * quantity)
        state.update(
            phase="ORDER_STYLE",
            whole_shares=quantity,
            requested_quantity=quantity,
            requested_allocation=_amount(value),
            estimated_spend=_amount(value),
            unallocated_cash="0.00",
        )
        proceeds = _money(value * rate)
        market_line = stock_market._market_session_copy(
            stock_market._exchange_label(state["exchange"]),
            stock_market._normalise_market_state(state.get("market_state") or "UNKNOWN"),
        )
        caveat = f" {market_line[1]}" if market_line[1] else ""
        question = "Sell now at market, or set a minimum price?"
        funding = state["funding_currency"]
        about = (
            f" (about {_label(funding, proceeds)})" if funding != state["quote_currency"] else ""
        )
        answer = (
            f"{_name(state)} is {_label(state['quote_currency'], price)}.{caveat} "
            f"Selling {_shares(quantity)} of your {held:,} brings in {_label(state['quote_currency'], value)}{about} "
            f"before the fee, paid into your {state.get('funding_account_label') or 'savings'}. {question}"
        )
        return self._transition(
            state,
            answer=answer,
            question=question,
            choices=("BUY_NOW", "SET_LIMIT", "CANCEL"),
            status=ConversationStatus.WAITING,
            outcome="SIZE_CONFIRMED",
        )

    @staticmethod
    def _sell_quote_line(state: dict) -> str:
        session, caveat = stock_market._market_session_copy(
            stock_market._exchange_label(state["exchange"]),
            stock_market._normalise_market_state(state.get("market_state") or "UNKNOWN"),
        )
        return f"{_name(state)} is {_label(state['quote_currency'], state['price'])}. {session}"

    def _less_than_one_share(self, state: dict, brief: InvestmentBrief) -> ConversationTransition:
        price = _label(brief.instrument.quote_currency, brief.market.price)
        if brief.fx_estimate is not None and brief.funding_currency != brief.instrument.quote_currency and brief.requested_allocation:
            rate = _decimal(brief.fx_estimate.converted_amount, field="fx") / brief.requested_allocation
            if brief.allocation_currency == brief.instrument.quote_currency:
                price += f" (about {_label(brief.funding_currency, brief.market.price * rate)})"
        amount = _label(brief.allocation_currency, brief.requested_allocation)
        question = f"How many shares, or how much would you like to invest?"
        state = {
            "phase": "SIZE",
            "instrument_label": brief.instrument.symbol,
            "objective": state.get("objective", "GENERAL_ACQUISITION"),
        }
        return self._transition(
            state,
            answer=(
                f"{amount} isn't enough for one whole share of {brief.instrument.name}: one share is {price}. {question}"
            ),
            question=question,
            choices=("CANCEL",),
            status=ConversationStatus.WAITING,
            outcome="SIZE_REQUESTED",
        )

    def _quote(self, state: dict, intent: stock_market.RawInvestmentIntent, context: TurnContext) -> ConversationTransition:
        if self.stock_provider is None or self.database is None:
            return self._reask(
                state,
                "I can’t verify the quote right now. Please give the share count or amount again in a moment.",
                ("CANCEL",),
            )
        try:
            brief = stock_market.build_investment_brief(
                intent, self.stock_provider, captured_at=context.now
            )
            accounts = self.database.get_state().get("accounts", [])
            source = next(
                (account for account in accounts if account.get("is_user_source")),
                None,
            )
            if source is not None:
                brief = stock_market.assess_funding(
                    brief,
                    account_label=str(source["label"]),
                    account_id=str(source["account_id"]),
                    account_currency=str(source["currency"]),
                    balance=Decimal(int(source["balance_minor"])) / 100,
                )
        except (stock_market.StockMarketError, KeyError, TypeError, ValueError):
            return self._reask(
                state,
                "I couldn’t verify the quote or available balance. Please try that investment size again, or cancel.",
                ("CANCEL",),
            )
        quoted_state = self._state_from_brief(brief)
        quoted_state["objective"] = state.get("objective", "GENERAL_ACQUISITION")
        if brief.whole_shares < 1:
            return self._less_than_one_share(quoted_state, brief)
        if self._has_shortfall(quoted_state):
            question, choices = self._funding_question(quoted_state)
            quoted_state["phase"] = "FUNDING"
        else:
            question = "Buy now at market, or set a maximum price?"
            choices = ("BUY_NOW", "SET_LIMIT", "CANCEL")
            quoted_state["phase"] = "ORDER_STYLE"
        return self._transition(
            quoted_state,
            answer=brief_response(brief),
            question=question,
            choices=choices,
            status=ConversationStatus.WAITING,
            outcome="SIZE_CONFIRMED",
        )

    def _build_sized_brief(
        self, state: dict, interpreted: Any, context: TurnContext
    ) -> ConversationTransition:
        if self.stock_provider is None or self.database is None:
            return self._reask(
                state,
                "I can’t verify the quote right now. Please give the share count or amount again in a moment.",
                ("CANCEL",),
            )
        instrument_label = state.get("instrument_label") or state.get("name")
        if not isinstance(instrument_label, str) or not instrument_label.strip():
            return self._reask(
                state,
                "I couldn’t identify the stock in this request. Please restate the share count, or cancel.",
                ("CANCEL",),
            )
        proposed = {
            "size_kind": interpreted.size_kind,
            "quantity": interpreted.quantity,
            "quantity_evidence": interpreted.quantity_evidence,
            "allocation_evidence": interpreted.allocation_evidence,
        }
        intent = stock_market.raw_intent_from_model_size(
            instrument_label,
            state.get("objective", "GENERAL_ACQUISITION"),
            proposed,
        )
        if intent is None:
            return self._reask(
                state,
                "I couldn’t verify that amount or share count. Please say it again, or cancel.",
                ("CANCEL",),
            )
        return self._quote(state, intent, context)

    # ------------------------------------------------------------ order style
    def _ready_market(self, state: dict) -> ConversationTransition:
        state["phase"] = "READY"
        state["order_type"] = "MARKET"
        state["time_in_force"] = "DAY"
        answer = (
            "Market sell order ready for your review. Nothing has been sold yet."
            if state.get("side") == "SELL"
            else "Market order draft prepared for the next eligible session. No order was created."
        )
        return self._transition(
            state,
            answer=answer,
            question=None,
            choices=(),
            status=ConversationStatus.READY_FOR_DRAFT,
            outcome="READY_FOR_DRAFT",
        )

    def _limit_set(self, state: dict, limit: Decimal) -> ConversationTransition:
        price = _decimal(state["price"], field="price")
        selling = state.get("side") == "SELL"
        if not (price * Decimal("0.5") <= limit <= price * Decimal("1.5")):
            question = self._question_for_phase("LIMIT_PRICE", state)
            state["phase"] = "LIMIT_PRICE"
            return self._transition(
                state,
                answer=(
                    f"{_label(state['quote_currency'], limit)} is far from the current price of "
                    f"{_label(state['quote_currency'], price)}. {question}"
                ),
                question=question,
                choices=("CANCEL",),
                status=ConversationStatus.WAITING,
                outcome="LIMIT_PRICE_REQUESTED",
            )
        state["limit_price"] = _amount(limit)
        state["phase"] = "TIME_IN_FORCE"
        kind = "Minimum" if selling else "Limit"
        return self._transition(
            state,
            answer=f"{kind} price set at {state['quote_currency']} {state['limit_price']}.",
            question="Should the limit order be valid for today or stay open until cancelled?",
            choices=("DAY", "GTC", "CANCEL"),
            status=ConversationStatus.WAITING,
            outcome="LIMIT_PRICE_SET",
        )

    def _funding_question(self, state: dict) -> tuple[str, tuple[str, ...]]:
        sources = []
        if self.database is not None:
            try:
                sources = [
                    account for account in self.database.get_state().get("accounts", [])
                    if account.get("is_user_source") and account.get("currency") == state.get("funding_currency")
                ]
            except Exception:
                sources = []
        if len(sources) > 1:
            return (
                "Use the available balance, choose another funding account, or cancel?",
                ("USE_AVAILABLE_FUNDS", "CHANGE_FUNDING_ACCOUNT", "CANCEL"),
            )
        return (
            "Should I buy as many shares as your balance allows, or cancel?",
            ("USE_AVAILABLE_FUNDS", "CANCEL"),
        )

    # ------------------------------------------------------------ public view
    def render_public(self, state_json: str) -> dict:
        state = self._decode_state(state_json)
        if state.get("phase") == "SIZE" and not state.get("symbol"):
            return {
                "instrument_label": state["instrument_label"],
                "objective": state.get("objective", "GENERAL_ACQUISITION"),
                "phase": "SIZE",
            }
        public = {
            "side": state.get("side", "BUY"),
            "phase": state.get("phase"),
            "symbol": state["symbol"],
            "name": state["name"],
            "exchange": state["exchange"],
            "quote_currency": state["quote_currency"],
            "price": state["price"],
            "trailing_pe": state.get("trailing_pe"),
            "two_week_high": state["two_week_high"],
            "two_week_low": state["two_week_low"],
            "one_year_change_pct": state["one_year_change_pct"],
            "requested_allocation": state["requested_allocation"],
            "allocation_currency": state["allocation_currency"],
            "funding_currency": state["funding_currency"],
            "requested_quantity": state.get("requested_quantity"),
            "whole_shares": state["whole_shares"],
            "estimated_spend": state["estimated_spend"],
            "unallocated_cash": state["unallocated_cash"],
        }
        if state.get("held_quantity") is not None:
            public["held_quantity"] = state["held_quantity"]
        if state.get("funding_account_label"):
            public["funding_account_label"] = state["funding_account_label"]
        if state.get("funding_balance") is not None:
            public["funding_balance"] = state["funding_balance"]
        if state.get("funding_shortfall") is not None:
            public["funding_shortfall"] = state["funding_shortfall"]
        if state.get("limit_price") is not None:
            public["limit_price"] = state["limit_price"]
        if state.get("order_type") is not None:
            public["order_type"] = state["order_type"]
        if state.get("time_in_force") is not None:
            public["time_in_force"] = state["time_in_force"]
        return public

    def _state_from_brief(self, brief: InvestmentBrief) -> dict:
        fx_rate = None
        if brief.fx_estimate is not None and brief.requested_allocation:
            fx_rate = (
                _decimal(brief.fx_estimate.converted_amount, field="fx")
                / brief.requested_allocation
            )
        return {
            "phase": "ORDER_STYLE",
            "instrument_id": f"YF:{brief.instrument.symbol}",
            "symbol": brief.instrument.symbol,
            "name": brief.instrument.name,
            "exchange": brief.instrument.exchange,
            "quote_currency": brief.instrument.quote_currency,
            "price": _amount(brief.market.price),
            "market_state": brief.market.market_state,
            "quote_source": brief.market.source,
            "quote_captured_at": brief.market.captured_at.isoformat(),
            "trailing_pe": (
                _amount(brief.market.trailing_pe)
                if brief.market.trailing_pe is not None
                else None
            ),
            "two_week_high": _amount(brief.market.two_week_high),
            "two_week_low": _amount(brief.market.two_week_low),
            "one_year_change_pct": _amount(brief.market.one_year_change_pct),
            "requested_allocation": _amount(brief.requested_allocation),
            "allocation_currency": brief.allocation_currency,
            "requested_quantity": brief.requested_quantity,
            "funding_currency": brief.funding_currency,
            "fx_rate": str(fx_rate) if fx_rate is not None else None,
            "fx_pair": brief.fx_estimate.pair if brief.fx_estimate is not None else None,
            "fx_source": brief.fx_estimate.source if brief.fx_estimate is not None else None,
            "fx_captured_at": (
                brief.fx_estimate.captured_at.isoformat()
                if brief.fx_estimate is not None
                else None
            ),
            "funding_account_id": brief.funding_account_id or "acct_savings_01",
            "funding_account_label": brief.funding_account_label,
            "funding_balance": (
                _amount(brief.funding_balance)
                if brief.funding_balance is not None
                else None
            ),
            "funding_required": (
                _amount(brief.funding_required)
                if brief.funding_required is not None
                else None
            ),
            "funding_shortfall": (
                _amount(brief.funding_shortfall)
                if brief.funding_shortfall is not None
                else None
            ),
            "whole_shares": brief.whole_shares,
            "estimated_spend": _amount(brief.estimated_spend),
            "unallocated_cash": _amount(brief.unallocated_cash),
            "limit_price": None,
            "order_type": None,
            "time_in_force": None,
        }

    @staticmethod
    def _decode_state(state_json: str) -> dict:
        try:
            state = json.loads(state_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConversationRejected("INVESTMENT_STATE_INVALID") from exc
        if not isinstance(state, dict) or not (
            state.get("symbol")
            or state.get("phase") == "SIZE" and state.get("instrument_label")
        ):
            raise ConversationRejected("INVESTMENT_STATE_INVALID")
        return state

    def _transition(
        self,
        state: dict,
        *,
        answer: str,
        question: str | None,
        choices: tuple[str, ...],
        status: ConversationStatus,
        outcome: str,
        advance_version: bool = True,
    ) -> ConversationTransition:
        return ConversationTransition(
            state=state,
            status=status,
            outcome=outcome,
            answer=answer,
            question=question,
            allowed_choices=choices,
            public_view=self.render_public(json.dumps(state)),
            advance_version=advance_version,
        )

    def _reask(
        self, state: dict, question: str, choices: tuple[str, ...]
    ) -> ConversationTransition:
        return self._transition(
            state,
            answer=question,
            question=question,
            choices=choices,
            status=ConversationStatus.WAITING,
            outcome="REASKED",
            advance_version=False,
        )

    @staticmethod
    def _allowed_actions_for_phase(phase: object) -> frozenset[str]:
        return {
            "FUNDING": frozenset({"USE_AVAILABLE_FUNDS", "CHANGE_FUNDING_ACCOUNT", "CANCEL"}),
            "ORDER_STYLE": frozenset({"BUY_NOW", "SET_LIMIT", "CANCEL"}),
            "LIMIT_PRICE": frozenset({"CANCEL"}),
            "TIME_IN_FORCE": frozenset({"DAY", "GTC", "CANCEL"}),
        }.get(str(phase), frozenset())

    @staticmethod
    def _recover_bounded_choice(phase: object, text: str) -> str | None:
        """Recover only unmistakable choices already allowed in this phase."""

        normalized = " ".join(text.casefold().replace("’", "'").split())
        if not normalized:
            return None

        if normalized in {
            "cancel",
            "stop",
            "no",
            "never mind",
            "nevermind",
            "don't proceed",
            "do not proceed",
        }:
            return "CANCEL"

        if phase == "FUNDING":
            account_words = ("account", "funding source", "pay from")
            change_words = ("change", "switch", "another", "different", "other")
            if any(word in normalized for word in account_words) and any(
                word in normalized for word in change_words
            ):
                return "CHANGE_FUNDING_ACCOUNT"

            reduce_words = (
                "use",
                "lower",
                "reduce",
                "adjust",
                "decrease",
                "bring it down",
            )
            available_words = (
                "available",
                "balance",
                "afford",
                "what i have",
                "whatever i have",
                "money i have",
                "in savings",
            )
            if any(word in normalized for word in reduce_words) and any(
                word in normalized for word in available_words
            ):
                return "USE_AVAILABLE_FUNDS"

        if phase == "ORDER_STYLE":
            limit_words = ("limit", "maximum price", "max price", "price cap", "minimum price", "min price")
            if any(word in normalized for word in limit_words):
                return "SET_LIMIT"

            market_words = (
                "market",
                "current price",
                "opening bell",
                "when it opens",
                "when the market opens",
            )
            proceed_words = ("buy", "sell", "go ahead", "proceed", "execute", "place")
            if any(word in normalized for word in market_words) and any(
                word in normalized for word in proceed_words
            ):
                return "BUY_NOW"

        if phase == "TIME_IN_FORCE":
            if (
                "gtc" in normalized
                or "good till cancelled" in normalized
                or "good till canceled" in normalized
                or (
                    any(word in normalized for word in ("keep", "stay"))
                    and "open" in normalized
                    and any(word in normalized for word in ("cancel", "cancelled", "canceled"))
                )
            ):
                return "GTC"
            if (
                normalized in {"day", "today", "day order"}
                or "for today" in normalized
                or "only keep" in normalized and "today" in normalized
                or "expire today" in normalized
            ):
                return "DAY"

        return None

    @staticmethod
    def _question_for_phase(phase: object, state: Mapping | None = None) -> str:
        selling = bool(state) and state.get("side") == "SELL"
        if selling:
            return {
                "SIZE": "How many shares would you like to sell?",
                "ORDER_STYLE": "Sell now at market, or set a minimum price?",
                "LIMIT_PRICE": "What's the lowest price you'll accept?",
                "TIME_IN_FORCE": "Should the limit order be valid for today or stay open until cancelled?",
            }.get(str(phase), "Continue the sale.")
        return {
            "SIZE": "How many shares would you like, or what amount would you like to invest?",
            "FUNDING": "Use the available balance, choose another funding account, or cancel?",
            "ORDER_STYLE": "Buy now at market, or set a maximum price?",
            "LIMIT_PRICE": "What maximum price should I use?",
            "TIME_IN_FORCE": "Should the limit order be valid for today or stay open until cancelled?",
        }.get(str(phase), "Continue the investment request.")

    @staticmethod
    def _has_shortfall(state: dict) -> bool:
        value = state.get("funding_shortfall")
        return value is not None and _decimal(value, field="funding_shortfall") > 0

    @staticmethod
    def _use_available_funds(state: dict) -> None:
        balance = _decimal(state.get("funding_balance"), field="funding_balance")
        rate = _decimal(state.get("fx_rate") or "1", field="fx_rate")
        if rate <= 0:
            raise ConversationRejected("INVESTMENT_STATE_INVALID")
        price = _decimal(state["price"], field="price")
        # Leave room for the brokerage fee (0.08%, at least S$0.50) and a
        # little price movement, so the order fits the balance when it fills.
        spendable = max(Decimal("0"), balance - max(Decimal("0.50"), balance * Decimal("0.0008")))
        requested = _money(spendable / rate)
        shares = int((requested / (price * Decimal("1.02"))).to_integral_value(rounding=ROUND_DOWN))
        spend = _money(price * shares)
        state["requested_allocation"] = _amount(requested)
        state["requested_quantity"] = shares
        state["whole_shares"] = shares
        state["estimated_spend"] = _amount(spend)
        state["unallocated_cash"] = _amount(requested - spend)
        state["funding_required"] = _amount(_money(spend * rate))
        state["funding_shortfall"] = "0.00"

    @staticmethod
    def _funding_repaired_answer(state: dict) -> str:
        funding = state["funding_currency"]
        about = (
            f" (about {_label(funding, _decimal(state['funding_required'], field='funding_required'))})"
            if funding != state["quote_currency"] else ""
        )
        return (
            f"With your available {_label(funding, state['funding_balance'])}, I can buy "
            f"{_shares(int(state['whole_shares']))} of {_name(state)} for about "
            f"{_label(state['quote_currency'], state['estimated_spend'])}{about}."
        )

    @staticmethod
    def _parse_limit(text: str, current_price: Decimal) -> Decimal | None:
        if not text:
            return None
        lowered = text.casefold()
        percent = _PERCENT.search(text)
        if percent:
            amount = Decimal(percent.group("value"))
            if amount <= 0 or amount > 50:
                return None
            if any(word in lowered for word in ("below", "under", "lower", "less", "cheaper", "down")):
                return _money(current_price * (Decimal("1") - amount / 100))
            if any(word in lowered for word in ("above", "over", "higher", "more", "up")):
                return _money(current_price * (Decimal("1") + amount / 100))
            return None
        match = _ABSOLUTE.search(text)
        if match:
            amount = Decimal(match.group("value"))
            return _money(amount) if amount > 0 else None
        # Spoken prices: "two hundred and thirty dollars".
        try:
            size = stock_request.parse_size(text, allow_bare_count=True)
        except ValueError:
            return None
        if size is not None and size.allocation:
            return _money(size.allocation)
        if size is not None and size.quantity:
            return _money(Decimal(size.quantity))
        return None

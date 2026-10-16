"""Kokoro result speech with session-scoped ephemeral artifacts."""

from __future__ import annotations

import json
import logging
import secrets
import time
import asyncio
import copy
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import httpx

# Server-made identifiers only (token_urlsafe); never a path.
_ARTIFACT_ID = re.compile(r"[A-Za-z0-9_-]{16,80}")


logger = logging.getLogger(__name__)
KOKORO_SPEED = 1.20
PROMPT_INJECTION_BLOCKED_SPEECH = (
    "I couldn’t do that because prompt injection was blocked. Nothing was sent."
)


@dataclass(frozen=True)
class SpeechArtifact:
    artifact_id: str
    mime_type: str = "audio/wav"


class SpeechPromptRejected(ValueError):
    pass


_STATIC_HANDS_FREE_PROMPTS = {
    "wake_greeting": "Hi, I'm Dimon. How can I help you?",
    "wake_ack": "Yes?",
    "nothing_to_cancel": "There's nothing to cancel.",
    "nothing_waiting": "There's nothing waiting to be confirmed.",
    "generic_error": "Something went wrong there. Please check the screen.",
    "all_done": "That's everything.",
    "done": "Done.",
    "check_screen": "Please check the screen.",
    "prompt_injection_blocked": PROMPT_INJECTION_BLOCKED_SPEECH,
    "choose_option": "Which option did you mean?",
    "choose_option_again": "Sorry, which option did you mean?",
    "leave_for_now": "I'll leave that one for now.",
    "missing_amount": "Please say the whole request again with the amount.",
    "missing_recipient": "Who would you like to pay? Tell me the recipient’s name, and I’ll check it with you.",
    "investor_profile": "Before your first investment, please answer the four questions on screen.",
    "investment_review": "Investments need Singpass and your face. Review the order on screen.",
    "face_confirm": "Please confirm with your face on screen.",
    "identity_then_face": "First verify your identity with Singpass, then confirm with your face on screen.",
    "identity_verification_failed": "Singpass identity verification wasn’t completed, so the order could not be approved. Nothing was bought. Verify your identity and try again.",
    "extra_check_face": "This request needs an extra check and your face on screen.",
    "new_payee_face": "A new payee needs your face on screen.",
    "nothing_sent": "Nothing was sent.",
    "cancelled_not_sent": "Okay, cancelled. Nothing was sent.",
    "order_cancelled": "Okay, the order was cancelled. Nothing moved.",
    "order_failed": "The order did not go through. Nothing moved.",
    "connection_lost": "I lost the connection. Please check the screen before trying again.",
    "voice_confirmation": "Please say, Yes Confirm.",
    "voice_confirmation_retry": "Please say, Yes Confirm.",
    "voice_mismatch": "That didn't sound like your registered voice.",
    "did_not_catch": "I didn't catch that.",
    "heard_nothing": "I didn't hear anything.",
    "voice_confirmation_failed": "Voice confirmation didn't work. Please confirm on screen.",
    "device_signature_failed": "Your device didn't sign it, so nothing was sent.",
    "voice_sample_inconsistent": "That sounded different from before. Please try again.",
    "voice_prompt_not_heard": "I didn't hear that phrase. Say it once more, on its own.",
    "voice_try_again": "I didn't get that. Once more.",
    "voice_registration_face": "Thanks. Now confirm with your face on screen to turn on voice approvals.",
    "voice_registered": "Voice approvals are on. For payments under 200 dollars, say Yes Confirm.",
    "recipient_cancelled": "I've cancelled that payment request. Nothing was sent.",
    "recipient_social_close": "You're welcome. I've cancelled that payment request. Nothing was sent.",
    "recipient_again": "I still need the recipient's name. For example, Alicia Powell. Nothing has been sent.",
    "recipient_stopped": "I couldn't confirm a recipient, so I've cancelled that payment request. Nothing was sent.",
}

_ENROLLMENT_PROMPTS = (
    "My voice is my confirmation for Dimon",
    "Send money only when I say so",
    "Every payment needs my own voice",
)


def _exact_prompt_values(values: Mapping[str, Any], *names: str) -> None:
    if set(values) != set(names):
        raise SpeechPromptRejected("prompt values do not match the template")


def _prompt_integer(values: Mapping[str, Any], name: str, *, minimum: int, maximum: int) -> int:
    value = values.get(name)
    if type(value) is not int or not minimum <= value <= maximum:
        raise SpeechPromptRejected(f"{name} is invalid")
    return value


def _prompt_transcript(values: Mapping[str, Any], name: str) -> str:
    value = values.get(name)
    value = value.strip() if isinstance(value, str) else ""
    if not value or len(value) > 240 or any(ord(char) < 32 for char in value):
        raise SpeechPromptRejected(f"{name} is invalid")
    return value


def transcription_disagreement_message(live_text: str) -> str:
    return (
        f'I heard “{live_text},” but the final transcription was different. '
        f'Did you mean “{live_text}”?'
    )


def render_hands_free_prompt(prompt_key: str, values: Mapping[str, Any]) -> str:
    """Render only enumerated, server-owned hands-free speech templates."""

    if prompt_key in _STATIC_HANDS_FREE_PROMPTS:
        _exact_prompt_values(values)
        return _STATIC_HANDS_FREE_PROMPTS[prompt_key]
    if prompt_key in {"multi_requests", "multi_actions"}:
        _exact_prompt_values(values, "count")
        count = _prompt_integer(values, "count", minimum=1, maximum=20)
        noun = "requests" if prompt_key == "multi_requests" else "actions"
        return f"I heard {count} {noun}. Let's go through them."
    if prompt_key in {"request_number", "action_number"}:
        _exact_prompt_values(values, "index", "total")
        index = _prompt_integer(values, "index", minimum=1, maximum=20)
        total = _prompt_integer(values, "total", minimum=1, maximum=20)
        if index > total:
            raise SpeechPromptRejected("index is invalid")
        noun = "Request" if prompt_key == "request_number" else "Action"
        return f"{noun} {index} of {total}."
    if prompt_key == "enrollment_prompt":
        _exact_prompt_values(values, "index")
        index = _prompt_integer(
            values, "index", minimum=0, maximum=len(_ENROLLMENT_PROMPTS) - 1
        )
        return f"Please say: {_ENROLLMENT_PROMPTS[index]}."
    if prompt_key == "transcription_disagreement":
        _exact_prompt_values(values, "live_text")
        return transcription_disagreement_message(_prompt_transcript(values, "live_text"))
    raise SpeechPromptRejected("unknown speech prompt")


def build_result_summary(
    *,
    status: str,
    action_kind: str,
    amount: int,
    currency: str,
    recipient_display: str | None,
    reference: str | None = None,
    batch_count: int | None = None,
) -> str | None:
    """Construct the only result text that local or gateway TTS may receive."""

    if currency != "SGD" or not isinstance(amount, int) or amount <= 0:
        return None
    amount_text = f"{amount // 100} dollars and {amount % 100:02d} cents"
    if status == "EXECUTED_SYNTHETIC_TRANSFER" and action_kind in {
        "BENEFICIARY_TRANSFER",
        "PAYNOW_MOBILE_TRANSFER",
    }:
        if not recipient_display:
            return None
        summary = f"Synthetic transfer complete. {amount_text} moved to {recipient_display}."
    elif (
        status == "EXECUTED_SYNTHETIC_BATCH"
        and action_kind == "BENEFICIARY_TRANSFER_BATCH"
    ):
        if batch_count is not None:
            summary = (
                f"Synthetic batch complete. {amount_text} moved across "
                f"{batch_count} transfers."
            )
        elif recipient_display:
            summary = f"Synthetic batch complete. {amount_text} moved to {recipient_display}."
        else:
            return None
    else:
        return None
    if reference:
        summary += f" Reference {reference}."
    return summary


def build_stock_allocation_summary(instrument_label: object) -> str | None:
    """Build speech for the supported stock clarification from a label only."""
    if not isinstance(instrument_label, str):
        return None
    label = instrument_label.strip()
    if len(label) > 80 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .&'()/\-]{0,79}", label):
        return None
    return f"How much would you like to allocate to {label}?"


def build_order_review_summary(order: object = None) -> str:
    if isinstance(order, dict):
        quantity = order.get("quantity")
        instrument = order.get("instrument")
        name = instrument.get("name") if isinstance(instrument, dict) else None
        debit_minor = order.get("max_funding_debit_minor")
        maximum = (
            _spoken_money(order.get("funding_currency") or "SGD", Decimal(debit_minor) / 100)
            if isinstance(debit_minor, int) and not isinstance(debit_minor, bool)
            else None
        )
        if isinstance(quantity, int) and not isinstance(quantity, bool) and quantity > 0 \
                and isinstance(name, str) and name.strip() and maximum:
            shares = "share" if quantity == 1 else "shares"
            return (
                f"Buy {quantity} {shares} of {name.strip()}. The maximum debit is {maximum}. "
                "Review the details and confirm it on screen."
            )
    return "Your stock purchase is prepared. Review the details and confirm it on screen."


def _spoken_decimal(value: object, places: int = 1) -> str | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    quantum = Decimal(1).scaleb(-places)
    return f"{number.quantize(quantum, rounding=ROUND_HALF_EVEN):,.{places}f}"


def _spoken_money(currency: object, value: object) -> str | None:
    if not isinstance(currency, str) or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    whole = int(amount)
    cents = int((amount - whole) * 100)
    currency = currency.upper()
    if currency == "USD":
        dollar_label = "U. S. dollar" if whole == 1 else "U. S. dollars"
        cent_label = "U. S. cent" if cents == 1 else "U. S. cents"
    elif currency == "SGD":
        dollar_label = "Singapore dollar" if whole == 1 else "Singapore dollars"
        cent_label = "Singapore cent" if cents == 1 else "Singapore cents"
    else:
        return None
    if whole == 0 and cents:
        return f"{cents} {cent_label}"
    if cents:
        cents_label = "cent" if cents == 1 else "cents"
        return f"{whole:,} {dollar_label} and {cents} {cents_label}"
    return f"{whole:,} {dollar_label}"


_SPOKEN_SGD_PATTERN = re.compile(
    r"(?<![\w$])(?:S\$|SGD)\s*(?P<amount>\d[\d,]*(?:\.\d{1,2})?)(?![\w.])",
    re.IGNORECASE,
)


def _expand_spoken_singapore_dollars(text: str) -> str:
    """Expand display-oriented SGD notation before it reaches the voice model."""

    def replace(match: re.Match[str]) -> str:
        spoken = _spoken_money("SGD", match.group("amount").replace(",", ""))
        return spoken or match.group(0)

    return _SPOKEN_SGD_PATTERN.sub(replace, text)


def _spoken_symbol(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    words = []
    for character in value.strip().upper():
        if character.isalnum():
            words.append(character)
        elif character == ".":
            words.append("dot")
        elif character == "-":
            words.append("dash")
    return " ".join(words) or None


def _spoken_exchange(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().upper()
    if "NASDAQ" in normalized:
        return "Nasdaq"
    if "NYSE" in normalized or "NEW YORK" in normalized:
        return "the New York Stock Exchange"
    if "SINGAPORE" in normalized or normalized in {"SGX", "SES"}:
        return "the Singapore Exchange"
    return value.strip()


def _spoken_market_session(exchange: str, state: str) -> tuple[str, str | None]:
    """Keep speech aligned with the brief while avoiding Yahoo's raw labels."""

    normalized = state.strip().upper().replace("-", "_")
    normalized = {
        "REGULAR": "OPEN",
        "PRE": "PRE_MARKET",
        "PREMARKET": "PRE_MARKET",
        "POST": "AFTER_HOURS",
        "POSTMARKET": "AFTER_HOURS",
    }.get(normalized, normalized)
    if normalized == "OPEN":
        return f"{exchange} is currently trading.", None
    if normalized == "PRE_MARKET":
        return f"{exchange} is currently in pre-market.", "A market order would wait until the open."
    if normalized == "AFTER_HOURS":
        return f"{exchange} is in after-hours trading.", "A market order would wait until the next open."
    if normalized == "CLOSED":
        return f"{exchange} is currently closed.", "A market order would wait until it reopens."
    return f"{exchange} status unavailable; market order must wait.", None


def stock_brief_voice_summary(result: dict) -> str | None:
    investing = result.get("investing")
    if not isinstance(investing, dict) or investing.get("view") != "REAL_STOCK_BRIEF":
        return None
    brief = investing.get("brief")
    if not isinstance(brief, dict):
        return None
    instrument = brief.get("instrument")
    market = brief.get("market")
    if not isinstance(instrument, dict) or not isinstance(market, dict):
        return None

    symbol = _spoken_symbol(instrument.get("symbol"))
    currency = instrument.get("quote_currency")
    price = _spoken_money(currency, market.get("price"))
    if not symbol or not price:
        return None

    shortfall_number = _spoken_decimal(brief.get("funding_shortfall"), 2)
    if (
        shortfall_number is not None
        and Decimal(shortfall_number.replace(",", "")) > 0
    ):
        required = _spoken_money(
            brief.get("funding_currency"), brief.get("funding_required")
        )
        balance = _spoken_money(
            brief.get("funding_currency"), brief.get("funding_balance")
        )
        shortfall = _spoken_money(
            brief.get("funding_currency"), brief.get("funding_shortfall")
        )
        account = brief.get("funding_account_label")
        if not all((required, balance, shortfall)) or not isinstance(account, str):
            return None
        requested_quantity = brief.get("requested_quantity")
        if isinstance(requested_quantity, int) and not isinstance(requested_quantity, bool):
            request_text = (
                f"Buying {requested_quantity:,} whole share"
                f"{'s' if requested_quantity != 1 else ''} needs about {required}"
            )
        else:
            allocation = _spoken_money(
                brief.get("allocation_currency"), brief.get("requested_allocation")
            )
            if not allocation:
                return None
            request_text = f"Your allocation of {allocation} needs about {required}"
        parts = [
            f"{symbol} is {price}.",
            f"{request_text}, but your "
            f"{account} balance is {balance}.",
            f"You're short {shortfall}.",
        ]
        question = brief.get("next_question")
        if isinstance(question, str) and question.strip():
            parts.append(question.strip())
        return " ".join(parts)

    exchange = _spoken_exchange(instrument.get("exchange"))
    state = market.get("market_state")
    if not exchange or not isinstance(state, str):
        return None

    market_session, order_caveat = _spoken_market_session(exchange, state)
    parts = [f"{symbol} is {price}.", market_session]
    if order_caveat:
        parts.append(order_caveat)

    trailing_pe = market.get("trailing_pe")
    pe = _spoken_decimal(trailing_pe)
    parts.append(
        f"The price to earnings ratio is {pe} times."
        if pe is not None
        else "The price to earnings ratio is unavailable."
    )

    yearly_change = _spoken_decimal(market.get("one_year_change_pct"))
    if yearly_change is None:
        return None
    yearly_number = Decimal(yearly_change.replace(",", ""))
    direction = "up" if yearly_number >= 0 else "down"
    parts.append(f"It is {direction} {abs(yearly_number):.1f} percent over one year.")

    if brief.get("allocation_assumption"):
        allocation = _spoken_money(brief.get("allocation_currency"), brief.get("requested_allocation"))
        if allocation is None:
            return None
        parts.append(f"I treated your allocation as {allocation}.")

    shares = brief.get("whole_shares")
    spend = _spoken_money(currency, brief.get("estimated_spend"))
    cash_left = _spoken_money(currency, brief.get("unallocated_cash"))
    if not isinstance(shares, int) or isinstance(shares, bool) or not spend or not cash_left:
        return None
    requested_quantity = brief.get("requested_quantity")
    if isinstance(requested_quantity, int) and not isinstance(requested_quantity, bool):
        quantity_text = f"Buying {shares:,} whole share{'s' if shares != 1 else ''} costs {spend}"
        fx_estimate = brief.get("fx_estimate")
        if isinstance(fx_estimate, dict):
            pair = fx_estimate.get("pair")
            converted_currency = pair.split("/", 1)[1] if isinstance(pair, str) and "/" in pair else None
            converted = _spoken_money(converted_currency, fx_estimate.get("converted_amount"))
            if converted is None:
                return None
            quantity_text += f", about {converted}"
        parts.append(quantity_text + ".")
    else:
        fx_estimate = brief.get("fx_estimate")
        sizing_prefix = "That"
        if isinstance(fx_estimate, dict):
            pair = fx_estimate.get("pair")
            converted_currency = pair.split("/", 1)[1] if isinstance(pair, str) and "/" in pair else None
            converted = _spoken_money(converted_currency, fx_estimate.get("converted_amount"))
            if converted is None:
                return None
            sizing_prefix = f"That allocation is about {converted} and"
        parts.append(
            f"{sizing_prefix} buys {shares:,} whole shares for {spend}, leaving {cash_left}."
        )

    shortfall = _spoken_decimal(brief.get("funding_shortfall"), 2)
    if shortfall is not None and Decimal(shortfall.replace(",", "")) > 0:
        balance = _spoken_money(brief.get("funding_currency"), brief.get("funding_balance"))
        missing = _spoken_money(brief.get("funding_currency"), brief.get("funding_shortfall"))
        account = brief.get("funding_account_label")
        if not balance or not missing or not isinstance(account, str):
            return None
        parts.append(f"Your {account} balance is {balance}, so you're short {missing}.")

    question = brief.get("next_question")
    if isinstance(question, str) and question.strip():
        parts.append(question.strip())
    return " ".join(parts)


class KokoroSynthesizer:
    def __init__(self, base_url: str, *, timeout: float = 30.0):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Kokoro URL must be HTTP")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def __call__(self, text: str) -> bytes:
        # Keep the product name written as "Dimon", while giving Kokoro the
        # phonetic spelling the team chose for speech output.
        spoken_text = _expand_spoken_singapore_dollars(text)
        spoken_text = re.sub(r"\bDimon\b", "Diamon", spoken_text, flags=re.IGNORECASE)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, trust_env=False) as client:
            response = await client.post(
                "/v1/speech",
                json={"text": spoken_text},
            )
            if response.status_code == 404:
                return await self._gradio_compat(client, spoken_text)
            response.raise_for_status()
            if response.headers.get("content-type", "").split(";", 1)[0] != "audio/wav":
                raise RuntimeError("Kokoro returned an invalid media type")
            return response.content

    async def _gradio_compat(self, client: httpx.AsyncClient, text: str) -> bytes:
        response = await client.post(
            "/gradio_api/call/KOKORO_TTS_API",
            json={"data": [text, "American English", "af_heart", KOKORO_SPEED, False, False, "WAV", 300, 150]},
        )
        response.raise_for_status()
        event_id = response.json().get("event_id")
        if not isinstance(event_id, str):
            raise RuntimeError("Kokoro did not return an event ID")
        audio_url = None
        async with client.stream(
            "GET", f"/gradio_api/call/KOKORO_TTS_API/{event_id}"
        ) as stream:
            stream.raise_for_status()
            event_type = ""
            async for line in stream.aiter_lines():
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:") and event_type == "complete":
                    payload = json.loads(line[5:].strip())
                    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                        audio_url = payload[0].get("url")
                        break
                elif line.startswith("data:") and event_type == "error":
                    raise RuntimeError("Kokoro synthesis failed")
        if not isinstance(audio_url, str):
            raise RuntimeError("Kokoro queue ended without audio")
        resolved = urljoin(self.base_url + "/", audio_url)
        if urlparse(resolved).netloc != urlparse(self.base_url).netloc:
            raise RuntimeError("Kokoro returned an untrusted audio URL")
        parsed = urlsplit(resolved)
        route = "/gradio_api/file="
        offset = parsed.path.find(route)
        if offset > 0:
            resolved = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path[offset:], parsed.query, parsed.fragment)
            )
        audio = await client.get(resolved)
        audio.raise_for_status()
        return audio.content

    async def synthesize_result(self, result: dict) -> bytes:
        text = result_summary(result)
        if text is None:
            raise ValueError("speech requires a committed result")
        return await self(text)


def result_summary(result: dict) -> str | None:
    if (
        result.get("outcome") == "BLOCKED"
        and result.get("reason_code") == "PROMPT_INJECTION_BLOCKED"
    ):
        return PROMPT_INJECTION_BLOCKED_SPEECH
    if (
        result.get("outcome") in {"BLOCKED", "FAILED"}
        and result.get("reason_code") in {
            "INSTRUMENT_NOT_FOUND",
            "INSTRUMENT_AMBIGUOUS",
            "UNSUPPORTED_INSTRUMENT",
        }
    ):
        message = result.get("answer") or result.get("question")
        message = message.strip() if isinstance(message, str) else ""
        return message if 0 < len(message) <= 320 else None
    if (
        result.get("outcome") == "CLARIFICATION_REQUEST"
        and result.get("reason_code") == "TRANSCRIPTION_DISAGREEMENT"
    ):
        live_text = result.get("transcription_disagreement", {}).get("live_text")
        if isinstance(live_text, str):
            try:
                return transcription_disagreement_message(
                    _prompt_transcript({"live_text": live_text}, "live_text")
                )
            except SpeechPromptRejected:
                return None
    if result.get("outcome") == "MULTI_ACTION_CONFIRMATION_REQUEST":
        return (
            "I’ve prepared each requested action. Review and approve the payments "
            "individually, then continue with the stock purchase review."
        )
    if (
        result.get("outcome") == "CLARIFICATION_REQUEST"
        and result.get("reason_code") in {
            "MULTI_ACTION_EVIDENCE_INVALID",
            "MULTI_ACTION_ITEM_UNRESOLVED",
            "MULTI_ACTION_CHILD_PREPARATION_FAILED",
        }
    ):
        answer = result.get("answer")
        if isinstance(answer, str) and answer.strip():
            return answer.strip()
        return (
            "I couldn’t verify every requested action. Nothing has been approved. "
            "Please clarify the request."
        )
    if result.get("outcome") == "ANSWERED" and result.get("reason_code") == "GREETING":
        return "Hi, I’m Dimon. How can I help you?"
    if result.get("outcome") == "ANSWERED" and result.get("reason_code") == "CONVERSATIONAL":
        answer = result.get("answer")
        allowed = {
            "You're welcome. What else can I help with?",
            "Thank you. What would you like help with?",
            "I'm ready to help. What would you like to do?",
            "Hi. What would you like help with?",
            "There isn't a pending request to cancel. Nothing was sent.",
        }
        return answer if answer in allowed else None
    if (
        result.get("outcome") == "ANSWERED"
        and result.get("reason_code") == "CONVERSATION_TURN_APPLIED"
    ):
        answer = result.get("answer")
        answer = answer.strip() if isinstance(answer, str) else ""
        question = result.get("question")
        question = question.strip() if isinstance(question, str) else ""
        conversation = result.get("conversation")
        if isinstance(conversation, dict) and conversation.get("status") == "WAITING" and question:
            return question if not answer else answer if question in answer else f"{answer} {question}"
        return answer or None
    if (
        result.get("outcome") == "CLARIFICATION_REQUEST"
        and result.get("reason_code") == "MISSING_RECIPIENT"
    ):
        # This is a server-authored clarification. Keep the wording bounded so
        # the voice path can read it without synthesizing arbitrary prose.
        return "Who would you like to pay? Tell me the recipient’s name, and I’ll check it with you."
    if (
        result.get("outcome") == "CLARIFICATION_REQUEST"
        and result.get("reason_code")
        in {
            "CLOSE_RECIPIENT_MATCH",
            "AMBIGUOUS_RECIPIENT",
            "PHONETIC_RECIPIENT_MATCH",
        }
    ):
        question = result.get("question")
        return question.strip() if isinstance(question, str) and question.strip() else None
    if (
        result.get("outcome") == "CLARIFICATION_REQUEST"
        and result.get("reason_code") == "BILL_PAYMENT_CONFIRMATION_REQUIRED"
    ):
        choices = result.get("receipt_choices")
        if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
            choice = choices[0]
            biller = choice.get("recipient_display_name")
            source = choice.get("source_label")
            amount_minor = choice.get("amount_minor")
            currency = choice.get("currency")
            amount = (
                _spoken_money(currency, Decimal(amount_minor) / 100)
                if isinstance(amount_minor, int) and not isinstance(amount_minor, bool)
                else None
            )
            if all(isinstance(value, str) and value.strip() for value in (biller, source, amount)):
                return (
                    f"I’m ready to pay your {biller} bill for {amount} from {source}. "
                    "Review the details and confirm on your device."
                )
        question = result.get("answer") or result.get("question")
        return question.strip() if isinstance(question, str) and question.strip() else None
    if (
        result.get("outcome") == "CLARIFICATION_REQUEST"
        and result.get("reason_code")
        in {
            "BILLER_REQUIRED",
            "AMOUNT_REQUIRED",
            "CURRENCY_REQUIRED",
            "BILLER_AMBIGUOUS",
            "BILLER_UNSUPPORTED",
            "SCHEDULED_BILL_PAYMENT_UNSUPPORTED",
            "MISSING_AMOUNT",
        }
    ):
        # These questions are created by deterministic bill/payment handling.
        # Forward only the bounded server-authored clarification, never raw
        # model output, to speech synthesis.
        question = result.get("answer") or result.get("question")
        return question.strip() if isinstance(question, str) and question.strip() else None
    paper_result = result.get("paper_order_result")
    if isinstance(paper_result, dict):
        status = paper_result.get("status")
        reason = paper_result.get("reason_code") or result.get("reason_code")
        if status == "FILLED":
            trade = paper_result.get("trade")
            if not isinstance(trade, dict):
                return None
            quantity = trade.get("quantity")
            symbol = _spoken_symbol(trade.get("symbol"))
            fill_minor = trade.get("fill_price_minor")
            debit_minor = trade.get("funding_debit_minor")
            if type(fill_minor) is not int or type(debit_minor) is not int:
                return None
            quote_currency = trade.get("quote_currency") or result.get("quote_currency")
            funding_currency = trade.get("funding_currency") or "SGD"
            fill_price = _spoken_money(quote_currency, Decimal(fill_minor) / 100)
            total = _spoken_money(funding_currency, Decimal(debit_minor) / 100)
            if type(quantity) is not int or quantity <= 0 or not symbol or not fill_price or not total:
                return None
            summary = (
                f"Purchase complete. Bought {quantity} {'share' if quantity == 1 else 'shares'} "
                f"of {symbol}, at {fill_price} each. "
                f"{total} was taken from your account, and your portfolio is updated."
            )
            execution_evidence = paper_result.get("execution_evidence")
            if isinstance(execution_evidence, dict) and execution_evidence.get("demo_override"):
                actual_state = str(
                    execution_evidence.get("actual_market_state") or "unavailable"
                ).lower()
                summary += (
                    f" This was a simulated fill using the latest timestamped quote because "
                    f"Nasdaq was {actual_state}. No real money was used."
                )
            return summary
        if reason in {"PRICE_MOVED_BEYOND_LIMIT", "FUNDING_MOVED_BEYOND_LIMIT"}:
            return (
                "I couldn’t fill the order because the price moved above the limit you approved. "
                "No shares were bought, and your balance is unchanged. Ask me for a fresh quote to try again."
            )
        if status in {"QUEUED", "OPEN"}:
            return (
                "The market is not open yet. Your order is waiting for the market to open; "
                "it has not filled, and your balance has not changed."
            )
        if status in {"REJECTED", "EXPIRED", "CANCELLED"}:
            return (
                "The stock order was not completed. No shares were bought, and your balance is unchanged."
            )
    if result.get("outcome") == "ORDER_CONFIRMATION_REQUEST" and (
        isinstance(result.get("paper_order"), dict)
        or isinstance(result.get("order"), dict)
    ):
        return build_order_review_summary(result.get("paper_order") or result.get("order"))
    if (
        result.get("outcome") == "ANSWERED"
        and result.get("reason_code") == "REAL_STOCK_ALLOCATION_REQUIRED"
    ):
        # This question is assembled server-side from the grounded instrument
        # label; don't synthesize arbitrary model prose for other outcomes.
        return build_stock_allocation_summary(result.get("instrument_label"))
    if result.get("outcome") == "ANSWERED" and result.get("reason_code") == "REAL_STOCK_BRIEF":
        spoken_brief = stock_brief_voice_summary(result)
        if spoken_brief is not None:
            return spoken_brief
        answer = result.get("answer")
        return answer if isinstance(answer, str) and answer.strip() else None
    if result.get("outcome") == "EXECUTED_SYNTHETIC_TRANSFER":
        action = result.get("resolved_action") or {}
        recipient = action.get("recipient_display_name")
        amount = result.get("amount_minor")
        currency = result.get("currency", "SGD")
        if not isinstance(recipient, str) or not isinstance(amount, int):
            return None
        reference = None
        validated = result.get("validated_intent")
        if isinstance(validated, dict):
            candidate = validated.get("payment_reference")
            reference = candidate if isinstance(candidate, str) else None
        proposed_kind = validated.get("kind") if isinstance(validated, dict) else None
        return build_result_summary(
            status="EXECUTED_SYNTHETIC_TRANSFER",
            action_kind=proposed_kind or action.get("kind", "BENEFICIARY_TRANSFER"),
            amount=amount,
            currency=currency,
            recipient_display=recipient,
            reference=reference,
        )
    if result.get("outcome") == "EXECUTED_SYNTHETIC_BATCH":
        amount = result.get("amount_minor")
        count = len(result.get("resolved_actions") or [])
        currency = result.get("currency", "SGD")
        if not isinstance(amount, int) or count < 1:
            return None
        return build_result_summary(
            status="EXECUTED_SYNTHETIC_BATCH",
            action_kind="BENEFICIARY_TRANSFER_BATCH",
            amount=amount,
            currency=currency,
            recipient_display=None,
            batch_count=count,
        )
    return None


class SpeechService:
    def __init__(
        self,
        *,
        base_url: str,
        audio_dir: Path,
        synthesizer: Callable[[str], Awaitable[bytes]] | None = None,
        max_wav_bytes: int = 8 * 1_048_576,
        ttl_seconds: int = 3600,
        read_back_synthesizer: Callable[[str], Awaitable[bytes]] | None = None,
    ):
        self.read_back_synthesizer = read_back_synthesizer
        self.base_url = base_url
        self.audio_dir = Path(audio_dir)
        self.synthesizer = synthesizer or KokoroSynthesizer(base_url)
        self.max_wav_bytes = int(max_wav_bytes)
        self.ttl_seconds = int(ttl_seconds)
        self._artifacts: dict[str, tuple[str, Path, float]] = {}
        self._pending: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def _summary(result: dict) -> str | None:
        return result_summary(result)

    async def speak_result(self, result: dict, context) -> SpeechArtifact | None:
        text = self._summary(result)
        if text is None:
            return None
        artifact_id = secrets.token_urlsafe(18)
        path = self.audio_dir / context.session_id / f"{artifact_id}.wav"
        self._artifacts[artifact_id] = (context.session_id, path, time.time() + self.ttl_seconds)
        try:
            await self._synthesize_into(result, text, path)
            return SpeechArtifact(artifact_id) if path.is_file() else None
        except Exception as exc:
            logger.warning(
                "speech_artifact_generation_failed artifact_id=%s kind=text error_type=%s error_code=%s http_status=%s",
                artifact_id,
                type(exc).__name__,
                getattr(exc, "reason_code", ""),
                getattr(getattr(exc, "response", None), "status_code", ""),
            )
            self._artifacts.pop(artifact_id, None)
            return None

    def queue_result(self, result: dict, context) -> str | None:
        """Reserve an audio artifact and synthesize it outside the response path."""
        text = self._summary(result)
        if text is None:
            return None
        artifact_id = secrets.token_urlsafe(18)
        path = self.audio_dir / context.session_id / f"{artifact_id}.wav"
        self._artifacts[artifact_id] = (context.session_id, path, time.time() + self.ttl_seconds)
        self._pending.add(artifact_id)
        logger.info(
            "speech_artifact_queued request_id=%s",
            result.get("request_id", ""),
        )
        self._mark_pending(path)
        task = asyncio.create_task(
            self._synthesize_queued(copy.deepcopy(result), text, artifact_id, path)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return artifact_id

    def queue_text(self, text: str, context) -> str | None:
        """Synthesize arbitrary server-built text (e.g. a draft read-back)."""
        if not text:
            return None
        artifact_id = secrets.token_urlsafe(18)
        path = self.audio_dir / context.session_id / f"{artifact_id}.wav"
        self._artifacts[artifact_id] = (context.session_id, path, time.time() + self.ttl_seconds)
        self._pending.add(artifact_id)
        self._mark_pending(path)
        task = asyncio.create_task(self._synthesize_text(text, artifact_id, path))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return artifact_id

    async def _synthesize_text(self, text: str, artifact_id: str, path: Path) -> None:
        try:
            synthesizer = self.read_back_synthesizer or self.synthesizer
            synthesize_result = getattr(synthesizer, "synthesize_result", None)
            if callable(synthesize_result):
                data = await synthesize_result(
                    {
                        "outcome": "ANSWERED",
                        "reason_code": "CONVERSATION_TURN_APPLIED",
                        "answer": text,
                    }
                )
            else:
                data = await synthesizer(text)
            if isinstance(data, bytes) and len(data) <= self.max_wav_bytes and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            else:
                logger.warning(
                    "speech_artifact_generation_failed artifact_id=%s kind=text error_code=INVALID_WAV",
                    artifact_id,
                )
                self._artifacts.pop(artifact_id, None)
        except Exception as exc:
            logger.warning(
                "speech_artifact_generation_failed artifact_id=%s kind=text error_type=%s error_code=%s http_status=%s",
                artifact_id,
                type(exc).__name__,
                getattr(exc, "reason_code", ""),
                getattr(getattr(exc, "response", None), "status_code", ""),
            )
            self._artifacts.pop(artifact_id, None)
        finally:
            self._pending.discard(artifact_id)
            path.with_suffix(".pending").unlink(missing_ok=True)

    async def _synthesize_queued(
        self,
        result: dict,
        text: str,
        artifact_id: str,
        path: Path,
    ) -> None:
        try:
            await self._synthesize_into(result, text, path)
            if not path.is_file():
                self._artifacts.pop(artifact_id, None)
                logger.warning(
                    "speech_artifact_generation_failed request_id=%s kind=result error_code=ARTIFACT_MISSING",
                    result.get("request_id", ""),
                )
            else:
                logger.info(
                    "speech_artifact_ready request_id=%s",
                    result.get("request_id", ""),
                )
        except Exception as exc:
            logger.warning(
                "speech_artifact_generation_failed request_id=%s kind=result error_type=%s error_code=%s http_status=%s",
                result.get("request_id", ""),
                type(exc).__name__,
                getattr(exc, "reason_code", ""),
                getattr(getattr(exc, "response", None), "status_code", ""),
            )
            self._artifacts.pop(artifact_id, None)
        finally:
            self._pending.discard(artifact_id)
            path.with_suffix(".pending").unlink(missing_ok=True)

    async def _synthesize_into(self, result: dict, text: str, path: Path) -> None:
        synthesize_result = getattr(self.synthesizer, "synthesize_result", None)
        if callable(synthesize_result):
            data = await synthesize_result(result)
        else:
            data = await self.synthesizer(text)
        if not isinstance(data, bytes) or len(data) > self.max_wav_bytes:
            raise ValueError("speech service returned an invalid audio size")
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise ValueError("speech service returned a non-WAV artifact")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @staticmethod
    def _mark_pending(path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.with_suffix(".pending").touch()
        except OSError:
            pass

    def _disk_path(self, artifact_id: str, context) -> Path | None:
        """Where another worker process would have written this artifact.
        Only the requesting session's own folder is ever looked in."""

        if not _ARTIFACT_ID.fullmatch(artifact_id or "") or not _ARTIFACT_ID.fullmatch(context.session_id or ""):
            return None
        return self.audio_dir / context.session_id / f"{artifact_id}.wav"

    def is_pending(self, artifact_id: str, context) -> bool:
        record = self._artifacts.get(artifact_id)
        if record is not None:
            return record[0] == context.session_id and artifact_id in self._pending
        path = self._disk_path(artifact_id, context)
        marker = path.with_suffix(".pending") if path else None
        try:
            return bool(marker and marker.is_file() and time.time() - marker.stat().st_mtime < 60)
        except OSError:
            return False

    async def read(self, artifact_id: str, context) -> bytes | None:
        self.cleanup()
        record = self._artifacts.get(artifact_id)
        if record is not None:
            if record[0] != context.session_id:
                return None
            path = record[1]
        else:
            path = self._disk_path(artifact_id, context)
            if path is None:
                return None
        try:
            if time.time() - path.stat().st_mtime > self.ttl_seconds:
                return None
            return path.read_bytes()
        except OSError:
            return None

    def cleanup(self) -> None:
        now = time.time()
        expired = [key for key, (_, path, expiry) in self._artifacts.items() if expiry <= now]
        for key in expired:
            _, path, _ = self._artifacts.pop(key)
            self._pending.discard(key)
            path.unlink(missing_ok=True)

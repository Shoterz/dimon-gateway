"""Understanding stock requests by fixed rules, with or without the model.

The language model often recognises "buy S$500 of Tesla" first, but it is
rate-limited and sometimes unavailable. These rules cover the everyday ways
of asking to buy, sell or look up a listed company so that such a request
never falls through to the payment path. They only read the user's words:
which company, buy or sell, and how many shares or how much money. Prices,
balances and holdings are always checked by the server afterwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from .grounding import GroundingError, _bare_amount_minor, _BARE_NUMBER

BUY, SELL, RESEARCH = "BUY", "SELL", "RESEARCH"

_PAYMENT = re.compile(
    r"\b(?:send|pay|paid|transfer|paynow|pay\s*now|give|bank\s+in|top\s+up|remit|owe)\b", re.IGNORECASE
)
_RECURRING = re.compile(r"\b(?:a|per|each|every)\s+month\b|\bmonthly\b", re.IGNORECASE)
_SELL = re.compile(r"\b(?:sell|selling|sold|offload|dump|liquidate|cash\s+out|get\s+rid\s+of)\b", re.IGNORECASE)
_BUY = re.compile(r"\b(?:buy|buying|purchase|invest|investing|acquire)\b", re.IGNORECASE)
# Everyday verbs that mean "buy" only next to shares or an amount:
# "get me 3 shares of Nvidia", "put $500 into Tesla", "I want 10 Apple shares".
_SOFT_BUY = re.compile(
    r"\b(?:put|grab|pick\s+up|get|want|wanna|would\s+like|'d\s+like|d\s+like|own)\b", re.IGNORECASE
)
_NEGATED = re.compile(
    r"\b(?:don'?t|do\s+not|never|not|stop)\s+(?:to\s+)?(?:buy|sell|purchase|invest)", re.IGNORECASE
)
_STOCK_WORDS = re.compile(r"\b(?:shares?|stocks?|equity|equities)\b", re.IGNORECASE)
_RESEARCH = re.compile(
    r"\b(?:price|prices|quote|trading\s+at|doing|worth|valuation|p\s*/?\s*e|look\s+up|tell\s+me\s+about|"
    r"how\s+much\s+is|how\s+much\s+are|performance|performing|stock\s+(?:price|info))\b",
    re.IGNORECASE,
)
# Something a stock request is not about.
_NOT_A_COMPANY = re.compile(
    r"\b(?:portfolio|investments?|holdings?|balance|account|accounts|savings|wealth|money|bill|bills|"
    r"plan|plans|payee|payees|recommend\w*|suggest\w*|trending|ideas?|net|cash|card|cards|"
    r"transactions?|spending|spend|salary|loan|loans|mortgage|insurance|rates?|interest|fx|currency)\b",
    re.IGNORECASE,
)
_NUMBER_WORD = {
    "a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "couple": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_SHARE_WORDS = r"(?:whole\s+)?(?:shares?|stocks?|units?)"
_COUNT = (
    r"(?P<count>\d{1,3}(?:,\d{3})+|\d{1,6}|a\s+couple\s+of|a|an|single|"
    + _BARE_NUMBER + r")"
)
_QUANTITY = re.compile(r"(?<![\w$.])" + _COUNT + r"\s+(?:more\s+)?" + _SHARE_WORDS + r"\b", re.IGNORECASE)
_CURRENCY = r"(?P<cur>US\$|USD|S\$|SGD|\$)"
_AMOUNT_NUMBER = r"(?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)\s*(?P<scale>k|m|thousand|grand)?"
_MONEY_BEFORE = re.compile(r"(?<![\w$])" + _CURRENCY + r"\s*" + _AMOUNT_NUMBER + r"(?![\w.])", re.IGNORECASE)
_MONEY_AFTER = re.compile(
    r"(?<![\w$.])" + _AMOUNT_NUMBER + r"\s*(?P<word>singapore\s+dollars?|sing\s+dollars?|us\s+dollars?|"
    r"american\s+dollars?|dollars?|bucks?|sgd|usd)\b",
    re.IGNORECASE,
)
_WORD_MONEY = re.compile(
    r"(?<![\w$])(?P<words>" + _BARE_NUMBER + r")\s+(?P<word>singapore\s+dollars?|us\s+dollars?|dollars?|bucks?|sgd|usd)\b",
    re.IGNORECASE,
)
# "invest 300 in Apple": a bare number right after an investing verb is money.
_BARE_INVEST = re.compile(
    r"\b(?:invest|put)\s+(?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)\s*(?P<scale>k|thousand|grand)?\s+(?:in|into|on)\b",
    re.IGNORECASE,
)
# "buy 1k of Apple", "two grand of Tesla": thousands are always money.
_BARE_THOUSANDS = re.compile(
    r"(?<![\w$.])(?P<num>\d+(?:\.\d+)?)\s*(?P<scale>k|grand)\b(?!\s+(?:whole\s+)?(?:shares?|stocks?|units?))",
    re.IGNORECASE,
)
# "buy 2 nvidia": a bare count right after the verb, not followed by money words.
_VERB_COUNT = re.compile(
    r"\b(?:buy|purchase|sell|get\s+me|grab|acquire|offload)\s+(?P<count>\d{1,6}|" + _BARE_NUMBER + r")\s+"
    r"(?!(?:dollars?|bucks?|sgd|usd|percent|%|k\b|thousand|grand|worth))(?=[a-z])",
    re.IGNORECASE,
)
_ALL = re.compile(r"\b(?:all|everything|entire|whole\s+(?:position|lot|holding))\b|\ball\s+of\s+(?:it|them|my)\b", re.IGNORECASE)
_HALF = re.compile(r"\bhalf\b", re.IGNORECASE)

# Words around the company name in a request. What is left is the name.
_FILLER = frozenset("""
    i i'd id i'm im me my mine we us our you your can could would will should shall please pls kindly
    want wanna like love need to for of in into on at with from by and or the a an some any more another extra
    few couple single whole one share shares stock stocks unit units equity equities worth now today
    right away immediately asap straight just also then too so ok okay hey hi dimon um uh er
    buy buying bought purchase purchasing invest investing put acquire grab pick up get getting order own have
    sell selling sold offload dump liquidate cash out rid exit position holding holdings lot
    all everything entire half
    price prices quote quotes trading doing how how's hows what what's whats is are was tell about look check
    info information current currently latest today's todays value valuation pe p e performance performing much
    market open opens opening close closed
    dollars dollar bucks sgd usd singapore sing american k thousand grand
    let let's lets go ahead it them this that
""".split())
_POSSESSIVE = re.compile(r"(?:'s|’s|s')$", re.IGNORECASE)


@dataclass(frozen=True)
class StockRequest:
    action: str                       # BUY, SELL or RESEARCH
    label: str                        # the company as the user said it
    quantity: int | None = None
    allocation: Decimal | None = None
    explicit_currency: str | None = None
    bare_dollar: bool = False         # "$500" or "500 dollars": currency not named
    fraction: str | None = None       # ALL or HALF, for sells
    evidence: str | None = None       # the words that gave the size


def _count_value(raw: str) -> int | None:
    token = " ".join(raw.casefold().split())
    if token in {"a couple of"}:
        return 2
    if token in _NUMBER_WORD:
        return _NUMBER_WORD[token]
    if re.fullmatch(r"[\d,]+", token):
        value = int(token.replace(",", ""))
        return value if value > 0 else None
    try:
        minor = _bare_amount_minor(token)
    except GroundingError:
        return None
    return minor // 100 if minor % 100 == 0 and minor > 0 else None


def _scaled(number: str, scale: str | None) -> Decimal:
    value = Decimal(number.replace(",", ""))
    scale = (scale or "").casefold()
    if scale in {"k", "thousand", "grand"}:
        value *= 1000
    elif scale == "m":
        value *= 1_000_000
    return value


def _currency_of(token: str) -> tuple[str | None, bool]:
    token = " ".join(token.casefold().split())
    if token in {"us$", "usd"} or token.startswith(("us dollar", "american")):
        return "USD", False
    if token in {"s$", "sgd"} or token.startswith(("singapore", "sing ")):
        return "SGD", False
    return None, True


@dataclass(frozen=True)
class Size:
    quantity: int | None = None
    allocation: Decimal | None = None
    explicit_currency: str | None = None
    bare_dollar: bool = False
    fraction: str | None = None
    evidence: str | None = None
    spans: tuple[tuple[int, int], ...] = ()


def parse_size(text: str, *, allow_bare_count: bool = False) -> Size | None:
    """How many shares or how much money, from a request or a short reply.

    Returns None when no size is said, and raises ValueError when two
    different sizes are said ("5 shares for $500"), since guessing which one
    was meant could buy the wrong amount."""

    found: list[Size] = []
    for match in _MONEY_BEFORE.finditer(text):
        currency, bare = _currency_of(match.group("cur"))
        found.append(Size(allocation=_scaled(match.group("num"), match.group("scale")), explicit_currency=currency,
                          bare_dollar=bare, evidence=match.group(0), spans=(match.span(),)))
    for match in _MONEY_AFTER.finditer(text):
        if any(s <= match.start() < e for size in found for s, e in size.spans):
            continue
        currency, bare = _currency_of(match.group("word"))
        found.append(Size(allocation=_scaled(match.group("num"), match.group("scale")), explicit_currency=currency,
                          bare_dollar=bare, evidence=match.group(0), spans=(match.span(),)))
    for match in _WORD_MONEY.finditer(text):
        if any(s <= match.start() < e for size in found for s, e in size.spans) or re.match(r"\d", match.group("words")):
            continue
        try:
            minor = _bare_amount_minor(match.group("words"))
        except GroundingError:
            continue
        currency, bare = _currency_of(match.group("word"))
        found.append(Size(allocation=Decimal(minor) / 100, explicit_currency=currency, bare_dollar=bare,
                          evidence=match.group(0), spans=(match.span(),)))
    for match in _BARE_INVEST.finditer(text):
        if any(s <= match.start("num") < e for size in found for s, e in size.spans):
            continue
        found.append(Size(allocation=_scaled(match.group("num"), match.group("scale")), bare_dollar=True,
                          evidence=match.group(0), spans=(match.span("num"),)))
    for match in _BARE_THOUSANDS.finditer(text):
        if any(s <= match.start() < e for size in found for s, e in size.spans):
            continue
        found.append(Size(allocation=_scaled(match.group("num"), match.group("scale")), bare_dollar=True,
                          evidence=match.group(0), spans=(match.span(),)))
    for match in _QUANTITY.finditer(text):
        if any(s <= match.start() < e for size in found for s, e in size.spans):
            continue
        count = _count_value(match.group("count"))
        if count is not None:
            found.append(Size(quantity=count, evidence=match.group(0), spans=(match.span(),)))
    if not any(size.quantity for size in found):
        match = _VERB_COUNT.search(text)
        if match and not any(s <= match.start("count") < e for size in found for s, e in size.spans):
            count = _count_value(match.group("count"))
            if count is not None:
                found.append(Size(quantity=count, evidence=match.group("count"), spans=(match.span("count"),)))
    if allow_bare_count and not found:
        match = re.fullmatch(r"\W*(?:just\s+|make\s+it\s+|only\s+)?(?P<count>\d{1,6}|" + _BARE_NUMBER + r")\W*(?:please)?\W*",
                             text, re.IGNORECASE)
        if match:
            count = _count_value(match.group("count"))
            if count is not None:
                found.append(Size(quantity=count, evidence=match.group("count"), spans=(match.span("count"),)))
    fraction = "ALL" if _ALL.search(text) else "HALF" if _HALF.search(text) else None
    if not found:
        return Size(fraction=fraction) if fraction else None
    distinct = {(size.quantity, size.allocation, size.explicit_currency) for size in found}
    if len(distinct) > 1:
        raise ValueError("AMBIGUOUS_SIZE")
    first = found[0]
    return Size(first.quantity, first.allocation, first.explicit_currency, first.bare_dollar, fraction,
                first.evidence, tuple(span for size in found for span in size.spans))


def _label(text: str, spans: tuple[tuple[int, int], ...], after: int = 0) -> str | None:
    kept = list(text)
    for start, end in spans:
        kept[start:end] = " " * (end - start)
    # The company follows the verb: "help me buy Nvidia" -> "Nvidia". A name
    # said before it ("Apple, buy 5") is used only if nothing follows.
    remainder = "".join(kept)
    if after and _label(remainder[after:], ()) is not None:
        remainder = remainder[after:]
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9&.'’\-]*", remainder)
    name = []
    for word in words:
        bare = _POSSESSIVE.sub("", word.strip(".'’-"))
        if not bare or bare.casefold() in _FILLER or word.casefold() in _FILLER:
            if name:
                name.append(None)   # a gap: the name ended
            continue
        if re.fullmatch(r"[\d.,]+(?:k|m)?", bare, re.IGNORECASE):
            if name:
                name.append(None)
            continue
        name.append(bare)
    # The company is the first run of kept words ("buy 5 Coca Cola shares
    # for my savings" -> "Coca Cola").
    runs, current = [], []
    for word in name + [None]:
        if word is None:
            if current:
                runs.append(current)
            current = []
        else:
            current.append(word)
    if not runs:
        return None
    label = " ".join(runs[0])
    if len(runs[0]) > 6 or not re.search(r"[A-Za-z]", label):
        return None
    return label


def parse(text: str) -> StockRequest | None:
    """A stock request in the user's words, or None if it is not one."""

    if not text or _PAYMENT.search(text) or _RECURRING.search(text):
        return None
    try:
        size = parse_size(text)
    except ValueError:
        size = None
        ambiguous = True
    else:
        ambiguous = False
    sized = ambiguous or bool(size and (size.quantity or size.allocation))
    if _NEGATED.search(text):
        return None
    verb = _SELL.search(text)
    if verb:
        action = SELL
    elif (verb := _BUY.search(text)) or (
        (verb := _SOFT_BUY.search(text)) and (sized or _STOCK_WORDS.search(text))
    ):
        action = BUY
    elif _RESEARCH.search(text):
        action = RESEARCH
    else:
        return None
    spans = size.spans if size else ()
    label = _label(text, spans, verb.end() if verb else 0)
    if label is None or _NOT_A_COMPANY.search(label):
        return None
    if action == RESEARCH:
        return StockRequest(RESEARCH, label)
    if ambiguous:
        # Two sizes in one sentence: ask for one rather than guess.
        return StockRequest(action, label)
    if action == BUY and size and size.fraction and not (size.quantity or size.allocation):
        size = None
    if size is None:
        return StockRequest(action, label)
    return StockRequest(action, label, size.quantity, size.allocation, size.explicit_currency,
                        size.bare_dollar, size.fraction if action == SELL else None, size.evidence)

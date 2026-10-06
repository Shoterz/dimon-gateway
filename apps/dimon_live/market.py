"""A simulated market: fictional SGX-style instruments plus a few well-known
US companies, all with generated prices.

The local companies and funds are fictional. The US companies are real, so
their descriptions stay to plain, widely known facts about what they do, and
their prices are generated like every other price here: they are labelled
"simulated price" and are never a live quote. Prices follow a deterministic random walk per
instrument (the same for every session and every restart), updated every
15 minutes during the day, so "trending" and returns are computed from data
rather than invented. Everything is in Singapore dollars and cents.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

SGT = timezone(timedelta(hours=8))
EPOCH = date(2025, 1, 1)
DISCLAIMER = (
    "Simulated market: fictional local companies and funds, plus well-known US companies whose "
    "prices are generated here, not live quotes. Suggestions come from fixed rules "
    "applied to price data and your risk profile, not from the AI. Not financial advice: "
    "investments can fall as well as rise, and past performance does not guarantee future results."
)


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    kind: str                  # STOCK, ETF, REIT, BOND_ETF
    sector: str
    risk_level: int            # 1 (lowest) to 5 (highest)
    dividend_yield_bp: int     # basis points per year
    base_price_minor: int      # anchor price, cents
    annual_drift: float
    annual_volatility: float
    summary: str
    strengths: tuple[str, ...]
    risks: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    real_company: bool = False   # a real listed company with a simulated price


CATALOG: tuple[Instrument, ...] = (
    Instrument("MLB", "Merlion Bank Holdings", "STOCK", "Financials", 3, 520, 3450, 0.07, 0.18,
               "A large local bank with retail, business and wealth arms.",
               ("Pays a steady, well-covered dividend", "Strong capital buffer", "Growing digital banking customer base"),
               ("Profits fall if interest rates are cut", "Exposed to loan defaults in a downturn"),
               ("merlion", "merlion bank")),
    Instrument("STR", "Straits Telco", "STOCK", "Telecommunications", 2, 580, 245, 0.03, 0.14,
               "Mobile, broadband and data-centre services.",
               ("Recurring subscription revenue", "High dividend yield", "Data-centre arm growing fast"),
               ("Slow growth in a saturated home market", "Heavy spending on network upgrades"),
               ("straits", "straits telco")),
    Instrument("LCR", "Lion City Industrial REIT", "REIT", "Real estate", 3, 640, 212, 0.04, 0.16,
               "Owns logistics warehouses and business parks, paying out most of its rent.",
               ("Distributes about 90% of income as dividends", "Long leases with inflation-linked rents", "High occupancy"),
               ("Sensitive to interest-rate rises", "Tenant demand falls in recessions"),
               ("lion city", "lion city reit")),
    Instrument("EQX", "Equator Semiconductor", "STOCK", "Technology", 5, 40, 1880, 0.16, 0.42,
               "Designs chips for AI servers and electric vehicles.",
               ("Revenue growing quickly with AI demand", "Leading design patents", "Very strong long-term growth potential"),
               ("Very volatile price", "Chip cycles can cut profits sharply", "Little dividend"),
               ("equator",)),
    Instrument("HGE", "Harbourgreen Energy", "STOCK", "Renewable energy", 4, 150, 620, 0.10, 0.32,
               "Builds and runs solar and wind farms across Southeast Asia.",
               ("Long-term power contracts", "Benefits from the regional shift to clean energy"),
               ("Project delays and high debt", "Policy changes affect subsidies"),
               ("harbourgreen",)),
    Instrument("HCA", "Healthpoint Asia", "STOCK", "Healthcare", 3, 200, 1325, 0.08, 0.20,
               "Hospitals and clinics in Singapore and the region.",
               ("Ageing population supports demand", "Pricing power in private healthcare"),
               ("Regulation of healthcare fees", "Costly hospital expansions"),
               ("healthpoint",)),
    Instrument("SDX", "Singapore Blue Chip Index ETF", "ETF", "Broad market (Singapore)", 3, 410, 385, 0.06, 0.13,
               "Tracks a basket of the 30 largest companies listed in Singapore.",
               ("Instant diversification across 30 companies", "Low annual fee", "Regular dividends"),
               ("Concentrated in banks and property", "Falls with the local market"),
               ("blue chip", "blue chip etf", "singapore etf", "index etf", "sti etf")),
    Instrument("GWE", "Global World Equity ETF", "ETF", "Global equities", 3, 180, 1140, 0.08, 0.15,
               "Holds about 1,500 companies across developed and emerging markets.",
               ("Spread across thousands of companies and many countries", "Captures global economic growth", "Low cost core holding"),
               ("Currency movements affect returns", "Falls in global sell-offs"),
               ("global world", "global etf", "world etf", "world equity")),
    Instrument("ATI", "Asia Tech Innovators ETF", "ETF", "Technology (Asia)", 4, 60, 890, 0.12, 0.27,
               "Holds leading technology and internet companies across Asia.",
               ("Exposure to fast-growing Asian tech", "Diversified across about 50 companies"),
               ("Volatile, tech-heavy", "Regulatory risk in some markets"),
               ("asia tech", "tech etf")),
    Instrument("SGB", "Singapore Government Bond ETF", "BOND_ETF", "Government bonds", 1, 300, 1020, 0.015, 0.045,
               "Holds Singapore government bonds of different maturities.",
               ("Very low risk of default", "Steady interest income", "Cushions a portfolio when shares fall"),
               ("Low long-term growth", "Price dips when interest rates rise"),
               ("bond etf", "government bond", "bonds", "sg bond")),
    # Real companies with simulated prices (in S$). Plain, well-known facts only.
    Instrument("NVDA", "NVIDIA", "STOCK", "Technology (US)", 5, 3, 18000, 0.15, 0.48,
               "Designs graphics processors and AI accelerator chips used in data centres and PCs.",
               ("Leading supplier of chips for training AI models", "Software ecosystem that keeps developers on its chips"),
               ("Very volatile price", "Sales depend heavily on data-centre spending cycles"),
               ("nvidia", "nvidia corp", "nvidia corporation", "nvda"), real_company=True),
    Instrument("AAPL", "Apple", "STOCK", "Technology (US)", 3, 45, 29000, 0.09, 0.26,
               "Makes the iPhone, Mac and iPad, and sells services such as the App Store.",
               ("Very large, loyal customer base", "Growing services revenue"),
               ("Depends on iPhone sales", "Regulation of app stores"),
               ("apple", "apple inc", "aapl"), real_company=True),
    Instrument("MSFT", "Microsoft", "STOCK", "Technology (US)", 3, 70, 56000, 0.10, 0.24,
               "Sells Windows, Office and the Azure cloud platform.",
               ("Recurring software subscriptions", "Large cloud business"),
               ("Heavy spending on data centres", "Competition in cloud and AI"),
               ("microsoft", "msft"), real_company=True),
    Instrument("GOOGL", "Alphabet", "STOCK", "Technology (US)", 3, 45, 23000, 0.10, 0.28,
               "Owns Google Search, YouTube, Android and Google Cloud.",
               ("Dominant search advertising business", "Growing cloud division"),
               ("Advertising falls in downturns", "Antitrust cases against Google"),
               ("alphabet", "google", "googl"), real_company=True),
    Instrument("AMZN", "Amazon", "STOCK", "Consumer and cloud (US)", 4, 0, 27000, 0.11, 0.30,
               "Runs an online store and Amazon Web Services, a large cloud provider.",
               ("Large cloud business", "Huge logistics network"),
               ("Thin retail profit margins", "Pays no dividend"),
               ("amazon", "amzn"), real_company=True),
    Instrument("TSLA", "Tesla", "STOCK", "Automotive (US)", 5, 0, 40000, 0.12, 0.60,
               "Makes electric cars and batteries for homes and power grids.",
               ("Well-known electric vehicle brand", "Energy storage business"),
               ("Extremely volatile price", "Price competition from other car makers"),
               ("tesla", "tsla"), real_company=True),
)
BY_SYMBOL = {instrument.symbol: instrument for instrument in CATALOG}


def _seed(*parts: object) -> int:
    return int.from_bytes(hashlib.sha256(":".join(map(str, parts)).encode()).digest()[:8], "big")


@lru_cache(maxsize=64)
def _log_path(symbol: str, days: int) -> tuple[float, ...]:
    """Daily log-price offsets from the anchor, day 0 = EPOCH."""

    instrument = BY_SYMBOL[symbol]
    rng = random.Random(_seed("dimon-market-v1", symbol))
    daily_vol = instrument.annual_volatility / math.sqrt(252)
    daily_drift = instrument.annual_drift / 365 - daily_vol**2 / 2
    level, path = 0.0, []
    for _ in range(days + 1):
        path.append(level)
        level += daily_drift + daily_vol * rng.gauss(0, 1)
    return tuple(path)


def _day_index(day: date) -> int:
    return max(0, (day - EPOCH).days)


def close_minor(symbol: str, day: date) -> int:
    instrument = BY_SYMBOL[symbol]
    path = _log_path(symbol, _day_index(day) + 1)
    return max(1, round(instrument.base_price_minor * math.exp(path[_day_index(day)])))


def price_minor(symbol: str, now: datetime) -> int:
    """The live price: yesterday's close plus today's moves, in 15-minute steps."""

    instrument = BY_SYMBOL[symbol]
    local = now.astimezone(SGT)
    previous = close_minor(symbol, local.date() - timedelta(days=1))
    slot_vol = instrument.annual_volatility / math.sqrt(252 * 32)
    level = 0.0
    for slot in range(local.hour * 4 + local.minute // 15 + 1):
        level += slot_vol * random.Random(_seed("intraday", symbol, local.date().isoformat(), slot)).gauss(0, 1)
    return max(1, round(previous * math.exp(level)))


def change_pct(symbol: str, now: datetime, days: int) -> float:
    today = price_minor(symbol, now)
    past = close_minor(symbol, now.astimezone(SGT).date() - timedelta(days=days))
    return (today - past) / past * 100


def realised_volatility_pct(symbol: str, now: datetime, days: int = 90) -> float:
    end = now.astimezone(SGT).date()
    closes = [close_minor(symbol, end - timedelta(days=offset)) for offset in range(days, -1, -1)]
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    mean = sum(returns) / len(returns)
    return math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1) * 252) * 100


def snapshot(symbol: str, now: datetime) -> dict:
    instrument = BY_SYMBOL[symbol]
    return {
        "symbol": instrument.symbol,
        "name": instrument.name,
        "kind": instrument.kind,
        "sector": instrument.sector,
        "risk_level": instrument.risk_level,
        "dividend_yield_pct": instrument.dividend_yield_bp / 100,
        "price_minor": price_minor(symbol, now),
        "change_1d_pct": round(change_pct(symbol, now, 1), 2),
        "change_1m_pct": round(change_pct(symbol, now, 30), 2),
        "change_3m_pct": round(change_pct(symbol, now, 91), 2),
        "change_1y_pct": round(change_pct(symbol, now, 365), 2),
        "volatility_pct": round(realised_volatility_pct(symbol, now), 1),
        "summary": instrument.summary,
        "strengths": list(instrument.strengths),
        "risks": list(instrument.risks),
        "simulated_price": instrument.real_company,
    }


def history(symbol: str, now: datetime, days: int = 90) -> list[dict]:
    end = now.astimezone(SGT).date()
    return [
        {"date": (end - timedelta(days=offset)).isoformat(), "close_minor": close_minor(symbol, end - timedelta(days=offset))}
        for offset in range(days, 0, -1)
    ]

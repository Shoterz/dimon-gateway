"""Server-owned synthetic biller directory and deterministic name resolver.

The entries model supported destinations for the demo only. They are not
connected to real bill accounts or payment rails.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


ResolutionStatus = Literal["MATCH", "AMBIGUOUS", "UNSUPPORTED"]


@dataclass(frozen=True)
class Biller:
    biller_id: str
    display_name: str
    aliases: tuple[str, ...]
    statement_amount_minor: int | None = None


@dataclass(frozen=True)
class BillerResolution:
    status: ResolutionStatus
    biller: Biller | None = None
    candidates: tuple[Biller, ...] = ()


# This is deliberately a small, explicit synthetic directory. Add entries
# only when their canonical ID and demonstration label are defined here.
SYNTHETIC_BILLERS: tuple[Biller, ...] = (
    Biller(
        "biller_sp_services",
        "SP Services",
        ("SP Services", "Singapore Power", "SP Group", "Singapore Power Group"),
        8_642,
    ),
    Biller(
        "biller_singtel",
        "Singtel",
        ("Singtel", "Singapore Telecommunications", "Singapore Telecom"),
        5_800,
    ),
    Biller("biller_starhub", "StarHub", ("StarHub", "Star Hub")),
    Biller("biller_m1", "M1", ("M1", "M One")),
    Biller(
        "biller_pub_water",
        "PUB Water",
        ("PUB Water", "PUB", "Public Utilities Board", "Water", "PopWater"),
        3_280,
    ),
    Biller("biller_iras", "IRAS", ("IRAS", "Inland Revenue Authority of Singapore")),
    Biller("biller_dbs_card", "DBS Credit Card", ("DBS Credit Card", "DBS Card")),
)


def normalize_biller_name(value: str) -> str:
    """Normalize punctuation and spacing without fuzzy or phonetic matching."""

    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def resolve_biller(label: str) -> BillerResolution:
    """Resolve an exact alias, or return explicit prefix ambiguity/unsupported.

    Prefix matching is only used when there is no exact alias. The resolver
    never picks the first of multiple matches and never guesses from similarity.
    """

    normalized = normalize_biller_name(label)
    if not normalized:
        return BillerResolution("UNSUPPORTED")

    exact = tuple(
        biller
        for biller in SYNTHETIC_BILLERS
        if any(normalize_biller_name(alias) == normalized for alias in biller.aliases)
    )
    if len(exact) == 1:
        return BillerResolution("MATCH", biller=exact[0])
    if len(exact) > 1:
        return BillerResolution("AMBIGUOUS", candidates=exact)

    prefix = tuple(
        biller
        for biller in SYNTHETIC_BILLERS
        if any(
            normalize_biller_name(alias).startswith(normalized + " ")
            for alias in (biller.display_name, *biller.aliases)
        )
    )
    # One short component such as "M" is too broad to resolve or offer as a
    # useful selection. Require a meaningful prefix for candidate suggestions.
    if len(normalized) < 3:
        return BillerResolution("UNSUPPORTED")
    unique = {biller.biller_id: biller for biller in prefix}
    candidates = tuple(unique.values())
    if len(candidates) == 1:
        return BillerResolution("MATCH", biller=candidates[0])
    if len(candidates) > 1:
        return BillerResolution("AMBIGUOUS", candidates=candidates)
    return BillerResolution("UNSUPPORTED")

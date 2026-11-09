"""Cross-platform dedupe (Review Focus 4): one physical item = one deal = one push.

A new deal is a duplicate of a stored deal when it is the same listing, or when the key matches, prices are within 5% (PLN),
it is <= 7 days apart, and the seller hash matches (same platform) or the normalised city matches AND the titles
are similar (rapidfuzz token_set_ratio >= 85) (cross-platform: seller hashes include the source, so they never
match across platforms; a big city alone has many sellers of one model).
"""
from __future__ import annotations

import json
import unicodedata
from datetime import timedelta
from decimal import Decimal

PRICE_TOLERANCE = Decimal("0.05")
WINDOW = timedelta(days=7)
TITLE_SIMILARITY = 85  # rapidfuzz token_set_ratio, on lowercased titles without diacritics or punctuation


def _buy_pln(deal) -> Decimal | None:
    try:
        v = json.loads(deal.evidence_json or "{}").get("buy_pln")
        return None if v is None else Decimal(str(v))
    except (ValueError, ArithmeticError):
        return None


PC_KEY = "pc:bundle"


def components_of(store, deal) -> list[str] | None:
    """Sorted component set of a `pc:bundle` deal: `evidence.components` when the pipeline stored it, else the parts
    the rules find in the stored listing text (keys). None when unknown."""
    try:
        ev = json.loads(deal.evidence_json or "{}").get("components")
    except ValueError:
        ev = None
    if ev:
        return sorted(str(c) for c in ev)
    r = store.conn.execute("select title, description from listings where id=?", (deal.listing_ref,)).fetchone()
    if r is None:
        return None
    from .partout import rule_components  # lazy: partout pulls valuation and economics
    got = [c.key for c in rule_components(r["title"] or "", r["description"] or "")]
    return sorted(got) or None


def norm_city(city: str | None) -> str | None:
    """lowercase, no diacritics, whitespace collapsed; None when empty."""
    t = unicodedata.normalize("NFKD", (city or "").translate(str.maketrans("\u0142\u0141", "ll")))
    t = " ".join("".join(c for c in t if not unicodedata.combining(c)).lower().split())
    return t or None


def _listing(store, listing_ref: int):
    return store.conn.execute("select seller_hash, city, title from listings where id=?", (listing_ref,)).fetchone()


def similar_titles(a: str | None, b: str | None) -> bool:
    from rapidfuzz import fuzz, utils
    na, nb = norm_city(a), norm_city(b)  # the same folding: lowercase, no diacritics, whitespace collapsed
    return bool(na and nb) and fuzz.token_set_ratio(na, nb, processor=utils.default_process) >= TITLE_SIMILARITY


def _close(a: Decimal | None, b: Decimal | None) -> bool:
    return a is not None and b is not None and max(a, b) > 0 and abs(a - b) <= PRICE_TOLERANCE * max(a, b)


def is_duplicate(store, deal) -> bool:
    """Same listing; or same key, buy price within 5% (PLN) and created within 7 days, plus either
    (A) the same seller hash, or (B) on any platform, the same normalised city (missing city never matches) and
    similar titles (token_set_ratio >= 85)."""
    mine = _listing(store, deal.listing_ref)
    seller = mine["seller_hash"] if mine else None
    city = norm_city(mine["city"]) if mine else None
    price = _buy_pln(deal)
    mine_parts = components_of(store, deal) if deal.key == PC_KEY else None
    for other in store.deals():
        if deal.id is not None and other.id == deal.id:
            continue
        if other.listing_ref == deal.listing_ref:
            return True
        if not deal.key or other.key != deal.key or abs(deal.created_at - other.created_at) > WINDOW:
            continue
        if not _close(price, _buy_pln(other)):
            continue
        if deal.key == PC_KEY and components_of(store, other) != mine_parts:
            continue  # one key for every PC: two machines of one seller are the same item only with the same parts
        theirs = _listing(store, other.listing_ref)
        if theirs is None:
            continue
        if seller and theirs["seller_hash"] == seller:
            return True
        if city and norm_city(theirs["city"]) == city and similar_titles(mine["title"], theirs["title"]):
            return True
    return False

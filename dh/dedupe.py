"""Cross-platform dedupe (Review Focus 4): one physical item = one deal = one push.

A new deal is a duplicate of a stored deal when it is the same listing, or when seller hash, product key match,
the prices are within 5% (in PLN) and the stored deal was created within 7 days.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

PRICE_TOLERANCE = Decimal("0.05")
WINDOW = timedelta(days=7)


def _buy_pln(deal) -> Decimal | None:
    try:
        v = json.loads(deal.evidence_json or "{}").get("buy_pln")
        return None if v is None else Decimal(str(v))
    except (ValueError, ArithmeticError):
        return None


def _seller(store, listing_ref: int) -> str | None:
    r = store.conn.execute("select seller_hash from listings where id=?", (listing_ref,)).fetchone()
    return r["seller_hash"] if r else None


def is_duplicate(store, deal) -> bool:
    mine_seller = _seller(store, deal.listing_ref)
    mine_price = _buy_pln(deal)
    for other in store.deals():
        if deal.id is not None and other.id == deal.id:
            continue
        if other.listing_ref == deal.listing_ref:
            return True
        if not mine_seller or not deal.key or other.key != deal.key:
            continue
        if abs(deal.created_at - other.created_at) > WINDOW:
            continue
        if _seller(store, other.listing_ref) != mine_seller:
            continue
        theirs = _buy_pln(other)
        if mine_price is None or theirs is None or max(mine_price, theirs) <= 0:
            continue
        if abs(mine_price - theirs) <= PRICE_TOLERANCE * max(mine_price, theirs):
            return True
    return False

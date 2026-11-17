"""Tori.fi (FI).

The HTML search page renders client-side, but robots.txt allows the JSON endpoint it calls:
`/recommerce/forsale/search/api/search/SEARCH_ID_BAP_COMMON?q=<query>` — same Adevinta "BAP" family as
Blocket/Finn. Docs carry price, location, coordinates, epoch-ms timestamp, images and flags
(`sold`, `shipping_exists`, `buy_now`). `trade_type` is Finnish: Myydään = for sale, Ostetaan = wanted.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote_plus

from ..models import Listing
from .base import Adapter, AdapterError, Query, norm, price_of, product_ld, seller_hash

BASE = "https://www.tori.fi"
API = f"{BASE}/recommerce/forsale/search/api/search/SEARCH_ID_BAP_COMMON"
WANTED_TYPES = {"ostetaan", "vaihdetaan", "vaihto"}  # Ostetaan = wanted, Vaihdetaan/Vaihto = a swap


class Tori(Adapter):
    name = "tori"
    domain = "www.tori.fi"
    country = "FI"
    currency = "EUR"
    gone_markers = ("ilmoitus on poistettu", "tämä ilmoitus on myyty", "ad has been removed")

    id_in_final_url = True

    def search_url(self, q: Query) -> str:
        return f"{API}?q={quote_plus(q.text)}"

    def page_urls(self, q: Query) -> list[str]:
        return [self.search_url(q)]

    async def _get(self, fetcher, url: str, kind: str = "json"):
        return await super()._get(fetcher, url, kind="json")

    def parse_page(self, text: str) -> list[Listing]:
        return parse_search(text)

    def _parse_detail(self, listing: Listing, html: str) -> Listing:
        ld = product_ld(html)
        full = norm(ld.get("description"))
        if full:
            listing.description = full
        imgs = ld.get("image") or []
        if isinstance(imgs, str):
            imgs = [imgs]
        if imgs:
            listing.photos = imgs
        return listing

    def detail_price(self, html: str) -> Decimal | None:
        pr = price_of(str(((product_ld(html)).get("offers") or {}).get("price") or ""), "EUR")
        return pr[0] if pr else None


ADAPTER = Tori()


def parse_search(text: str) -> list[Listing]:
    import json
    try:
        docs = (json.loads(text).get("docs") or [])
    except ValueError as e:
        raise AdapterError(f"tori: search API did not return JSON ({e!r})") from e
    out: list[Listing] = []
    for d in docs:
        flags = set(d.get("flags") or [])
        if "sold" in flags or (d.get("trade_type") or "").lower() in WANTED_TYPES:
            continue
        p = d.get("price") or {}
        amt = p.get("amount")
        if amt is None or amt <= 0:
            continue
        pr = price_of(str(amt), "EUR")
        if pr is None:
            continue
        iid = d.get("id")
        url = d.get("canonical_url")
        if not iid or not url:
            continue
        ts = d.get("timestamp")
        posted = datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if ts else None
        coords = d.get("coordinates") or {}
        photos = [("https:" + u if str(u).startswith("//") else u) for u in (d.get("image_urls") or [])]
        l = ADAPTER.make(
            source_id=str(iid), url=url, title=norm(d.get("heading")),
            description=norm(d.get("heading")),  # the search doc carries no body; detail fills it
            price=pr[0], currency=p.get("currency_code") or "EUR",
            city=norm(d.get("location")) or None, lat=coords.get("lat"), lon=coords.get("lon"),
            seller_hash=None,  # search docs expose no seller identity
            is_business="retailer" in flags,
            ships=True if "shipping_exists" in flags else None,
            photos=photos, posted_at=posted)
        out.append(l)
    return out

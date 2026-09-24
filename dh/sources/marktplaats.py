"""Marktplaats.nl (NL).

robots.txt forbids the JSON endpoint `/lrp/api/search*` (and `/search?q=*`), so it is NOT used. The HTML search
page `/q/<query>/` is allowed; it embeds the same listing objects in `__NEXT_DATA__`
(props.pageProps.searchRequestAndResponse.listings), which is what the adapter parses.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from ..models import Listing
from .base import Adapter, AdapterError, Query, abs_url, in_range, local_dt, norm, now_utc, price_of, seller_hash

BASE = "https://www.marktplaats.nl"
LIMIT = 30
# FIXED = asking price; FAST_BID = "Bieden" with an asking price. MIN_BID is a starting bid, SEE_DESCRIPTION/
# RESERVED/FREE etc. have no usable price.
PRICE_TYPES = {"FIXED", "FAST_BID"}
NL_MONTHS = {"jan": 1, "feb": 2, "mrt": 3, "apr": 4, "mei": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "okt": 10,
             "nov": 11, "dec": 12}
WANTED_RE = re.compile(r"^\W*(gezocht|gevraagd|ik zoek|wanted)\b", re.I)  # buyer's ads ("GEZOCHT: Gamepc met RTX 4090")
DATE_RE = re.compile(r"^(\d{1,2})\s+([a-z]{3})\.?\s+'?(\d{2,4})$", re.I)


def _posted(text: str | None) -> datetime | None:
    if not text:
        return None
    t = text.strip().lower()
    today = now_utc().astimezone(ZoneInfo("Europe/Amsterdam")).replace(
        hour=0, minute=0, second=0, microsecond=0)
    if t == "vandaag":
        return today.astimezone(timezone.utc)
    if t == "gisteren":
        return (today - timedelta(days=1)).astimezone(timezone.utc)
    m = DATE_RE.match(t)
    if m and m.group(2) in NL_MONTHS:
        y = int(m.group(3))
        y += 2000 if y < 100 else 0
        return local_dt(datetime(y, NL_MONTHS[m.group(2)], int(m.group(1))), "Europe/Amsterdam")
    return None


class Marktplaats(Adapter):
    name = "marktplaats"
    domain = "www.marktplaats.nl"
    country = "NL"
    currency = "EUR"
    gone_markers = ("deze advertentie is niet meer beschikbaar", "advertentie niet gevonden")

    def search_url(self, q: Query) -> str:
        return f"{BASE}/q/{quote_plus(q.text)}/"

    async def search(self, query: Query, fetcher) -> list[Listing]:
        res = await self._get(fetcher, self.search_url(query))
        if res.status != 200:
            raise AdapterError(f"{self.name}: search status {res.status}")
        return parse_search(res.text, query)

    def parse_detail(self, listing: Listing, html: str) -> Listing:
        ld = _product_ld(html)
        full = norm(ld.get("description"))
        if full:
            # keep the structured lines (Staat/Levering) the search JSON added
            tail = [l for l in listing.description.splitlines() if l.startswith(("Staat:", "Levering:"))]
            listing.description = "\n".join([full, *tail])
        imgs = ld.get("image") or []
        if isinstance(imgs, str):
            imgs = [imgs]
        if imgs:
            listing.photos = imgs
        return listing

    def detail_price(self, html: str) -> Decimal | None:
        pr = price_of(str(((_product_ld(html)).get("offers") or {}).get("price") or ""), "EUR")
        return pr[0] if pr else None


def _product_ld(html: str) -> dict:
    for m in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "Product":
            return d
    return {}


ADAPTER = Marktplaats()


NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def extract_listings(html: str) -> list[dict]:
    m = NEXT_DATA_RE.search(html)
    if not m:
        raise AdapterError("marktplaats: __NEXT_DATA__ not found (page layout changed or interstitial)")
    try:
        data = json.loads(m.group(1))
        return data["props"]["pageProps"]["searchRequestAndResponse"]["listings"] or []
    except (ValueError, KeyError, TypeError) as e:
        raise AdapterError(f"marktplaats: unexpected __NEXT_DATA__ shape ({e!r})") from e


def parse_search(html: str, q: Query | None = None) -> list[Listing]:
    items = extract_listings(html)
    out: list[Listing] = []
    for it in items:
        info = it.get("priceInfo") or {}
        if info.get("priceType") not in PRICE_TYPES or it.get("reserved"):
            continue
        cents = info.get("priceCents") or 0
        pr = price_of(str(Decimal(cents) / 100), "EUR") if cents else None
        if pr is None:
            continue
        if WANTED_RE.match(it.get("title") or ""):
            continue
        iid = it.get("itemId")
        if not iid or not it.get("vipUrl"):
            continue
        loc = it.get("location") or {}
        attrs = {a.get("key"): a.get("value") for a in (it.get("attributes") or [])}
        extra = []
        if attrs.get("condition"):
            extra.append(f"Staat: {attrs['condition']}")
        delivery = attrs.get("delivery") or ""
        if delivery:
            extra.append(f"Levering: {delivery}")
        ships = None
        pickup_only = False
        if delivery:
            low = delivery.lower()
            ships = "verzend" in low
            pickup_only = low.strip() == "ophalen"
        photos = [("https:" + u if u.startswith("//") else u) for u in (it.get("imageUrls") or [])]
        sid = (it.get("sellerInformation") or {}).get("sellerId")
        l = ADAPTER.make(
            source_id=iid, url=abs_url(BASE, it["vipUrl"]), title=norm(it.get("title")),
            description="\n".join(x for x in [norm(it.get("description")), *extra] if x), price=pr[0], currency="EUR",
            city=loc.get("cityName"), lat=loc.get("latitude"), lon=loc.get("longitude"),
            seller_hash=seller_hash("marktplaats", sid), ships=ships, pickup_only=pickup_only, photos=photos,
            posted_at=_posted(it.get("date")))
        if q is None or in_range(l.price, l.currency, q):
            out.append(l)
    return out

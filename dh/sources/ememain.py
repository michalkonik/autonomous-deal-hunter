"""2ememain.be (BE) — the Belgian twin of Marktplaats (same platform, same `__NEXT_DATA__` search payload).

robots.txt forbids `/search?q=*`; the HTML search page `/q/<query>/` is allowed and embeds the listing objects
in `__NEXT_DATA__` (props.pageProps.searchRequestAndResponse.listings), which is what the adapter parses.
The page is served in French ("Aujourd'hui", "18 oct. 26") while structured `attributes` values come in Dutch
("Verzenden"/"Ophalen") — both languages are handled.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..models import Listing
from .base import Adapter, Query, local_dt, norm, now_utc, price_of, product_ld, seller_hash
from .marktplaats import NEXT_DATA_RE, extract_listings  # same platform, same payload contract

BASE = "https://www.2ememain.be"
PRICE_TYPES = {"FIXED", "FAST_BID"}
FR_MONTHS = {"janv": 1, "févr": 2, "fevr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6, "juil": 7, "août": 8,
             "aout": 8, "sept": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12}
WANTED_RE = re.compile(r"^\W*(recherche|cherché|cherche|gezocht|wanted)\b", re.I)
DATE_RE = re.compile(r"^(\d{1,2})\s+([a-zûôé]{3,4}\.?)\s+'?(\d{2,4})$", re.I)
_TODAY = ("aujourd'hui", "aujourd hui", "vandaag")
_YESTERDAY = ("hier", "gisteren")


def _posted(text: str | None) -> datetime | None:
    if not text:
        return None
    t = text.strip().lower().replace("\u2019", "'")
    today = now_utc().astimezone(ZoneInfo("Europe/Brussels")).replace(hour=0, minute=0, second=0, microsecond=0)
    if t in _TODAY:
        return today.astimezone(timezone.utc)
    if t in _YESTERDAY:
        return (today - timedelta(days=1)).astimezone(timezone.utc)
    m = DATE_RE.match(t.replace(".", ""))
    if m:
        month = FR_MONTHS.get(m.group(2)[:4]) or FR_MONTHS.get(m.group(2)[:3])
        if month:
            y = int(m.group(3))
            y += 2000 if y < 100 else 0
            return local_dt(datetime(y, month, int(m.group(1))), "Europe/Brussels")
    return None


class E2emain(Adapter):
    name = "2ememain"
    domain = "www.2ememain.be"
    country = "BE"
    currency = "EUR"
    gone_markers = ("cette annonce n'est plus disponible", "annonce introuvable", "deze advertentie is niet meer")

    id_in_final_url = True

    def search_url(self, q: Query) -> str:
        from urllib.parse import quote_plus
        return f"{BASE}/q/{quote_plus(q.text)}/"

    def page_urls(self, q: Query) -> list[str]:
        return [self.search_url(q)]

    def parse_page(self, text: str) -> list[Listing]:
        return parse_search(text)

    def _parse_detail(self, listing: Listing, html: str) -> Listing:
        ld = product_ld(html)
        full = norm(ld.get("description"))
        if full:
            tail = [l for l in listing.description.splitlines() if l.startswith(("État:", "Staat:", "Livraison:", "Levering:"))]
            listing.description = "\n".join([full, *tail])
        imgs = ld.get("image") or []
        if isinstance(imgs, str):
            imgs = [imgs]
        if imgs:
            listing.photos = imgs
        return listing

    def detail_price(self, html: str) -> "Decimal | None":
        pr = price_of(str(((product_ld(html)).get("offers") or {}).get("price") or ""), "EUR")
        return pr[0] if pr else None


ADAPTER = E2emain()


def parse_search(html: str) -> list[Listing]:
    items = extract_listings(html)  # raises AdapterError with the 2ememain page when the payload vanished
    out: list[Listing] = []
    for it in items:
        info = it.get("priceInfo") or {}
        if info.get("priceType") not in PRICE_TYPES or it.get("reserved"):
            continue
        cents = info.get("priceCents") or 0
        from decimal import Decimal
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
            extra.append(f"État: {attrs['condition']}")
        delivery = attrs.get("delivery") or ""
        if delivery:
            extra.append(f"Livraison: {delivery}")
        ships = None
        pickup_only = False
        if delivery:
            low = delivery.lower()
            ships = ("verzend" in low) or ("envoi" in low)
            pickup_only = low.strip() in ("ophalen", "enlèvement sur place", "retrait sur place")
        photos = [("https:" + u if u.startswith("//") else u) for u in (it.get("imageUrls") or [])]
        sid = (it.get("sellerInformation") or {}).get("sellerId")
        from .base import abs_url
        l = ADAPTER.make(
            source_id=iid, url=abs_url(BASE, it["vipUrl"]), title=norm(it.get("title")),
            description="\n".join(x for x in [norm(it.get("description")), *extra] if x), price=pr[0], currency="EUR",
            city=loc.get("cityName"), lat=loc.get("latitude"), lon=loc.get("longitude"),
            seller_hash=seller_hash("2ememain", sid), ships=ships, pickup_only=pickup_only, photos=photos,
            posted_at=_posted(it.get("date")))
        out.append(l)
    return out

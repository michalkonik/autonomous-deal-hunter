"""sprzedajemy.pl (PL).

robots.txt has `Disallow: *inp_*`, which covers every search URL (`?inp_text[v]=...`) and every sort option
(`sort=inp_srt_...`). So the search form is NOT used: the adapter browses the allowed category listing pages
(`/elektronika/.../karty-graficzne`, `?offset=30` for page 2) and filters locally against the query words.
Detail pages carry schema.org Product JSON-LD.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal

from selectolax.lexbor import LexborHTMLParser

from ..models import Listing
from .base import (Adapter, AdapterError, Query, abs_url, age_days, in_range, kinds_for, local_dt, matches_query,
                   norm, price_of, seller_hash)

BASE = "https://sprzedajemy.pl"
CARD = "article.element"
CARD_LINK = "h2.title a.offerLink"
CARD_PRICE = ".pricing .price"
CARD_SELLER_TYPE = ".seller-type-info__label"
CARD_CITY = ".location .city"
CARD_TIME = "time.time"
CARD_IMG = "li.has.photo img"
ID_RE = re.compile(r"-nr(\d+)(?:[/?#]|$)")
MONTHS = {"sty": 1, "lut": 2, "mar": 3, "kwi": 4, "maj": 5, "cze": 6, "lip": 7, "sie": 8, "wrz": 9,
          "paź": 10, "paz": 10, "lis": 11, "gru": 12}


class Sprzedajemy(Adapter):
    name = "sprzedajemy"
    domain = "sprzedajemy.pl"
    country = "PL"
    currency = "PLN"
    gone_markers = ("ogłoszenie nieaktualne", "ogłoszenie zakończone", "ogłoszenie wygasło",
                    "ogłoszenie zostało usunięte", "oferta nieaktualna")
    id_in_final_url = True

    pages = 2
    PAGE = 30
    ELEKTRONIKA = "/elektronika/komputery-i-akcesoria"
    CATEGORIES = {
        "gpu": ELEKTRONIKA + "/podzespoly-bazowe/karty-graficzne",
        "cpu": ELEKTRONIKA + "/podzespoly-bazowe/procesory",
        "ram": ELEKTRONIKA + "/podzespoly-bazowe/pamieci",
        "laptop": ELEKTRONIKA + "/laptopy-i-netbooki",
        "disk": ELEKTRONIKA + "/dyski-napedy-i-nosniki",
        "monitor": ELEKTRONIKA + "/monitory-i-projektory",
        "tablet": ELEKTRONIKA + "/tablety-i-palmtopy",
    }

    def page_urls(self, q: Query) -> list[str]:
        paths = [self.CATEGORIES[k] for k in kinds_for(q) if k in self.CATEGORIES] or [self.ELEKTRONIKA]
        return [f"{BASE}{p}" + (f"?offset={self.PAGE * i}" if i else "") for p in paths for i in range(self.pages)]

    async def search(self, query: Query, fetcher) -> list[Listing]:
        out: dict[str, Listing] = {}
        urls = self.page_urls(query)
        failed = 0
        for url in urls:
            res = await self._get(fetcher, url)
            if res.status != 200:
                failed += 1
                continue
            for l in parse_search(res.text, query):
                out.setdefault(l.source_id, l)
        if failed == len(urls):
            raise AdapterError(f"{self.name}: every category page failed")
        return list(out.values())

    def parse_detail(self, listing: Listing, html: str) -> Listing:
        tree = LexborHTMLParser(html)
        ld = _product_ld(html)
        d = tree.css_first(".offerDescription")
        desc = norm(d.text(separator="\n")) if d is not None else norm(ld.get("description"))
        extra = []
        cond = ((ld.get("offers") or {}).get("itemCondition") or "")
        if "UsedCondition" in cond:
            extra.append("Stan: używany")
        elif "NewCondition" in cond:
            extra.append("Stan: nowy")
        listing.description = "\n".join(x for x in [desc, *extra] if x) or listing.description
        imgs = ld.get("image") or []
        if isinstance(imgs, str):
            imgs = [imgs]
        if imgs:
            listing.photos = imgs
        name = tree.css_first(".userInfo .name")
        if name is not None:
            listing.seller_hash = seller_hash(self.name, name.attributes.get("title") or name.text(strip=True))
        m = re.search(r"Na Sprzedajemy\.pl od (\w{3})\w*\s+(\d{4})", html)
        if m and m.group(1).lower() in MONTHS:
            listing.seller_age_days = age_days(datetime(int(m.group(2)), MONTHS[m.group(1).lower()], 1))
        stype = tree.css_first(".seller-type-info__label")
        if stype is not None:
            listing.is_business = "prywatn" not in stype.text().lower()
        return listing

    def detail_price(self, html: str) -> Decimal | None:
        ld = _product_ld(html)
        pr = price_of(str((ld.get("offers") or {}).get("price") or ""), "PLN")
        if pr:
            return pr[0]
        n = LexborHTMLParser(html).css_first(".price")
        pr = price_of(n.text() if n is not None else "", "PLN")
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


ADAPTER = Sprzedajemy()


def parse_search(html: str, q: Query | None = None) -> list[Listing]:
    tree = LexborHTMLParser(html)
    out: list[Listing] = []
    for card in tree.css(CARD):
        a = card.css_first(CARD_LINK)
        if a is None:
            continue
        href = a.attributes.get("href") or ""
        m = ID_RE.search(href)
        if not m:
            continue
        pn = card.css_first(CARD_PRICE)
        pr = price_of(pn.text() if pn is not None else "", "PLN")
        if pr is None:
            continue
        st = card.css_first(CARD_SELLER_TYPE)
        city = card.css_first(CARD_CITY)
        tm = card.css_first(CARD_TIME)
        img = card.css_first(CARD_IMG)
        posted = None
        if tm is not None and tm.attributes.get("datetime"):
            try:
                posted = local_dt(datetime.strptime(tm.attributes["datetime"], "%Y-%m-%d %H:%M:%S"), "Europe/Warsaw")
            except ValueError:
                pass
        l = ADAPTER.make(source_id=m.group(1), url=abs_url(BASE, href), title=norm(a.text()), price=pr[0],
                         currency=pr[1], city=norm(city.text()) if city is not None else None,
                         is_business=("prywatn" not in st.text().lower()) if st is not None else None,
                         photos=[img.attributes["src"]] if img is not None and img.attributes.get("src") else [],
                         posted_at=posted)
        if q is None or (in_range(l.price, l.currency, q) and matches_query(l, q)):
            out.append(l)
    return out

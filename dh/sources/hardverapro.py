"""HardverApró (HU). Server-rendered listing table. robots.txt only forbids `keres.php?...offset=`, so only
page 1 of a search (newest first) is read."""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from urllib.parse import quote_plus

from selectolax.lexbor import LexborHTMLParser

from ..models import Listing
from .base import (Adapter, AdapterError, Query, abs_url, in_range, local_dt, norm, price_of, relative_day,
                   seller_hash, years_to_days)

BASE = "https://hardverapro.hu"
CARD = "li.media[data-uadid]"
CARD_TITLE = ".uad-col-title h1 a"
CARD_PRICE = ".uad-col-title .uad-price .text-nowrap"
CARD_CITY = ".uad-cities"
CARD_USER = ".uad-user-text a"
CARD_RATING = ".uad-rating"
CARD_TIME = ".uad-col-info .uad-time time"
CARD_IMG = ".uad-col-image img"
TZ = "Europe/Budapest"
REL_RE = re.compile(r"^(ma|tegnap)\s+(\d{1,2}):(\d{2})$")
ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:\s+(\d{2}):(\d{2}))?$")


def _posted(text: str) -> datetime | None:
    text = norm(text)
    m = REL_RE.match(text)
    if m:
        return relative_day(m.group(1), int(m.group(2)), int(m.group(3)), TZ, ("ma",), ("tegnap",))
    m = ISO_RE.match(text)
    if m:
        return local_dt(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0),
                                 int(m.group(5) or 0)), TZ)
    return None


class Hardverapro(Adapter):
    name = "hardverapro"
    domain = "hardverapro.hu"
    country = "HU"
    currency = "HUF"
    gone_markers = ("a hirdetés nem elérhető", "a hirdetés lejárt", "a hirdetés törölve", "törölt hirdetés",
                    "a keresett hirdetés nem található")

    def search_url(self, q: Query) -> str:
        return f"{BASE}/aprok/keres.php?stext={quote_plus(q.text)}&buying=0"

    async def search(self, query: Query, fetcher) -> list[Listing]:
        res = await self._get(fetcher, self.search_url(query))
        if res.status != 200:
            raise AdapterError(f"{self.name}: search status {res.status}")
        return parse_search(res.text, query)

    def parse_detail(self, listing: Listing, html: str) -> Listing:
        tree = LexborHTMLParser(html)
        parts = []
        d = tree.css_first(".rtif-content.uad-content")
        if d is not None:
            parts.append(norm(d.text(separator="\n")))
        for tr in tree.css(".uad-details tr"):
            th, td = tr.css_first("th"), tr.css_first("td")
            if th is not None and td is not None:
                parts.append(f"{norm(th.text())} {norm(td.text())}")
        listing.description = "\n".join(p for p in parts if p) or listing.description
        photos = [abs_url("https://hardverapro.hu", im.attributes["src"]) for im in tree.css(".carousel img")
                  if im.attributes.get("src")]
        if photos:
            listing.photos = list(dict.fromkeys(photos))
        block = tree.css_first(".uad-time-location")
        if block is not None:
            u = block.css_first("a[href^='/tag/']")
            if u is not None:
                listing.seller_hash = seller_hash(self.name, u.text(strip=True))
            txt = norm(block.text())
            m = re.search(r"(\d+) éve regisztrált", txt)
            if m:
                listing.seller_age_days = years_to_days(int(m.group(1)))
            else:
                m = re.search(r"(\d+) hónapja regisztrált", txt)
                if m:
                    listing.seller_age_days = int(m.group(1)) * 30
            m = re.search(r"(\d+) pozitív", txt)
            if m:
                listing.seller_ratings = int(m.group(1))
            m = re.search(r"Feladás időpontja\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", txt)
            if m:
                listing.posted_at = _posted(m.group(1)) or listing.posted_at
        return listing

    def detail_price(self, html: str) -> Decimal | None:
        n = LexborHTMLParser(html).css_first(".uad-details h2")
        pr = price_of(n.text() if n is not None else "", "HUF")
        return pr[0] if pr else None


ADAPTER = Hardverapro()


def parse_search(html: str, q: Query | None = None) -> list[Listing]:
    tree = LexborHTMLParser(html)
    out: list[Listing] = []
    for li in tree.css(CARD):
        cls = li.attributes.get("class") or ""
        if "uad-status-iced" in cls:  # reserved / on hold ("jegelve")
            continue
        a = li.css_first(CARD_TITLE)
        pn = li.css_first(CARD_PRICE)
        if a is None or pn is None:
            continue
        pr = price_of(pn.text(), "HUF")  # "Csere", "Keresek", "" -> None
        if pr is None:
            continue
        city = li.css_first(CARD_CITY)
        user = li.css_first(CARD_USER)
        rating = li.css_first(CARD_RATING)
        tm = li.css_first(CARD_TIME)
        img = li.css_first(CARD_IMG)
        ratings = None
        if rating is not None:
            m = re.search(r"(\d+)", rating.attributes.get("title") or rating.text())
            ratings = int(m.group(1)) if m else None
        src = img.attributes.get("data-retina-url") or img.attributes.get("src") if img is not None else None
        l = ADAPTER.make(source_id=li.attributes["data-uadid"], url=abs_url(BASE, a.attributes.get("href") or ""),
                         title=norm(a.text()), price=pr[0], currency=pr[1],
                         city=norm(city.text()) if city is not None else None,
                         seller_hash=seller_hash("hardverapro", user.text(strip=True)) if user is not None else None,
                         seller_ratings=ratings, is_business=True if "uad-business-user" in cls else None,
                         photos=[abs_url("https://hardverapro.hu", src)] if src else [],
                         posted_at=_posted(tm.text()) if tm is not None else None)
        if q is None or in_range(l.price, l.currency, q):
            out.append(l)
    return out

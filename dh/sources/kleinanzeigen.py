"""Kleinanzeigen.de (DE). Server-rendered Astro pages; utility-class CSS, so selectors are structural
(article[data-adid], h3 a, text patterns) rather than class names."""
from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from urllib.parse import quote

from selectolax.lexbor import LexborHTMLParser

from ..models import Listing
from .base import Adapter, Query, abs_url, age_days, local_dt, norm, price_of, relative_day, seller_hash

BASE = "https://www.kleinanzeigen.de"
CARD = "#srchrslt-adtable article[data-adid]"
CARD_TITLE = "h3 a"
CARD_IMG = "img"
PLZ_RE = re.compile(r"^\d{5}\b")
TIME_RE = re.compile(r"^(Heute|Gestern),\s*(\d{1,2}):(\d{2})$|^(\d{2})\.(\d{2})\.(\d{4})$")
TZ = "Europe/Berlin"


def _posted(text: str) -> datetime | None:
    m = TIME_RE.match(text.strip())
    if not m:
        return None
    if m.group(1):
        return relative_day(m.group(1), int(m.group(2)), int(m.group(3)), TZ, ("heute",), ("gestern",))
    return local_dt(datetime(int(m.group(6)), int(m.group(5)), int(m.group(4))), TZ)


class Kleinanzeigen(Adapter):
    name = "kleinanzeigen"
    domain = "www.kleinanzeigen.de"
    country = "DE"
    currency = "EUR"
    gone_markers = ("anzeige ist nicht mehr verfügbar", "die anzeige wurde gelöscht",
                    "diese anzeige ist nicht mehr", "anzeige wurde deaktiviert")
    id_in_final_url = True

    def search_url(self, q: Query) -> str:
        slug = re.sub(r"[^\w]+", "-", q.text.strip().lower()).strip("-")
        return f"{BASE}/s-{quote(slug)}/k0"

    def page_urls(self, q: Query) -> list[str]:
        return [self.search_url(q)]

    def parse_page(self, text: str) -> list[Listing]:
        return parse_search(text)

    def _parse_detail(self, listing: Listing, html: str) -> Listing:
        tree = LexborHTMLParser(html)
        d = tree.css_first("#viewad-description-text")
        parts = [norm(d.text(separator="\n")) if d is not None else ""]
        kv = []
        for li in tree.css("#viewad-details li.addetailslist--detail"):
            v = li.css_first(".addetailslist--detail--value")
            if v is None:
                continue
            val = norm(v.text())
            label = norm(li.text().replace(v.text(), "", 1))
            kv.append(f"{label}: {val}")
        if kv:
            parts.append("; ".join(kv))
        listing.description = "\n".join(p for p in parts if p) or listing.description
        photos = []
        for s in tree.css('#viewad-product script[type="application/ld+json"]'):
            try:
                j = json.loads(s.text())
            except ValueError:
                continue
            if isinstance(j, dict) and j.get("contentUrl"):
                photos.append(j["contentUrl"])
        if photos:
            listing.photos = list(dict.fromkeys(photos))
        m = re.search(r'"posterid":"(\d+)"', html)
        if m:
            listing.seller_hash = seller_hash(self.name, m.group(1))
        m = re.search(r'"Verkaeufer":"(\w+)"', html)
        if m:
            listing.is_business = m.group(1) != "privat"
        m = re.search(r'"versand":"(true|false)"', html)
        if m:
            listing.ships = m.group(1) == "true"
        m = re.search(r"Aktiv seit (\d{2})\.(\d{2})\.(\d{4})", html)
        if m:
            listing.seller_age_days = age_days(datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))))
        loc = tree.css_first("#viewad-locality")
        if loc is not None:
            listing.city = norm(loc.text())
        return listing

    def detail_price(self, html: str) -> Decimal | None:
        n = LexborHTMLParser(html).css_first("#viewad-price")
        pr = price_of(n.text() if n is not None else "", "EUR")
        return pr[0] if pr else None


ADAPTER = Kleinanzeigen()


def parse_search(html: str) -> list[Listing]:
    tree = LexborHTMLParser(html)
    out: list[Listing] = []
    for card in tree.css(CARD):
        for s in card.css("script,style,svg"):
            s.decompose()
        adid = card.attributes.get("data-adid")
        a = card.css_first(CARD_TITLE)
        if not adid or a is None:
            continue
        texts = [norm(x.text()) for x in card.css("p,span")]
        if any(t == "Gesuch" for t in texts):  # wanted ad
            continue
        # current price = first priced <p>; a struck-through old price ("Preis gesenkt") follows it
        price_txt = next((norm(p.text()) for p in card.css("p")
                          if "line-through" not in (p.attributes.get("class") or "")
                          and "€" in p.text() and re.search(r"\d", p.text())), None)
        pr = price_of(price_txt, "EUR") if price_txt else None
        if pr is None:  # "VB" only, "Zu verschenken", empty
            continue
        city = next((t for t in texts if PLZ_RE.match(t)), None)
        when = next((t for t in texts if TIME_RE.match(t)), None)
        img = card.css_first(CARD_IMG)
        href = card.attributes.get("data-href") or a.attributes.get("href") or ""
        ships = True if "Versand möglich" in texts else None
        is_pro = card.css_first('a[href^="/pro/"]') is not None
        l = ADAPTER.make(source_id=adid, url=abs_url(BASE, href), title=norm(a.text()), price=pr[0], currency=pr[1],
                         city=city, ships=ships, is_business=True if is_pro else None,
                         photos=[img.attributes["src"]] if img is not None and img.attributes.get("src") else [],
                         posted_at=_posted(when) if when else None)
        out.append(l)
    return out

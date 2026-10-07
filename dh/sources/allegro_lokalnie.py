"""Allegro Lokalnie (PL). HTML search + detail; robots.txt only blocks /oferty/wystaw/*."""
from __future__ import annotations

import html as htmllib
import json
import re
from decimal import Decimal
from urllib.parse import quote

from selectolax.lexbor import LexborHTMLParser

from ..models import Listing
from .base import Adapter, Query, abs_url, norm, price_of, seller_hash, years_to_days

BASE = "https://allegrolokalnie.pl"
# selectors (search page)
CARD = "article.mlc-itembox__container"
CARD_LINK = "a.mlc-itembox"
CARD_TITLE = ".mlc-itembox__title"
CARD_PRICE_INT = ".ml-offer-price__dollars"
CARD_PRICE_CENTS = ".ml-offer-price__cents"
CARD_PRICE_CCY = ".ml-offer-price__currency"
CARD_PARAM = ".mlc-itembox__params__param"
CARD_BADGE = ".ml-badges__badge"
CARD_CITY = ".mlc-itembox__location address"
CARD_IMG = ".mlc-itembox__image img"
CARD_TYPE = ".mlc-itembox__offer-type"
# selectors (detail page)
DESC = ".mlc-offer__description"
PARAM_ROW = "tr[data-mlc-param]"
GALLERY_IMG = ".mlc-offer__gallery img"
DELIVERY_ROW = ".mlc-delivery-options__row"
DELIVERY_SECTION = "[data-mlc-delivery-location], .mlc-delivery-location"
PRICE_META = "meta[itemprop=price]"
SELLER_ATTR = "data-mlc-seller-details"
OFFER_ID_RE = re.compile(r"/oferta/([^/?#]+)")


def _is_business(badge_text: str) -> bool | None:
    t = badge_text.lower()
    if "osoba prywatna" in t:
        return False
    if "firma" in t or "firmowy" in t:
        return True
    return None


def parse_search(html: str) -> list[Listing]:
    tree = LexborHTMLParser(html)
    out: list[Listing] = []
    ad = ADAPTER.make
    for card in tree.css(CARD):
        a = card.css_first(CARD_LINK)
        title_n = card.css_first(CARD_TITLE)
        if a is None or title_n is None:
            continue
        href = a.attributes.get("href") or ""
        m = OFFER_ID_RE.search(href)
        uid = m.group(1) if m else None  # URL slug: stable and present in the final URL of a live ad
        if not uid:
            continue
        pi = card.css_first(CARD_PRICE_INT)
        if pi is None:
            continue
        cents = card.css_first(CARD_PRICE_CENTS)
        ccy_n = card.css_first(CARD_PRICE_CCY)
        ptxt = pi.text(strip=True) + (("," + cents.text(strip=True).lstrip(",")) if cents else "") + " " + (
            ccy_n.text(strip=True) if ccy_n else "zł")
        pr = price_of(ptxt, "PLN")
        if pr is None:
            continue
        params = [norm(p.text()) for p in card.css(CARD_PARAM)]
        badge = " ".join(norm(b.text()) for b in card.css(CARD_BADGE))
        city_n = card.css_first(CARD_CITY)
        img = card.css_first(CARD_IMG)
        typ = card.css_first(CARD_TYPE)
        desc = "; ".join(params)
        l = ad(source="allegro_lokalnie", country="PL", currency=pr[1], source_id=uid,
               url=abs_url(BASE, href), title=norm(title_n.text()), description=desc, price=pr[0],
               city=norm(city_n.text()) if city_n else None, is_business=_is_business(badge),
               photos=[img.attributes["src"]] if img is not None and img.attributes.get("src") else [])
        # auctions ("Licytacja") show the current bid, not a price a buyer can pay
        if typ is not None and "licyt" in typ.text().lower():
            continue
        out.append(l)
    return out


class AllegroLokalnie(Adapter):
    name = "allegro_lokalnie"
    domain = "allegrolokalnie.pl"
    country = "PL"
    currency = "PLN"
    gone_markers = ("oferta jest już nieaktywna", "oferta nieaktualna", "ogłoszenie nieaktualne",
                    "oferta zakończona", "oferta została zakończona")

    id_in_final_url = True

    def page_urls(self, q: Query) -> list[str]:
        return [f"{BASE}/oferty/q/{quote(q.text, safe='')}?sort=startingTime-desc"]

    def parse_page(self, text: str) -> list[Listing]:
        return parse_search(text)

    def _parse_detail(self, listing: Listing, html: str) -> Listing:
        tree = LexborHTMLParser(html)
        parts = []
        d = tree.css_first(DESC)
        if d is not None:
            parts.append(norm(d.text(separator="\n")))
        for row in tree.css(PARAM_ROW):
            k = row.css_first(".mlc-params__parameter-name")
            v = row.css_first(".mlc-params__parameter-value")
            if k is not None and v is not None:
                parts.append(f"{norm(k.text())}: {norm(v.text())}")
        listing.description = "\n".join(p for p in parts if p) or listing.description
        photos = []
        for im in tree.css(GALLERY_IMG):
            src = im.attributes.get("src") or ""
            if src and "80x80" not in src:
                photos.append(re.sub(r"/s\d+x\d+/", "/original/", src))
        if photos:
            listing.photos = list(dict.fromkeys(photos))
        m = re.search(SELLER_ATTR + r'="([^"]*)"', html)
        if m:
            try:
                data = json.loads(htmllib.unescape(m.group(1)))
            except ValueError:
                data = {}
            seller = data.get("seller") or {}
            loc = data.get("location") or {}
            listing.seller_hash = seller_hash(self.name, seller.get("id")) or listing.seller_hash
            if seller.get("type"):
                listing.is_business = seller["type"] != "individual"
            am = re.search(r"od\s+(\d+)\s+(lat|rok|lata)", seller.get("ageString") or "")
            if am:
                listing.seller_age_days = years_to_days(int(am.group(1)))
            else:
                mm = re.search(r"od\s+(\d+)\s+miesi", seller.get("ageString") or "")
                if mm:
                    listing.seller_age_days = int(mm.group(1)) * 30
            if loc.get("city"):
                listing.city = loc["city"]
        rows = tree.css(DELIVERY_ROW)
        if rows:
            listing.ships, listing.pickup_only = True, False
        elif tree.css_first(DELIVERY_SECTION) is not None:  # delivery block parsed and lists no option
            listing.ships, listing.pickup_only = False, True
        return listing

    def detail_price(self, html: str) -> Decimal | None:
        n = LexborHTMLParser(html).css_first(PRICE_META)
        pr = price_of((n.attributes.get("content") if n is not None else "") or "", "PLN")
        return pr[0] if pr else None

    def gone_on_transport_failure(self, res) -> bool:
        # a removed offer 301-loops between /oferta/<slug> and /o/oferta/<slug>
        return "/o/oferta/" in res.url


ADAPTER = AllegroLokalnie()

"""taniekomputery.pl. Polish shop for refurbished, post-lease laptops, desktops and tablets.

A plain GET of the category page returns the product cards (checked 2026-11-12). The struck
`.price-old` amount is the previous price, not the offer. The "show 40/80/120" control is not a
link, so only the first screen is read and filtered locally against the query.
"""
from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from ..models import Listing
from .base import Adapter, Query, abs_url, kinds_for, norm, price_of, seller_hash

BASE = "https://taniekomputery.pl"
CARD = "div.pr-box"
NOTE = "condition: refurbished\nWarranty: shop warranty. Delivery in Poland."
SECTIONS = {
    "laptop": "/en/refurbished-laptops",
    "tablet": "/en/refurbished-tablets",
    "cpu": "/en/refurbished-computers",
    "monitor": "/en/refurbished-computers",
}


def sections_for(q: Query) -> list[str]:
    found = [SECTIONS[k] for k in kinds_for(q) if k in SECTIONS]
    return found or [SECTIONS["laptop"]]


def _current_price(node) -> tuple[Decimal, str] | None:
    """Offer price. `.price-old` is the previous amount and is removed before parsing."""
    if node is None:
        return None
    text = norm(node.text())
    old = node.css_first(".price-old")
    if old is not None:
        text = text.replace(norm(old.text()), " ", 1)
    return price_of(text, "PLN")


def _slug(href: str) -> str | None:
    path = urlsplit(href).path.strip("/")
    if not path or path.startswith("javascript"):
        return None
    return path


class Taniekomputery(Adapter):
    name = "taniekomputery"
    domain = "taniekomputery.pl"
    country = "PL"
    currency = "PLN"
    category_only = True
    id_in_final_url = True
    gone_markers = ("page not found", "nie znaleziono", "404 not found")

    def page_urls(self, q: Query) -> list[str]:
        return [BASE + path for path in sections_for(q)]

    def parse_page(self, text: str) -> list[Listing]:
        return parse_search(text)

    def _parse_detail(self, listing: Listing, html: str) -> Listing:
        tree = LexborHTMLParser(html)
        parts = [listing.description]
        h = tree.css_first("h1")
        if h is not None and norm(h.text()):
            listing.title = norm(h.text())
        spec = tree.css_first(".txt-1")
        if spec is not None:
            parts.append(norm(spec.text()))
        listing.description = "\n".join(p for p in parts if p)
        listing.seller_hash = seller_hash(self.name, "taniekomputery")
        photos = []
        for im in tree.css("img"):
            src = im.attributes.get("data-src") or im.attributes.get("src") or ""
            if "/img/towary/" in src:
                photos.append(abs_url(BASE, src))
        if photos:
            listing.photos = list(dict.fromkeys(photos))
        return listing

    def detail_price(self, html: str) -> Decimal | None:
        pr = _current_price(LexborHTMLParser(html).css_first(".price"))
        return pr[0] if pr else None


ADAPTER = Taniekomputery()


def parse_search(html: str) -> list[Listing]:
    tree = LexborHTMLParser(html)
    out: list[Listing] = []
    for card in tree.css(CARD):
        a = card.css_first("a[href]")
        if a is None:
            continue
        href = a.attributes.get("href") or ""
        slug = _slug(href)
        pr = _current_price(card.css_first(".price"))
        name = card.css_first(".name")
        title = norm(name.text()) if name is not None else ""
        if not slug or pr is None or not title:
            continue
        spec = card.css_first(".txt-1")
        desc = NOTE
        if spec is not None and norm(spec.text()):
            desc = f"{NOTE}\n{norm(spec.text())}"
        img = card.css_first("img")
        src = ""
        if img is not None:
            src = img.attributes.get("data-src") or ""
        out.append(ADAPTER.make(
            source_id=slug, url=abs_url(BASE, href), title=title, price=pr[0], currency="PLN",
            description=desc, ships=True, is_business=True,
            photos=[abs_url(BASE, src)] if src.startswith("/") or src.startswith("http") else []))
    return out

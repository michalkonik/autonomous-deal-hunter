"""gebrauchte-technik.de. German shop for refurbished computers. It ships to Poland.

The shipping page lists Poland and charges 9,99 EUR for EU delivery (checked 2026-11-13).
A plain GET of `/search?search=` returns the product cards. Only the first page is read.
`span.product-price` is the current price. `span.list-price` is the previous price and is
not the offer. The asterisk on the amount is the shop's shipping footnote, not a second price.
"""
from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import quote_plus, urlsplit

from selectolax.lexbor import LexborHTMLParser

from ..models import Listing
from .base import Adapter, Query, norm, price_of, seller_hash

BASE = "https://gebrauchte-technik.de"
NOTE = "condition: refurbished\nVersand nach Polen: 9,99 EUR."
_GONE_WORDS = ("ausverkauft", "nicht verfügbar", "nicht verfuegbar", "nicht lieferbar")
# Longer grades first, so "sehr-gut" is not read as "gut".
_GRADES = ("wie-neu", "sehr-gut", "neuwertig", "hervorragend", "akzeptabel", "gut", "neu")


class GebrauchteTechnik(Adapter):
    name = "gebrauchtetechnik"
    domain = "gebrauchte-technik.de"
    country = "DE"
    currency = "EUR"
    id_in_final_url = True
    gone_markers = ("seite nicht gefunden", "404 not found")

    def page_urls(self, q: Query) -> list[str]:
        return [f"{BASE}/search?search={quote_plus(q.text)}"]

    def parse_page(self, text: str) -> list[Listing]:
        return parse_search(text)

    def _parse_detail(self, listing: Listing, html: str) -> Listing:
        listing.seller_hash = seller_hash(self.name, "gebrauchtetechnik")
        return listing

    def detail_price(self, html: str) -> Decimal | None:
        tree = LexborHTMLParser(html)
        node = tree.css_first(".product-detail-price") or tree.css_first("span.product-price")
        parsed = price_of(node.text() if node is not None else "", "EUR")
        return parsed[0] if parsed else None


ADAPTER = GebrauchteTechnik()


def _grade(url: str) -> str:
    slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    for grade in _GRADES:
        if slug.endswith("-" + grade):
            return grade.replace("-", " ")
    return ""


def _slug(url: str) -> str:
    return urlsplit(url).path.strip("/")


def parse_search(html: str) -> list[Listing]:
    tree = LexborHTMLParser(html)
    out: list[Listing] = []
    for card in tree.css("div.product-box"):
        text = norm(card.text()).lower()
        if any(w in text for w in _GONE_WORDS):
            continue
        name = card.css_first("a.product-name")
        price_node = card.css_first("span.product-price")
        if name is None or price_node is None:
            continue
        url = (name.attributes.get("href") or "").split("#", 1)[0]
        sid = _slug(url)
        parsed = price_of(price_node.text(), "EUR")
        title = norm(name.text())
        # A bare "neu" in the title is classified as a new product. The grade stays in the description.
        if re.search(r"(?i)(?<![a-z])neu(?![a-z])", title) and not re.search(r"(?i)wie\s+neu", title):
            title = re.sub(r"(?i)(?<![a-z])neu(?![a-z])", " ", title)
            title = norm(title)
        if not sid or parsed is None or not title or not url.startswith("https://"):
            continue
        grade = _grade(url)
        img = card.css_first("img")
        src = (img.attributes.get("src") or "") if img is not None else ""
        out.append(ADAPTER.make(
            source_id=sid, url=url, title=title, price=parsed[0],
            description=NOTE + (f"\ngrade: {grade}" if grade else ""),
            ships=True, is_business=True,
            seller_hash=seller_hash(ADAPTER.name, "gebrauchtetechnik"),
            photos=[src] if src.startswith("https://") else []))
    return out

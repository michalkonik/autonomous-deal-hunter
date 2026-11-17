"""amso.pl. Kraków shop for post-lease and ex-display computers. Delivery in Poland.

A plain GET of `search.php?text=` returns the product cards (checked 2026-11-13). Only the first
page is read. `strong.price` is what the shop charges now. The struck "Cena regularna" and the
omnibus 30-day price are not the offer, and the points price is not złoty. Rows marked
unavailable ("Chwilowo niedostępny") are skipped.
"""
from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote_plus

from selectolax.lexbor import LexborHTMLParser

from ..models import Listing
from .base import Adapter, Query, abs_url, norm, price_of, seller_hash

BASE = "https://amso.pl"
NOTE = "condition: refurbished\nGwarancja sklepu. Dostawa w Polsce."


class Amso(Adapter):
    name = "amso"
    domain = "amso.pl"
    country = "PL"
    currency = "PLN"
    id_in_final_url = True
    gone_markers = ("nie znaleziono", "404 not found")

    def page_urls(self, q: Query) -> list[str]:
        return [f"{BASE}/search.php?text={quote_plus(q.text)}"]

    def parse_page(self, text: str) -> list[Listing]:
        return parse_search(text)

    def _parse_detail(self, listing: Listing, html: str) -> Listing:
        listing.seller_hash = seller_hash(self.name, "amso")
        return listing

    def empty_search(self, res) -> bool:
        return res.status == 404 and "noproduct" in (getattr(res, "url", "") or "").lower()

    def detail_price(self, html: str) -> Decimal | None:
        node = LexborHTMLParser(html).css_first("strong.price")
        parsed = price_of(node.text() if node is not None else "", "PLN")
        return parsed[0] if parsed else None


ADAPTER = Amso()


def _available(card) -> bool:
    if card.attributes.get("data-product_disable") == "true":
        return False
    label = card.css_first(".label.--disable")
    if label is not None and norm(label.text()):
        return False
    return True


def parse_search(html: str) -> list[Listing]:
    tree = LexborHTMLParser(html)
    out: list[Listing] = []
    for card in tree.css("div.product"):
        if not _available(card):
            continue
        name = card.css_first("a.product__name")
        price_node = card.css_first("strong.price")
        if name is None or price_node is None:
            continue
        href = name.attributes.get("href") or ""
        pid = str(card.attributes.get("data-product_id") or "").strip()
        parsed = price_of(price_node.text(), "PLN")
        title = norm(name.text())
        if not pid or parsed is None or not title or not href:
            continue
        img = card.css_first("img")
        src = abs_url(BASE, img.attributes.get("src") or "") if img is not None else ""
        out.append(ADAPTER.make(
            source_id=pid, url=abs_url(BASE, href), title=title, price=parsed[0],
            description=NOTE, ships=True, is_business=True,
            seller_hash=seller_hash(ADAPTER.name, "amso"),
            photos=[src] if src.startswith("https://") else []))
    return out

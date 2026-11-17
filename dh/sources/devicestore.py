"""device.store. Polish shop (Bydgoszcz) for ex-lease computers, priced in euro.

A plain GET of `/en/search.html?text=` returns the product cards (checked 2026-11-13). The
storefront's country control has Poland selected, Polish VAT is 23%, and the shop states
delivery in Poland. Only the first page is read. `strong.price` is the current gross price.
Empty skeleton cards and unavailable rows are skipped.
"""
from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote_plus, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from ..models import Listing
from .base import Adapter, Query, abs_url, norm, price_of, seller_hash

BASE = "https://device.store"
NOTE = "condition: refurbished\nWarranty: up to 2 years. Delivery in Poland."


class Devicestore(Adapter):
    name = "devicestore"
    domain = "device.store"
    country = "PL"
    currency = "EUR"
    id_in_final_url = True
    gone_markers = ("page not found", "nie znaleziono", "404 not found")

    def page_urls(self, q: Query) -> list[str]:
        return [f"{BASE}/en/search.html?text={quote_plus(q.text)}"]

    def parse_page(self, text: str) -> list[Listing]:
        return parse_search(text)

    def _parse_detail(self, listing: Listing, html: str) -> Listing:
        listing.seller_hash = seller_hash(self.name, "devicestore")
        return listing

    def empty_search(self, res) -> bool:
        return res.status == 404 and "noproduct" in (getattr(res, "url", "") or "").lower()

    def detail_price(self, html: str) -> Decimal | None:
        node = LexborHTMLParser(html).css_first("#projector_price_value")
        parsed = price_of(node.text() if node is not None else "", "EUR")
        return parsed[0] if parsed else None


ADAPTER = Devicestore()


def _clean(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _available(card) -> bool:
    if card.attributes.get("data-product_disable") == "true":
        return False
    label = card.css_first(".label.--disable")
    return not (label is not None and norm(label.text()))


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
        href = _clean(abs_url(BASE, name.attributes.get("href") or ""))
        pid = str(card.attributes.get("data-product_id") or "").strip()
        parsed = price_of(price_node.text(), "EUR")
        title = norm(name.text())
        if not pid or parsed is None or not title or not href.startswith("https://"):
            continue
        img = card.css_first("img")
        src = abs_url(BASE, (img.attributes.get("src") or "") if img is not None else "")
        out.append(ADAPTER.make(
            source_id=pid, url=href, title=title, price=parsed[0],
            description=NOTE, ships=True, is_business=True,
            seller_hash=seller_hash(ADAPTER.name, "devicestore"),
            photos=[src] if src.startswith("https://") else []))
    return out

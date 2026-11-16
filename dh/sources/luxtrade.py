"""luxtrade.pl. Polish shop for refurbished phones. Delivery in Poland.

A plain GET of `/szukaj?controller=search&s=` returns the product cards (checked 2026-11-13).
Only the first page is read. `span.price` is the current price. The link's `content` URL is the
specific grade (the hash names the condition). The shop banner says up to 24 months of warranty.
"""
from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import quote_plus, urlsplit

from selectolax.lexbor import LexborHTMLParser

from ..models import Listing
from .base import Adapter, Query, norm, price_of, seller_hash

BASE = "https://luxtrade.pl"
NOTE = "condition: refurbished\nGwarancja sklepu: do 24 miesięcy. Dostawa w Polsce."
_GRADE = re.compile(r"stan-([a-z0-9_]+)", re.I)
_GONE_WORDS = ("brak w magazynie", "niedostępny", "niedostepny")


class Luxtrade(Adapter):
    name = "luxtrade"
    domain = "luxtrade.pl"
    country = "PL"
    currency = "PLN"
    id_in_final_url = True
    gone_markers = ("nie znaleziono", "404 not found")

    def page_urls(self, q: Query) -> list[str]:
        return [f"{BASE}/szukaj?controller=search&s={quote_plus(q.text)}"]

    def parse_page(self, text: str) -> list[Listing]:
        return parse_search(text)

    def _parse_detail(self, listing: Listing, html: str) -> Listing:
        listing.seller_hash = seller_hash(self.name, "luxtrade")
        return listing

    def detail_price(self, html: str) -> Decimal | None:
        # The grade price is `.current-price-value`. Other `span.price` nodes on the page are
        # accessories and related products, so they are not the offer.
        tree = LexborHTMLParser(html)
        node = tree.css_first(".current-price .current-price-value")
        if node is None:
            return None
        parsed = price_of(node.text() or node.attributes.get("content") or "", "PLN")
        return parsed[0] if parsed else None


ADAPTER = Luxtrade()


def _identity(wrap, card) -> tuple[str, str]:
    content = (wrap.attributes.get("content") or "").split("#", 1)[0]
    href = (wrap.attributes.get("href") or "").split("#", 1)[0]
    pid = str(card.attributes.get("data-id-product") or "").strip()
    attr = str(card.attributes.get("data-id-product-attribute") or "").strip()
    if content and pid and attr and f"{pid}-{attr}" in urlsplit(content).path:
        return content, f"{pid}-{attr}"
    return href, pid


def _grade(wrap) -> str:
    raw = wrap.attributes.get("content") or ""
    m = _GRADE.search(raw)
    return m.group(1).replace("_", " ") if m else ""


def parse_search(html: str) -> list[Listing]:
    tree = LexborHTMLParser(html)
    out: list[Listing] = []
    for wrap in tree.css("a.product-wrapper"):
        card = wrap.css_first("article.product-miniature")
        if card is None:
            continue
        text = norm(card.text()).lower()
        if any(w in text for w in _GONE_WORDS):
            continue
        title_node = card.css_first("h3")
        price_node = card.css_first("span.price")
        if title_node is None or price_node is None:
            continue
        url, sid = _identity(wrap, card)
        parsed = price_of(price_node.text(), "PLN")
        title = norm(title_node.text())
        if not sid or parsed is None or not title or not url.startswith("https://"):
            continue
        grade = _grade(wrap)
        img = card.css_first("img")
        src = (img.attributes.get("src") or "") if img is not None else ""
        out.append(ADAPTER.make(
            source_id=sid, url=url, title=title, price=parsed[0],
            description=NOTE + (f"\ngrade: {grade}" if grade else ""),
            ships=True, is_business=True,
            seller_hash=seller_hash(ADAPTER.name, "luxtrade"),
            photos=[src] if src.startswith("https://") else []))
    return out

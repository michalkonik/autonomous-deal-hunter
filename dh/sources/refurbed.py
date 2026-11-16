"""refurbed.pl. Professional refurbished electronics with delivery to Poland included in the price.

robots.txt (`User-agent: *`) only disallows `/o/`, login, logout, signup and account. A plain GET of
`/search/?query=` is allowed and the first page is server-rendered product cards (checked 2026-11-12).
The "show more" control is `href="#"`, so only page 1 is read. The struck "(Nowa)" amount is the new-shop
comparison, and `product-price-discounted` is a voucher price: neither is the offer.
"""
from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import quote_plus

from selectolax.lexbor import LexborHTMLParser

from ..models import Listing
from .base import Adapter, Query, norm, price_of, seller_hash

BASE = "https://www.refurbed.pl"
CARD = "article[data-test='product-tile']"
NOTE = ("condition: refurbished\n"
        "Gwarancja sklepu: minimum 12 miesięcy. Dostawa do Polski wliczona w cenę.")
# "5 676,00 zł" — the offer. Does not match "32.0 GB" or a bare rating "5,0".
_PRICE = re.compile(r"\d{1,3}(?:[ \u00a0\u202f]\d{3})*,\d{2}\s*zł", re.I)
_NOWA = re.compile(r"\(\s*nowa\s*\)", re.I)
_BADGE = re.compile(r"\b(?:mała ilość|bestseller)\b", re.I)
_VARIANT = re.compile(r"\+\d+")
_PATH = re.compile(r"/p/([^?#]+)")


def _offer_path(href: str) -> str | None:
    """Path after `/p/`, including the instance id when the card links to one configuration."""
    m = _PATH.search(href or "")
    if not m:
        return None
    path = m.group(1).strip("/")
    return path or None


def _offer_title(text: str) -> str:
    """Card text without the offer price, the struck new price, stock badges, or "(Nowa)"."""
    t = _PRICE.sub(" ", text or "")
    t = _NOWA.sub(" ", t)
    t = _BADGE.sub(" ", t)
    t = _VARIANT.sub(" ", t)
    return norm(t)


def _photos(tree) -> list[str]:
    out = []
    for im in tree.css("img"):
        src = im.attributes.get("data-original") or im.attributes.get("src") or ""
        if src.startswith("https://files.refurbed.com/"):
            out.append(src)
    meta = tree.css_first("meta[property='og:image']")
    if meta is not None:
        src = meta.attributes.get("content") or ""
        if src.startswith("https://"):
            out.append(src)
    return list(dict.fromkeys(out))


class Refurbed(Adapter):
    name = "refurbed"
    domain = "www.refurbed.pl"
    country = "PL"
    currency = "PLN"
    gone_markers = ("nie znaleziono strony",)
    id_in_final_url = True

    def search_url(self, q: Query) -> str:
        return f"{BASE}/search/?query={quote_plus(q.text)}"

    def page_urls(self, q: Query) -> list[str]:
        return [self.search_url(q)]

    def parse_page(self, text: str) -> list[Listing]:
        return parse_search(text)

    def _parse_detail(self, listing: Listing, html: str) -> Listing:
        tree = LexborHTMLParser(html)
        parts = [listing.description]
        inst = tree.css_first("[data-test='instance-name']")
        if inst is not None:
            parts.append(norm(inst.text()))
        warranty = tree.css_first("[data-test='details-attribute-warranty']")
        if warranty is not None:
            parts.append(norm(warranty.text()))
        listing.description = "\n".join(p for p in parts if p)
        merchant = tree.css_first("[data-test='merchant-name']")
        if merchant is not None and norm(merchant.text()):
            listing.seller_hash = seller_hash(self.name, norm(merchant.text()))
        photos = _photos(tree)
        if photos:
            listing.photos = photos
        return listing

    def detail_price(self, html: str) -> Decimal | None:
        # Exact attribute: product-price-discounted is a voucher, not this price.
        node = LexborHTMLParser(html).css_first("[data-test='product-price']")
        pr = price_of(node.text() if node is not None else "", "PLN")
        return pr[0] if pr else None


ADAPTER = Refurbed()


def parse_search(html: str) -> list[Listing]:
    tree = LexborHTMLParser(html)
    out: list[Listing] = []
    for card in tree.css(CARD):
        a = card.css_first("a[href*='/p/']")
        price_node = card.css_first("[data-test='product-price']")
        if a is None or price_node is None:
            continue
        path = _offer_path(a.attributes.get("href") or "")
        pr = price_of(price_node.text(), "PLN")
        title = _offer_title(a.text())
        if not path or pr is None or not title:
            continue
        img = card.css_first("img")
        src = ""
        if img is not None:
            src = img.attributes.get("data-original") or img.attributes.get("src") or ""
        out.append(ADAPTER.make(
            source_id=path, url=f"{BASE}/p/{path}/", title=title,
            price=pr[0], currency="PLN", description=NOTE, ships=True, is_business=True,
            photos=[src] if src.startswith("https://") else []))
    return out

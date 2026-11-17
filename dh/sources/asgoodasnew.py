"""asgoodasnew.de. Refurbished electronics. The shop ships to Poland.

The shipping page (Lieferung & Versand, checked 2026-11-13) prices Poland at 10,90 EUR and names DHL
for Germany and Poland. Search is a plain GET of `index.php?cl=search`. One page, 32 articles.

Each article has condition grades. `data-variant` `price` is the price without a voucher code.
The amount inside `span.voucher` ("mit dem Code") is not the offer. Out-of-stock grades
("Jetzt vormerken") are skipped. Grade "neu" is still refurbished stock of the same device, so that
word is kept out of the title. It would otherwise be compared with new-shop prices.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from urllib.parse import quote_plus, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from ..models import Listing
from .base import Adapter, Query, norm, price_of, product_ld, seller_hash

BASE = "https://asgoodasnew.de"
NOTE = "condition: refurbished\nGarantie: 30 Monate. Versand nach Polen mit DHL."
_BARE_NEU = re.compile(r"(?i)(?<![a-z])neu(?![a-z])")
_WIE_NEU = re.compile(r"(?i)wie\s+neu")


def _money(raw) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        if raw <= 0:
            return None
        return Decimal(str(raw))
    parsed = price_of(str(raw), "EUR")
    return parsed[0] if parsed else None


def _clean(href: str) -> str:
    """Product path without the session id (`force_sid`)."""
    if not href:
        return ""
    if href.startswith("/"):
        href = BASE + href
    parts = urlsplit(href)
    if not parts.netloc or not parts.path or parts.path == "/":
        return ""
    return urlunsplit(("https", parts.netloc, parts.path, "", ""))


def _title(name: str, grade: str) -> str:
    """Product name plus the grade, without a bare "neu" (that grade is still refurbished)."""
    title = norm(name)
    g = norm(grade)
    if g and g.lower() != "neu":
        title = f"{title} - {g}"
    if title and not _WIE_NEU.search(title):
        title = norm(_BARE_NEU.sub(" ", title))
    return title


def _desc(grade: str) -> str:
    g = norm(grade)
    return NOTE + (f"\ngrade: {g}" if g else "")


def _voucher_shown(html: str) -> bool:
    low = html.lower()
    return "mit dem code" in low or "withvoucher" in low


class Asgoodasnew(Adapter):
    name = "asgoodasnew"
    domain = "asgoodasnew.de"
    country = "DE"
    currency = "EUR"
    gone_markers = ("seite nicht gefunden",)

    def page_urls(self, q: Query) -> list[str]:
        return [f"{BASE}/index.php?cl=search&searchparam={quote_plus(q.text)}&ldtype=grid&_artperpage=32"]

    def parse_page(self, text: str) -> list[Listing]:
        return parse_search(text)

    def _parse_detail(self, listing: Listing, html: str) -> Listing:
        listing.seller_hash = seller_hash(self.name, "asgoodasnew")
        photos = []
        for im in LexborHTMLParser(html).css("img"):
            src = im.attributes.get("src") or ""
            if src.startswith("https://imageservice.asgoodasnew.com/"):
                photos.append(src)
        if photos:
            listing.photos = list(dict.fromkeys(photos))[:4]
        return listing

    def detail_price(self, html: str) -> Decimal | None:
        """Price without the voucher code. Footnote ¹ on the page is that amount."""
        tree = LexborHTMLParser(html)
        if _voucher_shown(html):
            old = tree.css_first("[data-battery-price='old']")
            if old is not None:
                raw = old.attributes.get("data-base-price") or old.text()
                parsed = price_of(raw, "EUR")
                if parsed:
                    return parsed[0]
        offers = product_ld(html).get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict) and offers.get("price") not in (None, ""):
            parsed = price_of(str(offers["price"]), "EUR")
            if parsed:
                return parsed[0]
        node = tree.css_first("[itemprop='price']")
        if node is None:
            return None
        parsed = price_of(node.attributes.get("content") or node.text(), "EUR")
        return parsed[0] if parsed else None


ADAPTER = Asgoodasnew()


def _from_data(data: dict, href: str, photos: list[str]) -> Listing | None:
    vid = str(data.get("id") or "").strip()
    price = _money(data.get("price"))
    title = _title(str(data.get("name") or ""), str(data.get("variant") or ""))
    url = _clean(href)
    if not vid or price is None or not title or not url:
        return None
    return ADAPTER.make(
        source_id=vid, url=url, title=title, price=price,
        description=_desc(str(data.get("variant") or "")),
        ships=True, is_business=True,
        seller_hash=seller_hash(ADAPTER.name, "asgoodasnew"),
        photos=list(photos))


def _variant(node, photos: list[str]) -> Listing | None:
    bar = node.css_first(".listitem-variants-infobar")
    if bar is not None and "outofstock" in (bar.attributes.get("class") or ""):
        return None
    if node.css_first(".listitem-variants-badge--outofstock") is not None:
        return None
    raw = node.attributes.get("data-variant") or ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return _from_data(data, node.attributes.get("href") or "", photos)


def parse_search(html: str) -> list[Listing]:
    tree = LexborHTMLParser(html)
    out: list[Listing] = []
    for art in tree.css("article.listitem"):
        img = art.css_first("img")
        src = (img.attributes.get("src") or "") if img is not None else ""
        photos = [src] if src.startswith("https://") else []
        variants = art.css("a.listitem-variants-item")
        if variants:
            for node in variants:
                row = _variant(node, photos)
                if row is not None:
                    out.append(row)
            continue
        anchor = art.css_first("a[data-product]")
        if anchor is None:
            continue
        try:
            data = json.loads(anchor.attributes.get("data-product") or "")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            row = _from_data(data, anchor.attributes.get("href") or "", photos)
            if row is not None:
                out.append(row)
    return out

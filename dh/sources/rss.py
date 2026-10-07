"""Deal-community RSS adapters: Pepper.pl and hotukdeals ("hot" feeds).

Both sites run the same platform; the feeds carry a `pepper:merchant` element (name, price) and a title like
"212° - Product za 249,99zł @ Shop". The price is parsed from the title first, then from the merchant attribute.
Listings are retailer promotions, so `is_business=True`. The feeds pass robots.txt for plain, honestly
identified requests; they are fetched only through the polite Fetcher.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime

from ..models import Listing
from .base import Adapter, AdapterError, Query, filter_query, norm, price_of

NS = {"pepper": "http://www.pepper.com/rss"}
TEMP_PREFIX = re.compile(r"^\s*-?\d+\s*°\s*[-–]\s*")
# a price in a title: "za 249,99zł", "2 399 zł", "£7.99", "€ 19"
_PRICE_IN_TITLE = re.compile(
    r"(?:[£€$]\s?\d[\d  .,]*\d|[£€$]\s?\d|\d[\d  .,]*\d\s?(?:zł|zl|pln|€|eur|£|gbp)\b|\d\s?(?:zł|zl|pln|€|£)(?!\w))",
    re.I)
ID_RE = re.compile(r"(\d{4,})/?$")


_OLD = re.compile(r"(?:było|bylo|was|zamiast|instead of|normally|rrp|ze)\s*\(?\s*$", re.I)
_DEAL = re.compile(r"(?:\bza|\bdo|\bfor|\bonly|\bnow)\s*$", re.I)


def title_price(title: str, default_ccy: str):
    """The deal price in a title: the one after "za"/"do" (the last such, so "z X do Y" gives Y), never a
    "(było X)" / "was X" old price; otherwise the last remaining price."""
    ms = [m for m in _PRICE_IN_TITLE.finditer(title) if not _OLD.search(title[:m.start()][-14:])]
    if not ms:
        return None
    marked = [m for m in ms if _DEAL.search(title[:m.start()][-8:])]
    return price_of((marked or ms)[-1 if marked or len(ms) > 1 else 0].group(0), default_ccy)


def parse_feed(xml: str, adapter: "RssAdapter") -> list[Listing]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise AdapterError(f"{adapter.name}: feed is not valid XML ({e})") from e
    out: list[Listing] = []
    for it in root.iter("item"):
        raw_title = norm(it.findtext("title"))
        title = TEMP_PREFIX.sub("", raw_title)
        link = norm(it.findtext("link")) or norm(it.findtext("guid"))
        m = ID_RE.search(link)
        if not title or not m:
            continue
        merchant = it.find("pepper:merchant", NS)
        pr = title_price(title, adapter.currency)
        if pr is None and merchant is not None:
            pr = price_of(merchant.get("price"), adapter.currency)
        if pr is None:  # voucher, freebie ("Gratis"), price on request
            continue
        posted = None
        try:
            posted = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc)
        except (TypeError, ValueError):
            pass
        img = it.find("{http://search.yahoo.com/mrss/}content")
        shop = merchant.get("name") if merchant is not None else None
        cat = norm(it.findtext("category"))
        out.append(adapter.make(
            source_id=m.group(1), url=link, title=title, price=pr[0], currency=pr[1], is_business=True,
            description=norm(f"{cat}; shop: {shop}" if shop else cat), posted_at=posted,
            photos=[img.get("url")] if img is not None and img.get("url") else []))
    return out


class RssAdapter(Adapter):
    def __init__(self, name: str, url: str, country: str, currency: str):
        self.name, self.url, self.country, self.currency = name, url, country, currency
        self.domain = re.sub(r"^https?://([^/]+)/.*$", r"\1", url)

    async def search(self, query: Query, fetcher, to_pln=None) -> list[Listing]:
        res = await self._get(fetcher, self.url)
        if res.status != 200:
            raise AdapterError(f"{self.name}: feed status {res.status}")
        return filter_query(parse_feed(res.text, self), query, to_pln or self.to_pln, text=True)

    def parse_detail(self, listing: Listing, html: str) -> Listing:
        return listing  # the feed entry is all there is; the deal page is not fetched

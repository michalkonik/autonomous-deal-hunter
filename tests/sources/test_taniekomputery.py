"""taniekomputery.pl: the offer is the current price, not the struck previous price."""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal

from dh.match import rules_extract
from dh.sources import Query
from dh.sources import taniekomputery as tk

from helpers import FakeFetcher, Res, check_listings, fixture_text

run = asyncio.run


def test_taniekomputery_uses_the_current_price():
    ls = tk.parse_search(fixture_text("taniekomputery", "search.html"))
    check_listings(ls, "PLN", n=2)
    assert ls[0].price == Decimal("1494.40")
    assert ls[1].price == Decimal("974.50")
    assert ls[0].url == "https://taniekomputery.pl/en/refurbished-laptops-dell-latitude-5510-i7"
    assert "16 GB DDR4" in ls[0].description
    assert ls[0].ships is True and ls[0].is_business is True
    assert rules_extract(ls[0].title, ls[0].description, "en").condition == "used"
    assert "2544" not in str(ls[0].price)


def test_taniekomputery_category_pages_and_detail_price():
    ad = tk.ADAPTER
    assert ad.page_urls(Query("macbook pro")) == ["https://taniekomputery.pl/en/refurbished-laptops"]
    assert ad.page_urls(Query("rtx 4090")) == ["https://taniekomputery.pl/en/refurbished-laptops"]
    html = fixture_text("taniekomputery", "detail.html")
    listing = tk.parse_search(fixture_text("taniekomputery", "search.html"))[0]
    detailed = ad.parse_detail(listing, html)
    assert "512 GB SSD" in detailed.description
    assert detailed.seller_hash and re.fullmatch(r"[0-9a-f]{64}", detailed.seller_hash)
    assert "taniekomputery" not in detailed.description
    assert detailed.photos[0].startswith("https://taniekomputery.pl/img/towary/")
    assert ad.detail_price(html) == Decimal("1494.40")
    ok, price = run(ad.alive(listing, FakeFetcher(default=Res(200, html, listing.url))))
    assert ok and price == Decimal("1494.40")
    f = FakeFetcher(default=Res(200, fixture_text("taniekomputery", "search.html")))
    assert len(run(ad.search(Query("latitude"), f))) == 1
    assert f.urls == ["https://taniekomputery.pl/en/refurbished-laptops"]

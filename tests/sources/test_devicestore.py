"""device.store: the offer is strong.price, not a struck amount, and the query string is dropped."""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal

from dh.match import has_papers, rules_extract
from dh.sources import Query
from dh.sources import devicestore as ds

from helpers import FakeFetcher, Res, check_listings, fixture_text

run = asyncio.run
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_devicestore_uses_the_gross_price_and_skips_unavailable():
    ls = ds.parse_search(fixture_text("devicestore", "search.html"))
    check_listings(ls, "EUR", n=1)
    assert len(ls) == 1
    assert ls[0].source_id == "11638"
    assert ls[0].price == Decimal("215.59")
    assert ls[0].url == "https://device.store/en/products/hp-elitebook-735-g6-11638.html"
    assert "query_id" not in ls[0].url
    assert ls[0].ships is True and ls[0].is_business is True and ls[0].country == "PL"
    assert ls[0].photos == ["https://device.store/hpeciai/elitebook.jpg"]
    assert HEX64.fullmatch(ls[0].seller_hash)
    assert "devicestore" not in ls[0].description
    ex = rules_extract(ls[0].title, ls[0].description, "pl")
    assert ex.condition == "used"
    assert has_papers(ls[0].title, ls[0].description)


def test_devicestore_no_product_page_is_an_empty_search():
    f = FakeFetcher(default=Res(404, "none", "https://device.store/en/noproduct.html?reason=product&text=rtx"))
    assert run(ds.ADAPTER.search(Query("rtx 4060 ti"), f)) == []


def test_devicestore_search_url_and_detail_price():
    ad = ds.ADAPTER
    assert ad.page_urls(Query("elitebook")) == ["https://device.store/en/search.html?text=elitebook"]
    html = fixture_text("devicestore", "detail.html")
    listing = ds.parse_search(fixture_text("devicestore", "search.html"))[0]
    assert ad.detail_price(html) == Decimal("215.59")
    ok, price = run(ad.alive(listing, FakeFetcher(default=Res(200, html, listing.url))))
    assert ok and price == Decimal("215.59")
    assert run(ad.alive(listing, FakeFetcher(default=Res(404)))) == (False, None)

"""luxtrade.pl: the offer is span.price for one grade, not a struck regular price."""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal

from dh.match import has_papers, rules_extract
from dh.sources import Query
from dh.sources import luxtrade as lx

from helpers import FakeFetcher, Res, check_listings, fixture_text

run = asyncio.run
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_luxtrade_uses_the_grade_price_and_skips_unavailable():
    ls = lx.parse_search(fixture_text("luxtrade", "search.html"))
    check_listings(ls, "PLN", n=1)
    assert len(ls) == 1
    assert ls[0].source_id == "36-3293"
    assert ls[0].price == Decimal("3599.00")
    assert "4299" not in str(ls[0].price)
    assert ls[0].url == "https://luxtrade.pl/iphone-16-pro-max/36-3293-apple-iphone-16-pro-max-bialy.html"
    assert "iPhone 16 Pro Max" in ls[0].title
    assert "grade: bardzo dobry" in ls[0].description
    assert ls[0].ships is True and ls[0].is_business is True and ls[0].country == "PL"
    assert ls[0].photos[0].startswith("https://luxtrade.pl/")
    assert HEX64.fullmatch(ls[0].seller_hash)
    assert "luxtrade" not in ls[0].description
    ex = rules_extract(ls[0].title, ls[0].description, "pl")
    assert ex.condition == "used"
    assert has_papers(ls[0].title, ls[0].description)


def test_luxtrade_search_url_and_detail_price():
    ad = lx.ADAPTER
    assert ad.page_urls(Query("iphone 16")) == ["https://luxtrade.pl/szukaj?controller=search&s=iphone+16"]
    html = fixture_text("luxtrade", "detail.html")
    listing = lx.parse_search(fixture_text("luxtrade", "search.html"))[0]
    assert ad.detail_price(html) == Decimal("3599.00")
    ok, price = run(ad.alive(listing, FakeFetcher(default=Res(200, html, listing.url))))
    assert ok and price == Decimal("3599.00")
    assert run(ad.alive(listing, FakeFetcher(default=Res(404)))) == (False, None)

"""amso.pl: the offer is strong.price, not the struck regular price or the points price."""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal

from dh.match import has_papers, rules_extract
from dh.sources import Query
from dh.sources import amso

from helpers import FakeFetcher, Res, check_listings, fixture_text

run = asyncio.run
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_amso_uses_the_current_price_and_skips_unavailable():
    ls = amso.parse_search(fixture_text("amso", "search.html"))
    check_listings(ls, "PLN", n=1)
    assert len(ls) == 1
    assert ls[0].source_id == "291915"
    assert ls[0].price == Decimal("1133.10")
    assert "1259" not in str(ls[0].price) and "19262" not in str(ls[0].price)
    assert ls[0].url == "https://amso.pl/product-pol-291915-Lenovo-ThinkBook.html"
    assert "ThinkBook" in ls[0].title
    assert ls[0].ships is True and ls[0].is_business is True and ls[0].country == "PL"
    assert ls[0].photos == ["https://amso.pl/hpeciai/pol_il_thinkbook-291915.webp"]
    assert HEX64.fullmatch(ls[0].seller_hash)
    assert "amso" not in ls[0].description
    ex = rules_extract(ls[0].title, ls[0].description, "pl")
    assert ex.condition == "used"
    assert has_papers(ls[0].title, ls[0].description)


def test_amso_no_product_page_is_an_empty_search():
    f = FakeFetcher(default=Res(404, "none", "https://amso.pl/noproduct.php?reason=product&text=rtx"))
    assert run(amso.ADAPTER.search(Query("rtx 3080 ti"), f)) == []


def test_amso_search_url_and_detail_price():
    ad = amso.ADAPTER
    assert ad.page_urls(Query("elitebook")) == ["https://amso.pl/search.php?text=elitebook"]
    html = fixture_text("amso", "detail.html")
    listing = amso.parse_search(fixture_text("amso", "search.html"))[0]
    assert ad.detail_price(html) == Decimal("1133.10")
    detailed = ad.parse_detail(listing, html)
    assert HEX64.fullmatch(detailed.seller_hash)
    ok, price = run(ad.alive(listing, FakeFetcher(default=Res(200, html, listing.url))))
    assert ok and price == Decimal("1133.10")
    assert run(ad.alive(listing, FakeFetcher(default=Res(404)))) == (False, None)
    gone = run(ad.alive(listing, FakeFetcher(default=Res(200, "Nie znaleziono strony", listing.url))))
    assert gone == (False, None)
    f = FakeFetcher(default=Res(200, fixture_text("amso", "search.html")))
    assert len(run(ad.search(Query("elitebook"), f))) == 1
    assert f.urls == ["https://amso.pl/search.php?text=elitebook"]

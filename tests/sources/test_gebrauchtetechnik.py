"""gebrauchte-technik.de: the offer is span.product-price, not the list price."""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal

from dh.match import has_papers, rules_extract
from dh.sources import Query
from dh.sources import gebrauchtetechnik as gt

from helpers import FakeFetcher, Res, check_listings, fixture_text

run = asyncio.run
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_gebrauchtetechnik_uses_the_current_price_and_skips_sold_out():
    ls = gt.parse_search(fixture_text("gebrauchtetechnik", "search.html"))
    check_listings(ls, "EUR", n=1)
    assert len(ls) == 1
    assert ls[0].source_id == "hp-elitebook-850-g8-sehr-gut"
    assert ls[0].price == Decimal("469.00")
    assert "549" not in str(ls[0].price)
    assert ls[0].url == "https://gebrauchte-technik.de/hp-elitebook-850-g8-sehr-gut"
    assert " neu" not in ls[0].title.lower() and not ls[0].title.lower().startswith("neu")
    assert "grade: sehr gut" in ls[0].description
    assert ls[0].ships is True and ls[0].is_business is True and ls[0].country == "DE"
    assert ls[0].photos == ["https://gebrauchte-technik.de/media/elitebook.jpg"]
    assert HEX64.fullmatch(ls[0].seller_hash)
    ex = rules_extract(ls[0].title, ls[0].description, "de")
    assert ex.condition == "used"
    assert has_papers(ls[0].title, ls[0].description) is False


def test_gebrauchtetechnik_search_url_and_detail_price():
    ad = gt.ADAPTER
    assert ad.page_urls(Query("elitebook")) == ["https://gebrauchte-technik.de/search?search=elitebook"]
    html = fixture_text("gebrauchtetechnik", "detail.html")
    listing = gt.parse_search(fixture_text("gebrauchtetechnik", "search.html"))[0]
    assert ad.detail_price(html) == Decimal("469.00")
    ok, price = run(ad.alive(listing, FakeFetcher(default=Res(200, html, listing.url))))
    assert ok and price == Decimal("469.00")
    assert run(ad.alive(listing, FakeFetcher(default=Res(404)))) == (False, None)

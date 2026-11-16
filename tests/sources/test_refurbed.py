"""refurbed.pl: server-rendered search page, offer price only (not the struck new price)."""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal

from dh.match import has_papers, rules_extract
from dh.sources import Query
from dh.sources import refurbed as rf

from helpers import FakeFetcher, Res, check_listings, fixture_text

run = asyncio.run
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_refurbed_search_parses_offer_price_not_the_new_comparison():
    ls = rf.parse_search(fixture_text("refurbed", "search.html"))
    check_listings(ls, "PLN", n=3)
    assert [l.source_id for l in ls] == [
        "dell-xps-15-9520/176552b", "lenovo-thinkpad-x1-extreme-g4", "hp-probook-440-g10"]
    assert ls[0].price == Decimal("6252.40")
    assert ls[0].url == "https://www.refurbed.pl/p/dell-xps-15-9520/176552b/"
    assert "RTX 3050" in ls[0].title and "32.0 GB" in ls[0].title
    assert "Mała" not in ls[0].title and "+2" not in ls[0].title
    assert "(Nowa)" not in ls[0].title and not re.search(r"(?<![a-z])nowa(?![a-z])", ls[0].title.lower())
    assert "12000" not in str(ls[0].price)
    assert ls[0].ships is True and ls[0].is_business is True and ls[0].country == "PL"
    assert ls[0].photos == ["https://files.refurbed.com/pi/dell-xps-15-9520.jpg"]
    ex = rules_extract(ls[0].title, ls[0].description, "pl")
    assert ex.condition == "used"
    assert has_papers(ls[0].title, ls[0].description)


def test_refurbed_search_url_is_the_allowed_first_page():
    ad = rf.ADAPTER
    url = ad.search_url(Query("rtx 4090"))
    assert url == "https://www.refurbed.pl/search/?query=rtx+4090"
    assert "/o/" not in url and "api.refurbed" not in url
    f = FakeFetcher(default=Res(200, fixture_text("refurbed", "search.html")))
    assert len(run(ad.search(Query("rtx 4090"), f))) == 3
    assert f.urls == [url]


def test_refurbed_detail_price_ignores_the_voucher_and_hashes_the_merchant():
    ad = rf.ADAPTER
    html = fixture_text("refurbed", "detail.html")
    listing = rf.parse_search(fixture_text("refurbed", "search.html"))[0]
    detailed = ad.parse_detail(listing, html)
    assert ad.detail_price(html) == Decimal("5676.00")
    assert "RTX 4000" in detailed.description and "Gwarancja 12 miesięcy" in detailed.description
    assert "Laptop Markt" not in detailed.description and "Laptop Markt" not in detailed.title
    assert HEX64.fullmatch(detailed.seller_hash)
    assert detailed.photos[0].startswith("https://files.refurbed.com/")
    ok, price = run(ad.alive(listing, FakeFetcher(default=Res(200, html, listing.url))))
    assert ok and price == Decimal("5676.00")
    assert run(ad.alive(listing, FakeFetcher(default=Res(404)))) == (False, None)
    gone = run(ad.alive(listing, FakeFetcher(default=Res(200, "Nie znaleziono strony.", listing.url))))
    assert gone == (False, None)
    # "Prawie wyprzedane" is still for sale.
    assert run(ad.alive(listing, FakeFetcher(default=Res(200, html, listing.url))))[0] is True

"""asgoodasnew.de: the offer is the grade price without the voucher code."""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal

from dh.match import has_papers, rules_extract
from dh.sources import Query
from dh.sources import asgoodasnew as ag

from helpers import FakeFetcher, Res, check_listings, fixture_text

run = asyncio.run
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_asgoodasnew_uses_the_grade_price_not_the_voucher():
    ls = ag.parse_search(fixture_text("asgoodasnew", "search.html"))
    check_listings(ls, "EUR", n=3)
    by_id = {l.source_id: l for l in ls}
    assert set(by_id) == {"AN2", "AN3", "AN4"}
    assert by_id["AN2"].price == Decimal("1385")
    assert "1235" not in str(by_id["AN2"].price)
    assert by_id["AN2"].url == "https://asgoodasnew.de/Apple-MacBooks/macbook-pro-sehr-gut.html"
    assert "sehr gut" in by_id["AN2"].title
    assert not re.search(r"(?<![a-z])neu(?![a-z])", by_id["AN2"].title.lower())
    assert by_id["AN3"].price == Decimal("1505")
    assert "wie neu" in by_id["AN3"].title.lower()
    assert by_id["AN4"].price == Decimal("499")
    assert not re.search(r"(?<![a-z])neu(?![a-z])", by_id["AN4"].title.lower())
    assert by_id["AN2"].ships is True and by_id["AN2"].is_business is True
    assert by_id["AN2"].country == "DE" and by_id["AN2"].currency == "EUR"
    assert by_id["AN2"].photos == ["https://imageservice.asgoodasnew.com/290/1/title.jpg"]
    assert HEX64.fullmatch(by_id["AN2"].seller_hash)
    assert "asgoodasnew" not in by_id["AN2"].description
    used = rules_extract(by_id["AN2"].title, by_id["AN2"].description, "de")
    assert used.condition == "used"
    like = rules_extract(by_id["AN3"].title, by_id["AN3"].description, "de")
    assert like.condition == "like_new"
    top = rules_extract(by_id["AN4"].title, by_id["AN4"].description, "de")
    assert top.condition == "used"
    assert has_papers(by_id["AN2"].title, by_id["AN2"].description)


def test_asgoodasnew_search_is_one_page_without_a_session():
    ad = ag.ADAPTER
    url = ad.page_urls(Query("macbook pro"))[0]
    assert url == ("https://asgoodasnew.de/index.php?cl=search&searchparam=macbook+pro"
                   "&ldtype=grid&_artperpage=32")
    assert "force_sid" not in url
    f = FakeFetcher(default=Res(200, fixture_text("asgoodasnew", "search.html")))
    assert len(run(ad.search(Query("macbook pro"), f))) == 3
    assert f.urls == [url]


def test_asgoodasnew_detail_price_ignores_the_voucher_code():
    ad = ag.ADAPTER
    html = fixture_text("asgoodasnew", "detail.html")
    listing = ag.parse_search(fixture_text("asgoodasnew", "search.html"))[0]
    assert ad.detail_price(html) == Decimal("1385.00")
    detailed = ad.parse_detail(listing, html)
    assert detailed.photos[0].startswith("https://imageservice.asgoodasnew.com/")
    assert HEX64.fullmatch(detailed.seller_hash)
    ok, price = run(ad.alive(listing, FakeFetcher(default=Res(200, html, listing.url))))
    assert ok and price == Decimal("1385.00")
    plain = ('<html><script type="application/ld+json">'
             '{"@type":"Product","offers":{"@type":"Offer","price":499,"priceCurrency":"EUR"}}'
             '</script><span itemprop="price">499</span></html>')
    assert ad.detail_price(plain) == Decimal("499")
    assert run(ad.alive(listing, FakeFetcher(default=Res(404)))) == (False, None)
    gone = run(ad.alive(listing, FakeFetcher(default=Res(200, "Seite nicht gefunden", listing.url))))
    assert gone == (False, None)

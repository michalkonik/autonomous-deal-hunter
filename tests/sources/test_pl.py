"""Polish adapters: allegro_lokalnie, sprzedajemy. Fixtures captured live on the rig (see tests/fixtures/capture.py)."""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal

import pytest

from dh.sources import AdapterError, Query
from dh.sources.base import filter_query
from dh.sources import allegro_lokalnie as al
from dh.sources import sprzedajemy as sp

from dh.sources.base import seller_hash as base_hash
from helpers import FakeFetcher, Res, check_listings, fixture_text, sub_first

run = asyncio.run


# ---------------------------------------------------------------- allegro lokalnie
def test_allegro_lokalnie_search_parses_fixture():
    ls = al.parse_search(fixture_text("allegro_lokalnie", "search.html"))
    check_listings(ls, "PLN")
    assert all(l.url.startswith("https://allegrolokalnie.pl/oferta/") for l in ls)
    assert any(l.is_business is False for l in ls)  # "Sprzedający: osoba prywatna" badge


def test_allegro_lokalnie_price_parsing_edge_cases():
    html = fixture_text("allegro_lokalnie", "search.html")
    base_n = len(al.parse_search(html))
    first_id = al.parse_search(html)[0].source_id
    zero = sub_first(html, r'(class="ml-offer-price__dollars">)[^<]*', r"\g<1>0")
    assert first_id not in {l.source_id for l in al.parse_search(zero)}
    assert len(al.parse_search(zero)) == base_n - 1
    nego = sub_first(html, r'<span class="ml-offer-price__dollars">[^<]*</span>', "do negocjacji")
    assert len(al.parse_search(nego)) == base_n - 1


def test_allegro_lokalnie_skips_auctions():
    html = fixture_text("allegro_lokalnie", "search.html")
    n = len(al.parse_search(html))
    auction = sub_first(html, r'(class="mlc-itembox__offer-type[^"]*">)[^<]*', r"\g<1>Licytacja ")
    assert len(al.parse_search(auction)) == n - 1


def test_allegro_lokalnie_detail_fixture():
    ad = al.ADAPTER
    l = al.parse_search(fixture_text("allegro_lokalnie", "search.html"))[0]
    d = ad.parse_detail(l, fixture_text("allegro_lokalnie", "detail.html"))
    assert len(d.description) > 50
    assert re.fullmatch(r"[0-9a-f]{64}", d.seller_hash)
    assert d.seller_age_days and d.seller_age_days >= 365
    assert d.ships is True and d.pickup_only is False
    assert d.photos and all(p.startswith("https://") for p in d.photos)
    assert ad.detail_price(fixture_text("allegro_lokalnie", "detail.html")) > 0


def test_allegro_lokalnie_search_url_and_robots():
    ad = al.ADAPTER
    f = FakeFetcher({"allegrolokalnie.pl": Res(200, fixture_text("allegro_lokalnie", "search.html"))})
    ls = run(ad.search(Query("rtx 4090"), f))
    assert f.urls == ["https://allegrolokalnie.pl/oferty/q/rtx%204090?sort=startingTime-desc"]
    assert len(ls) >= 10
    with pytest.raises(AdapterError, match="robots"):
        run(ad.search(Query("x"), FakeFetcher(default=Res(0, "", "", disallowed=True))))
    with pytest.raises(AdapterError, match="blocked"):
        run(ad.search(Query("x"), FakeFetcher(default=Res(403, "", "", blocked=True))))


def test_allegro_lokalnie_pln_price_bounds():
    html = fixture_text("allegro_lokalnie", "search.html")
    prices = sorted(l.price for l in al.parse_search(html))
    lo, hi = prices[len(prices) // 4], prices[3 * len(prices) // 4]
    ls = filter_query(al.parse_search(html), Query("x", min_price_pln=lo, max_price_pln=hi))
    assert ls and len(ls) < len(prices) and all(lo <= l.price <= hi for l in ls)


def test_allegro_lokalnie_alive():
    ad = al.ADAPTER
    l = al.parse_search(fixture_text("allegro_lokalnie", "search.html"))[0]
    detail = fixture_text("allegro_lokalnie", "detail.html")
    ok, price = run(ad.alive(l, FakeFetcher(default=Res(200, detail))))
    assert ok and price > 0
    assert run(ad.alive(l, FakeFetcher(default=Res(404)))) == (False, None)
    assert run(ad.alive(l, FakeFetcher(default=Res(410)))) == (False, None)
    # a removed offer 301-loops between /oferta/ and /o/oferta/: the Fetcher gives status 0 at the loop
    loop = Res(0, "", "https://allegrolokalnie.pl/o/oferta/x")
    assert run(ad.alive(l, FakeFetcher(default=loop))) == (False, None)
    gone = Res(200, "<html>Oferta jest już nieaktywna</html>")
    assert run(ad.alive(l, FakeFetcher(default=gone))) == (False, None)
    with pytest.raises(AdapterError):  # plain network failure is not "gone"
        run(ad.alive(l, FakeFetcher(default=Res(0, "", "https://allegrolokalnie.pl/oferta/x"))))


# ---------------------------------------------------------------- sprzedajemy
def test_sprzedajemy_search_parses_fixture():
    ls = sp.parse_search(fixture_text("sprzedajemy", "search.html"))
    check_listings(ls, "PLN")
    assert all(re.fullmatch(r"\d+", l.source_id) for l in ls)
    assert any(l.posted_at for l in ls) and any(l.city for l in ls)


def test_sprzedajemy_price_parsing_edge_cases():
    html = fixture_text("sprzedajemy", "search.html")
    n = len(sp.parse_search(html))
    for bad in ("do negocjacji", "0 zł", "Zamienię", ""):
        mutated = sub_first(html, r'(<span class="price[^"]*">)[^<]*', rf"\g<1>{bad}")
        assert len(sp.parse_search(mutated)) == n - 1, bad


def test_sprzedajemy_local_query_filter():
    html = fixture_text("sprzedajemy", "search.html")
    allq = sp.parse_search(html)
    word = re.findall(r"[A-Za-z]{4,}", allq[0].title)[0].lower()
    some = filter_query(allq, Query(word), text=True)
    assert some and len(some) <= len(allq) and all(word in l.title.lower() for l in some)
    assert filter_query(allq, Query("zzzz-no-such-product-qq"), text=True) == []
    assert sp.ADAPTER.category_only


def test_sprzedajemy_detail_fixture():
    ad = sp.ADAPTER
    html = fixture_text("sprzedajemy", "detail.html")
    l = sp.parse_search(fixture_text("sprzedajemy", "search.html"))[0]
    d = ad.parse_detail(l, html)
    assert d.description and d.photos
    assert re.fullmatch(r"[0-9a-f]{64}", d.seller_hash)
    assert d.is_business is not None
    assert ad.detail_price(html) > 0


def test_sprzedajemy_never_uses_search_form():
    """robots.txt: `Disallow: *inp_*` covers every search and sort URL, so only category pages are fetched."""
    ad = sp.ADAPTER
    f = FakeFetcher(default=Res(200, fixture_text("sprzedajemy", "search.html")))
    run(ad.search(Query("rtx 4090"), f))
    assert f.urls and not any("inp_" in u or "szukaj" in u for u in f.urls)
    assert f.urls[0].endswith("/podzespoly-bazowe/karty-graficzne") and f.urls[1].endswith("?offset=30")


def test_sprzedajemy_alive():
    ad = sp.ADAPTER
    l = sp.parse_search(fixture_text("sprzedajemy", "search.html"))[0]
    detail = fixture_text("sprzedajemy", "detail.html")
    ok, price = run(ad.alive(l, FakeFetcher(default=Res(200, detail, l.url))))
    assert ok and price > 0
    assert run(ad.alive(l, FakeFetcher(default=Res(404)))) == (False, None)
    # expired ads redirect to a category/search page: the id is gone from the final URL
    redirected = Res(200, "<html>lista</html>", "https://sprzedajemy.pl/elektronika")
    assert run(ad.alive(l, FakeFetcher(default=redirected))) == (False, None)
    assert run(ad.alive(l, FakeFetcher(default=Res(200, "<html>Ogłoszenie nieaktualne</html>", l.url)))) == (False, None)


def test_allegro_lokalnie_source_id_is_url_slug_and_alive_checks_it():
    ad = al.ADAPTER
    l = al.parse_search(fixture_text("allegro_lokalnie", "search.html"))[0]
    assert l.url.endswith("/oferta/" + l.source_id)
    moved = Res(200, fixture_text("allegro_lokalnie", "detail.html"), "https://allegrolokalnie.pl/oferty/q/x")
    assert run(ad.alive(l, FakeFetcher(default=moved))) == (False, None)


def test_allegro_lokalnie_pickup_only_only_when_delivery_block_was_parsed():
    ad = al.ADAPTER
    base = al.parse_search(fixture_text("allegro_lokalnie", "search.html"))
    l = base[0]
    none = ad.parse_detail(base[1], "<html><body><div class='mlc-offer__description'>x</div></body></html>")
    assert none.ships is None and none.pickup_only is False  # page not understood: no claim
    pick = ad.parse_detail(base[2], "<html><body><section data-mlc-delivery-location>Dostawa</section></body></html>")
    assert pick.ships is False and pick.pickup_only is True
    ship = ad.parse_detail(l, fixture_text("allegro_lokalnie", "detail.html"))
    assert ship.ships is True and ship.pickup_only is False


def test_sprzedajemy_seller_is_stable_id_never_display_name():
    ad = sp.ADAPTER
    html = fixture_text("sprzedajemy", "detail.html")
    l = sp.parse_search(fixture_text("sprzedajemy", "search.html"))[0]
    d = ad.parse_detail(l, html)
    assert d.seller_hash == base_hash("sprzedajemy", re.search(r"/oferty-uzytkownika-(\d+)", html).group(1))
    no_id = re.sub(r"/oferty-uzytkownika-\d+", "/x", html)
    l2 = sp.parse_search(fixture_text("sprzedajemy", "search.html"))[1]
    assert ad.parse_detail(l2, no_id).seller_hash is None


def test_sprzedajemy_detail_price_only_from_main_ad_block():
    ad = sp.ADAPTER
    assert ad.detail_price('<html><body><span class="price">9 999 zł</span></body></html>') is None
    box = '<div class="offer-price-box"><strong class="price"><span> 4 000 zł </span></strong></div>'
    assert ad.detail_price(f'<html><body><span class="price">9 999 zł</span>{box}</body></html>') == 4000


def test_search_403_is_one_request_and_adapter_error():
    """Block handling (backoff, disable) lives in the Fetcher; adapters neither retry nor probe other URLs."""
    for mod, q in ((al, "rtx"), (sp, "rtx")):
        f = FakeFetcher(default=Res(403, "", "", blocked=True))
        with pytest.raises(AdapterError, match="blocked"):
            run(mod.ADAPTER.search(Query(q), f))
        assert len(f.urls) == 1

"""Task 10: eBay Browse API adapter (fixture is synthetic: no keys yet), Pepper/hotukdeals RSS, retail price watch."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from dh.models import CompRow
from dh.sources import AdapterError, Query, registry
from dh.sources import ebay, retail, rss
from dh.sources.base import now_utc
from dh.store import Store

from helpers import FakeFetcher, Res, fixture_text

run = asyncio.run
HEX64 = r"[0-9a-f]{64}"
KEYS = {"ebay#client_id": "cid", "ebay#client_secret": "sec"}


class StoreFetcher(FakeFetcher):
    def __init__(self, store, *a, **k):
        super().__init__(*a, **k)
        self.store = store


class FakeFx:
    async def rate(self, ccy, day):
        return {"EUR": Decimal("4.30"), "GBP": Decimal("5.00"), "PLN": Decimal(1)}[ccy]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def _health(store):
    return [tuple(r) for r in store.conn.execute("select source, kind, detail from health")]


# ================================================================ eBay
class Recorder:
    """ApiClient request fn: token POST then search, replies from fixtures."""

    def __init__(self, search_status=200, search_body=None):
        self.calls, self.status = [], search_status
        self.body = search_body if search_body is not None else fixture_text("ebay", "search_de.json")

    async def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url, headers, data))
        if "oauth2/token" in url:
            return 200, json.dumps({"access_token": "tok123", "expires_in": 7200, "token_type": "Application Access Token"})
        return self.status, self.body


def _client(rec, **kw):
    async def nosleep(_): pass
    return ebay.ApiClient(rec, min_interval_s=0, sleep=nosleep, **kw)


def test_ebay_disabled_without_keys(store):
    rec = Recorder()
    ad = ebay.EbayBrowse("EBAY_DE", secret_fn=lambda ref: None, client=_client(rec))
    assert ad.enabled is False
    f = StoreFetcher(store)
    assert run(ad.search(Query(text="rtx 4090"), f)) == []
    assert run(ad.search(Query(text="rtx 4090"), f)) == []
    assert run(ad.active_comps("gpu:rtx-4090:24", f)) == []
    assert rec.calls == []  # nothing requested
    h = _health(store)
    assert len(h) == 1 and h[0][0] == "ebay" and "no eBay keys" in h[0][2]  # exactly one health event


def test_ebay_one_key_missing_is_disabled(store):
    ad = ebay.EbayBrowse("EBAY_PL", secret_fn=lambda ref: "x" if ref.endswith("client_id") else None)
    assert ad.enabled is False


def test_ebay_parses_fixture():
    ad = ebay.EbayBrowse("EBAY_DE", secret_fn=lambda r: None)
    ls = ebay.parse_search(fixture_text("ebay", "search_de.json"), ad)
    # auction and price 0 are dropped; the box-only and for-parts titles are kept for the extractor to flag
    assert [l.source_id for l in ls] == ["v1|314159265358|0", "v1|314159265359|0", "v1|314159265360|0", "v1|314159265361|0"]
    a, b, box, parts = ls
    assert (a.price, a.currency, a.country, a.source) == (Decimal("1549.00"), "EUR", "DE", "ebay_de")
    assert a.url == "https://www.ebay.de/itm/314159265358" and a.ships is True and a.is_business is False
    assert b.is_business is True and "condition: NEW" in b.description
    assert a.seller_ratings == 412 and a.seller_score == 99.2 and re.fullmatch(HEX64, a.seller_hash)
    assert "private_seller_1" not in repr(a)  # seller names are never kept
    assert "for parts" in parts.description
    assert a.posted_at == datetime(2026, 10, 27, 8, 15, tzinfo=timezone.utc)


def test_ebay_variation_listing_priced_at_highest_option(store):
    """Trap: title advertises the strongest option but the search price is the cheapest one (Koniu's FIFFEX
    screenshot: 'RTX 5090' at 2399 while the 5090 option costs 6699). The group endpoint must re-price."""
    async def req(method, url, headers, data, timeout):
        if "oauth2/token" in url:
            return 200, json.dumps({"access_token": "tok", "expires_in": 7200})
        if "get_items_by_item_group" in url:
            assert "item_group_id=999000111222" in url and headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_DE"
            return 200, fixture_text("ebay", "group_de.json")
        return 200, fixture_text("ebay", "search_variations_de.json")

    async def nosleep(_): pass
    ad = ebay.EbayBrowse("EBAY_DE", secret_fn=KEYS.get, client=ebay.ApiClient(req, min_interval_s=0, sleep=nosleep))
    ls = run(ad.search(Query(text="gaming pc"), StoreFetcher(store)))
    v = next(l for l in ls if l.source_id == "v1|999000111222|616564534592")
    assert v.price == Decimal("6699.00") and v.currency == "EUR"
    assert "variation listing verified: 3 options, price set to the highest option 6699.00 EUR" in v.description
    plain = next(l for l in ls if l.source_id == "v1|314159265358|0")
    assert "variation" not in plain.description and plain.price == Decimal("1549.00")


def test_ebay_variation_group_check_failure_keeps_warning(store):
    async def req(method, url, headers, data, timeout):
        if "oauth2/token" in url:
            return 200, json.dumps({"access_token": "tok", "expires_in": 7200})
        if "get_items_by_item_group" in url:
            return 400, '{"error": "not a group"}'
        return 200, fixture_text("ebay", "search_variations_de.json")

    async def nosleep(_): pass
    ad = ebay.EbayBrowse("EBAY_DE", secret_fn=KEYS.get, client=ebay.ApiClient(req, min_interval_s=0, sleep=nosleep))
    ls = run(ad.search(Query(text="gaming pc"), StoreFetcher(store)))
    v = next(l for l in ls if l.source_id == "v1|999000111222|616564534592")
    assert v.price == Decimal("2399.00")  # untouched
    assert "price may be for the cheapest option" in v.description  # the extractor/LLM still sees the warning


def test_us_item_ships_to_poland_only_when_regions_say_so():
    assert ebay.poland_in_ship_to({}) is None
    assert ebay.poland_in_ship_to({"shipToLocations": {"regionIncluded": [
        {"regionName": "Poland", "regionType": "COUNTRY"}]}}) is True
    assert ebay.poland_in_ship_to({"shipToLocations": {"regionIncluded": [
        {"regionName": "Worldwide", "regionType": "WORLDWIDE"}],
        "regionExcluded": [{"regionName": "PL", "regionType": "COUNTRY"}]}}) is False
    assert ebay.poland_in_ship_to({"shipToLocations": {"regionIncluded": [
        {"regionName": "United States", "regionType": "COUNTRY"}]}}) is False

    us = {
        "itemId": "v1|206686241832|0", "title": "RTX 3060 Ti", "itemWebUrl": "https://www.ebay.com/itm/206686241832",
        "price": {"value": "699.00", "currency": "USD"}, "buyingOptions": ["FIXED_PRICE"],
        "itemLocation": {"country": "US", "postalCode": "750**"},
        "shippingOptions": [{"shippingCost": {"value": "20.00", "currency": "USD"}}],
        "seller": {"username": "us_seller", "feedbackScore": 10},
    }
    domestic = json.dumps({"itemSummaries": [{**us, "shipToLocations": {"regionIncluded": [
        {"regionName": "United States", "regionType": "COUNTRY"}]}}]})
    abroad = json.dumps({"itemSummaries": [{**us, "shipToLocations": {"regionIncluded": [
        {"regionName": "Worldwide", "regionType": "WORLDWIDE"}]}}]})
    ad = ebay.EbayBrowse("EBAY_US", secret_fn=KEYS.get)
    no = ebay.parse_search(domestic, ad)[0]
    yes = ebay.parse_search(abroad, ad)[0]
    assert no.country == "US" and no.ships is False and "does not ship to Poland" in no.description
    assert yes.ships is True and "ships to Poland" in yes.description


def test_confirm_ships_to_poland_reads_getitem_once(store):
    seen = []

    async def req(method, url, headers, data, timeout):
        seen.append(url)
        if "oauth2/token" in url:
            return 200, json.dumps({"access_token": "tok", "expires_in": 7200})
        assert "v1%7C206686241832%7C0" in url
        assert headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
        return 200, json.dumps({"shipToLocations": {"regionIncluded": [
            {"regionName": "Europe", "regionType": "COUNTRY_REGION"}]}})

    async def nosleep(_): pass
    ad = ebay.EbayBrowse("EBAY_US", secret_fn=KEYS.get, client=ebay.ApiClient(req, min_interval_s=0, sleep=nosleep))
    item = ebay.parse_search(json.dumps({"itemSummaries": [{
        "itemId": "v1|206686241832|0", "title": "RTX 3060 Ti",
        "itemWebUrl": "https://www.ebay.com/itm/206686241832",
        "price": {"value": "699.00", "currency": "USD"}, "buyingOptions": ["FIXED_PRICE"],
        "itemLocation": {"country": "US"}, "seller": {"username": "us_seller"},
    }]}), ad)[0]
    assert item.ships is None
    assert run(ad.confirm_ships_to_poland(item)) is True
    assert run(ad.confirm_ships_to_poland(item)) is True
    assert sum(1 for u in seen if "oauth2" not in u) == 1  # cached; no second getItem


def test_ebay_search_sends_token_marketplace_and_filter(store):
    rec = Recorder()
    ad = ebay.EbayBrowse("EBAY_PL", secret_fn=KEYS.get, client=_client(rec))
    ls = run(ad.search(Query(text="rtx 4090"), StoreFetcher(store)))
    assert len(ls) == 4
    (m1, u1, h1, d1), (m2, u2, h2, _) = rec.calls
    assert m1 == "POST" and u1 == ebay.TOKEN_URL and h1["Authorization"].startswith("Basic ") and "client_credentials" in d1
    assert m2 == "GET" and u2.startswith(ebay.SEARCH_URL)
    assert h2["Authorization"] == "Bearer tok123" and h2["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_PL"
    assert h2["User-Agent"].startswith("DealHunter/")
    q = parse_qs(urlsplit(u2).query)
    assert q["q"] == ["rtx 4090"] and q["filter"] == ["itemLocationCountry:PL,buyingOptions:{FIXED_PRICE}"]
    run(ad.search(Query(text="rtx 4090"), StoreFetcher(store)))
    assert sum(1 for c in rec.calls if c[0] == "POST") == 1  # token cached


def test_ebay_active_comps_feed_valuation(store):
    rec = Recorder()
    ad = ebay.EbayBrowse("EBAY_DE", secret_fn=KEYS.get, client=_client(rec))
    comps = run(ad.active_comps("gpu:rtx-4090:24", StoreFetcher(store)))
    # only the two real RTX 4090 cards: no box-only title, no for-parts listing
    assert sorted(c.ref for c in comps) == ["ebay:v1|314159265358|0", "ebay:v1|314159265359|0"]
    assert all(isinstance(c, CompRow) and c.kind == "active_ask" and c.channel == "ebay_de" and c.currency == "EUR"
               and c.key == "gpu:rtx-4090:24" for c in comps)
    assert {c.condition for c in comps} <= {"used", "new", "like_new", None}
    assert parse_qs(urlsplit(rec.calls[-1][1]).query)["q"] == ["rtx 4090"]
    from dh.valuation import ingest_comps
    assert run(ingest_comps(store, comps, FakeFx())) == 2
    rows = store.comps("gpu:rtx-4090:24")
    assert sorted(Decimal(r["price_pln"]) for r in rows) == [Decimal("6660.70"), Decimal("8165.70")]
    assert run(ingest_comps(store, comps, FakeFx())) == 2  # active asks refresh, never duplicate
    assert len(store.comps("gpu:rtx-4090:24")) == 2


def test_ebay_api_backoff_and_disable(store):
    rec = Recorder(search_status=429, search_body="{}")
    t = [0.0]
    ad = ebay.EbayBrowse("EBAY_DE", secret_fn=KEYS.get, client=_client(rec, clock=lambda: t[0]))
    f = StoreFetcher(store)
    with pytest.raises(AdapterError):
        run(ad.search(Query(text="x"), f))
    with pytest.raises(AdapterError, match="backing off"):  # no request during the 60 s backoff
        run(ad.search(Query(text="x"), f))
    n = len(rec.calls)
    t[0] += 61
    with pytest.raises(AdapterError):
        run(ad.search(Query(text="x"), f))
    t[0] += 121
    with pytest.raises(AdapterError):
        run(ad.search(Query(text="x"), f))
    assert ad.client.disabled and len(rec.calls) == n + 2
    with pytest.raises(AdapterError, match="disabled"):
        run(ad.search(Query(text="x"), f))
    kinds = [k for _, k, _ in _health(store)]
    assert kinds.count("blocked") == 3 and "disabled" in kinds


def _basic(cid, sec):
    import base64
    return "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode()


def test_two_keys_switch_on_quota_and_do_not_disable(store):
    keys = {"ebay#client_id": "cid1", "ebay#client_secret": "sec1",
            "ebay2#client_id": "cid2", "ebay2#client_secret": "sec2"}

    class Rec:
        def __init__(self):
            self.calls = []

        async def __call__(self, method, url, headers, data, timeout):
            self.calls.append((method, url, headers, data))
            auth = headers.get("Authorization", "")
            if "oauth2/token" in url:
                tok = "tok1" if auth == _basic("cid1", "sec1") else "tok2"
                return 200, json.dumps({"access_token": tok, "expires_in": 7200})
            if auth == "Bearer tok1":
                return 429, "{}", {"retry-after": "90"}
            return 200, fixture_text("ebay", "search_de.json")

    rec = Rec()
    t = [0.0]
    ad = ebay.EbayBrowse("EBAY_DE", secret_fn=keys.get, client=_client(rec, clock=lambda: t[0]))
    ls = run(ad.search(Query(text="rtx 4090"), StoreFetcher(store)))
    assert ls and not ad.client.disabled and ad.client.refusals == 0
    auths = [c[2]["Authorization"] for c in rec.calls]
    assert auths[0] == _basic("cid1", "sec1") and "Bearer tok1" in auths
    assert auths[-2] == _basic("cid2", "sec2") and auths[-1] == "Bearer tok2"
    kinds = [k for _, k, _ in _health(store)]
    assert "quota" in kinds and "blocked" not in kinds and "disabled" not in kinds
    # key 1 is cooling; the next search stays on key 2 and does not ask for a new token
    n = len(rec.calls)
    run(ad.search(Query(text="rtx 4090"), StoreFetcher(store)))
    assert len(rec.calls) == n + 1 and rec.calls[-1][2]["Authorization"] == "Bearer tok2"


def test_two_keys_both_cooling_back_off_without_another_call(store):
    keys = {"ebay#client_id": "cid1", "ebay#client_secret": "sec1",
            "ebay2#client_id": "cid2", "ebay2#client_secret": "sec2"}

    class Rec:
        def __init__(self):
            self.calls = []

        async def __call__(self, method, url, headers, data, timeout):
            self.calls.append((method, url, dict(headers), data))
            if "oauth2/token" in url:
                who = "a" if headers.get("Authorization") == _basic("cid1", "sec1") else "b"
                return 200, json.dumps({"access_token": who, "expires_in": 7200})
            return 429, "{}", {"retry-after": "60"}

    rec = Rec()
    ad = ebay.EbayBrowse("EBAY_DE", secret_fn=keys.get, client=_client(rec, clock=lambda: 0.0))
    with pytest.raises(AdapterError, match="backing off"):
        run(ad.search(Query(text="x"), StoreFetcher(store)))
    n = len(rec.calls)
    assert not ad.client.disabled
    with pytest.raises(AdapterError, match="backing off"):
        run(ad.search(Query(text="x"), StoreFetcher(store)))
    assert len(rec.calls) == n


def test_load_key_sets_stops_at_a_gap_and_skips_a_secretless_item():
    def hole(ref):
        return {"ebay#client_id": "a", "ebay#client_secret": "b",
                "ebay3#client_id": "e", "ebay3#client_secret": "f"}.get(ref)
    assert ebay.load_key_sets(hole) == [("a", "b")]

    def skip(ref):
        return {"ebay#client_id": "a", "ebay#client_secret": "b",
                "ebay2#client_id": "c",
                "ebay3#client_id": "e", "ebay3#client_secret": "f"}.get(ref)
    assert ebay.load_key_sets(skip) == [("a", "b"), ("e", "f")]
    seen = []

    def eight(ref):
        seen.append(ref)
        item = ref.split("#", 1)[0]
        n = 1 if item == "ebay" else int(item.replace("ebay", ""))
        if n <= 8:
            return "id" if ref.endswith("client_id") else "sec"
        return "no"
    assert len(ebay.load_key_sets(eight)) == 8
    assert not any(s.startswith("ebay9") for s in seen)


def test_query_for_key():
    assert ebay.query_for_key("gpu:rtx-4090:24") == "rtx 4090"
    assert ebay.query_for_key("ram:ddr5:32:2x16") == "ddr5 32gb"
    assert ebay.query_for_key("ssd:nvme:2tb") == "ssd nvme 2tb"


# ================================================================ RSS
def _pepper():
    return rss.RssAdapter("pepper_pl", "https://www.pepper.pl/rss/hot", "PL", "PLN")


def test_rss_parses_pepper_fixture():
    ls = rss.parse_feed(fixture_text("rss", "pepper.xml"), _pepper())
    by = {l.source_id: l for l in ls}
    assert set(by) == {"1377909", "1377415", "1390001", "1390002"}  # the "Gratis" item has no price: skipped
    gpu = by["1390001"]
    assert gpu.title.startswith("Karta graficzna Gigabyte GeForce RTX 5070")  # "340° - " prefix stripped
    assert (gpu.price, gpu.currency, gpu.source, gpu.country, gpu.is_business) == (Decimal("2399"), "PLN", "pepper_pl", "PL", True)
    assert gpu.url == "https://www.pepper.pl/promocje/karta-graficzna-rtx-5070-2399-1390001"
    assert gpu.posted_at == datetime(2026, 10, 29, 17, 0, tzinfo=timezone.utc)
    assert by["1377415"].price == Decimal("249.99")  # price from the title
    assert by["1377909"].price == Decimal("1886")  # no price in title: merchant attribute "1 886zł"
    assert by["1377909"].photos and "shop: MediaMarkt" in by["1377909"].description


def test_rss_hotukdeals_gbp():
    ad = rss.RssAdapter("hotukdeals", "https://www.hotukdeals.com/rss/hot", "GB", "GBP")
    ls = {l.source_id: l for l in rss.parse_feed(fixture_text("rss", "hotukdeals.xml"), ad)}
    assert ls["5020001"].price == Decimal("549.99") and ls["5020001"].currency == "GBP"
    assert ls["5017136"].price == Decimal("7.99") and ls["5017136"].country == "GB"


def test_rss_search_filters_by_query_and_is_polite(store):
    f = StoreFetcher(store, {"pepper.pl/rss/hot": Res(200, fixture_text("rss", "pepper.xml"))})
    ls = run(_pepper().search(Query(text="rtx 5070"), f))
    assert [l.source_id for l in ls] == ["1390001"]
    assert f.urls == ["https://www.pepper.pl/rss/hot"]
    for bad, why in ((Res(403, "", blocked=True), "blocked"), (Res(200, "", disallowed=True), "robots"),
                     (Res(200, "<html>not xml"), "XML"), (Res(500, ""), "status 500")):
        with pytest.raises(AdapterError, match=why):
            run(_pepper().search(Query(text="x"), StoreFetcher(store, {"pepper": bad})))


@pytest.mark.parametrize("title,ccy,expected", [
    ("Karta za 249,99zł @ Shop", "PLN", Decimal("249.99")), ("SSD 2 399 zł", "PLN", Decimal("2399")),
    ("Case £7.99 delivered", "GBP", Decimal("7.99")), ("RTX 4090 gratis", "PLN", None),
    ("1TB SSD 2 szt", "PLN", None), ("Offer 0 zł", "PLN", None)])
def test_rss_title_price(title, ccy, expected):
    r = rss.title_price(title, ccy)
    assert (r[0] if r else None) == expected


# ================================================================ retail
SRC = [{"shop": "morele", "url": "https://www.morele.net/kategoria/karty-graficzne-12/"},
       {"shop": "xkom", "url": "https://www.x-kom.pl/p/1318530-karta-graficzna-nvidia-gigabyte-geforce-rtx-5070-gaming-oc-12gb-gddr7-dlss5.html"}]


def _pages(morele_price="4399.00", xkom_price=3299):
    m = fixture_text("retail", "morele_category.html").replace('"4399.00"', f'"{morele_price}"')
    x = fixture_text("retail", "xkom_product.html").replace('"price": 3299', f'"price": {xkom_price}')
    assert str(morele_price) in m and str(xkom_price) in x
    return {"morele.net": Res(200, m), "x-kom.pl": Res(200, x)}


def test_retail_parse_products():
    ps = retail.parse_products(fixture_text("retail", "morele_category.html"), "https://x/")
    assert [p["price"] for p in ps] == [Decimal("3599.00"), Decimal("4399.00"), Decimal("6290.00"), Decimal("10.00")]
    assert [p["available"] for p in ps] == [True, True, False, True]
    x = retail.parse_products(fixture_text("retail", "xkom_product.html"), "https://www.x-kom.pl/p/1")
    assert x[0]["price"] == Decimal(3299) and x[0]["url"] == "https://www.x-kom.pl/p/1" and x[0]["sku"] == "1318530"


def test_retail_emits_only_on_drop(store):
    w = retail.RetailWatch(SRC)
    t0 = now_utc()

    def scan(**kw):
        return run(w.scan(StoreFetcher(store, _pages(**kw)), store, now=t0))

    assert scan() == []                       # first sighting: baseline only
    assert scan() == []                       # unchanged
    assert scan(morele_price="4200.00") == []  # -4.5%: below the 10% threshold
    got = scan(morele_price="3700.00")         # 4200 -> 3700 = -11.9%
    assert len(got) == 1
    l = got[0]
    assert (l.source, l.price, l.currency, l.is_business) == ("retail", Decimal("3700.00"), "PLN", True)
    assert l.title.startswith("Karta graficzna Gigabyte GeForce RTX 5070") and "dropped from 4200.00" in l.description
    assert l.url.startswith("https://www.morele.net/") and l.source_id.startswith("morele:")
    assert scan(morele_price="3700.00") == []  # same price again: nothing new
    # out-of-stock RTX 5070 Ti and the box-only item never become listings
    assert all("5070 Ti" not in x.title and "Karton" not in x.title for x in got)


def test_retail_feeds_retail_new_comps(store):
    w = retail.RetailWatch(SRC)
    run(w.scan(StoreFetcher(store, _pages()), store, now=now_utc()))
    rows = store.comps("gpu:rtx-5070:12")
    assert {r["kind"] for r in rows} == {"retail_new"} and {r["channel"] for r in rows} == {"morele", "xkom"}
    assert sorted(Decimal(r["price_pln"]) for r in rows) == [Decimal("3299.00"), Decimal("4399.00")]
    run(w.scan(StoreFetcher(store, _pages()), store, now=now_utc()))
    assert len(store.comps("gpu:rtx-5070:12")) == 2  # same day: idempotent
    assert store.comps("gpu:rtx-5070-ti:16") == []  # out of stock: not a price
    from dh.valuation import estimate
    est = estimate(store, "gpu:rtx-5070:12", None, "olx", now_utc())
    assert est is None  # retail alone is a ceiling, not a clears-at comparable


def test_retail_emits_below_p25(store):
    t0 = now_utc()
    for i in range(6):  # used market clears around 5000 PLN
        store.add_comp("gpu:rtx-5070:12", "used", "olx", Decimal("5000") + i, t0, "owner_sale", f"s{i}")
    w = retail.RetailWatch([SRC[0]])
    got = run(w.scan(StoreFetcher(store, _pages()), store, now=t0))  # first sighting, but 4399 < P25
    assert [l.price for l in got] == [Decimal("4399.00")] and "below clears-at P25" in got[0].description


def test_retail_blocked_page_reports(store):
    w = retail.RetailWatch(SRC)
    f = StoreFetcher(store, {"morele.net": Res(403, "", blocked=True), "x-kom.pl": Res(200, "", disallowed=True)})
    with pytest.raises(AdapterError, match="blocked"):
        run(w.scan(f, store))
    ok = StoreFetcher(store, {"morele.net": Res(403, "", blocked=True), "x-kom.pl": Res(200, fixture_text("retail", "xkom_product.html"))})
    assert run(w.scan(ok, store)) == []  # one shop down, the other still scanned


# ================================================================ registry
def test_registry_has_new_adapters_and_ebay_is_disabled_without_keys(monkeypatch):
    monkeypatch.setattr("dh.notify.bw_secret", lambda ref: None)
    reg = registry()
    for n in ("ebay_de", "ebay_pl", "pepper_pl", "hotukdeals", "retail"):
        assert n in reg
    assert reg["ebay_de"].enabled is False
    assert reg["retail"].watch and all(w["shop"] and w["url"].startswith("https://") for w in reg["retail"].watch)
    assert reg["pepper_pl"].domain == "www.pepper.pl" and reg["hotukdeals"].currency == "GBP"


# ================================================================ shared base contract (redaction, FX-aware bounds)
def test_rss_phone_and_email_redacted():
    xml = fixture_text("rss", "pepper.xml").replace(
        "<strong>2 399zł - x-kom</strong>", "<strong>2 399zł - x-kom</strong> dzwoń 512 345 678 lub a@b.pl")
    l = next(x for x in rss.parse_feed(xml, _pepper()) if x.source_id == "1390001")
    assert "512 345 678" not in l.description and "a@b.pl" not in l.description


def test_rss_title_phone_redacted_and_flagged():
    xml = fixture_text("rss", "pepper.xml").replace("za 2 399 zł @ x-kom", "za 2 399 zł tel. 512 345 678")
    l = next(x for x in rss.parse_feed(xml, _pepper()) if x.source_id == "1390001")
    assert "512 345 678" not in l.title and l.contact_in_text is True and l.price == Decimal("2399")


def test_ebay_description_redacted_and_eur_bounds_filtered(store):
    body = json.loads(fixture_text("ebay", "search_de.json"))
    body["itemSummaries"][0]["condition"] = "Used, call +49 176 12345678"
    rec = Recorder(search_body=json.dumps(body))
    ad = ebay.EbayBrowse("EBAY_DE", secret_fn=KEYS.get, client=_client(rec))
    f = StoreFetcher(store)
    to_pln = lambda amt, ccy: amt * Decimal("4.30") if ccy == "EUR" else None
    ls = run(ad.search(Query(text="rtx 4090"), f))
    assert "12345678" not in ls[0].description and ls[0].contact_in_text
    # 1549 EUR = 6660.70 PLN, 1899 EUR = 8165.70 PLN, 25 EUR = 107.50, 600 EUR = 2580
    q = Query(text="rtx 4090", min_price_pln=Decimal("3000"), max_price_pln=Decimal("7000"))
    got = run(ad.search(q, f, to_pln=to_pln))
    assert [l.source_id for l in got] == ["v1|314159265358|0"]
    # no rate available: bounds cannot be applied, listings are kept (shared filter_query behaviour)
    assert len(run(ad.search(q, f))) == 4


def test_retail_listing_text_goes_through_make(store):
    w = retail.RetailWatch([SRC[0]])
    t0 = now_utc()
    run(w.scan(StoreFetcher(store, _pages()), store, now=t0))
    got = run(w.scan(StoreFetcher(store, _pages(morele_price="3700.00")), store, now=t0))
    assert got and got[0].contact_in_text is False


# ================================================================ review round 1
def test_retail_p25_channel_from_enabled_exit_channels():
    pick = retail.pick_p25_channel
    assert pick(["allegro", "olx", "ebay_pl", "ebay_de"]) == "allegro"
    assert pick(["olx", "ebay_pl", "ebay_de"]) == "olx"
    assert pick(["ebay_de", "ebay_pl"]) == "ebay_pl"
    assert pick(["ebay_de"]) is None and pick([]) is None
    assert retail.RetailWatch([], exit_channels=["ebay_pl"]).estimate_channel == "ebay_pl"
    assert retail.RetailWatch([]).estimate_channel == "allegro"  # from config/economics.yaml


def test_retail_p25_skipped_without_pl_channel_and_passes_chosen_channel(store):
    t0 = now_utc()
    for i in range(6):
        store.add_comp("gpu:rtx-5070:12", "used", "olx", Decimal("5000") + i, t0, "owner_sale", f"s{i}")
    w = retail.RetailWatch([SRC[0]], exit_channels=["ebay_de"])
    assert run(w.scan(StoreFetcher(store, _pages()), store, now=t0)) == []  # no P25 check at all
    seen = []
    def est(store_, key, cond, channel, day):
        seen.append(channel)
        return None
    w2 = retail.RetailWatch([SRC[0]], exit_channels=["ebay_pl", "olx"])
    run(w2.scan(StoreFetcher(store, _pages()), store, now=t0, estimate=est))
    assert set(seen) == {"olx"}


def test_retail_below_p25_not_reemitted(store):
    t0 = now_utc()
    for i in range(6):
        store.add_comp("gpu:rtx-5070:12", "used", "olx", Decimal("5000") + i, t0, "owner_sale", f"s{i}")
    w = retail.RetailWatch([SRC[0]], exit_channels=["olx"])
    assert len(run(w.scan(StoreFetcher(store, _pages()), store, now=t0))) == 1
    assert run(w.scan(StoreFetcher(store, _pages()), store, now=t0)) == []  # same price: already emitted
    assert len(run(w.scan(StoreFetcher(store, _pages(morele_price="4100.00")), store, now=t0))) == 1  # new price (retail ceiling 0.95*4399 caps P25)


def test_api_client_host_allowlist_and_shared_registry():
    assert ebay.ALLOWED_API_HOSTS == {"api.ebay.com"}
    c = ebay.api_client_for("api.ebay.com")
    assert ebay.api_client_for("API.ebay.com") is c
    assert ebay.EbayBrowse("EBAY_DE", secret_fn=lambda r: None).client is c is ebay.EbayBrowse("EBAY_PL", secret_fn=lambda r: None).client
    with pytest.raises(AdapterError):
        ebay.api_client_for("www.ebay.de")
    rec = Recorder()
    cl = _client(rec)
    for url in ("https://www.ebay.de/itm/1", "https://evil.example/x", "https://api.ebay.com.evil.example/x", "api.ebay.com/x"):
        with pytest.raises(AdapterError, match="ALLOWED_API_HOSTS"):
            run(cl.call("GET", url))
    assert rec.calls == []  # nothing was sent


def test_shared_client_shares_refusals(store):
    rec = Recorder(search_status=429, search_body="{}")
    cl = _client(rec, clock=lambda: 0.0)
    de = ebay.EbayBrowse("EBAY_DE", secret_fn=KEYS.get, client=cl)
    pl = ebay.EbayBrowse("EBAY_PL", secret_fn=KEYS.get, client=cl)
    with pytest.raises(AdapterError):
        run(de.search(Query(text="x"), StoreFetcher(store)))
    n = len(rec.calls)
    with pytest.raises(AdapterError, match="backing off"):  # the other marketplace sees the same backoff
        run(pl.search(Query(text="x"), StoreFetcher(store)))
    assert len(rec.calls) == n


def test_api_client_honours_retry_after():
    class R:
        calls = 0
        async def __call__(self, *a):
            R.calls += 1
            return 429, "", {"retry-after": "300"}
    t = [0.0]
    cl = _client(R(), clock=lambda: t[0])
    with pytest.raises(AdapterError):
        run(cl.call("GET", "https://api.ebay.com/x"))
    t[0] = 200  # beyond our own 60 s, still inside Retry-After
    with pytest.raises(AdapterError, match="backing off"):
        run(cl.call("GET", "https://api.ebay.com/x"))
    t[0] = 301
    with pytest.raises(AdapterError, match="api refused"):
        run(cl.call("GET", "https://api.ebay.com/x"))
    assert R.calls == 2


def test_no_keys_event_recorded_once_for_both_marketplaces(store):
    f = StoreFetcher(store)
    for cc in ("EBAY_DE", "EBAY_PL", "EBAY_DE"):
        run(ebay.EbayBrowse(cc, secret_fn=lambda r: None, client=_client(Recorder())).search(Query(text="x"), f))
    assert [k for _, k, _ in _health(store)].count("no_keys") == 1


def test_bw_secret_cached_per_process(monkeypatch):
    calls = []
    monkeypatch.setattr("dh.notify.bw_secret", lambda ref: calls.append(ref))
    ebay.clear_secret_cache()
    a = ebay.EbayBrowse("EBAY_DE")
    assert a.enabled is False and ebay.EbayBrowse("EBAY_PL").enabled is False and a.enabled is False
    assert calls == ["ebay#client_id"]  # a missing id ends the list before the secret, and before ebay2
    ebay.clear_secret_cache()


@pytest.mark.parametrize("title,expected", [
    ("Karta od 3 999 zł do 2 899 zł", Decimal("2899")),
    ("Karta za 2 899 zł (było 3 999 zł)", Decimal("2899")),
    ("Karta (było 3 999 zł) teraz 2 899 zł", Decimal("2899")),
    ("Monitor 1 299 zł, kod -10% do 100 zł", Decimal("1299")),  # "do 100 zł" is the coupon's cap, not the price
    ("Karta z 3 999 zł do 2 899 zł", Decimal("2899")),
    ("Karta 2 899 zł", Decimal("2899"))])
def test_rss_title_price_prefers_deal_price(title, expected):
    assert rss.title_price(title, "PLN")[0] == expected


@pytest.mark.parametrize("title,expected", [
    # the old-price words need a word boundary: "prize"/"Größe ze" endings, "canvas" are not "ze"/"was"
    ("Grand prize 499 zł RTX 4070", Decimal("499")),
    ("Canvas 199 zł", Decimal("199")),
    ("Etui Größe 99 zł", Decimal("99")),
    ("Karta ze 3 999 zł teraz 2 899 zł", Decimal("2899")),  # a real "ze" old price still loses
    ("Karta was 3 999 zł now 2 899 zł", Decimal("2899")),
    # coupon caps and discount amounts are never the price
    ("Kupon -20% do 100 zł na RTX 4070 Super", None),
    ("Kod rabatowy 15% do 300 zł na karty graficzne", None),
    ("RTX 4070 Super 2 499 zł + kupon -10% do 200 zł", Decimal("2499")),
    ("RTX 4070 za 2 499 zł, rabat 100 zł z kodem", Decimal("2499")),
    ("Cashback 200 zł na RTX 5070", None),
    ("15% off up to £50 on graphics cards", None),
    ("Karta od 3 999 zł do 2 899 zł", Decimal("2899")),  # a plain "do" after a price range is the deal price
])
def test_rss_title_price_word_boundaries_and_coupons(title, expected):
    r = rss.title_price(title, "GBP" if "£" in title else "PLN")
    assert (r[0] if r else None) == expected, title


def test_prefetch_resolves_ebay_keys_in_parallel_once_per_run(monkeypatch):
    """Profiling: two sequential `bw-secret` calls (~4.8 s each with no item) blocked a round's event loop for
    ~9.6 s. build_context now prefetches both refs in parallel threads; `enabled` then reads the cache."""
    import threading
    import time
    calls, active, peak = [], [0], [0]
    lock = threading.Lock()

    known = {"ebay#client_id": "cid", "ebay#client_secret": "sec"}

    def slow(ref):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.3)
        with lock:
            active[0] -= 1
        calls.append(ref)
        return known.get(ref)
    monkeypatch.setattr("dh.notify.bw_secret", slow)
    ebay.clear_secret_cache()
    t0 = time.monotonic()
    ebay.prefetch_secrets()
    # every candidate ref in one wave: 8 eBay items x 2 fields, plus the Allegro pair
    assert time.monotonic() - t0 < 1.0 and peak[0] == len(ebay.KEY_REFS)
    assert sorted(calls) == sorted(ebay.KEY_REFS)
    assert ebay.EbayBrowse("EBAY_DE").enabled is True and ebay.EbayBrowse("EBAY_PL").enabled is True
    ebay.prefetch_secrets()
    assert len(calls) == len(ebay.KEY_REFS)  # cached: not asked again
    ebay.clear_secret_cache()


def test_build_context_prefetches_the_ebay_keys(tmp_path, monkeypatch):
    from pathlib import Path

    from dh import config, runtime
    seen = []
    monkeypatch.setattr(ebay, "prefetch_secrets", lambda *a, **k: seen.append(1))
    repo = Path(__file__).resolve().parents[2]
    st = Store(tmp_path / "t.db")
    try:
        runtime.build_context(config.load(repo), tmp_path, st, fetcher=None, no_llm=True)
    finally:
        st.close()
    assert seen == [1]


def test_ebay_ship_to_pl_filter_on_foreign_markets(store):
    """Every non-PL market filters to items the seller ships to Poland; PL itself does not need it."""
    from urllib.parse import parse_qs as pq, urlsplit as us
    rec = Recorder()
    ad = ebay.EbayBrowse("EBAY_GB", secret_fn=KEYS.get, client=_client(rec))
    run(ad.search(Query(text="rtx 4090"), StoreFetcher(store)))
    assert pq(us(rec.calls[-1][1]).query)["filter"] == ["itemLocationCountry:GB,deliveryCountry:PL,buyingOptions:{FIXED_PRICE}"]
    for cc, cn in (("EBAY_FR", "FR"), ("EBAY_CH", "CH")):
        flt = pq(us(ebay.EbayBrowse(cc).search_url(Query(text="x"))).query)["filter"][0]
        assert flt == f"itemLocationCountry:{cn},deliveryCountry:PL,buyingOptions:{{FIXED_PRICE}}"
    assert "deliveryCountry" not in ebay.EbayBrowse("EBAY_PL").search_url(Query(text="x"))
    for cc, cn in (("EBAY_US", "US"), ("EBAY_CA", "CA")):
        flt = pq(us(ebay.EbayBrowse(cc).search_url(Query(text="x"))).query)["filter"][0]
        assert flt == f"itemLocationCountry:{cn},deliveryCountry:PL,buyingOptions:{{FIXED_PRICE}}"
    br = ebay.EbayBrowse("EBAY_US", location="BR", source_name="ebay_br")
    assert br.name == "ebay_br" and br.marketplace == "EBAY_US" and br.currency == "USD"
    flt = pq(us(br.search_url(Query(text="x"))).query)["filter"][0]
    assert flt == "itemLocationCountry:BR,deliveryCountry:PL,buyingOptions:{FIXED_PRICE}"

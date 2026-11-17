import json
from decimal import Decimal

import pytest

from dh.models import Listing
from dh.sources.base import AdapterError, Query
from dh.sources.tori import ADAPTER, parse_search

from helpers import Res, check_listings, fixture_text, sub_first


def ls():
    return parse_search(fixture_text("tori", "search.json"))


def test_search_parses_docs():
    out = ls()
    check_listings(out, "EUR")
    for l in out:  # Finnish site: prices EUR, ids numeric, canonical URLs
        assert l.url.startswith("https://www.tori.fi/recommerce/forsale/item/")
    a = out[0]
    assert a.price == Decimal("280") and a.currency == "EUR"
    assert a.title.startswith("Asus ROG Strix GeForce RTX 3070")
    assert a.city  # location present ("Jyväskylä" style)


def test_business_flag_from_flags():
    biz = [l for l in ls() if l.is_business]
    assert biz and all(l.is_business for l in biz)  # 'retailer' flag => business
    assert any(l.is_business is False for l in ls())


def test_sold_and_wanted_dropped():
    docs = {"docs": [
        {"id": "1", "heading": "A", "price": {"amount": 10, "currency_code": "EUR"},
         "trade_type": "Myydään", "flags": ["private", "sold"],
         "canonical_url": "https://www.tori.fi/recommerce/forsale/item/1"},
        {"id": "2", "heading": "B", "price": {"amount": 20, "currency_code": "EUR"},
         "trade_type": "Ostetaan", "flags": ["private"],
         "canonical_url": "https://www.tori.fi/recommerce/forsale/item/2"},
        {"id": "3", "heading": "C", "price": {"amount": 30, "currency_code": "EUR"},
         "trade_type": "Myydään", "flags": ["private"],
         "canonical_url": "https://www.tori.fi/recommerce/forsale/item/3"},
        {"id": "4", "heading": "Vaihdetaan GTX 1070", "price": {"amount": 150, "currency_code": "EUR"},
         "trade_type": "Vaihdetaan", "flags": ["private"],
         "canonical_url": "https://www.tori.fi/recommerce/forsale/item/4"}]}
    out = parse_search(json.dumps(docs))
    assert [l.source_id for l in out] == ["3"]  # sold, wanted and swap gone


def test_price_edge_cases():
    docs = {"docs": [
        {"id": str(i), "heading": "x", "trade_type": "Myydään",
         "canonical_url": f"https://www.tori.fi/recommerce/forsale/item/{i}", **p}
        for i, p in enumerate([{"price": {"amount": 0}}, {"price": {"amount": None}},
                               {"price": None}, {"price": {"amount": 5, "currency_code": "EUR"}}])]}
    assert [l.price for l in parse_search(json.dumps(docs))] == [Decimal("5")]


def test_search_api_failure_raises():
    with pytest.raises(AdapterError, match="search API did not return JSON|disallows"):
        ADAPTER.parse_page('<!doctype html><div class="v-progress"></div>')


def test_detail_price_and_fields():
    out = ls()
    html = fixture_text("tori", "detail.html")
    l = ADAPTER.parse_detail(out[0], html)
    assert ADAPTER.detail_price(html) == Decimal("280")
    assert l.description and len(l.description) > len(l.title)  # body beats heading
    assert l.photos


def test_detail_price_survives_missing_offers():
    html = fixture_text("tori", "detail.html")
    mod = html.replace('"offers"', '"xoffers"')
    assert ADAPTER.detail_price(mod) is None  # no offers block => no anchor


def test_search_url_hits_api_with_encoded_query():
    urls = ADAPTER.page_urls(Query(text="rtx 3070 ti"))
    assert urls == ["https://www.tori.fi/recommerce/forsale/search/api/search/"
                    "SEARCH_ID_BAP_COMMON?q=rtx+3070+ti"]


@pytest.mark.asyncio
async def test_search_fetches_json_and_parses():
    from dh.sources.base import Query as Q

    class JsonFetcher:
        def __init__(self):
            self.urls = []

        async def get(self, url, *, kind="html", headers=None):
            self.urls.append((url, kind))
            return Res(200, fixture_text("tori", "search.json"), url)

    f = JsonFetcher()
    out = await ADAPTER.search(Q(text="rtx 3070"), f)
    assert out and f.urls[0][1] == "json"


@pytest.mark.asyncio
async def test_search_non200_raises():
    class F:
        async def get(self, url, *, kind="html", headers=None):
            return Res(500, "", url)
    with pytest.raises(AdapterError, match="status 500"):
        await ADAPTER.search(Query(text="x"), F())


def test_gone_markers_real_finnish_forms():
    low = "Tämä ilmoitus on myyty. Ilmoitus on poistettu.".lower()  # base matches against lowered text
    assert any(m in low for m in ADAPTER.gone_markers)
    assert not any(m in "myydään hyvä näytönohjain" for m in ADAPTER.gone_markers)


def test_image_protocol_relative_upgraded():
    docs = {"docs": [{"id": "9", "heading": "x", "trade_type": "Myydään",
                      "price": {"amount": 5, "currency_code": "EUR"},
                      "image_urls": ["//img.tori.net/a/b"],
                      "canonical_url": "https://www.tori.fi/recommerce/forsale/item/9"}]}
    assert parse_search(json.dumps(docs))[0].photos == ["https://img.tori.net/a/b"]

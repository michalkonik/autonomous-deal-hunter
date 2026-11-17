"""Allegro REST public offer search. Fixtures are synthetic. The website is never requested."""
from __future__ import annotations

import asyncio
import base64
import json
from decimal import Decimal

import pytest

from dh.sources import AdapterError, Query
from dh.sources import allegro as al
from dh.sources.base import now_utc
from dh.store import Store

from helpers import fixture_text

run = asyncio.run
KEYS = {"allegro#client_id": "cid", "allegro#client_secret": "sec"}


class Rec:
    def __init__(self, search=(200, None), token=(200, None)):
        self.calls = []
        self.search = search if search[1] is not None else (200, fixture_text("allegro", "listing.json"))
        self.token = token if token[1] is not None else (200, json.dumps({"access_token": "tokA", "expires_in": 43200}))

    async def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url, headers, data))
        if "oauth/token" in url:
            return self.token
        return self.search


def client(rec, **kw):
    async def nosleep(_):
        pass
    return al.AllegroClient(rec, min_interval_s=0, sleep=nosleep, **kw)


class StoreFetcher:
    def __init__(self, store):
        self.store = store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def health(store):
    return [tuple(r) for r in store.conn.execute("select source, kind, detail from health")]


def test_disabled_without_keys_records_one_event(store):
    rec = Rec()
    ad = al.Allegro(secret_fn=lambda r: None, client=client(rec))
    f = StoreFetcher(store)
    assert ad.enabled is False
    assert run(ad.search(Query(text="rtx 4090"), f)) == []
    assert run(ad.search(Query(text="rtx 4090"), f)) == []
    assert rec.calls == []
    h = health(store)
    assert len(h) == 1 and h[0][0] == "allegro" and "no Allegro keys" in h[0][2]


def test_parse_keeps_buy_now_and_drops_the_rest():
    ad = al.Allegro(secret_fn=lambda r: None)
    ls = al.parse_listing(fixture_text("allegro", "listing.json"), ad)
    assert [l.source_id for l in ls] == ["1111222233", "6666777788"]
    a, b = ls
    assert a.url == "https://allegro.pl/oferta/1111222233"
    assert a.price == Decimal("6499.00") and a.currency == "PLN"
    assert a.is_business is False and a.ships is True and "delivery: free" in a.description
    assert a.photos == ["https://a.allegroimg.com/original/rtx4090.jpg"]
    assert "hiddenLogin" not in repr(a) and "shopLogin" not in repr(b)
    assert b.is_business is True and "delivery: free" not in b.description


def test_search_sends_registered_user_agent_and_caches_token(store):
    rec = Rec()
    ad = al.Allegro(secret_fn=KEYS.get, client=client(rec))
    ls = run(ad.search(Query(text="rtx 4090"), StoreFetcher(store)))
    assert [l.source_id for l in ls] == ["1111222233", "6666777788"]
    (m1, u1, h1, d1), (m2, u2, h2, _) = rec.calls
    assert m1 == "POST" and u1 == al.TOKEN_URL
    assert h1["User-Agent"] == al.DEFAULT_UA
    assert h1["Authorization"] == "Basic " + base64.b64encode(b"cid:sec").decode()
    assert "client_credentials" in d1
    assert m2 == "GET" and u2.startswith(al.LISTING_URL)
    assert h2["User-Agent"] == al.DEFAULT_UA
    assert h2["Accept"] == al.ACCEPT and h2["Authorization"] == "Bearer tokA"
    assert "phrase=rtx+4090" in u2 and "fallback=false" in u2 and "searchMode=REGULAR" in u2
    run(ad.search(Query(text="rtx 4090"), StoreFetcher(store)))
    assert sum(1 for c in rec.calls if c[0] == "POST") == 1


def test_website_host_is_refused(store):
    rec = Rec()
    cl = client(rec)
    with pytest.raises(AdapterError, match="not fetched"):
        run(cl.call("GET", "https://www.allegro.pl/listing?string=rtx"))
    with pytest.raises(AdapterError, match="not fetched"):
        run(cl.call("GET", "https://allegro.pl.evil.example/offers/listing"))
    assert rec.calls == []


def test_detail_and_alive_do_not_fetch(store):
    rec = Rec()
    ad = al.Allegro(secret_fn=KEYS.get, client=client(rec))
    ls = run(ad.search(Query(text="rtx 4090"), StoreFetcher(store)))
    n = len(rec.calls)
    assert run(ad.detail(ls[0], StoreFetcher(store))) is ls[0]
    with pytest.raises(AdapterError, match="not fetched"):
        run(ad.alive(ls[0], StoreFetcher(store)))
    assert len(rec.calls) == n


def test_verification_required_disables_without_a_second_call(store):
    body = json.dumps({"errors": [{"code": "VerificationRequired",
                                   "message": "Access is denied. Verification is required."}]})
    rec = Rec(search=(403, body))
    ad = al.Allegro(secret_fn=KEYS.get, client=client(rec))
    f = StoreFetcher(store)
    assert run(ad.search(Query(text="rtx"), f)) == []
    assert run(ad.search(Query(text="rtx"), f)) == []
    assert not ad.enabled
    assert sum(1 for c in rec.calls if "offers/listing" in c[1]) == 1
    assert health(store)[0][1] == "not_verified"
    assert "not fetched" in health(store)[0][2]


def test_bad_credentials_disable(store):
    rec = Rec(token=(401, json.dumps({"error": "invalid_client"})))
    ad = al.Allegro(secret_fn=KEYS.get, client=client(rec))
    assert run(ad.search(Query(text="rtx"), StoreFetcher(store))) == []
    assert run(ad.search(Query(text="rtx"), StoreFetcher(store))) == []
    assert [k for _, k, _ in health(store)] == ["bad_credentials"]
    assert len(rec.calls) == 1


def test_quota_backs_off_then_disables(store):
    rec = Rec(search=(429, "{}"))
    t = [0.0]
    ad = al.Allegro(secret_fn=KEYS.get, client=client(rec, clock=lambda: t[0]))
    f = StoreFetcher(store)
    with pytest.raises(AdapterError, match="refused"):
        run(ad.search(Query(text="x"), f))
    with pytest.raises(AdapterError, match="backing off"):
        run(ad.search(Query(text="x"), f))
    n = len(rec.calls)
    t[0] += 61
    with pytest.raises(AdapterError):
        run(ad.search(Query(text="x"), f))
    t[0] += 121
    with pytest.raises(AdapterError):
        run(ad.search(Query(text="x"), f))
    assert ad.client.disabled and len(rec.calls) == n + 2


def test_registry_includes_allegro_with_the_validated_user_agent():
    from dh.config import load
    from dh.sources import registry
    from dh.sources.base import ROOT
    reg = registry(load(ROOT), ROOT)
    assert reg["allegro"].name == "allegro"
    assert reg["allegro"].domain == "api.allegro.pl"
    assert reg["allegro"].user_agent == al.DEFAULT_UA
    assert "family-finance-planner" in reg["allegro"].user_agent

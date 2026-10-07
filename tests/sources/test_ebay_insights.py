"""Task 23: eBay Marketplace Insights sold comps (fixture is synthetic; no scope granted yet)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from dh.models import CompRow
from dh.sources import AdapterError, ebay, ebay_insights as ei
from dh.store import Store

from helpers import fixture_text

run = asyncio.run
KEY = "gpu:rtx-4090:24"
KEYS = {"ebay#client_id": "cid", "ebay#client_secret": "sec"}


class Rec:
    def __init__(self, token=(200, None), sales=(200, None)):
        self.calls = []
        self.token = token if token[1] is not None else (200, json.dumps({"access_token": "tokI", "expires_in": 7200}))
        self.sales = sales if sales[1] is not None else (200, fixture_text("ebay", "sales_de.json"))

    async def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url, headers, data))
        return self.token if "oauth2/token" in url else self.sales


def client(rec, **kw):
    async def nosleep(_): pass
    return ebay.ApiClient(rec, min_interval_s=0, sleep=nosleep, **kw)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def health(store):
    return [tuple(r) for r in store.conn.execute("select source, kind, detail from health")]


def test_sold_comps_from_fixture(store):
    rec = Rec()
    ad = ei.EbayInsights(secret_fn=KEYS.get, client=client(rec), store=store)
    rows = run(ad.sold_comps(KEY))
    # box-only, for-parts, price 0 and a different chip are dropped
    assert sorted(r.ref for r in rows) == ["ebay_sold:v1|314000000001|0:2026-10-25T10:12:00.000Z",
                                           "ebay_sold:v1|314000000002|0:2026-10-20T18:00:00.000Z"]
    r = [x for x in rows if "0001" in x.ref][0]
    assert isinstance(r, CompRow) and (r.kind, r.channel, r.currency, r.price, r.key) == ("ebay_sold", "ebay_de", "EUR", Decimal("1480.00"), KEY)
    assert r.at == datetime(2026, 10, 25, 10, 12, tzinfo=timezone.utc) and r.condition == "used" and r.weight == 1.0
    (m1, u1, h1, d1), (m2, u2, h2, _) = rec.calls
    assert m1 == "POST" and ebay.TOKEN_URL == u1 and "buy.marketplace.insights" in d1
    assert m2 == "GET" and u2.startswith("https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search")
    assert parse_qs(urlsplit(u2).query)["q"] == ["rtx 4090"]
    assert h2["Authorization"] == "Bearer tokI" and h2["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_DE"
    assert health(store) == []


def test_ingest_into_valuation(store):
    from dh.valuation import ingest_comps

    class Fx:
        async def rate(self, ccy, day):
            return Decimal("4.30")
    rows = run(ei.EbayInsights(secret_fn=KEYS.get, client=client(Rec()), store=store).sold_comps(KEY))
    assert run(ingest_comps(store, rows, Fx())) == 2
    assert run(ingest_comps(store, rows, Fx())) == 0  # sold comps are idempotent by ref
    assert len(store.comps(KEY)) == 2 and {c["kind"] for c in store.comps(KEY)} == {"ebay_sold"}


def test_scope_missing_disables_with_one_health_event(store):
    rec = Rec(token=(400, json.dumps({"error": "invalid_scope", "error_description": "The scope is invalid"})))
    ad = ei.EbayInsights(secret_fn=KEYS.get, client=client(rec), store=store)
    assert run(ad.sold_comps(KEY)) == [] and run(ad.sold_comps(KEY)) == []
    assert not ad.enabled and len(rec.calls) == 1  # no second token or search request
    assert health(store) == [("ebay_insights", "not_granted", "Marketplace Insights not granted")]


def test_search_403_means_not_granted_once(store):
    rec = Rec(sales=(403, '{"errors":[{"errorId":1100}]}'))
    ad = ei.EbayInsights(secret_fn=KEYS.get, client=client(rec), store=store)
    assert run(ad.sold_comps(KEY)) == []
    assert run(ad.sold_comps(KEY)) == []
    assert len(rec.calls) == 2 and not ad.enabled
    assert [k for _, k, _ in health(store)].count("not_granted") == 1


def test_no_keys_disabled(store):
    rec = Rec()
    ad = ei.EbayInsights(secret_fn=lambda r: None, client=client(rec), store=store)
    assert run(ad.sold_comps(KEY)) == [] and rec.calls == []
    assert [k for _, k, _ in health(store)] == ["no_keys"]


def test_server_error_raises():
    ad = ei.EbayInsights(secret_fn=KEYS.get, client=client(Rec(sales=(500, "x"))))
    with pytest.raises(AdapterError):
        run(ad.sold_comps(KEY))


def test_default_client_is_shared_accessor():
    ad = ei.EbayInsights(secret_fn=KEYS.get)
    assert ad.client is ebay.api_client_for("api.ebay.com")

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dh.money import Fx, FxError, parse_price
from dh.store import Store

FIX = Path(__file__).parent / "fixtures" / "nbp"


@pytest.mark.parametrize("text,ccy,expected", [
    ("3 500 zł", "PLN", (Decimal("3500"), "PLN")),
    ("3.500,00 €", "EUR", (Decimal("3500.00"), "EUR")),
    ("1,2 tys. zł", "PLN", (Decimal("1200"), "PLN")),
    ("12 990 Kč", "CZK", (Decimal("12990"), "CZK")),
    ("189 000 Ft", "HUF", (Decimal("189000"), "HUF")),
    ("Za darmo", "PLN", None),
    ("0 zł", "PLN", None),
    ("do negocjacji", "PLN", None),
    ("", "PLN", None),
    ("3 500 zł", "PLN", (Decimal("3500"), "PLN")),
    ("1 234,50 zł", "PLN", (Decimal("1234.50"), "PLN")),
    ("1,234.50 €", "EUR", (Decimal("1234.50"), "EUR")),
    ("899,99 PLN", "EUR", (Decimal("899.99"), "PLN")),
    ("4500", "PLN", (Decimal("4500"), "PLN")),
    ("EUR 1.250", "PLN", (Decimal("1250"), "EUR")),
    ("$99", "PLN", (Decimal("99"), "USD")),
    ("-5 zł", "PLN", None),
    ("0,500 zł", "PLN", (Decimal("0.500"), "PLN")),
    ("0.500 €", "EUR", (Decimal("0.500"), "EUR")),
    ("3,500 zł", "PLN", (Decimal("3500"), "PLN")),
    ("10,500 zł", "PLN", (Decimal("10500"), "PLN")),
])
def test_parse_price(text, ccy, expected):
    assert parse_price(text, ccy) == expected


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def __call__(self, url):
        self.calls.append(url)
        for frag, resp in self.routes.items():
            if frag in url:
                return resp
        return 404, "not found"


def nbp(name):
    return (FIX / name).read_text()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


async def test_fx_falls_back_to_previous_business_day(store):
    # 2026-10-24 and 2026-10-25 are Sat/Sun: 404, then Friday 10-23 answers.
    http = FakeHttp({
        "/eur/2026-10-24/": (404, nbp("eur_2026-10-24_404.txt")),
        "/eur/2026-10-25/": (404, "404 NotFound"),
        "/eur/2026-10-23/": (200, nbp("eur_2026-10-28.json")),
    })
    fx = Fx(store, http, today=lambda: date(2026, 11, 30))
    assert await fx.rate("EUR", date(2026, 10, 25)) == Decimal("4.4090")
    assert [u.split("/eur/")[1][:10] for u in http.calls] == ["2026-10-25", "2026-10-24", "2026-10-23"]
    # cached under the requested day: no further HTTP
    assert await fx.rate("eur", date(2026, 10, 25)) == Decimal("4.4090")
    assert len(http.calls) == 3


async def test_fx_exact_day_and_cache_in_db(store):
    http = FakeHttp({"/eur/2026-10-29/": (200, nbp("eur_2026-10-29.json"))})
    fx = Fx(store, http, today=lambda: date(2026, 10, 30))
    assert await fx.rate("EUR", date(2026, 10, 29)) == Decimal("4.3951")
    row = store.conn.execute("select rate_pln from fx where date='2026-10-29' and currency='EUR'").fetchone()
    assert row["rate_pln"] == "4.3951"
    fx2 = Fx(store, FakeHttp({}), today=lambda: date(2026, 10, 30))  # new instance: served from DB
    assert await fx2.rate("EUR", date(2026, 10, 29)) == Decimal("4.3951")


async def test_fx_fallback_not_cached_for_today(store):
    # today's table may not be published yet: do not pin yesterday's rate under today's date
    http = FakeHttp({"/eur/2026-10-29/": (404, "x"), "/eur/2026-10-28/": (200, nbp("eur_2026-10-28.json"))})
    fx = Fx(store, http, today=lambda: date(2026, 10, 29))
    assert await fx.rate("EUR", date(2026, 10, 29)) == Decimal("4.4090")
    assert store.conn.execute("select count(*) c from fx where date='2026-10-29'").fetchone()["c"] == 0


async def test_fx_conversions(store):
    http = FakeHttp({
        "/eur/2026-10-29/": (200, nbp("eur_2026-10-29.json")),
        "/usd/2026-10-29/": (200, json.dumps({"rates": [{"effectiveDate": "2026-10-29", "mid": 4.0}]})),
    })
    fx = Fx(store, http, today=lambda: date(2026, 10, 30))
    d = date(2026, 10, 29)
    assert await fx.to_pln(Decimal("100"), "EUR", d) == Decimal("439.51")
    assert await fx.to_pln(Decimal("100"), "PLN", d) == Decimal("100.00")
    assert await fx.pln_to_usd(Decimal("1000"), d) == Decimal("250.00")
    assert await fx.pln_to_usd(Decimal("1001"), d) == Decimal("250.25")


async def test_fx_gives_up_after_limit(store):
    http = FakeHttp({})
    fx = Fx(store, http, today=lambda: date(2026, 11, 30), max_back_days=5)
    with pytest.raises(FxError):
        await fx.rate("EUR", date(2026, 10, 25))
    assert len(http.calls) == 6


async def test_fx_bad_request_is_error_not_fallback(store):
    http = FakeHttp({"/eur/": (400, "400 BadRequest - Invalid date range")})
    fx = Fx(store, http, today=lambda: date(2026, 10, 29))
    with pytest.raises(FxError):
        await fx.rate("EUR", date(2026, 12, 1))
    assert len(http.calls) == 1


@pytest.mark.live
async def test_live_nbp_today_eur(store):
    from datetime import date as d
    fx = Fx(store)
    r = await fx.rate("EUR", d.today())
    print("LIVE EUR", d.today(), r)
    assert Decimal("3") < r < Decimal("6")


async def test_fx_network_error_becomes_fx_error(store):
    import httpx

    async def boom(url):
        raise httpx.ConnectError("down")
    with pytest.raises(FxError):
        await Fx(store, boom).rate("EUR", date(2026, 10, 29))


async def test_fx_rate_with_date_weekend_returns_friday(store):
    http = FakeHttp({
        "/eur/2026-10-25/": (404, "404 NotFound"),
        "/eur/2026-10-24/": (404, "404 NotFound"),
        "/eur/2026-10-23/": (200, nbp("eur_2026-10-28.json")),
    })
    fx = Fx(store, http, today=lambda: date(2026, 11, 30))
    assert await fx.rate_with_date("EUR", date(2026, 10, 25)) == (Decimal("4.4090"), date(2026, 10, 23))
    n = len(http.calls)
    # served from the DB cache, effective date preserved
    assert await fx.rate_with_date("EUR", date(2026, 10, 25)) == (Decimal("4.4090"), date(2026, 10, 23))
    assert len(http.calls) == n
    assert await fx.rate("EUR", date(2026, 10, 25)) == Decimal("4.4090")
    assert await fx.rate_with_date("PLN", date(2026, 10, 25)) == (Decimal("1"), date(2026, 10, 25))


async def test_fx_adds_effective_date_column_to_old_db(tmp_path):
    s = Store(tmp_path / "old.db")
    s.conn.execute("drop table fx")
    s.conn.execute("create table fx (date text not null, currency text not null, rate_pln text not null, "
                   "primary key (date, currency))")
    s.conn.execute("insert into fx values ('2026-10-29','EUR','4.3951')")
    s.conn.commit()
    fx = Fx(s, FakeHttp({}), today=lambda: date(2026, 10, 30))
    assert await fx.rate_with_date("EUR", date(2026, 10, 29)) == (Decimal("4.3951"), date(2026, 10, 29))
    s.close()

"""EU adapters: kleinanzeigen, bazos (cz/sk), hardverapro, marktplaats. Fixtures captured live on the rig."""
from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal

import pytest

from dh.sources import AdapterError, Query
from dh.sources import bazos as bz
from dh.sources import hardverapro as hv
from dh.sources import kleinanzeigen as ka
from dh.sources import marktplaats as mp
from dh.sources import ememain as em
from dh.sources.base import filter_query, price_of

from pathlib import Path

from dh.sources import allegro_lokalnie as al
from helpers import FakeFetcher, Res, check_listings, fixture_text, sub_first

run = asyncio.run
HEX64 = r"[0-9a-f]{64}"


# ---------------------------------------------------------------- Review Focus 2: price strings
@pytest.mark.parametrize("text,ccy,expected", [
    ("2.900 €", "EUR", (Decimal("2900"), "EUR")),
    ("1 899 990 Ft", "HUF", (Decimal("1899990"), "HUF")),
    ("50 000 Kč", "CZK", (Decimal("50000"), "CZK")),
    ("3 500 zł", "PLN", (Decimal("3500"), "PLN")),
    ("1,2 tys. zł", "PLN", (Decimal("1200"), "PLN")),
    ("do negocjacji", "PLN", None), ("0 zł", "PLN", None), ("0 €", "EUR", None), ("VB", "EUR", None),
    ("Dohodou", "CZK", None), ("Csere", "HUF", None), ("Zu verschenken", "EUR", None), ("", "EUR", None),
])
def test_price_parsing_edge_cases(text, ccy, expected):
    assert price_of(text, ccy) == expected


# ---------------------------------------------------------------- kleinanzeigen
def test_kleinanzeigen_search_parses_fixture():
    ls = ka.parse_search(fixture_text("kleinanzeigen", "search.html"))
    check_listings(ls, "EUR")
    assert all("/s-anzeige/" in l.url for l in ls)
    assert any(l.ships for l in ls)


def test_kleinanzeigen_skips_wanted_and_unpriced():
    html = fixture_text("kleinanzeigen", "search.html")
    ls = ka.parse_search(html)
    n = len(ls)
    for bad in ("VB", "0 €", "Zu verschenken"):
        mutated = sub_first(html, rf'(data-adid="{ls[0].source_id}".*?)>\d[\d.]* €[^<]*<', rf"\g<1>>{bad}<")
        assert len(ka.parse_search(mutated)) == n - 1, bad
    # a "Gesuch" (wanted) tag makes a priced card a buyer's ad: skipped
    wanted = sub_first(html, rf'(<article[^>]*data-adid="{ls[0].source_id}"[^>]*>)', r"\g<1><span>Gesuch</span>")
    assert len(ka.parse_search(wanted)) == n - 1
    assert ls[0].source_id not in {l.source_id for l in ka.parse_search(wanted)}


def test_kleinanzeigen_detail_fixture():
    html = fixture_text("kleinanzeigen", "detail.html")
    l = ka.parse_search(fixture_text("kleinanzeigen", "search.html"))[0]
    d = ka.ADAPTER.parse_detail(l, html)
    assert len(d.description) > 40 and "Zustand" in d.description
    assert re.fullmatch(HEX64, d.seller_hash)
    assert d.seller_age_days and d.seller_age_days > 30
    assert d.ships in (True, False) and d.is_business in (True, False)
    assert d.photos
    assert ka.ADAPTER.detail_price(html) > 0


def test_kleinanzeigen_search_url_alive():
    ad = ka.ADAPTER
    assert ad.search_url(Query("RTX 4090")) == "https://www.kleinanzeigen.de/s-rtx-4090/k0"
    l = ka.parse_search(fixture_text("kleinanzeigen", "search.html"))[0]
    detail = fixture_text("kleinanzeigen", "detail.html")
    ok, price = run(ad.alive(l, FakeFetcher(default=Res(200, detail, l.url))))
    assert ok and price > 0
    assert run(ad.alive(l, FakeFetcher(default=Res(404)))) == (False, None)
    gone = Res(200, "<html>Die Anzeige ist nicht mehr verfügbar</html>", l.url)
    assert run(ad.alive(l, FakeFetcher(default=gone))) == (False, None)
    moved = Res(200, "<html>Suche</html>", "https://www.kleinanzeigen.de/s-rtx-4090/k0")
    assert run(ad.alive(l, FakeFetcher(default=moved))) == (False, None)
    with pytest.raises(AdapterError):
        run(ad.alive(l, FakeFetcher(default=Res(503))))


# ---------------------------------------------------------------- bazos
@pytest.mark.parametrize("country,currency", [("cz", "CZK"), ("sk", "EUR")])
def test_bazos_search_parses_fixture(country, currency):
    ad = bz.Bazos(country)
    ls = bz.parse_search(fixture_text(f"bazos_{country}", "search.html"), ad)
    check_listings(ls, currency)
    assert all(f"pc.bazos.{country}/inzerat/" in l.url for l in ls)
    assert all(l.seller_hash for l in ls) and any(l.posted_at for l in ls)


def test_bazos_price_parsing_edge_cases():
    ad = bz.Bazos("cz")
    html = fixture_text("bazos_cz", "search.html")
    n = len(bz.parse_search(html, ad))
    for bad in ("Dohodou", "0 Kč", "V textu"):
        mutated = sub_first(html, r'(<div class="inzeratycena">.*?<span[^>]*>)[^<]*', rf"\g<1>{bad}")
        assert len(bz.parse_search(mutated, ad)) == n - 1, bad


def test_bazos_local_filter_and_sections():
    ad = bz.Bazos("cz")
    html = fixture_text("bazos_cz", "search.html")
    allq = bz.parse_search(html, ad)
    rtx = filter_query(allq, Query("rtx"), text=True)
    assert 0 < len(rtx) <= len(allq)
    assert all("rtx" in f"{l.title} {l.description}".lower() for l in rtx)
    assert filter_query(allq, Query("zzz-nonexistent-qq"), text=True) == []
    assert ad.category_only
    assert bz.sections_for("cz", Query("rtx 4090")) == ["graficka"]
    assert bz.sections_for("cz", Query("macbook pro")) == ["notebook"]
    assert bz.sections_for("cz", Query("something else")) == ["pc"]
    assert bz.sections_for("at", Query("rtx 4090")) == ["grafikkarten"]
    assert filter_query(allq, Query(""), text=True) == allq


def test_bazos_never_uses_search_form():
    """robots.txt disallows /search.php and *hledat=* *cenaod=* *order=*; only category pages are read."""
    ad = bz.Bazos("cz", pages=2)
    f = FakeFetcher(default=Res(200, fixture_text("bazos_cz", "search.html")))
    ls = run(ad.search(Query("rtx"), f))
    assert f.urls == ["https://pc.bazos.cz/graficka/", "https://pc.bazos.cz/graficka/20/"]
    assert not any(x in u for u in f.urls for x in ("search.php", "hledat", "cenaod", "order="))
    assert ls and len({l.source_id for l in ls}) == len(ls)  # page 2 repeats are de-duplicated
    with pytest.raises(AdapterError):
        run(ad.search(Query("rtx"), FakeFetcher(default=Res(0, "", "", disallowed=True))))


@pytest.mark.parametrize("country", ["cz", "sk"])
def test_bazos_detail_and_alive(country):
    ad = bz.Bazos(country)
    html = fixture_text(f"bazos_{country}", "detail.html")
    l = bz.parse_search(fixture_text(f"bazos_{country}", "search.html"), ad)[0]
    d = ad.parse_detail(l, html)
    assert len(d.description) > 20 and d.photos and re.fullmatch(HEX64, d.seller_hash)
    price = ad.detail_price(html)
    assert price and price > 0
    ok, p2 = run(ad.alive(l, FakeFetcher(default=Res(200, html, l.url))))
    assert ok and p2 == price
    assert run(ad.alive(l, FakeFetcher(default=Res(410)))) == (False, None)
    assert run(ad.alive(l, FakeFetcher(default=Res(200, "Inzerát již není aktivní", l.url)))) == (False, None)


def test_bazos_seller_hash_is_salted_and_source_scoped():
    cz = bz.parse_search(fixture_text("bazos_cz", "search.html"), bz.Bazos("cz"))[0]
    again = bz.parse_search(fixture_text("bazos_cz", "search.html"), bz.Bazos("cz"))[0]
    assert cz.seller_hash == again.seller_hash
    assert "Martin" not in json.dumps(cz.__dict__, default=str)


# ---------------------------------------------------------------- hardverapro
def test_hardverapro_search_parses_fixture():
    ls = hv.parse_search(fixture_text("hardverapro", "search.html"))
    check_listings(ls, "HUF")
    assert all(l.url.startswith("https://hardverapro.hu/apro/") for l in ls)
    assert all(l.seller_hash for l in ls)


def test_hardverapro_skips_reserved_and_unpriced():
    html = fixture_text("hardverapro", "search.html")
    ls = hv.parse_search(html)
    n = len(ls)
    for bad in ("Csere", "0 Ft", "Keresek"):
        mutated = sub_first(html, r'(<div class="uad-col uad-col-title">.*?class="text-nowrap">)[^<]*', rf"\g<1>{bad}")
        assert len(hv.parse_search(mutated)) == n - 1, bad
    iced = [i for i in re.findall(r'<li class="media[^"]*uad-status-iced[^"]*"', html)]
    all_cards = len(re.findall(r'<li class="media', html))
    assert len(iced) + n <= all_cards  # iced ("jegelve") ads are never returned


def test_hardverapro_detail_and_alive():
    ad = hv.ADAPTER
    html = fixture_text("hardverapro", "detail.html")
    l = hv.parse_search(fixture_text("hardverapro", "search.html"))[0]
    d = ad.parse_detail(l, html)
    assert len(d.description) > 40 and d.photos and re.fullmatch(HEX64, d.seller_hash)
    assert d.seller_age_days and d.seller_ratings is not None
    price = ad.detail_price(html)
    assert price and price > 0
    ok, p2 = run(ad.alive(l, FakeFetcher(default=Res(200, html, l.url))))
    assert ok and p2 == price
    assert run(ad.alive(l, FakeFetcher(default=Res(404)))) == (False, None)
    assert run(ad.alive(l, FakeFetcher(default=Res(200, "A hirdetés nem elérhető", l.url)))) == (False, None)


def test_hardverapro_search_url_avoids_disallowed_offset():
    ad = hv.ADAPTER
    url = ad.search_url(Query("rtx 4090"))
    assert url == "https://hardverapro.hu/aprok/keres.php?stext=rtx+4090&buying=0" and "offset=" not in url
    f = FakeFetcher(default=Res(200, fixture_text("hardverapro", "search.html")))
    assert len(run(ad.search(Query("rtx 4090"), f))) >= 10 and f.urls == [url]


# ---------------------------------------------------------------- marktplaats
def test_marktplaats_search_parses_fixture():
    ls = mp.parse_search(fixture_text("marktplaats", "search.html"))
    check_listings(ls, "EUR")
    assert all(l.url.startswith("https://www.marktplaats.nl/v/") for l in ls)
    assert all(l.seller_hash and l.photos for l in ls)
    assert any("Staat:" in l.description for l in ls)


def test_marktplaats_price_parsing_edge_cases():
    html = fixture_text("marktplaats", "search.html")
    data = json.loads(re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S).group(1))
    items = data["props"]["pageProps"]["searchRequestAndResponse"]["listings"]
    n = len(mp.parse_search(html))
    assert any(i["priceInfo"]["priceType"] == "MIN_BID" for i in items), "fixture should contain a MIN_BID ad"

    def mutate(fn):
        d = json.loads(json.dumps(data))
        fn(d["props"]["pageProps"]["searchRequestAndResponse"]["listings"])
        return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(d)}</script>'

    def zero(ls):
        next(i for i in ls if i["priceInfo"]["priceType"] in ("FIXED", "FAST_BID") and i["priceInfo"]["priceCents"])[
            "priceInfo"]["priceCents"] = 0

    def min_bid(ls):
        next(i for i in ls if i["priceInfo"]["priceType"] == "FIXED" and i["priceInfo"]["priceCents"])[
            "priceInfo"]["priceType"] = "MIN_BID"

    def see_desc(ls):
        next(i for i in ls if i["priceInfo"]["priceType"] == "FIXED" and i["priceInfo"]["priceCents"])[
            "priceInfo"] = {"priceCents": 0, "priceType": "SEE_DESCRIPTION"}

    def reserved(ls):
        next(i for i in ls if i["priceInfo"]["priceType"] == "FIXED" and i["priceInfo"]["priceCents"])["reserved"] = True

    for fn in (zero, min_bid, see_desc, reserved):
        assert len(mp.parse_search(mutate(fn))) == n - 1, fn.__name__


def test_marktplaats_layout_change_is_an_error_not_empty():
    with pytest.raises(AdapterError, match="__NEXT_DATA__"):
        mp.parse_search("<html>interstitial</html>")
    with pytest.raises(AdapterError, match="shape"):
        mp.parse_search('<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>')


def test_marktplaats_uses_html_not_disallowed_api():
    """robots.txt: Disallow /lrp/api/search* and /search?q=*; /q/<query>/ is allowed."""
    ad = mp.ADAPTER
    f = FakeFetcher(default=Res(200, fixture_text("marktplaats", "search.html")))
    assert len(run(ad.search(Query("rtx 4090"), f))) >= 10
    assert f.urls == ["https://www.marktplaats.nl/q/rtx+4090/"]
    assert "/lrp/api/" not in f.urls[0] and "/search?q=" not in f.urls[0]


def test_marktplaats_detail_and_alive():
    ad = mp.ADAPTER
    html = fixture_text("marktplaats", "detail.html")
    l = mp.parse_search(fixture_text("marktplaats", "search.html"))[0]
    d = ad.parse_detail(l, html)
    assert d.description and d.photos
    assert "Levering:" in d.description or "Staat:" in d.description
    ok, price = run(ad.alive(l, FakeFetcher(default=Res(200, html, l.url))))
    assert ok and price > 0
    assert run(ad.alive(l, FakeFetcher(default=Res(410)))) == (False, None)
    moved = Res(200, html, "https://www.marktplaats.nl/q/rtx/")  # redirected away: id not in the final URL
    assert run(ad.alive(l, FakeFetcher(default=moved))) == (False, None)


def test_marktplaats_skips_wanted_ads():
    html = fixture_text("marktplaats", "search.html")
    ls = mp.parse_search(html)
    victim = ls[0].title
    mutated = html.replace(json.dumps(victim, ensure_ascii=False)[1:-1], "GEZOCHT: " + victim, 1)
    assert len(mp.parse_search(mutated)) == len(ls) - 1


def test_alive_requires_price_confirmation_everywhere():
    """A page that loads with no gone marker but no readable price is "unverifiable", never (True, None)."""
    cases = [(ka.ADAPTER, ka.parse_search(fixture_text("kleinanzeigen", "search.html"))[0]),
             (hv.ADAPTER, hv.parse_search(fixture_text("hardverapro", "search.html"))[0]),
             (mp.ADAPTER, mp.parse_search(fixture_text("marktplaats", "search.html"))[0]),
             (bz.Bazos("cz"), bz.parse_search(fixture_text("bazos_cz", "search.html"), bz.Bazos("cz"))[0])]
    for ad, l in cases:
        with pytest.raises(AdapterError, match="unverifiable"):
            run(ad.alive(l, FakeFetcher(default=Res(200, "<html><body>ok</body></html>", l.url))))


def test_hardverapro_and_bazos_alive_detect_redirect_away():
    ad = hv.ADAPTER
    l = hv.parse_search(fixture_text("hardverapro", "search.html"))[0]
    html = fixture_text("hardverapro", "detail.html")
    assert run(ad.alive(l, FakeFetcher(default=Res(200, html, "https://hardverapro.hu/aprok/index.html")))) == (False, None)
    b = bz.Bazos("cz")
    bl = bz.parse_search(fixture_text("bazos_cz", "search.html"), b)[0]
    assert run(b.alive(bl, FakeFetcher(default=Res(200, fixture_text("bazos_cz", "detail.html"),
                                                   "https://pc.bazos.cz/graficka/")))) == (False, None)


# ---------------------------------------------------------------- FX-aware bounds (one place: filter_query)
def test_filter_query_converts_foreign_prices_to_pln():
    rates = {"EUR": Decimal("4.30"), "HUF": Decimal("0.0110")}
    conv = lambda amt, ccy: amt * rates[ccy]  # noqa: E731
    eur = ka.parse_search(fixture_text("kleinanzeigen", "search.html"))
    huf = hv.parse_search(fixture_text("hardverapro", "search.html"))
    q = Query("x", min_price_pln=Decimal(5000), max_price_pln=Decimal(12000))
    for ls, ccy in ((eur, "EUR"), (huf, "HUF")):
        out = filter_query(ls, q, conv)
        assert out and len(out) < len(ls)
        assert all(Decimal(5000) <= l.price * rates[ccy] <= Decimal(12000) for l in out)
        assert all(l not in out for l in ls if not Decimal(5000) <= l.price * rates[ccy] <= Decimal(12000))


def test_filter_query_missing_rate_keeps_listing_and_logs_once(caplog):
    from dh.sources import base
    base._warned_ccy.clear()
    eur = ka.parse_search(fixture_text("kleinanzeigen", "search.html"))
    q = Query("x", max_price_pln=Decimal(1))
    with caplog.at_level("WARNING"):
        assert filter_query(eur, q, None) == eur
        assert filter_query(eur, q, lambda a, c: None) == eur
    assert len([r for r in caplog.records if "no PLN rate" in r.message]) == 1


def test_adapter_search_applies_fx_bounds():
    ad = hv.ADAPTER
    f = FakeFetcher(default=Res(200, fixture_text("hardverapro", "search.html")))
    all_ = run(ad.search(Query("rtx 4090"), f))
    q = Query("rtx 4090", max_price_pln=Decimal(5000))
    some = run(ad.search(q, f, to_pln=lambda a, c: a * Decimal("0.011")))
    assert 0 < len(some) < len(all_) and all(l.price * Decimal("0.011") <= 5000 for l in some)


# ---------------------------------------------------------------- redaction at storage
def test_redact_phone_email_tel():
    from dh.sources.base import redact
    t, hit = redact("Zadzwoń 600 123 456 lub pisz a.b@example.com, tel:+48600123456 albo +49 170 1234567. Serial 12345678.")
    assert hit and "600" not in t and "@" not in t and "tel:" not in t and "170" not in t
    assert t.count("[redacted]") == 4 and "Serial 12345678." in t
    assert redact("Karta RTX 4090, 24GB, stan idealny") == ("Karta RTX 4090, 24GB, stan idealny", False)
    t, hit = redact("Telefon: 600123456")
    assert hit and "600123456" not in t


def test_search_and_detail_redact_contact_and_flag_it():
    html = fixture_text("allegro_lokalnie", "detail.html")
    l = al.parse_search(fixture_text("allegro_lokalnie", "search.html"))[0]
    d = al.ADAPTER.parse_detail(l, html + "<div class='mlc-offer__description'>Sprzedam. SMS: 600 062 184 lub x@y.pl</div>")
    # the fixture description may differ; build a controlled one
    d = al.ADAPTER.parse_detail(l, "<div class='mlc-offer__description'>Sprzedam. SMS: 600 062 184 lub x@y.pl</div>")
    assert "600 062" not in d.description and "x@y.pl" not in d.description and d.contact_in_text is True
    clean = al.ADAPTER.parse_detail(l, "<div class='mlc-offer__description'>Sprzedam, odbior Krakow</div>")
    assert clean.contact_in_text is True  # sticky: the flag never resets once set
    fresh = al.parse_search(fixture_text("allegro_lokalnie", "search.html"))[1]
    assert al.ADAPTER.parse_detail(fresh, "<div class='mlc-offer__description'>Sprzedam</div>").contact_in_text is False


def test_redacted_listing_still_trips_the_risk_contact_feature():
    from dh import config, risk
    from dh.models import Extraction
    l = al.parse_search(fixture_text("allegro_lokalnie", "search.html"))[0]
    d = al.ADAPTER.parse_detail(l, "<div class='mlc-offer__description'>Sprzedam. Zadzwon 600 123 456</div>")
    assert "600" not in d.description and d.contact_in_text
    cfg = config.load(Path(__file__).resolve().parents[2])
    r = risk.assess(d, Extraction(key=None, category=None, certainty=0.9, condition="used"), None, cfg)
    # final review I3 ruling: a (redacted) phone number is the score feature contact_in_text, not a hard exclude
    assert not r.hard_exclude and "contact_in_text" in r.flags


# ---------------------------------------------------------------- 2ememain (BE)
def test_2ememain_search_parses_fixture():
    ls = em.parse_search(fixture_text("2ememain", "search.html"))
    check_listings(ls, "EUR", n=8)
    assert all(l.url.startswith("https://www.2ememain.be/v/") for l in ls)
    assert all(l.seller_hash and l.photos for l in ls)


def test_2ememain_price_and_flag_mutations():
    html = fixture_text("2ememain", "search.html")
    data = json.loads(re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S).group(1))
    n = len(em.parse_search(html))
    assert n >= 5

    def mutate(fn):
        d = json.loads(json.dumps(data))
        fn(d["props"]["pageProps"]["searchRequestAndResponse"]["listings"])
        return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(d)}</script>'

    def zero(ls):
        next(i for i in ls if i.get("priceInfo", {}).get("priceType") == "FIXED" and i["priceInfo"]["priceCents"])[
            "priceInfo"]["priceCents"] = 0

    def reserved(ls):
        next(i for i in ls if i.get("priceInfo", {}).get("priceType") == "FIXED" and i["priceInfo"]["priceCents"])[
            "reserved"] = True

    def wanted(ls):
        next(i for i in ls if i.get("priceInfo", {}).get("priceType") == "FIXED")["title"] = "RECHERCHE : RTX 3070"

    def no_url(ls):
        next(i for i in ls if i.get("priceInfo", {}).get("priceType") == "FIXED")["vipUrl"] = None

    for fn in (zero, reserved, wanted, no_url):
        assert len(em.parse_search(mutate(fn))) == n - 1, fn.__name__


def test_2ememain_dates_french():
    from dh.sources.ememain import _posted
    assert _posted("Aujourd'hui") is not None
    assert _posted("Hier") is not None
    from zoneinfo import ZoneInfo
    d = _posted("18 oct. 26")
    loc = d.astimezone(ZoneInfo("Europe/Brussels"))
    assert loc.year == 2026 and loc.month == 10 and loc.day == 18
    assert _posted("18 oct. 2026") == d
    assert _posted("il y a 2 jours") is None


def test_2ememain_layout_change_is_an_error_not_empty():
    with pytest.raises(AdapterError, match="__NEXT_DATA__"):
        em.parse_search("<html>interstitial</html>")


def test_2ememain_uses_allowed_html_page():
    ad = em.ADAPTER
    f = FakeFetcher(default=Res(200, fixture_text("2ememain", "search.html")))
    assert len(run(ad.search(Query("rtx 4090"), f))) >= 5
    assert f.urls == ["https://www.2ememain.be/q/rtx+4090/"]


def test_2ememain_detail_and_alive():
    ad = em.ADAPTER
    html = fixture_text("2ememain", "detail.html")
    l = em.parse_search(fixture_text("2ememain", "search.html"))[0]
    d = ad.parse_detail(l, html)
    assert d.description and d.photos
    ok, price = run(ad.alive(l, FakeFetcher(default=Res(200, html, l.url))))
    assert ok and price > 0
    assert run(ad.alive(l, FakeFetcher(default=Res(410)))) == (False, None)

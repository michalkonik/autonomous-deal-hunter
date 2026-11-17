"""Valuation: weighted exit-price distributions per key, condition and channel."""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from dh import config, ledger, valuation
from dh.models import CompRow, Extraction, Listing
from dh.store import Store

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 10, 29)
NOW = datetime(2026, 10, 29, 23, 59, 59, tzinfo=timezone.utc)
KEY = "gpu:rtx-4090:24"


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "dh.db")
    yield s
    s.close()


def comp(store, price, kind="ebay_sold", days=1.0, channel="allegro", condition="used", ref=None):
    store.add_comp(KEY, condition, channel, Decimal(price), ago(days), kind, ref or f"{kind}:{price}:{days}")


def lst(sid, price, *, source="allegro_lokalnie", currency="PLN", title=None):
    return Listing(source=source, source_id=sid, url=f"https://example.test/{source}/{sid}",
                   title=sid if title is None else title, description="",
                   price=Decimal(price), currency=currency, country=None, city=None, lat=None, lon=None,
                   seller_hash=None, seller_age_days=None, seller_ratings=None, seller_score=None, ships=True,
                   pickup_only=False, is_business=False, photos=[], posted_at=None, seen_at=NOW)


def extract_all(store, condition="used"):
    for r in store.conn.execute("select id from listings").fetchall():
        store.save_extraction(r["id"], Extraction(key=KEY, category="gpu", certainty=0.95, condition=condition))


class FakeFx:
    """Rates per (currency, day): proves conversion happens at the comp date, not today."""

    def __init__(self, rates: dict):
        self.rates = {k: Decimal(v) for k, v in rates.items()}
        self.calls: list = []

    async def rate(self, ccy, day):
        self.calls.append((ccy, day))
        return self.rates[(ccy, day)]

    async def rate_with_date(self, ccy, day):
        return await self.rate(ccy, day), day


def put_rate(store, day, ccy, rate, effective=None):
    store.conn.execute("insert or replace into fx (date, currency, rate_pln, effective_date) values (?,?,?,?)",
                       (day.isoformat(), ccy, rate, (effective or day).isoformat()))
    store.conn.commit()


# ------------------------------------------------------------------ quantiles


def test_weighted_quantiles():
    q = valuation.weighted_quantile
    D = Decimal
    # equal weights: midpoint interpolation reproduces the ordinary median
    assert q([D(1), D(2), D(3), D(4)], [1, 1, 1, 1], 0.5) == D("2.5")
    assert q([D(1), D(2), D(3), D(4)], [1, 1, 1, 1], 0.25) == D("1.5")
    assert q([D(4), D(1), D(3), D(2)], [1, 1, 1, 1], 0.5) == D("2.5")  # order does not matter
    assert q([D(7)], [0.2], 0.25) == D(7)
    # ends clamp
    assert q([D(1), D(2)], [1, 1], 0.0) == D(1) and q([D(1), D(2)], [1, 1], 1.0) == D(2)
    # a heavy sale pulls the median toward it: positions 0.5/1.4, 1.1/1.4, 1.3/1.4
    m = q([D(100), D(200), D(300)], [1.0, 0.2, 0.2], 0.5)
    assert m == pytest.approx(Decimal("133.333333"), abs=Decimal("0.001"))
    with pytest.raises(ValueError):
        q([], [], 0.5)


def test_estimate_weights_by_kind(store):
    for i in range(3):
        comp(store, "3000", "ebay_sold", days=1 + i)
        comp(store, "4000", "active_ask", days=1 + i)  # ×0.92 = 3680, weight 0.3
    e = valuation.estimate(store, KEY, "used", "allegro", DAY)
    assert e is not None and e.channel == "allegro" and e.key == KEY and e.condition == "used"
    assert e.median_pln == Decimal("3000.00")  # 3 sales outweigh 3 asks
    assert Decimal("3000") < e.p75_pln < Decimal("3680")
    assert e.n_effective == pytest.approx(3.9) and e.n_sold == 3 and e.n_sold_effective == pytest.approx(3.0)
    assert e.newest_days == pytest.approx(1.0)
    assert {c.price_pln for c in e.comps if c.kind == "active_ask"} == {Decimal("3680.00")}
    assert {c.weight for c in e.comps if c.kind == "active_ask"} == {0.3}
    assert e.ceiling_pln is None and e.notes == []


def test_owner_sale_and_ask_history_weights(store):
    comp(store, "3000", "owner_sale")
    comp(store, "5000", "ask_history")
    e = valuation.estimate(store, KEY, "used", "allegro", DAY)
    assert {(c.kind, c.weight) for c in e.comps} == {("owner_sale", 1.0), ("ask_history", 0.2)}
    assert e.n_effective == pytest.approx(1.2) and e.n_sold == 1


def test_ceiling_caps_estimate(store):
    for i in range(3):
        comp(store, "5000", "ebay_sold", days=1 + i)
    comp(store, "4800", "retail_new", days=2, condition="new", channel="ceneo")
    comp(store, "4900", "retail_new", days=3, condition="new", channel="geizhals")
    e = valuation.estimate(store, KEY, "used", "allegro", DAY)
    assert e.ceiling_pln == Decimal("4560.00")  # lowest new price × 0.95
    assert e.p25_pln == e.median_pln == e.p75_pln == Decimal("4560.00")
    assert "capped_at_ceiling" in e.notes
    assert all(c.kind != "retail_new" for c in e.comps)  # the ceiling is not a comparable
    # an explicit retail price (e.g. from RetailWatch) is used when lower
    e2 = valuation.estimate(store, KEY, "used", "allegro", DAY, retail_new_pln=Decimal("4000"))
    assert e2.ceiling_pln == Decimal("3800.00") and e2.median_pln == Decimal("3800.00")


def test_stale_retail_price_is_not_a_ceiling(store):
    comp(store, "5000", "ebay_sold")
    comp(store, "4000", "retail_new", days=45, condition="new", channel="ceneo")
    e = valuation.estimate(store, KEY, "used", "allegro", DAY)
    assert e.ceiling_pln is None and e.median_pln == Decimal("5000.00")


def test_expensive_asks_do_not_set_the_exit_when_a_cheap_cluster_exists(store):
    """Fifteen fantasy asks must not hide three real ones, and must not delete those real ones as 'low outliers'."""
    for i, price in enumerate(("3200", "3400", "3600")):
        comp(store, price, "active_ask", days=1, ref=f"low:{i}")
    for i in range(8):
        comp(store, "14000", "active_ask", days=1, ref=f"high:{i}")
    e = valuation.estimate(store, KEY, "used", "allegro", DAY)
    assert e is not None and e.median_pln == Decimal("3128.00")  # 3400 x the 0.92 ask-to-sale ratio
    assert "outlier_dropped:8" in e.notes
    assert all(c.price_pln < Decimal("4000") for c in e.comps)


def test_outlier_ask_does_not_set_the_exit(store):
    """One price several times the rest of the set is not a benchmark. Three agreeing sales still are."""
    for i in range(3):
        comp(store, "3000", "ebay_sold", days=1 + i, ref=f"sold:{i}")
    comp(store, "20000", "active_ask", days=1, ref="swap-priced")
    e = valuation.estimate(store, KEY, "used", "allegro", DAY)
    assert e is not None and e.median_pln == Decimal("3000.00")
    assert "outlier_dropped:1" in e.notes
    assert all(c.price_pln < Decimal("4000") for c in e.comps)


def test_returns_none_when_no_comps(store):
    assert valuation.estimate(store, KEY, "used", "allegro", DAY) is None
    comp(store, "3000", days=45)  # only older than 30 days: trend material, not a valuation
    assert valuation.estimate(store, KEY, "used", "allegro", DAY) is None
    comp(store, "3000", condition="new")  # another condition
    assert valuation.estimate(store, KEY, "used", "allegro", DAY) is None
    assert valuation.estimate(store, KEY, "used", "ebay_us", DAY) is None  # unknown market: own channel only
    comp(store, "3000", days=-2)  # in the future of `day`: ignored
    assert valuation.estimate(store, KEY, "used", "allegro", DAY) is None


def test_trend_sign(store):
    for d in (35, 40, 50):
        comp(store, "4000", days=d)
    for d in (1, 5, 10):
        comp(store, "3600", days=d)
    e = valuation.estimate(store, KEY, "used", "allegro", DAY)
    assert e.trend_30d == pytest.approx(-0.10)
    assert e.n_effective == pytest.approx(3.0)  # comps older than 30 days are not counted
    assert e.median_pln == Decimal("3600.00")
    assert all(c.at >= ago(30) for c in e.comps)


def test_trend_rising_and_unknown(store):
    for d in (35, 40):
        comp(store, "3000", days=d)
    comp(store, "3300", days=2)
    assert valuation.estimate(store, KEY, "used", "allegro", DAY).trend_30d == pytest.approx(0.10)


def test_trend_none_without_older_comps(store):
    comp(store, "3300", days=2)
    assert valuation.estimate(store, KEY, "used", "allegro", DAY).trend_30d is None


def test_own_channel_preferred_over_fallback(store):
    comp(store, "3000", channel="allegro")
    comp(store, "9000", channel="olx")
    e = valuation.estimate(store, KEY, "used", "allegro", DAY)
    assert e.median_pln == Decimal("3000.00") and "fallback" not in e.notes


def test_pl_fallback_pool_with_reduced_weight(store):
    """Allegro Lokalnie is the used benchmark when it has a comp. OLX is not mixed in."""
    comp(store, "3000", channel="olx")
    comp(store, "3200", "probably_sold", channel="allegro_lokalnie")
    comp(store, "9000", channel="kleinanzeigen")  # EU: not in a PL channel's pool
    e = valuation.estimate(store, KEY, "used", "allegro", DAY)
    assert e.notes == ["allegro_used"]
    assert [(c.channel, c.weight) for c in e.comps] == [("allegro_lokalnie", 0.5)]  # probably_sold × 1
    assert all(c.fallback for c in e.comps)
    assert e.n_effective == pytest.approx(0.5)
    assert e.n_sold == 1 and e.n_sold_effective == pytest.approx(0.5)
    assert e.median_pln == Decimal("3200.00")


def test_eu_fallback_converts_at_comp_date(store):
    """ebay_de has no own comps: EU ledger asks are converted to PLN at the NBP rate of the comp date."""
    src = "kleinanzeigen"
    arm = ledger.arm_id(src, "rtx 4090")
    ledger.observe(store, src, [lst("k1", "1000", source=src, currency="EUR"),
                                lst("k2", "1100", source=src, currency="EUR")], ago(1), arm=arm)
    extract_all(store)
    store.conn.execute("insert into fx (date, currency, rate_pln, effective_date) values (?,?,?,?)",
                       ((NOW - timedelta(days=2)).date().isoformat(), "EUR", "4.3000", "2026-10-27"))
    store.conn.commit()
    e = valuation.estimate(store, KEY, "used", "ebay_de", DAY)
    assert e is not None and "fallback_eu" in e.notes
    # 1000 € × 4.30 × 0.92 = 3956.00, 1100 € → 4351.60; weight 0.3 × 0.7
    assert sorted(c.price_pln for c in e.comps) == [Decimal("3956.00"), Decimal("4351.60")]
    assert {c.weight for c in e.comps} == {0.21}
    assert "fx_stale" not in e.notes  # a 1-day gap (NBP 10-27 for a 10-28 comp) is normal, not stale (<= 3 days)
    # a PL exit channel with no PL comps uses the EU pool at × 0.5
    e_pl = valuation.estimate(store, KEY, "used", "allegro", DAY)
    assert "fallback_eu" in e_pl.notes and {c.weight for c in e_pl.comps} == {0.15}


def test_missing_fx_rate_skips_comp_with_note(store):
    src = "kleinanzeigen"
    ledger.observe(store, src, [lst("k1", "1000", source=src, currency="EUR")], ago(1),
                   arm=ledger.arm_id(src, "rtx 4090"))
    extract_all(store)
    comp(store, "4000", channel="kleinanzeigen")  # stored already in PLN
    e = valuation.estimate(store, KEY, "used", "ebay_de", DAY)  # no own comps: EU fallback pool
    assert [c.channel for c in e.comps] == ["kleinanzeigen"] and len(e.comps) == 1
    assert "fx_missing:EUR" in e.notes and "fallback_eu" in e.notes
    # own-channel comps present: the EU pool is not used, so no FX note
    comp(store, "4100", channel="ebay_de")
    assert valuation.estimate(store, KEY, "used", "ebay_de", DAY).notes == []


def test_ledger_probably_sold_and_live_asks_feed_the_estimate(store):
    src = "allegro_lokalnie"
    arm = ledger.arm_id(src, "rtx 4090")
    base = [lst(f"m{i}", p, title=f"RTX 4090 24GB sample {i}") for i, p in enumerate(("3000", "3200", "3400", "3600"))]
    t0 = ago(2)
    ledger.observe(store, src, base, t0, arm=arm)
    new = lst("new", "3100", title="RTX 4090 gone")
    ledger.observe(store, src, base + [new], t0 + timedelta(hours=4), arm=arm)
    ledger.observe(store, src, base, t0 + timedelta(hours=8), arm=arm)
    ledger.observe(store, src, base, t0 + timedelta(hours=12), arm=arm)
    ledger.sweep_disappeared(store, src, t0 + timedelta(hours=12))
    extract_all(store)
    e = valuation.estimate(store, KEY, "used", "allegro_lokalnie", DAY)
    kinds = sorted((c.kind, c.price_pln) for c in e.comps)
    assert kinds[0] == ("active_ask", Decimal("2760.00"))  # 3000 × 0.92
    assert ("probably_sold", Decimal("3100.00")) in kinds and len(kinds) == 5
    assert e.n_sold == 1 and e.days_to_sell == pytest.approx(8 / 24)
    # the listing being valued is never its own comparable
    lid = store.conn.execute("select id from listings where source_id='m0'").fetchone()["id"]
    e2 = valuation.estimate(store, KEY, "used", "allegro_lokalnie", DAY, exclude_listing_ids={lid})
    assert len(e2.comps) == 4
    e3 = valuation.estimate(store, KEY, "used", "allegro_lokalnie", DAY, include_listing_asks=False)
    assert [c.kind for c in e3.comps] == ["probably_sold"]


def test_condition_none_means_any(store):
    comp(store, "3000", condition="used")
    comp(store, "3400", condition="like_new")
    assert len(valuation.estimate(store, KEY, None, "allegro", DAY).comps) == 2


def test_datetime_day_and_naive_rejected(store):
    comp(store, "3000", days=1)
    assert valuation.estimate(store, KEY, "used", "allegro", ago(0.5)) is not None
    assert valuation.estimate(store, KEY, "used", "allegro", ago(3)) is None  # the comp is in its future
    with pytest.raises(ValueError):
        valuation.estimate(store, KEY, "used", "allegro", datetime(2026, 10, 29, 12))


def test_params_and_channels_from_repo_config():
    cfg = config.load(ROOT)
    p = valuation.Params.from_config(cfg)
    assert p.ask_to_sale_ratio == Decimal("0.92") and p.weights["probably_sold"] == 0.5
    assert (p.fallback_pl_pool, p.fallback_eu_pool_for_pl, p.fallback_eu_pool_for_eu) == (0.7, 0.5, 0.7)
    assert p.ceiling_factor == Decimal("0.95") and p.fx_max_back_days == 4
    assert p.outlier_low == Decimal("0.5") and p.outlier_high == Decimal("3.0")
    assert ledger.comp_weights() == p.weights  # one source of truth for the weights
    with pytest.raises(KeyError):
        valuation.Params.from_config(config.Config({"valuation": {"weights": {}}}))
    ch = valuation.Channels.from_config(cfg)
    assert ch.channel_of("allegro_lokalnie") == "allegro_lokalnie" and ch.market_of("allegro_lokalnie") == "PL"
    assert ch.market_of("kleinanzeigen") == "DE" and ch.market_of("ebay_de") == "DE"
    for c in ("allegro", "olx", "ebay_pl"):
        assert ch.market_of(c) == "PL"
    assert ch.channel_of("unknown_source") == "unknown_source" and ch.market_of("unknown_source") is None


def test_per_adapter_channel_override():
    cfg = config.Config({"sources": {"adapters": {"allegro_lokalnie": {"enabled": True, "channel": "allegro"}}}})
    ch = valuation.Channels.from_config(cfg)
    assert ch.channel_of("allegro_lokalnie") == "allegro"


def test_unknown_market_has_no_fallback(store):
    comp(store, "3000", channel="olx")
    assert valuation.estimate(store, KEY, "used", "ebay_us", DAY) is None


def test_comp_rows_are_ingested_in_pln(store):
    rows = [CompRow(key=KEY, condition="used", channel="ebay_de", kind="active_ask", price=Decimal("1000"),
                    currency="EUR", at=ago(1), weight=0.3, ref="https://www.ebay.de/itm/1"),
            CompRow(key=KEY, condition="used", channel="ebay_pl", kind="active_ask", price=Decimal("4000"),
                    currency="PLN", at=ago(1), weight=0.3, ref="https://www.ebay.pl/itm/2")]

    n = asyncio.run(valuation.ingest_comps(store, rows, FakeFx({("EUR", ago(1).date()): "4.25"})))
    assert n == 2
    got = sorted((r["channel"], r["price_pln"]) for r in store.comps(KEY))
    assert got == [("ebay_de", "4250.00"), ("ebay_pl", "4000.00")]


def test_ingest_refreshes_active_asks_by_ref(store):
    fx = FakeFx({("EUR", ago(3).date()): "4.20", ("EUR", ago(1).date()): "4.30"})
    ask = CompRow(key=KEY, condition="used", channel="ebay_de", kind="active_ask", price=Decimal("1000"),
                  currency="EUR", at=ago(3), weight=0.3, ref="https://www.ebay.de/itm/1")
    sold = CompRow(key=KEY, condition="used", channel="ebay_de", kind="ebay_sold", price=Decimal("900"),
                   currency="EUR", at=ago(3), weight=1.0, ref="https://www.ebay.de/itm/9")
    assert asyncio.run(valuation.ingest_comps(store, [ask, sold], fx)) == 2
    # a day later the ask is cut to 950 €: same ref, refreshed price and time, converted at the new date's rate
    cut = dataclasses.replace(ask, price=Decimal("950"), at=ago(1))
    assert asyncio.run(valuation.ingest_comps(store, [cut, sold], fx)) == 1  # the sale is not re-written
    rows = {r["kind"]: r for r in store.comps(KEY)}
    assert len(store.comps(KEY)) == 2
    assert rows["active_ask"]["price_pln"] == "4085.00" and rows["active_ask"]["sold_at"] == ago(1).isoformat()
    assert rows["ebay_sold"]["price_pln"] == "3780.00"  # 900 × 4.20 at its own date


def test_like_new_comps_price_a_used_buy_and_never_a_new_one(store):
    comp(store, "3000", condition="used")
    comp(store, "3200", condition="like_new")
    comp(store, "9000", condition="new")
    assert len(valuation.estimate(store, KEY, "used", "allegro", DAY).comps) == 2
    assert len(valuation.estimate(store, KEY, "new", "allegro", DAY).comps) == 1
    assert valuation.benchmark_condition("new") == "new"
    assert valuation.benchmark_condition("like_new") == "used"
    assert valuation.benchmark_condition("unknown") == "used"
    assert valuation.benchmark_condition(None) == "used"


def test_unknown_condition_comps_count_as_used(store):
    comp(store, "3000", condition="unknown")
    comp(store, "3200", condition="used")
    comp(store, "9000", condition="new")
    comp(store, "500", condition="for_parts")
    assert len(valuation.estimate(store, KEY, "used", "allegro", DAY).comps) == 2
    assert len(valuation.estimate(store, KEY, "unknown", "allegro", DAY).comps) == 2
    assert len(valuation.estimate(store, KEY, "new", "allegro", DAY).comps) == 1
    assert len(valuation.estimate(store, KEY, None, "allegro", DAY).comps) == 3


def _eur_listing(store, sid, price, seen):
    src = "kleinanzeigen"
    ledger.observe(store, src, [lst(sid, price, source=src, currency="EUR")], seen, arm=ledger.arm_id(src, "4090"))


def test_fx_conversion_uses_each_comps_own_date(store):
    d1, d2 = ago(2), ago(1)
    _eur_listing(store, "k1", "1000", d1)
    _eur_listing(store, "k2", "1000", d2)
    extract_all(store)
    put_rate(store, d1.date(), "EUR", "4.00")
    put_rate(store, d2.date(), "EUR", "4.50")
    e = valuation.estimate(store, KEY, "used", "kleinanzeigen", DAY)
    by_ref = {c.ref.rsplit("/", 1)[1]: c.price_pln for c in e.comps}
    assert by_ref == {"k1": Decimal("3680.00"), "k2": Decimal("4140.00")}  # × 0.92 ask discount
    assert "fx_stale" not in e.notes and "fx_missing:EUR" not in e.notes


def test_fx_rate_older_than_four_days_is_missing(store):
    _eur_listing(store, "k1", "1000", ago(1))
    extract_all(store)
    put_rate(store, ago(6).date(), "EUR", "4.30")
    assert valuation.estimate(store, KEY, "used", "kleinanzeigen", DAY) is None
    put_rate(store, ago(5).date(), "EUR", "4.30")  # comp date - 4 days: accepted, stale
    e = valuation.estimate(store, KEY, "used", "kleinanzeigen", DAY)
    assert e.notes == ["fx_stale"]


def test_fallback_tier_is_decided_after_fx(store):
    """The own channel has only an unconvertible EUR ask, so the EU pool is used, not an empty own pool."""
    _eur_listing(store, "k1", "1000", ago(1))
    extract_all(store)
    comp(store, "4000", channel="olx")
    e = valuation.estimate(store, KEY, "used", "kleinanzeigen", DAY)
    assert e is not None and "fallback_eu" in e.notes and "fx_missing:EUR" in e.notes
    assert [c.channel for c in e.comps] == ["olx"] and e.comps[0].weight == 0.7


def test_pl_tiers_prefer_pl_pool_over_eu(store):
    comp(store, "3000", channel="kleinanzeigen")
    e = valuation.estimate(store, KEY, "used", "allegro", DAY)
    assert e.notes == ["fallback_eu"] and e.comps[0].weight == 0.5 and e.n_sold_effective == pytest.approx(0.5)
    comp(store, "3500", channel="sprzedajemy")
    e = valuation.estimate(store, KEY, "used", "allegro", DAY)
    assert e.notes == ["fallback_pl"] and [c.channel for c in e.comps] == ["sprzedajemy"]
    comp(store, "3700", channel="allegro")
    e = valuation.estimate(store, KEY, "used", "allegro", DAY)
    assert e.notes == [] and [c.channel for c in e.comps] == ["allegro"]


def test_eu_exit_pool_includes_pl(store):
    comp(store, "3000", channel="olx")
    e = valuation.estimate(store, KEY, "used", "ebay_de", DAY)
    assert e.notes == ["fallback_eu"] and e.comps[0].weight == 0.7


def test_used_exit_prefers_allegro_over_ebay_asks_and_the_new_ceiling(store):
    """eBay asks and the new-retail ceiling do not price a used exit once Allegro Lokalnie has used comps."""
    for price in ("50000", "52000", "54000"):
        comp(store, price, "active_ask", channel="ebay_pl", condition="unknown", ref=f"ebay:{price}")
    comp(store, "41251.51", "retail_new", days=2, condition="new", channel="ceneo", ref="retail")
    for price in ("24000", "26000", "29000", "32000"):
        comp(store, price, "ebay_sold", channel="allegro_lokalnie", condition="used", ref=f"al:{price}")
    e = valuation.estimate(store, KEY, "used", "ebay_pl", DAY)
    assert e.notes == ["allegro_used"]
    assert {c.channel for c in e.comps} == {"allegro_lokalnie"}
    assert e.median_pln == Decimal("27500.00")  # midpoint of 26000 and 29000
    assert e.ceiling_pln == Decimal("39188.93")  # 41251.51 × 0.95, and it does not replace the used price
    assert e.p25_pln < e.ceiling_pln and "capped_at_ceiling" not in e.notes


def test_one_allegro_comp_is_not_topped_up_from_ebay(store):
    for price in ("5000", "5200", "5400"):
        comp(store, price, "ebay_sold", channel="ebay_pl", condition="used", ref=f"fill:{price}")
    comp(store, "1800", "ebay_sold", channel="allegro_lokalnie", condition="used", ref="only-al")
    e = valuation.estimate(store, KEY, "used", "ebay_pl", DAY)
    assert e.notes == ["allegro_used"]
    assert [c.channel for c in e.comps] == ["allegro_lokalnie"]
    assert e.median_pln == Decimal("1800.00")


def test_new_exit_prefers_allegro_new_asks(store):
    for price in ("4000", "4200", "4500"):
        comp(store, price, "ebay_sold", channel="ebay_pl", condition="new", ref=f"ebay-new:{price}")
    for price in ("2500", "2600", "2700"):
        comp(store, price, "ebay_sold", channel="allegro_lokalnie", condition="new", ref=f"al-new:{price}")
    e = valuation.estimate(store, KEY, "new", "olx", DAY)
    assert e.notes == ["allegro_new"]
    assert {c.channel for c in e.comps} == {"allegro_lokalnie"}
    assert e.median_pln == Decimal("2600.00")


def test_no_allegro_comp_keeps_the_channel_fallback(store):
    for price in ("50000", "52000", "54000"):
        comp(store, price, "ebay_sold", channel="ebay_pl", condition="used", ref=f"ebay-own:{price}")
    e = valuation.estimate(store, KEY, "used", "ebay_pl", DAY)
    assert e.notes == [] and {c.channel for c in e.comps} == {"ebay_pl"}
    assert e.median_pln == Decimal("52000.00")


def test_warm_fx_uses_callers_params(store):
    _eur_listing(store, "k1", "1000", ago(5))
    extract_all(store)
    fx = FakeFx({("EUR", ago(5).date()): "4.30"})
    assert asyncio.run(valuation.warm_fx(store, fx, KEY, "used", DAY)) == 0  # 5-day-old ask: not live by default
    wide = dataclasses.replace(valuation.default_params(), active_ask_max_age_days=10)
    assert asyncio.run(valuation.warm_fx(store, fx, KEY, "used", DAY, params=wide)) == 1
    assert fx.calls == [("EUR", ago(5).date())]


def test_trend_window_follows_lookback(store):
    comp(store, "4000", days=50)
    comp(store, "3600", days=2)
    assert valuation.estimate(store, KEY, "used", "allegro", DAY).trend_30d == pytest.approx(-0.10)
    short = dataclasses.replace(valuation.default_params(), lookback_days=45)
    assert valuation.estimate(store, KEY, "used", "allegro", DAY, params=short).trend_30d is None


# ------------------------------------------------------------------ deferred minor: the scope of the FX notes


def _eur_row(days, ref, price="1000"):
    return CompRow(key=KEY, condition="used", channel="kleinanzeigen", kind="probably_sold", price=Decimal(price),
                   currency="EUR", at=ago(days), weight=0.5, ref=ref)


def test_fx_notes_cover_only_the_recent_comps(store):
    """fx_missing / fx_stale were raised by comps older than recent_days, which never reach the quantiles."""
    recent = [_eur_row(1, "r1"), _eur_row(2, "r2")]
    put_rate(store, ago(1).date(), "EUR", "4.30")
    put_rate(store, ago(2).date(), "EUR", "4.30")
    old_missing = _eur_row(40, "old1")  # trend window only, and no rate cached for its day
    e = valuation.estimate(store, KEY, "used", "kleinanzeigen", DAY, ledger_rows=[*recent, old_missing])
    assert e is not None and e.notes == [], e.notes
    put_rate(store, ago(45).date(), "EUR", "4.10")  # an old comp converted with a 4-day-old rate: still no note
    e = valuation.estimate(store, KEY, "used", "kleinanzeigen", DAY, ledger_rows=[*recent, _eur_row(41, "old2")])
    assert e.notes == [], e.notes
    e = valuation.estimate(store, KEY, "used", "kleinanzeigen", DAY,
                           ledger_rows=[*recent, _eur_row(3, "r3")])  # a RECENT comp without a rate: noted
    assert "fx_missing:EUR" in e.notes


@pytest.mark.parametrize("gap,stale", [(0, False), (2, False), (3, False), (4, True)])
def test_fx_stale_only_when_the_rate_is_older_than_fx_stale_days(store, gap, stale):
    put_rate(store, ago(1).date(), "EUR", "4.30", effective=ago(1 + gap).date())
    e = valuation.estimate(store, KEY, "used", "kleinanzeigen", DAY, ledger_rows=[_eur_row(1, "r1")])
    assert ("fx_stale" in e.notes) is stale, (gap, e.notes)

"""Valuation: weighted exit-price distributions per key, condition and channel."""
from __future__ import annotations

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


def lst(sid, price, *, source="allegro_lokalnie", currency="PLN"):
    return Listing(source=source, source_id=sid, url=f"https://example.test/{source}/{sid}", title=sid, description="",
                   price=Decimal(price), currency=currency, country=None, city=None, lat=None, lon=None,
                   seller_hash=None, seller_age_days=None, seller_ratings=None, seller_score=None, ships=True,
                   pickup_only=False, is_business=False, photos=[], posted_at=None, seen_at=NOW)


def extract_all(store, condition="used"):
    for r in store.conn.execute("select id from listings").fetchall():
        store.save_extraction(r["id"], Extraction(key=KEY, category="gpu", certainty=0.95, condition=condition))


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
    assert e.n_effective == pytest.approx(3.9) and e.n_sold == 3
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


def test_returns_none_when_no_comps(store):
    assert valuation.estimate(store, KEY, "used", "allegro", DAY) is None
    comp(store, "3000", days=45)  # only older than 30 days: trend material, not a valuation
    assert valuation.estimate(store, KEY, "used", "allegro", DAY) is None
    comp(store, "3000", condition="new")  # another condition
    assert valuation.estimate(store, KEY, "used", "allegro", DAY) is None
    comp(store, "3000", channel="kleinanzeigen")  # an EU market cannot value a PL exit channel
    assert valuation.estimate(store, KEY, "used", "allegro", DAY) is None
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
    comp(store, "3000", channel="olx")
    comp(store, "3200", "probably_sold", channel="allegro_lokalnie")
    comp(store, "9000", channel="kleinanzeigen")  # EU: not in a PL channel's pool
    e = valuation.estimate(store, KEY, "used", "allegro", DAY)
    assert "fallback" in e.notes
    assert sorted((c.channel, c.weight) for c in e.comps) == [("allegro_lokalnie", 0.35), ("olx", 0.7)]
    assert all(c.fallback for c in e.comps)
    assert e.n_effective == pytest.approx(1.05)


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
    assert e is not None and "fallback" in e.notes
    # 1000 € × 4.30 × 0.92 = 3956.00, 1100 € → 4351.60; weight 0.3 × 0.7
    assert sorted(c.price_pln for c in e.comps) == [Decimal("3956.00"), Decimal("4351.60")]
    assert {c.weight for c in e.comps} == {0.21}
    # the same EU asks never value a PL exit channel
    assert valuation.estimate(store, KEY, "used", "allegro", DAY) is None


def test_missing_fx_rate_skips_comp_with_note(store):
    src = "kleinanzeigen"
    ledger.observe(store, src, [lst("k1", "1000", source=src, currency="EUR")], ago(1),
                   arm=ledger.arm_id(src, "rtx 4090"))
    extract_all(store)
    comp(store, "4000", channel="kleinanzeigen")  # stored already in PLN
    e = valuation.estimate(store, KEY, "used", "ebay_de", DAY)  # no own comps: EU fallback pool
    assert [c.channel for c in e.comps] == ["kleinanzeigen"] and len(e.comps) == 1
    assert "fx_missing:EUR" in e.notes and "fallback" in e.notes
    # own-channel comps present: the EU pool is not used, so no FX note
    comp(store, "4100", channel="ebay_de")
    assert valuation.estimate(store, KEY, "used", "ebay_de", DAY).notes == []


def test_ledger_probably_sold_and_live_asks_feed_the_estimate(store):
    src = "allegro_lokalnie"
    arm = ledger.arm_id(src, "rtx 4090")
    base = [lst(f"m{i}", p) for i, p in enumerate(("3000", "3200", "3400", "3600"))]
    t0 = ago(2)
    ledger.observe(store, src, base, t0, arm=arm)
    new = lst("new", "3100")
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
    assert p.fallback_factor == 0.7 and p.ceiling_factor == Decimal("0.95")
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

    class FakeFx:
        async def rate(self, ccy, day):
            return {"EUR": Decimal("4.25")}[ccy]

    import asyncio
    n = asyncio.run(valuation.ingest_comps(store, rows, FakeFx()))
    assert n == 2
    got = sorted((r["channel"], r["price_pln"]) for r in store.comps(KEY))
    assert got == [("ebay_de", "4250.00"), ("ebay_pl", "4000.00")]
    assert asyncio.run(valuation.ingest_comps(store, rows, FakeFx())) == 0  # idempotent by (kind, ref)


def test_unknown_condition_comps_count_as_used(store):
    comp(store, "3000", condition="unknown")
    comp(store, "3200", condition="used")
    comp(store, "9000", condition="new")
    comp(store, "500", condition="for_parts")
    assert len(valuation.estimate(store, KEY, "used", "allegro", DAY).comps) == 2
    assert len(valuation.estimate(store, KEY, "unknown", "allegro", DAY).comps) == 2
    assert len(valuation.estimate(store, KEY, "new", "allegro", DAY).comps) == 1
    assert len(valuation.estimate(store, KEY, None, "allegro", DAY).comps) == 3

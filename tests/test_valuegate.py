"""Value gate (dh/valuegate.py): the evidence-and-edge screen. Market-data collection against the comps
store, verdict decisions, and the scheduler mapping (keep / tier-5 watch / drop)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from dh import scheduler, valuegate
from dh.store import Store

import test_scheduler as ts

NOW = datetime.now(timezone.utc).replace(microsecond=0)
KEY = ts.KEY
P = valuegate.GateParams()


def seed(st, asks=(), sold=(), retail=(), condition="used", days=3):
    for i, p in enumerate(asks):
        st.add_comp(KEY, condition, "allegro_lokalnie", Decimal(p), NOW - timedelta(days=days), "active_ask", f"a:{i}")
    for i, p in enumerate(sold):
        st.add_comp(KEY, condition, "olx", Decimal(p), NOW - timedelta(days=days), "owner_sale", f"s:{i}")
    for i, p in enumerate(retail):
        st.add_comp(KEY, "new", "allegro", Decimal(p), NOW - timedelta(days=days), "retail_new", f"r:{i}")


def make_round(tmp_path):
    ctx = ts.make_ctx(tmp_path, ts.FakeAdapter([]), None)
    return scheduler._Round(ctx), ctx.store


# ------------------------------------------------------------------ collection

def test_collect_cleans_junk_and_counts_kinds(tmp_path):
    st = Store(tmp_path / "t.db")
    seed(st, asks=["40000"] * 15 + ["1000", "1500"], sold=["31000", "32000", "33000"], retail=["48000"])
    md = valuegate.collect(st, KEY, "used", NOW, P)
    assert md.n_junk == 2 and len(md.asks) == 15  # 1000/1500 are variation traps, not supply
    assert md.ask_median == Decimal("40000") and md.retail_floor == Decimal("48000")
    assert md.n_sold == 3 and md.sold_recent


def test_collect_dedupes_asks_by_ref_and_skips_other_conditions(tmp_path):
    st = Store(tmp_path / "t.db")
    st.add_comp(KEY, "used", "allegro_lokalnie", Decimal("30000"), NOW, "active_ask", "same")
    st.add_comp(KEY, "used", "allegro_lokalnie", Decimal("29000"), NOW - timedelta(days=1), "active_ask", "same")
    st.add_comp(KEY, "new", "allegro_lokalnie", Decimal("20000"), NOW, "active_ask", "fresh")  # not our condition
    md = valuegate.collect(st, KEY, "used", NOW, P)
    assert md.asks == [Decimal("29000")]  # cheapest sighting per ref; the new-condition ask is out


def test_collect_includes_a_live_listing_ask_the_comps_table_missed(tmp_path):
    """Retail rows alone must not look like a market with no asks. The live listing is an ask."""
    from dh.models import Extraction, Listing
    st = Store(tmp_path / "t.db")
    seed(st, retail=["10000"])
    listing = Listing("allegro_lokalnie", "live", "https://example.test/live", "RTX 3060 12GB", "",
                      Decimal("2500"), "PLN", "PL", None, None, None, None, None, None, None, None, False, None,
                      [], None, NOW)
    lid = st.upsert_listing(listing)
    st.save_extraction(lid, Extraction(key=KEY, category="gpu", certainty=0.95, condition="used"))
    md = valuegate.collect(st, KEY, "used", NOW, P)
    assert md.asks == [Decimal("2500")] and md.retail_floor == Decimal("10000")


def test_foreign_buy_is_compared_in_pln(tmp_path):
    """2000 EUR is not a 2000 PLN buy. At 4.40 the card is 8800 PLN and no longer clears the retail discount."""
    from datetime import date
    from types import SimpleNamespace
    rnd, st = make_round(tmp_path)
    seed(st, asks=["9000"] * 3, retail=["10000"])
    day = date(2026, 11, 12)
    a = _appraisal(buy="2000")
    a.inp = SimpleNamespace(buy_price=Decimal("2000"), buy_ccy="EUR", day=day)
    a.fxr = SimpleNamespace(served={("EUR", day): (Decimal("4.40"), day)})
    out = rnd._value_gate(a)
    assert out is None and rnd.stats.dropped.get("value_gate_at_market") == 1  # 8800 vs asks at 9000


def test_collect_ignores_stale_rows(tmp_path):
    st = Store(tmp_path / "t.db")
    st.add_comp(KEY, "used", "allegro_lokalnie", Decimal("30000"), NOW - timedelta(days=120), "active_ask", "old")
    md = valuegate.collect(st, KEY, "used", NOW, P)
    assert md.asks == [] and md.n_sold == 0


# ------------------------------------------------------------------ verdicts

def md_of(asks=(), retail=None, n_sold=0, sold_recent=True):
    return valuegate.MarketData(asks=[Decimal(a) for a in asks], retail_floor=Decimal(retail) if retail else None,
                                n_sold=n_sold, sold_recent=sold_recent)


def test_under_200_usd_is_dropped():
    md = md_of(asks=["36000", "37000"], n_sold=3)
    v = valuegate.verdict(Decimal("30000"), md, P, cons_usd=Decimal("199"), capital_usd=Decimal("1500"))
    assert v.action == "drop" and v.code == "small_deal"


def test_thin_margin_demotes_a_keep():
    md = md_of(asks=["36000", "37000"], n_sold=3)  # 30000 is far enough under the low ask that price is not the issue
    v = valuegate.verdict(Decimal("30000"), md, P, cons_usd=Decimal("220"), capital_usd=Decimal("5000"))
    assert v.action == "watch" and v.code == "thin_margin" and "thin margin" in v.note


def test_return_at_the_bar_still_keeps():
    md = md_of(asks=["36000", "37000"], n_sold=3)
    v = valuegate.verdict(Decimal("30000"), md, P, cons_usd=Decimal("200"), capital_usd=Decimal("1500"))
    assert v.action == "keep"


def test_keep_on_sold_evidence_and_scarce_supply():
    md = md_of(asks=["36000", "37000"], n_sold=3)
    v = valuegate.verdict(Decimal("30000"), md, P)
    assert v.action == "keep" and "sold comps" in v.note


def test_keep_on_retail_discount_and_deep_discount():
    md = md_of(asks=["40000"] * 12, retail="48000")
    assert valuegate.verdict(Decimal("30000"), md, P).action == "keep"  # under the asks and under new


def test_no_market_data_does_not_invent_a_tier():
    v = valuegate.verdict(Decimal("3200"), md_of(), P)
    assert v.action == "keep" and v.code == "gate_no_data"


def test_the_only_ask_being_the_listing_is_not_a_deal():
    v = valuegate.verdict(Decimal("3200"), md_of(asks=["3200"]), P)
    assert v.action == "drop" and v.code == "no_benchmark"


def test_at_the_ask_is_not_a_deal():
    """Matching the cheapest open asks is the market, even with no sold comps to prove it."""
    md = md_of(asks=["1000", "1030", "1060", "1090", "1120"])
    v = valuegate.verdict(Decimal("1000"), md, P)
    assert v.action == "drop" and v.code == "at_market"
    # sales in hand are the exit, so a buy near the asks is not thrown out for that alone
    near = md_of(asks=["30500", "30800"], n_sold=3)
    assert valuegate.verdict(Decimal("30000"), near, P).action == "keep"


def test_watch_when_cheap_but_unproven():
    md = md_of(asks=["1400", "1450", "1500", "1550", "1600"])  # 1000 is under the low ask, but nothing has sold
    v = valuegate.verdict(Decimal("1000"), md, P)
    assert v.action == "watch" and v.code == "gate_watch"


def test_above_the_low_asks_is_not_a_pushed_deal():
    md = md_of(asks=["600"] * 12 + ["610"] * 6, retail="800", n_sold=3, sold_recent=True)
    v = valuegate.verdict(Decimal("650"), md, P)  # sales exist, but many asks are already cheaper
    assert v.action == "watch"


def test_drop_market_price_on_liquid_pool():
    md = md_of(asks=[str(800 + i * 10) for i in range(29)])  # median 940, 15 cheaper than a 1000 buy
    v = valuegate.verdict(Decimal("1000"), md, P)
    assert v.action == "drop" and v.code == "market_price"


def test_under_retail_but_above_live_asks_is_not_a_deal():
    md = md_of(asks=[str(800 + i * 10) for i in range(29)], retail="1300")  # 1000 < shop, but many asks are 800
    assert valuegate.verdict(Decimal("1000"), md, P).action == "drop"


def test_a_thin_pool_at_the_ask_is_still_the_market():
    md = md_of(asks=["600", "610", "620"])
    assert valuegate.verdict(Decimal("615"), md, P).action == "drop"


def test_drop_variation_trap_buy():
    md = md_of(asks=["40000"] * 10, retail="48000")
    v = valuegate.verdict(Decimal("500"), md, P)  # a bracket riding a 5090 listing
    assert v.action == "drop" and v.code == "junk_ask"


def test_disabled_gate_keeps_everything():
    off = valuegate.GateParams(enabled=False)
    assert valuegate.verdict(Decimal("1000"), md_of(asks=["900"] * 20), off).action == "keep"


def test_from_valuation_defaults_without_section():
    assert valuegate.GateParams.from_valuation({}) == P
    p = valuegate.GateParams.from_valuation({"gate": {"enabled": False, "max_cheaper": 5, "junk_fraction": 0.2}})
    assert not p.enabled and p.max_cheaper == 5 and p.junk_fraction == Decimal("0.2")


# ------------------------------------------------------------------ scheduler mapping

def _appraisal(key=KEY, condition="used", buy="1000"):
    """The minimal Appraisal the gate reads: key, condition, buy price, tier, why."""
    from types import SimpleNamespace
    from dh import tiering
    from dh.models import Extraction
    return SimpleNamespace(ex=Extraction(key=key, category="gpu" if key else None, certainty=0.9, condition=condition),
                           inp=SimpleNamespace(buy_price=Decimal(buy)), why=[],
                           tier=tiering.TierResult(tier=2, star=False, floor_usd=Decimal("0")))


def test_value_gate_watch_sets_tier_five(tmp_path):
    rnd, st = make_round(tmp_path)
    seed(st, asks=["1400", "1450", "1500", "1550", "1600"])
    a = _appraisal()
    out = rnd._value_gate(a)
    assert out is a and a.tier.tier == 5 and any("value gate watch" in r for r in a.why)


def test_value_gate_drop_returns_none(tmp_path):
    rnd, st = make_round(tmp_path)
    seed(st, asks=[str(800 + i * 10) for i in range(20)])
    out = rnd._value_gate(_appraisal())
    assert out is None and rnd.stats.dropped.get("value_gate_market_price") == 1


def test_value_gate_keep_passes_through(tmp_path):
    rnd, st = make_round(tmp_path)
    seed(st, asks=["1600"], sold=["1400", "1450", "1500"])
    a = _appraisal()
    assert rnd._value_gate(a) is a and a.tier.tier == 2 and rnd.stats.dropped == {}


def test_value_gate_skips_pc_bundles(tmp_path):
    rnd, st = make_round(tmp_path)
    seed(st, asks=[str(800 + i * 10) for i in range(20)])
    a = _appraisal(key="pc:bundle")
    assert rnd._value_gate(a) is a and a.tier.tier == 2


def test_thin_margin_on_a_pc_is_a_watch_and_a_real_one_stays(tmp_path):
    rnd, _st = make_round(tmp_path)
    small = _appraisal(key="pc:bundle")
    small.cons_usd, small.capital_usd = Decimal("40"), Decimal("2000")
    assert rnd._value_gate(small) is None and rnd.stats.dropped.get("value_gate_small_deal") == 1
    thin = _appraisal(key="pc:bundle")
    thin.cons_usd, thin.capital_usd = Decimal("220"), Decimal("5000")
    assert rnd._value_gate(thin).tier.tier == 5 and any("thin margin" in r for r in thin.why)
    fat = _appraisal(key="pc:bundle")
    fat.cons_usd, fat.capital_usd = Decimal("400"), Decimal("2000")
    assert rnd._value_gate(fat).tier.tier == 2


def test_value_gate_skips_keyless_pool_fallback_appraisals(tmp_path):
    rnd, st = make_round(tmp_path)
    seed(st, asks=[str(800 + i * 10) for i in range(20)])
    a = _appraisal(key=None)
    assert rnd._value_gate(a) is a and a.tier.tier == 2 and rnd.gate_cache == {}


def test_value_gate_cache_is_key_and_condition_scoped(tmp_path):
    rnd, st = make_round(tmp_path)
    seed(st, asks=["1000"] * 20)
    rnd._value_gate(_appraisal())
    assert (KEY, "used") in rnd.gate_cache
    seed(st, asks=["2000", "2100"], condition="like_new")
    rnd._value_gate(_appraisal(condition="like_new"))  # like-new is priced against the used pool
    assert (KEY, "like_new") not in rnd.gate_cache
    assert rnd.gate_cache[(KEY, "used")].asks == [Decimal("1000")] * 20
    seed(st, asks=["3000"], condition="new")
    rnd._value_gate(_appraisal(condition="new"))
    assert (KEY, "new") in rnd.gate_cache and rnd.gate_cache[(KEY, "used")].asks == [Decimal("1000")] * 20


def _new_listing(title="", description=""):
    from types import SimpleNamespace
    return SimpleNamespace(title=title, description=description)


def test_paperless_new_at_the_shop_price_is_a_watch(tmp_path):
    rnd, st = make_round(tmp_path)
    seed(st, asks=["12000"], sold=["11000", "11500", "11800"], retail=["10000"], condition="new")
    a = _appraisal(condition="new", buy="10000")
    a.listing = _new_listing("RTX 3060 12GB")
    out = rnd._value_gate(a)
    assert out is a and a.tier.tier == 5 and any("shop price" in r for r in a.why)


def test_paperless_new_below_the_shop_price_stays(tmp_path):
    rnd, st = make_round(tmp_path)
    seed(st, asks=["9000"], retail=["10000"], condition="new")
    a = _appraisal(condition="new", buy="8000")
    a.listing = _new_listing("RTX 3060 12GB")
    assert rnd._value_gate(a).tier.tier == 2


def test_new_with_a_guarantee_is_not_forced_under_the_shop_price(tmp_path):
    rnd, st = make_round(tmp_path)
    seed(st, asks=["12000"], sold=["11000", "11500", "11800"], retail=["10000"], condition="new")
    a = _appraisal(condition="new", buy="10000")
    a.listing = _new_listing("RTX 3060 12GB", "gwarancja sklepu")
    assert rnd._value_gate(a).tier.tier == 2


def test_paperless_new_without_a_shop_price_is_a_watch(tmp_path):
    rnd, st = make_round(tmp_path)
    seed(st, asks=["5000"], sold=["4000", "4200", "4300"], condition="new")
    a = _appraisal(condition="new", buy="2000")
    a.listing = _new_listing("RTX 3060 12GB")
    out = rnd._value_gate(a)
    assert out.tier.tier == 5 and any("no shop price" in r for r in a.why)


def test_a_new_pc_skips_the_shop_price_rule(tmp_path):
    rnd, _st = make_round(tmp_path)
    a = _appraisal(key="pc:bundle", condition="new")
    a.listing = _new_listing("Komputer gamingowy")
    assert rnd._value_gate(a).tier.tier == 2

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


def test_collect_ignores_stale_rows(tmp_path):
    st = Store(tmp_path / "t.db")
    st.add_comp(KEY, "used", "allegro_lokalnie", Decimal("30000"), NOW - timedelta(days=120), "active_ask", "old")
    md = valuegate.collect(st, KEY, "used", NOW, P)
    assert md.asks == [] and md.n_sold == 0


# ------------------------------------------------------------------ verdicts

def md_of(asks=(), retail=None, n_sold=0, sold_recent=True):
    return valuegate.MarketData(asks=[Decimal(a) for a in asks], retail_floor=Decimal(retail) if retail else None,
                                n_sold=n_sold, sold_recent=sold_recent)


def test_keep_on_sold_evidence_and_scarce_supply():
    md = md_of(asks=["30500", "30800"], n_sold=3)
    v = valuegate.verdict(Decimal("30000"), md, P)
    assert v.action == "keep" and "sold comps" in v.note


def test_keep_on_retail_discount_and_deep_discount():
    md = md_of(asks=["40000"] * 12, retail="48000")  # 40000 = 0.833 of new -> evidence; deep vs median? no
    assert valuegate.verdict(Decimal("40000"), md, P).action == "keep"  # retail evidence + scarce (0 cheaper)


def test_watch_without_evidence():
    md = md_of(asks=["1000", "1030", "1060", "1090", "1120"])  # no sold, no retail: an ask pool proves nothing
    v = valuegate.verdict(Decimal("1000"), md, P)
    assert v.action == "watch" and v.code == "gate_watch"


def test_watch_when_evidence_holds_but_no_edge():
    md = md_of(asks=["600"] * 12 + ["610"] * 6, retail="800", n_sold=3, sold_recent=True)
    v = valuegate.verdict(Decimal("650"), md, P)  # buy above 18 cheaper asks: no edge; below 0.9 x retail: not noise
    assert v.action == "watch"


def test_drop_market_price_on_liquid_pool():
    md = md_of(asks=[str(800 + i * 10) for i in range(29)])  # median 940, 15 cheaper than a 1000 buy
    v = valuegate.verdict(Decimal("1000"), md, P)
    assert v.action == "drop" and v.code == "market_price"


def test_watch_not_drop_when_priced_well_below_retail():
    md = md_of(asks=[str(800 + i * 10) for i in range(29)], retail="1300")  # 1000 < 0.9 x 1300: still maybe a deal
    assert valuegate.verdict(Decimal("1000"), md, P).action == "watch"


def test_watch_not_drop_on_thin_pool():
    md = md_of(asks=["600", "610", "620"])  # fewer than min_real_asks: too thin to judge
    assert valuegate.verdict(Decimal("615"), md, P).action == "watch"


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
    seed(st, asks=["1000", "1030", "1060", "1090", "1120"])
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
    seed(st, asks=["1050"], sold=["1400", "1450", "1500"])
    a = _appraisal()
    assert rnd._value_gate(a) is a and a.tier.tier == 2 and rnd.stats.dropped == {}


def test_value_gate_skips_pc_bundles(tmp_path):
    rnd, st = make_round(tmp_path)
    seed(st, asks=[str(800 + i * 10) for i in range(20)])
    a = _appraisal(key="pc:bundle")
    assert rnd._value_gate(a) is a and a.tier.tier == 2


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
    rnd._value_gate(_appraisal(condition="like_new"))
    assert (KEY, "like_new") in rnd.gate_cache and rnd.gate_cache[(KEY, "used")].asks == [Decimal("1000")] * 20

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from dh import config, tiering
from dh.models import Extraction
from dh.risk import Risk

REPO = Path(__file__).resolve().parent.parent
RULES = config.load(REPO).rules


@dataclass
class Est:  # stand-in for valuation.ExitEstimate
    n_sold_effective: float = 20.0
    n_sold: int = 20  # display only: confidence must not read it
    days_to_sell: float | None = 5.0


def ext(**kw):
    base = dict(key="gpu:rtx-4090:24", category="gpu", certainty=0.95, condition="used")
    base.update(kw)
    return Extraction(**base)


def rk(p=0.01, hard=False):
    return Risk(p_scam=p, hard_exclude=hard)


@pytest.mark.parametrize("cons,exp,effort,conf,cap,tier,star", [
    (320, 520, "easy", "high", 2000, 1, True),
    (320, 400, "easy", "medium", 2000, 2, False),
    (60, 80, "easy", "low", 500, 3, False),
    (45, 60, "easy", "high", 500, 4, False),
    (100, 150, "moderate", "high", 900, 4, False),
    (20, 30, "easy", "high", 300, None, False),
    (900, 1200, "heavy", "high", 31000, None, False),
    # boundaries
    (300, 300, "easy", "high", 2000, 1, False),            # t1 is >=
    (299.99, 300, "easy", "high", 2000, 2, False),
    (50, 50, "easy", "low", 100, 3, False),                 # floor is >=
    (40, 40, "easy", "high", 100, 4, False),                # exactly floor * 0.8
    (39.99, 40, "easy", "high", 100, None, False),
    (400, 500, "easy", "medium", 30000, 2, True),           # star is >=, cap is strict
    (400, 500, "easy", "medium", 30000.01, None, False),
    (250, 260, "heavy", "high", 1000, 2, False),            # heavy floor 250 < t1 300
    (30, 600, "easy", "high", 100, None, False),            # no star on an unlogged deal
])
def test_assign(cons, exp, effort, conf, cap, tier, star):
    r = tiering.assign(cons, exp, effort, conf, cap, RULES)
    assert (r.tier, r.star) == (tier, star)
    assert r.floor_usd == Decimal(str(RULES.floors[effort])) and r.reasons


@pytest.mark.parametrize("effort", ["easy", "moderate", "heavy"])
@pytest.mark.parametrize("net", [20, 40, 45, 60, 99, 100, 119, 150, 240, 250, 300, 320, 900, 5000])
def test_hard_excluded_is_never_tiered(effort, net):
    level, why = tiering.confidence(ext(), Est(), rk(0.01, hard=True), 60, 1000, RULES)
    assert level == "excluded" and why[0].startswith("hard exclude")
    r = tiering.assign(net, net * 2, effort, level, 1000, RULES)
    assert r.tier is None and r.star is False and r.reasons[0].startswith("hard exclude")


def test_hard_exclude_wins_over_everything():
    # certain match, many comps, verified, low p_scam: still excluded
    risk = Risk(p_scam=0.0, hard_exclude=True, flags=["offplatform_url"])
    level, why = tiering.confidence(ext(), Est(n_sold_effective=99), risk, 0, 100, RULES)
    assert level == "excluded" and "offplatform_url" in why[0]


def test_tier4_reason_uses_configured_margin():
    rules = RULES.to_dict()
    rules = {**rules, "tiers": {**rules["tiers"], "t4_floor_margin": 0.30}}
    r = tiering.assign(36, 40, "easy", "high", 100, rules)       # 35 = 50 * 0.7
    assert r.tier == 4 and any("30%" in x for x in r.reasons)


def test_assign_reasons_and_decimal_inputs():
    r = tiering.assign(Decimal("320"), Decimal("520"), "easy", "high", Decimal("2000"), RULES)
    assert r.tier == 1 and r.star and any("high confidence" in x for x in r.reasons)
    assert tiering.assign(900, 1200, "heavy", "high", 31000, RULES).reasons == ["over capital cap (31000 > 30000 USD)"]


def test_assign_accepts_plain_dict():
    assert tiering.assign(320, 520, "easy", "high", 2000, RULES.to_dict()).tier == 1


def test_high_confidence_needs_more_comps_for_big_capital():
    est = lambda n: Est(n_sold_effective=n)  # noqa: E731
    conf = lambda n, cap: tiering.confidence(ext(), est(n), rk(), 60, cap, RULES)[0]  # noqa: E731
    assert conf(12, 5000) == "high"          # 5000 is not "over 5000"
    assert conf(11, 5000) == "medium"
    assert conf(12, 5001) == "medium"
    assert conf(20, 5001) == "high"
    assert conf(20, 15000) == "high"
    assert conf(20, 15001) == "medium"
    assert conf(30, 15001) == "high"
    assert tiering.min_comps(5000, RULES) == 12 and tiering.min_comps(5001, RULES) == 20
    assert tiering.min_comps(15001, RULES) == 30


def test_confidence_high_requires_everything():
    ok = dict(extraction=ext(), estimate=Est(), risk=rk(), verified_age_s=120, capital_usd=1000, rules=RULES)
    assert tiering.confidence(**ok) == ("high", [])
    # controller-accepted: an unknown condition is never high (it is low), a known one is required for high
    assert tiering.confidence(**{**ok, "extraction": ext(condition="unknown")})[0] == "low"
    assert tiering.confidence(**{**ok, "extraction": ext(condition="like_new")})[0] == "high"
    for change, label in [
        (dict(extraction=ext(certainty=0.89)), "certainty"),
        (dict(risk=rk(0.05)), "p_scam"),
        (dict(verified_age_s=601), "verified"),
        (dict(verified_age_s=None), "verified"),
        (dict(extraction=ext(defects=["fan noise"])), "defects"),
        (dict(estimate=Est(n_sold_effective=11)), "comparables"),
    ]:
        level, why = tiering.confidence(**{**ok, **change})
        assert level == "medium" and any(label in w for w in why), (change, level, why)
    assert tiering.confidence(**{**ok, "verified_age_s": 600})[0] == "high"   # exactly 10 min is fine
    assert tiering.confidence(**{**ok, "extraction": ext(certainty=0.9)})[0] == "high"


def test_confidence_low():
    ok = dict(extraction=ext(), estimate=Est(), risk=rk(), verified_age_s=120, capital_usd=1000, rules=RULES)
    for change in [dict(extraction=ext(certainty=0.7)), dict(extraction=ext(key=None, certainty=0.95)),
                   dict(risk=rk(0.15)), dict(extraction=ext(condition="unknown")),
                   dict(estimate=Est(n_sold_effective=5)), dict(estimate=None)]:
        level, why = tiering.confidence(**{**ok, **change})
        assert level == "low" and why, change
    assert tiering.confidence(**{**ok, "estimate": Est(n_sold_effective=6)})[0] == "medium"   # half of 12


@pytest.mark.parametrize("country,channel,flags,est,bundle,expected", [
    ("PL", "olx", [], Est(), 0, "easy"),
    (None, "allegro", [], Est(), 0, "easy"),          # missing country from a Polish marketplace: PL
    (None, "kleinanzeigen", [], Est(), 0, "moderate"),  # missing country from anywhere else: unknown, moderate
    ("PL", "allegro", [], Est(days_to_sell=14), 0, "easy"),
    ("PL", "allegro", [], Est(days_to_sell=14.1), 0, "moderate"),
    ("PL", "allegro", [], Est(days_to_sell=None), 0, "easy"),
    ("DE", "olx", ["bdo_check"], Est(), 0, "moderate"),
    ("PL", "ebay_de", [], Est(), 0, "moderate"),        # sold to a German buyer: a non-PL leg
    ("el", "olx", ["bdo_check"], Est(), 0, "moderate"),  # EL = Greece, EU
    ("US", "allegro", ["bdo_check"], Est(), 0, "heavy"),
    ("UK", "allegro", [], Est(), 0, "heavy"),
    ("CN", "allegro", [], Est(), 0, "heavy"),
    ("DE", "olx", ["needs_vat_ue", "bdo_check"], Est(), 0, "heavy"),
    ("PL", "olx", [], Est(), 2, "easy"),
    ("PL", "olx", [], Est(), 3, "heavy"),
])
def test_effort_class(country, channel, flags, est, bundle, expected):
    e = ext(bundle_items=["x"] * bundle)
    src = "kleinanzeigen" if channel == "kleinanzeigen" else "olx"
    assert tiering.effort_class(NS(country=country, source=src), channel, flags, est, extraction=e) == expected


def test_effort_class_without_extraction():
    assert tiering.effort_class(NS(country="PL"), "olx", [], Est()) == "easy"


def test_confidence_counts_effective_sold_weight_not_rows():
    """24 probably-sold rows (weight 0.5) are 12 effective sales; fallback comps count at their reduced weight."""
    ok = dict(extraction=ext(), risk=rk(), verified_age_s=120, capital_usd=1000, rules=RULES)
    assert tiering.confidence(estimate=Est(n_sold=24, n_sold_effective=12.0), **ok)[0] == "high"
    level, why = tiering.confidence(estimate=Est(n_sold=30, n_sold_effective=11.9), **ok)
    assert level == "medium" and any("11.9 effective sold comparables < 12" in w for w in why)
    assert tiering.confidence(estimate=Est(n_sold=12, n_sold_effective=5.5), **ok)[0] == "low"  # < half of 12
